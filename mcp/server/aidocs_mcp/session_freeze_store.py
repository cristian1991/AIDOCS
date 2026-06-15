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


class SessionFreezeStore:
    """Per-project session-freeze state. One active row per session."""

    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs_identity.sqlite3"

    def init_db(self, project_root: Path) -> None:
        path = self.db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_freeze (
                    session_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    fingerprint_phrase TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_session_freeze_request
                    ON session_freeze(request_id);
            """)
            conn.commit()

    def set_freeze(
        self,
        project_root: Path,
        *,
        session_id: str,
        request_id: str,
        fingerprint_phrase: str,
        kind: str = KIND_SELF_APPROVE,
        ttl_seconds: int | None = None,
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_freeze "
                "(session_id, request_id, fingerprint_phrase, kind, "
                " frozen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    request_id,
                    fingerprint_phrase,
                    kind,
                    frozen_at,
                    expires_at,
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
        )

    def get_active_freeze(
        self,
        project_root: Path,
        session_id: str,
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT session_id, request_id, fingerprint_phrase, "
                "kind, frozen_at, expires_at FROM session_freeze "
                "WHERE session_id = ?",
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
        )

    def clear_freeze(
        self,
        project_root: Path,
        session_id: str,
    ) -> bool:
        """Drop the freeze row. Returns True if a row was deleted,
        False when no freeze was active.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "DELETE FROM session_freeze WHERE session_id = ?",
                (session_id,),
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
            cur = conn.execute(
                "DELETE FROM session_freeze WHERE request_id = ?",
                (request_id,),
            )
            conn.commit()
            return int(cur.rowcount or 0)

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
                    "SELECT session_id, request_id, fingerprint_phrase,"
                    " kind, frozen_at, expires_at FROM session_freeze"
                    " WHERE session_id = ?",
                    (session_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT session_id, request_id, fingerprint_phrase,"
                    " kind, frozen_at, expires_at FROM session_freeze",
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
                "SELECT session_id, request_id, fingerprint_phrase, "
                "kind, frozen_at, expires_at FROM session_freeze "
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
        )
