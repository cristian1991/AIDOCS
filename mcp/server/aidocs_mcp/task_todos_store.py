"""task_todos — task-owned execution-support items.

Todos (ai_task todo modes, #83) are task-owned, not session-owned.
Mutations restricted to the
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
import uuid
from pathlib import Path
from typing import Any

# #755/#756: the ONE canonical connect. Every site below was
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none of them
# set a single pragma, so this store ran with foreign_keys OFF (its FKs
# inert), no busy_timeout, and the default synchronous=FULL fsync tax.
# DURABILITY: RUNTIME (the helper's default). This table is explicitly a
# DERIVED cache: the todo event log is the authority (see
# rebuild_from_events -- "sqlite = derived"), so a commit lost to a power
# cut is re-folded from the log rather than gone. A todo is a work item,
# not a grant: losing one hands nobody an authority back.
from ._sqlite_connect import connect as _canonical_connect


# #101 urgency tiers (Empire directive 2026-05-01): same ladder as the
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
    with _canonical_connect(db, row_factory=False) as conn:
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
        # Migration 2026-07-07: global_id (uuid) — the STABLE cross-agent entity
        # id for event-sourced sync. Local autoincrement collides across agent
        # clones (fresh clone restarts the counter). Backfilled. Idempotent.
        if "global_id" not in cols:
            conn.execute("ALTER TABLE task_todos ADD COLUMN global_id TEXT NOT NULL DEFAULT ''")
            conn.row_factory = sqlite3.Row
            for _r in conn.execute("SELECT id FROM task_todos WHERE global_id = ''").fetchall():
                conn.execute(
                    "UPDATE task_todos SET global_id = ? WHERE id = ?", (uuid.uuid4().hex, _r["id"])
                )
            conn.row_factory = None
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_todos_global_id ON task_todos(global_id)"
            )
        conn.commit()


def _emit_todo(project_root: Path, global_id: str, op: str, fields: dict, *, session_id: str = "") -> None:
    """Store-layer emit for the todo stream (Phase 1). Best-effort; never raises.
    Every todo write path feeds the canonical event log here."""
    try:
        from . import sync_store

        sync_store.emit(project_root, "todo", global_id, op, fields, session_id=session_id)
    except Exception:
        pass


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
    with _canonical_connect(_db_path(project_root), row_factory=False) as conn:
        gid = uuid.uuid4().hex
        cur = conn.execute(
            "INSERT INTO task_todos "
            "(task_id, session_id, content, status, tags_json, urgency, "
            " created_at, updated_at, global_id) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (task_id, session_id, content, tags_json, urgency, now, now, gid),
        )
        conn.commit()
        new_id = cur.lastrowid
    _emit_todo(
        project_root, gid, "add",
        {
            "task_id": task_id, "session_id": session_id, "content": content,
            "status": "open", "tags": tags or [], "urgency": urgency,
            "created_at": now, "updated_at": now,
        },
        session_id=session_id or "",
    )
    return {
        "id": new_id,
        "global_id": gid,
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
    try:
        global_id = row["global_id"] or ""
    except (KeyError, IndexError):
        global_id = ""
    return {
        "id": row["id"],
        "global_id": global_id,
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


def _tags_filter_clause(tags: list[str] | None) -> tuple[str, list[str]]:
    """SQL fragment for the any-of tags list filter on tags_json.

    Returns ("", []) for an empty/absent filter (no-op — full listing).
    Matching uses the JSON-quoted LIKE anchor ('%"tag"%') so a requested
    tag only matches a whole stored tag, never a substring ('bug' never
    hits 'debug'). Blank entries are noise, not a match-nothing filter.
    """
    wanted = [str(t).strip() for t in (tags or []) if str(t).strip()]
    if not wanted:
        return "", []
    ors = " OR ".join(["tags_json LIKE ?"] * len(wanted))
    return f" AND ({ors})", [f'%"{t}"%' for t in wanted]


def list_for_task(
    project_root: Path,
    *,
    task_id: str,
    include_done: bool = False,
    include_removed: bool = False,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Default list: current task's open/in_progress/blocked todos.

    include_done=True adds done + skipped.
    include_removed=True adds tombstoned rows.
    tags=[...] keeps only rows whose tag set intersects the requested
    tags (any-of); empty/None applies no tag filtering.
    """
    init_db(project_root)
    try:
        from . import sync_store

        sync_store.maybe_hydrate(project_root, "todo", hydrate_from_events)  # fold-on-read
    except Exception:
        pass
    statuses: list[str] = ["open", "in_progress", "blocked"]
    if include_done:
        statuses.extend(["done", "skipped"])
    if include_removed:
        statuses.append("removed")
    placeholders = ",".join("?" * len(statuses))
    tag_clause, tag_params = _tags_filter_clause(tags)
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM task_todos "
            f"WHERE task_id = ? AND status IN ({placeholders}){tag_clause} "
            f"ORDER BY {_URGENCY_ORDER_SQL}, created_at ASC",
            (task_id, *statuses, *tag_params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_for_session_unresolved(
    project_root: Path,
    *,
    session_id: str,
    include_done: bool = False,
    include_removed: bool = False,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """session-scope list: todos from tasks in this session whose parent
    task status is NOT done, filtered by visible statuses.

    tags=[...] keeps only rows whose tag set intersects the requested
    tags (any-of); empty/None applies no tag filtering.

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
    tag_clause, tag_params = _tags_filter_clause(tags)
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        # Fallback path: filter by session_id + visible status only.
        # Parent-task-status filter is a future refinement once the
        # task table schema is confirmed stable.
        rows = conn.execute(
            f"SELECT * FROM task_todos "
            f"WHERE session_id = ? AND status IN ({placeholders}){tag_clause} "
            f"ORDER BY {_URGENCY_ORDER_SQL}, task_id, created_at ASC",
            (session_id, *statuses, *tag_params),
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
    reason: str | None = None,
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
    with _canonical_connect(_db_path(project_root)) as conn:
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
            note = (reason or "").strip()
            if not note:
                return {"ok": False, "error": "no updates provided"}
            # Reason-only ANNOTATION (#314): no field changed, but a
            # non-empty reason was given. Record it — bump updated_at and
            # emit an update event carrying the reason (mirrors how remove
            # persists removed_reason) — then return ok.
            conn.execute(
                "UPDATE task_todos SET updated_at = ? WHERE id = ?",
                (now, todo_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM task_todos WHERE id = ?",
                (todo_id,),
            ).fetchone()
            gid = str(row["global_id"] if "global_id" in row.keys() else "")
            if gid:
                _emit_todo(
                    project_root, gid, "update",
                    {"updated_at": now, "reason": note},
                )
            return {"ok": True, "annotation": True, "reason": note, **_row_to_dict(row)}
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
        gid = str(row["global_id"] if "global_id" in row.keys() else "")
    _changed: dict[str, Any] = {"updated_at": now}
    if status is not None:
        _changed["status"] = status
    if content is not None:
        _changed["content"] = content
    if tags is not None:
        _changed["tags"] = tags
    if urgency is not None:
        _changed["urgency"] = urgency
    if gid:
        _emit_todo(project_root, gid, "update", _changed)
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
    with _canonical_connect(_db_path(project_root)) as conn:
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
        gid = str(existing["global_id"] if "global_id" in existing.keys() else "")
    if gid:
        _emit_todo(
            project_root, gid, "update",
            {"status": "removed", "updated_at": now, "removed_at": now, "removed_reason": reason},
        )
    return {
        "ok": True,
        "id": todo_id,
        "status": "removed",
        "removed_at": now,
        "removed_reason": reason,
    }


def _materialize_todo(project_root: Path, folded: dict[str, dict]) -> None:
    """Upsert folded todo event-state into sqlite by global_id (DERIVE step; no
    re-emit)."""
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        for gid, r in folded.items():
            existing = conn.execute(
                "SELECT * FROM task_todos WHERE global_id = ?", (gid,)
            ).fetchone()
            if existing is not None:
                # #399 / generalized #376 — NON-DESTRUCTIVE MERGE. The folded
                # state is a PATCH over the live row, never a full overwrite. A
                # field ABSENT from the fold (e.g. a status-only update whose
                # add-event was quarantined) keeps its existing value; content is
                # extra-guarded so an empty content never blanks a non-empty body.
                new_content = str(r.get("content") or "")
                content = new_content if new_content.strip() else str(existing["content"] or "")
                task_id = str(r["task_id"]) if "task_id" in r.keys() else str(existing["task_id"] or "")
                session_id = (
                    str(r["session_id"]) if "session_id" in r.keys() else str(existing["session_id"] or "")
                )
                status = str(r["status"]) if "status" in r.keys() else str(existing["status"] or "open")
                tags_json = (
                    json.dumps(r["tags"] or []) if "tags" in r.keys() else str(existing["tags_json"] or "[]")
                )
                urgency = str(r["urgency"]) if "urgency" in r.keys() else str(existing["urgency"] or "normal")
                updated_at = str(r.get("updated_at") or existing["updated_at"] or _now())
                removed_at = r["removed_at"] if "removed_at" in r.keys() else existing["removed_at"]
                removed_reason = (
                    r["removed_reason"] if "removed_reason" in r.keys() else existing["removed_reason"]
                )
                conn.execute(
                    "UPDATE task_todos SET task_id=?, session_id=?, content=?, status=?, "
                    "tags_json=?, urgency=?, updated_at=?, removed_at=?, removed_reason=? "
                    "WHERE global_id=?",
                    (task_id, session_id, content, status, tags_json, urgency, updated_at,
                     removed_at, removed_reason, gid),
                )
            else:
                task_id = str(r.get("task_id", ""))
                session_id = str(r.get("session_id", ""))
                content = str(r.get("content", ""))
                status = str(r.get("status", "open"))
                tags_json = json.dumps(r.get("tags") or [])
                urgency = str(r.get("urgency", "normal"))
                created_at = str(r.get("created_at") or _now())
                updated_at = str(r.get("updated_at") or created_at)
                conn.execute(
                    "INSERT INTO task_todos (task_id, session_id, content, status, tags_json, "
                    "urgency, created_at, updated_at, removed_at, removed_reason, global_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, session_id, content, status, tags_json, urgency, created_at,
                     updated_at, r.get("removed_at"), r.get("removed_reason"), gid),
                )
        conn.commit()


def hydrate_from_events(project_root: Path) -> int:
    """Phase 2/3: fold the todo event log into sqlite (materialized cache).

    AUTHORITY (#376, 2026-07-13): folds ONLY receipted events (produced by an
    authenticated write on THIS gate). Incoming/foreign/forged event files
    without a receipt are QUARANTINED (clear-status log), never applied — same
    authority boundary as the backlog stream. Idempotent; does NOT re-emit."""
    init_db(project_root)
    from . import sync_store

    all_events = sync_store.GitEventTransport(project_root).read("todo")
    authoritative, incoming = sync_store.split_by_authority(
        project_root, "todo", all_events
    )
    if incoming:
        sync_store.record_quarantine(project_root, "todo", incoming)
    folded = sync_store.fold_events(authoritative)
    _materialize_todo(project_root, folded)
    return len(folded)


def rebuild_from_events(project_root: Path, *, adopt_incoming: bool = False) -> int:
    """Phase 3: rebuild sqlite from the AUTHORITATIVE todo event log (sqlite =
    derived). Default rebuilds only this gate's receipted history; an unreceipted
    file can neither survive nor enter via rebuild.

    OPERATOR-APPROVED SNAPSHOT RECOVERY (#376): ``adopt_incoming=True`` adopts
    EVERY current event file as authoritative (the operator declares the present
    event log to be truth) before rebuilding — the explicit, operator-gated
    import path, never reached on fold-on-read."""
    init_db(project_root)
    from . import sync_store

    if adopt_incoming:
        sync_store.adopt_events_as_authoritative(project_root, "todo", None)
    with _canonical_connect(_db_path(project_root), row_factory=False) as conn:
        conn.execute("DELETE FROM task_todos")
        conn.commit()
    return hydrate_from_events(project_root)


def seed_events_from_sqlite(project_root: Path) -> int:
    """One-time backfill (idempotent): emit an 'add' event carrying the CURRENT
    state of every todo row with NO event yet. Skips rows already in the log
    (re-run = no-op); includes tombstoned rows. Returns rows seeded."""
    init_db(project_root)
    from . import sync_store

    seen = {e.entity_id for e in sync_store.GitEventTransport(project_root).read("todo")}
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM task_todos").fetchall()
    seeded = 0
    for row in rows:
        gid = str(row["global_id"] or "")
        if not gid or gid in seen:
            continue
        d = _row_to_dict(row)
        _emit_todo(
            project_root, gid, "add",
            {
                "task_id": d["task_id"], "session_id": d["session_id"],
                "content": d["content"], "status": d["status"], "tags": d["tags"],
                "urgency": d["urgency"], "created_at": d["created_at"],
                "updated_at": d["updated_at"], "removed_at": d["removed_at"],
                "removed_reason": d["removed_reason"],
            },
            session_id=d["session_id"] or "",
        )
        seeded += 1
    return seeded
