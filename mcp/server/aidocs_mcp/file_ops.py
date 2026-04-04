"""Line-based file operations with strong guardrails.

Three operations:
    get_lines  - Read specific lines from any file (fast, no index needed)
    edit_lines - Replace a line range with new content, with optional content verification
    batch_edit - Apply multiple edits atomically across one or more files

All operations are line-based (1-indexed) and file-type agnostic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

from .config_schema import (
    ConfigEditMode,
    SETTINGS_CATALOG,
    is_setting_agent_editable,
    self_edit_available_in_profile,
    validate_setting_value,
)


@dataclass(slots=True)
class LineRange:
    """A range of lines from a file."""

    path: str
    start_line: int
    end_line: int
    lines: list[str]
    total_lines: int

    @property
    def content(self) -> str:
        return "\n".join(self.lines)

    @property
    def line_count(self) -> int:
        return len(self.lines)


@dataclass(slots=True)
class EditResult:
    """Result of a line edit operation."""

    success: bool
    path: str
    start_line: int
    end_line: int
    old_content: str
    new_content: str
    lines_removed: int
    lines_added: int
    error: str | None = None


@dataclass(slots=True)
class BatchEditResult:
    """Result of a batch edit operation."""

    success: bool
    total_edits: int
    applied: int
    failed: int
    results: list[EditResult]
    error: str | None = None


# ── Safety limits ──

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_LINE_COUNT = 200  # Max lines per get_lines call
MAX_BATCH_EDITS = 20  # Max edits per batch_edit call

# Files that should never be edited by agents
SENSITIVE_PATTERNS = (".env", "credentials", "secrets", ".key", ".pem", ".pfx")

# System/OS directories that are never valid project roots
BLOCKED_ROOTS = {
    "c:/windows",
    "c:/program files",
    "c:/program files (x86)",
    "c:/programdata",
    "c:/users/public",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/var",
    "/boot",
    "/sys",
    "/proc",
    "/lib",
    "/lib64",
    "/opt",
    "/root",
}

# The MCP server's own source — agents cannot edit the tool they're running on
_SELF_DIR: str = str(Path(__file__).resolve().parent).replace("\\", "/").lower()

# Markers that indicate a directory is a valid project root
PROJECT_MARKERS = (
    ".git",
    ".MEMORY",
    "CLAUDE.md",
    "AGENTS.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    ".csproj",
    ".sln",
)


def _validate_project_root(project_root: Path, *, write: bool = False) -> None:
    """Ensure project_root is a legitimate project directory, not a system path.

    Args:
        write: If True, also block self-editing of the MCP server directory.
               Reads are always allowed on the MCP server (agents need to inspect code).
    """
    resolved = project_root.resolve()
    lower_str = str(resolved).replace("\\", "/").lower()

    # Block system directories (always, read or write)
    for blocked in BLOCKED_ROOTS:
        if lower_str == blocked or lower_str.startswith(blocked + "/"):
            raise ValueError(
                f"Refusing to operate on system directory: {resolved}. "
                f"File operations are restricted to project directories."
            )

    # Must be an existing directory
    if not resolved.is_dir():
        raise ValueError(f"Project root does not exist: {resolved}")

    # Must have at least one project marker (git, package.json, .csproj, etc.)
    has_marker = any(
        (resolved / marker).exists() or any(resolved.glob(f"**/{marker}"))
        if marker.startswith(".") and not (resolved / marker).exists()
        else False
        for marker in PROJECT_MARKERS[:4]  # Only check fast markers (first 4)
    )
    # Fallback: check if any file with common project extensions exists
    if not has_marker:
        has_marker = any((resolved / marker).exists() for marker in PROJECT_MARKERS)
    if not has_marker:
        raise ValueError(
            f"Directory does not appear to be a project root: {resolved}. "
            f"No project markers found ({', '.join(PROJECT_MARKERS[:5])}, ...)."
        )


def _resolve_path(
    project_root: Path,
    relative_path: str,
    *,
    write: bool = False,
    config_edit_mode: ConfigEditMode | None = None,
) -> Path:
    """Resolve and validate a file path within the project root.

    Args:
        write: If True, also block paths inside the MCP server directory.
    """
    # Reject absolute paths — only relative paths allowed
    if os.path.isabs(relative_path):
        raise ValueError(
            f"Absolute paths are not allowed: {relative_path}. "
            f"Use a path relative to the project root."
        )

    # Normalize separators
    clean = relative_path.replace("\\", "/").lstrip("/")

    # Security: prevent path traversal
    if ".." in clean.split("/"):
        raise ValueError(f"Path traversal not allowed: {relative_path}")

    abs_path = (project_root / clean).resolve()

    # Security: ensure resolved path is within project root
    try:
        abs_path.relative_to(project_root.resolve())
    except ValueError:
        raise ValueError(f"Path escapes project root: {relative_path}")

    # Security: prevent WRITING to MCP server source unless dev_mode is on (reads always allowed)
    if write:
        abs_lower = str(abs_path).replace("\\", "/").lower()
        if abs_path.name.lower() == "aidocs-plugin.json":
            raise ValueError(
                f"Config files are never editable by agents: {relative_path}. "
                f"Edit manually or via the installer."
            )
        if abs_path.name.lower() == "aidocs.toml":
            canonical_project_config = (
                project_root.resolve() / "aidocs.toml"
            ).resolve()
            if abs_path != canonical_project_config:
                raise ValueError(
                    f"Only the project-root aidocs.toml may be agent-edited: {relative_path}."
                )
            if config_edit_mode != "explicit_user_permitted":
                raise ValueError(
                    "aidocs.toml edits require config_edit_mode='explicit_user_permitted'."
                )
        if abs_lower.startswith(_SELF_DIR):
            if not self_edit_available_in_profile("release"):
                raise ValueError(
                    f"Refusing to edit AIDOCS MCP server source in release profile: {relative_path}. "
                    f"Self-edit is not available in release behavior."
                )

    return abs_path


def _check_sensitive(path: str) -> None:
    """Block edits to sensitive files (credentials, keys, env)."""
    lower = path.lower()
    for pattern in SENSITIVE_PATTERNS:
        if pattern in lower:
            raise ValueError(
                f"Refusing to operate on potentially sensitive file: {path}. "
                f"Matched pattern: {pattern}"
            )


def _read_file_lines(abs_path: Path) -> list[str]:
    """Read a file and return its lines (no trailing newline stripping)."""
    if not abs_path.is_file():
        raise FileNotFoundError(f"File not found: {abs_path}")

    size = abs_path.stat().st_size
    if size > MAX_FILE_SIZE:
        raise ValueError(
            f"File too large ({size:,} bytes, max {MAX_FILE_SIZE:,}): {abs_path.name}"
        )

    text = abs_path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines()


def _file_ends_with_newline(abs_path: Path) -> bool:
    """Return whether the file currently ends with a newline byte."""
    data = abs_path.read_bytes()
    return data.endswith(b"\n") if data else False


def _write_lines(abs_path: Path, lines: list[str], *, final_newline: bool) -> None:
    text = "\n".join(lines)
    if final_newline and lines:
        text += "\n"
    abs_path.write_text(text, encoding="utf-8")


def _canonical_relative_path(project_root: Path, abs_path: Path) -> str:
    return abs_path.relative_to(project_root.resolve()).as_posix()


def _render_lines(lines: list[str], *, final_newline: bool) -> str:
    text = "\n".join(lines)
    if final_newline and lines:
        return text + "\n"
    return text


def _flatten_config_paths(value: object, prefix: str) -> set[str]:
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, nested in value.items():
            nested_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.update(_flatten_config_paths(nested, nested_prefix))
        return paths
    return {prefix} if prefix else set()


def _diff_config_paths(before: object, after: object, prefix: str = "") -> set[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        paths: set[str] = set()
        for key in sorted(set(before) | set(after)):
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.update(
                _diff_config_paths(before.get(key), after.get(key), next_prefix)
            )
        return paths
    if isinstance(before, dict) or isinstance(after, dict):
        return _flatten_config_paths(before, prefix) | _flatten_config_paths(
            after, prefix
        )
    return {prefix} if before != after and prefix else set()


def _load_toml_text(text: str, *, path: str) -> dict[str, object]:
    if tomllib is None:
        raise ValueError("TOML parsing is unavailable in this runtime.")
    try:
        loaded = tomllib.loads(text or "") if text.strip() else {}
    except Exception as exc:
        raise ValueError(f"Invalid TOML for {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(
            f"Invalid TOML root for {path}: expected a table at the document root."
        )
    return loaded


def _validate_config_edit(
    abs_path: Path,
    *,
    current_text: str,
    updated_text: str,
    config_edit_mode: ConfigEditMode | None,
) -> None:
    if abs_path.name.lower() != "aidocs.toml":
        return

    current_config = _load_toml_text(current_text, path=str(abs_path))
    updated_config = _load_toml_text(updated_text, path=str(abs_path))

    for setting_path in sorted(_diff_config_paths(current_config, updated_config)):
        metadata = SETTINGS_CATALOG.get(setting_path)
        if metadata is None:
            # Skip intermediate paths that have catalog entries below them.
            # The leaf paths will be validated separately.
            if any(key.startswith(setting_path + ".") for key in SETTINGS_CATALOG):
                continue
            raise ValueError(f"Config setting is not agent-editable: {setting_path}.")
        if metadata["security_sensitive"]:
            raise ValueError(
                f"Security-sensitive config cannot be agent-edited: {setting_path}."
            )
        if not is_setting_agent_editable(
            setting_path, scope="project", edit_mode=config_edit_mode
        ):
            raise ValueError(
                f"Config setting requires controlled edit permission: {setting_path}."
            )
        current_value: object = updated_config
        for part in setting_path.split("."):
            if not isinstance(current_value, dict) or part not in current_value:
                raise ValueError(
                    f"Config setting is not agent-editable: {setting_path}."
                )
            current_value = current_value[part]
        validate_setting_value(setting_path, current_value)


# ── Public API ──


def get_lines(
    project_root: Path,
    path: str,
    start_line: int = 1,
    count: int = 50,
    *,
    show_line_numbers: bool = True,
) -> dict[str, object]:
    """Read specific lines from any file.

    Args:
        project_root: Project root directory.
        path: Relative path to the file.
        start_line: First line to read (1-indexed). Clamped to valid range.
        count: Number of lines to read. Clamped to MAX_LINE_COUNT.
        show_line_numbers: If True, prefix each line with its line number.

    Returns:
        {
            "path": str,
            "start_line": int,
            "end_line": int,
            "total_lines": int,
            "content": str,          # The requested lines (with optional line numbers)
            "lines": [str],          # Raw lines without numbers
            "has_more": bool,        # True if file has more lines after end_line
            "truncated": bool,       # True if count was clamped
        }
    """
    _validate_project_root(project_root)
    abs_path = _resolve_path(project_root, path)
    all_lines = _read_file_lines(abs_path)
    total = len(all_lines)

    # Clamp inputs
    start = max(1, min(start_line, total))
    requested_count = count
    count = max(1, min(count, MAX_LINE_COUNT))
    truncated = count < requested_count

    end = min(start + count - 1, total)

    # Extract lines (convert from 1-indexed to 0-indexed)
    selected = all_lines[start - 1 : end]

    if show_line_numbers:
        width = len(str(end))
        numbered = [f"{start + i:>{width}}  {line}" for i, line in enumerate(selected)]
        content = "\n".join(numbered)
    else:
        content = "\n".join(selected)

    result: dict[str, object] = {
        "path": path,
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "content": content,
    }
    if end < total:
        result["has_more"] = True
    if truncated:
        result["truncated"] = True
    return result


def create_file(
    project_root: Path,
    path: str,
    content: str,
    *,
    config_edit_mode: ConfigEditMode | None = None,
) -> dict[str, object]:
    """Create a new file with exact content.

    This is intentionally separate from edit_lines so callers can express
    first-write intent directly without insert-mode gymnastics.
    """
    try:
        _validate_project_root(project_root, write=True)
        abs_path = _resolve_path(
            project_root, path, write=True, config_edit_mode=config_edit_mode
        )
        _check_sensitive(path)
    except ValueError as exc:
        return {
            "success": False,
            "path": path,
            "created": False,
            "bytes_written": 0,
            "lines_written": 0,
            "error": str(exc),
        }

    if abs_path.exists():
        return {
            "success": False,
            "path": path,
            "created": False,
            "bytes_written": 0,
            "lines_written": 0,
            "error": f"File already exists: {path}",
        }

    try:
        _validate_config_edit(
            abs_path,
            current_text="",
            updated_text=content,
            config_edit_mode=config_edit_mode,
        )
    except ValueError as exc:
        return {
            "success": False,
            "path": path,
            "created": False,
            "bytes_written": 0,
            "lines_written": 0,
            "error": str(exc),
        }

    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    canonical_path = _canonical_relative_path(project_root, abs_path)

    return {
        "success": True,
        "path": canonical_path,
        "canonical_path": canonical_path,
        "created": True,
        "bytes_written": len(content.encode("utf-8")),
        "lines_written": len(content.splitlines()),
        "error": None,
    }


def edit_lines(
    project_root: Path,
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    *,
    expect: str | None = None,
    dry_run: bool = False,
    mode: str = "auto",
    config_edit_mode: ConfigEditMode | None = None,
) -> dict[str, object]:
    """Replace a range of lines with new content.

    Safety features:
        - Returns old content so caller can verify what was replaced
        - Optional `expect` parameter: if provided, edit is rejected unless
          the current content of the line range matches exactly
        - `dry_run` mode: returns what would change without writing

    Args:
        project_root: Project root directory.
        path: Relative path to the file.
        start_line: First line to replace (1-indexed, inclusive).
        end_line: Last line to replace (1-indexed, inclusive). Use same as start_line to replace a single line.
                  Use start_line - 1 (i.e., end_line < start_line) to INSERT before start_line without removing any lines.
        new_content: Replacement text. Can be multi-line (split on \\n).
        expect: If provided, the current content of lines start..end must match this exactly (trimmed).
                If it doesn't match, the edit is rejected with a diff.
        dry_run: If True, return what would change without writing the file.
        mode: `auto`, `insert`, or `replace`.

    Returns:
        {
            "success": bool,
            "path": str,
            "start_line": int,
            "end_line": int,
            "old_content": str,      # What was there before (empty for inserts)
            "new_content": str,      # What was written
            "lines_removed": int,
            "lines_added": int,
            "dry_run": bool,
            "error": str | None,     # Set if expect mismatch or other error
        }
    """
    try:
        _validate_project_root(project_root, write=True)
    except ValueError as exc:
        return _edit_error(path, start_line, end_line, str(exc))
    try:
        abs_path = _resolve_path(
            project_root, path, write=True, config_edit_mode=config_edit_mode
        )
    except ValueError as exc:
        return _edit_error(path, start_line, end_line, str(exc))
    canonical_path = _canonical_relative_path(project_root, abs_path)
    try:
        _check_sensitive(path)
    except ValueError as exc:
        return _edit_error(path, start_line, end_line, str(exc))
    original_final_newline = _file_ends_with_newline(abs_path)
    all_lines = _read_file_lines(abs_path)
    total = len(all_lines)

    # Validate line range
    if start_line < 1:
        return _edit_error(path, start_line, end_line, "start_line must be >= 1")
    if start_line > total + 1:
        return _edit_error(
            path,
            start_line,
            end_line,
            f"start_line {start_line} exceeds file length ({total} lines)",
        )

    mode_value = mode.strip().lower()
    if mode_value not in {"auto", "insert", "replace"}:
        return _edit_error(path, start_line, end_line, f"Unknown mode: {mode}")

    # Handle insert mode (end_line < start_line means "insert before start_line")
    is_insert = mode_value == "insert" or (
        mode_value == "auto" and end_line < start_line
    )
    if mode_value == "replace" and end_line < start_line:
        return _edit_error(
            path, start_line, end_line, "replace mode requires end_line >= start_line"
        )
    if is_insert:
        old_lines: list[str] = []
        insert_at = start_line - 1  # 0-indexed position to insert before
    else:
        # Clamp end_line to file length
        end_line = min(end_line, total)
        old_lines = all_lines[start_line - 1 : end_line]
        insert_at = start_line - 1

    old_content = "\n".join(old_lines)
    new_lines = new_content.split("\n") if new_content.strip() else []

    # Expect check (safety gate)
    if expect is not None:
        expected_trimmed = expect.strip()
        actual_trimmed = old_content.strip()
        if expected_trimmed != actual_trimmed:
            return {
                "success": False,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "old_content": old_content,
                "new_content": new_content,
                "lines_removed": 0,
                "lines_added": 0,
                "dry_run": dry_run,
                "error": f"Content mismatch — expected:\n{expected_trimmed}\n\nactual:\n{actual_trimmed}",
            }

    if is_insert:
        result_lines = all_lines[:insert_at] + new_lines + all_lines[insert_at:]
    else:
        result_lines = all_lines[: start_line - 1] + new_lines + all_lines[end_line:]

    touches_file_end = (is_insert and insert_at == total) or (
        not is_insert and end_line >= total
    )
    final_newline = original_final_newline
    if touches_file_end and new_content.endswith("\n"):
        final_newline = True

    try:
        _validate_config_edit(
            abs_path,
            current_text=_render_lines(all_lines, final_newline=original_final_newline),
            updated_text=_render_lines(result_lines, final_newline=final_newline),
            config_edit_mode=config_edit_mode,
        )
    except ValueError as exc:
        return _edit_error(path, start_line, end_line, str(exc))

    if dry_run:
        return {
            "success": True,
            "path": canonical_path,
            "canonical_path": canonical_path,
            "start_line": start_line,
            "end_line": end_line,
            "old_content": old_content,
            "new_content": new_content,
            "lines_removed": len(old_lines),
            "lines_added": len(new_lines),
            "dry_run": True,
            "error": None,
        }

    # Write back
    _write_lines(abs_path, result_lines, final_newline=final_newline)

    # Omit old_content/new_content on success — agent already knows both
    return {
        "success": True,
        "path": canonical_path,
        "canonical_path": canonical_path,
        "start_line": start_line,
        "end_line": end_line,
        "lines_removed": len(old_lines),
        "lines_added": len(new_lines),
        "dry_run": False,
        "error": None,
    }


def batch_edit(
    project_root: Path,
    edits: list[dict[str, object]],
    *,
    dry_run: bool = False,
    atomic: bool = True,
    config_edit_mode: ConfigEditMode | None = None,
) -> dict[str, object]:
    """Apply multiple line edits atomically.

    If `atomic` is True (default), ALL edits are validated first (including expect checks).
    If any edit would fail, NONE are applied. This prevents partial corruption.

    Each edit in the list has the same fields as edit_lines:
        { "path": str, "start_line": int, "end_line": int, "new_content": str, "expect": str | None, "mode": str | None }

    Args:
        project_root: Project root directory.
        edits: List of edit operations.
        dry_run: If True, validate and return results without writing.
        atomic: If True, all-or-nothing — reject entire batch if any edit fails validation.

    Returns:
        {
            "success": bool,
            "total_edits": int,
            "applied": int,
            "failed": int,
            "results": [EditResult],
            "error": str | None,
        }
    """
    try:
        _validate_project_root(project_root, write=True)
    except ValueError as exc:
        return {
            "success": False,
            "total_edits": len(edits),
            "applied": 0,
            "failed": len(edits),
            "results": [],
            "error": str(exc),
        }

    if len(edits) > MAX_BATCH_EDITS:
        return {
            "success": False,
            "total_edits": len(edits),
            "applied": 0,
            "failed": len(edits),
            "results": [],
            "error": f"Too many edits ({len(edits)}). Maximum is {MAX_BATCH_EDITS}.",
        }

    if not edits:
        return {
            "success": True,
            "total_edits": 0,
            "applied": 0,
            "failed": 0,
            "results": [],
            "error": None,
        }

    # Phase 1: Read all files and validate all edits
    file_cache: dict[str, list[str]] = {}
    file_paths: dict[str, Path] = {}
    file_final_newlines: dict[str, bool] = {}
    validations: list[dict[str, object]] = []

    for edit in edits:
        path = str(edit.get("path", ""))
        start = int(edit.get("start_line", 0))
        end = int(edit.get("end_line", 0))
        new_content = str(edit.get("new_content", ""))
        expect = edit.get("expect")
        expect_str = str(expect) if expect is not None else None
        mode = str(edit.get("mode", "auto")).strip().lower()

        try:
            abs_path = _resolve_path(
                project_root, path, write=True, config_edit_mode=config_edit_mode
            )
            _check_sensitive(path)
            if abs_path.name.lower() == "aidocs.toml":
                raise ValueError(
                    "Use edit_lines for aidocs.toml so config policy can be validated safely."
                )
            canonical_path = _canonical_relative_path(project_root, abs_path)

            if canonical_path not in file_cache:
                file_cache[canonical_path] = _read_file_lines(abs_path)
                file_paths[canonical_path] = abs_path
                file_final_newlines[canonical_path] = _file_ends_with_newline(abs_path)

            all_lines = file_cache[canonical_path]
            total = len(all_lines)

            if start < 1 or start > total + 1:
                validations.append(
                    _edit_error(
                        path,
                        start,
                        end,
                        f"Invalid start_line {start} (file has {total} lines)",
                    )
                )
                continue

            if mode not in {"auto", "insert", "replace"}:
                validations.append(
                    _edit_error(path, start, end, f"Unknown mode: {mode}")
                )
                continue
            is_insert = mode == "insert" or (mode == "auto" and end < start)
            if mode == "replace" and end < start:
                validations.append(
                    _edit_error(
                        path, start, end, "replace mode requires end_line >= start_line"
                    )
                )
                continue
            if is_insert:
                old_lines: list[str] = []
            else:
                clamped_end = min(end, total)
                old_lines = all_lines[start - 1 : clamped_end]

            old_content = "\n".join(old_lines)

            # Expect check
            if expect_str is not None:
                if expect_str.strip() != old_content.strip():
                    validations.append(
                        {
                            "success": False,
                            "path": path,
                            "start_line": start,
                            "end_line": end,
                            "old_content": old_content,
                            "new_content": new_content,
                            "lines_removed": 0,
                            "lines_added": 0,
                            "dry_run": dry_run,
                            "error": f"Content mismatch at {path}:{start}-{end}",
                        }
                    )
                    continue

            validations.append(
                {
                    "success": True,
                    "path": path,
                    "canonical_path": canonical_path,
                    "start_line": start,
                    "end_line": end,
                    "lines_removed": len(old_lines),
                    "lines_added": len(new_content.split("\n"))
                    if new_content.strip()
                    else 0,
                    "dry_run": dry_run,
                    "error": None,
                    # old_content/new_content kept only for dry_run and error cases
                    **({"old_content": old_content, "new_content": new_content} if dry_run else {}),
                }
            )

        except Exception as exc:
            validations.append(_edit_error(path, start, end, str(exc)))

    # Check if any failed
    failures = [v for v in validations if not v["success"]]

    if atomic and failures:
        return {
            "success": False,
            "total_edits": len(edits),
            "applied": 0,
            "failed": len(failures),
            "results": validations,
            "error": f"{len(failures)} edit(s) failed validation. Atomic mode: no edits applied.",
        }

    if dry_run:
        return {
            "success": len(failures) == 0,
            "total_edits": len(edits),
            "applied": len(validations) - len(failures),
            "failed": len(failures),
            "results": validations,
            "error": None,
        }

    # Phase 2: Apply edits (process files in reverse line order to preserve line numbers)
    # Group edits by file
    edits_by_file: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for i, (edit, validation) in enumerate(zip(edits, validations)):
        if not validation["success"]:
            continue
        canonical_path = str(validation.get("canonical_path", edit.get("path", "")))
        if canonical_path not in edits_by_file:
            edits_by_file[canonical_path] = []
        edits_by_file[canonical_path].append((i, edit))

    # Apply edits per file, processing from bottom to top to preserve line numbers
    for canonical_path, file_edits in edits_by_file.items():
        abs_path = file_paths[canonical_path]
        all_lines = file_cache[canonical_path]
        total = len(all_lines)
        final_newline = file_final_newlines[canonical_path]

        # Sort by start_line descending (bottom-up)
        file_edits.sort(key=lambda x: -int(x[1].get("start_line", 0)))

        for idx, edit in file_edits:
            start = int(edit.get("start_line", 0))
            end = int(edit.get("end_line", 0))
            new_content = str(edit.get("new_content", ""))
            new_lines = new_content.split("\n") if new_content.strip() else []
            mode = str(edit.get("mode", "auto")).strip().lower()

            is_insert = mode == "insert" or (mode == "auto" and end < start)
            if is_insert:
                insert_at = start - 1
                all_lines = all_lines[:insert_at] + new_lines + all_lines[insert_at:]
                if insert_at == total and new_content.endswith("\n"):
                    final_newline = True
            else:
                clamped_end = min(end, len(all_lines))
                all_lines = all_lines[: start - 1] + new_lines + all_lines[clamped_end:]
                if end >= total and new_content.endswith("\n"):
                    final_newline = True

        # Write the modified file
        _write_lines(abs_path, all_lines, final_newline=final_newline)

    applied = len(validations) - len(failures)
    return {
        "success": len(failures) == 0,
        "total_edits": len(edits),
        "applied": applied,
        "failed": len(failures),
        "results": validations,
        "error": None if not failures else f"{len(failures)} edit(s) failed.",
    }


def _edit_error(path: str, start: int, end: int, error: str) -> dict[str, object]:
    """Create a failed edit result."""
    return {
        "success": False,
        "path": path,
        "start_line": start,
        "end_line": end,
        "old_content": "",
        "new_content": "",
        "lines_removed": 0,
        "lines_added": 0,
        "dry_run": False,
        "error": error,
    }
