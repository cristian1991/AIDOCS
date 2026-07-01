"""Host Policy Service - Canonical validation for all host adapters.

This module provides shared validation logic that can be called from:
- MCP (via file_ops.py)
- OpenCode plugin
- Claude Code hooks
- Codex adapters
- Any future host

No policy logic should be duplicated in host adapters.
"""

from pathlib import Path
from typing import Any


def validate_edit_syntax(
    file_path: str | Path,
    new_content: str,
) -> dict[str, Any]:
    """Validate that an edit would not introduce syntax errors.

    Args:
        file_path: Path to the file being edited
        new_content: The new content being inserted

    Returns:
        {"valid": True} if valid, {"valid": False, "error": "message"} if invalid

    """
    abs_path = Path(file_path) if isinstance(file_path, str) else file_path

    if not new_content:
        return {"valid": True}

    # Try tree-sitter first — covers all languages.
    # tree-sitter is a hard dep (#52) and check_syntax raises
    # RuntimeError when its validators can't decide (#57). Surface
    # RuntimeError as a validation refusal — fail closed per #57,
    # never silently pass when the validator is broken.
    from .tree_sitter_service import check_syntax as ts_check

    try:
        result = ts_check(abs_path, new_content)
    except RuntimeError as e:
        return {
            "valid": False,
            "error": f"Validator unavailable; refusing to fail-open: {e}",
        }
    if result is not None:
        return {"valid": False, "error": f"Syntax error: {result}"}

    # Stdlib fallbacks for formats tree-sitter doesn't cover
    suffix = abs_path.suffix.lower()

    if suffix == ".py":
        import ast

        try:
            ast.parse(new_content)
        except SyntaxError as e:
            return {
                "valid": False,
                "error": f"Python syntax error at line {e.lineno or '?'}: {e.msg}",
            }

    elif suffix == ".json":
        import json

        try:
            json.loads(new_content)
        except json.JSONDecodeError as e:
            return {"valid": False, "error": f"JSON error at line {e.lineno}: {e.msg}"}

    elif suffix == ".toml":
        # tomllib is stdlib since Python 3.11. requires-python = ">=3.11"
        # so this is always available — pre-#57 the dead `tomli` fallback
        # path silently passed every TOML edit when tomllib import failed
        # (which it can't on 3.11+). Drop the fail-open per invariant #57.
        import tomllib

        try:
            tomllib.loads(new_content)
        except Exception as e:
            return {"valid": False, "error": f"TOML error: {e}"}

    elif suffix in (".yaml", ".yml"):
        # PyYAML is a hard dep (#57, 2026-04-27). Pre-fix had
        # `except ImportError: pass` that silently passed every YAML
        # edit when PyYAML wasn't installed. Drop the fail-open path.
        import yaml

        try:
            yaml.safe_load(new_content)
        except yaml.YAMLError as e:
            return {"valid": False, "error": f"YAML error: {e}"}

    elif suffix in (".xml", ".html", ".csproj", ".resx", ".config"):
        import xml.etree.ElementTree as ET

        try:
            ET.fromstring(new_content)
        except ET.ParseError as e:
            return {"valid": False, "error": f"XML error: {e}"}

    return {"valid": True}


def validate_edit(
    file_path: str | Path,
    new_content: str,
    check_syntax: bool = True,
) -> dict[str, Any]:
    """Validate an edit operation.

    Currently only syntax validation is implemented.
    Additional checks (sensitive file, protected file, etc.) can be added.

    Args:
        file_path: Path to the file being edited
        new_content: The new content being inserted
        check_syntax: Whether to run syntax validation

    Returns:
        {"valid": True} if valid, {"valid": False, "error": "message"} if invalid

    """
    if not check_syntax:
        return {"valid": True}

    return validate_edit_syntax(file_path, new_content)
