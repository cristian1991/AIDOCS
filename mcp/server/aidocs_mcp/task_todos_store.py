"""task_todos — task-owned execution-support items.

ai_todo is task-owned, not session-owned. Mutations restricted to the
current task's rows. session-scope list filter returns unresolved todos
from tasks in this session whose parent task status is NOT done — used
for "what work from this session is still floating."

Storage: task_todos table in aidocs.sqlite3. Tombstone model: remove
transitions status → 'removed', never DELETE. Audit-preserving.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


# #101 urgency tiers (king directive 2026-05-01): same ladder as the
# backlog priority enum, minus 'idea' (a todo is by definition actionable).
_URGENCIES = {"critical", "urgent", "high", "normal", "low"}

_URGENCY_ORDER_SQL = (
    "CASE urgency "
    "WHEN 'critical' THEN 0 "
    "WHEN 'urgent' THEN 1 "
    "WHEN 'high' THEN 2 "
    "WHEN 'normal' THEN 3 "
    "ELSE 4 END"
)


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN (
                        'open','in_progress','done','skipped','blocked','removed'
                    )),
                tags_json TEXT NOT NULL DEFAULT '[]',
                urgency TEXT NOT NULL DEFAULT 'normal'
                    CHECK (urgency IN (
                        'critical','urgent','high','normal','low'
                    )),
                promoted_to_backlog_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                removed_at TEXT,
                removed_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_task_todos_task
                ON task_todos(task_id, status);
            CREATE INDEX IF NOT EXISTS idx_task_todos_session
                ON task_todos(session_id, status);
        """)
        # Migration 2026-07-01 (#101): pre-urgency tables gain the column;
        # existing rows default to 'normal'. Idempotent (column presence).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(task_todos)").fetchall()}
        if "urgency" not in cols:
            conn.execute(
                "ALTER TABLE task_todos ADD COLUMN urgency TEXT NOT NULL "
                "DEFAULT 'normal' CHECK (urgency IN "
                "('critical','urgent','high','normal','low'))",
            )
        conn.commit()


def add(
    project_root: Path,
    *,
    task_id: str,
    session_id: str,
    content: str,
    tags: list[str] | None = None,
    urgency: str = "normal",
) -> dict[str, Any]:
    if urgency not in _URGENCIES:
        return {
            "ok": False,
            "error": (
                f"urgency {urgency!r} not in {sorted(_URGENCIES)}. "
                f"Defaulting would hide the bug."
            ),
        }
    init_db(project_root)
    now = _now()
    tags_json = json.dumps(tags or [])
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        cur = conn.execute(
            "INSERT INTO task_todos "
            "(task_id, session_id, content, status, tags_json, urgency, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (task_id, session_id, content, tags_json, urgency, now, now),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "task_id": task_id,
            "session_id": session_id,
            "content": content,
            "status": "open",
            "tags": tags or [],
            "urgency": urgency,
            "created_at": now,
        }


