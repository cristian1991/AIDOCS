"""DO NOT TOUCH — protect / unprotect file operations.

Spec: .MEMORY/sessions/2026-04-13-repo-and-dashboard-audit/plans/do-not-touch-gate-spec.md

These are the tool-side ops that MCP exposes as `ai_protect`.

ASYMMETRIC GATING (2026-06-11): adding protection is UNGATED — it is
additive safety (the header surfaces restrictions on read, makes editors
extra-careful, fences subagents), so the agent picks the file by its own
recognition and writes the `why`. REMOVING protection stays strictly
gated: unprotect_file requires an active per-turn user grant AND the
registry identity-check (only the protecting user or admin+ can strip a
header). Without that, remove refuses / escalates.

The conductor is responsible for reading the file + siblings BEFORE
calling protect (see workflow rule "reading costs less than writing").
This module writes the header; the conductor writes the content of
the `why` block and the `pair_files` list.
"""

from __future__ import annotations

from pathlib import Path

from .protected_file import (
    PROTECTION_SENTINEL,
    has_protection_sentinel,
)
from .protected_file_runtime import (
    get_protect_grants as _legacy_local_protect_grants,
)
from .protected_file_runtime import (
    get_unprotect_grants as _legacy_local_unprotect_grants,
)


def _active_grants(project_root: Path, axis: str) -> set[str]:
    """Return the union of (sqlite-backed per-session grants) +
    (module-level legacy grants).

    Sqlite is the authoritative cross-process source — claude_hook
    (CC's hook subprocess) writes there; ai_protect runs in the MCP
    server subprocess and could never see the module-level lists
    (#236, 2026-05-12). The module-level union is kept for tests
    that set the lists directly and for any path without an active
    managed session.
    """
    sqlite_grants: list[str] = []
    try:
        from .managed_mode_service import resolve_managed_session
        from .runtime_bootstrap_service import get_runtime

        runtime = get_runtime()
        gate = runtime.hub.query_gate
        # '__unbound__' is the crash/reconnect fallback key: the UPS
        # hook writes grants there when no managed session is bound
        # (2026-06-11). Always read it in union with the bound session.
        sids = ["__unbound__"]
        _bound_sid = resolve_managed_session(runtime.hub.managed_mode, project_root)
        if _bound_sid:
            sids.append(_bound_sid)
        for sid in sids:
            if axis == "protect":
                sqlite_grants += list(gate.get_protect_grants(project_root, sid))
            elif axis == "unprotect":
                sqlite_grants += list(gate.get_unprotect_grants(project_root, sid))
            elif axis == "protected_edit":
                sqlite_grants += list(
                    gate.get_protected_edit_grants(project_root, sid),
                )
    except Exception:
        sqlite_grants = []
    if axis == "protect":
        local = _legacy_local_protect_grants()
    elif axis == "unprotect":
        local = _legacy_local_unprotect_grants()
    else:
        local = []
    return {_canonical(g) for g in list(sqlite_grants) + list(local) if g}


def _consume_grant(project_root: Path, axis: str, rel: str) -> None:
    """Persist-until-consumed (operator directive 2026-06-11): a
    path-specific grant survives across turns UNTIL the protection
    state change it authorized actually happens — then it is removed
    so the authority doesn't outlive its purpose. The blanket '*'
    grant is never consumed here (it is per-turn by construction).
    Best-effort: a store failure leaves the grant in place.
    """
    try:
        from .managed_mode_service import resolve_managed_session
        from .runtime_bootstrap_service import get_runtime

        runtime = get_runtime()
        gate = runtime.hub.query_gate
        sids = ["__unbound__"]
        _bound_sid = resolve_managed_session(runtime.hub.managed_mode, project_root)
        if _bound_sid:
            sids.append(_bound_sid)
        for sid in sids:
            if axis == "protect":
                cur = gate.get_protect_grants(project_root, sid)
                kept = [p for p in cur if _canonical(p) != rel]
                if len(kept) != len(cur):
                    gate.set_protect_grants(project_root, sid, kept)
            elif axis == "unprotect":
                cur = gate.get_unprotect_grants(project_root, sid)
                kept = [p for p in cur if _canonical(p) != rel]
                if len(kept) != len(cur):
                    gate.set_unprotect_grants(project_root, sid, kept)
    except Exception:
        pass


