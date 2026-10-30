"""SQL registry for DO NOT TOUCH protected files — authoritative
source for "who protected this" so identity-match checks on
`ai_protect(remove)` don't trust the file header (which is writable).

Header stays informational (why + pair_files for human readers).
Removal flow reads this table to verify the requester matches the
protector, else escalates to admin via escalation_hook.

Co-located with identity_store's DB so the registry-owner and
user-identity stay in a single file for FK safety.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

# Owner id of every row a DNT header SYNC writes. Rows with any other owner
# were written by an operator (ai_protect add) and no sync may touch them.
SYSTEM_DNT_OWNER = "system_dnt_sync"


@dataclass(frozen=True)
class ProtectionRecord:
    path: str
    protected_by_user_id: str
    protected_at: str
    why: str
    pair_files: list[str]
    machine_id: str
    # #62 (2026-04-27): structured DNT header fields. Empty/default
    # for legacy rows (path-only protection added via ai_protect).
    # Populated when ai_protect mode='sync' parses a structured DNT
    # header from disk (per the operator-named schema:
    # dnt-id / dnt-master / dnt-pair / dnt-baseline / dnt-cost /
    # dnt-incident / dnt-forbid / dnt-allow / dnt-on-edit).
    dnt_id: str = ""
    dnt_role: str = ""  # "master" | "satellite" | ""
    forbid_list: list[str] = ()
    allow_list: list[str] = ()
    incidents: list[str] = ()
    baseline: str = ""
    cost: str = ""
    full_header_text: str = ""
    # #205 (Memory Slice 4): optional SYMBOL scope. Empty = whole-file
    # protection (legacy + default). Non-empty = the protection covers
    # exactly this function/method (qualified name), giving DNT the same
    # unit granularity memory_symbol_anchors already has.
    symbol: str = ""


class ProtectedFileRegistryStore:
    """Single-table registry of (project_root, path) → protection owner.

    Writes on ai_protect(add); reads on ai_protect(remove) /
    ai_protect(list); deletes on successful remove.
    """

    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs_identity.sqlite3"

    def init_db(self, project_root: Path) -> None:
        path = self.db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # #746: this file is SHARED by five stores, so whichever creates it
        # decides the journal mode every later connection inherits (journal_mode
        # lives in the FILE HEADER). Every creator therefore goes through the one
        # canonical connect (#755) -- WAL by luck is not WAL by design.
        with _canonical_connect(path, durability=_Durability.RUNTIME) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS protected_file_registry (
                    path TEXT PRIMARY KEY,
                    protected_by_user_id TEXT NOT NULL,
                    protected_at TEXT NOT NULL,
                    why TEXT NOT NULL DEFAULT '',
                    pair_files TEXT NOT NULL DEFAULT '[]',
                    machine_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS
                    idx_protected_file_registry_user
                    ON protected_file_registry(protected_by_user_id);
            """)
            # #151-mirror migration: rename legacy dnt_banners_shown
            # column cli_session_id -> epoch_id (the stored value was
            # always the derived agent_memory_epoch). Guarded +
            # idempotent — no-op on fresh DBs and safe to run twice.
            try:
                _bs_cols = [
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(dnt_banners_shown)"
                    ).fetchall()
                ]
                if "cli_session_id" in _bs_cols and "epoch_id" not in _bs_cols:
                    conn.execute(
                        "ALTER TABLE dnt_banners_shown "
                        "RENAME COLUMN cli_session_id TO epoch_id"
                    )
            except Exception:
                pass
            # #62 migration (2026-04-27): structured DNT header columns.
            # Idempotent — ALTER TABLE ADD COLUMN with IF NOT EXISTS
            # via try/except.
            for ddl in (
                "ALTER TABLE protected_file_registry ADD COLUMN dnt_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE protected_file_registry ADD COLUMN dnt_role TEXT NOT NULL DEFAULT ''",
                ("ALTER TABLE protected_file_registry ADD COLUMN "
                "forbid_list TEXT NOT NULL DEFAULT '[]'"),
                ("ALTER TABLE protected_file_registry ADD COLUMN "
                "allow_list TEXT NOT NULL DEFAULT '[]'"),
                ("ALTER TABLE protected_file_registry ADD COLUMN "
                "incidents TEXT NOT NULL DEFAULT '[]'"),
                "ALTER TABLE protected_file_registry ADD COLUMN baseline TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE protected_file_registry ADD COLUMN cost TEXT NOT NULL DEFAULT ''",
                ("ALTER TABLE protected_file_registry ADD COLUMN "
                "full_header_text TEXT NOT NULL DEFAULT ''"),
                # #205: symbol-scope column (additive, guarded — same
                # pattern as the #62/#104 migrations). '' = whole file.
                ("ALTER TABLE protected_file_registry ADD COLUMN "
                "symbol TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    conn.execute(ddl)
                except Exception:
                    pass  # column already exists
            # Family lookup index: by dnt_id (for "all files in this
            # family") and by role (for "find the master").
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS
                    idx_protected_file_registry_dnt_id
                    ON protected_file_registry(dnt_id);
                CREATE INDEX IF NOT EXISTS
                    idx_protected_file_registry_dnt_role
                    ON protected_file_registry(dnt_id, dnt_role);
            """)
            # Per-conductor banner suppression: track which DNT
            # families this conversation has already seen the banner
            # for.
            #
            # Column `epoch_id` stores the derived agent_memory_epoch
            # (sha256 over host_kind + project_root +
            # aidocs_work_session_id + host_session_id +
            # compaction_count). See dnt_banner_injector.py and
            # agent_memory_epoch.py for the derivation.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dnt_banners_shown (
                    epoch_id TEXT NOT NULL,
                    dnt_id TEXT NOT NULL,
                    first_shown_at TEXT NOT NULL,
                    PRIMARY KEY (epoch_id, dnt_id)
                );
            """)
            # MUTATION REVISION (r0b2 re-review P0): one row, bumped by
            # triggers on EVERY insert / update / delete of the registry, so
            # any writer - this code, an older build, a raw sqlite3 client -
            # moves it. `instance` is random per DB file, so a recreated or
            # different DB never reproduces an old (instance, rev) pair.
            # system_dnt_fingerprint() reads it; no aggregate is trusted.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dnt_registry_revision (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    instance TEXT NOT NULL,
                    rev INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS trg_protected_file_registry_rev_ins
                    AFTER INSERT ON protected_file_registry
                    BEGIN UPDATE dnt_registry_revision SET rev = rev + 1 WHERE id = 1; END;
                CREATE TRIGGER IF NOT EXISTS trg_protected_file_registry_rev_upd
                    AFTER UPDATE ON protected_file_registry
                    BEGIN UPDATE dnt_registry_revision SET rev = rev + 1 WHERE id = 1; END;
                CREATE TRIGGER IF NOT EXISTS trg_protected_file_registry_rev_del
                    AFTER DELETE ON protected_file_registry
                    BEGIN UPDATE dnt_registry_revision SET rev = rev + 1 WHERE id = 1; END;
                -- PER-FILE DNT ATTESTATION (r0b2 d6b3dac7-69a): the file version
                -- (mtime_ns, size_bytes) at which a file's DNT state was last
                -- observed, and the digest of the registry row that state
                -- implies ('' = no header). Written in the SAME transaction as
                -- the row it vouches for (record_dnt_headers /
                -- remove_system_rows / attest_files).
                CREATE TABLE IF NOT EXISTS dnt_file_attestation (
                    path TEXT PRIMARY KEY,
                    mtime_ns INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    row_digest TEXT NOT NULL,
                    attested_at TEXT NOT NULL
                );
            """)
            if conn.execute("SELECT 1 FROM dnt_registry_revision WHERE id = 1").fetchone() is None:
                conn.execute(
                    "INSERT OR IGNORE INTO dnt_registry_revision (id, instance, rev) "
                    "VALUES (1, lower(hex(randomblob(16))), 0)"
                )
            conn.commit()

    def record(
        self,
        project_root: Path,
        *,
        path: str,
        protected_by_user_id: str,
        why: str = "",
        pair_files: list[str] | None = None,
        machine_id: str = "",
        symbol: str = "",
    ) -> None:
        """Insert or replace the protection record. Replacement shape
        is a deliberate over-protect → re-protect flow: if the file
        already carries a header and someone runs ai_protect(add) again
        with a new why or identity, the authoritative record updates.
        Re-protection doesn't lose the original — the why field can be
        appended by the caller if they want history.
        """
        self.init_db(project_root)
        rec_path = path.replace("\\", "/").lstrip("/")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pair_json = json.dumps(pair_files or [])
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO protected_file_registry "
                "(path, protected_by_user_id, protected_at, why, "
                " pair_files, machine_id, symbol) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rec_path,
                    protected_by_user_id,
                    ts,
                    why,
                    pair_json,
                    machine_id,
                    (symbol or "").strip(),
                ),
            )
            conn.commit()

    def get(
        self,
        project_root: Path,
        path: str,
    ) -> ProtectionRecord | None:
        self.init_db(project_root)
        rec_path = path.replace("\\", "/").lstrip("/")
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT path, protected_by_user_id, protected_at, why, "
                "pair_files, machine_id, COALESCE(symbol, '') AS symbol "
                "FROM protected_file_registry "
                "WHERE path = ?",
                (rec_path,),
            ).fetchone()
        if row is None:
            return None
        try:
            pairs = json.loads(row["pair_files"] or "[]")
            if not isinstance(pairs, list):
                pairs = []
        except (json.JSONDecodeError, TypeError):
            pairs = []
        return ProtectionRecord(
            path=str(row["path"]),
            protected_by_user_id=str(row["protected_by_user_id"]),
            protected_at=str(row["protected_at"]),
            why=str(row["why"] or ""),
            pair_files=pairs,
            machine_id=str(row["machine_id"] or ""),
            symbol=str(row["symbol"] or ""),
        )

    def protection_for_unit(
        self,
        project_root: Path,
        path: str,
        *,
        symbol: str = "",
    ) -> ProtectionRecord | None:
        """#205 symbol-aware coverage resolver for a {file, symbol} unit.

        - a whole-file row (symbol='') covers the file and EVERY symbol;
        - a symbol-scoped row covers exactly its symbol;
        - a whole-file query (symbol='') is covered by ANY row on the
          path — conservative: the file contains a protected unit and
          editors must know before touching it.
        Returns the covering record or None.
        """
        rec = self.get(project_root, path)
        if rec is None:
            return None
        want = (symbol or "").strip()
        if not rec.symbol or not want:
            return rec
        return rec if rec.symbol == want else None

    def remove(self, project_root: Path, path: str) -> bool:
        """Drop the registry row. Returns True if a row was deleted,
        False when the file wasn't in the registry (header-only
        orphan — caller decides whether to proceed).
        """
        self.init_db(project_root)
        rec_path = path.replace("\\", "/").lstrip("/")
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            cur = conn.execute(
                "DELETE FROM protected_file_registry WHERE path = ?",
                (rec_path,),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_all(
        self,
        project_root: Path,
    ) -> list[ProtectionRecord]:
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT path, protected_by_user_id, protected_at, why, "
                "pair_files, machine_id FROM protected_file_registry "
                "ORDER BY path ASC",
            ).fetchall()
        out: list[ProtectionRecord] = []
        for row in rows:
            try:
                pairs = json.loads(row["pair_files"] or "[]")
                if not isinstance(pairs, list):
                    pairs = []
            except (json.JSONDecodeError, TypeError):
                pairs = []
            out.append(
                ProtectionRecord(
                    path=str(row["path"]),
                    protected_by_user_id=str(row["protected_by_user_id"]),
                    protected_at=str(row["protected_at"]),
                    why=str(row["why"] or ""),
                    pair_files=pairs,
                    machine_id=str(row["machine_id"] or ""),
                ),
            )
        return out

    # ── #62 (2026-04-27): structured DNT header support ──

    def record_dnt_header(
        self,
        project_root: Path,
        *,
        path: str,
        dnt_id: str,
        dnt_role: str,
        master: str = "",
        pair_files: list[str] | None = None,
        forbid_list: list[str] | None = None,
        allow_list: list[str] | None = None,
        incidents: list[str] | None = None,
        baseline: str = "",
        cost: str = "",
        full_header_text: str = "",
        why: str = "",
        protected_by_user_id: str = "system_dnt_sync",
        machine_id: str = "",
        only_if_system: bool = False,
    ) -> bool:
        """Upsert a row with structured DNT header fields populated.

        Used by ai_protect mode='sync' (Phase 2) when parsing a DNT
        header from disk. dnt_id is the family key. dnt_role is
        'master' (file holds full payload) or 'satellite' (file
        carries 1-line reference to master).

        Distinct from `record()`: that's the legacy ai_protect add
        flow, where `protected_by_user_id` is a real human's id and
        `why` is a free-text protection reason. Here, the DNT header
        IS the contract; user_id defaults to 'system_dnt_sync' so the
        ownership-check on remove still works (only an admin can
        remove a DNT-synced row, not a random user).

        OWNERSHIP (r0b2 review): ``only_if_system=True`` is the mode every
        header SYNC uses. The row is inserted when the path has none and
        updated only when the existing row is owned by SYSTEM_DNT_OWNER; an
        operator row (ai_protect add) is never replaced. The guard is the
        upsert's own WHERE clause, so no read-then-write race can bypass it.
        Returns True when a row was written, False when an operator row on
        the path was preserved.
        """
        self.init_db(project_root)
        rec_path = path.replace("\\", "/").lstrip("/")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cols = (
            "path, protected_by_user_id, protected_at, why, pair_files, machine_id, "
            "dnt_id, dnt_role, forbid_list, allow_list, incidents, baseline, cost, "
            "full_header_text"
        )
        values = (
            rec_path,
            protected_by_user_id,
            ts,
            why,
            json.dumps(pair_files or []),
            machine_id,
            dnt_id,
            dnt_role,
            json.dumps(forbid_list or []),
            json.dumps(allow_list or []),
            json.dumps(incidents or []),
            baseline,
            cost,
            full_header_text,
        )
        qm = ", ".join("?" * len(values))
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            before = self._begin_counted(conn)
            if not only_if_system:
                conn.execute(f"INSERT OR REPLACE INTO protected_file_registry ({cols}) VALUES ({qm})", values)
                self._commit_counted(conn, project_root, before)
                return True
            # Same columns as the INSERT OR REPLACE above (symbol reset to its
            # default, as a replace would), but only over a SYSTEM row.
            updates = ", ".join(f"{c.strip()} = excluded.{c.strip()}" for c in cols.split(",")[1:])
            cur = conn.execute(
                f"INSERT INTO protected_file_registry ({cols}) VALUES ({qm}) "
                f"ON CONFLICT(path) DO UPDATE SET {updates}, symbol = '' "
                "WHERE protected_file_registry.protected_by_user_id = ?",
                (*values, SYSTEM_DNT_OWNER),
            )
            self._commit_counted(conn, project_root, before)
            return cur.rowcount > 0

    # ── per-file attestation ──

    @staticmethod
    def _write_attestations(conn: Any, items: Any, drop: Any = ()) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for path, mtime_ns, size_bytes, digest in items or ():
            conn.execute(
                "INSERT OR REPLACE INTO dnt_file_attestation (path, mtime_ns, size_bytes, row_digest, attested_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(path).replace("\\", "/").lstrip("/"), int(mtime_ns), int(size_bytes), str(digest), ts),
            )
        for path in drop or ():
            conn.execute(
                "DELETE FROM dnt_file_attestation WHERE path = ?", (str(path).replace("\\", "/").lstrip("/"),)
            )

    def attest_files(self, project_root: Path, items: Any, *, drop: Any = ()) -> None:
        """Record (path, mtime_ns, size_bytes, row_digest) attestations and drop
        the given paths' attestations, in ONE transaction (no row change)."""
        items, drop = list(items or ()), list(drop or ())
        if not items and not drop:
            return
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            before = self._begin_counted(conn)
            try:
                self._write_attestations(conn, items, drop)
            except Exception:
                conn.rollback()
                raise
            self._commit_counted(conn, project_root, before)

    def get_attestations(
        self, project_root: Path, paths: list[str] | set[str] | None = None
    ) -> dict[str, tuple[int, int, str]]:
        """path -> (mtime_ns, size_bytes, row_digest); all, or just ``paths``."""
        self.init_db(project_root)
        out: dict[str, tuple[int, int, str]] = {}
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            if paths is None:
                rows = conn.execute("SELECT path, mtime_ns, size_bytes, row_digest FROM dnt_file_attestation")
                out = {str(r[0]): (int(r[1]), int(r[2]), str(r[3])) for r in rows}
            else:
                wanted = sorted({str(p).replace("\\", "/").lstrip("/") for p in paths})
                for i in range(0, len(wanted), 500):
                    chunk = wanted[i : i + 500]
                    qm = ",".join("?" * len(chunk))
                    for r in conn.execute(
                        f"SELECT path, mtime_ns, size_bytes, row_digest FROM dnt_file_attestation WHERE path IN ({qm})",
                        tuple(chunk),
                    ):
                        out[str(r[0])] = (int(r[1]), int(r[2]), str(r[3]))
        return out

    def record_dnt_headers(
        self, project_root: Path, rows: list[dict[str, Any]], *, attestations: Any = ()
    ) -> list[bool]:
        """record_dnt_header(only_if_system=True) for MANY rows in ONE
        connection and ONE BEGIN IMMEDIATE transaction (one init, one pragma
        setup), own revision bumps counted once for the whole batch inside
        that transaction. Each row dict takes record_dnt_header's keyword
        fields (``path``, ``dnt_id``, ``dnt_role`` required). Returns, per
        row, whether it was written (False: an operator row was preserved).
        All-or-nothing: any row failing rolls the whole batch back and
        raises, so the caller reports every row of the batch as failed."""
        if not rows:
            return []
        self.init_db(project_root)
        cols = (
            "path, protected_by_user_id, protected_at, why, pair_files, machine_id, "
            "dnt_id, dnt_role, forbid_list, allow_list, incidents, baseline, cost, "
            "full_header_text"
        )
        updates = ", ".join(f"{c.strip()} = excluded.{c.strip()}" for c in cols.split(",")[1:])
        sql = (
            f"INSERT INTO protected_file_registry ({cols}) VALUES ({', '.join('?' * 14)}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}, symbol = '' "
            "WHERE protected_file_registry.protected_by_user_id = ?"
        )
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        written: list[bool] = []
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            before = self._begin_counted(conn)
            try:
                for r in rows:
                    cur = conn.execute(
                        sql,
                        (
                            str(r["path"]).replace("\\", "/").lstrip("/"),
                            SYSTEM_DNT_OWNER,
                            ts,
                            r.get("why", ""),
                            json.dumps(r.get("pair_files") or []),
                            r.get("machine_id", ""),
                            r["dnt_id"],
                            r["dnt_role"],
                            json.dumps(r.get("forbid_list") or []),
                            json.dumps(r.get("allow_list") or []),
                            json.dumps(r.get("incidents") or []),
                            r.get("baseline", ""),
                            r.get("cost", ""),
                            r.get("full_header_text", ""),
                            SYSTEM_DNT_OWNER,
                        ),
                    )
                    written.append(cur.rowcount > 0)
                # the attestations vouching for these rows, in the same txn
                self._write_attestations(conn, attestations)
            except Exception:
                conn.rollback()
                raise
            self._commit_counted(conn, project_root, before)
        return written

    # ── own-write accounting (r0b2 re-review P1) ──
    #
    # A sync must tell ITS OWN registry writes from anyone else's: it accepts
    # an end revision as verified only when it equals the start revision plus
    # the bumps of its own writes. Each counted write runs in BEGIN IMMEDIATE
    # and reads the revision before and after inside that one transaction, so
    # no other writer can land between the two reads. Counted per store
    # INSTANCE and per DB file: a sync uses one instance for all its writes;
    # writes through other instances (other threads, banners) stay foreign.

    def _rev_value(self, conn: Any) -> int | None:
        row = conn.execute("SELECT rev FROM dnt_registry_revision WHERE id = 1").fetchone()
        return int(row[0]) if row else None

    def _begin_counted(self, conn: Any) -> int | None:
        conn.execute("BEGIN IMMEDIATE")
        return self._rev_value(conn)

    def _commit_counted(self, conn: Any, project_root: Path, before: int | None) -> None:
        after = self._rev_value(conn)
        conn.commit()
        if before is not None and after is not None:
            bumps = getattr(self, "_own_bumps", None)
            if bumps is None:
                bumps = self._own_bumps = {}
            key = str(self.db_path(project_root))
            bumps[key] = bumps.get(key, 0) + (after - before)

    def own_revision_bumps(self, project_root: Path) -> int:
        """Revision bumps caused by writes made through THIS instance."""
        return int((getattr(self, "_own_bumps", None) or {}).get(str(self.db_path(project_root)), 0))

    def remove_system_rows(
        self, project_root: Path, paths: list[str], *, attestations: Any = (), drop_attestations: Any = ()
    ) -> list[str]:
        """Delete the rows of ``paths`` ONLY where SYSTEM_DNT_OWNER owns them
        (the stale-row reconcile), in ONE transaction; returns the paths
        actually deleted. The owner test is inside each DELETE, so an
        operator row on a path is never deleted. ``attestations`` (e.g. "no
        header at version V") and ``drop_attestations`` are applied in the
        SAME transaction: a removal and the attestation explaining it commit
        or roll back together."""
        if not paths and not attestations and not drop_attestations:
            return []
        self.init_db(project_root)
        deleted: list[str] = []
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            before = self._begin_counted(conn)
            try:
                for p in paths:
                    rec_path = p.replace("\\", "/").lstrip("/")
                    cur = conn.execute(
                        "DELETE FROM protected_file_registry WHERE path = ? AND protected_by_user_id = ?",
                        (rec_path, SYSTEM_DNT_OWNER),
                    )
                    if cur.rowcount > 0:
                        deleted.append(p)
                self._write_attestations(conn, attestations, drop_attestations)
            except Exception:
                conn.rollback()
                raise
            self._commit_counted(conn, project_root, before)
        return deleted

    def list_system_dnt_paths(
        self, project_root: Path, paths: list[str] | set[str] | tuple[str, ...] | None = None
    ) -> list[str]:
        """Paths of the rows a header sync owns (SYSTEM_DNT_OWNER). With
        ``paths`` the selection is done IN SQL (chunked), so a 1-file reindex
        reads at most its own rows, never the whole table."""
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            if paths is None:
                rows = conn.execute(
                    "SELECT path FROM protected_file_registry WHERE protected_by_user_id = ? ORDER BY path",
                    (SYSTEM_DNT_OWNER,),
                ).fetchall()
                return [str(r[0]) for r in rows]
            wanted = sorted({str(p).replace("\\", "/").lstrip("/") for p in paths})
            out: list[str] = []
            for i in range(0, len(wanted), 500):
                chunk = wanted[i : i + 500]
                qm = ",".join("?" * len(chunk))
                out.extend(
                    str(r[0])
                    for r in conn.execute(
                        f"SELECT path FROM protected_file_registry WHERE protected_by_user_id = ? AND path IN ({qm})",
                        (SYSTEM_DNT_OWNER, *chunk),
                    )
                )
        return sorted(out)

    def system_dnt_fingerprint(self, project_root: Path) -> str:
        """Exact change token of the registry: ``<instance>:<rev>`` from
        dnt_registry_revision, which SQLite triggers bump on every insert,
        update and delete of protected_file_registry by ANY writer (any row,
        operator rows included - over-invalidation only costs one extra
        verification). Equal tokens mean no row was mutated in between; a
        recreated DB has a new instance. If the revision row is missing the
        token is random, so it never matches a stored one (fail closed: the
        caller verifies in full)."""
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            row = conn.execute("SELECT instance, rev FROM dnt_registry_revision WHERE id = 1").fetchone()
        if row is None:
            return f"unrevisioned:{secrets.token_hex(16)}"
        return f"{row[0]}:{int(row[1])}"

    def get_full_many(self, project_root: Path, paths: list[str]) -> dict[str, ProtectionRecord]:
        """get_full for many paths over ONE connection (chunked IN)."""
        self.init_db(project_root)
        wanted = sorted({str(p).replace("\\", "/").lstrip("/") for p in paths})
        out: dict[str, ProtectionRecord] = {}
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            for i in range(0, len(wanted), 500):
                chunk = wanted[i : i + 500]
                qm = ",".join("?" * len(chunk))
                for row in conn.execute(f"SELECT * FROM protected_file_registry WHERE path IN ({qm})", tuple(chunk)):
                    rec = _row_to_full_record(row)
                    if rec is not None:
                        out[rec.path] = rec
        return out

    def get_full(
        self,
        project_root: Path,
        path: str,
    ) -> ProtectionRecord | None:
        """Like get() but populates structured DNT fields too."""
        self.init_db(project_root)
        rec_path = path.replace("\\", "/").lstrip("/")
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM protected_file_registry WHERE path = ?",
                (rec_path,),
            ).fetchone()
        return _row_to_full_record(row)

    def find_family_by_path(
        self,
        project_root: Path,
        path: str,
    ) -> tuple[str, str] | None:
        """Return (dnt_id, dnt_role) for a path, or None when the path
        isn't in any DNT family. Hot path — every read tool calls this
        to decide whether to surface a banner.
        """
        self.init_db(project_root)
        rec_path = path.replace("\\", "/").lstrip("/")
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            row = conn.execute(
                "SELECT dnt_id, dnt_role FROM protected_file_registry "
                "WHERE path = ? AND dnt_id != ''",
                (rec_path,),
            ).fetchone()
        if row is None:
            return None
        return (str(row[0]), str(row[1]))

    def get_family_master(
        self,
        project_root: Path,
        dnt_id: str,
    ) -> ProtectionRecord | None:
        """Return the master record for a DNT family — the one carrying
        the full_header_text payload. None if no master is registered.
        """
        if not dnt_id:
            return None
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM protected_file_registry "
                "WHERE dnt_id = ? AND dnt_role = 'master' LIMIT 1",
                (dnt_id,),
            ).fetchone()
        return _row_to_full_record(row)

    def list_family(
        self,
        project_root: Path,
        dnt_id: str,
    ) -> list[ProtectionRecord]:
        """All records (master + satellites) in a DNT family."""
        if not dnt_id:
            return []
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM protected_file_registry WHERE dnt_id = ? "
                "ORDER BY CASE dnt_role WHEN 'master' THEN 0 ELSE 1 END, "
                "path ASC",
                (dnt_id,),
            ).fetchall()
        return [r for r in (_row_to_full_record(row) for row in rows) if r is not None]

    # ── Banner-shown tracking ──

    def mark_banner_shown(
        self,
        project_root: Path,
        *,
        epoch_id: str,
        dnt_id: str,
    ) -> None:
        """Record that this conductor has seen the banner for this
        family in this conversation. INSERT OR IGNORE — duplicate
        marks are a no-op.
        """
        if not epoch_id or not dnt_id:
            return
        self.init_db(project_root)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO dnt_banners_shown "
                "(epoch_id, dnt_id, first_shown_at) "
                "VALUES (?, ?, ?)",
                (epoch_id, dnt_id, ts),
            )
            conn.commit()

    def was_banner_shown(
        self,
        project_root: Path,
        *,
        epoch_id: str,
        dnt_id: str,
    ) -> bool:
        """True when this conductor has already seen the banner for
        this family in this conversation. Suppresses re-banner.
        """
        if not epoch_id or not dnt_id:
            return False
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            row = conn.execute(
                "SELECT 1 FROM dnt_banners_shown WHERE epoch_id = ? AND dnt_id = ? LIMIT 1",
                (epoch_id, dnt_id),
            ).fetchone()
        return row is not None
    def list_banners_shown(
        self,
        project_root: Path,
        *,
        epoch_id: str,
        prefix: str = "",
    ) -> list[str]:
        """All dnt_id markers recorded for one epoch_id, optionally
        filtered to a marker-namespace prefix (e.g. ``scrollread:``).
        Additive read API for cross-epoch ledgers (doctrine resurface
        #316 keys its scroll-read ledger on the stable agent_context_id
        instead of the rotating epoch). Sorted for determinism.
        """
        if not epoch_id:
            return []
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            if prefix:
                rows = conn.execute(
                    "SELECT dnt_id FROM dnt_banners_shown "
                    "WHERE epoch_id = ? AND dnt_id LIKE ? ORDER BY dnt_id ASC",
                    (epoch_id, prefix + "%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT dnt_id FROM dnt_banners_shown "
                    "WHERE epoch_id = ? ORDER BY dnt_id ASC",
                    (epoch_id,),
                ).fetchall()
        return [str(r[0]) for r in rows]

    def clear_banners_shown_for_conductor(
        self,
        project_root: Path,
        epoch_id: str,
    ) -> int:
        """Clear all banner-shown rows for one epoch_id. Used
        when the conversation_key changes (operator /clear or new
        conversation per #58 contract). Returns count cleared.
        """
        if not epoch_id:
            return 0
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            cur = conn.execute(
                "DELETE FROM dnt_banners_shown WHERE epoch_id = ?",
                (epoch_id,),
            )
            conn.commit()
            return cur.rowcount or 0


def dnt_row_digest(
    *,
    dnt_id: str,
    dnt_role: str,
    pair_files: Any,
    forbid_list: Any,
    allow_list: Any,
    incidents: Any,
    baseline: str,
    cost: str,
    full_header_text: str,
) -> str:
    """Content digest of a system DNT row (every field a header determines).
    Equal for a row and the header it was written from; any rewrite of the
    row - equal length or not - changes it."""
    import hashlib

    payload = json.dumps(
        [
            str(dnt_id or ""),
            str(dnt_role or ""),
            list(pair_files or []),
            list(forbid_list or []),
            list(allow_list or []),
            list(incidents or []),
            str(baseline or ""),
            str(cost or ""),
            str(full_header_text or ""),
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_digest(rec: ProtectionRecord | None) -> str:
    """dnt_row_digest of a stored SYSTEM row; '' when there is none."""
    if rec is None or rec.protected_by_user_id != SYSTEM_DNT_OWNER or not rec.dnt_id:
        return ""
    return dnt_row_digest(
        dnt_id=rec.dnt_id,
        dnt_role=rec.dnt_role,
        pair_files=rec.pair_files,
        forbid_list=rec.forbid_list,
        allow_list=rec.allow_list,
        incidents=rec.incidents,
        baseline=rec.baseline,
        cost=rec.cost,
        full_header_text=rec.full_header_text,
    )


def _row_to_full_record(row) -> ProtectionRecord | None:
    """Convert a sqlite Row with all columns into a fully-populated
    ProtectionRecord. Returns None when row is None.
    """
    if row is None:
        return None

    def _safe_list(raw: str) -> list[str]:
        try:
            parsed = json.loads(raw or "[]")
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    keys = row.keys() if hasattr(row, "keys") else []

    def _col(name: str, default: str = "") -> str:
        return str(row[name]) if name in keys and row[name] is not None else default

    return ProtectionRecord(
        path=_col("path"),
        protected_by_user_id=_col("protected_by_user_id"),
        protected_at=_col("protected_at"),
        why=_col("why"),
        pair_files=_safe_list(_col("pair_files", "[]")),
        machine_id=_col("machine_id"),
        dnt_id=_col("dnt_id"),
        dnt_role=_col("dnt_role"),
        forbid_list=tuple(_safe_list(_col("forbid_list", "[]"))),
        allow_list=tuple(_safe_list(_col("allow_list", "[]"))),
        incidents=tuple(_safe_list(_col("incidents", "[]"))),
        baseline=_col("baseline"),
        cost=_col("cost"),
        full_header_text=_col("full_header_text"),
    )
