"""palace_control_store — SQLite store for palace-specific control state.

Per RFC 003 §15: `palace_disable` is a feature flag separate from
AIDOCS's former kill switch (which was a bypass, not a refuse-all;
excised by #404). When `palace_disable` is
active, only `ai_palace_*` tools and `hub.palace.*` writes refuse;
the rest of AIDOCS operates normally.

Storage: ``<project>/.MEMORY/.index/aidocs.sqlite3`` (the same DB
that backs other AIDOCS-canonical state per
``preflight_service._db_path``).

Adopt by copying to ``aidocs_mcp/palace_control_store.py``. No
modifications expected.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS palace_disable_state (
    project_root  TEXT PRIMARY KEY,
    disabled      INTEGER NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT '',
    set_by        TEXT NOT NULL DEFAULT 'operator',
    set_at        TEXT NOT NULL DEFAULT ''
);
"""


class PalaceControlStore:
    """Thin SQLite wrapper for palace_disable state.

    One row per project_root. ``is_palace_disabled(project_root)``
    returns False by default (palace enabled when no row exists).
    """

    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"

    def init_db(self, project_root: Path) -> None:
        path = self.db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def is_palace_disabled(self, project_root: Path) -> bool:
        """Return True iff a row exists with disabled=1.

        Fail-closed: any read error returns False (palace enabled).
        Operators rely on the gate to refuse when active, not on a
        broken gate to refuse when state is unreadable.
        """
        try:
            with sqlite3.connect(str(self.db_path(project_root))) as conn:
                row = conn.execute(
                    "SELECT disabled FROM palace_disable_state WHERE project_root = ?",
                    (str(project_root),),
                ).fetchone()
                return bool(row and row[0])
        except sqlite3.Error:
            return False

    def set_palace_disabled(
        self,
        project_root: Path,
        *,
        disabled: bool,
        reason: str = "",
        set_by: str = "operator",
    ) -> None:
        from datetime import datetime

        now = datetime.now(tz=UTC).isoformat()
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                """INSERT INTO palace_disable_state
                   (project_root, disabled, reason, set_by, set_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(project_root) DO UPDATE SET
                       disabled = excluded.disabled,
                       reason = excluded.reason,
                       set_by = excluded.set_by,
                       set_at = excluded.set_at""",
                (str(project_root), 1 if disabled else 0, reason, set_by, now),
            )
            conn.commit()
