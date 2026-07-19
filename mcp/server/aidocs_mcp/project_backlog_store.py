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
  merged       — absorbed into an umbrella item (#450). merged_into
                 points at the surviving id; the row is KEPT (never
                 removed) and reversible: update(status='open') clears
                 merged_into. Hidden from list by default
                 (include_merged=True shows).

Tombstone model: remove transitions status → 'removed', never DELETE.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

_STATUSES = {"open", "in_progress", "done", "blocked", "rejected", "removed", "merged"}
# #101 urgency tiers (Empire directive 2026-05-01): 'urgent' sits between
# critical and high ("operationally bleeding now"); 'medium' renamed to
# 'normal'. 'medium' remains an INPUT alias (coerced) so old callers and
# the dashboard embed keep working; stored rows use one name.
_PRIORITIES = {"critical", "urgent", "high", "normal", "low", "idea"}
_PRIORITY_ALIASES = {"medium": "normal"}

# Canonical severity order, highest first — the ONE list consumers iterate
# (roadmap bands, dashboards). Derive from this; never hardcode a copy:
# the pre-#101 hardcoded copy in roadmap_layer_progress silently DROPPED
# items in unknown bands from progress.
PRIORITY_ORDER: tuple[str, ...] = ("critical", "urgent", "high", "normal", "low", "idea")


def _canon_priority(priority: str) -> str:
    return _PRIORITY_ALIASES.get(priority, priority)

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
        # #242 recovery guard: a rebuild killed between DROP and RENAME
        # strands every row in project_backlog__new — and the CREATE IF NOT
        # EXISTS below would quietly build an EMPTY table over the evidence
        # (the 2026-07-04 incident). Complete the rename FIRST.
        from .store_migrations import recover_interrupted_rename

        recover_interrupted_rename(
            conn,
            "project_backlog",
            finish_statements=[
                "CREATE INDEX IF NOT EXISTS idx_project_backlog_status "
                "ON project_backlog(status, priority)",
                "CREATE INDEX IF NOT EXISTS idx_project_backlog_linked "
                "ON project_backlog(linked_task_id)",
            ],
            guard_views=["canonical_rows"],
        )
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS project_backlog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN (
                        'open','in_progress','done','blocked','rejected','removed','merged'
                    )),
                priority TEXT NOT NULL DEFAULT 'normal'
                    CHECK (priority IN (
                        'critical','urgent','high','normal','low','idea'
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
                removed_reason TEXT,
                merged_into INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_project_backlog_status
                ON project_backlog(status, priority);
            CREATE INDEX IF NOT EXISTS idx_project_backlog_linked
                ON project_backlog(linked_task_id);
        """)
        # Migration 2026-07-01 (#101): pre-urgency tables carry a CHECK
        # without 'urgent' — SQLite cannot ALTER a CHECK, so rebuild the
        # table once (copy-first, §XII): new schema, rows copied with
        # medium→normal, indexes recreated. Idempotent: the rebuilt
        # table's sql contains 'urgent', so this never runs twice.
        master = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='project_backlog'",
        ).fetchone()
        if master and "'urgent'" not in (master[0] or ""):
            old_cols = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(project_backlog)",
                ).fetchall()
            ]
            # #242: the whole rebuild runs atomically — the old shape
            # (executescript for CREATE, then DROP;RENAME in a second
            # executescript, each statement autocommitting) had a kill
            # window between DROP and RENAME that ate the table on
            # 2026-07-04. atomic_rebuild = ONE BEGIN IMMEDIATE…COMMIT.
            from .store_migrations import atomic_rebuild

            new_table_sql = """
                CREATE TABLE project_backlog__new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN (
                            'open','in_progress','done','blocked','rejected','removed'
                        )),
                    priority TEXT NOT NULL DEFAULT 'normal'
                        CHECK (priority IN (
                            'critical','urgent','high','normal','low','idea'
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
                    removed_reason TEXT,
                    title TEXT NOT NULL DEFAULT ''
                )
            """
            new_cols = {
                "id", "content", "status", "priority", "tags_json",
                "created_in_session_id", "source_task_id", "promoted_from_todo_id",
                "linked_task_id", "created_at", "updated_at", "completed_at",
                "removed_at", "removed_reason", "title",
            }
            common = [c for c in old_cols if c in new_cols and c != "priority"]
            col_list = ", ".join(common)
            atomic_rebuild(
                conn,
                [
                    new_table_sql,
                    f"INSERT INTO project_backlog__new ({col_list}, priority) "
                    f"SELECT {col_list}, "
                    f"CASE priority WHEN 'medium' THEN 'normal' ELSE priority END "
                    f"FROM project_backlog",
                    "DROP TABLE project_backlog",
                    "ALTER TABLE project_backlog__new RENAME TO project_backlog",
                    "CREATE INDEX IF NOT EXISTS idx_project_backlog_status "
                    "ON project_backlog(status, priority)",
                    "CREATE INDEX IF NOT EXISTS idx_project_backlog_linked "
                    "ON project_backlog(linked_task_id)",
                ],
                guard_views=["canonical_rows"],
            )
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
        # Migration 2026-07-07: global_id (uuid) — the STABLE cross-agent entity
        # id for event-sourced sync. The local autoincrement 'id' collides across
        # agent clones (a fresh gate clone restarts the counter), so events key on
        # global_id. Backfilled for existing rows. Idempotent.
        if "global_id" not in cols:
            conn.execute("ALTER TABLE project_backlog ADD COLUMN global_id TEXT NOT NULL DEFAULT ''")
            conn.row_factory = sqlite3.Row
            for _r in conn.execute("SELECT id FROM project_backlog WHERE global_id = ''").fetchall():
                conn.execute(
                    "UPDATE project_backlog SET global_id = ? WHERE id = ?",
                    (uuid.uuid4().hex, _r["id"]),
                )
            conn.row_factory = None
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_backlog_global_id "
                "ON project_backlog(global_id)"
            )
        # Migration 2026-07-18 (#450): 'merged' status + merged_into column.
        # SQLite cannot ALTER a CHECK, so tables whose CHECK predates 'merged'
        # are rebuilt once (atomic, copy-first — §XII/#242), PRESERVING every
        # live column (title + global_id included — this runs AFTER their
        # backfills so the copy carries them). Idempotent: the rebuilt table's
        # sql contains 'merged', so this never runs twice.
        master_m = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='project_backlog'",
        ).fetchone()
        if master_m and "'merged'" not in (master_m[0] or ""):
            from .store_migrations import atomic_rebuild

            old_cols_m = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(project_backlog)",
                ).fetchall()
            ]
            merged_table_sql = """
                CREATE TABLE project_backlog__new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN (
                            'open','in_progress','done','blocked','rejected','removed','merged'
                        )),
                    priority TEXT NOT NULL DEFAULT 'normal'
                        CHECK (priority IN (
                            'critical','urgent','high','normal','low','idea'
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
                    removed_reason TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    global_id TEXT NOT NULL DEFAULT '',
                    merged_into INTEGER
                )
            """
            merged_new_cols = {
                "id", "content", "status", "priority", "tags_json",
                "created_in_session_id", "source_task_id", "promoted_from_todo_id",
                "linked_task_id", "created_at", "updated_at", "completed_at",
                "removed_at", "removed_reason", "title", "global_id", "merged_into",
            }
            common_m = [c for c in old_cols_m if c in merged_new_cols]
            col_list_m = ", ".join(common_m)
            atomic_rebuild(
                conn,
                [
                    merged_table_sql,
                    f"INSERT INTO project_backlog__new ({col_list_m}) "
                    f"SELECT {col_list_m} FROM project_backlog",
                    "DROP TABLE project_backlog",
                    "ALTER TABLE project_backlog__new RENAME TO project_backlog",
                    "CREATE INDEX IF NOT EXISTS idx_project_backlog_status "
                    "ON project_backlog(status, priority)",
                    "CREATE INDEX IF NOT EXISTS idx_project_backlog_linked "
                    "ON project_backlog(linked_task_id)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_backlog_global_id "
                    "ON project_backlog(global_id)",
                ],
                guard_views=["canonical_rows"],
            )
        # Additive guard: merged_into on tables whose CHECK already carries
        # 'merged' (fresh CREATE path) but predate the column. Cheap ALTER.
        cols_m = {
            row[1] for row in conn.execute("PRAGMA table_info(project_backlog)").fetchall()
        }
        if "merged_into" not in cols_m:
            conn.execute("ALTER TABLE project_backlog ADD COLUMN merged_into INTEGER")
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
    try:
        global_id = row["global_id"] or ""
    except (KeyError, IndexError):
        global_id = ""
    try:
        merged_into = row["merged_into"]
    except (KeyError, IndexError):
        merged_into = None
    return {
        "merged_into": merged_into,
        "id": row["id"],
        "global_id": global_id,
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


def _emit_backlog(project_root: Path, global_id: str, op: str, fields: dict, *, session_id: str = "") -> None:
    """Store-layer emit for the backlog stream (Phase 1). Best-effort; never
    raises. The event log is the source of truth — EVERY backlog write path feeds
    it here, so tools/dashboard/migrations/imports are all covered by law."""
    try:
        from . import sync_store

        sync_store.emit(project_root, "backlog", global_id, op, fields, session_id=session_id)
    except Exception:
        pass


def add(
    project_root: Path,
    *,
    content: str,
    priority: str = "normal",
    tags: list[str] | None = None,
    created_in_session_id: str | None = None,
    source_task_id: str | None = None,
) -> dict[str, Any]:
    priority = _canon_priority(priority)
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
        gid = uuid.uuid4().hex
        cur = conn.execute(
            "INSERT INTO project_backlog "
            "(content, title, status, priority, tags_json, "
            " created_in_session_id, source_task_id, "
            " created_at, updated_at, global_id) "
            "VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
            (content, title, priority, tags_json, created_in_session_id, source_task_id, now, now, gid),
        )
        conn.commit()
        new_id = cur.lastrowid
    _emit_backlog(
        project_root, gid, "add",
        {
            "content": content, "title": title, "status": "open", "priority": priority,
            "tags": tags or [], "created_at": now, "updated_at": now,
            "created_in_session_id": created_in_session_id, "source_task_id": source_task_id,
        },
        session_id=created_in_session_id or "",
    )
    return {
        "ok": True,
        "id": new_id,
        "global_id": gid,
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
    tags: list[str] | None = None,
    include_removed: bool = False,
    include_merged: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    init_db(project_root)
    try:
        from . import sync_store

        sync_store.maybe_hydrate(project_root, "backlog", hydrate_from_events)  # fold-on-read
    except Exception:
        pass
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    else:
        # Default view hides tombstoned (removed) and absorbed (merged, #450)
        # rows; each returns via its explicit include_* flag or a direct
        # status= filter.
        hidden: list[str] = []
        if not include_removed:
            hidden.append("removed")
        if not include_merged:
            hidden.append("merged")
        if hidden:
            clauses.append(
                "status NOT IN (" + ", ".join(["?"] * len(hidden)) + ")"
            )
            params.extend(hidden)
    if priority is not None:
        clauses.append("priority = ?")
        params.append(_canon_priority(priority))
    if tag_filter:
        clauses.append("tags_json LIKE ?")
        params.append(f'%"{tag_filter}"%')
    # tags=[...] — any-of intersection: keep rows whose tag set carries at
    # least one requested tag (tags=[x] ≡ tag_filter=x). Blank entries are
    # noise, not a match-nothing filter. The JSON-quoted LIKE anchor
    # ('%"tag"%') matches whole stored tags only ('bug' never hits 'debug').
    wanted_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    if wanted_tags:
        clauses.append("(" + " OR ".join(["tags_json LIKE ?"] * len(wanted_tags)) + ")")
        params.extend(f'%"{t}"%' for t in wanted_tags)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Priority ordering: critical > urgent > high > normal > low > idea.
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
            f"    WHEN 'urgent' THEN 1 "
            f"    WHEN 'high' THEN 2 "
            f"    WHEN 'normal' THEN 3 "
            f"    WHEN 'low' THEN 4 "
            f"    ELSE 5 END, "
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
    reason: str | None = None,
    allow_clear: bool = False,
) -> dict[str, Any]:
    """Patch a backlog item. STRICTLY NON-DESTRUCTIVE (#399): only fields
    explicitly passed change — content=None leaves body/title untouched,
    tags=None leaves tags untouched. An empty/whitespace content is a
    DELIBERATE clear only when allow_clear=True accompanies it; without the
    flag it is refused (silent body+title wipe was the #399 data loss).
    Reversibility (#450): setting status on a 'merged' row back to an active
    status clears merged_into.
    """
    init_db(project_root)
    if status is not None and status not in _STATUSES:
        return {"ok": False, "error": f"status {status!r} not in {sorted(_STATUSES)}"}
    if priority is not None:
        priority = _canon_priority(priority)
        if priority not in _PRIORITIES:
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
        _unmerge = (
            status is not None
            and status != "merged"
            and str(existing["status"] or "") == "merged"
        )
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status == "done":
                sets.append("completed_at = ?")
                params.append(now)
            if _unmerge:
                # #450 reversibility: leaving 'merged' clears the umbrella
                # pointer — the item stands on its own again.
                sets.append("merged_into = NULL")
        if content is not None:
            if not content.strip() and not allow_clear:
                return {
                    "ok": False,
                    "error": (
                        "content='' would CLEAR this item's body and title "
                        "(#399 non-destructive contract). Pass allow_clear=True "
                        "to clear deliberately, or omit content to leave the "
                        "body untouched."
                    ),
                }
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
            note = (reason or "").strip()
            if not note:
                return {"ok": False, "error": "no updates provided"}
            # Reason-only ANNOTATION (#314): no field changed, but a
            # non-empty reason was given. Record it — bump updated_at and
            # emit an update event carrying the reason (mirrors how remove
            # persists removed_reason) — then return ok.
            conn.execute(
                "UPDATE project_backlog SET updated_at = ? WHERE id = ?",
                (now, backlog_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM project_backlog WHERE id = ?",
                (backlog_id,),
            ).fetchone()
            gid = str(row["global_id"] if "global_id" in row.keys() else "")
            if gid:
                _emit_backlog(
                    project_root, gid, "update",
                    {"updated_at": now, "reason": note},
                )
            return {"ok": True, "annotation": True, "reason": note, **_row_to_dict(row)}
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
        gid = str(row["global_id"] if "global_id" in row.keys() else "")
    _changed: dict[str, Any] = {"updated_at": now}
    if status is not None:
        _changed["status"] = status
        if status == "done":
            _changed["completed_at"] = now
        if _unmerge:
            _changed["merged_into"] = None
    if content is not None:
        _changed["content"] = content
        _changed["title"] = _extract_title(content)
    if priority is not None:
        _changed["priority"] = priority
    if tags is not None:
        _changed["tags"] = tags
    if linked_task_id is not None:
        _changed["linked_task_id"] = linked_task_id
    if gid:
        _emit_backlog(project_root, gid, "update", _changed)
    return {"ok": True, **_row_to_dict(row)}


def _row_title(row: sqlite3.Row) -> str:
    try:
        title = str(row["title"] or "")
    except (KeyError, IndexError):
        title = ""
    return title or _extract_title(str(row["content"] or ""))


def _row_tags(row: sqlite3.Row) -> list[str]:
    try:
        parsed = json.loads(row["tags_json"] or "[]")
        if isinstance(parsed, list):
            return [str(t) for t in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def merge(
    project_root: Path,
    *,
    ids: list[int],
    umbrella_id: int | None = None,
) -> dict[str, Any]:
    """#450: merge N OPEN backlog items into one umbrella item.

    Absorbed items become status='merged' + merged_into=<umbrella id> —
    KEPT, never removed (tombstone discipline). The umbrella body appends
    an '## Absorbed' section (one '- #id title' line per absorbed item)
    and takes the tag UNION. Reversible: update(status='open') on a merged
    item clears merged_into. Every write emits the NORMAL per-row update
    event via _emit_backlog — the same sync path as any update, never a
    forked emit.

    umbrella_id defaults to the LOWEST id in `ids`. When given, it may be
    outside `ids` (absorb all listed items into it).
    """
    init_db(project_root)
    uniq: list[int] = []
    for raw in ids or []:
        val = int(raw)
        if val not in uniq:
            uniq.append(val)
    if umbrella_id is not None:
        umbrella_id = int(umbrella_id)
        if umbrella_id not in uniq:
            uniq.append(umbrella_id)
    if len(uniq) < 2:
        return {"ok": False, "error": "merge requires >= 2 distinct backlog ids"}
    target = umbrella_id if umbrella_id is not None else min(uniq)
    now = _now()
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        rows: dict[int, sqlite3.Row] = {}
        for bid in uniq:
            row = conn.execute(
                "SELECT * FROM project_backlog WHERE id = ?", (bid,)
            ).fetchone()
            if row is None:
                return {"ok": False, "error": f"backlog id={bid} not found"}
            rows[bid] = row
        umbrella = rows[target]
        if str(umbrella["status"]) in ("removed", "merged"):
            return {
                "ok": False,
                "error": (
                    f"umbrella id={target} is {umbrella['status']}; "
                    "pick an active umbrella"
                ),
            }
        absorbed_ids = [b for b in uniq if b != target]
        not_open = [
            (b, str(rows[b]["status"])) for b in absorbed_ids if str(rows[b]["status"]) != "open"
        ]
        if not_open:
            return {
                "ok": False,
                "error": f"merge absorbs OPEN items only; not open: {not_open}",
            }
        # Tag UNION — order-preserving: umbrella's tags first, then each
        # absorbed item's, first occurrence wins.
        union: list[str] = []
        for bid in (target, *absorbed_ids):
            for t in _row_tags(rows[bid]):
                if t not in union:
                    union.append(t)
        # Umbrella body: append the '## Absorbed' ledger (id + title per
        # absorbed item). Re-merges into the same umbrella extend the
        # existing section instead of duplicating the heading.
        body = str(umbrella["content"] or "").rstrip()
        absorbed_lines = "\n".join(
            f"- #{b} {_row_title(rows[b])}" for b in absorbed_ids
        )
        if "## Absorbed" in body:
            body = f"{body}\n{absorbed_lines}\n"
        else:
            body = f"{body}\n\n## Absorbed\n{absorbed_lines}\n"
        new_title = _extract_title(body)
        conn.execute(
            "UPDATE project_backlog SET content = ?, title = ?, tags_json = ?, "
            "updated_at = ? WHERE id = ?",
            (body, new_title, json.dumps(union), now, target),
        )
        for b in absorbed_ids:
            conn.execute(
                "UPDATE project_backlog SET status = 'merged', merged_into = ?, "
                "updated_at = ? WHERE id = ?",
                (target, now, b),
            )
        conn.commit()
        gid_of = {
            b: str(rows[b]["global_id"] if "global_id" in rows[b].keys() else "")
            for b in uniq
        }
    # Normal update events through the ONE store-layer emit (#450: merge is
    # a batch of ordinary updates on the wire — no forked event op).
    if gid_of.get(target):
        _emit_backlog(
            project_root, gid_of[target], "update",
            {"content": body, "title": new_title, "tags": union, "updated_at": now},
        )
    for b in absorbed_ids:
        if gid_of.get(b):
            _emit_backlog(
                project_root, gid_of[b], "update",
                {"status": "merged", "merged_into": target, "updated_at": now},
            )
    return {
        "ok": True,
        "umbrella_id": target,
        "merged_ids": absorbed_ids,
        "tags": union,
        "title": new_title,
    }


def similar_open_items(
    project_root: Path,
    *,
    tags: list[str],
    exclude_id: int | None = None,
    min_shared: int = 2,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """#450 suggestion half: OPEN items sharing >= min_shared tags with the
    given tag set. Terse [{id, title}] — advisory only, NEVER auto-merges.
    Fewer than min_shared input tags can never overlap enough → [].
    """
    wanted = {str(t).strip() for t in (tags or []) if str(t).strip()}
    if len(wanted) < min_shared:
        return []
    out: list[dict[str, Any]] = []
    for item in list_backlog(project_root, status="open", limit=500):
        if exclude_id is not None and item["id"] == exclude_id:
            continue
        if len(wanted & set(item["tags"])) >= min_shared:
            out.append({"id": item["id"], "title": item["title"]})
            if len(out) >= limit:
                break
    return out


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
        gid = str(existing["global_id"] if "global_id" in existing.keys() else "")
    if gid:
        # remove is a TOMBSTONE-via-status ('removed'), audit-preserving -> an
        # 'update' event (status=removed), NOT an 'delete' op (which would drop
        # the entity from the folded view; the backlog keeps removed items).
        _emit_backlog(
            project_root, gid, "update",
            {"status": "removed", "updated_at": now, "removed_at": now, "removed_reason": reason},
        )
    return {
        "ok": True,
        "id": backlog_id,
        "status": "removed",
        "removed_at": now,
        "removed_reason": reason,
    }


def _materialize_backlog(project_root: Path, folded: dict[str, dict]) -> None:
    """Upsert folded event-state into sqlite by global_id. This is the DERIVE
    step — it writes sqlite directly and does NOT re-emit (no mutation-path loop)."""
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        for gid, r in folded.items():
            existing = conn.execute(
                "SELECT * FROM project_backlog WHERE global_id = ?", (gid,)
            ).fetchone()
            if existing is not None:
                # #399 / generalized #376 — NON-DESTRUCTIVE MERGE. The folded
                # state is a PATCH over the live row, never a full overwrite. A
                # field ABSENT from the fold (e.g. a status-only update whose
                # add-event was quarantined by the authority split, so its
                # content/priority/tags never reached the fold) keeps its existing
                # value. A DB write may not unintentionally destroy fields it was
                # never told to change. Content is extra-guarded: an empty/
                # whitespace content never blanks a non-empty body.
                new_content = str(r.get("content") or "")
                content = new_content if new_content.strip() else str(existing["content"] or "")
                title = str(r.get("title") or _extract_title(content) or existing["title"] or "")
                status = str(r["status"]) if "status" in r.keys() else str(existing["status"] or "open")
                priority = (
                    _canon_priority(str(r["priority"]))
                    if "priority" in r.keys()
                    else str(existing["priority"] or "normal")
                )
                tags_json = (
                    json.dumps(r["tags"] or []) if "tags" in r.keys() else str(existing["tags_json"] or "[]")
                )
                updated_at = str(r.get("updated_at") or existing["updated_at"] or _now())
                removed_at = r["removed_at"] if "removed_at" in r.keys() else existing["removed_at"]
                removed_reason = (
                    r["removed_reason"] if "removed_reason" in r.keys() else existing["removed_reason"]
                )
                completed_at = (
                    r["completed_at"] if "completed_at" in r.keys() else existing["completed_at"]
                )
                merged_into = (
                    r["merged_into"] if "merged_into" in r.keys() else existing["merged_into"]
                )
                conn.execute(
                    "UPDATE project_backlog SET content=?, title=?, status=?, priority=?, "
                    "tags_json=?, updated_at=?, removed_at=?, removed_reason=?, completed_at=?, "
                    "merged_into=? "
                    "WHERE global_id=?",
                    (content, title, status, priority, tags_json, updated_at,
                     removed_at, removed_reason, completed_at, merged_into, gid),
                )
            else:
                content = str(r.get("content", ""))
                title = str(r.get("title") or _extract_title(content))
                status = str(r.get("status", "open"))
                priority = _canon_priority(str(r.get("priority", "normal")))
                tags_json = json.dumps(r.get("tags") or [])
                created_at = str(r.get("created_at") or _now())
                updated_at = str(r.get("updated_at") or created_at)
                conn.execute(
                    "INSERT INTO project_backlog (content, title, status, priority, tags_json, "
                    "created_in_session_id, source_task_id, created_at, updated_at, "
                    "removed_at, removed_reason, completed_at, merged_into, global_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (content, title, status, priority, tags_json,
                     r.get("created_in_session_id"), r.get("source_task_id"),
                     created_at, updated_at, r.get("removed_at"), r.get("removed_reason"),
                     r.get("completed_at"), r.get("merged_into"), gid),
                )
        conn.commit()


def hydrate_from_events(project_root: Path) -> int:
    """Phase 2/3: fold the backlog event log into sqlite (materialized cache).

    AUTHORITY (#376, 2026-07-13): folds ONLY events that carry a local
    authoritative receipt (produced by an authenticated write on THIS gate).
    Incoming events that merely appeared in the events dir — a fresh clone's
    foreign files or a FORGED file with a self-asserted actor + max HLC — have
    no receipt and are QUARANTINED (recorded to a clear-status log, never
    applied), so a received file can never inject a phantom row or overwrite an
    existing one. Idempotent; does NOT re-emit. Returns entities materialized."""
    init_db(project_root)
    from . import sync_store

    all_events = sync_store.GitEventTransport(project_root).read("backlog")
    authoritative, incoming = sync_store.split_by_authority(
        project_root, "backlog", all_events
    )
    if incoming:
        sync_store.record_quarantine(project_root, "backlog", incoming)
    folded = sync_store.fold_events(authoritative)
    _materialize_backlog(project_root, folded)
    return len(folded)


def rebuild_from_events(project_root: Path, *, adopt_incoming: bool = False) -> int:
    """Phase 3: rebuild sqlite from the AUTHORITATIVE event log (sqlite =
    derived). Clears the table then materializes the receipted fold — the repair
    path for stale/suspicious sqlite. By default rebuilds ONLY this gate's own
    canonical history (receipted events); an unreceipted (incoming/forged) file
    can neither survive nor be introduced by a rebuild.

    OPERATOR-APPROVED SNAPSHOT RECOVERY (#376): ``adopt_incoming=True`` first
    adopts EVERY current event file as authoritative (the operator declares the
    present event log to be truth — the disaster-recovery / fresh-clone
    bootstrap), then rebuilds. This is the explicit, operator-gated path for
    importing prior history; it is NEVER reached on the fold-on-read path, which
    stays receipted-only so a received file can never auto-mutate canonical
    state."""
    init_db(project_root)
    from . import sync_store

    if adopt_incoming:
        sync_store.adopt_events_as_authoritative(project_root, "backlog", None)
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.execute("DELETE FROM project_backlog")
        conn.commit()
    return hydrate_from_events(project_root)


def seed_events_from_sqlite(project_root: Path) -> int:
    """One-time backfill: emit an 'add' event carrying the CURRENT state of every
    backlog row that has NO event yet, so rows created before store-layer emit
    existed enter the canonical log. IDEMPOTENT — rows whose global_id is already
    an event entity are skipped, so re-running is a no-op. Includes tombstoned
    (removed) rows so the log reflects full reality. Returns rows seeded.
    """
    init_db(project_root)
    from . import sync_store

    seen = {e.entity_id for e in sync_store.GitEventTransport(project_root).read("backlog")}
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM project_backlog").fetchall()
    seeded = 0
    for row in rows:
        gid = str(row["global_id"] or "")
        if not gid or gid in seen:
            continue
        d = _row_to_dict(row)
        _emit_backlog(
            project_root, gid, "add",
            {
                "content": d["content"], "title": d["title"], "status": d["status"],
                "priority": d["priority"], "tags": d["tags"],
                "created_at": d["created_at"], "updated_at": d["updated_at"],
                "created_in_session_id": d["created_in_session_id"],
                "source_task_id": d["source_task_id"],
                "removed_at": d["removed_at"], "removed_reason": d["removed_reason"],
                "completed_at": d["completed_at"],
            },
            session_id=d["created_in_session_id"] or "",
        )
        seeded += 1
    return seeded
