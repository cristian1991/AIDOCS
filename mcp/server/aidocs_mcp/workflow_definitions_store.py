"""workflow_definitions — canonical SOURCE for workflow rules + actions.

No-file-layer doctrine (Stage 5c): the workflow rule / action DEFINITIONS
that ``workflow_action_service.compile_project_rules`` parses used to live
in ``.MEMORY/rules/workflow-rules.md`` + ``workflow-actions.md``. They now
live HERE, in sqlite, edited via the dashboard (workflow_definition_* MCP
tools). compile reads the active rows; the compiled output stays in the
``workflow_actions`` table (Beat 3) and the heuristic-judge scan over
compiled actions is unchanged.

Each row's ``body`` is one definition string exactly as the compiler
consumes it (a bullet body, optionally with a ``[verify:…]`` suffix) — so
swapping the source from markdown to sqlite needs no parser change.

Tombstone model: remove transitions status → 'removed' (never DELETE), so
a retired definition cannot silently resurrect and audit stays intact.
Storage: ``workflow_definitions`` table in ``.MEMORY/.index/aidocs.sqlite3``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

# #755/#756: the ONE canonical connect. Every site below was
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none of them
# set a single pragma, so this store ran with foreign_keys OFF (its FKs
# inert), no busy_timeout, and the default synchronous=FULL fsync tax.
# DURABILITY: AUDIT, i.e. the FULL this file already had. These rows are
# the canonical SOURCE of the workflow rules the gate enforces -- operator
# governance, not runtime state, and nothing re-derives them (the markdown
# they replaced is gone). A definition lost to a power cut is a rule that
# stops being enforced without anyone deciding it should, and the file's
# own tombstone model exists so "audit stays intact". Definition CRUD is a
# dashboard action, so keeping FULL costs nothing measurable.
from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

_KINDS = ("rule", "action")


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with _canonical_connect(
        db, durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_definitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('rule','action')),
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','removed')),
                ordering INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                removed_at TEXT,
                removed_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_definitions_kind_status
                ON workflow_definitions(kind, status, ordering);
        """)
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "body": row["body"],
        "status": row["status"],
        "ordering": row["ordering"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def add(
    project_root: Path,
    *,
    kind: str,
    body: str,
    source: str = "manual",
    ordering: int | None = None,
) -> dict[str, Any]:
    if kind not in _KINDS:
        return {"ok": False, "error": f"kind {kind!r} not in {list(_KINDS)}"}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "body is required"}
    init_db(project_root)
    now = _now()
    with _canonical_connect(
        _db_path(project_root), durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        if ordering is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(ordering), 0) + 1 FROM workflow_definitions WHERE kind = ?",
                (kind,),
            ).fetchone()
            ordering = int(row[0])
        cur = conn.execute(
            "INSERT INTO workflow_definitions "
            "(kind, body, status, ordering, source, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?, ?, ?)",
            (kind, body, ordering, source, now, now),
        )
        conn.commit()
        return {
            "ok": True,
            "id": cur.lastrowid,
            "kind": kind,
            "body": body,
            "status": "active",
            "ordering": ordering,
            "source": source,
        }


def list_active(
    project_root: Path,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    db = _db_path(project_root)
    if not db.is_file():
        return []
    with _canonical_connect(db, durability=_Durability.AUDIT) as conn:
        conn.row_factory = sqlite3.Row
        try:
            if kind is not None:
                rows = conn.execute(
                    "SELECT * FROM workflow_definitions "
                    "WHERE kind = ? AND status = 'active' "
                    "ORDER BY ordering, id",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM workflow_definitions WHERE status = 'active' "
                    "ORDER BY kind, ordering, id",
                ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [_row_to_dict(r) for r in rows]


def active_bodies(project_root: Path, kind: str) -> list[str]:
    """Ordered active definition strings for a kind — what compile consumes."""
    return [d["body"] for d in list_active(project_root, kind)]


def get(project_root: Path, def_id: int) -> dict[str, Any] | None:
    """Read a single definition row by id (any status), or None if absent.

    Used by the mutation service to capture the pre-mutation kind / body /
    status for audit attribution before an update or remove.
    """
    db = _db_path(project_root)
    if not db.is_file():
        return None
    with _canonical_connect(db, durability=_Durability.AUDIT) as conn:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM workflow_definitions WHERE id = ?",
                (int(def_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return _row_to_dict(row) if row is not None else None


def update(
    project_root: Path,
    *,
    def_id: int,
    body: str | None = None,
    status: str | None = None,
    ordering: int | None = None,
) -> dict[str, Any]:
    if status is not None:
        # Tombstone integrity (Stage 5c): a retirement must carry removed_at /
        # removed_reason, which only remove() sets. Allowing status='removed'
        # through the generic update would tombstone a definition without that
        # metadata, so block it here and route retirements through remove().
        if status == "removed":
            return {
                "ok": False,
                "error": "use remove() to tombstone; update cannot set status='removed'",
            }
        if status != "active":
            return {"ok": False, "error": f"bad status {status!r}"}
    db = _db_path(project_root)
    if not db.is_file():
        return {"ok": False, "error": "no store"}
    sets: list[str] = []
    params: list[Any] = []
    if body is not None:
        sets.append("body = ?")
        params.append(body.strip())
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if ordering is not None:
        sets.append("ordering = ?")
        params.append(int(ordering))
    if not sets:
        return {"ok": False, "error": "nothing to update"}
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(int(def_id))
    with _canonical_connect(
        db, durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        cur = conn.execute(
            f"UPDATE workflow_definitions SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
        return {"ok": cur.rowcount > 0, "id": def_id}


def remove(
    project_root: Path,
    *,
    def_id: int,
    reason: str = "",
) -> dict[str, Any]:
    """Tombstone: status → 'removed' (never DELETE)."""
    db = _db_path(project_root)
    if not db.is_file():
        return {"ok": False, "error": "no store"}
    now = _now()
    with _canonical_connect(
        db, durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        cur = conn.execute(
            "UPDATE workflow_definitions SET status = 'removed', "
            "updated_at = ?, removed_at = ?, removed_reason = ? "
            "WHERE id = ? AND status = 'active'",
            (now, now, reason, int(def_id)),
        )
        conn.commit()
        return {"ok": cur.rowcount > 0, "id": def_id, "status": "removed"}


def count_active(project_root: Path) -> int:
    db = _db_path(project_root)
    if not db.is_file():
        return 0
    with _canonical_connect(
        db, durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM workflow_definitions WHERE status = 'active'",
                ).fetchone()[0],
            )
        except sqlite3.OperationalError:
            return 0
