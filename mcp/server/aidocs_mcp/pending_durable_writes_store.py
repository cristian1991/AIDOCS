"""pending_durable_writes — the update-intent durability ledger (#219/#221 PR-1).

When the UPS detector recognizes an operator UPDATE (plan/spec/priority/
decision change), a PENDING row is created here. A successful durable-write
tool call (ai_backlog / ai_task todo / ai_plan* / memory_capture) SATISFIES the
session's fresh pending rows; rows nobody satisfied EXPIRE loudly at the
end of the NEXT turn (Empire §9.3). The ledger is also the audit trail of
operator decisions (dashboard surface later).

Storage: pending_durable_writes (+ pending_dw_turns counter) in
aidocs.sqlite3, same home as the other session stores. Tombstone model:
rows transition status, never DELETE.

Statuses: pending → satisfied | expired | confirm_declined | false_positive.

``false_positive`` is the audited cleanup mark (update_intent_cleanup): the row
stays, ``satisfied_by`` carries ``false_positive:<reason>``. Stores created
before it existed are rebuilt in place by ``_ensure_false_positive_status``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

# #755/#756: the ONE canonical connect. All six sites were
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none set a
# single pragma (foreign_keys OFF, no busy_timeout).
# DURABILITY: AUDIT, i.e. the FULL these sites already had. A pending row
# is the ONLY record that an update was requested -- nothing
# re-derives it, which is the same call backlog_write_queue took -- and
# the module's whole reason to exist is that such an intent expires
# LOUDLY rather than silently. The satisfied/declined marks are the
# closing half of that record.
from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

_log = logging.getLogger(__name__)

# End-of-next-turn expiry (Empire §9.3): a row created on UPS turn N is still
# actionable through turn N+1 and expires when turn N+2 begins.
_EXPIRY_TURNS = 2


def _db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_TABLE_COLUMNS = (
    "id, session_id, ups_seq, detected_at, snippet, verbs_json, objects_json, "
    "suggested_target, confidence, ambiguous, status, satisfied_by, resolved_at"
)

_TABLE_DDL = """
            CREATE TABLE pending_durable_writes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ups_seq INTEGER NOT NULL,
                detected_at TEXT NOT NULL,
                snippet TEXT NOT NULL,
                verbs_json TEXT NOT NULL DEFAULT '[]',
                objects_json TEXT NOT NULL DEFAULT '[]',
                suggested_target TEXT NOT NULL DEFAULT 'backlog',
                confidence REAL NOT NULL DEFAULT 0.0,
                ambiguous INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending','satisfied','expired','confirm_declined',
                        'false_positive'
                    )),
                satisfied_by TEXT,
                resolved_at TEXT
            );
