from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProjectRegistryService:
    """Tracks AIDOCS-enabled projects touched by MCP calls across workspaces."""

    def _registry_path(self) -> Path:
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "AIDOCS" / "project-registry.json"

        xdg_config = os.getenv("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "aidocs" / "project-registry.json"

        return Path.home() / ".config" / "aidocs" / "project-registry.json"

    def _load(self) -> list[dict[str, Any]]:
        path = self._registry_path()
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        projects = payload.get("projects") if isinstance(payload, dict) else None
        if not isinstance(projects, list):
            return []
        return [item for item in projects if isinstance(item, dict)]

    def _write(self, projects: list[dict[str, Any]]) -> None:
        path = self._registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"projects": projects}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def record_project(
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

        # Only require .MEMORY/ — aidocs.toml is optional (only the AIDOCS source project has it)
        if not root.joinpath(".MEMORY").is_dir():
            return

        key = str(root)
        existing = self._load()
        kept: list[dict[str, Any]] = []
        prior: dict[str, Any] | None = None
        for item in existing:
            if str(item.get("project_root") or "") == key:
                prior = item
                continue
            kept.append(item)

        label = (
            (title or "").strip()
            or str((prior or {}).get("title") or "").strip()
            or root.name
            or "AIDOCS Project"
        )
        kept.append(
            {
                "project_root": key,
                "title": label,
                "managed_session_id": managed_session_id,
                "last_seen_at": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        kept.sort(key=lambda item: str(item.get("last_seen_at") or ""), reverse=True)
        self._write(kept[:200])

    def list_projects(self) -> list[dict[str, Any]]:
        return self._load()
