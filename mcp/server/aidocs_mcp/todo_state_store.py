"""Per-session TodoWrite state store.

Persists the most recent TodoWrite payload for a (project_root, session_id)
so the PostToolUse hook can diff incoming payloads against prior state and
map transitions to task_begin/task_update/task_complete.

Schema:
    session_todos(
        session_id TEXT PRIMARY KEY,
        todos_json TEXT NOT NULL,  -- JSON-serialized list of todo items
        updated_at TEXT NOT NULL
    )

Lives in the project's SQLite index (.MEMORY/.index/aidocs.sqlite3) so it
shares a connection pool with execution_events and the query gate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_todos (
            session_id TEXT PRIMARY KEY,
            todos_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.commit()


class TodoStateStore:
    """Read/write the most recent TodoWrite payload per session."""

    def get(self, project_root: Path, session_id: str) -> list[dict[str, Any]]:
        db = _db_path(project_root)
        if not db.is_file():
            return []
        with sqlite3.connect(str(db)) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT todos_json FROM session_todos WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return []
        try:
            data = json.loads(row[0])
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [t for t in data if isinstance(t, dict)]

    def set(
        self,
        project_root: Path,
        session_id: str,
        todos: list[dict[str, Any]],
    ) -> None:
        db = _db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(str(db)) as conn:
            _init(conn)
            conn.execute(
                "INSERT OR REPLACE INTO session_todos "
                "(session_id, todos_json, updated_at) VALUES (?, ?, ?)",
                (session_id, json.dumps(todos, default=str), now),
            )
            conn.commit()

    def clear(self, project_root: Path, session_id: str) -> None:
        db = _db_path(project_root)
        if not db.is_file():
            return
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "DELETE FROM session_todos WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()


def diff_todos(
    prev: list[dict[str, Any]],
    curr: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare two TodoWrite payloads and return transition signals.

    Returns:
        {
            "first_submission": bool,  # no prior state
            "all_completed": bool,     # every item now completed
            "completed_now": list,     # items that went ->completed this turn
            "started_now": list,       # items that went pending->in_progress
            "first_in_progress": dict | None,  # current in_progress item
            "pending_count": int,
            "in_progress_count": int,
            "completed_count": int,
            "total": int,
        }
    Keys on todo items: 'content' (description), 'status', 'activeForm'.

    """

    def _status(t: dict[str, Any]) -> str:
        return str(t.get("status", "")).lower()

    def _key(t: dict[str, Any]) -> str:
        return str(t.get("content", ""))

    prev_by_key = {_key(t): _status(t) for t in prev}

    completed_now: list[dict[str, Any]] = []
    started_now: list[dict[str, Any]] = []
    for t in curr:
        k = _key(t)
        s = _status(t)
        prev_s = prev_by_key.get(k)
        if s == "completed" and prev_s != "completed":
            completed_now.append(t)
        elif s == "in_progress" and prev_s != "in_progress":
            started_now.append(t)

    first_in_progress = next(
        (t for t in curr if _status(t) == "in_progress"),
        None,
    )
    statuses = [_status(t) for t in curr]
    return {
        "first_submission": len(prev) == 0 and len(curr) > 0,
        "all_completed": bool(curr) and all(s == "completed" for s in statuses),
        "completed_now": completed_now,
        "started_now": started_now,
        "first_in_progress": first_in_progress,
        "pending_count": statuses.count("pending"),
        "in_progress_count": statuses.count("in_progress"),
        "completed_count": statuses.count("completed"),
        "total": len(curr),
    }