# Language detection — extension → comment style we emit for headers.
#
# Each entry is (line_comment_prefix, block_open, block_close). If
# line_comment is set, we emit each header line prefixed. Else we wrap
# the whole header in block_open/close.
_HEADER_STYLES: dict[str, tuple[str | None, str | None, str | None]] = {
    # Line-comment languages
    ".cs": ("// ", None, None),
    ".ts": ("// ", None, None),
    ".tsx": ("// ", None, None),
    ".js": ("// ", None, None),
    ".jsx": ("// ", None, None),
    ".go": ("// ", None, None),
    ".rs": ("// ", None, None),
    ".java": ("// ", None, None),
    ".kt": ("// ", None, None),
    ".swift": ("// ", None, None),
    ".c": ("// ", None, None),
    ".cpp": ("// ", None, None),
    ".h": ("// ", None, None),
    ".hpp": ("// ", None, None),
    ".py": ("# ", None, None),
    ".rb": ("# ", None, None),
    ".sh": ("# ", None, None),
    ".yml": ("# ", None, None),
    ".yaml": ("# ", None, None),
    ".toml": ("# ", None, None),
    ".ini": ("# ", None, None),
    ".sql": ("-- ", None, None),
    # Block-comment languages
    ".css": (None, "/*", "*/"),
    ".scss": (None, "/*", "*/"),
    ".less": (None, "/*", "*/"),
    ".html": (None, "<!--", "-->"),
    ".htm": (None, "<!--", "-->"),
    ".xml": (None, "<!--", "-->"),
    ".md": (None, "<!--", "-->"),
    # Razor — @* ... *@ block
    ".cshtml": (None, "@*", "*@"),
    ".razor": (None, "@*", "*@"),
}


_HEADER_FENCE_TOP = "═" * 74
_HEADER_FENCE_MID = "─" * 74


def _build_header(
    language_style: tuple[str | None, str | None, str | None],
    why: str,
    pair_files: list[str],
) -> str:
    """Build the header block using the language's comment style.

    Layout:
        <fence>
          ⚠️  DO NOT TOUCH WITHOUT EXPLICIT USER REQUEST  ⚠️
        <mid-fence>
          {why}
          [blank]
          Pair files:
            {pair1}
            {pair2}
        <fence>
    """
    line_prefix, block_open, block_close = language_style

    lines: list[str] = []
    lines.append(_HEADER_FENCE_TOP)
    lines.append(f" ⚠️  {PROTECTION_SENTINEL}  ⚠️")
    lines.append(_HEADER_FENCE_MID)
    if why:
        for wl in why.splitlines() or [why]:
            lines.append(f" {wl}")
        lines.append("")
    if pair_files:
        lines.append("Pair files:")
        for pf in pair_files:
            lines.append(f"  {pf}")
    lines.append(_HEADER_FENCE_TOP)

    if line_prefix is not None:
        body = "\n".join(f"{line_prefix.rstrip()} {ln}".rstrip() for ln in lines)
        return body + "\n"
    inner = "\n".join(f"   {ln}".rstrip() for ln in lines)
    return f"{block_open}\n{inner}\n{block_close}\n"


def _language_style_for(path: Path) -> tuple[str | None, str | None, str | None] | None:
    """Look up the comment style for a file extension. Returns None for
    unrecognized extensions — caller must handle by refusing.
    """
    suffix = path.suffix.lower()
    return _HEADER_STYLES.get(suffix)


def _canonical(path: str) -> str:
    return path.replace("\\", "/").strip()


