"""File operations service — line-based reading and editing with safety checks.

Three operations:
    get_lines    — Read specific lines from any file (fast, no index needed)
    edit_lines   — Replace a line range with new content, with optional content verification
    batch_edit   — Apply multiple edits atomically across one or more files

All operations are line-based (1-indexed) and file-type agnostic.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    "c:/windows", "c:/program files", "c:/program files (x86)",
    "c:/programdata", "c:/users/public",
    "/usr", "/bin", "/sbin", "/etc", "/var", "/boot", "/sys", "/proc",
    "/lib", "/lib64", "/opt", "/root",
}

# The MCP server's own source — agents cannot edit the tool they're running on
_SELF_DIR: str = str(Path(__file__).resolve().parent).replace("\\", "/").lower()

# Markers that indicate a directory is a valid project root
PROJECT_MARKERS = (
    ".git", ".MEMORY", "CLAUDE.md", "AGENTS.md",
    "package.json", "pyproject.toml", "Cargo.toml",
    "go.mod", "pom.xml", ".csproj", ".sln",
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

    # Block WRITING to the MCP server itself unless dev_mode is enabled (reads always allowed)
    if write and (lower_str == _SELF_DIR or _SELF_DIR.startswith(lower_str + "/")):
        from .config import DEV_MODE
        if not DEV_MODE:
            raise ValueError(
                f"Refusing to edit the AIDOCS MCP server directory. "
                f"Self-editing requires dev_mode=true in aidocs.toml [dev] section."
            )

    # Must be an existing directory
    if not resolved.is_dir():
        raise ValueError(f"Project root does not exist: {resolved}")

    # Must have at least one project marker (git, package.json, .csproj, etc.)
    has_marker = any(
        (resolved / marker).exists() or
        any(resolved.glob(f"**/{marker}")) if marker.startswith(".") and not (resolved / marker).exists() else False
        for marker in PROJECT_MARKERS[:4]  # Only check fast markers (first 4)
    )
    # Fallback: check if any file with common project extensions exists
    if not has_marker:
        has_marker = any(
            (resolved / marker).exists()
            for marker in PROJECT_MARKERS
        )
    if not has_marker:
        raise ValueError(
            f"Directory does not appear to be a project root: {resolved}. "
            f"No project markers found ({', '.join(PROJECT_MARKERS[:5])}, ...)."
        )


def _resolve_path(project_root: Path, relative_path: str, *, write: bool = False) -> Path:
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
        # Config files are NEVER editable by agents — absolute boundary
        config_names = {"aidocs.toml", "aidocs-plugin.json"}
        if abs_path.name.lower() in config_names:
            raise ValueError(
                f"Config files are never editable by agents: {relative_path}. "
                f"Edit manually or via the installer."
            )
        if abs_lower.startswith(_SELF_DIR):
            from .config import DEV_MODE
            if not DEV_MODE:
                raise ValueError(
                    f"Refusing to edit AIDOCS MCP server source: {relative_path}. "
                    f"Set dev_mode=true in aidocs.toml [dev] to enable."
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
        raise ValueError(f"File too large ({size:,} bytes, max {MAX_FILE_SIZE:,}): {abs_path.name}")

    text = abs_path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines()


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


def edit_lines(
    project_root: Path,
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    *,
    expect: str | None = None,
    dry_run: bool = False,
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
    abs_path = _resolve_path(project_root, path, write=True)
    try:
        _check_sensitive(path)
    except ValueError as exc:
        return _edit_error(path, start_line, end_line, str(exc))
    all_lines = _read_file_lines(abs_path)
    total = len(all_lines)

    # Validate line range
    if start_line < 1:
        return _edit_error(path, start_line, end_line, "start_line must be >= 1")
    if start_line > total + 1:
        return _edit_error(path, start_line, end_line, f"start_line {start_line} exceeds file length ({total} lines)")

    # Handle insert mode (end_line < start_line means "insert before start_line")
    is_insert = end_line < start_line
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

    if dry_run:
        return {
            "success": True,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "old_content": old_content,
            "new_content": new_content,
            "lines_removed": len(old_lines),
            "lines_added": len(new_lines),
            "dry_run": True,
            "error": None,
        }

    # Apply the edit
    if is_insert:
        result_lines = all_lines[:insert_at] + new_lines + all_lines[insert_at:]
    else:
        result_lines = all_lines[: start_line - 1] + new_lines + all_lines[end_line:]

    # Write back
    abs_path.write_text("\n".join(result_lines) + "\n", encoding="utf-8")

    return {
        "success": True,
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
        "old_content": old_content,
        "new_content": new_content,
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
) -> dict[str, object]:
    """Apply multiple line edits atomically.

    If `atomic` is True (default), ALL edits are validated first (including expect checks).
    If any edit would fail, NONE are applied. This prevents partial corruption.

    Each edit in the list has the same fields as edit_lines:
        { "path": str, "start_line": int, "end_line": int, "new_content": str, "expect": str | None }

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
    validations: list[dict[str, object]] = []

    for edit in edits:
        path = str(edit.get("path", ""))
        start = int(edit.get("start_line", 0))
        end = int(edit.get("end_line", 0))
        new_content = str(edit.get("new_content", ""))
        expect = edit.get("expect")
        expect_str = str(expect) if expect is not None else None

        try:
            abs_path = _resolve_path(project_root, path, write=True)
            _check_sensitive(path)

            if path not in file_cache:
                file_cache[path] = _read_file_lines(abs_path)

            all_lines = file_cache[path]
            total = len(all_lines)

            if start < 1 or start > total + 1:
                validations.append(_edit_error(path, start, end, f"Invalid start_line {start} (file has {total} lines)"))
                continue

            is_insert = end < start
            if is_insert:
                old_lines: list[str] = []
            else:
                clamped_end = min(end, total)
                old_lines = all_lines[start - 1 : clamped_end]

            old_content = "\n".join(old_lines)

            # Expect check
            if expect_str is not None:
                if expect_str.strip() != old_content.strip():
                    validations.append({
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
                    })
                    continue

            validations.append({
                "success": True,
                "path": path,
                "start_line": start,
                "end_line": end,
                "old_content": old_content,
                "new_content": new_content,
                "lines_removed": len(old_lines),
                "lines_added": len(new_content.split("\n")) if new_content.strip() else 0,
                "dry_run": dry_run,
                "error": None,
            })

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
        path = str(edit.get("path", ""))
        if path not in edits_by_file:
            edits_by_file[path] = []
        edits_by_file[path].append((i, edit))

    # Apply edits per file, processing from bottom to top to preserve line numbers
    for path, file_edits in edits_by_file.items():
        abs_path = _resolve_path(project_root, path)
        all_lines = file_cache[path]

        # Sort by start_line descending (bottom-up)
        file_edits.sort(key=lambda x: -int(x[1].get("start_line", 0)))

        for idx, edit in file_edits:
            start = int(edit.get("start_line", 0))
            end = int(edit.get("end_line", 0))
            new_content = str(edit.get("new_content", ""))
            new_lines = new_content.split("\n") if new_content.strip() else []

            is_insert = end < start
            if is_insert:
                insert_at = start - 1
                all_lines = all_lines[:insert_at] + new_lines + all_lines[insert_at:]
            else:
                clamped_end = min(end, len(all_lines))
                all_lines = all_lines[: start - 1] + new_lines + all_lines[clamped_end:]

        # Write the modified file
        abs_path.write_text("\n".join(all_lines) + "\n", encoding="utf-8")

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