"""


_TABLE = "pending_durable_writes"
_COLUMN_LIST = tuple(c.strip() for c in _TABLE_COLUMNS.split(","))


class PendingMigrationRefused(RuntimeError):
    """The stranded ``*_old`` table holds rows the live table does not; the
    migration refused to drop it (nothing was changed)."""

_OLD_TABLE = "pending_durable_writes_old"
_CREATE_IF_ABSENT_DDL = _TABLE_DDL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)


def _table_sql(conn: Any, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return None if row is None else str(row[0])


_STAGE = "pdw_stage"


def _ensure_false_positive_status(conn: Any) -> None:
    """Make the table's CHECK admit 'false_positive' (SQLite cannot alter a
    CHECK, so the table is rebuilt). ONE transaction, atomic and re-entrant.

    * The whole rebuild runs under ``BEGIN IMMEDIATE`` with plain ``execute()``
      statements (``executescript`` would COMMIT first and run each statement
      on its own). SQLite DDL is transactional: any failure -> ROLLBACK, the
      untouched old table is what remains.
    * NO ``ALTER TABLE ... RENAME``: a rename re-parses the whole schema and
      fails on ANY unrelated broken view (the live DB has ``canonical_rows``
      pointing at a missing table - found by the copied-DB rehearsal). Rows
      go through a TEMP stage table instead: stage -> drop -> create -> copy
      back -> drop stage. AUTOINCREMENT's high-water mark is preserved.
    * A stranded ``pending_durable_writes_old`` (a crash from a
      non-transactional predecessor) is recovered under the same lock by
      merging its rows into the live table (created if missing) with INSERT OR
      IGNORE, then dropping it. Rows and ids are never lost.
    * State is re-read after the lock is won, so a concurrent migrator that
      finished first makes this a no-op.
    * A lock wait longer than busy_timeout raises ``sqlite3.OperationalError``
      with nothing changed; the caller may defer and retry on the next init.
    """
    live = _table_sql(conn, _TABLE)
    old = _table_sql(conn, _OLD_TABLE)
    if old is None and (live is None or "false_positive" in live):
        return  # fast path: nothing to do, no write lock taken
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = _table_sql(conn, _TABLE)
        old = _table_sql(conn, _OLD_TABLE)
        if old is not None:
            if live is None:
                conn.execute(_CREATE_IF_ABSENT_DDL)
            conn.execute(
                f"INSERT OR IGNORE INTO {_TABLE} ({_TABLE_COLUMNS}) "
                f"SELECT {_TABLE_COLUMNS} FROM {_OLD_TABLE}"
            )
            # INSERT OR IGNORE silently skips an id collision. NEVER drop the
            # stranded table while it holds a row that is not present in the
            # live table by id AND full content: refuse (rolls back the merge
            # too, so both tables stay exactly as found) for manual review.
            same = " AND ".join(f"l.{c} IS o.{c}" for c in _COLUMN_LIST)
            lost = conn.execute(
                f"SELECT o.id FROM {_OLD_TABLE} o WHERE NOT EXISTS "
                f"(SELECT 1 FROM {_TABLE} l WHERE {same}) ORDER BY o.id LIMIT 20"
            ).fetchall()
            if lost:
                raise PendingMigrationRefused(
                    f"{_OLD_TABLE} holds rows absent (by id and content) from "
                    f"{_TABLE}: ids {[r[0] for r in lost]}; kept for manual review"
                )
            conn.execute(f"DROP TABLE {_OLD_TABLE}")
            live = _table_sql(conn, _TABLE)
        if live is not None and "false_positive" not in live:
            seq_row = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?", (_TABLE,)
            ).fetchone()
            conn.execute(
                f"CREATE TEMP TABLE {_STAGE} AS SELECT {_TABLE_COLUMNS} FROM {_TABLE}"
            )
            conn.execute("DROP INDEX IF EXISTS idx_pdw_session")
            conn.execute(f"DROP TABLE {_TABLE}")
            conn.execute(_TABLE_DDL)
            conn.execute(
                f"INSERT INTO {_TABLE} ({_TABLE_COLUMNS}) "
                f"SELECT {_TABLE_COLUMNS} FROM temp.{_STAGE}"
            )
            conn.execute(f"DROP TABLE temp.{_STAGE}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pdw_session "
                f"ON {_TABLE}(session_id, status)"
            )
            if seq_row is not None:
                conn.execute(
                    "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
                    (int(seq_row[0]), _TABLE),
                )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with _canonical_connect(
        str(db), durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        try:
            _ensure_false_positive_status(conn)
        except PendingMigrationRefused as exc:
            # Loud, but never break the hook: both tables are untouched and the
            # live table stays usable; retried (and re-logged) on the next init.
            _log.error("pending_durable_writes migration refused: %s", exc)
        except sqlite3.OperationalError as exc:
            # Lock contention ONLY: defer (table untouched and still fully
            # usable for every pre-existing flow); the next init retries.
            # Any other failure is a real fault and must surface.
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
        conn.execute(_CREATE_IF_ABSENT_DDL)
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_pdw_session
                ON pending_durable_writes(session_id, status);
            CREATE TABLE IF NOT EXISTS pending_dw_turns (
                session_id TEXT PRIMARY KEY,
                ups_seq INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()


def begin_turn(project_root: Path, session_id: str) -> dict[str, Any]:
    """Advance the session's UPS turn counter and expire stale pending rows.

    Returns {"ups_seq": int, "expired": [rows]} — the CALLER emits the loud
    ``durable_write_expired`` event per expired row (the store stays pure
    sqlite; event emission belongs to the hook layer).
    """
    init_db(project_root)
    with _canonical_connect(
        str(_db_path(project_root)), durability=_Durability.AUDIT
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO pending_dw_turns(session_id, ups_seq) VALUES(?, 1) "
            "ON CONFLICT(session_id) DO UPDATE SET ups_seq = ups_seq + 1",
            (session_id,),
        )
        seq = int(
            conn.execute(
                "SELECT ups_seq FROM pending_dw_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        stale = [
            _row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM pending_durable_writes "
                "WHERE session_id = ? AND status = 'pending' AND ups_seq <= ?",
                (session_id, seq - _EXPIRY_TURNS),
            ).fetchall()
        ]
        if stale:
            conn.execute(
                "UPDATE pending_durable_writes SET status = 'expired', resolved_at = ? "
                "WHERE session_id = ? AND status = 'pending' AND ups_seq <= ?",
                (_now(), session_id, seq - _EXPIRY_TURNS),
            )
        conn.commit()
    return {"ups_seq": seq, "expired": stale}


def create_pending(
    project_root: Path,
    session_id: str,
    *,
    ups_seq: int,
    snippet: str,
    verbs: tuple[str, ...] = (),
    objects: tuple[str, ...] = (),
    suggested_target: str = "backlog",
    confidence: float = 0.0,
    ambiguous: bool = False,
) -> int:
    init_db(project_root)
    with _canonical_connect(
        str(_db_path(project_root)),
        durability=_Durability.AUDIT,
        row_factory=False,
    ) as conn:
        cur = conn.execute(
            "INSERT INTO pending_durable_writes "
            "(session_id, ups_seq, detected_at, snippet, verbs_json, objects_json, "
            " suggested_target, confidence, ambiguous) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                int(ups_seq),
                _now(),
                snippet[:300],
                json.dumps(sorted(verbs)),
                json.dumps(sorted(objects)),
                suggested_target,
                float(confidence),
                1 if ambiguous else 0,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def satisfy_pending(project_root: Path, session_id: str, *, satisfied_by: str) -> list[int]:
    """Mark ALL of the session's pending rows satisfied by a successful
    durable write. Returns the satisfied row ids ([] when none pending)."""
    init_db(project_root)
    with _canonical_connect(
        str(_db_path(project_root)),
        durability=_Durability.AUDIT,
        row_factory=False,
    ) as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM pending_durable_writes "
                "WHERE session_id = ? AND status = 'pending'",
                (session_id,),
            ).fetchall()
        ]
        if ids:
            conn.execute(
                "UPDATE pending_durable_writes SET status = 'satisfied', "
                "satisfied_by = ?, resolved_at = ? "
                "WHERE session_id = ? AND status = 'pending'",
                (satisfied_by[:200], _now(), session_id),
            )
            conn.commit()
    return ids


def decline_pending(project_root: Path, session_id: str, row_id: int) -> bool:
    """Operator answered the ambiguity confirm with 'no change' — close the row."""
    init_db(project_root)
    with _canonical_connect(
        str(_db_path(project_root)),
        durability=_Durability.AUDIT,
        row_factory=False,
    ) as conn:
        cur = conn.execute(
            "UPDATE pending_durable_writes SET status = 'confirm_declined', resolved_at = ? "
            "WHERE id = ? AND session_id = ? AND status = 'pending'",
            (_now(), int(row_id), session_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_status(project_root: Path, row_id: int) -> str | None:
    """The row's current status, or None when no such row exists."""
    init_db(project_root)
    with _canonical_connect(
        str(_db_path(project_root)), durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        row = conn.execute(
            "SELECT status FROM pending_durable_writes WHERE id = ?", (int(row_id),)
        ).fetchone()
    return None if row is None else str(row[0])


def list_rows(
    project_root: Path, statuses: tuple[str, ...] = ("pending", "expired")
) -> list[dict[str, Any]]:
    """All rows (every session) in the given statuses, oldest first."""
    init_db(project_root)
    marks = ",".join("?" for _ in statuses)
    with _canonical_connect(
        str(_db_path(project_root)), durability=_Durability.AUDIT
    ) as conn:
        conn.row_factory = sqlite3.Row
        return [
            _row_to_dict(r)
            for r in conn.execute(
                f"SELECT * FROM pending_durable_writes WHERE status IN ({marks}) ORDER BY id",
                tuple(statuses),
            ).fetchall()
        ]


def mark_false_positive(project_root: Path, row_id: int, reason: str) -> bool:
    """Close a pending/expired row as a false positive. NEVER deletes: the row
    stays with ``satisfied_by='false_positive:<reason>'``. Idempotent - only
    pending/expired rows transition."""
    init_db(project_root)
    with _canonical_connect(
        str(_db_path(project_root)),
        durability=_Durability.AUDIT,
        row_factory=False,
    ) as conn:
        cur = conn.execute(
            "UPDATE pending_durable_writes SET status = 'false_positive', "
            "satisfied_by = ?, resolved_at = ? "
            "WHERE id = ? AND status IN ('pending','expired')",
            (f"false_positive:{reason}"[:200], _now(), int(row_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def list_pending(project_root: Path, session_id: str) -> list[dict[str, Any]]:
    init_db(project_root)
    with _canonical_connect(
        str(_db_path(project_root)), durability=_Durability.AUDIT
    ) as conn:
        conn.row_factory = sqlite3.Row
        return [
            _row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM pending_durable_writes "
                "WHERE session_id = ? AND status = 'pending' ORDER BY id",
                (session_id,),
            ).fetchall()
        ]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["verbs"] = json.loads(d.pop("verbs_json", "[]") or "[]")
    d["objects"] = json.loads(d.pop("objects_json", "[]") or "[]")
    d["ambiguous"] = bool(d.get("ambiguous"))
    return d
