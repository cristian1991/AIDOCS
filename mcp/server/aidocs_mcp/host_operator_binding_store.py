"""Host-session operator binding store.

Per /goal 2026-05-20. When `/aidocs` runs in a host (Claude Code,
OpenCode, etc.) the host process has a per-session UUID
(host_session_id) but NO operator identity. This store implements
the pairing handshake that binds an authenticated dashboard operator
to that host session:

  1. Host (/aidocs) → ``create_pending(...)`` mints a pending
     binding + a short pairing code. The host chat displays ONLY
     the pairing code + status — never a password or token.
  2. Dashboard (authenticated operator) → ``list_pending(...)`` shows
     the pending bindings, then ``approve(...)`` atomically binds
     the operator's user_id to that host_session_id.
  3. Hooks → ``resolve_operator(host_session_id)`` returns the bound
     operator's user_id (when approved + not expired) so the hook can
     build an operator_context for the host session.

Security model:
  - Pairing code is stored HASHED (sha256). The plaintext is shown
    once in the host chat; approval matches the hash.
  - approve() is an atomic ``UPDATE ... WHERE status='pending'`` so
    two concurrent approvals can't both win (the second sees
    rowcount 0).
  - WAL + busy_timeout + short transactions so the host-write and
    dashboard-approve paths don't deadlock under contention.
  - Expired pending bindings are never approvable; resolve() ignores
    expired/revoked rows.

The store NEVER stores passwords or operator tokens — only the
operator_user_id once an authenticated operator approves.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_PENDING_TTL_SECONDS = 600  # 10 min to approve a pairing


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "host_operator_bindings.sqlite3"


def _hash_code(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


@dataclass(frozen=True)
class HostBinding:
    binding_id: str
    host_kind: str
    host_session_id: str
    project_root: str
    aidocs_session_id: str
    requested_identity: str
    status: str  # pending | approved | revoked | expired
    operator_user_id: str
    created_at: str
    expires_at: str
    approved_at: str
    approved_by_role: str


class HostOperatorBindingStore:
    """SQLite-backed pairing store. WAL + busy_timeout + atomic
    pending→approved. Stateless across calls.
    """

    def db_path(self, project_root: Path) -> Path:
        return _db_path(project_root)

    def _connect(self, project_root: Path) -> sqlite3.Connection:
        path = _db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # busy_timeout (ms) so concurrent host-write + dashboard-
        # approve serialize instead of raising "database is locked".
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_db(self, project_root: Path) -> None:
        with self._connect(project_root) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS host_operator_bindings (
                    binding_id TEXT PRIMARY KEY,
                    host_kind TEXT NOT NULL,
                    host_session_id TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    aidocs_session_id TEXT NOT NULL DEFAULT '',
                    pairing_code_hash TEXT NOT NULL,
                    requested_identity TEXT NOT NULL DEFAULT '',
                    operator_user_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_at TEXT NOT NULL DEFAULT '',
                    approved_by_role TEXT NOT NULL DEFAULT ''
                )
                """,
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hob_host_session "
                "ON host_operator_bindings(host_session_id)",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hob_status ON host_operator_bindings(status)",
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Host side: create a pending binding (/aidocs)
    # ------------------------------------------------------------------

    def create_pending(
        self,
        project_root: Path,
        *,
        host_kind: str,
        host_session_id: str,
        aidocs_session_id: str = "",
        requested_identity: str = "",
        ttl_seconds: int = _DEFAULT_PENDING_TTL_SECONDS,
    ) -> tuple[str, str]:
        """Create a pending binding. Returns ``(binding_id,
        pairing_code)``. The pairing_code is plaintext (shown in the
        host chat once); only its hash is persisted.

        Idempotent-ish: if an un-expired pending binding already
        exists for this host_session_id, it is superseded (marked
        expired) and a fresh one minted, so a re-run of /aidocs
        doesn't accrete duplicate pending rows.
        """
        self.init_db(project_root)
        now = time.time()
        binding_id = "hb_" + secrets.token_hex(10)
        # 6-digit pairing code — easy to read aloud / type, low entropy
        # is fine because approval also requires an authenticated
        # operator AND a short TTL.
        pairing_code = f"{secrets.randbelow(1_000_000):06d}"
        with self._connect(project_root) as conn:
            # Supersede any live pending rows for this host session.
            conn.execute(
                "UPDATE host_operator_bindings SET status='expired' "
                "WHERE host_session_id=? AND status='pending'",
                (host_session_id,),
            )
            conn.execute(
                """
                INSERT INTO host_operator_bindings
                    (binding_id, host_kind, host_session_id,
                     project_root, aidocs_session_id, pairing_code_hash,
                     requested_identity, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    binding_id,
                    host_kind,
                    host_session_id,
                    str(project_root).replace("\\", "/"),
                    aidocs_session_id,
                    _hash_code(pairing_code),
                    requested_identity,
                    _iso(now),
                    _iso(now + max(60, int(ttl_seconds))),
                ),
            )
            conn.commit()
        return binding_id, pairing_code

    # ------------------------------------------------------------------
    # Dashboard side: list + approve
    # ------------------------------------------------------------------

    def _expire_stale(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE host_operator_bindings SET status='expired' "
            "WHERE status='pending' AND expires_at <= ?",
            (_iso_now(),),
        )

    def list_pending(self, project_root: Path) -> list[HostBinding]:
        """List currently-pending (not expired) bindings for the
        dashboard's approval queue. Lazily expires stale rows first.
        """
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            self._expire_stale(conn)
            conn.commit()
            rows = conn.execute(
                "SELECT * FROM host_operator_bindings WHERE status='pending' ORDER BY created_at",
            ).fetchall()
        return [self._row(r) for r in rows]

    def approve(
        self,
        project_root: Path,
        *,
        binding_id: str,
        operator_user_id: str,
        approved_by_role: str,
        pairing_code: str | None = None,
    ) -> tuple[bool, str]:
        """Atomically bind operator_user_id to the host session.

        Returns ``(ok, reason)``. The UPDATE is guarded by
        ``status='pending' AND expires_at > now`` so:
          - a second concurrent approve sees rowcount 0 → loses the
            race cleanly (reason='already_resolved')
          - an expired pending can't be approved (reason='expired')
          - a wrong pairing code (when provided) is rejected BEFORE
            the update (reason='bad_code')

        ``pairing_code`` is optional: the dashboard "Bind to me"
        button approves by binding_id (the operator sees the queue);
        a stricter flow can also require the code typed from the host.
        """
        if not operator_user_id:
            return False, "no_operator"
        self.init_db(project_root)
        now_iso = _iso_now()
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT status, expires_at, pairing_code_hash "
                "FROM host_operator_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                return False, "not_found"
            if row["status"] != "pending":
                return False, "already_resolved"
            if str(row["expires_at"]) <= now_iso:
                # Mark it expired so the queue self-cleans.
                conn.execute(
                    "UPDATE host_operator_bindings SET status='expired' "
                    "WHERE binding_id=? AND status='pending'",
                    (binding_id,),
                )
                conn.commit()
                return False, "expired"
            if pairing_code is not None:
                if _hash_code(pairing_code) != row["pairing_code_hash"]:
                    return False, "bad_code"
            # Atomic pending→approved. The WHERE clause re-checks
            # status so a racing approve can't double-bind.
            cur = conn.execute(
                "UPDATE host_operator_bindings "
                "SET status='approved', operator_user_id=?, "
                "    approved_at=?, approved_by_role=? "
                "WHERE binding_id=? AND status='pending' "
                "      AND expires_at > ?",
                (
                    operator_user_id,
                    now_iso,
                    approved_by_role,
                    binding_id,
                    now_iso,
                ),
            )
            conn.commit()
            if (cur.rowcount or 0) == 0:
                return False, "already_resolved"
        return True, "approved"

    def revoke(
        self,
        project_root: Path,
        *,
        binding_id: str,
    ) -> bool:
        """Revoke a binding (approved or pending), no ownership check.

        Low-level primitive — callers that need owner-or-admin
        enforcement use ``revoke_with_owner_check``. Returns True iff
        a row transitioned to revoked.
        """
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            cur = conn.execute(
                "UPDATE host_operator_bindings SET status='revoked' "
                "WHERE binding_id=? AND status IN ('pending','approved')",
                (binding_id,),
            )
            conn.commit()
        return (cur.rowcount or 0) > 0

    def revoke_with_owner_check(
        self,
        project_root: Path,
        *,
        binding_id: str,
        requester_user_id: str,
        is_admin: bool,
    ) -> tuple[bool, str]:
        """Owner-or-admin revoke. Returns ``(ok, reason)``.

        Authorization matrix:
          - approved binding, operator_user_id == requester  → owner,
            allowed.
          - approved binding, operator_user_id != requester  → allowed
            ONLY when is_admin (admin override), else 'not_owner'.
          - pending binding (operator_user_id == '')          → unowned;
            no operator has claimed it, so only an admin may decline
            it ('pending_requires_admin' otherwise). This is the
            explicit rule for unowned-pending revoke.
          - missing                                           → 'not_found'.
          - already revoked / expired                         →
            'already_resolved'.

        The actual transition is atomic (UPDATE WHERE status IN
        (pending,approved)) so a concurrent revoke can't double-count.
        """
        if not requester_user_id and not is_admin:
            return False, "unauthenticated"
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT status, operator_user_id FROM host_operator_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                return False, "not_found"
            status = str(row["status"])
            owner = str(row["operator_user_id"] or "")
            if status not in ("pending", "approved"):
                return False, "already_resolved"
            # Authorization decision.
            if owner and owner == requester_user_id:
                pass  # owner revoke
            elif is_admin:
                pass  # admin override (owned or unowned)
            elif owner == "":
                return False, "pending_requires_admin"
            else:
                return False, "not_owner"
            cur = conn.execute(
                "UPDATE host_operator_bindings SET status='revoked' "
                "WHERE binding_id=? AND status IN ('pending','approved')",
                (binding_id,),
            )
            conn.commit()
            if (cur.rowcount or 0) == 0:
                return False, "already_resolved"
        return True, "revoked"

    # ------------------------------------------------------------------
    # Hook side: resolve the bound operator for a host session
    # ------------------------------------------------------------------

    def resolve_operator(
        self,
        project_root: Path,
        host_session_id: str,
    ) -> str | None:
        """Return the operator_user_id bound to host_session_id, or
        None when no approved+live binding exists. Used by hooks to
        build an operator_context from the host's session UUID.

        Only an ``approved`` row that has not expired resolves. A
        revoked binding returns None (the hook falls back to
        unauthenticated).
        """
        if not host_session_id:
            return None
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT operator_user_id, expires_at, status "
                "FROM host_operator_bindings "
                "WHERE host_session_id=? AND status='approved' "
                "ORDER BY approved_at DESC LIMIT 1",
                (host_session_id,),
            ).fetchone()
        if row is None:
            return None
        # Approved bindings outlive the pending TTL — once bound, the
        # operator stays bound until revoked. (expires_at governs the
        # PENDING window only; we keep the column for audit but do not
        # expire an approved binding on it.)
        uid = str(row["operator_user_id"] or "")
        return uid or None

    def get(
        self,
        project_root: Path,
        binding_id: str,
    ) -> HostBinding | None:
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT * FROM host_operator_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        return self._row(row) if row else None

    def _row(self, r: sqlite3.Row) -> HostBinding:
        return HostBinding(
            binding_id=r["binding_id"],
            host_kind=r["host_kind"],
            host_session_id=r["host_session_id"],
            project_root=r["project_root"],
            aidocs_session_id=r["aidocs_session_id"],
            requested_identity=r["requested_identity"],
            status=r["status"],
            operator_user_id=r["operator_user_id"],
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            approved_at=r["approved_at"],
            approved_by_role=r["approved_by_role"],
        )
