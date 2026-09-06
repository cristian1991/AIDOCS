from __future__ import annotations

from pathlib import Path
from typing import Any

from .known_projects_store import KnownProjectsStore


class ProjectRegistryService:
    """Tracks AIDOCS-enabled projects touched by MCP calls across workspaces.

    Storage moved to install-wide sqlite (KnownProjectsStore) in Beat 4.
    Service keeps its original public shape so the few callers
    (mcp_server middleware, cross_project_ops, cli, dashboard tool)
    don't have to change. The legacy
    ``%APPDATA%/AIDOCS/project-registry.json`` is ingested + hard-deleted
    on first init_db() touch.
    """

    def __init__(self) -> None:
        self._store = KnownProjectsStore()

    def _registry_path(self) -> Path:
        # Legacy path kept advisory so diagnostics that show "registry
        # file location" still display something sensible. Actual
        # storage lives in the install-wide sqlite post-Beat-4.
        return self._store.legacy_json_path()

    def record_project(
        self,
        project_root: Path,
        *,
        managed_session_id: str | None = None,
        title: str | None = None,
    ) -> None:
        # The store handles legacy-JSON ingest on init and enforces the
        # AIDOCS marker guard at record() — pollution from buggy
        # subdir mkdirs never reaches the registry post-Beat-1+4.
        self._store.init_db()
        self._store.record(
            project_root,
            managed_session_id=managed_session_id,
            title=title,
        )

    def list_projects(self) -> list[dict[str, Any]]:
        self._store.init_db()
        return self._store.list_projects()

    def prune_stale(self) -> int:
        # Stale = entry whose project_root no longer carries the AIDOCS
        # marker (project deleted, moved, or had its .MEMORY/.aidocs
        # tree removed by the operator).
        self._store.init_db()
        return self._store.prune_stale()