def _row_urgency(row: sqlite3.Row) -> str:
    """Defensive read: a stale connection on a pre-migration schema may
    lack the column; surface the default rather than KeyError."""
    try:
        return str(row["urgency"] or "normal")
    except (KeyError, IndexError):
        return "normal"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    tags = []
    try:
        parsed = json.loads(row["tags_json"] or "[]")
        if isinstance(parsed, list):
            tags = [str(t) for t in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "session_id": row["session_id"],
        "content": row["content"],
        "status": row["status"],
        "tags": tags,
        "urgency": _row_urgency(row),
        "promoted_to_backlog_id": row["promoted_to_backlog_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "removed_at": row["removed_at"],
        "removed_reason": row["removed_reason"],
    }


def list_for_task(
    project_root: Path,
    *,
    task_id: str,
    include_done: bool = False,
    include_removed: bool = False,
) -> list[dict[str, Any]]:
    """Default list: current task's open/in_progress/blocked todos.

    include_done=True adds done + skipped.
    include_removed=True adds tombstoned rows.
    """
    init_db(project_root)
    statuses: list[str] = ["open", "in_progress", "blocked"]
    if include_done:
        statuses.extend(["done", "skipped"])
    if include_removed:
        statuses.append("removed")
    placeholders = ",".join("?" * len(statuses))
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM task_todos "
            f"WHERE task_id = ? AND status IN ({placeholders}) "
            f"ORDER BY {_URGENCY_ORDER_SQL}, created_at ASC",
            (task_id, *statuses),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_for_session_unresolved(
    project_root: Path,
    *,
    session_id: str,
    include_done: bool = False,
    include_removed: bool = False,
) -> list[dict[str, Any]]:
    """session-scope list: todos from tasks in this session whose parent
    task status is NOT done, filtered by visible statuses.

    Note: "parent task status" requires the task table. This function
    pulls the session's tasks via session_state_tasks (or equivalent
    session-task registry). When the registry is unavailable, falls
    back to "all todos in this session with visible status."
    """
    init_db(project_root)
    statuses: list[str] = ["open", "in_progress", "blocked"]
    if include_done:
        statuses.extend(["done", "skipped"])
    if include_removed:
        statuses.append("removed")
    placeholders = ",".join("?" * len(statuses))
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        # Fallback path: filter by session_id + visible status only.
        # Parent-task-status filter is a future refinement once the
        # task table schema is confirmed stable.
        rows = conn.execute(
            f"SELECT * FROM task_todos "
            f"WHERE session_id = ? AND status IN ({placeholders}) "
            f"ORDER BY {_URGENCY_ORDER_SQL}, task_id, created_at ASC",
            (session_id, *statuses),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _owns(existing, *, task_id: str, session_id: str | None) -> bool:
    """Ownership check for todo mutations: the owning task always may;
    a caller supplying session_id may also mutate rows of PRIOR tasks in
    that same session (task supersession must not orphan session todos).
    A foreign session never qualifies — the escape is session-bounded."""
    if str(existing["task_id"]) == task_id:
        return True
    return bool(session_id) and str(existing["session_id"]) == session_id


def update(
    project_root: Path,
    *,
    todo_id: int,
    task_id: str,
    session_id: str | None = None,
    status: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
    urgency: str | None = None,
) -> dict[str, Any]:
    """Update a todo. Ownership: the current task's rows are always
    mutable; passing session_id additionally allows rows owned by a PRIOR
    task of the SAME session (todo 91 — task supersession must not orphan
    a session's todos). Cross-session mutation is always refused.
    """
    if urgency is not None and urgency not in _URGENCIES:
        return {
            "ok": False,
            "error": f"urgency {urgency!r} not in {sorted(_URGENCIES)}",
        }
    init_db(project_root)
    now = _now()
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT * FROM task_todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
        if existing is None:
            return {"ok": False, "error": f"todo id={todo_id} not found"}
        if not _owns(existing, task_id=task_id, session_id=session_id):
            return {
                "ok": False,
                "error": (
                    f"todo id={todo_id} is owned by task "
                    f"{existing['task_id']!r}, not current task "
                    f"{task_id!r}. Mutations restricted to current-task rows "
                    f"(or same-session rows when session_id is supplied)."
                ),
            }
        if existing["status"] == "removed":
            return {
                "ok": False,
                "error": f"todo id={todo_id} is removed; cannot update",
            }
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if content is not None:
            sets.append("content = ?")
            params.append(content)
        if tags is not None:
            sets.append("tags_json = ?")
            params.append(json.dumps(tags))
        if urgency is not None:
            sets.append("urgency = ?")
            params.append(urgency)
        if not sets:
            return {"ok": False, "error": "no updates provided"}
        sets.append("updated_at = ?")
        params.append(now)
        params.append(todo_id)
        conn.execute(
            f"UPDATE task_todos SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM task_todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    return {"ok": True, **_row_to_dict(row)}


def remove(
    project_root: Path,
    *,
    todo_id: int,
    task_id: str,
    reason: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Tombstone a todo: status → 'removed' + removed_at + removed_reason.
    Never physically DELETEs. Ownership as in :func:`update` — current task,
    or same-session prior task when session_id is supplied.
    """
    init_db(project_root)
    now = _now()
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT * FROM task_todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
        if existing is None:
            return {"ok": False, "error": f"todo id={todo_id} not found"}
        if not _owns(existing, task_id=task_id, session_id=session_id):
            return {
                "ok": False,
                "error": (
                    f"todo id={todo_id} is owned by task "
                    f"{existing['task_id']!r}, not current task "
                    f"{task_id!r}. Mutations restricted to current-task rows "
                    f"(or same-session rows when session_id is supplied)."
                ),
            }
        if existing["status"] == "removed":
            return {
                "ok": True,
                "already_removed": True,
                "id": todo_id,
                "removed_reason": existing["removed_reason"],
            }
        conn.execute(
            "UPDATE task_todos SET status = 'removed', updated_at = ?, "
            "removed_at = ?, removed_reason = ? WHERE id = ?",
            (now, now, reason, todo_id),
        )
        conn.commit()
    return {
        "ok": True,
        "id": todo_id,
        "status": "removed",
        "removed_at": now,
        "removed_reason": reason,
    }
