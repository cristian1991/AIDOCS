"""Session freeze store — UX/control state layer for #39 confirm pipeline.

When the judge classifies a tool call as confirmable_destructive, the
hook creates an escalation request (existing primitive in
`escalation_store`) AND a freeze row here. The freeze row owns the
"this session is frozen, no other tools, no continued work" UX
contract. The escalation row owns the approval/grant backend.

Two concepts, two layers, one bridge: every freeze row points at an
escalation request_id.

## Freeze kinds

  self_approve       — operator-rank verdict. Single-turn semantics
                       per #39: lasts from blocked tool call until
                       the next UserPromptSubmit. That UPS resolves
                       it exactly once (exact phrase mints grant /
                       cancel pattern records denial / anything else
                       silently clears).

  admin_escalation   — admin-rank verdict. Persistent across multiple
                       UPS until admin runs aidocs admin approve in a
                       separate session. Operator's prompts during
                       the wait do NOT auto-cancel.

Phase A (this commit) ships self_approve only. admin_escalation is a
recognized kind but the polling logic lives in claude_hook, not here.

## Single-row-per-session contract

At most ONE active freeze per session. Setting a new freeze on a
session that already has one REPLACES the prior — the rationale is
that a fresh confirmable verdict supersedes any unresolved older
freeze. This shouldn't happen in normal flow (the prior freeze
prevents new tool calls) but defends against race conditions where a
parallel hook event somehow slips through.

Co-located with escalation_store on the identity DB so freeze rows
and request rows live together for atomicity.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .agent_memory_epoch import (
    derive_agent_context_id,
    derive_aidocs_session_id,
    derive_session_uuid,
)

KIND_SELF_APPROVE = "self_approve"
KIND_ADMIN_ESCALATION = "admin_escalation"
# Repeated flat security violations (command_read_intent, sensitive reads,
# unknown-external, raw-shell T0, hostile preflight). Cleared ONLY by
# operator/admin (like admin_escalation) — never by a UPS self-approve
# phrase, because the offending actor must not be able to talk its own
# way out of a lockdown it triggered.
KIND_REPEATED_SECURITY_VIOLATION = "repeated_security_violation"
# Hostile OPERATOR prompt caught at UserPromptSubmit (preflight forbidden
# verdict). Freezes IMMEDIATELY on the first nasty attempt — the prompt is
# judged before the agent ever sees it, so there is no "agent gets two
# chances" ladder. Like the repeated-violation kind, it is admin/operator
# recovery only, never a UPS self_approve.
KIND_HOSTILE_OPERATOR_PROMPT = "hostile_operator_prompt"

VALID_KINDS: frozenset[str] = frozenset(
    {
        KIND_SELF_APPROVE,
        KIND_ADMIN_ESCALATION,
        KIND_REPEATED_SECURITY_VIOLATION,
        KIND_HOSTILE_OPERATOR_PROMPT,
    },
)


@dataclass(frozen=True)
class SessionFreeze:
    session_id: str
    request_id: str
    fingerprint_phrase: str
    kind: str
    frozen_at: str
    expires_at: str | None  # NULL for admin_escalation; ISO ts for self_approve TTL
    host_session_id: str = ""  # raw host token, attribution + derivation input only
    user_id: str = ""  # logged-in operator, attribution only (NOT part of the key)
    agent_context_id: str = ""  # DERIVED per-agent key (identity-spine); '' = session-wide
    aidocs_session_id: str = ""  # DERIVED canonical security-scope id


class SessionFreezeStore:
    """Per-project session-freeze state. One active row per session."""

    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs_identity.sqlite3"

    def init_db(self, project_root: Path) -> None:
        path = self.db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            # Per-agent freeze isolation keyed on the CANONICAL DERIVED identity
            # (identity-spine 2026-06-29): the per-agent axis is
            # agent_context_id = derive_agent_context_id(host_kind, project,
            # host_session_id), so two agents sharing a raw host_session_id
            # string across different host_kinds do NOT collide. session_id is
            # the work-session scope; aidocs_session_id is stored as the
            # canonical security-scope id. host_session_id is attribution +
            # derivation input only, never a primary key.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_freeze (
                    session_id TEXT NOT NULL,
                    agent_context_id TEXT NOT NULL DEFAULT '',
                    aidocs_session_id TEXT NOT NULL DEFAULT '',
                    host_session_id TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL,
                    fingerprint_phrase TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    expires_at TEXT,
                    user_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (session_id, agent_context_id)
                );
                CREATE INDEX IF NOT EXISTS idx_session_freeze_request
                    ON session_freeze(request_id);
            """)
            # Migrate any older shape (legacy PK=session_id, or the composite
            # PK=(session_id, host_session_id) era) onto the derived-key schema
            # via recreate-copy-rename. Existing rows are all claude_code (the
            # only live host today), so backfill the derived ids with
            # host_kind='claude_code'. Rows with host_session_id='' derive to
            # agent_context_id='' (session-wide), preserving that fallback.
            # #243 crash-atomic migration + recovery. Two trigger states:
            #  (1) FRESH: session_freeze is still the old shape (no
            #      agent_context_id) and no _legacy table yet -> rename aside,
            #      create the new table, restore derived rows, drop _legacy.
            #  (2) RECOVERY: a pre-fix crash committed an EMPTY new-schema
            #      session_freeze and stranded the rows in session_freeze_legacy
            #      (the fail-open — active freezes vanished and the re-fire guard
            #      was defeated by the empty new table). Restore from _legacy.
            # BOTH run under ONE transaction (store_migrations.atomic_migration):
            # a kill rolls back to the intact pre-migration state, so the source
            # rows are NEVER gone before the destination is populated. Only
            # single-statement conn.execute inside the txn — executescript would
            # auto-commit and defeat the atomicity.
            from .store_migrations import atomic_migration, table_exists

            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_freeze)").fetchall()}
            if "agent_context_id" not in cols or table_exists(conn, "session_freeze_legacy"):
                with atomic_migration(conn):
                    if not table_exists(conn, "session_freeze_legacy"):
                        conn.execute(
                            "ALTER TABLE session_freeze RENAME TO session_freeze_legacy"
                        )
                        conn.execute(
                            """
                            CREATE TABLE session_freeze (
                                session_id TEXT NOT NULL,
                                agent_context_id TEXT NOT NULL DEFAULT '',
                                aidocs_session_id TEXT NOT NULL DEFAULT '',
                                host_session_id TEXT NOT NULL DEFAULT '',
                                request_id TEXT NOT NULL,
                                fingerprint_phrase TEXT NOT NULL,
                                kind TEXT NOT NULL,
                                frozen_at TEXT NOT NULL,
                                expires_at TEXT,
                                user_id TEXT NOT NULL DEFAULT '',
                                PRIMARY KEY (session_id, agent_context_id)
                            )
                            """
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_session_freeze_request "
                            "ON session_freeze(request_id)"
                        )
                    lcols = {
                        r[1]
                        for r in conn.execute(
                            "PRAGMA table_info(session_freeze_legacy)"
                        ).fetchall()
                    }
                    host_expr = "host_session_id" if "host_session_id" in lcols else "''"
                    user_expr = "user_id" if "user_id" in lcols else "''"
                    legacy = conn.execute(
                        f"SELECT session_id, {host_expr} AS hsid, request_id, "
                        f"fingerprint_phrase, kind, frozen_at, expires_at, "
                        f"{user_expr} AS uid FROM session_freeze_legacy",
                    ).fetchall()
                    for r in legacy:
                        s_id = str(r[0])
                        hsid = str(r[1] or "")
                        acid, asid = self._derive_keys(
                            project_root,
                            session_id=s_id,
                            host_session_id=hsid,
                            host_kind="claude_code",
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO session_freeze "
                            "(session_id, agent_context_id, aidocs_session_id, "
                            " host_session_id, request_id, fingerprint_phrase, kind, "
                            " frozen_at, expires_at, user_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (s_id, acid, asid, hsid, r[2], r[3], r[4], r[5], r[6], str(r[7] or "")),
                        )
                    conn.execute("DROP TABLE session_freeze_legacy")
            conn.commit()

    def _derive_keys(
        self,
        project_root: Path,
        *,
        session_id: str,
        host_session_id: str,
        host_kind: str,
    ) -> tuple[str, str]:
        """(agent_context_id, aidocs_session_id) from the canonical stack.

        Empty host_session_id -> ('', '') = the legacy/session-wide bucket.
        agent_context_id is the per-agent axis (excludes the work session, so
        it is compaction- AND work-session-stable); aidocs_session_id is the
        per-agent-per-session security-scope id.
        """
        agent_context_id = derive_agent_context_id(
            host_kind=host_kind,
            project_root=project_root,
            host_session_id=host_session_id,
        )
        session_uuid = derive_session_uuid(project_root, session_id)
        aidocs_session_id = derive_aidocs_session_id(
            host_kind=host_kind,
            project_root=project_root,
            host_session_id=host_session_id,
            session_uuid=session_uuid,
        )
        return agent_context_id, aidocs_session_id

    def set_freeze(
        self,
        project_root: Path,
        *,
        session_id: str,
        request_id: str,
        fingerprint_phrase: str,
        kind: str = KIND_SELF_APPROVE,
        ttl_seconds: int | None = None,
        host_session_id: str = "",
        user_id: str = "",
        host_kind: str = "claude_code",
    ) -> SessionFreeze:
        """Replace any existing freeze on this session with a fresh one.

        ttl_seconds policy (lane 1.5, 2026-05-04):
          - self_approve: defaults to 300s (5 min). The single-turn
            contract clears via UPS, but a stale lock without TTL
            survives MCP restart, fresh CLI, and any UPS that fails
            to fire the resolver. TTL is the safety net.
          - admin_escalation: defaults to None — the freeze persists
            until admin decides via escalation_store.
          - Caller may override either default by passing
            ttl_seconds explicitly.
        """
        # Q2 doctrine 2026-05-04: freeze is an emergency lockdown
        # for malicious-detected calls. NO automatic TTL — that
        # would give attackers exactly the timing window they want.
        # Operator/admin clears explicitly. Callers may still pass
        # ttl_seconds explicitly when a specific bounded lock is
        # intended.
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown freeze kind: {kind!r}")
        if not session_id.strip():
            raise ValueError("session_id required")
        if not request_id.strip():
            raise ValueError("request_id required")
        if not fingerprint_phrase.strip():
            raise ValueError("fingerprint_phrase required")
        self.init_db(project_root)
        now = time.time()
        frozen_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(now),
        )
        expires_at: str | None = None
        if ttl_seconds is not None and ttl_seconds > 0:
            expires_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(now + ttl_seconds),
            )
        acid, asid = self._derive_keys(
            project_root,
            session_id=session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
        )
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_freeze "
                "(session_id, agent_context_id, aidocs_session_id, host_session_id, "
                " request_id, fingerprint_phrase, kind, frozen_at, expires_at, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    acid,
                    asid,
                    host_session_id,
                    request_id,
                    fingerprint_phrase,
                    kind,
                    frozen_at,
                    expires_at,
                    user_id,
                ),
            )
            conn.commit()
        return SessionFreeze(
            session_id=session_id,
            request_id=request_id,
            fingerprint_phrase=fingerprint_phrase,
            kind=kind,
            frozen_at=frozen_at,
            expires_at=expires_at,
            host_session_id=host_session_id,
            user_id=user_id,
            agent_context_id=acid,
            aidocs_session_id=asid,
        )

    def get_active_freeze(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
        operator_user_id: str = "",
    ) -> SessionFreeze | None:
        """Return the active freeze for a session, or None if not frozen.

        Caller is responsible for honoring the kind:
          - self_approve: resolve on next UPS
          - admin_escalation: poll escalation_store for admin decision

        Expired self_approve rows (when ttl_seconds was set and clock
        passed) are NOT auto-cleaned here — the single-turn contract
        means UPS-level resolution is the canonical clear path. This
        getter just returns the row as-is.
        """
        self.init_db(project_root)
        acid, _ = self._derive_keys(
            project_root,
            session_id=session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
        )
        _cols = (
            "SELECT session_id, agent_context_id, aidocs_session_id, "
            "host_session_id, request_id, fingerprint_phrase, kind, "
            "frozen_at, expires_at, user_id FROM session_freeze "
        )
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            if operator_user_id:
                # Operator-wide (security.freeze_all_sessions_on_malicious_intent):
                # frozen if THIS session has a freeze OR the SAME authenticated
                # operator has a malicious-intent (hostile-operator-prompt) freeze
                # in ANY session. The session's own row is preferred in the order
                # so session-local context is returned when present.
                # AGENT-scoped (operator 2026-07-15, agent-identity invariant):
                # a frozen agent stays frozen after switching work sessions, so
                # match on agent_context_id (the agent) — NOT session_id — when
                # the agent identity is known. Fall back to the legacy
                # session-wide bucket only when there is no host_session_id.
                if acid:
                    row = conn.execute(
                        _cols
                        + "WHERE agent_context_id = ? "
                        "   OR (session_id = ? AND agent_context_id = '') "
                        "   OR (user_id = ? AND user_id != '' AND kind = ?) "
                        "ORDER BY (agent_context_id = ?) DESC, frozen_at DESC LIMIT 1",
                        (acid, session_id, operator_user_id, KIND_HOSTILE_OPERATOR_PROMPT, acid),
                    ).fetchone()
                else:
                    row = conn.execute(
                        _cols
                        + "WHERE (session_id = ? AND agent_context_id = '') "
                        "   OR (user_id = ? AND user_id != '' AND kind = ?) "
                        "ORDER BY (session_id = ?) DESC, frozen_at DESC LIMIT 1",
                        (session_id, operator_user_id, KIND_HOSTILE_OPERATOR_PROMPT, session_id),
                    ).fetchone()
            elif acid:
                # AGENT-scoped: the agent's OWN freeze follows it across work
                # sessions (agent_context_id). A legacy SESSION-WIDE freeze
                # (agent_context_id '') still applies within its own session —
                # keep matching it so the session-wide bucket is not lost.
                row = conn.execute(
                    _cols + "WHERE agent_context_id = ? "
                    "   OR (session_id = ? AND agent_context_id = '') "
                    "ORDER BY (agent_context_id = ?) DESC, frozen_at DESC LIMIT 1",
                    (acid, session_id, acid),
                ).fetchone()
            else:
                # Legacy session-wide bucket (no host_session_id -> no agent id):
                # nothing to follow across sessions, so key on the session label.
                row = conn.execute(
                    _cols + "WHERE session_id = ? AND agent_context_id = '' "
                    "ORDER BY frozen_at DESC LIMIT 1",
                    (session_id,),
                ).fetchone()

        if row is None:
            return None
        # Q2 doctrine 2026-05-04: no lazy expiration sweep. A
        # malicious-detected lockdown must NOT dissolve on a clock
        # — only operator/admin action clears it.
        return SessionFreeze(
            session_id=str(row["session_id"]),
            request_id=str(row["request_id"]),
            fingerprint_phrase=str(row["fingerprint_phrase"]),
            kind=str(row["kind"]),
            frozen_at=str(row["frozen_at"]),
            expires_at=(str(row["expires_at"]) if row["expires_at"] is not None else None),
            host_session_id=str(row["host_session_id"]),
            user_id=str(row["user_id"]),
            agent_context_id=str(row["agent_context_id"]),
            aidocs_session_id=str(row["aidocs_session_id"]),
        )

    def clear_freeze(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str | None = None,
        host_kind: str = "claude_code",
    ) -> bool:
        """Drop the freeze row. Returns True if a row was deleted,
        False when no freeze was active.

        When ``host_session_id`` is given, deletes ONLY that agent's row
        (the composite PK), so clearing one co-session agent's freeze does
        NOT lift another agent's lock — the per-agent isolation the PK
        promises now holds on the clear path too. When None, clears every
        row for the session (admin whole-session clear / legacy callers).
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            if host_session_id is None:
                cur = conn.execute(
                    "DELETE FROM session_freeze WHERE session_id = ?",
                    (session_id,),
                )
            else:
                acid, _ = self._derive_keys(
                    project_root,
                    session_id=session_id,
                    host_session_id=host_session_id,
                    host_kind=host_kind,
                )
                cur = conn.execute(
                    "DELETE FROM session_freeze "
                    "WHERE session_id = ? AND agent_context_id = ?",
                    (session_id, acid),
                )
            conn.commit()
            return cur.rowcount > 0

    def clear_freeze_by_request(
        self,
        project_root: Path,
        request_id: str,
    ) -> int:
        """Drop any freeze row pointing at this escalation request_id.

        Returns the number of rows deleted. Admin tools that hold a
        request_id (not a session_id) use this.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            # If this request points at an operator-wide malicious-intent freeze,
            # clearing it lifts the operator across ALL their sessions (one clear,
            # all sessions) — security.freeze_all_sessions_on_malicious_intent.
            target = conn.execute(
                "SELECT user_id, kind FROM session_freeze WHERE request_id = ? LIMIT 1",
                (request_id,),
            ).fetchone()
            cur = conn.execute(
                "DELETE FROM session_freeze WHERE request_id = ?",
                (request_id,),
            )
            deleted = int(cur.rowcount or 0)
            if (
                target is not None
                and str(target["kind"]) == KIND_HOSTILE_OPERATOR_PROMPT
                and str(target["user_id"] or "")
            ):
                cur2 = conn.execute(
                    "DELETE FROM session_freeze WHERE user_id = ? AND user_id != '' AND kind = ?",
                    (str(target["user_id"]), KIND_HOSTILE_OPERATOR_PROMPT),
                )
                deleted += int(cur2.rowcount or 0)
            conn.commit()
            return deleted

    def sweep_expired(self, project_root: Path) -> int:
        """Delete every freeze row whose expires_at is in the past.

        Called at MCP boot (lane 1.5, 2026-05-04) so a self_approve
        lock with a passed TTL cannot survive a server restart. Also
        called explicitly by tests. Returns the number of rows
        deleted. Best-effort — never raises.
        """
        self.init_db(project_root)
        try:
            now_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time()),
            )
            with sqlite3.connect(str(self.db_path(project_root))) as conn:
                cur = conn.execute(
                    "DELETE FROM session_freeze WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (now_iso,),
                )
                conn.commit()
                return int(cur.rowcount or 0)
        except Exception:
            return 0

    def list_active_freezes(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
    ) -> list[SessionFreeze]:
        """Enumerate active freeze rows. With session_id=None,
        returns every active freeze in the project; otherwise
        filters to that session (0 or 1 row in practice).

        Q2 doctrine 2026-05-04: NO automatic expiration sweep. A
        malicious-detected lockdown must NOT dissolve on a clock —
        only operator/admin action clears it.
        """
        self.init_db(project_root)
        rows: list[SessionFreeze] = []
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            if session_id is not None:
                cur = conn.execute(
                    "SELECT session_id, agent_context_id, aidocs_session_id,"
                    " host_session_id, request_id, fingerprint_phrase, kind,"
                    " frozen_at, expires_at, user_id"
                    " FROM session_freeze WHERE session_id = ?",
                    (session_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT session_id, agent_context_id, aidocs_session_id,"
                    " host_session_id, request_id, fingerprint_phrase, kind,"
                    " frozen_at, expires_at, user_id"
                    " FROM session_freeze",
                )
            for row in cur.fetchall():
                rows.append(
                    SessionFreeze(
                        session_id=str(row["session_id"]),
                        request_id=str(row["request_id"]),
                        fingerprint_phrase=str(row["fingerprint_phrase"]),
                        kind=str(row["kind"]),
                        frozen_at=str(row["frozen_at"]),
                        expires_at=(
                            str(row["expires_at"]) if row["expires_at"] is not None else None
                        ),
                        host_session_id=str(row["host_session_id"]),
                        user_id=str(row["user_id"]),
                        agent_context_id=str(row["agent_context_id"]),
                        aidocs_session_id=str(row["aidocs_session_id"]),
                    ),
                )
        return rows

    def get_active_freeze_by_id(
        self,
        project_root: Path,
        freeze_id: str,
    ) -> SessionFreeze | None:
        """Resolve a freeze by its `request_id` (its stable id).
        Admin tools prefer this over session_id — unambiguous by
        construction.

        Q2 doctrine 2026-05-04: NO automatic expiration sweep on
        admin lookups. Operator/admin clears explicitly.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT session_id, agent_context_id, aidocs_session_id, "
                "host_session_id, request_id, fingerprint_phrase, kind, "
                "frozen_at, expires_at, user_id FROM session_freeze "
                "WHERE request_id = ?",
                (freeze_id,),
            ).fetchone()
        if row is None:
            return None
        return SessionFreeze(
            session_id=str(row["session_id"]),
            request_id=str(row["request_id"]),
            fingerprint_phrase=str(row["fingerprint_phrase"]),
            kind=str(row["kind"]),
            frozen_at=str(row["frozen_at"]),
            expires_at=(str(row["expires_at"]) if row["expires_at"] is not None else None),
            host_session_id=str(row["host_session_id"]),
            user_id=str(row["user_id"]),
            agent_context_id=str(row["agent_context_id"]),
            aidocs_session_id=str(row["aidocs_session_id"]),
        )

    def snapshot_prompt_submit_state(
        self,
        project_root: Path,
        *,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
    ) -> dict[str, object]:
        """Capture this work-session/agent freeze scope; errors propagate."""
        from .prompt_submit_store_snapshot import capture_scoped_rows

        self.init_db(project_root)
        agent_context_id, _ = self._derive_keys(
            project_root,
            session_id=session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
        )
        scopes = {
            "session_freeze": (
                "session_id = ? AND agent_context_id IN (?, '')",
                (session_id, agent_context_id),
            )
        }
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            return capture_scoped_rows(conn, scopes)

    def restore_prompt_submit_state(
        self,
        project_root: Path,
        snapshot: dict[str, object],
        *,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
    ) -> None:
        """Restore exactly this freeze scope, deleting rows created in T."""
        from .prompt_submit_store_snapshot import restore_scoped_rows

        self.init_db(project_root)
        agent_context_id, _ = self._derive_keys(
            project_root,
            session_id=session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
        )
        scopes = {
            "session_freeze": (
                "session_id = ? AND agent_context_id IN (?, '')",
                (session_id, agent_context_id),
            )
        }
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            restore_scoped_rows(conn, scopes, snapshot)
