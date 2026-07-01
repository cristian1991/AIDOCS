"""Canonical SQLite registry for session existence/membership.

The control decision "does session <id> exist and belong to this project?"
(project_authority.session_belongs — the gate a local/cross bind keys off)
is read from the ``session_membership`` table, NOT from the presence of a
SESSION.md file. SESSION.md is the exported verbatim record (human-readable,
authored by the doctrine), not the authority for the membership decision.

Deleting SESSION.md does NOT revoke membership — authority lives in SQL.
Conversely, dropping a stray SESSION.md into the sessions tree does NOT
mint membership: only ``register`` (called by create_session) or the
explicit one-time legacy migration can. That STRENGTHENS the old file
guard — a bare folder was already rejected; now an unregistered SESSION.md
is too.

Read/write doctrine (legacy-ingest seal): normal read and write paths
(``is_member``, ``list_members``, ``register``, ``unregister``) only ensure
the schema exists — they are otherwise SQL-only and side-effect-free, and
they NEVER scan the sessions tree for SESSION.md files. A read can therefore
never MINT membership from a file. The one-time legacy import of existing
``.MEMORY/sessions/<id>/SESSION.md`` lives behind the explicit, authenticated
migration entry point ``migrate_legacy_once`` (driven by the
``migrate-control-authority`` admin CLI command / bounded bootstrap phase).
It imports once and seals a marker; afterwards the filesystem is never
scanned for authority again.
"""

from __future__ import annotations

from pathlib import Path

from ._sqlite_index_store_base import SQLiteIndexStoreBase

_SESSION_FILE = "SESSION.md"

# Process-level "schema ensured" guard (UPS sqlite-open seal, 2026-06-02): keyed
# by resolved db path. ensure_schema is idempotent CREATE-IF-NOT-EXISTS DDL;
# managed_mode.get_mode's membership annotation calls is_member (→ ensure_schema)
# many times per hook event, each a fresh sqlite open. Ensure AT MOST ONCE per
# (process, db). The membership READ/WRITE each still opens its own connection,
# so authority semantics and cross-process truth are unchanged.
_MEMBERSHIP_SCHEMA_ENSURED: set[str] = set()


def _valid_session_id(sid: str) -> str:
    """Validate a session id before it can enter SQL authority. A directory
    name with separators/traversal/surrounding whitespace is never a valid
    session key. Returns "" when valid, else a human reason.
    """
    if not sid:
        return "empty session id"
    if any(c in sid for c in ("/", "\\", "..")) or sid != sid.strip():
        return f"invalid session id: {sid!r}"
    return ""


