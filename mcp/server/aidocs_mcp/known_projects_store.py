from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class KnownProjectsStore:
    """Install-wide registry of AIDOCS-enabled projects touched by MCP
    calls — sqlite-backed replacement for ``project-registry.json``.

    Lives in the same install-wide DB as ConfigStore (``~/.aidocs/
    config.sqlite3`` by default, overridable via the
    ``AIDOCS_GLOBAL_CONFIG_DB`` env var). One row per project_root.
    Records are gated by the AIDOCS marker (``.MEMORY/.aidocs/index.aidocs``)
    so polluted subdirs from buggy index-store mkdirs never leak into
    the registry.

    Legacy JSON at the previous registry path is ingested + hard-deleted
    on first ``init_db()`` so the install carries one source of truth.
    """

    def _global_db_path(self) -> Path:
        # Mirrors ConfigStore._global_db_path so both stores share the
        # same install-wide DB and the same env-var test override path.
        override = os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".aidocs" / "config.sqlite3"

    def legacy_json_path(self) -> Path:
        # Pre-Beat-4 the registry lived as JSON. This method exposes the
        # legacy path so tests can stage migration scenarios; production
        # callers should use list_projects() and never touch the JSON.
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "AIDOCS" / "project-registry.json"
        xdg_config = os.getenv("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "aidocs" / "project-registry.json"
        return Path.home() / ".config" / "aidocs" / "project-registry.json"

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        # Same close-on-exit pattern as SQLiteIndexStoreBase.session() so
        # Windows WAL handles never linger across calls.
        db_path = self._global_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _has_aidocs_marker(root: Path) -> bool:
        # Anti-pollution gate: only paths whose root carries the
        # canonical AIDOCS marker get registered. Subdirs that happened
        # to acquire a stray .MEMORY/ via a buggy mkdir don't qualify.
        return (root / ".MEMORY" / ".aidocs" / "index.aidocs").is_file()

    def init_db(self) -> None:
        with self._session() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS known_projects (
                    project_root TEXT PRIMARY KEY,
                    title TEXT,
                    managed_session_id TEXT,
                    last_seen_at TEXT NOT NULL
                );
                """,
            )
        self._ingest_legacy_json()

    def _ingest_legacy_json(self) -> None:
        path = self.legacy_json_path()
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt JSON stays on disk for operator triage; rest of
            # the system carries on with an empty registry that
            # repopulates on next tool call.
            return
        projects = payload.get("projects") if isinstance(payload, dict) else None
        if not isinstance(projects, list):
            return

        ingested = 0
        with self._session() as conn:
            existing = conn.execute("SELECT 1 FROM known_projects LIMIT 1").fetchone()
            if existing is not None:
                # Migration already happened or table populated by a
                # concurrent process; just delete the legacy file.
                path.unlink()
                return
            for entry in projects:
                if not isinstance(entry, dict):
                    continue
                root_str = str(entry.get("project_root") or "").strip()
                if not root_str:
                    continue
                root_path = Path(root_str)
                # Drop pollution: only keep entries that still pass the
                # AIDOCS marker guard. The legacy JSON was written by
                # the pre-Beat-1 record_project that accepted any
                # ``.MEMORY/`` directory.
                if not self._has_aidocs_marker(root_path):
                    continue
                title = str(entry.get("title") or "").strip() or root_path.name
                managed_session_id = entry.get("managed_session_id")
                if managed_session_id is not None:
                    managed_session_id = str(managed_session_id)
                last_seen_at = str(entry.get("last_seen_at") or "").strip() or self._timestamp()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO known_projects
                        (project_root, title, managed_session_id, last_seen_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(root_path), title, managed_session_id, last_seen_at),
                )
                ingested += 1
        path.unlink()
        return

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def record(
        self,
        project_root: Path,
        *,
        managed_session_id: str | None = None,
        title: str | None = None,
    ) -> None:
        try:
            root = project_root.resolve()
        except Exception:
            root = project_root
        if not self._has_aidocs_marker(root):
            return
        key = str(root)
        now = self._timestamp()
        effective_title = (title or "").strip() or root.name or "AIDOCS Project"
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO known_projects
                    (project_root, title, managed_session_id, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_root) DO UPDATE SET
                    title = excluded.title,
                    managed_session_id = excluded.managed_session_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (key, effective_title, managed_session_id, now),
            )

    def list_projects(self) -> list[dict[str, Any]]:
        with self._session() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT project_root, title, managed_session_id, last_seen_at
                      FROM known_projects
                     ORDER BY last_seen_at DESC
                    """,
                ).fetchall()
            except sqlite3.OperationalError:
                # Table missing — treat as empty registry. Caller gets
                # the same behavior as a fresh install.
                return []
        return [dict(row) for row in rows]

    def prune_stale(self) -> int:
        rows = self.list_projects()
        stale_keys = [
            row["project_root"]
            for row in rows
            if not self._has_aidocs_marker(Path(row["project_root"]))
        ]
        if not stale_keys:
            return 0
        with self._session() as conn:
            conn.executemany(
                "DELETE FROM known_projects WHERE project_root = ?",
                [(key,) for key in stale_keys],
            )
        return len(stale_keys)