def protect_file(
    project_root: Path,
    path: str,
    *,
    why: str = "",
    pair_files: list[str] | None = None,
) -> dict[str, object]:
    """Add the DO NOT TOUCH header to a file. UNGATED (2026-06-11):
    protection is additive safety, so no user-intent grant is required —
    the agent picks the file and records `why`. Removal stays gated.
    """
    rel = _canonical(path)
    # UNGATED by operator doctrine 2026-06-11: protection is ADDITIVE
    # safety — the header surfaces restrictions on read, makes editors
    # extra-careful, and fences subagents. The agent picks the file via
    # its own recognition and records the `why`; demanding an exact
    # user-intent phrase + exact path here guarded the WRONG direction
    # (the dangerous direction is REMOVAL, which stays strictly grant-
    # gated in unprotect_file + identity-checked in the registry).

    abs_path = project_root / rel
    if not abs_path.is_file():
        return {
            "success": False,
            "path": rel,
            "error": f"File not found: {rel}",
        }

    style = _language_style_for(abs_path)
    if style is None:
        return {
            "success": False,
            "path": rel,
            "error": (
                f"Unknown language for {rel} (extension "
                f"'{abs_path.suffix}'). Add the header manually, or "
                f"use a recognized extension."
            ),
        }

    content = abs_path.read_text(encoding="utf-8", errors="replace")
    if has_protection_sentinel(content):
        _consume_grant(project_root, "protect", rel)
        return {
            "success": True,
            "path": rel,
            "status": "already_protected",
        }

    header = _build_header(style, why, list(pair_files or []))
    new_content = header + content
    abs_path.write_text(new_content, encoding="utf-8")

    # The file is now protected — the grant fulfilled its purpose.
    _consume_grant(project_root, "protect", rel)
    return {
        "success": True,
        "path": rel,
        "status": "protected",
        "pair_files": list(pair_files or []),
    }


def unprotect_file(project_root: Path, path: str) -> dict[str, object]:
    """Remove the DO NOT TOUCH header from a file. Requires an active
    grant in the per-turn unprotect list.
    """
    rel = _canonical(path)
    grants = _active_grants(project_root, "unprotect")
    if rel not in grants and "*" not in grants:
        return {
            "success": False,
            "path": rel,
            "error": (
                f"Refusing to unprotect {rel}: no active user-intent "
                f"grant for this path. Ask the user to say something "
                f"like 'remove DO NOT TOUCH from {rel}' in their next "
                f"message."
            ),
        }

    abs_path = project_root / rel
    if not abs_path.is_file():
        return {
            "success": False,
            "path": rel,
            "error": f"File not found: {rel}",
        }

    content = abs_path.read_text(encoding="utf-8", errors="replace")
    if not has_protection_sentinel(content):
        _consume_grant(project_root, "unprotect", rel)
        return {
            "success": True,
            "path": rel,
            "status": "not_protected",
        }

    # Strip the header block. The sentinel appears in the first 20 lines;
    # we need to find the contiguous header that contains it and drop
    # that block. Strategy: find the first line that contains the
    # sentinel, then walk outward to find the fence lines that bound the
    # header block.
    lines = content.splitlines(keepends=True)
    sentinel_idx = next(
        (i for i, ln in enumerate(lines) if PROTECTION_SENTINEL in ln),
        None,
    )
    if sentinel_idx is None:
        # Shouldn't happen — has_protection_sentinel just said yes.
        return {
            "success": False,
            "path": rel,
            "error": "Internal error: sentinel phrase present but line not found.",
        }

    # Walk up to block-open or first line.
    start = sentinel_idx
    while start > 0:
        prev = lines[start - 1].strip()
        # Comment-only lines (with optional comment-prefix chars) are
        # part of the header; stop at a non-comment line or blank.
        is_header_adjacent = (
            prev.startswith("//")
            or prev.startswith("#")
            or prev.startswith("--")
            or prev.startswith("/*")
            or prev.startswith("*")
            or prev.startswith("<!--")
            or prev.startswith("@*")
            or prev == ""
        )
        if not is_header_adjacent:
            break
        start -= 1
    # Walk down similarly.
    end = sentinel_idx
    while end < len(lines) - 1:
        nxt = lines[end + 1].strip()
        is_header_adjacent = (
            nxt.startswith("//")
            or nxt.startswith("#")
            or nxt.startswith("--")
            or nxt.startswith("*/")
            or nxt.startswith("*")
            or nxt.startswith("-->")
            or nxt.startswith("*@")
            or nxt == ""
        )
        if not is_header_adjacent:
            break
        end += 1

    # Also trim a trailing blank line after the header if present.
    new_lines = lines[:start] + lines[end + 1 :]
    # Strip a single leading blank line left behind.
    if new_lines and not new_lines[0].strip():
        new_lines = new_lines[1:]

    new_content = "".join(new_lines)
    abs_path.write_text(new_content, encoding="utf-8")

    # The file is now unprotected — the grant fulfilled its purpose.
    _consume_grant(project_root, "unprotect", rel)
    return {
        "success": True,
        "path": rel,
        "status": "unprotected",
    }


