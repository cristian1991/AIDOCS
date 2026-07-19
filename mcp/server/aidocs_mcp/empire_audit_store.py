"""Empire audit ledger — the multi-kingdom archive of execution events.

Backlog #140 (Empire directive: "empire is the canonical home for ledgers";
empire-doctrine §XI: the kingdom carries WHAT IT IS, the empire carries
WHAT HAS HAPPENED).

Design:
- The KINGDOM store (``ExecutionIndexStore`` → ``<project>/.MEMORY/.index/
  aidocs.sqlite3`` table ``execution_events``) remains the fail-closed
  authority. Every audit event still lands there first.
- The EMPIRE store (``~/.aidocs/empire.sqlite3``, env override
  ``AIDOCS_EMPIRE_DB``) mirrors each committed kingdom row into
  ``empire_audit_events`` — the same columns PLUS ``project_root`` and
  ``project_id``, because a multi-kingdom ledger must know which kingdom
  each row belongs to.
- The mirror is BEST-EFFORT BY DESIGN: an empire-write failure must never
  fail or slow the kingdom audit. Failures are swallowed (with a one-line
  stderr note).

REPORT-FIRST DEBT (v1): the kingdom's Merkle-chain fields (``prev_hash``,
``chain_seq``, ``in_hash``, ``out_hash``, ``hash_version``) are copied
VERBATIM as received data. The empire does NOT recompute its own chain
over its own row order, so the empire ledger is tamper-evident only
per-kingdom-session via the copied hashes, not as a unified empire chain.
An empire-side chain is deferred to a follow-up.

Audit v3 (#440, 2026-07-18): the kingdom's row hash now also folds
``session_id``, ``capability_name``, ``action_kind``, ``target_entity``,
``status``, ``effective_role``, ``scope_type``, ``scope_id``,
``permission_name`` and ``principal_type`` (``hash_version='v3'``). No
change is needed here beyond this note: every one of those columns is
already in ``_EVENT_COLUMNS``/the mirror schema, so v3 rows ride along
verbatim exactly like v1/v2 — the empire still performs no recompute.

Audit v4 (#441, 2026-07-18): the kingdom's row hash additionally folds the
causal ``turn_id`` (``hash_version='v4'``). The column is added to
``_EVENT_COLUMNS``/both DDLs (+ an additive ALTER for pre-existing empire
DBs) so v4 rows ride along verbatim exactly like v1/v2/v3 — the empire
still performs no recompute.

Audit v5 (#467, 2026-07-18): the kingdom's row hash additionally folds the
causal instruction binding — ``instruction_id``, ``instruction_revision``
and ``causal_edge`` — in ONE bump (the #467 hash law). All three columns
are added to ``_EVENT_COLUMNS``/both DDLs (+ additive ALTERs for
pre-existing empire/archive DBs) so v5 rows ride along verbatim exactly
like v1..v4 — the empire still performs no recompute.

Also v1: rows written through ``record_event_on_connection`` by atomic
callers that own their own transaction (e.g. ConfigStore config-write +
audit in one commit) are only mirrored when they arrive via
``record_event`` — mirroring inside the caller's open transaction could
archive a row that later rolls back, which would make the archive lie.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Module-level enable flag (default ON). Tests / operators can flip it,
# or set AIDOCS_EMPIRE_AUDIT_MIRROR=0 in the environment.
MIRROR_ENABLED = True

# Per-process, per-path "schema ensured" guard (same pattern as
# execution_index_store). Tests that swap AIDOCS_EMPIRE_DB clear it.
from .schema_memo import SchemaMemo

_SCHEMA_ENSURED = SchemaMemo()  # identity-validated (2026-07-09, schema_memo.py)

# Column order shared by the mirror write and the copy-back read. Matches
# execution_events (kingdom) exactly; empire adds project_root/project_id.
_EVENT_COLUMNS = (
    "event_id",
    "run_id",
    "event_kind",
    "source_kind",
    "session_id",
    "procedure_id",
    "capability_name",
    "action_kind",
    "target_entity",
    "status",
    "payload_json",
    "observed_at",
    "task_id",
    "prev_hash",
    "chain_seq",
    "user_id",
    "effective_role",
    "scope_type",
    "scope_id",
    "permission_name",
    "principal_type",
    "in_hash",
    "out_hash",
    "result_json",
    "hash_version",
    "agent_epoch",
    "turn_id",
    "instruction_id",
    "instruction_revision",
    "causal_edge",
)


def empire_db_path() -> Path:
    """Empire sqlite path. Honors AIDOCS_EMPIRE_DB so tests never touch the
    operator's real ``~/.aidocs/empire.sqlite3``."""
    override = os.environ.get("AIDOCS_EMPIRE_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "empire.sqlite3"


def empire_audit_archive_db_path() -> Path:
    """Cold-archive sqlite for rotated ``empire_audit_events`` rows (#384).

    Sits NEXT TO the hot empire DB (``empire_audit_archive.sqlite3`` by
    default) so the hot-path file stays small while nothing is ever
    deleted-without-archive. Honors AIDOCS_EMPIRE_AUDIT_ARCHIVE_DB so tests
    never touch the operator's real archive.
    """
    override = os.environ.get("AIDOCS_EMPIRE_AUDIT_ARCHIVE_DB", "").strip()
    if override:
        return Path(override)
    return empire_db_path().with_name("empire_audit_archive.sqlite3")


# Default age (days) after which audit rows become ELIGIBLE for rotation to
# the archive DB. Overridable per-call and via
# AIDOCS_EMPIRE_AUDIT_RETENTION_DAYS; a value <= 0 disables rotation entirely
# (fail-safe: keep everything hot rather than prune on a bad config).
RETENTION_DAYS_DEFAULT = 90


def _retention_days() -> int:
    raw = os.environ.get("AIDOCS_EMPIRE_AUDIT_RETENTION_DAYS", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return RETENTION_DAYS_DEFAULT
    return RETENTION_DAYS_DEFAULT


def mirror_enabled() -> bool:
    if not MIRROR_ENABLED:
        return False
    return os.environ.get("AIDOCS_EMPIRE_AUDIT_MIRROR", "").strip() not in {"0", "false", "off"}


def project_id_for_root(project_root: Path) -> str:
    """Deterministic kingdom id: sha256 of the resolved project root."""
    import hashlib

    raw = str(Path(project_root).resolve())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# One DDL, two homes: the hot empire DB and the #384 cold archive share the
# exact same table shape so rotated rows move VERBATIM (every Merkle-chain
# column included) and chain verification reads either file identically.
_AUDIT_EVENTS_DDL = """
                CREATE TABLE IF NOT EXISTS empire_audit_events (
                    project_root TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    run_id TEXT,
                    event_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    session_id TEXT,
                    procedure_id TEXT,
                    capability_name TEXT,
                    action_kind TEXT,
                    target_entity TEXT,
                    status TEXT,
                    payload_json TEXT,
                    observed_at TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    -- Kingdom Merkle-chain fields, copied VERBATIM (see
                    -- module docstring: no empire-side recompute in v1).
                    prev_hash TEXT NOT NULL DEFAULT '',
                    chain_seq INTEGER NOT NULL DEFAULT 0,
                    user_id TEXT NOT NULL DEFAULT 'operator',
                    effective_role TEXT NOT NULL DEFAULT 'super_admin',
                    scope_type TEXT NOT NULL DEFAULT 'global',
                    scope_id TEXT,
                    permission_name TEXT,
                    principal_type TEXT NOT NULL DEFAULT 'human',
                    in_hash TEXT NOT NULL DEFAULT '',
                    out_hash TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
                    hash_version TEXT NOT NULL DEFAULT 'v1',
                    agent_epoch TEXT NOT NULL DEFAULT '',
                    turn_id TEXT NOT NULL DEFAULT '',
                    instruction_id TEXT NOT NULL DEFAULT '',
                    instruction_revision INTEGER NOT NULL DEFAULT 0,
                    causal_edge TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (project_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_empire_audit_project_observed
                    ON empire_audit_events(project_id, observed_at);
                -- #384: rotation bookkeeping (last_rotated_at day stamp).
                -- Harmless no-op table in the archive file.
                CREATE TABLE IF NOT EXISTS empire_audit_maintenance (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
                """


class EmpireAuditStore:
    """Multi-kingdom audit archive in the empire DB."""

    def db_path(self) -> Path:
        return empire_db_path()

    def archive_db_path(self) -> Path:
        return empire_audit_archive_db_path()

    def connect(self) -> sqlite3.Connection:
        db = self.db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn

    def connect_archive(self) -> sqlite3.Connection:
        db = self.archive_db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
        """Additive ALTERs for pre-existing empire/archive DBs whose table
        predates a column in _AUDIT_EVENTS_DDL (CREATE IF NOT EXISTS never
        upgrades an existing table). Benign 'duplicate column' swallowed —
        same try/except-existing pattern as the kingdom store."""
        for column_def in (
            # #441 audit v4: causal turn id, mirrored verbatim.
            "ALTER TABLE empire_audit_events ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''",
            # #467 audit v5: causal instruction binding, mirrored verbatim
            # (one bump, all three columns together — the #467 hash law).
            (
                "ALTER TABLE empire_audit_events ADD COLUMN instruction_id "
                "TEXT NOT NULL DEFAULT ''"
            ),
            (
                "ALTER TABLE empire_audit_events ADD COLUMN instruction_revision "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            (
                "ALTER TABLE empire_audit_events ADD COLUMN causal_edge "
                "TEXT NOT NULL DEFAULT ''"
            ),
        ):
            try:
                conn.execute(column_def)
            except Exception:
                pass

    def init_db(self) -> None:
        db_path = self.db_path()
        if _SCHEMA_ENSURED.is_current(db_path):
            return
        with self.connect() as conn:
            conn.executescript(_AUDIT_EVENTS_DDL)
            self._apply_additive_migrations(conn)
        _SCHEMA_ENSURED.mark(db_path)

    def init_archive_db(self) -> None:
        db_path = self.archive_db_path()
        if _SCHEMA_ENSURED.is_current(db_path):
            return
        with self.connect_archive() as conn:
            conn.executescript(_AUDIT_EVENTS_DDL)
            self._apply_additive_migrations(conn)
        _SCHEMA_ENSURED.mark(db_path)

    # ------------------------------------------------------------------
    # Write path (mirror) — best-effort by design.
    # ------------------------------------------------------------------
    def record_event_mirror(self, project_root: Path, event_row: dict[str, Any]) -> bool:
        """Mirror one committed kingdom execution_events row into the empire.

        BEST-EFFORT: never raises, never slows the kingdom path beyond one
        local sqlite insert. Returns True on success, False on any failure
        (a stderr note is emitted so silent archive gaps stay observable).
        """
        try:
            if not mirror_enabled():
                return False
            self.init_db()
            resolved_root = str(Path(project_root).resolve())
            values = [resolved_root, project_id_for_root(project_root)]
            values.extend(event_row.get(col) for col in _EVENT_COLUMNS)
            placeholders = ", ".join("?" for _ in values)
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO empire_audit_events "
                    f"(project_root, project_id, {', '.join(_EVENT_COLUMNS)}) "
                    f"VALUES ({placeholders}) "
                    "ON CONFLICT(project_id, event_id) DO NOTHING",
                    values,
                )
            # #384: opportunistic retention — at most one attempt per UTC
            # day, best-effort, never fails or slows the mirror beyond the
            # one stamp read (the rotate itself runs on a daemon thread).
            self.maybe_auto_rotate(background=True)
            return True
        except Exception as exc:
            try:
                sys.stderr.write(
                    f"[aidocs empire_audit] mirror skipped (kingdom unaffected): "
                    f"{type(exc).__name__}: {exc}\n",
                )
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # Operator quill: restore empire rows back into a kingdom DB.
    # ------------------------------------------------------------------
    def ledger_copy_to_kingdom(
        self,
        project_root: Path,
        since_iso: str | None = None,
        until_iso: str | None = None,
    ) -> dict[str, int]:
        """Materialize this kingdom's empire rows into the kingdom's own
        ``execution_events`` (e.g. after a lost/rebuilt ``.MEMORY/.index``).

        Rows are matched on project_id (derived from ``project_root``);
        optional ``since_iso``/``until_iso`` bound ``observed_at``
        (inclusive). Idempotent: rows whose event_id already exists in the
        kingdom are skipped. All columns — including the kingdom's own
        Merkle fields — are preserved verbatim.

        #384: reads the hot DB AND the cold archive, so a restore after
        rotation is as complete as one before it.

        Returns ``{"copied": n, "skipped": m}``.
        """
        from .execution_index_store import ExecutionIndexStore

        self.init_db()
        self.init_archive_db()
        pid = project_id_for_root(project_root)
        sql = (
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM empire_audit_events "
            "WHERE project_id = ?"
        )
        params: list[Any] = [pid]
        if since_iso and since_iso.strip():
            sql += " AND observed_at >= ?"
            params.append(since_iso.strip())
        if until_iso and until_iso.strip():
            sql += " AND observed_at <= ?"
            params.append(until_iso.strip())
        merged: dict[str, Any] = {}
        for opener in (self.connect_archive, self.connect):
            with opener() as conn:
                for row in conn.execute(sql, params).fetchall():
                    merged[str(row["event_id"])] = row
        rows = sorted(
            merged.values(),
            key=lambda r: (
                str(r["session_id"] or ""),
                int(r["chain_seq"] or 0),
                str(r["observed_at"] or ""),
            ),
        )

        kingdom = ExecutionIndexStore()
        kingdom.init_db(project_root)
        copied = 0
        skipped = 0
        insert_sql = (
            f"INSERT INTO execution_events ({', '.join(_EVENT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)}) "
            "ON CONFLICT(event_id) DO NOTHING"
        )
        with kingdom.connect(project_root) as kconn:
            for row in rows:
                exists = kconn.execute(
                    "SELECT 1 FROM execution_events WHERE event_id = ? LIMIT 1",
                    (row["event_id"],),
                ).fetchone()
                if exists is not None:
                    skipped += 1
                    continue
                kconn.execute(insert_sql, tuple(row[col] for col in _EVENT_COLUMNS))
                copied += 1
        return {"copied": copied, "skipped": skipped}

    # ------------------------------------------------------------------
    # #384 retention: rotate old rows into the cold archive, chain-safe.
    # ------------------------------------------------------------------
    _ALL_COLUMNS = ("project_root", "project_id", *_EVENT_COLUMNS)

    @staticmethod
    def _cutoff_iso(retention_days: int, now: Any = None) -> str:
        from datetime import UTC, datetime, timedelta

        base = now if now is not None else datetime.now(UTC)
        return (
            (base - timedelta(days=retention_days))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _archive_rows_verbatim(self, aconn: sqlite3.Connection, rows: list) -> None:
        """Copy rows into the archive byte-for-byte (all columns, Merkle
        fields included). Idempotent on the (project_id, event_id) PK."""
        cols = ", ".join(self._ALL_COLUMNS)
        placeholders = ", ".join("?" for _ in self._ALL_COLUMNS)
        aconn.executemany(
            f"INSERT INTO empire_audit_events ({cols}) VALUES ({placeholders}) "
            "ON CONFLICT(project_id, event_id) DO NOTHING",
            [tuple(row[c] for c in self._ALL_COLUMNS) for row in rows],
        )

    @staticmethod
    def _ids_present(
        aconn: sqlite3.Connection, project_id: str, event_ids: list[str]
    ) -> bool:
        """True iff EVERY event_id is present in the archive for this
        kingdom — the never-prune-unarchived floor."""
        for i in range(0, len(event_ids), 400):
            chunk = event_ids[i : i + 400]
            marks = ", ".join("?" for _ in chunk)
            got = aconn.execute(
                "SELECT COUNT(*) FROM empire_audit_events "
                f"WHERE project_id = ? AND event_id IN ({marks})",
                [project_id, *chunk],
            ).fetchone()[0]
            if int(got) != len(chunk):
                return False
        return True

    def rotate_audit_events(
        self,
        retention_days: int | None = None,
        *,
        now: Any = None,
        vacuum: bool = True,
    ) -> dict[str, Any]:
        """Rotate old ``empire_audit_events`` rows into the archive DB (#384).

        THE CHAIN WINS over naive age pruning — two structural guarantees:

        1. SESSION GRANULARITY. Rows that carry a Merkle chain (non-empty
           ``session_id``) rotate only as a WHOLE (project_id, session_id)
           group, and only when the group's NEWEST row is older than the
           cutoff. A session's chain is therefore never split across the
           hot and archive files. Chainless rows (``session_id`` empty →
           ``chain_seq=0``/``prev_hash=''`` by construction, see
           ``ExecutionIndexStore.record_event_on_connection``) rotate
           row-wise by age.
        2. ARCHIVE-BEFORE-PRUNE. Every row is copied VERBATIM (all columns,
           ``prev_hash``/``in_hash``/``out_hash``/``hash_version`` included)
           and its presence in the archive re-read BEFORE the hot delete;
           a group whose copy cannot be verified is left hot untouched.

        After pruning, each rotated session's chain is re-verified through
        :meth:`verify_session_chain` (which reads hot+archive union) and the
        result is tallied — a pre-existing mirror gap (the mirror is
        best-effort by design) is REPORTED, never repaired and never a
        reason to keep dead weight hot, because rotation moves rows
        verbatim and cannot itself break an intact chain.

        ``retention_days``: default from AIDOCS_EMPIRE_AUDIT_RETENTION_DAYS
        or ``RETENTION_DAYS_DEFAULT``; <= 0 disables (nothing moves).
        ``vacuum``: reclaim hot-file space after a prune (the point of #384
        — 74MB of audit log off the hot path).
        """
        days = retention_days if retention_days is not None else _retention_days()
        stats: dict[str, Any] = {
            "ok": True,
            "retention_days": days,
            "sessions_rotated": 0,
            "rows_archived": 0,
            "chainless_archived": 0,
            "rows_pruned": 0,
            "chains_verified": 0,
            "chains_broken": 0,
            "broken_sessions": [],
            "vacuumed": False,
        }
        if days <= 0:
            stats["disabled"] = True
            return stats
        cutoff = self._cutoff_iso(days, now)
        stats["cutoff"] = cutoff
        # Rotation NEVER creates databases: a missing hot DB means nothing
        # to rotate (and, under the background thread, that the env-resolved
        # path moved after the claim — the TOCTOU that materialized empty
        # DBs in foreign tmp dirs when tests swapped AIDOCS_EMPIRE_DB
        # mid-rotate). Guard before any connect(): sqlite would create the
        # file on first touch.
        if not Path(self.db_path()).exists():
            stats["skipped_no_db"] = True
            return stats
        self.init_db()
        self.init_archive_db()
        cols = ", ".join(self._ALL_COLUMNS)
        rotated_sessions: list[tuple[str, str]] = []
        with self.connect() as conn, self.connect_archive() as aconn:
            groups = conn.execute(
                "SELECT project_id, session_id FROM empire_audit_events "
                "WHERE session_id IS NOT NULL AND session_id != '' "
                "GROUP BY project_id, session_id "
                "HAVING MAX(observed_at) < ?",
                (cutoff,),
            ).fetchall()
            for g in groups:
                pid, sid = str(g["project_id"]), str(g["session_id"])
                rows = conn.execute(
                    f"SELECT {cols} FROM empire_audit_events "
                    "WHERE project_id = ? AND session_id = ? "
                    "ORDER BY chain_seq ASC, observed_at ASC",
                    (pid, sid),
                ).fetchall()
                if not rows:
                    continue
                self._archive_rows_verbatim(aconn, rows)
                aconn.commit()
                ids = [str(r["event_id"]) for r in rows]
                if not self._ids_present(aconn, pid, ids):
                    continue  # never prune what the archive can't prove
                for i in range(0, len(ids), 400):
                    chunk = ids[i : i + 400]
                    marks = ", ".join("?" for _ in chunk)
                    conn.execute(
                        "DELETE FROM empire_audit_events "
                        f"WHERE project_id = ? AND event_id IN ({marks})",
                        [pid, *chunk],
                    )
                stats["sessions_rotated"] += 1
                stats["rows_archived"] += len(rows)
                stats["rows_pruned"] += len(rows)
                rotated_sessions.append((pid, sid))
            # Chainless rows (no Merkle chain by construction): age-prune
            # row-wise, same archive-before-prune floor.
            loose = conn.execute(
                f"SELECT {cols} FROM empire_audit_events "
                "WHERE (session_id IS NULL OR session_id = '') "
                "AND observed_at < ?",
                (cutoff,),
            ).fetchall()
            if loose:
                self._archive_rows_verbatim(aconn, loose)
                aconn.commit()
                by_pid: dict[str, list[str]] = {}
                for r in loose:
                    by_pid.setdefault(str(r["project_id"]), []).append(
                        str(r["event_id"])
                    )
                for pid, ids in by_pid.items():
                    if not self._ids_present(aconn, pid, ids):
                        continue
                    for i in range(0, len(ids), 400):
                        chunk = ids[i : i + 400]
                        marks = ", ".join("?" for _ in chunk)
                        conn.execute(
                            "DELETE FROM empire_audit_events "
                            f"WHERE project_id = ? AND event_id IN ({marks})",
                            [pid, *chunk],
                        )
                    stats["chainless_archived"] += len(ids)
                    stats["rows_pruned"] += len(ids)
            conn.commit()
        # Chain-verify-after-rotation proof: every rotated session must still
        # walk end-to-end through the union view.
        for pid, sid in rotated_sessions:
            verdict = self.verify_session_chain(pid, sid)
            if verdict.get("verified"):
                stats["chains_verified"] += 1
            else:
                stats["chains_broken"] += 1
                stats["broken_sessions"].append(
                    {"project_id": pid, "session_id": sid, **verdict}
                )
        if vacuum and stats["rows_pruned"]:
            try:
                with self.connect() as conn:
                    conn.execute("VACUUM")
                stats["vacuumed"] = True
            except Exception:
                stats["vacuumed"] = False
        return stats

    def verify_session_chain(self, project_id: str, session_id: str) -> dict[str, Any]:
        """Walk one kingdom-session's Merkle chain across the hot + archive
        UNION (#384) — same semantics as the kingdom's
        ``ExecutionIndexStore.verify_audit_chain``, same per-row
        ``hash_version`` formula selection (delegated to
        ``_row_hash_from_stored_row`` so the v1/v2/v3 boundaries behave
        identically). Rotation moves rows verbatim, so an intact chain
        verifies identically before and after rotation; a broken verdict
        means the rows were tampered with or the best-effort mirror missed
        rows — never that rotation split a chain (it structurally cannot).
        """
        from .execution_index_store import ExecutionIndexStore

        self.init_db()
        self.init_archive_db()
        cols = ", ".join(self._ALL_COLUMNS)
        merged: dict[str, dict[str, Any]] = {}
        for opener in (self.connect_archive, self.connect):
            with opener() as conn:
                rows = conn.execute(
                    f"SELECT {cols} FROM empire_audit_events "
                    "WHERE project_id = ? AND session_id = ?",
                    (project_id, session_id),
                ).fetchall()
            for r in rows:
                merged[str(r["event_id"])] = dict(r)
        ordered = sorted(
            merged.values(),
            key=lambda r: (int(r.get("chain_seq") or 0), str(r.get("observed_at") or "")),
        )
        total = len(ordered)
        if total == 0:
            return {"verified": True, "total": 0, "broken_at": None}
        expected_prev = ""
        for idx, row in enumerate(ordered):
            if str(row.get("prev_hash") or "") != expected_prev:
                return {
                    "verified": False,
                    "total": total,
                    "broken_at": idx,
                    "broken_event_id": row.get("event_id"),
                    "broken_chain_seq": int(row.get("chain_seq") or 0),
                    "expected_prev_hash": expected_prev,
                    "stored_prev_hash": str(row.get("prev_hash") or ""),
                }
            expected_prev = ExecutionIndexStore._row_hash_from_stored_row(row)
        return {"verified": True, "total": total, "broken_at": None}

    # ------------------------------------------------------------------
    # Opportunistic trigger — at most one rotation attempt per UTC day.
    # ------------------------------------------------------------------
    def _claim_rotation_stamp(self) -> bool:
        """Advance the ``last_rotated_at`` day stamp (hot DB) iff it is not
        already today. One cheap SELECT on non-rotation days; the stamp
        advances even if the rotation later fails, so a poisoned DB cannot
        turn every audit write into a rotation attempt. Never raises."""
        try:
            from datetime import UTC, datetime

            today = datetime.now(UTC).strftime("%Y-%m-%d")
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT value FROM empire_audit_maintenance "
                    "WHERE key = 'last_rotated_at'",
                ).fetchone()
                if row is not None and str(row["value"] or "")[:10] == today:
                    return False
                conn.execute(
                    "INSERT INTO empire_audit_maintenance (key, value) "
                    "VALUES ('last_rotated_at', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (today,),
                )
            return True
        except Exception:
            return False

    def maybe_auto_rotate(self, *, background: bool = False) -> bool:
        """Day-stamped auto-rotation hook for the mirror write path: at most
        one rotation attempt per UTC day. ``background=True`` (the mirror's
        setting) runs the heavy rotate in a daemon thread so the mirror
        never stalls on rotation day — the inline cost stays one stamp
        read. BEST-EFFORT: never raises. Returns True when a rotation was
        started (or completed, when synchronous)."""
        if not self._claim_rotation_stamp():
            return False
        if background:
            try:
                import threading

                claimed_db = str(self.db_path())

                def _rotate_pinned() -> None:
                    # Path pin: if the env-resolved empire DB changed between
                    # claim and run (tests swap AIDOCS_EMPIRE_DB), abort —
                    # never rotate a DB the stamp wasn't claimed on.
                    try:
                        if str(self.db_path()) != claimed_db:
                            return
                        self.rotate_audit_events()
                    except Exception:
                        pass

                threading.Thread(
                    target=_rotate_pinned,
                    name="empire-audit-rotate",
                    daemon=True,
                ).start()
                return True
            except Exception:
                return False
        try:
            self.rotate_audit_events()
            return True
        except Exception:
            return False
