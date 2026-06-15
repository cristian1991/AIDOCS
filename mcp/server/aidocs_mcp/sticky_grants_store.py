"""Sticky grants store — sqlite-backed replacement for the legacy
``.MEMORY/sessions/<sid>/sticky-grants.json`` sidecar.

Motivation (T0 security fix, backlog #15, 2026-04-25):
  - The sidecar JSON was writable by any code path that could touch
    .MEMORY/ — no config-gate coverage, no RBAC, no audit trail.
  - An injection vector (pre-2026-04-24 Layer-2 NLP tool-surfacing)
    silently wrote `bash` into the sidecar, defeating T0 check_raw_shell.
  - SQLite storage restores the "security state lives in sqlite behind
    the config gate" invariant (see security.md).

Schema: two-tier grants.
  - Tier 1 (tool-class): subcommand=NULL. Grants the tool wholesale
    for the session. Example: "you can use bash from now on".
  - Tier 2 (tool+subcommand): subcommand='opencode' etc. Narrows the
    grant so bash is permitted only when invoking that subcommand.
    bash.deny.* still trumps the grant at policy-eval time.

Lifecycle:
  register_grant() — one row per registration, with phrase + judge
                     verdict + confirmation answer captured for audit.
  active_grants_for_session() — returns rows where revoked_at IS NULL.
  revoke_grant() — sets revoked_at/by/reason. Keeps the row for audit.
  ingest_legacy_sidecar() — one-shot migration from the JSON sidecar.
                            Tokens in _STICKY_RAW_TOOL_FORBIDDEN are
                            dropped (matches query_gate.py sink filter).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase

# Mirrors query_gate._STICKY_RAW_TOOL_FORBIDDEN. Duplicated (not imported)
# so legacy ingest can sanitize without pulling query_gate into this
# module's import graph — these stores must not cycle.
_LEGACY_SIDECAR_FORBIDDEN: frozenset[str] = frozenset(
    {
        "bash",
        "grep",
        "read",
        "edit",
        "write",
        "multiedit",
        "patch",
        "apply_patch",
    },
)


@dataclass(frozen=True)
class StickyGrant:
    grant_id: str
    session_id: str
    tier: int
    tool: str
    subcommand: str | None
    phrase: str
    registered_at: str
    registered_by_user_id: str
    judge_verdict: str
    confirmation_answer: str
    revoked_at: str | None
    revoked_by_user_id: str | None
    revoked_reason: str | None


class StickyGrantsStore(SQLiteIndexStoreBase):
    """Sticky user-intent grants — one row per registration.

    Active grants for a session = rows where revoked_at IS NULL.
    Revoked rows persist for audit.
    """

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sticky_grants (
                    grant_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    subcommand TEXT,
                    phrase TEXT NOT NULL DEFAULT '',
                    registered_at TEXT NOT NULL,
                    registered_by_user_id TEXT NOT NULL DEFAULT '',
                    judge_verdict TEXT NOT NULL DEFAULT '',
                    confirmation_answer TEXT NOT NULL DEFAULT '',
                    revoked_at TEXT,
                    revoked_by_user_id TEXT,
                    revoked_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sticky_grants_session_active
                    ON sticky_grants(session_id, revoked_at);
                CREATE INDEX IF NOT EXISTS idx_sticky_grants_session_tool
                    ON sticky_grants(session_id, tool, revoked_at);
                -- Phase 3 of backlog #15: pending confirmations written
                -- when judge says require_confirm. AskUserQuestion reply
                -- resolves the pending row: yes → register_grant, no →
                -- delete. Single-turn TTL: unconfirmed pendings are
                -- cleared at the start of the next UserPromptSubmit so
                -- stale confirmations don't accumulate.
                CREATE TABLE IF NOT EXISTS sticky_grants_pending (
                    pending_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    subcommand TEXT,
                    phrase TEXT NOT NULL DEFAULT '',
                    judge_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sticky_grants_pending_session
                    ON sticky_grants_pending(session_id);
                """,
            )

    @staticmethod
    def _gen_grant_id(session_id: str, tool: str, subcommand: str | None) -> str:
        """Deterministic-ish id. Time-stamped so repeat registrations of
        the same (tool, subcommand) don't collide on the primary key —
        an operator can legitimately re-grant after revoking.
        """
        seed = f"{session_id}|{tool}|{subcommand or ''}|{time.time_ns()}"
        return "g_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def register_grant(
        self,
        project_root: Path,
        *,
        session_id: str,
        tier: int,
        tool: str,
        subcommand: str | None = None,
        phrase: str = "",
        registered_by_user_id: str = "",
        judge_verdict: str = "",
        confirmation_answer: str = "",
        registration_source: str = "unknown",
    ) -> str:
        """Insert a new grant. Returns grant_id. Callers should verify
        tier/tool/subcommand shape BEFORE calling — this store doesn't
        second-guess the judge.
        """
        if tier not in (1, 2):
            raise ValueError(f"tier must be 1 or 2, got {tier!r}")
        if not tool.strip():
            raise ValueError("tool required")
        if tier == 2 and not (subcommand or "").strip():
            raise ValueError("tier 2 requires subcommand")
        if tier == 1 and subcommand is not None and subcommand.strip():
            raise ValueError("tier 1 must not carry a subcommand")
        self.init_db(project_root)
        grant_id = self._gen_grant_id(session_id, tool, subcommand)
        registered_at = self._now()
        with self.session(project_root) as conn:
            conn.execute(
                "INSERT INTO sticky_grants (grant_id, session_id, tier, tool, "
                "subcommand, phrase, registered_at, registered_by_user_id, "
                "judge_verdict, confirmation_answer) VALUES (?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?)",
                (
                    grant_id,
                    session_id,
                    int(tier),
                    tool.strip(),
                    (subcommand.strip() if subcommand else None),
                    phrase,
                    registered_at,
                    registered_by_user_id,
                    judge_verdict,
                    confirmation_answer,
                ),
            )

        # AIDOCS sticky audit — Patch C-sticky (canonical 2026-04-29).
        # Closes invariant #37 condition #8 partial violation: register
        # is now visible in the tamper-evident execution_events chain,
        # not only in the sticky_grants table's own columns.
        #
        # Single emission site — every successful sticky-grant write
        # routes through this method, including:
        #   - Layer-1 raw-tool grants via query_gate._write_sticky
        #   - Layer-2 NLP surfacing via add_sticky_user_intent_tools
        #   - pending → active promotion via consume_pending
        #   - legacy-sidecar ingest via ingest_legacy_sidecar
        # judge_verdict + confirmation_answer in the payload let
        # auditors distinguish "operator-confirmed via popup"
        # (judge_verdict from grant_registration_judge) from
        # "legacy-write-path" (no popup, direct write).
        #
        # Best-effort emission: a failure of the audit store must NOT
        # roll back the grant write — registration succeeded above.
        # The audit row excludes `phrase` (operator prompt slice)
        # because that lives in the sticky_grants table itself; the
        # audit row carries only registration metadata so the chain
        # surfaces "what was granted, when, under which judge verdict"
        # without duplicating prompt content.
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="sticky_grant_registered",
                source_kind="sticky_grants_store.register_grant",
                session_id=session_id or None,
                capability_name=tool.strip(),
                action_kind="sticky_register",
                target_entity=(
                    f"{tool.strip()}:{subcommand.strip()}" if subcommand else tool.strip()
                ),
                status="allowed",
                payload={
                    "grant_id": grant_id,
                    "session_id": session_id,
                    "tier": int(tier),
                    "tool": tool.strip(),
                    "subcommand": (subcommand.strip() if subcommand else None),
                    "registration_source": registration_source,
                    "judge_verdict": judge_verdict,
                    "confirmation_answer": confirmation_answer,
                    "registered_by_user_id": registered_by_user_id,
                    "registered_at": registered_at,
                },
            )
        except Exception:
            pass

        return grant_id

    def active_grants_for_session(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[StickyGrant]:
        """Return non-revoked grants for the session, newest first."""
        self.init_db(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT grant_id, session_id, tier, tool, subcommand, phrase, "
                "registered_at, registered_by_user_id, judge_verdict, "
                "confirmation_answer, revoked_at, revoked_by_user_id, "
                "revoked_reason FROM sticky_grants WHERE session_id = ? "
                "AND revoked_at IS NULL ORDER BY registered_at DESC",
                (session_id,),
            ).fetchall()
        return [self._row_to_grant(r) for r in rows]

    def active_tools_for_session(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        """Flat list of tool names from tier-1 active grants.

        Callers that used the legacy sidecar's `sticky` list want this
        shape — keeps the migration at query_gate._load_sticky trivial.
        Tier-2 grants are NOT returned here — they're scope narrowers
        on a tier-1 grant, not independent tool permissions. Fetch via
        active_bash_subcommands_for_session for the subcommand slice.
        """
        grants = self.active_grants_for_session(project_root, session_id)
        return sorted({g.tool for g in grants if g.tier == 1})

    def active_bash_subcommands_for_session(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        """Flat list of subcommands from tier-2 bash-scoped grants.

        Used by bash_policy to widen the allow-table check when a
        sticky scoped grant covers the invocation.
        """
        grants = self.active_grants_for_session(project_root, session_id)
        return sorted(
            {
                str(g.subcommand)
                for g in grants
                if g.tier == 2 and g.tool == "bash" and g.subcommand
            },
        )

    def revoke_grant(
        self,
        project_root: Path,
        *,
        grant_id: str,
        revoked_by_user_id: str = "",
        revoked_reason: str = "",
    ) -> bool:
        """Mark a grant revoked. Returns True if a row was updated.
        Idempotent — revoking an already-revoked grant is a no-op.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            cur = conn.execute(
                "UPDATE sticky_grants SET revoked_at = ?, "
                "revoked_by_user_id = ?, revoked_reason = ? "
                "WHERE grant_id = ? AND revoked_at IS NULL",
                (self._now(), revoked_by_user_id, revoked_reason, grant_id),
            )
            return cur.rowcount > 0

    def revoke_tool(
        self,
        project_root: Path,
        *,
        session_id: str,
        tool: str,
        revoked_by_user_id: str = "",
        revoked_reason: str = "",
    ) -> int:
        """Revoke all active grants for (session, tool). Returns count.
        Used when the operator says 'revoke bash grant' — nukes tier-1
        and all tier-2 scopes under that tool in one sweep.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            cur = conn.execute(
                "UPDATE sticky_grants SET revoked_at = ?, "
                "revoked_by_user_id = ?, revoked_reason = ? "
                "WHERE session_id = ? AND tool = ? AND revoked_at IS NULL",
                (self._now(), revoked_by_user_id, revoked_reason, session_id, tool.strip()),
            )
            return cur.rowcount

    def ingest_legacy_sidecar(
        self,
        project_root: Path,
        session_id: str,
        *,
        legacy_phrase: str = "legacy-import-2026-04-25",
    ) -> int:
        """One-shot migration: read .MEMORY/sessions/<sid>/sticky-grants.json,
        insert each token as a Tier-1 grant, rename the file to
        <...>.legacy.json so re-runs are idempotent.

        Tokens in _LEGACY_SIDECAR_FORBIDDEN are DROPPED (not imported) —
        they're raw-tool tokens that shouldn't have been in sticky in
        the first place. Matches query_gate.py's sink filter.

        Returns count of grants inserted. Zero when the sidecar is
        absent or already migrated.
        """
        sidecar = project_root / ".MEMORY" / "sessions" / session_id / "sticky-grants.json"
        if not sidecar.is_file():
            return 0
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(raw, dict):
            return 0
        tokens = raw.get("sticky")
        if not isinstance(tokens, list):
            return 0
        clean: list[str] = []
        for t in tokens:
            s = str(t).strip()
            if not s:
                continue
            if s.lower() in _LEGACY_SIDECAR_FORBIDDEN:
                continue
            clean.append(s)
        # Dedup against already-registered active grants so a second
        # ingest (ops running this on a half-migrated tree) doesn't
        # double-write.
        existing = set(self.active_tools_for_session(project_root, session_id))
        inserted = 0
        for tool in clean:
            if tool in existing:
                continue
            self.register_grant(
                project_root,
                session_id=session_id,
                tier=1,
                tool=tool,
                phrase=legacy_phrase,
                registered_by_user_id="legacy-import",
                judge_verdict="skipped-legacy-import",
                confirmation_answer="skipped-legacy-import",
                registration_source="legacy_sidecar_migration",
            )
            inserted += 1
        # Rename sidecar to .legacy.json so subsequent reads fall
        # through to sqlite — preserved on disk for forensic review.
        try:
            legacy_path = sidecar.with_suffix(".legacy.json")
            if legacy_path.exists():
                # Tombstone collision — operator may have already
                # migrated manually. Skip rename; leave both.
                pass
            else:
                sidecar.rename(legacy_path)
        except OSError:
            pass
        return inserted

    # ── Pending-confirmation CRUD (Phase 3 of backlog #15) ──────────

    def record_pending(
        self,
        project_root: Path,
        *,
        session_id: str,
        tier: int,
        tool: str,
        subcommand: str | None = None,
        phrase: str = "",
        judge_reason: str = "",
    ) -> str:
        """Write a pending-confirmation row. Returns pending_id.

        Caller (claude_hook) surfaces judge_reason via AskUserQuestion;
        the operator's yes/no resolves the pending row next turn.
        """
        if tier not in (1, 2):
            raise ValueError(f"tier must be 1 or 2, got {tier!r}")
        if not tool.strip():
            raise ValueError("tool required")
        self.init_db(project_root)
        pending_id = (
            "p_"
            + hashlib.sha256(
                f"{session_id}|{tool}|{subcommand or ''}|{time.time_ns()}".encode(),
            ).hexdigest()[:16]
        )
        with self.session(project_root) as conn:
            conn.execute(
                "INSERT INTO sticky_grants_pending (pending_id, "
                "session_id, tier, tool, subcommand, phrase, "
                "judge_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pending_id,
                    session_id,
                    int(tier),
                    tool.strip(),
                    (subcommand.strip() if subcommand else None),
                    phrase,
                    judge_reason,
                    self._now(),
                ),
            )
        return pending_id

    def list_pending(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[dict[str, Any]]:
        self.init_db(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT pending_id, session_id, tier, tool, subcommand, "
                "phrase, judge_reason, created_at FROM sticky_grants_pending "
                "WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def consume_pending(
        self,
        project_root: Path,
        *,
        pending_id: str,
        answer: str,
        registered_by_user_id: str = "",
    ) -> StickyGrant | None:
        """Resolve a pending confirmation.

        answer='yes' → registers the grant as StickyGrant, returns it.
        answer='no' (or anything else) → deletes pending, returns None.
        Caller is expected to surface the answer to the operator.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT pending_id, session_id, tier, tool, subcommand, "
                "phrase, judge_reason FROM sticky_grants_pending "
                "WHERE pending_id = ?",
                (pending_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM sticky_grants_pending WHERE pending_id = ?",
                (pending_id,),
            )
        if (answer or "").strip().lower() not in ("yes", "y", "confirm", "approve"):
            return None
        grant_id = self.register_grant(
            project_root,
            session_id=str(row["session_id"]),
            tier=int(row["tier"]),
            tool=str(row["tool"]),
            subcommand=(str(row["subcommand"]) if row["subcommand"] else None),
            phrase=str(row["phrase"] or ""),
            registered_by_user_id=registered_by_user_id,
            judge_verdict=str(row["judge_reason"] or ""),
            confirmation_answer=answer,
            registration_source="pending_confirm",
        )
        grants = self.active_grants_for_session(
            project_root,
            str(row["session_id"]),
        )
        return next((g for g in grants if g.grant_id == grant_id), None)

    def clear_expired_pending(
        self,
        project_root: Path,
        session_id: str,
    ) -> int:
        """Drop all pending rows for a session. Called at the top of
        each UserPromptSubmit so unconfirmed pendings don't accumulate
        (single-turn TTL contract). Returns count deleted.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            cur = conn.execute(
                "DELETE FROM sticky_grants_pending WHERE session_id = ?",
                (session_id,),
            )
            return cur.rowcount

    @staticmethod
    def _row_to_grant(row: Any) -> StickyGrant:
        return StickyGrant(
            grant_id=str(row["grant_id"]),
            session_id=str(row["session_id"]),
            tier=int(row["tier"]),
            tool=str(row["tool"]),
            subcommand=(str(row["subcommand"]) if row["subcommand"] else None),
            phrase=str(row["phrase"] or ""),
            registered_at=str(row["registered_at"]),
            registered_by_user_id=str(row["registered_by_user_id"] or ""),
            judge_verdict=str(row["judge_verdict"] or ""),
            confirmation_answer=str(row["confirmation_answer"] or ""),
            revoked_at=(str(row["revoked_at"]) if row["revoked_at"] else None),
            revoked_by_user_id=(
                str(row["revoked_by_user_id"]) if row["revoked_by_user_id"] else None
            ),
            revoked_reason=(str(row["revoked_reason"]) if row["revoked_reason"] else None),
        )
