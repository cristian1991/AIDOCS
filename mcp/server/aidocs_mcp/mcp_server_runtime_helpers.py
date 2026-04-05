from __future__ import annotations

from pathlib import Path
from typing import Any


def registered_tools(server: Any) -> list[Any]:
    components = getattr(getattr(server, "_local_provider", None), "_components", {})
    return [component for key, component in components.items() if str(key).startswith("tool:")]


def project_root_from_args(arguments: dict[str, Any] | None) -> Path | None:
    if not isinstance(arguments, dict):
        return None
    # Support both 'root' (new) and 'project_root' (legacy)
    raw = arguments.get("root") or arguments.get("project_root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw)


_last_known_project_root: Path | None = None


def set_default_project_root(root: Path) -> None:
    """Called when managed mode activates to set the session default."""
    global _last_known_project_root
    _last_known_project_root = root


def resolve_project_root(root: str | None) -> Path:
    """Resolve root param — uses default if not provided."""
    if root and root.strip():
        return Path(root)
    if _last_known_project_root is not None:
        return _last_known_project_root
    raise ValueError("root parameter required — no default project root available")


def capture_enabled(name: str, arguments: dict[str, Any] | None) -> bool:
    if name in {"execution_run_record", "execution_event_record"}:
        return False
    return project_root_from_args(arguments) is not None


def summarize_tool_result(result: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "result_type": type(result).__name__,
    }
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        summary["structured_keys"] = sorted(str(key) for key in structured.keys())[:10]
        result_value = structured.get("result")
        if isinstance(result_value, list):
            summary["result_length"] = len(result_value)
        elif isinstance(result_value, dict):
            summary["result_length"] = len(result_value)
        elif result_value is not None:
            summary["result_scalar_type"] = type(result_value).__name__
    content = getattr(result, "content", None)
    if isinstance(content, list):
        summary["content_items"] = len(content)
        summary["content_types"] = [type(item).__name__ for item in content[:5]]
    return summary


def all_capabilities(hub: Any, project_root: Path) -> list[dict[str, Any]]:
    return hub.capabilities.find_capabilities(project_root, query=None, limit=1000)


def all_procedures(hub: Any, project_root: Path) -> list[dict[str, Any]]:
    return hub.procedures.find_procedures(project_root, query=None, limit=1000)


def resolve_related_root(hub: Any, root: str | Path, name: str) -> Path:
    resolved = hub.related.resolve_related_project_path(Path(root), name)
    if resolved is None:
        raise FileNotFoundError(
            f"Related project '{name}' is not configured or its path does not exist."
        )
    return resolved
