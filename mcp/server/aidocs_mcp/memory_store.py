from __future__ import annotations

from pathlib import Path

from .types import MemoryWriteResult


class MemoryStore:
    """File-backed canonical memory reader/writer."""

    def memory_root(self, project_root: Path) -> Path:
        return project_root / ".MEMORY"

    def read_memory(self, project_root: Path, targets: list[str]) -> dict[str, str]:
        root = self.memory_root(project_root)
        result: dict[str, str] = {}
        for target in targets:
            rel = target.replace("/.MEMORY/", "").lstrip("/")
            path = root / rel
            if path.exists() and path.is_file():
                result[target] = path.read_text(encoding="utf-8")
        return result

    def capture_memory(
        self,
        project_root: Path,
        kind: str,
        content: str,
        target_hint: str | None = None,
    ) -> MemoryWriteResult:
        root = self.memory_root(project_root)
        target = self._resolve_target(root, kind, content, target_hint)
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        new_content = self._append_bullet(existing, content)
        target.write_text(new_content, encoding="utf-8")
        return MemoryWriteResult(target_file=target, content=content)

    def _resolve_target(self, root: Path, kind: str, content: str, target_hint: str | None) -> Path:
        mapping = {
            "rule": root / "rules" / "workflow-rules.md",
            "feedback": root / "rules" / "workflow-rules.md",
            "system": root / "system" / "architecture.md",
            "config": root / "config" / "personality.md",
            "domain": root / "domains" / "general.md",
            "project": root / "domains" / "project-state.md",
            "user": root / "domains" / "user-profile.md",
            "reference": root / "domains" / "references.md",
            "related_project": root / "related-projects" / "FIXES_BY_OTHER_AGENTS.md",
        }

        kind_folders = {
            "rule": "rules",
            "feedback": "rules",
            "system": "system",
            "config": "config",
            "domain": "domains",
            "project": "domains",
            "user": "domains",
            "reference": "domains",
            "related_project": "related-projects",
        }

        if target_hint:
            rel = target_hint.replace("/.MEMORY/", "").lstrip("/")
            normalized = rel.replace("\\", "/").strip()
            if "/" not in normalized:
                # Bare filename — route to the appropriate folder for this kind
                folder = kind_folders.get(kind, "domains")
                filename = normalized if normalized.endswith(".md") else f"{normalized}.md"
                return root / folder / filename
            candidate = Path(normalized)
            if candidate.suffix == "":
                candidate = candidate.with_suffix(".md")
            return root / candidate

        inferred = self._infer_target_from_content(root, kind, content=content)
        if inferred is not None:
            return inferred

        return mapping.get(kind, root / "domains" / "general.md")

    def _infer_target_from_content(self, root: Path, kind: str, content: str | None) -> Path | None:
        text = (content or "").strip().lower()
        if kind in {"rule", "feedback"}:
            routes = self._get_memory_routes(root)
            for route in routes:
                if any(tok in text for tok in route["tokens"]):
                    return root / route["target"]
            return root / "rules" / "workflow-rules.md"
        if kind == "project":
            return root / "domains" / "project-state.md"
        if kind == "user":
            return root / "domains" / "user-profile.md"
        if kind == "reference":
            return root / "domains" / "references.md"
        if kind != "domain" or not text:
            return None

        # Domain kind — check workflow tokens from config
        routes = self._get_memory_routes(root)
        workflow_route = next(
            (r for r in routes if r["target"].endswith("workflow-rules.md")),
            None,
        )
        if workflow_route and any(tok in text for tok in workflow_route["tokens"]):
            return root / workflow_route["target"]

        return root / "domains" / "general.md"

    _memory_routes_cache: list[dict[str, object]] | None = None

    def _get_memory_routes(self, root: Path) -> list[dict[str, object]]:
        if self._memory_routes_cache is not None:
            return self._memory_routes_cache
        from .intent_guard import load_memory_routing_config
        self._memory_routes_cache = load_memory_routing_config()
        return self._memory_routes_cache

    def _append_bullet(self, existing: str, content: str) -> str:
        normalized = existing.rstrip()
        bullet = f"- {content.strip()}"
        if not normalized:
            return bullet + "\n"
        return normalized + "\n" + bullet + "\n"
