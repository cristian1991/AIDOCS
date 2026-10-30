"""#475 (2) — conductor-minted session-scaffold work-grants.

A conductor (bound managed-mode session) may mint a short-lived grant
naming a session_id PATTERN (fnmatch, e.g. ``war-*``) that permits a
dispatched agent's ``task_begin`` to scaffold that named session. The
dispatch itself is the operator-chain authority; the grant makes it
legible and bounded (pattern scope + TTL) instead of forcing wars to
hand-write ``.MEMORY/sessions/<id>/SESSION.md``.

Storage mirrors the query-gate user-intent-grant precedent (sqlite in
aidocs.sqlite3, provenance columns, TTL) — same architecture, its own
table because these grants are keyed by PATTERN over sessions that do
not exist yet, not by an existing session row. Minting and consumption
are audit-stamped via ``ExecutionIndexStore.record_event`` by the
callers in ``RuntimeService``.
"""

from __future__ import annotations

import fnmatch
import time
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase

# TTL bounds: short-lived by design. A grant is a dispatch artifact,
# not a standing permission.
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600
DEFAULT_TTL_SECONDS = 900


class SessionScaffoldGrantStore(SQLiteIndexStoreBase):
    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_scaffold_grants (
                    grant_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern TEXT NOT NULL,
                    granted_by_session TEXT NOT NULL,
                    host_session_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at_epoch REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                )
                """,
            )

    def mint(
        self,
        project_root: Path,
        *,
        pattern: str,
        granted_by_session: str,
        host_session_id: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Insert a grant row and return it. Caller enforces authority
        (conductor binding) and records the audit event."""
        ttl = max(MIN_TTL_SECONDS, min(int(ttl_seconds or DEFAULT_TTL_SECONDS), MAX_TTL_SECONDS))
        expires = time.time() + ttl
        created_at = self._timestamp()
        self.init_db(project_root)
        with self.session(project_root) as conn:
            cur = conn.execute(
                "INSERT INTO session_scaffold_grants "
                "(pattern, granted_by_session, host_session_id, created_at, "
                "expires_at_epoch) VALUES (?, ?, ?, ?, ?)",
                (pattern, granted_by_session, host_session_id, created_at, expires),
            )
            grant_id = int(cur.lastrowid)
        return {
            "grant_id": grant_id,
            "pattern": pattern,
            "granted_by_session": granted_by_session,
            "host_session_id": host_session_id,
            "created_at": created_at,
            "expires_at_epoch": expires,
            "ttl_seconds": ttl,
        }

    def find_active(self, project_root: Path, session_id: str) -> dict[str, Any] | None:
        """Newest unexpired, unrevoked grant whose pattern matches
        ``session_id`` (fnmatch, case-sensitive); None when no grant
        covers it."""
        sid = (session_id or "").strip()
        if not sid:
            return None
        self.init_db(project_root)
        now = time.time()
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT * FROM session_scaffold_grants "
                "WHERE revoked = 0 AND expires_at_epoch > ? "
                "ORDER BY grant_id DESC",
                (now,),
            ).fetchall()
        for row in rows:
            if fnmatch.fnmatchcase(sid, str(row["pattern"])):
                return dict(row)
        return None
