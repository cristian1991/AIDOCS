from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class ManagedModeService:
    """Project-local managed-mode state for normal prompt routing."""

    def config_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "aidocs-managed.json"

    def get_mode(self, project_root: Path) -> dict[str, object]:
        path = self.config_path(project_root)
        if not path.is_file():
            return {
                "active": False,
                "path": str(path),
                "session_id": None,
                "activated_at": None,
                "last_updated": None,
                "source": None,
            }
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("active", False)
        data.setdefault("path", str(path))
        return data

    def set_mode(self, project_root: Path, session_id: str, source: str = "/aidocs") -> dict[str, object]:
        path = self.config_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = self._timestamp()
        current = self.get_mode(project_root)
        payload = {
            "active": True,
            "path": str(path),
            "session_id": session_id,
            "activated_at": current.get("activated_at") or now,
            "last_updated": now,
            "source": source,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Set default project root so tools can omit the root parameter
        from .mcp_server_runtime_helpers import set_default_project_root
        set_default_project_root(project_root)
        return payload

    def clear_mode(self, project_root: Path) -> dict[str, object]:
        path = self.config_path(project_root)
        if path.exists():
            path.unlink()
        return {
            "active": False,
            "path": str(path),
            "session_id": None,
            "activated_at": None,
            "last_updated": None,
            "source": None,
        }

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
