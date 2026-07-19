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

Statuses: pending → satisfied | expired | confirm_declined.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

# End-of-next-turn expiry (Empire §9.3): a row created on UPS turn N is still
# actionable through turn N+1 and expires when turn N+2 begins.
_EXPIRY_TURNS = 2


def _db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_durable_writes (
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
                        'pending','satisfied','expired','confirm_declined'
                    )),
                satisfied_by TEXT,
                resolved_at TEXT
            );
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
    with sqlite3.connect(str(_db_path(project_root))) as conn:
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
    with sqlite3.connect(str(_db_path(project_root))) as conn:
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
    with sqlite3.connect(str(_db_path(project_root))) as conn:
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
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        cur = conn.execute(
            "UPDATE pending_durable_writes SET status = 'confirm_declined', resolved_at = ? "
            "WHERE id = ? AND session_id = ? AND status = 'pending'",
            (_now(), int(row_id), session_id),
        )
        conn.commit()
        return cur.rowcount > 0


def list_pending(project_root: Path, session_id: str) -> list[dict[str, Any]]:
    init_db(project_root)
    with sqlite3.connect(str(_db_path(project_root))) as conn:
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
