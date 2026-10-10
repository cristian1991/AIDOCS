"""task_lifecycle — the lightweight, SQL-only task record.

The canonical "is a task active?" signal historically lived in the session
markdown (Status: active). Under the no-file-layer doctrine ("No loose
scrolls. The ledger is the product.") the task lifecycle is recorded HERE,
in sqlite, with no SESSION.md/PLAN.md side effects.

This backs the UserPromptSubmit auto-task: when an imperative or
investigation prompt arrives and no task is active for the session, the
hook opens a task row here so the agent doesn't have to (removing the
"call task_begin first" friction). Tombstone model: complete transitions
status, never DELETE — audit-preserving, and a closed task can't be
resurrected.

Storage: ``tasks`` table in ``.MEMORY/.index/aidocs.sqlite3`` (same db as
task_todos, which anticipated this "task table").
"""

from __future__ import annotations

import sqlite3

# #755/#756: the ONE canonical connect. Every site below was
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none of them
# set a single pragma, so this store ran with foreign_keys OFF (its FKs
# inert), no busy_timeout, and the default synchronous=FULL fsync tax.
# DURABILITY: RUNTIME (the helper's default). A task row is session
# bookkeeping -- "is a task active for this session" -- not evidence and
# not an authority: losing the last write hands nobody a permission back,
# it costs one re-opened task. active_task() also runs on EVERY hook
# event, so this is exactly where synchronous=NORMAL's 8-10x lands.
from ._sqlite_connect import connect as _canonical_connect
from ._sqlite_connect import mark_schema_ensured as _mark_schema_ensured
from ._sqlite_connect import schema_already_ensured as _schema_already_ensured
import time
import uuid
from pathlib import Path
from typing import Any

