"""The LOCAL project registration store -- a neutral public module.

Design decision (2026-10-29): no public module imports a gate file. A local box
binds its project root to a canonical cloud project id (local backlog 988,
written by ``local_cloud_link.link_project`` after the authority confirmed the
entitlement) and reads that binding back (``project_binding_resolver``,
``project_locator``). That is a local file fact, so its store lives here:

  * ``registry_db_path`` -- where the per-home ``gate_projects`` table lives
    (#516: per home, never the machine-global identity file);
  * ``ensure_projects_table`` -- the ONE DDL for ``gate_projects``;
  * ``link_local`` / ``get_project`` -- persist and read one row.

It is a DTO store and nothing more: no tenancy, no selection, no visibility
or allowlist decision. The gate's ``GateProjectStore`` (private) builds its
registry and selection machinery on these same primitives, so reader, writer
and gate share one answer to "where does gate_projects live".

THE CALLER MUST HAVE VERIFIED ENTITLEMENT before ``link_local``: this module
persists, it does not judge.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from ._sqlite_connect import connect as _canonical_connect


def _iso_now() -> str:
    return datetime.fromtimestamp(time.time(), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def registry_db_path(project_root: Path) -> Path:
    """#516: PER-HOME, never the machine-global identity file. gate_projects
    carries no scope columns, so pooling it into one global file listed a
    foreign org's projects to a non-member and attributed a project to the
    wrong org."""
    from .identity_db import tenant_home_db_path

    return tenant_home_db_path(project_root)


def ensure_projects_table(conn: sqlite3.Connection) -> None:
    """Create ``gate_projects`` if absent and add ``org_id`` to a pre-#283 table.

    #283 P1: org is a first-class, DERIVED property of the project. Existing
    rows default to unbound '' (never a guessed org)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gate_projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'local',
            github_url TEXT NOT NULL DEFAULT '',
            github_ref TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            org_id TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(gate_projects)").fetchall()}
    if "org_id" not in cols:
        conn.execute("ALTER TABLE gate_projects ADD COLUMN org_id TEXT NOT NULL DEFAULT ''")


def get_project(home: Path, project_id: str) -> dict | None:
    """One ``gate_projects`` row as a dict, read-only. No DB, no table or no
    row reads as None; nothing is created by asking."""
    if not project_id:
        return None
    db = registry_db_path(home)
    if not Path(db).is_file():
        return None
    try:
        with _canonical_connect(db, read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM gate_projects WHERE project_id=?", (project_id,)
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    out = dict(row)
    out.setdefault("org_id", "")  # a pre-#283 row (reads do not migrate) is unbound
    return out


def link_local(home: Path, *, root: Path, project_id: str, org_id: str, name: str = "") -> dict:
    """Durably bind a LOCAL project ROOT to a canonical cloud project id.

    IDEMPOTENT, AND KEYED ON THE ROOT. Re-linking the same root must succeed --
    a deploy, a re-run or a corrected id are all ordinary -- so any existing
    row for this root is replaced rather than colliding on the primary key.
    ONE ROOT HAS ONE PROJECT: leaving a stale row behind would make the
    reader's scan order decide the answer.
    """
    from .identity_store import IdentityStore

    IdentityStore().init_db(home)
    db = registry_db_path(home)
    db.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(Path(root).resolve())
    with _canonical_connect(db, row_factory=False) as conn:
        ensure_projects_table(conn)
        # Drop any prior row for THIS root (possibly under a different id),
        # then upsert the new one. Both statements, one transaction.
        stale = [
            str(pid)
            for pid, existing_root in conn.execute(
                "SELECT project_id, root FROM gate_projects"
            ).fetchall()
            if str(pid) != str(project_id)
            and str(Path(str(existing_root)).resolve()).casefold() == resolved.casefold()
        ]
        for pid in stale:
            conn.execute("DELETE FROM gate_projects WHERE project_id=?", (pid,))
        conn.execute(
            "INSERT OR REPLACE INTO gate_projects (project_id, name, root, source, "
            "github_url, github_ref, created_at, org_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                str(project_id),
                str(name or Path(resolved).name),
                resolved,
                "local_cloud_link",
                "",
                "",
                _iso_now(),
                str(org_id or ""),
            ),
        )
        conn.commit()
    return get_project(home, project_id) or {}
