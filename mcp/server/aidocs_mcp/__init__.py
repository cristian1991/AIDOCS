"""AIDOCS MCP file-backed services."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path


def _version_from_pyproject() -> str:
    try:
        import tomllib
    except ModuleNotFoundError:
        return "0.0.0"

    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        version = project.get("version")
        return str(version) if version else "0.0.0"
    except Exception:
        return "0.0.0"


def get_version() -> str:
    version = _version_from_pyproject()
    if version != "0.0.0":
        return version
    try:
        return metadata.version("aidocs-mcp")
    except metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = get_version()