# Directory names we never walk into when scanning for protected files.
# Pulls common build / VCS / vendor dirs that have their own noise.
_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "bower_components",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".pytest-tmp",  # pytest scratch dirs
        ".mypy_cache",
        "dist",
        "build",
        "target",
        "bin",
        "obj",
        ".next",
        ".nuxt",
        ".MEMORY",  # AIDOCS memory tree — separate protection story
    },
)


def list_protected_files(project_root: Path) -> list[dict[str, object]]:
    """Walk the project and return every file carrying the sentinel.

    Each entry: {path, pair_files, why_excerpt}

    Skips VCS + build + vendor directories (see _SCAN_SKIP_DIRS).

    Today this is a filesystem walk; when the index column lands it
    will switch to a single SQL query for O(1) lookup.
    """
    from .protected_file import scan_protected_state

    results: list[dict[str, object]] = []
    project_root = project_root.resolve()
    for path in project_root.rglob("*"):
        # Skip non-files.
        if not path.is_file():
            continue
        # Skip anything inside a skip-dir.
        try:
            rel_parts = path.relative_to(project_root).parts
        except ValueError:
            continue
        if any(part in _SCAN_SKIP_DIRS for part in rel_parts):
            continue
        protected, pairs = scan_protected_state(path)
        if not protected:
            continue
        # Extract a first-paragraph excerpt of the why block.
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        why_excerpt = _first_why_line(content)
        results.append(
            {
                "path": "/".join(rel_parts),
                "pair_files": pairs,
                "why_excerpt": why_excerpt,
            },
        )
    results.sort(key=lambda r: r["path"])
    return results


def _first_why_line(content: str) -> str:
    """Pull the first non-fence line after the sentinel from the header."""
    from .protected_file import PROTECTION_SENTINEL

    lines = content.splitlines()[:20]
    sentinel_idx = next(
        (i for i, ln in enumerate(lines) if PROTECTION_SENTINEL in ln),
        None,
    )
    if sentinel_idx is None:
        return ""
    for ln in lines[sentinel_idx + 1 :]:
        stripped = ln.strip().strip("/*#@- \t")
        # Skip fence chars.
        if not stripped or set(stripped) <= set("═─-=_* "):
            continue
        return stripped[:120]
    return ""


def protect_files(
    project_root: Path,
    paths: list[str],
    *,
    why: str = "",
    pair_files: list[str] | None = None,
) -> dict[str, object]:
    """Batch-protect multiple files. Each path must have an active
    grant; ungranted paths are refused with the per-file reason but
    don't abort the whole batch.
    """
    results: list[dict[str, object]] = []
    protected_count = 0
    refused: list[str] = []

    for path in paths:
        r = protect_file(project_root, path, why=why, pair_files=pair_files)
        results.append(r)
        if r.get("success"):
            if r.get("status") == "protected":
                protected_count += 1
        else:
            refused.append(_canonical(path))

    return {
        "success": protected_count > 0 or not refused,
        "protected_count": protected_count,
        "refused": refused,
        "results": results,
    }
