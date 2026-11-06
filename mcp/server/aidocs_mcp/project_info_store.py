from __future__ import annotations

from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class ProjectInfoStore(SQLiteIndexStoreBase):
    """Project-scoped metadata (title, first-seen, last-seen, version).

    Single row per project_root. Lives in the big-boss ``aidocs.sqlite3``
    alongside existing index tables so cross-tool lookups stay one DB
    open away. Write path is idempotent UPSERT — repeated records from
    the middleware must not duplicate rows or reset ``created_at``.
    """

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_info (
                    root TEXT PRIMARY KEY,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    aidocs_version TEXT
                );
                """,
            )

    def record(
        self,
        project_root: Path,
        *,
        title: str | None = None,
        aidocs_version: str | None = None,
    ) -> None:
        now = self._timestamp()
        root_key = str(project_root.resolve())
        effective_title = (title or "").strip() or project_root.name
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO project_info (root, title, created_at, last_seen_at, aidocs_version)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(root) DO UPDATE SET
                    title = excluded.title,
                    last_seen_at = excluded.last_seen_at,
                    aidocs_version = COALESCE(excluded.aidocs_version, project_info.aidocs_version)
                """,
                (root_key, effective_title, now, now, aidocs_version),
            )

    def get(self, project_root: Path) -> dict[str, Any] | None:
        root_key = str(project_root.resolve())
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT root, title, created_at, last_seen_at, aidocs_version "
                "FROM project_info WHERE root = ?",
                (root_key,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)
