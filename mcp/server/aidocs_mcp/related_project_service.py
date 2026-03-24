from __future__ import annotations

from pathlib import Path


class RelatedProjectService:
    """Read related-project investigation config from project memory."""

    def config_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "related-projects.md"

    def list_related_projects(self, project_root: Path) -> list[dict[str, str]]:
        path = self.config_path(project_root)
        if not path.is_file():
            return []

        entries: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("## "):
                if current:
                    entries.append(current)
                current = {"name": line[3:].strip()}
                continue
            if current is None or not line.startswith("-") or ":" not in line:
                continue
            body = line[1:].strip()
            key, value = body.split(":", 1)
            current[key.strip().lower().replace(" ", "_")] = value.strip().strip('`')
        if current:
            entries.append(current)
        return entries

    def get_related_project(self, project_root: Path, name: str) -> dict[str, str] | None:
        needle = name.strip().lower()
        for entry in self.list_related_projects(project_root):
            if entry.get("name", "").strip().lower() == needle:
                return entry
        return None

    def resolve_related_project_path(self, project_root: Path, name: str) -> Path | None:
        entry = self.get_related_project(project_root, name)
        if not entry:
            return None
        raw = entry.get("path")
        if not raw:
            return None
        candidate = Path(raw)
        return candidate if candidate.exists() else None