class SessionMembershipStore(SQLiteIndexStoreBase):
    def ensure_schema(self, project_root: Path) -> None:
        """Create the membership tables if absent. Side-effect-free beyond
        idempotent schema creation — SAFE on read paths. NEVER scans the
        sessions tree (that is reserved for migrate_legacy_once).
        """
        key = str(self.db_path(project_root))
        if key in _MEMBERSHIP_SCHEMA_ENSURED:
            return
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_membership (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'create'
                );
                CREATE TABLE IF NOT EXISTS session_membership_meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                );
                """,
            )
        _MEMBERSHIP_SCHEMA_ENSURED.add(key)

    # Back-compat alias: schema-only, NO legacy ingest.
    def init_db(self, project_root: Path) -> None:
        self.ensure_schema(project_root)

    # ── explicit one-time legacy migration (NOT a read/write side effect) ─
    def migrate_legacy_once(self, project_root: Path) -> dict:
        """Import every pre-registry ``.MEMORY/sessions/<id>/SESSION.md`` into
        SQL exactly ONCE, then seal.

        This is the ONLY path that scans the sessions tree into authority. It
        is invoked explicitly by the authenticated ``migrate-control-authority``
        command (or the bounded bootstrap phase) — never by ``is_member`` /
        ``list_members`` or a normal register. A stale or freshly placed
        SESSION.md therefore cannot mint membership through a read.

        Each candidate session id (directory name carrying a SESSION.md) is
        VALIDATED before import; an invalid id is SKIPPED (recorded in
        ``skipped``) so a malformed directory never becomes authority. A bare
        folder with no SESSION.md is ignored (the pre-existing guard holds).

        Marker sealed only AFTER a successful import; an OSError mid-scan
        propagates WITHOUT sealing (retry next migration). Registration is
        idempotent (INSERT OR IGNORE). Skipped invalid ids do not block
        sealing — they are permanently invalid, not retryable.

        Returns {"status": already_migrated|migrated, "imported": N,
                 "skipped": [{"session_id", "reason"}, ...]}.
        """
        self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            done = conn.execute(
                "SELECT 1 FROM session_membership_meta WHERE k = 'ingested'",
            ).fetchone()
        if done:
            return {"status": "already_migrated", "imported": 0, "skipped": []}
        sessions_dir = project_root / ".MEMORY" / "sessions"
        imported = 0
        skipped: list[dict] = []
        if sessions_dir.is_dir():
            for child in sorted(sessions_dir.iterdir()):  # OSError → no seal
                if not (child / _SESSION_FILE).is_file():
                    continue  # bare folder is not a session (guard preserved)
                sid = child.name
                reason = _valid_session_id(sid)
                if reason:
                    skipped.append({"session_id": sid, "reason": reason})
                    continue
                self.register(project_root, sid, source="migrated", _skip_init=True)
                imported += 1
        # Import succeeded (or nothing to import) — seal the marker.
        with self.session(project_root) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_membership_meta (k, v) VALUES ('ingested', '1')",
            )
        return {"status": "migrated", "imported": imported, "skipped": skipped}

    # ── authority mutations ─────────────────────────────────────────
    def register(
        self,
        project_root: Path,
        session_id: str,
        *,
        source: str = "create",
        _skip_init: bool = False,
    ) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        if not _skip_init:
            self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO session_membership "
                "(session_id, created_at, source) VALUES (?, ?, ?)",
                (sid, self._timestamp(), source),
            )

    def unregister(self, project_root: Path, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            cur = conn.execute("DELETE FROM session_membership WHERE session_id = ?", (sid,))
            return cur.rowcount > 0

    # ── authority reads ─────────────────────────────────────────────
    def is_member(self, project_root: Path, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        # READ: schema-only, SQL-only. Never scans for SESSION.md files.
        self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT 1 FROM session_membership WHERE session_id = ?",
                (sid,),
            ).fetchone()
        return row is not None

    def list_members(self, project_root: Path) -> list[str]:
        # READ: schema-only, SQL-only. Never scans for SESSION.md files.
        self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT session_id FROM session_membership ORDER BY session_id",
            ).fetchall()
        return [r["session_id"] for r in rows]

    # ── seal state + bounded self-heal ──────────────────────────────
    def is_sealed(self, project_root: Path) -> bool:
        """True once the one-time legacy ingest has run (marker present).

        READ: schema-only, SQL-only — never scans the sessions tree. When
        sealed, ``ensure_member_or_heal`` will NEVER import a SESSION.md
        again; only ``create_session``/``register`` can mint membership
        afterwards. This is the gate that keeps file presence from ever
        becoming authority after the bounded migration completes.
        """
        self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT 1 FROM session_membership_meta WHERE k = 'ingested'",
            ).fetchone()
        return row is not None

    def on_disk_session_ids(self, project_root: Path) -> list[str]:
        """Directory names under .MEMORY/sessions/ carrying a valid SESSION.md.

        Observability ONLY — NOT an authority read. Used to report a
        list-vs-registry mismatch (legacy sessions that predate the registry
        and have not been migrated). Membership decisions never consult this;
        they read ``is_member``/``list_members`` exclusively.
        """
        out: list[str] = []
        sessions_dir = project_root / ".MEMORY" / "sessions"
        try:
            children = sorted(sessions_dir.iterdir())
        except OSError:
            return out
        for child in children:
            try:
                # _valid_session_id returns "" when the id is VALID.
                if (child / _SESSION_FILE).is_file() and not _valid_session_id(child.name):
                    out.append(child.name)
            except OSError:
                continue
        return out

    def ensure_member_or_heal(self, project_root: Path, session_id: str) -> bool:
        """Single membership-authority decision for any path where a session
        BECOMES active or session-scoped authority is implied.

        Fail-closed and SQL-authoritative:
          1. If already a member -> True (fast PK lookup, no scan).
          2. Else, ONLY when the legacy-ingest marker is ABSENT (on-disk
             sessions clearly predate the registry), run the bounded,
             idempotent, self-sealing ``migrate_legacy_once`` and re-check.
          3. Else (sealed, or still not a member after a heal) -> False.

        This NEVER mints authority from an arbitrary SESSION.md after the
        seal, and never makes file presence authority on its own: the only
        file->SQL path is the explicit, one-time, marker-gated migration.
        Pure-read callers (``session_belongs``) MUST NOT use this; they stay
        side-effect-free. Use this only at bind/active boundaries.
        """
        sid = (session_id or "").strip()
        if not sid:
            return False
        if self.is_member(project_root, sid):
            return True
        if not self.is_sealed(project_root):
            # bounded one-time legacy import (scans tree exactly once, seals).
            self.migrate_legacy_once(project_root)
            return self.is_member(project_root, sid)
        return False
