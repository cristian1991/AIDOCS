"""AIDOCS MCP file-backed services."""

from __future__ import annotations

import sys
from pathlib import Path

# RFC-4: vendored mempalace lives under <repo>/third_party/mempalace.
# Prepend it to sys.path so ``import mempalace`` resolves to the
# bundled copy rather than any externally installed wheel. AIDOCS is
# the Empire — MemPalace is the in-repo Palace engine, not an external
# dependency. This runs at first import of aidocs_mcp, before any
# downstream code triggers ``import mempalace``.
_VENDOR_ROOT = Path(__file__).resolve().parents[3] / "third_party" / "mempalace"
if _VENDOR_ROOT.is_dir() and str(_VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ROOT))


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


def _version_from_release_manifest() -> str:
    """Read version from the signed release manifest shipped with the
    deployed package. This is the authoritative version source on the
    server, where pyproject.toml does not ship (only aidocs_mcp/ does).
    Until 2026-05-27 the footer on the live login page rendered '0.0.0'
    because pyproject was missing and the fallback below didn't know
    about the manifest — the manifest is the canonical reply now.
    """
    import json

    manifest = Path(__file__).resolve().parent / "trust" / "release_manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        v = data.get("version")
        return str(v) if v else "0.0.0"
    except Exception:
        return "0.0.0"


def get_version() -> str:
    # Dev-box source of truth: pyproject.toml (always current at build).
    version = _version_from_pyproject()
    if version != "0.0.0":
        return version
    # Server source of truth: the signed release manifest.
    version = _version_from_release_manifest()
    if version != "0.0.0":
        return version
    # Lazy: importlib.metadata costs ~100 ms to import and is only needed in this
    # rare fallback (neither pyproject nor manifest readable). Importing it at
    # module top made every claude_hook process spawn pay ~100 ms for nothing.
    # Deferred here keeps __version__ identical while removing that cost from
    # the hot hook path.
    try:
        from importlib import metadata

        return metadata.version("aidocs-mcp")
    except Exception:
        return "0.0.0"


__version__ = get_version()
