from __future__ import annotations

from pathlib import Path
from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root


def register_memory_index_tools(
    *,
    server: Any,
    hub: Any,
    timed_sync: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def memory_read(targets: list[str], root: str = "") -> dict[str, str]:
        """Read canonical memory files by target path."""
        return hub.memory.read_memory(resolve_project_root(root), targets)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Memory Index",
        }
    )
    def index_sync(root: str = "") -> dict[str, int]:
        """Rebuild the derived SQLite memory/session index from files."""
        return hub.index.sync_all(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Memory Index Status",
        }
    )
    def index_status(root: str = "") -> dict[str, Any]:
        """Report current derived index status for the project."""
        return hub.index.status(resolve_project_root(root))

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Schema Index",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def schema_index_sync(timeout: int | None = None, root: str = "") -> dict[str, int]:
        """Rebuild the derived schema catalog from code and SQL files."""
        return hub.schema.sync_schema(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Search Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def memory_search(query: str, limit: int = 10, root: str = "") -> list[dict[str, str]]:
        """Search the derived memory index by path, title, or body text."""
        return hub.index.search_memory(resolve_project_root(root), query=query, limit=limit)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Code Index",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def code_index_sync(
        include_tests: bool = False, timeout: int | None = None, root: str = ""
    ) -> dict[str, int]:
        """Rebuild the derived code file manifest and summary index."""
        from .language_descriptors import validate_language_descriptors
        project_root = resolve_project_root(root)
        validation = validate_language_descriptors(project_root)
        descriptor_issues = validation.get("issues", [])
        result: dict[str, object] = {
            "code_files": hub.code.sync_code_files(project_root, include_tests=include_tests),
            "modules": hub.code.sync_modules(project_root),
        }
        if descriptor_issues:
            result["descriptor_issues"] = descriptor_issues
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Modules",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def code_get_modules(kind: str | None = None, root: str = "") -> list[dict[str, Any]]:
        """List detected project modules (workspaces, subprojects, informal modules)."""
        return hub.code.get_modules(resolve_project_root(root), kind=kind)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Module Files",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def code_get_module_files(
        module_path: str, limit: int = 200, root: str = ""
    ) -> list[dict[str, Any]]:
        """List all indexed source files belonging to a specific module."""
        return hub.code.get_module_files(resolve_project_root(root), module_path=module_path, limit=limit)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Index Status",
        }
    )
    def code_index_status(root: str = "") -> dict[str, Any]:
        """Report current derived code index status for the project."""
        return hub.code.code_status(resolve_project_root(root))
