from __future__ import annotations

from pathlib import Path

from .constants import MANAGED_MARKER


class ManagedFileService:
    """Marker-aware file rewriting for managed AIDOCS sections."""

    def has_marker(self, path: Path) -> bool:
        if not path.exists() or not path.is_file():
            return False
        return MANAGED_MARKER in path.read_text(encoding="utf-8")

    def split_managed(self, content: str) -> tuple[str, str] | None:
        if MANAGED_MARKER not in content:
            return None
        before, _, after = content.partition(MANAGED_MARKER)
        return before, after

    def read_user_section(self, path: Path) -> str:
        if not path.exists():
            return ""
        content = path.read_text(encoding="utf-8")
        split = self.split_managed(content)
        if split is None:
            return ""
        return split[1].lstrip("\r\n")

    def rewrite_managed_section(self, path: Path, managed_content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        split = self.split_managed(existing)

        managed_body = managed_content.rstrip("\r\n")
        if split is None:
            if existing.strip():
                new_content = f"{managed_body}\n\n{MANAGED_MARKER}\n\n{existing.rstrip()}\n"
            else:
                new_content = f"{managed_body}\n\n{MANAGED_MARKER}\n"
        else:
            user_section = split[1].lstrip("\r\n")
            if user_section.strip():
                new_content = f"{managed_body}\n\n{MANAGED_MARKER}\n\n{user_section.rstrip()}\n"
            else:
                new_content = f"{managed_body}\n\n{MANAGED_MARKER}\n"

        path.write_text(new_content, encoding="utf-8")
