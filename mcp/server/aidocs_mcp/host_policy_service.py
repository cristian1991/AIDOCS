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
    *,
    project_root: str | Path | None = None,
    before_content: str | None = None,
) -> dict[str, Any]:
    """Validate that an edit would not introduce syntax errors.

    For ``.py`` files in a bound project it also refuses an edit that would
    INTRODUCE Ruff findings under the project's own Ruff config (the shared
    ``ruff_edit_gate`` helper; see that module for the full contract).

    Args:
        file_path: Path to the file being edited (absolute, or relative to
            ``project_root``)
        new_content: The new content being inserted
        project_root: The bound project root. Ruff config discovery is bounded
            by it; without it the Ruff gate does not run (nothing to bind to).
        before_content: The snapshot ``new_content`` was reconstructed from.
            When the caller cannot supply it (a full-file write), the on-disk
            file is read here as the BEFORE ("" when it does not exist). That
            read is an EARLIER defense only: file_ops re-judges the exact
            snapshot it writes from and remains the authority.

    Returns:
        {"valid": True} if valid, {"valid": False, "error": "message"} if invalid.
        Ruff refusals also carry ``"reason"``: ``ruff_new_findings``,
        ``ruff_validator_error`` or ``ruff_validator_unavailable``. A Ruff
        validator failure is RETURNED as a refusal, never raised.

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

    # HTML is deliberately NOT here: it is not XML (DOCTYPE, void elements,
    # unquoted attributes), so an XML parser refuses valid pages. HTML is
    # error-tolerant by spec and has no strict parse verdict to enforce.
    elif suffix in (".xml", ".csproj", ".resx", ".config"):
        import xml.etree.ElementTree as ET

        try:
            ET.fromstring(new_content)
        except ET.ParseError as e:
            return {"valid": False, "error": f"XML error: {e}"}

    ruff_refusal = _ruff_edit_refusal(abs_path, new_content, project_root, before_content)
    if ruff_refusal is not None:
        return ruff_refusal

    return {"valid": True}


def _ruff_edit_refusal(
    abs_path: Path,
    new_content: str,
    project_root: str | Path | None,
    before_content: str | None,
) -> dict[str, Any] | None:
    """The Ruff edit gate, as a refusal dict or None. A validator failure is a
    refusal RETURNED here, never an exception for a caller to swallow."""
    if project_root is None or abs_path.suffix.lower() != ".py":
        return None
    from .ruff_edit_gate import check_edit

    target = abs_path if abs_path.is_absolute() else Path(project_root) / abs_path
    before = before_content
    if before is None:
        # No snapshot from the caller (full-file write): the on-disk file is the
        # earlier-defense BEFORE. file_ops re-judges its exact snapshot.
        try:
            before = target.read_text(encoding="utf-8") if target.is_file() else ""
        except (OSError, UnicodeDecodeError):
            before = ""
    result = check_edit(
        project_root=project_root,
        target=target,
        before=before,
        after=new_content,
    )
    if not result.refused:
        return None
    return {"valid": False, "error": result.refusal_message(), "reason": result.reason}
