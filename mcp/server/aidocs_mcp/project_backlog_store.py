"""project_backlog — project-owned durable future-work inventory.

ai_backlog is project-owned, not session-owned. session_id is metadata
(`created_in_session_id`), never ownership key. source_task_id +
promoted_from_todo_id + linked_task_id are nullable relation fields for
future promote/link operations (schema-ready; tool surface deferred).

Status semantics (canonical, do not drift):
  open         — active, not yet worked
  in_progress  — currently being worked
  blocked      — can't proceed, waiting on something
  done         — work completed successfully
  rejected     — CONSIDERED and intentionally DECLINED as work.
                 Product/engineering judgment: "we decided not to do this."
                 Has decision value; can be reviewed later.
  removed      — administratively HIDDEN/tombstoned from active lists
                 WITHOUT implying product judgment. Uses: duplicates,
                 accidental adds, stale items, cleanup. Audit-preserving.

Tombstone model: remove transitions status → 'removed', never DELETE.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_STATUSES = {"open", "in_progress", "done", "blocked", "rejected", "removed"}
_PRIORITIES = {"critical", "high", "medium", "low", "idea"}

# KISS title extraction (#59): real stored field, derived once at
# add() time, never re-parsed at list() time. First markdown heading
# (# / ## / ###) wins; falls back to first non-blank line; then to a
# trimmed prefix of the body. Hard cap at 160 chars so list-default
# rows stay lean.
_TITLE_MAX = 160


def _extract_title(content: str) -> str:
    if not content:
        return ""
    body = content.strip()
    if not body:
        return ""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Markdown heading: strip leading #'s and any trailing #'s.
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().rstrip("#").strip()
            if heading:
                return heading[:_TITLE_MAX]
        # First non-blank, non-heading line is the fallback.
        return stripped[:_TITLE_MAX]
    return body[:_TITLE_MAX]


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS project_backlog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN (
                        'open','in_progress','done','blocked','rejected','removed'
                    )),
                priority TEXT NOT NULL DEFAULT 'medium'
                    CHECK (priority IN (
                        'critical','high','medium','low','idea'
                    )),
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_in_session_id TEXT,
                source_task_id TEXT,
                promoted_from_todo_id INTEGER,
                linked_task_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                removed_at TEXT,
                removed_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_project_backlog_status
                ON project_backlog(status, priority);
            CREATE INDEX IF NOT EXISTS idx_project_backlog_linked
                ON project_backlog(linked_task_id);
        """)
        # Migration 2026-04-26: add title column (KISS list defaults
        # use it as primary identifier; derived from content at add()
        # time, backfilled here for pre-migration rows). Idempotent.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(project_backlog)").fetchall()}
        if "title" not in cols:
            conn.execute("ALTER TABLE project_backlog ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT id, content FROM project_backlog WHERE title = ''",
            ).fetchall():
                derived = _extract_title(row["content"] or "")
                conn.execute(
                    "UPDATE project_backlog SET title = ? WHERE id = ?",
                    (derived, row["id"]),
                )
            conn.row_factory = None
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    tags = []
    try:
        parsed = json.loads(row["tags_json"] or "[]")
        if isinstance(parsed, list):
            tags = [str(t) for t in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    # title column is post-migration. Older rows may not surface it
    # via row.keys() if a stale connection reads pre-ALTER schema; fall
    # back to a freshly-derived title in that defensive case so list
    # never returns an empty title field for an indexed item.
    try:
        title = row["title"] or ""
    except (KeyError, IndexError):
        title = ""
    if not title:
        title = _extract_title(row["content"] or "")
    return {
        "id": row["id"],
        "title": title,
        "content": row["content"],
        "status": row["status"],
        "priority": row["priority"],
        "tags": tags,
        "created_in_session_id": row["created_in_session_id"],
        "source_task_id": row["source_task_id"],
        "promoted_from_todo_id": row["promoted_from_todo_id"],
        "linked_task_id": row["linked_task_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "removed_at": row["removed_at"],
        "removed_reason": row["removed_reason"],
    }


def add(
    project_root: Path,
    *,
    content: str,
    priority: str = "medium",
    tags: list[str] | None = None,
    created_in_session_id: str | None = None,
    source_task_id: str | None = None,
) -> dict[str, Any]:
    if priority not in _PRIORITIES:
        return {
            "ok": False,
            "error": (
                f"priority {priority!r} not in "
                f"{sorted(_PRIORITIES)}. Defaulting would hide the bug."
            ),
        }
    init_db(project_root)
    now = _now()
    tags_json = json.dumps(tags or [])
    title = _extract_title(content)
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        cur = conn.execute(
            "INSERT INTO project_backlog "
            "(content, title, status, priority, tags_json, "
            " created_in_session_id, source_task_id, "
            " created_at, updated_at) "
            "VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?)",
            (content, title, priority, tags_json, created_in_session_id, source_task_id, now, now),
        )
        conn.commit()
        return {
            "ok": True,
            "id": cur.lastrowid,
            "content": content,
            "title": title,
            "status": "open",
            "priority": priority,
            "tags": tags or [],
            "created_at": now,
        }


def list_backlog(
    project_root: Path,
    *,
    status: str | None = None,
    priority: str | None = None,
    tag_filter: str | None = None,
    include_removed: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    init_db(project_root)
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    elif not include_removed:
        clauses.append("status != 'removed'")
    if priority is not None:
        clauses.append("priority = ?")
        params.append(priority)
    if tag_filter:
        clauses.append("tags_json LIKE ?")
        params.append(f'%"{tag_filter}"%')
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Priority ordering: critical > high > medium > low > idea.
    # status ordering: active first, then blocked, then closed.
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM project_backlog {where} "
            f"ORDER BY "
            f"  CASE status "
            f"    WHEN 'in_progress' THEN 0 "
            f"    WHEN 'open' THEN 1 "
            f"    WHEN 'blocked' THEN 2 "
            f"    WHEN 'done' THEN 3 "
            f"    WHEN 'rejected' THEN 4 "
            f"    ELSE 5 END, "
            f"  CASE priority "
            f"    WHEN 'critical' THEN 0 "
            f"    WHEN 'high' THEN 1 "
            f"    WHEN 'medium' THEN 2 "
            f"    WHEN 'low' THEN 3 "
            f"    ELSE 4 END, "
            f"  created_at ASC "
            f"LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_by_id(
    project_root: Path,
    *,
    backlog_id: int,
) -> dict[str, Any] | None:
    """Single-item read for ai_backlog mode='get'. Returns full row
    dict (including content) or None when id not found. Used by the
    paged-body get surface; list mode never calls this.
    """
    init_db(project_root)
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM project_backlog WHERE id = ?",
            (backlog_id,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def update(
    project_root: Path,
    *,
    backlog_id: int,
    status: str | None = None,
    content: str | None = None,
    priority: str | None = None,
    tags: list[str] | None = None,
    linked_task_id: str | None = None,
) -> dict[str, Any]:
    init_db(project_root)
    if status is not None and status not in _STATUSES:
        return {"ok": False, "error": f"status {status!r} not in {sorted(_STATUSES)}"}
    if priority is not None and priority not in _PRIORITIES:
        return {"ok": False, "error": f"priority {priority!r} not in {sorted(_PRIORITIES)}"}
    now = _now()
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT * FROM project_backlog WHERE id = ?",
            (backlog_id,),
        ).fetchone()
        if existing is None:
            return {"ok": False, "error": f"backlog id={backlog_id} not found"}
        if existing["status"] == "removed":
            return {
                "ok": False,
                "error": f"backlog id={backlog_id} is removed; cannot update",
            }
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status == "done":
                sets.append("completed_at = ?")
                params.append(now)
        if content is not None:
            sets.append("content = ?")
            params.append(content)
            # Re-derive title when body changes; title stays in sync.
            sets.append("title = ?")
            params.append(_extract_title(content))
        if priority is not None:
            sets.append("priority = ?")
            params.append(priority)
        if tags is not None:
            sets.append("tags_json = ?")
            params.append(json.dumps(tags))
        if linked_task_id is not None:
            sets.append("linked_task_id = ?")
            params.append(linked_task_id)
        if not sets:
            return {"ok": False, "error": "no updates provided"}
        sets.append("updated_at = ?")
        params.append(now)
        params.append(backlog_id)
        conn.execute(
            f"UPDATE project_backlog SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM project_backlog WHERE id = ?",
            (backlog_id,),
        ).fetchone()
    return {"ok": True, **_row_to_dict(row)}


def remove(
    project_root: Path,
    *,
    backlog_id: int,
    reason: str,
) -> dict[str, Any]:
    """Tombstone a backlog item. Never DELETEs."""
    init_db(project_root)
    now = _now()
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT * FROM project_backlog WHERE id = ?",
            (backlog_id,),
        ).fetchone()
        if existing is None:
            return {"ok": False, "error": f"backlog id={backlog_id} not found"}
        if existing["status"] == "removed":
            return {
                "ok": True,
                "already_removed": True,
                "id": backlog_id,
                "removed_reason": existing["removed_reason"],
            }
        conn.execute(
            "UPDATE project_backlog SET status = 'removed', "
            "updated_at = ?, removed_at = ?, removed_reason = ? "
            "WHERE id = ?",
            (now, now, reason, backlog_id),
        )
        conn.commit()
    return {
        "ok": True,
        "id": backlog_id,
        "status": "removed",
        "removed_at": now,
        "removed_reason": reason,
    }
