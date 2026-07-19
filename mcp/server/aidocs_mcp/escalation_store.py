"""Escalation store — Layer 9 C-2 approval workflow for denied
user-intent gates.

When a user tries to unlock a gate they don't have permission for
(e.g. dev says "allow psql" but lacks `security.allow_raw_shell`), the
system creates an EscalationRequest row with status='pending' and
tells the user "awaiting approval in dashboard".

A higher-role admin with `rbac.approve_escalations` sees the
pending request in the dashboard, reviews context (task, session,
plan, requested gate), clicks approve/deny. The store records the
decision; claude_hook picks it up on the next poll and either
issues the gate grant (sticky or per-turn) or relays the denial.

Fallback: operators can also approve via password-in-chat
(claude_hook scrubs the plaintext). Same status transitions, same
audit trail.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_CONSUMED = "consumed"
# System rollback (not an operator decision): a request created during a
# freeze mint that could not be completed (e.g. set_freeze failed). A
# cancelled request is neutralized — never pending (won't list/approve),
# never approved (can't be consumed). Distinct from 'denied' so audit can
# tell an operator denial from a system compensation.
STATUS_CANCELLED = "cancelled"

VALID_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PENDING,
        STATUS_APPROVED,
        STATUS_DENIED,
        STATUS_EXPIRED,
        STATUS_CONSUMED,
        STATUS_CANCELLED,
    },
)


DEFAULT_TTL_SECONDS = 15 * 60  # 15 minutes — long enough for async approval


@dataclass(frozen=True)
class EscalationRequest:
    request_id: str
    requester_user_id: str | None
    requester_label: str
    session_id: str | None
    task_id: str | None
    plan_path: str | None
    gate_permission: str
    gate_phrase: str
    sticky: bool
    status: str
    approver_user_id: str | None
    approver_label: str | None
    reason: str
    created_at: str
    decided_at: str | None
    expires_at: str
    extra: dict[str, Any] = field(default_factory=dict)
    machine_id: str = ""
    command_snippet: str | None = None


@dataclass(frozen=True)
class EscalationGrant:
    grant_id: str
    request_id: str
    user_id: str
    machine_id: str
    session_id: str
    permission_name: str
    command_hash: str | None
    approved_by_user_id: str
    approved_at: str
    expires_at: str
    max_uses: int
    uses_consumed: int
    # SEC-003 binding fields (2026-04-23). Empty-string defaults for
    # TEXT columns keep legacy rows matchable via the non-binding
    # paths (permission + command_hash).
    tool_name: str = ""
    path: str = ""
    task_id: str = ""
    scope: str = "once"
    expires_turn: int | None = None

    @property
    def remaining_uses(self) -> int:
        return max(0, self.max_uses - self.uses_consumed)

    def is_live(self, now_iso: str) -> bool:
        """Grant is live iff TTL not expired AND uses remain."""
        return self.expires_at > now_iso and self.remaining_uses > 0


class EscalationStore:
    """Per-project pending-approval sqlite store."""

    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs_identity.sqlite3"

    def init_db(self, project_root: Path) -> None:
        path = self.db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rbac_escalations (
                    request_id TEXT PRIMARY KEY,
                    requester_user_id TEXT,
                    requester_label TEXT NOT NULL,
                    session_id TEXT,
                    task_id TEXT,
                    plan_path TEXT,
                    gate_permission TEXT NOT NULL,
                    gate_phrase TEXT NOT NULL,
                    sticky INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    approver_user_id TEXT,
                    approver_label TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    expires_at TEXT NOT NULL,
                    extra_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_rbac_escalations_status
                    ON rbac_escalations(status);
                CREATE INDEX IF NOT EXISTS idx_rbac_escalations_session
                    ON rbac_escalations(session_id);

                -- Corpo escalation grants (2026-04-22). When an admin
                -- approves an escalation, we create a distinct grant
                -- row scoped to (user, machine, session, permission)
                -- with its own TTL + max_uses. The gate checks this
                -- table BEFORE bubbling another request — see
                -- escalation_hook.find_live_grant. Separates "approval
                -- decided" (rbac_escalations row flips to approved)
                -- from "grant is live and consumable" (this table).
                CREATE TABLE IF NOT EXISTS rbac_escalation_grants (
                    grant_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    machine_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    permission_name TEXT NOT NULL,
                    command_hash TEXT,
                    approved_by_user_id TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    uses_consumed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_rbac_grants_lookup
                    ON rbac_escalation_grants(
                        user_id, machine_id, session_id, permission_name
                    );
                CREATE INDEX IF NOT EXISTS idx_rbac_grants_request
                    ON rbac_escalation_grants(request_id);
            """)
            # Additive columns on rbac_escalations for machine_id +
            # command_snippet. ADD COLUMN IF NOT EXISTS isn't on
            # sqlite < 3.35; standard try/pass pattern.
            for ddl in (
                "ALTER TABLE rbac_escalations ADD COLUMN machine_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE rbac_escalations ADD COLUMN command_snippet TEXT",
                # SEC-003 (2026-04-23): per-grant binding fields so
                # approvals bind to (capability, tool, path, task_id,
                # fingerprint) and consumption only happens when the
                # attempted action matches. scope values: once | task |
                # session. default 'once'. expires_turn is nullable —
                # when set, the matcher also checks the session turn
                # counter for additional bounding.
                "ALTER TABLE rbac_escalation_grants ADD COLUMN tool_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE rbac_escalation_grants ADD COLUMN path TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE rbac_escalation_grants ADD COLUMN task_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE rbac_escalation_grants ADD COLUMN scope TEXT NOT NULL DEFAULT 'once'",
                "ALTER TABLE rbac_escalation_grants ADD COLUMN expires_turn INTEGER",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def create_request(
        self,
        project_root: Path,
        *,
        requester_label: str,
        gate_permission: str,
        gate_phrase: str,
        requester_user_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        plan_path: str | None = None,
        sticky: bool = False,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        extra: dict[str, Any] | None = None,
        machine_id: str = "",
        command_snippet: str | None = None,
    ) -> EscalationRequest:
        self.init_db(project_root)
        request_id = "esc_" + secrets.token_hex(8)
        now = time.time()
        created = _iso(now)
        expires = _iso(now + max(60, int(ttl_seconds)))
        extra_json = json.dumps(extra or {}, sort_keys=True)
        # Truncate command_snippet for readable dashboard rows; full
        # command lives in the audit event payload if needed.
        snippet = str(command_snippet) if command_snippet else None
        if snippet is not None and len(snippet) > 200:
            snippet = snippet[:197] + "..."
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO rbac_escalations "
                "(request_id, requester_user_id, requester_label, "
                "session_id, task_id, plan_path, gate_permission, "
                "gate_phrase, sticky, status, reason, created_at, "
                "expires_at, extra_json, machine_id, command_snippet) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)",
                (
                    request_id,
                    requester_user_id,
                    requester_label,
                    session_id,
                    task_id,
                    plan_path,
                    gate_permission,
                    gate_phrase,
                    1 if sticky else 0,
                    STATUS_PENDING,
                    created,
                    expires,
                    extra_json,
                    str(machine_id or ""),
                    snippet,
                ),
            )
            conn.commit()
        return EscalationRequest(
            request_id=request_id,
            requester_user_id=requester_user_id,
            requester_label=requester_label,
            session_id=session_id,
            task_id=task_id,
            plan_path=plan_path,
            gate_permission=gate_permission,
            gate_phrase=gate_phrase,
            sticky=sticky,
            status=STATUS_PENDING,
            approver_user_id=None,
            approver_label=None,
            reason="",
            created_at=created,
            decided_at=None,
            expires_at=expires,
            extra=extra or {},
            machine_id=str(machine_id or ""),
            command_snippet=snippet,
        )

    def get(
        self,
        project_root: Path,
        request_id: str,
    ) -> EscalationRequest | None:
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM rbac_escalations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_request(row)

    def list_pending(
        self,
        project_root: Path,
        session_id: str | None = None,
    ) -> list[EscalationRequest]:
        """Operators see these in the dashboard. Expired rows are
        swept to status='expired' on every list call so stale
        requests don't linger.
        """
        self.init_db(project_root)
        now_iso = _iso_now()
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "UPDATE rbac_escalations SET status = ? WHERE status = ? AND expires_at <= ?",
                (STATUS_EXPIRED, STATUS_PENDING, now_iso),
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            if session_id is None:
                rows = conn.execute(
                    "SELECT * FROM rbac_escalations WHERE status = ? ORDER BY created_at ASC",
                    (STATUS_PENDING,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rbac_escalations "
                    "WHERE status = ? AND session_id = ? "
                    "ORDER BY created_at ASC",
                    (STATUS_PENDING, session_id),
                ).fetchall()
        return [_row_to_request(r) for r in rows]

    def decide(
        self,
        project_root: Path,
        request_id: str,
        *,
        approve: bool,
        approver_user_id: str | None,
        approver_label: str,
        reason: str = "",
    ) -> EscalationRequest | None:
        """Set approve/deny. Refuses if the row isn't pending (double-
        decide safety). Returns the updated row, or None if unknown.
        """
        self.init_db(project_root)
        now_iso = _iso_now()
        new_status = STATUS_APPROVED if approve else STATUS_DENIED
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "UPDATE rbac_escalations SET status = ?, "
                "approver_user_id = ?, approver_label = ?, "
                "reason = ?, decided_at = ? "
                "WHERE request_id = ? AND status = ?",
                (
                    new_status,
                    approver_user_id,
                    approver_label,
                    reason,
                    now_iso,
                    request_id,
                    STATUS_PENDING,
                ),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM rbac_escalations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return _row_to_request(row) if row else None

    def cancel_request(
        self,
        project_root: Path,
        request_id: str,
        *,
        reason: str = "",
    ) -> bool:
        """System rollback of a PENDING request (compensation, not an
        operator decision). PENDING → cancelled; no-op on any other status.
        Returns True if a row was cancelled.

        Used when a freeze mint creates the escalation request but cannot
        complete (set_freeze fails), so no orphan pending request survives
        to be approved into a grant for an action that never had a freeze.
        """
        self.init_db(project_root)
        now_iso = _iso_now()
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "UPDATE rbac_escalations SET status = ?, "
                "approver_label = ?, reason = ?, "
                "decided_at = ? WHERE request_id = ? AND status = ?",
                (
                    STATUS_CANCELLED,
                    "system:freeze_mint_rollback",
                    reason or "system rollback",
                    now_iso,
                    request_id,
                    STATUS_PENDING,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def consume(
        self,
        project_root: Path,
        request_id: str,
    ) -> EscalationRequest | None:
        """Flip approved → consumed after claude_hook issues the grant.
        Prevents replay: a single approval unlocks exactly one gate
        event, unless sticky=true.
        """
        self.init_db(project_root)
        now_iso = _iso_now()
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "UPDATE rbac_escalations SET status = ?, decided_at = ? "
                "WHERE request_id = ? AND status = ?",
                (STATUS_CONSUMED, now_iso, request_id, STATUS_APPROVED),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM rbac_escalations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return _row_to_request(row) if row else None

    def poll_approved_for_session(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[EscalationRequest]:
        """Hook-side: which approvals are ready to consume for this
        session right now?
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM rbac_escalations "
                "WHERE status = ? AND session_id = ? "
                "ORDER BY decided_at ASC",
                (STATUS_APPROVED, session_id),
            ).fetchall()
        return [_row_to_request(r) for r in rows]

    # ── Scoped single-use grants (2026-04-22) ──────────────────────────

    def create_grant(
        self,
        project_root: Path,
        *,
        request_id: str,
        user_id: str,
        machine_id: str,
        session_id: str,
        permission_name: str,
        approved_by_user_id: str,
        ttl_seconds: int = 300,
        max_uses: int = 1,
        command_hash: str | None = None,
        # SEC-003 binding fields (2026-04-23). Back-compat: all default
        # to their schema defaults, so existing callers don't need
        # to change. operation_fingerprint is an alias for
        # command_hash — spec calls it operation_fingerprint, code
        # calls it command_hash; accept either name.
        tool_name: str = "",
        path: str = "",
        task_id: str = "",
        scope: str = "once",
        expires_turn: int | None = None,
        operation_fingerprint: str | None = None,
    ) -> str:
        """Create a scoped grant row when an admin approves an
        escalation. Grant is bound to the exact (user, machine,
        session, permission) tuple that requested it; nothing else
        can satisfy the gate with this grant.

        SEC-003 (2026-04-23): session-scope grants must be bounded —
        either expires_at (via ttl_seconds) OR expires_turn MUST be
        set. The matcher refuses to honor an unbounded session
        approval, so accepting one at creation would be a no-op
        footgun. Rejected with ValueError.

        Returns the full EscalationGrant object (back-compat shape).
        Tests that just want the id can read `.grant_id`.
        """
        # operation_fingerprint is the spec name; command_hash is the
        # legacy column name. Prefer the explicit parameter.
        if operation_fingerprint is not None:
            command_hash = operation_fingerprint
        scope = (scope or "once").strip().lower()
        if scope not in ("once", "task", "session"):
            scope = "once"
        if scope == "session":
            # Must be bounded. ttl_seconds is >0 by default so
            # expires_at is always set; expires_turn is optional but
            # counts as bounding too. We reject only the explicit
            # "unbounded session" case where caller passed ttl_seconds=0.
            if int(ttl_seconds) <= 0 and expires_turn is None:
                raise ValueError(
                    "session-scope escalation grant must have "
                    "expires_at (via ttl_seconds>0) or expires_turn",
                )
        self.init_db(project_root)
        grant_id = "grant_" + secrets.token_hex(8)
        now = time.time()
        approved = _iso(now)
        expires = _iso(now + max(60, int(ttl_seconds)))
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO rbac_escalation_grants "
                "(grant_id, request_id, user_id, machine_id, "
                "session_id, permission_name, command_hash, "
                "approved_by_user_id, approved_at, expires_at, "
                "max_uses, uses_consumed, tool_name, path, task_id, "
                "scope, expires_turn) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, "
                "?, ?, ?, ?, ?)",
                (
                    grant_id,
                    request_id,
                    user_id,
                    machine_id,
                    session_id,
                    permission_name,
                    command_hash,
                    approved_by_user_id,
                    approved,
                    expires,
                    max(1, int(max_uses)),
                    tool_name,
                    path,
                    task_id,
                    scope,
                    expires_turn,
                ),
            )
            conn.commit()
        return EscalationGrant(
            grant_id=grant_id,
            request_id=request_id,
            user_id=user_id,
            machine_id=machine_id,
            session_id=session_id,
            permission_name=permission_name,
            command_hash=command_hash,
            approved_by_user_id=approved_by_user_id,
            approved_at=approved,
            expires_at=expires,
            max_uses=max(1, int(max_uses)),
            uses_consumed=0,
            tool_name=tool_name,
            path=path,
            task_id=task_id,
            scope=scope,
            expires_turn=expires_turn,
        )

    def find_grant_by_id(
        self,
        project_root: Path,
        grant_id: str,
    ) -> EscalationGrant | None:
        """SEC-003 helper — return a grant by its id, regardless of
        live/consumed status. Useful for audit + test verification.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM rbac_escalation_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
        return _row_to_grant(row) if row else None

    def find_live_grant(
        self,
        project_root: Path,
        *,
        user_id: str,
        machine_id: str,
        session_id: str,
        permission_name: str,
        command_hash: str | None = None,
        tool_name: str | None = None,
    ) -> EscalationGrant | None:
        """Return the newest live grant matching the scope tuple, or
        None. A 'live' grant has uses_consumed < max_uses AND
        expires_at > now. When command_hash is provided on the grant,
        the caller's command_hash must match exactly; permission-only
        grants (command_hash IS NULL) accept any command for that
        permission.

        When ``tool_name`` is provided, the grant's stored tool_name must
        also match exactly (caller is responsible for normalizing both
        sides). This enforces strict surface binding — e.g. an ai_run
        approval cannot be consumed by raw Bash. When None, tool identity
        is not checked (back-compat for permission-scoped callers).
        """
        self.init_db(project_root)
        now_iso = _iso_now()
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM rbac_escalation_grants "
                "WHERE user_id = ? AND machine_id = ? "
                "AND session_id = ? AND permission_name = ? "
                "AND expires_at > ? AND uses_consumed < max_uses "
                "ORDER BY approved_at DESC",
                (
                    user_id,
                    machine_id,
                    session_id,
                    permission_name,
                    now_iso,
                ),
            ).fetchall()
        for r in rows:
            g = _row_to_grant(r)
            # Strict command match only when grant specifies one.
            if g.command_hash and command_hash != g.command_hash:
                continue
            # Strict surface match only when the caller requests it.
            if tool_name is not None and (g.tool_name or "") != tool_name:
                continue
            return g
        return None

    def consume_grant(
        self,
        project_root: Path,
        grant_id: str,
    ) -> EscalationGrant | None:
        """Increment uses_consumed. Returns the updated grant or None
        if not found. Callers check is_live BEFORE consume; this
        method does not re-check to avoid a TOCTOU race, but the
        UPDATE's WHERE uses_consumed < max_uses guard ensures we
        never over-consume.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "UPDATE rbac_escalation_grants "
                "SET uses_consumed = uses_consumed + 1 "
                "WHERE grant_id = ? AND uses_consumed < max_uses",
                (grant_id,),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM rbac_escalation_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
        return _row_to_grant(row) if row else None

    def list_grants_for_request(
        self,
        project_root: Path,
        request_id: str,
    ) -> list[EscalationGrant]:
        """Audit helper — all grants (live + expired + consumed) that
        were minted from a given request.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM rbac_escalation_grants "
                "WHERE request_id = ? ORDER BY approved_at ASC",
                (request_id,),
            ).fetchall()
        return [_row_to_grant(r) for r in rows]



    def snapshot_prompt_submit_state(
        self,
        project_root: Path,
        *,
        session_id: str,
        request_ids: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """Capture request/grant rows this prompt can mutate; errors propagate."""
        from .prompt_submit_store_snapshot import capture_scoped_rows

        self.init_db(project_root)
        ids = tuple(dict.fromkeys(item for item in request_ids if item))
        suffix = (
            " OR request_id IN (" + ", ".join("?" for _ in ids) + ")"
            if ids
            else ""
        )
        params = (session_id, *ids)
        scopes = {
            "rbac_escalations": (
                "session_id = ?" + suffix,
                params,
            ),
            "rbac_escalation_grants": (
                "session_id = ?" + suffix,
                params,
            ),
        }
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            return capture_scoped_rows(conn, scopes)

    def restore_prompt_submit_state(
        self,
        project_root: Path,
        snapshot: dict[str, object],
        *,
        session_id: str,
        request_ids: tuple[str, ...] = (),
    ) -> None:
        from .prompt_submit_store_snapshot import restore_scoped_rows

        self.init_db(project_root)
        ids = tuple(dict.fromkeys(item for item in request_ids if item))
        suffix = (
            " OR request_id IN (" + ", ".join("?" for _ in ids) + ")"
            if ids
            else ""
        )
        params = (session_id, *ids)
        scopes = {
            "rbac_escalations": ("session_id = ?" + suffix, params),
            "rbac_escalation_grants": ("session_id = ?" + suffix, params),
        }
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            restore_scoped_rows(conn, scopes, snapshot)


def _row_to_request(row: sqlite3.Row) -> EscalationRequest:
    keys = row.keys()
    return EscalationRequest(
        request_id=row["request_id"],
        requester_user_id=row["requester_user_id"],
        requester_label=row["requester_label"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        plan_path=row["plan_path"],
        gate_permission=row["gate_permission"],
        gate_phrase=row["gate_phrase"],
        sticky=bool(row["sticky"]),
        status=row["status"],
        approver_user_id=row["approver_user_id"],
        approver_label=row["approver_label"],
        reason=row["reason"] or "",
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        expires_at=row["expires_at"],
        extra=json.loads(row["extra_json"] or "{}"),
        machine_id=(row["machine_id"] if "machine_id" in keys else "") or "",
        command_snippet=(row["command_snippet"] if "command_snippet" in keys else None),
    )


def _row_to_grant(row: sqlite3.Row) -> EscalationGrant:
    # SEC-003 additive fields use getattr-style access so pre-migration
    # rows (without the new columns) don't crash the matcher; sqlite
    # .Row supports key-or-default via `keys()` check.
    def _col(name: str, default):
        try:
            return row[name]
        except (IndexError, KeyError):
            return default

    _expires_turn = _col("expires_turn", None)
    return EscalationGrant(
        grant_id=row["grant_id"],
        request_id=row["request_id"],
        user_id=row["user_id"],
        machine_id=row["machine_id"],
        session_id=row["session_id"],
        permission_name=row["permission_name"],
        command_hash=row["command_hash"],
        approved_by_user_id=row["approved_by_user_id"],
        approved_at=row["approved_at"],
        expires_at=row["expires_at"],
        max_uses=int(row["max_uses"]),
        uses_consumed=int(row["uses_consumed"]),
        tool_name=_col("tool_name", "") or "",
        path=_col("path", "") or "",
        task_id=_col("task_id", "") or "",
        scope=_col("scope", "once") or "once",
        expires_turn=(int(_expires_turn) if _expires_turn is not None else None),
    )


def _iso_now() -> str:
    return _iso(time.time())


def _iso(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(
        epoch,
        tz=UTC,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
