"""Global LAW store — the empire's two-tier memory.

Memory is two-tier (memory law-tier war, #213):
  - LOCAL  : per-project memory (memory_index in <project>/.MEMORY/.index)
  - GLOBAL : empire LAW (here) — rules/security/workflows sealed ONCE and read
             in EVERY project, instead of being seed-copied into each project's
             memory_index where they drift.

Global law lives in the global empire DB (~/.aidocs/empire.sqlite3), beside the
sovereign souls + empire skills, NOT in any project tree. The table is ensured
on every open (same durability discipline as empire_skills). This module is the
STORE only — WHO may write global law (RBAC + sovereign promotion) is Lane 3;
HOW it merges into discovery is Lane 2; this is the canonical ledger.

Retirement is soft (status='retired'): law is never hard-deleted, so a wrong
seal can be rolled back and the ledger keeps the full history.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

# #755: canonical connect — WAL, synchronous=NORMAL, busy_timeout,
# foreign_keys=ON. row_factory stays ROW (the helper's default): this store
# already read by name, so the hand-set line it replaces is redundant.
from ._sqlite_connect import connect as _canonical_connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS global_law (
    law_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    content         TEXT NOT NULL,
    keywords        TEXT NOT NULL DEFAULT '',
    sovereign_owner TEXT,
    source          TEXT NOT NULL DEFAULT 'manual',
    checksum        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_ACTIVE = "active"
_RETIRED = "retired"


def _empire_db() -> Path:
    """The global empire DB. Honors AIDOCS_EMPIRE_DB so tests isolate it the
    same way the skill/soul store does — never touch the operator's real one."""
    override = os.environ.get("AIDOCS_EMPIRE_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "empire.sqlite3"


def _conn() -> sqlite3.Connection:
    db = _empire_db()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = _canonical_connect(db)
    conn.execute(_SCHEMA)  # ensure-on-open: the table never silently vanishes
    return conn


def _checksum(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


def _row(r: sqlite3.Row) -> dict:
    return {
        "law_id": str(r["law_id"]),
        "kind": str(r["kind"]),
        "content": str(r["content"]),
        "keywords": str(r["keywords"] or ""),
        "sovereign_owner": r["sovereign_owner"],
        "source": str(r["source"]),
        "checksum": str(r["checksum"]),
        "status": str(r["status"]),
        "created_at": str(r["created_at"]),
        "updated_at": str(r["updated_at"]),
    }


def upsert_global_law(
    *,
    law_id: str,
    kind: str,
    content: str,
    keywords: str = "",
    sovereign_owner: str | None = None,
    source: str = "manual",
) -> dict:
    """Insert or update one global law row (re-activates a retired one).
    The STORE does not gate authority — Lane 3 (RBAC + promotion) does."""
    lid = (law_id or "").strip()
    if not lid:
        raise ValueError("law_id is required")
    if not (kind or "").strip():
        raise ValueError("kind is required")
    if not (content or "").strip():
        raise ValueError("content must be non-empty")
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO global_law
                (law_id, kind, content, keywords, sovereign_owner, source,
                 checksum, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            ON CONFLICT(law_id) DO UPDATE SET
                kind = excluded.kind,
                content = excluded.content,
                keywords = excluded.keywords,
                sovereign_owner = excluded.sovereign_owner,
                source = excluded.source,
                checksum = excluded.checksum,
                status = 'active',
                updated_at = CURRENT_TIMESTAMP
            """,
            (lid, kind, content, keywords, sovereign_owner, source, _checksum(content)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"law_id": lid, "kind": kind, "bytes": len(content), "checksum": _checksum(content)}


def read_global_law(law_id: str, *, include_retired: bool = False) -> dict | None:
    """Read one global law by id. Retired rows are hidden unless asked for."""
    lid = (law_id or "").strip()
    if not lid:
        return None
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM global_law WHERE law_id = ?", (lid,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if not include_retired and str(row["status"]) != _ACTIVE:
        return None
    return _row(row)


def list_active_global_law(*, kind: str | None = None) -> list[dict]:
    """All ACTIVE global law (optionally by kind), oldest-first."""
    conn = _conn()
    try:
        if kind:
            rows = conn.execute(
                "SELECT * FROM global_law WHERE status='active' AND kind=? "
                "ORDER BY created_at",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM global_law WHERE status='active' ORDER BY created_at",
            ).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def retire_global_law(law_id: str) -> bool:
    """Soft-retire (status='retired'). Law is never hard-deleted — a wrong seal
    is rolled back, and the full history survives. Returns True if a row moved."""
    lid = (law_id or "").strip()
    if not lid:
        return False
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE global_law SET status='retired', updated_at=CURRENT_TIMESTAMP "
            "WHERE law_id=? AND status='active'",
            (lid,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