_KINDS = ("work", "investigation")
_STATUSES = ("active", "done", "abandoned")


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    # ONE schema creation per process per file (#756): this ran on EVERY hook
    # event, paying an open + write lock to learn there was nothing to do. The
    # memo re-verifies the file still exists, so a deleted DB is rebuilt.
    if _schema_already_ensured(db, "task_lifecycle"):
        return
    db.parent.mkdir(parents=True, exist_ok=True)
    with _canonical_connect(db, row_factory=False) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                goal TEXT NOT NULL,
                kind TEXT NOT NULL
                    CHECK (kind IN ('work','investigation')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','done','abandoned')),
                source TEXT NOT NULL DEFAULT 'auto_ups',
                origin_prompt TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_session_status
                ON tasks(session_id, status);
            -- DB-enforced "one active task per session": a partial unique
            -- index makes a second concurrent active insert raise
            -- IntegrityError, so a check-then-insert race can never create
            -- two active tasks for the same session.
            CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_one_active_per_session
                ON tasks(session_id) WHERE status = 'active';
        """)
        conn.commit()
    _mark_schema_ensured(db, "task_lifecycle")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "session_id": row["session_id"],
        "goal": row["goal"],
        "kind": row["kind"],
        "status": row["status"],
        "source": row["source"],
        "origin_prompt": row["origin_prompt"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
    }


def active_task(
    project_root: Path,
    session_id: str,
) -> dict[str, Any] | None:
    """The most-recently-created ACTIVE task for the session, or None."""
    db = _db_path(project_root)
    if not db.is_file():
        return None
    with _canonical_connect(db) as conn:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND status = 'active' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return _row_to_dict(row) if row else None


def has_active_task(project_root: Path, session_id: str) -> bool:
    return active_task(project_root, session_id) is not None


def begin_task(
    project_root: Path,
    *,
    session_id: str,
    goal: str,
    kind: str = "work",
    source: str = "auto_ups",
    origin_prompt: str | None = None,
) -> dict[str, Any]:
    """Open a task row. Caller is responsible for the no-duplicate policy
    (check has_active_task first); this always inserts a new row.
    """
    if kind not in _KINDS:
        kind = "work"
    init_db(project_root)
    now = _now()
    task_id = f"task_{uuid.uuid4().hex[:24]}"
    with _canonical_connect(_db_path(project_root), row_factory=False) as conn:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, session_id, goal, kind, status, source, "
            " origin_prompt, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)",
            (task_id, session_id, goal, kind, source, origin_prompt, now, now),
        )
        conn.commit()
    return {
        "task_id": task_id,
        "session_id": session_id,
        "goal": goal,
        "kind": kind,
        "status": "active",
        "source": source,
        "origin_prompt": origin_prompt,
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
    }


def begin_task_if_none_active(
    project_root: Path,
    *,
    session_id: str,
    goal: str,
    kind: str = "work",
    source: str = "auto_ups",
    origin_prompt: str | None = None,
) -> dict[str, Any] | None:
    """Open a task only when no task is active for the session. Returns the
    new task, or None if one was already active (no-op). This is the
    friction-free auto-start the UserPromptSubmit hook calls.

    Race-safe: the fast-path check is advisory; the authoritative guard is
    the partial unique index (uq_tasks_one_active_per_session). If two
    callers race past the check, the losing INSERT raises IntegrityError
    and we return None — never two active tasks.
    """
    existing = active_task(project_root, session_id)
    if existing is not None:
        return None
    try:
        return begin_task(
            project_root,
            session_id=session_id,
            goal=goal,
            kind=kind,
            source=source,
            origin_prompt=origin_prompt,
        )
    except sqlite3.IntegrityError:
        # Another caller won the race and opened the active task first.
        return None


def complete_task(
    project_root: Path,
    *,
    session_id: str,
    task_id: str | None = None,
    next_status: str = "done",
) -> bool:
    """Close the active task (or a specific task_id) for the session.
    Returns True if a row transitioned. Tombstone: never DELETEs.
    """
    if next_status not in ("done", "abandoned"):
        next_status = "done"
    db = _db_path(project_root)
    if not db.is_file():
        return False
    now = _now()
    with _canonical_connect(db, row_factory=False) as conn:
        if task_id is not None:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, closed_at = ? "
                "WHERE task_id = ? AND session_id = ? AND status = 'active'",
                (next_status, now, now, task_id, session_id),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, closed_at = ? "
                "WHERE session_id = ? AND status = 'active'",
                (next_status, now, now, session_id),
            )
        conn.commit()
        return cur.rowcount > 0




def delete_prompt_submit_state(
    project_root: Path,
    *,
    session_id: str,
) -> None:
    """Delete task state for a session minted within this prompt submit."""
    init_db(project_root)
    with _canonical_connect(_db_path(project_root), row_factory=False) as conn:
        conn.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
        conn.commit()


def snapshot_prompt_submit_state(
    project_root: Path,
    *,
    session_id: str,
) -> dict[str, object]:
    """Capture every lifecycle row for the session; errors propagate."""
    from .prompt_submit_store_snapshot import capture_scoped_rows

    init_db(project_root)
    scopes = {"tasks": ("session_id = ?", (session_id,))}
    with _canonical_connect(_db_path(project_root), row_factory=False) as conn:
        return capture_scoped_rows(conn, scopes)


def restore_prompt_submit_state(
    project_root: Path,
    snapshot: dict[str, object],
    *,
    session_id: str,
) -> None:
    from .prompt_submit_store_snapshot import restore_scoped_rows

    init_db(project_root)
    scopes = {"tasks": ("session_id = ?", (session_id,))}
    # BORROWED HANDLE (#756) -- see EscalationStore.restore_prompt_submit_state
    # for the full reasoning. restore_scoped_rows opens `with conn:` on the
    # handle it is GIVEN -- a TRANSACTION on someone else's connection, not
    # ownership -- and a ClosingConnection would CLOSE it there, leaving the
    # enclosing block to commit a dead handle. So this function owns the
    # handle explicitly and closes it in `finally`.
    conn = _canonical_connect(_db_path(project_root), row_factory=False)
    conn._aidocs_borrowed = True  # noqa: SLF001 -- our own subclass
    try:
        with conn:
            restore_scoped_rows(conn, scopes, snapshot)
    finally:
        conn.close()
