from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

_GRANT_TTL_MINUTES = 30


class QueryGateStore:
    """Session-scoped gate tracking whether indexed-query tools were used recently."""

    _KEEP = object()

    @staticmethod
    def _normalize_known_exact_paths(payload: dict[str, object]) -> list[str]:
        paths = payload.get("known_exact_paths")
        if isinstance(paths, list):
            return [str(item) for item in paths if isinstance(item, str) and item]
        last_tool = payload.get("last_tool")
        if isinstance(last_tool, str) and last_tool.startswith("known_exact_path:"):
            _, _, granted_path = last_tool.partition(":")
            _, _, granted_path = granted_path.partition(":")
            return [granted_path] if granted_path else []
        return []

    @staticmethod
    def _normalize_lane_exact_paths(payload: dict[str, object]) -> list[str]:
        paths = payload.get("lane_exact_paths")
        if isinstance(paths, list):
            return [str(item) for item in paths if isinstance(item, str) and item]
        return []

    @staticmethod
    def _normalize_current_lane_id(payload: dict[str, object]) -> str | None:
        lane_id = payload.get("current_lane_id")
        if isinstance(lane_id, str) and lane_id.strip():
            return lane_id.strip()
        return None

    def gate_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "sessions" / session_id / "query-gate.json"

    def get(self, project_root: Path, session_id: str) -> dict[str, object]:
        path = self.gate_path(project_root, session_id)
        if not path.is_file():
            return {
                "session_id": session_id,
                "path": str(path),
                "allow_read": False,
                "last_tool": None,
                "known_exact_paths": [],
                "current_lane_id": None,
                "lane_exact_paths": [],
                "updated_at": None,
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}

        allow_read = bool(payload.get("allow_read", False))
        granted_at = payload.get("granted_at")
        if allow_read and granted_at:
            try:
                granted_time = datetime.strptime(granted_at, "%Y-%m-%d %H:%M:%S")
                if datetime.now() - granted_time > timedelta(
                    minutes=_GRANT_TTL_MINUTES
                ):
                    allow_read = False
            except (ValueError, TypeError):
                pass

        return {
            "session_id": session_id,
            "path": str(path),
            "allow_read": allow_read,
            "last_tool": payload.get("last_tool"),
            "known_exact_paths": self._normalize_known_exact_paths(payload),
            "current_lane_id": self._normalize_current_lane_id(payload),
            "lane_exact_paths": self._normalize_lane_exact_paths(payload),
            "updated_at": payload.get("updated_at"),
        }

    def set(
        self,
        project_root: Path,
        session_id: str,
        allow_read: bool,
        last_tool: str | None,
        known_exact_paths: list[str] | None | object = _KEEP,
        current_lane_id: str | None | object = _KEEP,
        lane_exact_paths: list[str] | None | object = _KEEP,
    ) -> dict[str, object]:
        path = self.gate_path(project_root, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = self.get(project_root, session_id)
        now = datetime.now()
        payload = {
            "allow_read": allow_read,
            "last_tool": last_tool,
            "granted_at": now.strftime("%Y-%m-%d %H:%M:%S") if allow_read else None,
            "known_exact_paths": list(
                dict.fromkeys(
                    current["known_exact_paths"]
                    if known_exact_paths is self._KEEP
                    else (known_exact_paths or [])
                )
            ),
            "current_lane_id": current.get("current_lane_id")
            if current_lane_id is self._KEEP
            else (str(current_lane_id).strip() if current_lane_id else None),
            "lane_exact_paths": list(
                dict.fromkeys(
                    current.get("lane_exact_paths", [])
                    if lane_exact_paths is self._KEEP
                    else (lane_exact_paths or [])
                )
            ),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return self.get(project_root, session_id)
