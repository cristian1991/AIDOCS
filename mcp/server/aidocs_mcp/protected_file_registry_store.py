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
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


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
        with sqlite3.connect(str(path)) as conn:
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
                "ALTER TABLE protected_file_registry ADD COLUMN "
                "forbid_list TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE protected_file_registry ADD COLUMN "
                "allow_list TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE protected_file_registry ADD COLUMN "
                "incidents TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE protected_file_registry ADD COLUMN baseline TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE protected_file_registry ADD COLUMN cost TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE protected_file_registry ADD COLUMN "
                "full_header_text TEXT NOT NULL DEFAULT ''",
                # #205: symbol-scope column (additive, guarded — same
                # pattern as the #62/#104 migrations). '' = whole file.
                "ALTER TABLE protected_file_registry ADD COLUMN "
                "symbol TEXT NOT NULL DEFAULT ''",
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
    ) -> None:
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
        """
        self.init_db(project_root)
        rec_path = path.replace("\\", "/").lstrip("/")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO protected_file_registry (
                    path, protected_by_user_id, protected_at,
                    why, pair_files, machine_id,
                    dnt_id, dnt_role, forbid_list, allow_list,
                    incidents, baseline, cost,
                    full_header_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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
                ),
            )
            conn.commit()

    def get_full(
        self,
        project_root: Path,
        path: str,
    ) -> ProtectionRecord | None:
        """Like get() but populates structured DNT fields too."""
        self.init_db(project_root)
        rec_path = path.replace("\\", "/").lstrip("/")
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
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
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "DELETE FROM dnt_banners_shown WHERE epoch_id = ?",
                (epoch_id,),
            )
            conn.commit()
            return cur.rowcount or 0


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
