"""Line-based file operations with strong guardrails.

Three operations:
    get_lines  - Read specific lines from any file (fast, no index needed)
    edit_lines - Replace a line range with new content, with optional content verification
    batch_edit - Apply multiple edits atomically across one or more files

All operations are line-based (1-indexed) and file-type agnostic.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

from .config import get_setting
from .config_schema import (
    ConfigEditMode,
)


# Mojibake = double-encoded UTF-8 (text decoded with the wrong codec, e.g.
# UTF-8 bytes read as cp1252/Latin-1). It lands when an agent round-trips a
# file through a non-UTF-8 layer: '—' → 'â€"', ''' → 'â€™', '⚠️' → 'âš ï¸',
# 'é' → 'Ã©'. These multi-char signatures are essentially never legitimate
# in source, so refusing a write that contains them stops corruption at the
# edit boundary (2026-06-11, operator). Language-agnostic — runs on EVERY
# file type, unlike the per-language syntax checks.
_MOJIBAKE_SIGNATURES: tuple[str, ...] = (
    "â€",  # â€™ â€œ â€ â€" â€" â€¦ — smart quotes / dashes / ellipsis
    "â‚¬",  # € double-encoded
    "Ã¢â‚¬",  # alternate encoding of the above
    "Ã©", "Ã¨", "Ã ", "Ã¢", "Ã´", "Ã»", "Ã§", "Ã®", "Ã¯", "Ã«", "Ã¼", "Ã±",
    "Â\xa0",  # mojibake non-breaking space
    "Â°", "Â©", "Â®", "Â£", "Â§", "Â±",  # symbols with a stray Â
    "ðŸ",  # mangled emoji (ðŸ˜€ …)
    "âš",  # mangled ⚠/symbol emoji (âš ï¸)
    "â„",  # mangled ℹ/™ symbols
    "ï¸",  # mangled variation selector (U+FE0F)
    "�",  # U+FFFD replacement char — a prior decode already failed
)


def _detect_mojibake(text: str, path: str) -> str | None:
    """Return an error naming the first mojibake signature, or None if clean.

    Refusal, not auto-repair: silently rewriting bytes in a security
    codebase could corrupt legitimate content. The writer re-emits clean
    UTF-8 once told exactly what tripped.
    """
    # Self-exemption: THIS module DEFINES the signatures, so the detector's own
    # file is the one place these byte sequences are legitimate. Exempt only this
    # exact file on disk; every other file (even one named file_ops.py) stays
    # fully screened.
    try:
        if Path(path).resolve() == Path(__file__).resolve():
            return None
    except (OSError, ValueError):
        pass
    for sig in _MOJIBAKE_SIGNATURES:
        idx = text.find(sig)
        if idx != -1:
            line = text.count("\n", 0, idx) + 1
            snippet = text[max(0, idx - 20):idx + 20].replace("\n", "\\n")
            return (
                f"Mojibake (double-encoded UTF-8) in {path} at line {line}: "
                f"found {sig!r} in '…{snippet}…'. The file was likely decoded "
                f"with the wrong codec somewhere. Re-emit the content as clean "
                f"UTF-8 (e.g. '—' not 'â€\"', ''' not 'â€™')."
            )
    return None


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
BATCH_CONFIRM_THRESHOLD = 10  # Batches above this require large_batch_confirm=True

# Files that should never be edited by agents
SENSITIVE_PATTERNS = (".env", "credentials", "secrets", ".key", ".pem", ".pfx")

# AIDOCS gate-state paths. Writes here disarm the managed-mode gate itself
# (red-team 2026-04-17 finding P0). Protected regardless of dev_mode; the
# only escape hatch is an explicit config_edit_mode="explicit_user_permitted"
# elevation token, which requires the human operator's intent.
GATE_CONFIG_PREFIXES = (
    ".memory/config/",
    ".memory/.aidocs/",
)

# Read-side denylist: paths the managed-read tools refuse even for reads.
# - `.git/**` → minor recon surface (remote URL, refs). Use `git_ops` or
#   dedicated tools if you actually need git metadata.
# - gate-config paths → reads would echo the managed-mode state; the human
#   should use `mode_get` or the dashboard instead.
READ_DENYLIST_PREFIXES = (
    ".git/",
    ".memory/config/",  # managed-mode state — use mode_get, not raw read
)

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


def _is_blocked_system_dir(lower_str: str) -> bool:
    """True when a (lowercased, forward-slash) path is a system directory that is
    NEVER a valid project root.

    Carve-out (king 2026-06-20, WebMCP M-tier fix): a registered TENANT project root
    lives under the designated gate-root workspace at
    .../gate-root/tenants/<org>/projects/<project> — UNDER /opt, which the broad /opt
    block would otherwise reject, killing M-tier editing from WebMCP. That tree is the
    operator's editable workspace, not a system dir. The carve-out is /opt-SCOPED and
    requires both an <org> AND a <project> segment, so: a spoofed /etc/gate-root/...
    stays blocked, the tenants/<org>/projects PARENT dirs stay blocked, and crucially
    the marker-bearing custody tree (/opt/aidocs/custody/...) + gate internals stay
    blocked — markers / containment / the project registry CANNOT distinguish those
    from tenant projects (all are deep git repos under /opt; record_project registers
    everything touched), so only the path ROLE can. Pure string logic; unit-tested in
    test_file_ops_tenant_allowlist.py.
    """
    import re

    norm = (lower_str or "").rstrip("/") or "/"
    is_tenant = bool(re.search(r"/gate-root/tenants/[^/]+/projects/[^/]+", norm))
    for blocked in BLOCKED_ROOTS:
        if norm == blocked or norm.startswith(blocked + "/"):
            if blocked == "/opt" and is_tenant:
                return False
            return True
    return False


# The MCP server's own source — agents cannot edit the tool they're running on.
# (kept for back-compat; extended below to cover the WHOLE AIDOCS source repo)
_SELF_DIR: str = str(Path(__file__).resolve().parent).replace("\\", "/").lower()


def _is_aidocs_source_repo(project_root: Path) -> bool:
    """True when project_root IS the AIDOCS source repo (not just a
    project that uses AIDOCS). Detection: presence of the Python package
    subdir, recognized whether the project is bound at the OUTER root
    (…/AIDOCS → mcp/server/aidocs_mcp) OR at the mcp/ subdir
    (…/AIDOCS/mcp → server/aidocs_mcp). The package, .venv, and pyproject
    all live in mcp/, so a dev binding there is natural and must not lose
    self-edit authority (2026-06-13). On a released install is_dev_flavor
    is False regardless, so widening detection cannot relax a release.
    Same predicate the dashboard uses to surface dev.* settings.
    """
    try:
        return (
            (project_root / "mcp" / "server" / "aidocs_mcp").is_dir()
            or (project_root / "server" / "aidocs_mcp").is_dir()
        )
    except OSError:
        return False


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
    # Doctrine 2026-05-29 (king triage, clean-VPS Gate 2b): detect
    # Windows-style absolute paths (drive letter or UNC root) BEFORE
    # the Path.resolve() step. On POSIX, Path("C:/Windows").resolve()
    # joins against cwd to produce "/<cwd>/C:/Windows" — the resulting
    # lower_str then starts with "/home/..." and never matches any
    # entry in BLOCKED_ROOTS, so the function falls through to the
    # is_dir() check and raises "Project root does not exist" instead
    # of the doctrinally-correct "Refusing to operate on system
    # directory". test_system_directory_blocked_for_{read,edit,batch}
    # pins this contract on every platform.
    # Fix: classify any drive-letter-prefixed or UNC-prefixed input as
    # a system-directory access attempt up front; lowercase + forward-
    # slash normalize for BLOCKED_ROOTS membership; raise the system-
    # directory error regardless of whether the prefix happens to be
    # in the allowlist (Windows-absolute paths have no legitimate role
    # as a project root on a POSIX VPS).
    # POSIX-only gate — on Windows, drive-letter and UNC inputs are
    # legitimate absolute paths and the regular is_dir() + BLOCKED_ROOTS
    # checks below handle them. The gate fires on POSIX where pathlib
    # silently joins them under cwd.
    if os.name == "posix":
        _raw_root_str = str(project_root)
        _is_windows_absolute = (
            len(_raw_root_str) >= 2 and _raw_root_str[1] == ":" and _raw_root_str[0].isalpha()
        ) or _raw_root_str.startswith("\\\\")
        if _is_windows_absolute:
            raise ValueError(
                f"Refusing to operate on system directory: {project_root}. "
                f"File operations are restricted to project directories.",
            )

    resolved = project_root.resolve()
    lower_str = str(resolved).replace("\\", "/").lower()

    # Block system directories (always, read or write). Tenant project roots under the
    # gate-root workspace are carved out of the /opt block (see _is_blocked_system_dir);
    # custody, gate internals, and bare system roots stay forbidden.
    if _is_blocked_system_dir(lower_str):
        raise ValueError(
            f"Refusing to operate on system directory: {resolved}. "
            f"File operations are restricted to project directories.",
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
            f"No project markers found ({', '.join(PROJECT_MARKERS[:5])}, ...).",
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
    # Reject absolute paths — only relative paths allowed.
    # Doctrine 2026-05-29 (king triage, clean-VPS Gate 2b): also
    # reject Windows-style absolute paths (drive letter, UNC root,
    # backslash separator). os.path.isabs() on POSIX does NOT
    # recognize "C:/Windows/System32/config/SAM" as absolute — it
    # falls through and gets joined under project_root, raising the
    # wrong error class. test_absolute_path_blocked pins this on
    # POSIX. The same is_windows_absolute() shape is in
    # _validate_project_root above; both gates share the doctrine
    # that backslash + drive-letter + UNC inputs are illegitimate at
    # any AIDOCS path-accepting entry point.
    # POSIX-only Windows-absolute detection (drive letter + UNC).
    # Plain backslash separators in a relative path (src\\foo.py) are
    # NOT rejected here — the .replace('\\','/') normalize a few lines
    # down + the ".." in split('/') traversal check below together
    # handle them safely. Only inputs that are absolute in WINDOWS
    # semantics get rejected here; on Windows os.path.isabs catches
    # them via the existing branch.
    _is_win_abs = os.name == "posix" and (
        (len(relative_path) >= 2 and relative_path[1] == ":" and relative_path[0].isalpha())
        or relative_path.startswith("\\\\")
    )
    if os.path.isabs(relative_path) or _is_win_abs:
        raise ValueError(
            f"Absolute paths are not allowed: {relative_path}. "
            f"Use a path relative to the project root.",
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
                f"Edit manually or via the installer.",
            )
        if abs_path.name.lower() == "aidocs.toml":
            raise ValueError(
                f"aidocs.toml is deprecated: {relative_path}. "
                f"Use the AIDOCS Dashboard to manage settings (SQLite config store).",
            )

        # Gate-config protection: writes to .MEMORY/config/** or .MEMORY/.aidocs/**
        # disarm the managed-mode gate itself. Require explicit elevation token.
        # (red-team 2026-04-17 P0 finding)
        rel_lower = clean.lower()
        if any(rel_lower.startswith(p) for p in GATE_CONFIG_PREFIXES):
            if config_edit_mode != "explicit_user_permitted":
                raise ValueError(
                    f"Protected gate-config path: {relative_path}. "
                    f"Edits to .MEMORY/config/** and .MEMORY/.aidocs/** require "
                    f'explicit elevation (config_edit_mode="explicit_user_permitted"). '
                    f"Use the AIDOCS Dashboard or CLI to change managed-mode state.",
                )

        # Hard-protected DATA tier (2026-06-13): project sqlite DBs, the
        # AIDOCS index, and gate-state JSON — anywhere in the project. These
        # are owned by the tooling and must never be hand-written by an edit
        # tool. sqlite is config_set-only (no override); the rest need the
        # admin-held security.hard_protected authority (agents always denied).
        from .config import GATE_HARD_PROTECTED_PATTERNS
        from .hard_protected_paths import is_hard_protected

        if is_hard_protected(clean, extra_patterns=GATE_HARD_PROTECTED_PATTERNS):
            base_lower = clean.rsplit("/", 1)[-1].lower()
            if ".sqlite3" in base_lower:
                raise ValueError(
                    f"Project sqlite database is config_set-only: {relative_path}. "
                    f"It is never editable by a direct file write — use config_set "
                    f"(the canonical store). No override unlocks this.",
                )
            from .enforcement import hard_protected_edit_authorized

            if not hard_protected_edit_authorized(project_root):
                raise ValueError(
                    f"Hard-protected data file: {relative_path}. Editing the AIDOCS "
                    f"index / gate-state requires the admin-held "
                    f"security.hard_protected authority (role check + escalation).",
                )

        # Self-edit protection: block writes anywhere in the AIDOCS
        # source repo unless dev_mode is on. King directive 2026-05-03:
        # extended from mcp/server/aidocs_mcp/ only to the WHOLE AIDOCS
        # root (apps/, core/, scripts/, intent_tokens/, gate_messages/,
        # etc.) so the next pentest-conductor cannot break out via an
        # unprotected sibling tree.
        # Exceptions: .MEMORY/* paths have their own gates (GATE_CONFIG_
        # PREFIXES above + DNT sentinel below) so session work flows
        # without dev_mode. Outside the AIDOCS source repo this branch
        # is a no-op (no .MEMORY override needed in user projects).
        rel_lower_self = clean.lower().replace("\\", "/")
        in_aidocs_source = _is_aidocs_source_repo(project_root)
        is_memory_path = rel_lower_self.startswith(".memory/") or rel_lower_self == ".memory"
        # The "whole AIDOCS root" fence fires ONLY for the LIVE install (the tree
        # the RUNNING gate package, _SELF_DIR, executes from). An imported project
        # that merely has the AIDOCS layout (a cloned AIDOCS_PRIVATE, or an ADB
        # builder project on the VPS) is NOT the live install, so it stays
        # editable. The running package itself (_SELF_DIR) is write-locked always.
        try:
            _root_abs = str(Path(project_root).resolve()).replace("\\", "/").lower()
        except (OSError, ValueError):
            _root_abs = ""
        in_live_install = bool(_root_abs) and (
            _SELF_DIR == _root_abs or _SELF_DIR.startswith(_root_abs + "/")
        )
        protected_self = (
            abs_lower.startswith(_SELF_DIR + "/")
            or abs_lower == _SELF_DIR
            or (in_aidocs_source and in_live_install and not is_memory_path)
        )
        if protected_self:
            # Self-edit authority is DEV-flavour-derived (2026-06-12): a
            # write to AIDOCS source is permitted ONLY when the install is
            # a DEV-flavour build AND project_root IS the canonical AIDOCS
            # source repo. There is no `dev.dev_mode` config flag and no
            # caller-privilege gate: on a contributor build EVERY agent
            # (conductor or spawned worker) may edit the source — that is
            # the dev workflow. On any other install nobody can.
            from .enforcement import dev_mode_authorized

            if not dev_mode_authorized(project_root):
                scope_msg = (
                    "AIDOCS source repo (entire project root)"
                    if in_aidocs_source and not abs_lower.startswith(_SELF_DIR + "/")
                    else "AIDOCS MCP server source"
                )
                raise ValueError(
                    f"AIDOCS self-edit blocked: {relative_path}. "
                    f"Path is in {scope_msg}; self-edit requires a "
                    f"DEV-flavour AIDOCS source build.",
                )

        # DO NOT TOUCH gate — protected files are edit-locked for sub-agents
        # without explicit grant + forced pair-file reads. Conductor can
        # edit freely. Sentinel removal is itself a protected op.
        # Spec: .MEMORY/sessions/2026-04-13-repo-and-dashboard-audit/plans/do-not-touch-gate-spec.md
        _check_protected_file(abs_path, clean, project_root)

    return abs_path


def _check_sensitive(path: str) -> None:
    """Block edits to sensitive files (credentials, keys, env)."""
    lower = path.lower()
    for pattern in SENSITIVE_PATTERNS:
        if pattern in lower:
            raise ValueError(
                f"Refusing to operate on potentially sensitive file: {path}. "
                f"Matched pattern: {pattern}",
            )


def _ensure_pair_files_read(
    project_root: Path,
    protected_path: str,
    pair_paths: list[str],
    read_tracker: list[str] | set[str] | tuple[str, ...],
) -> None:
    """Refuse an edit to a protected file until every pair file named in
    its sentinel header has been read this turn.

    The `Pair files:` block lists sibling modules that must be understood
    as a group. Skipping them risks half-informed edits that violate an
    invariant held across the pair. The per-turn read tracker
    (`get_turn_read_files`) records every file a caller has opened via
    ai_get_lines; this helper refuses the write when any entry is
    missing.

    `project_root` is accepted for symmetry with the sibling gate helpers
    even though this check is purely against the declared pair paths and
    the turn read set — both already rel-path-normalized.
    """
    if not pair_paths:
        return
    reads = {str(p).replace("\\", "/").strip() for p in read_tracker if p}
    missing = [p for p in pair_paths if p.replace("\\", "/").strip() not in reads]
    if missing:
        raise ValueError(
            f"protected file {protected_path}: pair-file context missing — read its "
            f"header's pair files first ({', '.join(missing)}) via ai_get_lines, then retry.",
        )


def _check_protected_file(abs_path: Path, relative_path: str, project_root: Path) -> None:
    """DO NOT TOUCH gate — refuse writes to protected files when the caller
    is a sub-agent without an active grant, and refuse sentinel-removal
    edits even from the conductor.

    Order of enforcement for a protected file:
      1. Force-read every declared pair file (applies to every caller).
      2. Sub-agent callers must additionally hold a per-turn grant.

    Called from `_resolve_path(write=True)` AFTER self-edit and gate-config
    checks. Reads are unaffected (only write paths reach this function).
    """
    if not abs_path.is_file():
        # Non-existing files can't be protected — ai_create_file is
        # deliberately exempt (can't protect a file that doesn't exist).
        return

    from .protected_file import has_protection_sentinel, parse_pair_files
    from .protected_file_runtime import (
        get_protected_edit_grants as _legacy_local_edit_grants,
    )
    from .protected_file_runtime import (
        get_turn_read_files,
        is_sub_agent_call,
    )

    def _edit_grants_cross_process() -> set[str]:
        """Sqlite-backed grants (set by claude_hook in CC's hook
        subprocess) unioned with module-level legacy grants. The
        module-level path can't cross process boundaries — #236 2026-05-12.
        """
        sqlite_grants: list[str] = []
        try:
            from .runtime_bootstrap_service import get_runtime

            runtime = get_runtime()
            managed = runtime.hub.managed_mode.get_mode(project_root)
            if managed.get("active"):
                sid = str(managed.get("session_id") or "").strip()
                if sid:
                    sqlite_grants = list(
                        runtime.hub.query_gate.get_protected_edit_grants(
                            project_root,
                            sid,
                        ),
                    )
        except Exception:
            sqlite_grants = []
        return {
            str(g).replace("\\", "/").strip()
            for g in list(sqlite_grants) + list(_legacy_local_edit_grants())
            if g
        }

    try:
        content = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    if not has_protection_sentinel(content):
        # File is not protected — the sentinel-removal check doesn't apply.
        return

    norm_rel = relative_path.replace("\\", "/")
    pairs = parse_pair_files(content)

    # Pair-file context is mandatory for sub-agents only. The conductor
    # has full trust and owns the session-wide context; forcing a pair-read
    # on conductor edits would break legitimate refactors. Runs before the
    # sub-agent grant check so a caller missing context gets the actionable
    # pair-list message instead of a grant-denial that hides the cause.
    if is_sub_agent_call():
        _ensure_pair_files_read(project_root, norm_rel, pairs, get_turn_read_files())
        grants = _edit_grants_cross_process()
        if norm_rel not in grants and "*" not in grants:
            pair_hint = ", ".join(pairs) if pairs else "(none declared)"
            raise ValueError(
                f"🛑 PROTECTED FILE: {norm_rel}. This file is marked "
                f"DO NOT TOUCH WITHOUT EXPLICIT USER REQUEST. You are a "
                f"sub-agent without an active grant for this file. Pair "
                f"files that must be understood as a group: {pair_hint}. "
                f"Report your finding back to the conductor — they cannot "
                f"grant access on their own; only the user's current "
                f"message can unlock this file via a phrase like "
                f"'override DO NOT TOUCH for {norm_rel}'.",
            )


def _would_remove_sentinel(abs_path: Path, new_content: str) -> bool:
    """Return True iff replacing abs_path with new_content would strip
    the DO NOT TOUCH sentinel from a previously-protected file.

    Used by str_replace / edit_lines / batch edits to detect (and refuse)
    attempts to silently un-protect a file by deleting its header before
    editing the body.
    """
    if not abs_path.is_file():
        return False
    from .protected_file import has_protection_sentinel

    try:
        old = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    was_protected = has_protection_sentinel(old)
    if not was_protected:
        return False
    will_be_protected = has_protection_sentinel(new_content)
    return not will_be_protected


def _strip_razor_comments_and_strings(text: str) -> str:
    """Best-effort scrub of Razor comments, string literals, and inline
    <script>/<style> bodies so brace-counting doesn't false-positive on
    `{` / `}` inside them. Used by _check_razor_syntax. Conservative:
    when in doubt, strip more aggressively (false-negatives in brace-
    balance are preferable to false-positives that refuse legitimate
    edits).

    Strips:
      - @* ... *@ Razor comments
      - <!-- ... --> HTML comments
      - <script ...>...</script> bodies (JS uses {} for blocks/objects)
      - <style ...>...</style> bodies (CSS uses {} for selectors)
      - "..." double-quoted strings (with backslash-escape)
      - '...' single-quoted strings (with backslash-escape)

    The script/style scrub strips ONLY the body (between the open and
    close tags), not the tags themselves — so the surrounding HTML
    structure is preserved.

    Preserves line structure (replaces with spaces) so error line
    numbers from downstream parsers still make sense.
    """
    import re as _re

    chars = list(text)

    def _wipe_range(start: int, end: int) -> None:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "

    # Strip in order: comments first (so script/style tags inside
    # comments don't confuse the body scrubber), then script/style
    # bodies, then strings (which still appear inside Razor C# blocks
    # we DO want to brace-count, so do them last on whatever remains).
    rebuild = "".join(chars)
    for pattern, flags in [
        (r"@\*.*?\*@", _re.DOTALL),
        (r"<!--.*?-->", _re.DOTALL),
    ]:
        for m in _re.finditer(pattern, rebuild, flags):
            _wipe_range(m.start(), m.end())
        rebuild = "".join(chars)

    # <style>...</style> bodies are pure CSS — wipe the whole body.
    # CSS has no Razor preprocessing in practice (Razor doesn't expand
    # @ inside <style> the way it does in <script>; CSS @media/@import
    # use @ for their own grammar, which would false-positive any
    # transition detector anyway).
    pattern = (
        r"(<\s*style\b[^>]*>)"
        r"(.*?)"
        r"(<\s*/\s*style\s*>)"
    )
    for m in _re.finditer(pattern, rebuild, _re.DOTALL | _re.IGNORECASE):
        _wipe_range(m.start(2), m.end(2))
    rebuild = "".join(chars)

    # <script>...</script> bodies are mixed Razor + JS. Wipe ONLY the
    # JS-only spans, leave Razor transitions intact so the brace
    # counter still sees them. Common patterns to preserve:
    #   @{ ... }                        — code blocks
    #   @if (...) { ... }               — control flow (and @for, @foreach, @while, @switch)
    #   @Model.X / @ViewBag.X / @JsT(...) / @Html.Raw(...) — expressions
    #   @* ... *@                       — comments (already stripped above)
    # Implementation: find spans of Razor transitions inside each
    # script body via a scanner; everything else inside the body gets
    # wiped. Conservative: only the JS-only parts lose their braces;
    # mixed false-positives degrade to "leave intact" rather than
    # "wipe legit Razor."
    pattern = (
        r"(<\s*script\b[^>]*>)"
        r"(.*?)"
        r"(<\s*/\s*script\s*>)"
    )
    for m in _re.finditer(pattern, rebuild, _re.DOTALL | _re.IGNORECASE):
        body_start, body_end = m.start(2), m.end(2)
        body = rebuild[body_start:body_end]
        # Find Razor transition spans inside the body. Their absolute
        # positions get added to the keep-list; everything else gets
        # wiped.
        keep_ranges: list[tuple[int, int]] = []
        i = 0
        n = len(body)
        while i < n:
            if body[i] == "@":
                if i + 1 < n and body[i + 1] == "{":
                    # @{ ... } — depth-counted block.
                    depth = 1
                    j = i + 2
                    while j < n and depth > 0:
                        if body[j] == "{":
                            depth += 1
                        elif body[j] == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        j += 1
                    keep_ranges.append((i, j + 1))
                    i = j + 1
                    continue
                # @if/@for/@foreach/@while/@switch followed by ( ... ) { ... }
                kw_match = _re.match(
                    r"@(if|for|foreach|while|switch)\b",
                    body[i:],
                )
                if kw_match:
                    end = i + kw_match.end()
                    # Skip to opening paren
                    while end < n and body[end] != "(":
                        end += 1
                    if end < n and body[end] == "(":
                        # Match parens
                        depth = 1
                        end += 1
                        while end < n and depth > 0:
                            if body[end] == "(":
                                depth += 1
                            elif body[end] == ")":
                                depth -= 1
                            end += 1
                        # Skip whitespace, then match { ... }
                        while end < n and body[end].isspace():
                            end += 1
                        if end < n and body[end] == "{":
                            depth = 1
                            end += 1
                            while end < n and depth > 0:
                                if body[end] == "{":
                                    depth += 1
                                elif body[end] == "}":
                                    depth -= 1
                                    if depth == 0:
                                        end += 1
                                        break
                                end += 1
                    keep_ranges.append((i, end))
                    i = end
                    continue
                # @expr or @Html.Raw(...) — single expression, stop at
                # whitespace / quote / non-identifier. Best-effort.
                j = i + 1
                # Eat dotted identifier path
                while j < n and (body[j].isalnum() or body[j] in "._"):
                    j += 1
                # Optional (...) call
                if j < n and body[j] == "(":
                    depth = 1
                    j += 1
                    while j < n and depth > 0:
                        if body[j] == "(":
                            depth += 1
                        elif body[j] == ")":
                            depth -= 1
                        j += 1
                keep_ranges.append((i, j))
                i = j
                continue
            i += 1
        # Wipe everything in body NOT in keep_ranges.
        keep_ranges.sort()
        cursor = 0
        for ks, ke in keep_ranges:
            if ks > cursor:
                _wipe_range(body_start + cursor, body_start + ks)
            cursor = max(cursor, ke)
        if cursor < n:
            _wipe_range(body_start + cursor, body_start + n)
    rebuild = "".join(chars)

    # String literals last (after script/style bodies are wiped).
    for pattern in (r'"(?:\\.|[^"\\])*"', r"'(?:\\.|[^'\\])*'"):
        for m in _re.finditer(pattern, rebuild):
            _wipe_range(m.start(), m.end())
        rebuild = "".join(chars)

    return "".join(chars)


def _extract_razor_code_blocks(text: str) -> list[tuple[int, str]]:
    """Extract @{ ... } code blocks from Razor source.

    Returns list of (line_number, fragment) tuples. line_number is 1-based
    and points at the line where `@{` opens; fragment is the C# body
    between the matched braces (excluding the @{ and closing }).

    Brace matching is depth-counted on the scrubbed text (comments and
    strings already removed) so braces inside string literals don't
    confuse the walker. If braces don't match, returns the partial list
    captured so far — the brace-balance Layer 1 check will independently
    flag the imbalance.
    """
    scrubbed = _strip_razor_comments_and_strings(text)
    blocks: list[tuple[int, str]] = []
    i = 0
    n = len(scrubbed)
    while i < n - 1:
        if scrubbed[i] == "@" and scrubbed[i + 1] == "{":
            line = scrubbed.count("\n", 0, i) + 1
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                ch = scrubbed[j]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth == 0:
                # Use the ORIGINAL text for the fragment (with strings
                # intact) so the C# validator sees real code.
                blocks.append((line, text[i + 2 : j]))
                i = j + 1
                continue
            # Unmatched — Layer 1 will catch the brace imbalance.
            break
        i += 1
    return blocks


_RAZOR_KW = (
    "if",
    "for",
    "foreach",
    "while",
    "switch",
    "using",
    "lock",
    "else",
    "do",
    "try",
    "catch",
    "finally",
)
_RAZOR_BLOCK_KW_WITH_PAREN = (
    "if",
    "for",
    "foreach",
    "while",
    "switch",
    "using",
    "lock",
)


def _markup_seen_since(text: str, body_start: int, pos: int) -> bool:
    """True if any HTML tag has been encountered between text[body_start]
    and text[pos]. Used by the RZ1010 check: an `@{` is only invalid
    when we're STILL in code mode (no markup transition yet). As soon
    as a `<tag>` appears, mode flips to markup, and a subsequent `@{`
    is legitimate (switches back to code).
    """
    j = body_start
    while j < pos:
        if text[j] == "<" and j + 1 < pos:
            nxt = text[j + 1]
            # Plain HTML tag: <tag, </tag, <text>
            if nxt.isalpha() or nxt == "/" or nxt == "!":
                return True
        j += 1
    return False


def _match_kw(text: str, start: int, end: int) -> str | None:
    """Match a Razor keyword starting at text[start]. Returns the
    keyword string or None. Word-boundary aware.
    """
    for kw in _RAZOR_KW:
        kw_end = start + len(kw)
        if kw_end > end:
            continue
        if text[start:kw_end] != kw:
            continue
        if kw_end == end or not (text[kw_end].isalnum() or text[kw_end] == "_"):
            return kw
    return None


def _razor_brace_balance_walk(text: str) -> str | None:
    """Razor-aware brace balance + structural rule check.

    Pre-fix this used a regex string-stripper that couldn't tell HTML
    attribute quotes from C# string quotes (3.4% false-positive rate
    on real DentalApp files).

    Walks Razor structurally, recursing into transition bodies so we
    can catch:
      - unclosed @{ }, @if{}, @for{}, @foreach{}, @while{}, @switch{}
      - unclosed @* *@ comments
      - RZ1010 — @{} nested inside an @if/@for/@foreach/@while body
        (already in code-block scope; @{} is invalid there)
      - orphan `}` standalone in markup region (RZ1006-ish)

    HTML attribute values and tag content are never counted. Razor
    expressions inside attributes (@Model.X, @(...)) are matched as
    @-transitions, not via brace counting.
    """
    return _walk_razor(text, 0, len(text), in_code_block=False, top_level=True)


def _walk_razor(
    text: str,
    start: int,
    end: int,
    *,
    in_code_block: bool,
    top_level: bool,
) -> str | None:
    """Walk text[start:end] looking for Razor transitions.

    in_code_block=True means caller is inside a Razor C# region
    (@if/@for/@foreach/@while/@{} body). Inside, encountering @{ is
    RZ1010 — already in code scope, the @{} wrapper is invalid.

    top_level means we're at the file root. Used to flag standalone
    `}` in markup region (RZ1006-ish).
    """
    i = start
    while i < end:
        ch = text[i]

        # @* ... *@ Razor comment
        if ch == "@" and i + 1 < end and text[i + 1] == "*":
            close = text.find("*@", i + 2)
            if close == -1 or close >= end:
                line = text.count("\n", 0, i) + 1
                return (
                    f"Edit would produce invalid Razor — unclosed @* *@ "
                    f"comment starting at line {line}"
                )
            i = close + 2
            continue

        # @{ ... } code block
        if ch == "@" and i + 1 < end and text[i + 1] == "{":
            start_line = text.count("\n", 0, i) + 1
            # RZ1010: @{ } nested inside an OPEN code-block body —
            # but ONLY if we're still in code mode (no HTML tag has
            # been encountered since entering the body). Razor flips
            # to markup mode when it sees `<tag>`; @{ inside a markup
            # span is legitimate (it switches back to code).
            if in_code_block and not _markup_seen_since(text, start, i):
                return (
                    f"Edit would produce invalid Razor — RZ1010 at line "
                    f"{start_line}: `@{{` nested inside @if/@for/@foreach/"
                    f"@while/@{{}} body without intervening markup. "
                    f"You're still in code-block scope; drop the `@{{` "
                    f"wrapper."
                )
            depth = 1
            j = i + 2
            body_start = j
            while j < end and depth > 0:
                c = text[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                return (
                    f"Edit would produce invalid Razor — unclosed @{{ }} "
                    f"block starting at line {start_line}"
                )
            inner = _walk_razor(
                text,
                body_start,
                j,
                in_code_block=True,
                top_level=False,
            )
            if inner is not None:
                return inner
            i = j + 1
            continue

        # @if/@for/@foreach/@while/@switch/@using/@lock and friends
        if ch == "@":
            kw = _match_kw(text, i + 1, end)
            if kw and kw in _RAZOR_BLOCK_KW_WITH_PAREN:
                start_line = text.count("\n", 0, i) + 1
                j = i + 1 + len(kw)
                while j < end and text[j].isspace():
                    j += 1
                if j < end and text[j] == "(":
                    depth = 1
                    j += 1
                    while j < end and depth > 0:
                        if text[j] == "(":
                            depth += 1
                        elif text[j] == ")":
                            depth -= 1
                        j += 1
                while j < end and text[j].isspace():
                    j += 1
                if j < end and text[j] == "{":
                    depth = 1
                    j += 1
                    body_start = j
                    while j < end and depth > 0:
                        c = text[j]
                        if c == "{":
                            depth += 1
                        elif c == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        j += 1
                    if depth != 0:
                        return (
                            f"Edit would produce invalid Razor — unclosed "
                            f"@{kw} {{ }} block starting at line {start_line}"
                        )
                    inner = _walk_razor(
                        text,
                        body_start,
                        j,
                        in_code_block=True,
                        top_level=False,
                    )
                    if inner is not None:
                        return inner
                    i = j + 1
                    # Razor flow-control continuations (else/catch/
                    # finally) appear AFTER the if-body without an `@`
                    # prefix. Consume any chained continuations so we
                    # don't mistake their bodies for markup.
                    while i < end:
                        k = i
                        while k < end and text[k].isspace():
                            k += 1
                        cont = None
                        for kw_cont in ("else if", "else", "catch", "finally"):
                            kw_end = k + len(kw_cont)
                            if kw_end > end:
                                continue
                            if text[k:kw_end] != kw_cont:
                                continue
                            if kw_end == end or not (text[kw_end].isalnum() or text[kw_end] == "_"):
                                cont = kw_cont
                                break
                        if cont is None:
                            break
                        m = k + len(cont)
                        while m < end and text[m].isspace():
                            m += 1
                        if m < end and text[m] == "(":
                            depth = 1
                            m += 1
                            while m < end and depth > 0:
                                if text[m] == "(":
                                    depth += 1
                                elif text[m] == ")":
                                    depth -= 1
                                m += 1
                        while m < end and text[m].isspace():
                            m += 1
                        if m < end and text[m] == "{":
                            depth = 1
                            m += 1
                            cont_body_start = m
                            while m < end and depth > 0:
                                c = text[m]
                                if c == "{":
                                    depth += 1
                                elif c == "}":
                                    depth -= 1
                                    if depth == 0:
                                        break
                                m += 1
                            if depth != 0:
                                cont_line = text.count("\n", 0, k) + 1
                                return (
                                    f"Edit would produce invalid Razor — "
                                    f"unclosed {cont} {{ }} block at line "
                                    f"{cont_line}"
                                )
                            inner = _walk_razor(
                                text,
                                cont_body_start,
                                m,
                                in_code_block=True,
                                top_level=False,
                            )
                            if inner is not None:
                                return inner
                            i = m + 1
                        else:
                            i = m
                    continue
                # No body (e.g. @using directive) — accepted.
                i = j
                continue
            # @else / @do / @try / @catch / @finally — keyword without
            # paren; consume and let next iteration find any { body.
            if kw and kw in _RAZOR_KW:
                i = i + 1 + len(kw)
                continue
            # @expr — dotted identifier + optional ( ... )
            j = i + 1
            while j < end and (text[j].isalnum() or text[j] in "._"):
                j += 1
            if j < end and text[j] == "(":
                depth = 1
                j += 1
                while j < end and depth > 0:
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                    j += 1
            i = j
            continue

        # Orphan-brace detection (RZ1006-ish for BONUS_FAILING) was
        # tried here but unconditionally false-positives on real Razor
        # patterns we can't structurally distinguish: JS-only .cshtml
        # partials (where the file IS a <script> body included by a
        # parent), CSS-in-template, JS placeholder text. Without
        # semantic Razor compilation the rule can't tell legitimate
        # `}` from orphan. Deferred — see backlog item filed for the
        # next iteration when a proper Razor source-generator pass
        # lands. RZ1010 (the operator-named trap) IS caught reliably
        # by the structural recursion, which is the bigger win.

        # Skip <style>...</style> bodies — CSS uses { } extensively
        # and has its own @media/@import grammar that confuses the
        # Razor scanner.
        if ch == "<" and not in_code_block:
            tag_match = None
            for tag in ("style", "script"):
                tag_open = f"<{tag}"
                if text[i : i + len(tag_open)].lower() == tag_open:
                    tag_match = tag
                    break
            if tag_match:
                # Find end of opening tag `>`
                gt = text.find(">", i)
                if gt != -1 and gt < end:
                    close_tag = f"</{tag_match}"
                    close_pos = text.lower().find(close_tag, gt + 1)
                    if close_pos != -1 and close_pos < end:
                        # Skip the entire body (jump past `</tag>`)
                        end_close = text.find(">", close_pos)
                        if end_close != -1 and end_close < end:
                            i = end_close + 1
                            continue

        i += 1

    return None


def _check_razor_syntax(text: str) -> str | None:
    """Razor (.cshtml) validation. Returns error string or None.

    Two layers:
      L1: Razor-aware brace balance walker (only counts braces inside
          Razor transitions; ignores HTML markup and attributes).
          Catches unclosed @if/@foreach/@switch/@{} blocks.
      L2: extract @{ ... } code blocks and validate each as a C#
          method body via csharp_edit_validator. Catches missing
          semicolons, bad expressions, etc. inside Razor code blocks.

    Skipped intentionally:
      - HTML structure (Razor is permissive)
      - tag helpers (.NET-build-time territory)
    """
    # Layer 1 — Razor-aware brace balance.
    l1_err = _razor_brace_balance_walk(text)
    if l1_err is not None:
        return l1_err

    # Layer 2 — validate each @{ ... } block as a C# method body.
    blocks = _extract_razor_code_blocks(text)
    if not blocks:
        return None
    try:
        from .csharp_edit_validator import validate_csharp_content
    except ImportError:
        # tree-sitter-c-sharp is a hard dep per #52; this branch is
        # defensive only. Per #57 we fail closed when the validator
        # itself can't load.
        return (
            "Edit would produce unvalidatable Razor — csharp_edit_validator "
            "import failed; refusing to fail-open per #57."
        )
    for line_num, fragment in blocks:
        # Wrap fragment in a synthetic class+method so the C# validator
        # sees a complete compilation unit. Top-of-file scaffolding adds
        # 3 lines; subtract from any error line to map back to .cshtml.
        SCAFFOLD_PREFIX_LINES = 3
        wrapped = (
            f"namespace _AidocsRazorScaffold;\nclass _F {{\n  void _M() {{\n{fragment}\n  }}\n}}\n"
        )
        result = validate_csharp_content(wrapped)
        if not result.get("ok"):
            errs = result.get("error_nodes") or []
            if errs:
                first = errs[0]
                inner_line = first.get("line", 1)
                # Map scaffold line back to .cshtml line.
                cshtml_line = max(
                    line_num,
                    line_num + (int(inner_line) - SCAFFOLD_PREFIX_LINES),
                )
                return (
                    f"Edit would produce invalid Razor — C# error in "
                    f"@{{ }} block at .cshtml line ~{cshtml_line}: "
                    f"unexpected `{(first.get('text') or '').strip()[:80]}`"
                )
            return "Edit would produce invalid Razor — C# error in @{ } block"

    return None


def _check_syntax(abs_path: Path, text: str) -> str | None:
    """Validate an edited file before write. Mojibake screen runs on ALL
    file types (encoding integrity is language-agnostic); the syntax
    screens below are per-language."""
    mojibake_err = _detect_mojibake(text, str(abs_path))
    if mojibake_err:
        return mojibake_err
    suffix = abs_path.suffix.lower()

    if suffix in {".js", ".cjs", ".mjs"}:
        tmp_suffix = suffix if suffix in {".cjs", ".mjs"} else ".js"
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                suffix=tmp_suffix,
                delete=False,
            ) as handle:
                handle.write(text)
                tmp_path = Path(handle.name)
            proc = subprocess.run(
                ["node", "--check", str(tmp_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                return None
            detail = (proc.stderr or proc.stdout or "").strip()
            # ONLY a genuine parse failure (node prints a SyntaxError) is an
            # invalid-JS verdict. A node CRASH — OOM / killed / empty output,
            # e.g. node v24 failing to reserve its V8 address space inside a
            # constrained request subprocess — is NOT a syntax verdict and must
            # NOT false-block a valid edit. Fall through to the tree-sitter check
            # below instead. (Reproduced via the real /v1/mcp endpoint: a trivially
            # valid `console.log('ok')` .js was refused because node exited
            # non-zero with empty stderr — a crash, not a SyntaxError.)
            if "SyntaxError" in detail:
                return f"Edit would produce invalid JavaScript — {detail}"
            # node could not actually validate — defer to tree-sitter.
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    # Try tree-sitter next — covers JS/TS/JSON and other supported languages.
    try:
        from .tree_sitter_service import check_syntax as ts_check

        result = ts_check(abs_path, text)
        if result is not None:
            return f"Edit would produce invalid code — {result}"
    except ImportError:
        pass

    if suffix == ".py":
        import ast as _ast

        try:
            _ast.parse(text)
        except SyntaxError as e:
            return f"Edit would produce invalid Python — SyntaxError at line {e.lineno or '?'}: {e.msg or 'unknown'}"

    elif suffix == ".json":
        import json as _json

        try:
            _json.loads(text)
        except _json.JSONDecodeError as e:
            return f"Edit would produce invalid JSON — {e.msg} at line {e.lineno}, col {e.colno}"

    elif suffix == ".toml":
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return None
        try:
            tomllib.loads(text)
        except Exception as e:
            return f"Edit would produce invalid TOML — {e}"

    elif suffix in (".yaml", ".yml"):
        try:
            import yaml

            yaml.safe_load(text)
        except ImportError:
            return None
        except yaml.YAMLError as e:
            return f"Edit would produce invalid YAML — {e}"

    elif suffix in (".xml", ".html", ".csproj", ".resx", ".config"):
        import xml.etree.ElementTree as _ET

        try:
            _ET.fromstring(text)
        except _ET.ParseError as e:
            return f"Edit would produce invalid XML — {e}"

    elif suffix == ".cs":
        # #66 (2026-04-27): C# AST validation via the dedicated
        # csharp_edit_validator (tree-sitter-c-sharp, hard dep per #52).
        # tree_sitter_service.check_syntax intentionally excludes .cs to
        # avoid grammar-version lag rejecting modern-but-valid code, but
        # the dedicated validator's grammar is pinned + tested against
        # C# 11/12 features (file-scoped namespace, primary constructor,
        # collection expression). Wire it in so writers actually catch
        # missing semicolons / mismatched braces / unterminated blocks.
        from .csharp_edit_validator import validate_csharp_content

        result = validate_csharp_content(text)
        if not result.get("ok"):
            errs = result.get("error_nodes") or []
            if errs:
                first = errs[0]
                return (
                    f"Edit would produce invalid C# — line "
                    f"{first.get('line', '?')}, col "
                    f"{first.get('column', '?')}: "
                    f"unexpected `{(first.get('text') or '').strip()[:80]}`"
                )
            return "Edit would produce invalid C#"

    elif suffix == ".cshtml":
        # #67 (2026-04-27): Razor validation. Two layers:
        #   L1 — brace balance across the whole file (catches unclosed
        #        @if/@foreach/@switch/@{} blocks and stray }).
        #   L2 — extract @{ ... } code blocks and validate each as C#
        #        via the #52 hardened csharp_edit_validator.
        # Deliberately skip @if(...) / @expr / @model / tag helpers —
        # those need finer parsing; defer until real bugs hit them.
        # HTML structure (unclosed tags etc.) is NOT validated — Razor
        # is permissive about that by design.
        razor_err = _check_razor_syntax(text)
        if razor_err is not None:
            return razor_err

    return None


def _read_file_lines(abs_path: Path) -> list[str]:
    """Read a file and return its lines (no trailing newline stripping)."""
    if not abs_path.is_file():
        raise FileNotFoundError(f"File not found: {abs_path}")

    size = abs_path.stat().st_size
    if size > MAX_FILE_SIZE:
        raise ValueError(f"File too large ({size:,} bytes, max {MAX_FILE_SIZE:,}): {abs_path.name}")

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
            paths.update(_diff_config_paths(before.get(key), after.get(key), next_prefix))
        return paths
    if isinstance(before, dict) or isinstance(after, dict):
        return _flatten_config_paths(before, prefix) | _flatten_config_paths(after, prefix)
    return {prefix} if before != after and prefix else set()


def _load_toml_text(text: str, *, path: str) -> dict[str, object]:
    if tomllib is None:
        raise ValueError("TOML parsing is unavailable in this runtime.")
    try:
        loaded = tomllib.loads(text or "") if text.strip() else {}
    except Exception as exc:
        raise ValueError(f"Invalid TOML for {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid TOML root for {path}: expected a table at the document root.")
    return loaded


def _validate_config_edit(
    abs_path: Path,
    *,
    current_text: str,
    updated_text: str,
    config_edit_mode: ConfigEditMode | None,
) -> None:
    """Deprecated: aidocs.toml edits are blocked at the write boundary.

    Settings now live in the SQLite config store and are managed via the
    dashboard. This function is kept as a no-op for backwards compatibility
    with callers in batch_edit / str_replace paths.
    """
    return


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
            "start": int,
            "end": int,
            "total": int,
            "content": str,          # The requested lines (with optional line numbers)
            "lines": [str],          # Raw lines without numbers
            "has_more": bool,        # True if file has more lines after end_line
            "truncated": bool,       # True if count was clamped
        }

    """
    _validate_project_root(project_root)
    # Read-side denylist: refuse `.git/**` and gate-config paths. Reads here
    # are either recon (git remote URL) or self-inspection of gate state
    # (use `mode_get`/dashboard instead). (red-team 2026-04-17 P2)
    _clean_rel = path.replace("\\", "/").lstrip("/").lower()
    for _prefix in READ_DENYLIST_PREFIXES:
        if _clean_rel.startswith(_prefix):
            raise ValueError(
                f"Protected path (read denylist): {path}. "
                f"Use dedicated tools (git_ops, mode_get, dashboard) instead.",
            )
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

    # Hidden-Unicode defense: strip tag-block / bidi / zero-width chars per line
    # and surface the total stripped count so the caller knows something was
    # cleaned. (Pillar "Rules File Backdoor" class, red-team 2026-04-17 P1.)
    from .unicode_safety import strip_hidden_unicode as _strip_hidden

    hidden_total = 0
    cleaned_selected: list[str] = []
    for line in selected:
        cleaned_line, n = _strip_hidden(line)
        hidden_total += n
        cleaned_selected.append(cleaned_line)
    selected = cleaned_selected

    if show_line_numbers:
        width = len(str(end))
        content_lines = [f"{start + i:>{width}}  {line}" for i, line in enumerate(selected)]
    else:
        content_lines = list(selected)
    content = "\n".join(content_lines)

    result: dict[str, object] = {
        "path": path,
        "start": start,
        "end": end,
        "total": total,
        "content": content,
        "lines": content_lines,
    }
    if end < total:
        result["has_more"] = True
    if truncated:
        result["truncated"] = True
    if hidden_total:
        result["hidden_unicode_stripped"] = hidden_total
    return result


# ── read_raw constants ──
# Soft cap balances "useful default" against "can't drown context in one call".
# 512KB covers most config/log/data files in one shot; larger files require
# explicit limit_bytes so the caller is forced to think about paging.
_READ_RAW_SOFT_CAP = 512 * 1024
# Hard cap is the maximum bytes the tool will EVER return in one call, even
# when the caller asks for more. 8MB is large enough for a single-page dump
# of most CSVs/logs; beyond this, byte-range pagination is mandatory.
_READ_RAW_HARD_CAP = 8 * 1024 * 1024


def read_raw(
    project_root: Path,
    path: str,
    *,
    offset_bytes: int = 0,
    limit_bytes: int | None = None,
    encoding: str = "utf-8",
) -> dict[str, object]:
    """Read a byte range of any file within the project root as text.

    Intended for non-indexed text files (logs, CSVs, config blobs, .resx,
    .csproj, etc.) that the code index does not parse. For PDFs/Excel/docx
    the Phase 2 structured-parser tools are more appropriate.

    Args:
        project_root: Project root directory.
        path: Relative path to the file.
        offset_bytes: Byte offset to start reading from (0-indexed).
        limit_bytes: Max bytes to return. Defaults to _READ_RAW_SOFT_CAP
            (512KB). Caller requests >_READ_RAW_HARD_CAP (8MB) are clamped.
        encoding: Text encoding. When decoding fails, the tool returns an
            error explaining how to retry (e.g. with encoding='latin-1').

    Returns:
        {
            "path": str,
            "total_size_bytes": int,
            "offset_bytes": int,
            "returned_bytes": int,
            "content": str | None,    # None when encoding fails
            "truncated": bool,        # True if file has more bytes past
                                      # offset_bytes + returned_bytes
            "encoding": str,
            "error": str | None,      # Set when decoding or read failed
        }

    """
    _validate_project_root(project_root)
    abs_path = _resolve_path(project_root, path)
    if not abs_path.is_file():
        return {
            "path": path,
            "total_size_bytes": 0,
            "offset_bytes": 0,
            "returned_bytes": 0,
            "content": None,
            "truncated": False,
            "encoding": encoding,
            "error": f"Not a file or does not exist: {path}",
        }

    total_size = abs_path.stat().st_size
    start = max(0, int(offset_bytes))
    if start > total_size:
        return {
            "path": path,
            "total_size_bytes": total_size,
            "offset_bytes": start,
            "returned_bytes": 0,
            "content": "",
            "truncated": False,
            "encoding": encoding,
            "error": f"offset_bytes ({start}) exceeds file size ({total_size}).",
        }

    effective_limit = _READ_RAW_SOFT_CAP if limit_bytes is None else int(limit_bytes)
    effective_limit = max(0, min(effective_limit, _READ_RAW_HARD_CAP))
    end = min(start + effective_limit, total_size)

    with abs_path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read(end - start)

    error: str | None = None
    try:
        content: str | None = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        content = None
        error = (
            f"Failed to decode {len(raw)} bytes as {encoding!r}: {exc}. "
            f"Retry with a different encoding (e.g. 'latin-1' for Windows-1252, "
            f"'utf-16' for some Windows logs)."
        )

    return {
        "path": path,
        "total_size_bytes": total_size,
        "offset_bytes": start,
        "returned_bytes": len(raw),
        "content": content,
        "truncated": end < total_size,
        "encoding": encoding,
        "error": error,
    }


def _edit_checkpoint(
    project_root: Path,
    canonical_path: str,
    reason: str,
    provenance: dict | None = None,
) -> dict:
    """Best-effort pre-mutation restore point for a governed edit.

    ADDITIVE and NON-BLOCKING: call this ONLY after every write guard has
    passed and just before the write. It never raises, never refuses the edit,
    and never relaxes a guard — it only records a truthful restore point
    (source/nontrivial) or reports it skipped/unavailable. ``provenance``
    (task/plan/session/lane) is recorded in the checkpoint manifest so the
    restore facade's context filters are truthful. The returned dict is
    attached to the edit result so rollback/checkpoint metadata is visible.
    """
    try:
        from .governed_deletion import checkpoint_for_edit

        return checkpoint_for_edit(
            project_root,
            canonical_path,
            reason=reason,
            provenance=provenance,
        )
    except Exception:
        return {
            "status": "checkpoint_unavailable",
            "checkpointed": False,
            "checkpoint_id": "",
            "mode": "",
        }


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
        abs_path = _resolve_path(project_root, path, write=True, config_edit_mode=config_edit_mode)
        _check_sensitive(path)
    except ValueError as exc:
        return {
            "success": False,
            "path": path,
            "created": False,
            "error": str(exc),
        }

    if abs_path.exists():
        return {
            "success": False,
            "path": path,
            "created": False,
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
            "error": str(exc),
        }

    # #66 (2026-04-27): pre-write AST/syntax validation. The other
    # writers (str_replace, edit_lines, batch_edit, anchor_replace)
    # already call _check_syntax on their final text. create_file did
    # not — bypass let broken syntax land on disk. Wire it in here so
    # all five writers share the same fail-closed contract.
    syntax_err = _check_syntax(abs_path, content)
    if syntax_err:
        return {
            "success": False,
            "path": path,
            "created": False,
            "error": syntax_err,
        }

    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    canonical_path = _canonical_relative_path(project_root, abs_path)

    # Warn if creating planning docs in root instead of .MEMORY/
    root_planning_warning = None
    if "/" not in canonical_path and canonical_path.lower().endswith(".md"):
        name_lower = canonical_path.lower()
        if name_lower not in {"claude.md", "agents.md", "readme.md", ".gitignore"}:
            root_planning_warning = "Consider placing planning docs in .MEMORY/roadmaps/ or .MEMORY/specs/ instead of project root."

    result: dict[str, object] = {
        "success": True,
        "path": canonical_path,
        "created": True,
        "lines": len(content.splitlines()),
    }
    if root_planning_warning:
        result["warning"] = root_planning_warning
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
    mode: str = "auto",
    config_edit_mode: ConfigEditMode | None = None,
    provenance: dict | None = None,
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
        abs_path = _resolve_path(project_root, path, write=True, config_edit_mode=config_edit_mode)
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
    is_insert = mode_value == "insert" or (mode_value == "auto" and end_line < start_line)
    if mode_value == "replace" and end_line < start_line:
        return _edit_error(
            path,
            start_line,
            end_line,
            "replace mode requires end_line >= start_line",
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

    touches_file_end = (is_insert and insert_at == total) or (not is_insert and end_line >= total)
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

    # Syntax validation — reject edits that break the full final file.
    updated_text = _render_lines(result_lines, final_newline=final_newline)
    syntax_err = _check_syntax(abs_path, updated_text)
    if syntax_err:
        return _edit_error(path, start_line, end_line, syntax_err)

    if dry_run:
        return {
            "success": True,
            "path": canonical_path,
            "start_line": start_line,
            "end_line": end_line,
            "old_content": old_content,
            "new_content": new_content,
            "lines_removed": len(old_lines),
            "lines_added": len(new_lines),
            "dry_run": True,
        }

    # Record edit history for rollback (diff only, not full file)
    try:
        from .edit_history import EditHistoryStore

        EditHistoryStore().record_edit(
            project_root,
            canonical_path,
            "edit_lines",
            old_content=old_content,
            new_content=new_content,
            start_line=start_line,
            end_line=end_line,
        )
    except Exception:
        pass

    # Pre-mutation restore point (after ALL guards, before the write).
    checkpoint = _edit_checkpoint(project_root, canonical_path, "edit_lines", provenance=provenance)

    # Write back
    _write_lines(abs_path, result_lines, final_newline=final_newline)

    result: dict[str, object] = {
        "success": True,
        "path": canonical_path,
        "start": start_line,
        "end": end_line,
        "removed": len(old_lines),
        "added": len(new_lines),
        "checkpoint": checkpoint,
    }
    return result


def batch_edit(
    project_root: Path,
    edits: list[dict[str, object]],
    *,
    dry_run: bool = False,
    atomic: bool = True,
    config_edit_mode: ConfigEditMode | None = None,
    large_batch_confirm: bool = False,
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
            "total": int,
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
            "total": len(edits),
            "applied": 0,
            "failed": len(edits),
            "results": [],
            "error": str(exc),
        }

    if len(edits) > BATCH_CONFIRM_THRESHOLD and not large_batch_confirm:
        # Typo in old_str is harmless at 3 occurrences, catastrophic at 300.
        # Explicit confirm forces the caller to acknowledge the blast radius
        # before a misuse wipes 100+ call sites.
        return {
            "success": False,
            "total": len(edits),
            "applied": 0,
            "failed": len(edits),
            "results": [],
            "error": (
                f"Batch of {len(edits)} edits exceeds safety threshold "
                f"({BATCH_CONFIRM_THRESHOLD}). Pass large_batch_confirm=True "
                f"to proceed, or split into smaller batches."
            ),
        }

    if not edits:
        return {
            "success": True,
            "total": 0,
            "applied": 0,
            "failed": 0,
            "results": [],
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
                project_root,
                path,
                write=True,
                config_edit_mode=config_edit_mode,
            )
            _check_sensitive(path)
            if abs_path.name.lower() == "aidocs.toml":
                raise ValueError(
                    "aidocs.toml is deprecated. Use the AIDOCS Dashboard to manage settings.",
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
                    ),
                )
                continue

            if mode not in {"auto", "insert", "replace"}:
                validations.append(_edit_error(path, start, end, f"Unknown mode: {mode}"))
                continue
            is_insert = mode == "insert" or (mode == "auto" and end < start)
            if mode == "replace" and end < start:
                validations.append(
                    _edit_error(path, start, end, "replace mode requires end_line >= start_line"),
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
                        },
                    )
                    continue

            validations.append(
                {
                    "success": True,
                    "path": canonical_path,
                    "start": start,
                    "end": end,
                    "removed": len(old_lines),
                    "added": len(new_content.split("\n")) if new_content.strip() else 0,
                    **(
                        {"dry_run": True, "old_content": old_content, "new_content": new_content}
                        if dry_run
                        else {}
                    ),
                },
            )

        except Exception as exc:
            validations.append(_edit_error(path, start, end, str(exc)))

    # Check if any failed
    failures = [v for v in validations if not v["success"]]

    if atomic and failures:
        return {
            "success": False,
            "total": len(edits),
            "applied": 0,
            "failed": len(failures),
            "results": validations,
            "error": f"{len(failures)} edits bad. atomic = no change.",
        }

    if dry_run:
        return {
            "success": len(failures) == 0,
            "total": len(edits),
            "applied": len(validations) - len(failures),
            "failed": len(failures),
            "results": validations,
        }

    # Phase 2: Apply edits (process files in reverse line order to preserve line numbers)
    # Group edits by file
    edits_by_file: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for i, (edit, validation) in enumerate(zip(edits, validations)):
        if not validation["success"]:
            continue
        canonical_path = str(validation.get("path") or edit.get("path") or "")
        if canonical_path not in edits_by_file:
            edits_by_file[canonical_path] = []
        edits_by_file[canonical_path].append((i, edit))

    # Apply edits per file, processing from bottom to top to preserve line numbers
    # Validate each file's final full content before writing.
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

        final_text = _render_lines(all_lines, final_newline=final_newline)
        syntax_err = _check_syntax(abs_path, final_text)
        if syntax_err:
            return {
                "success": False,
                "total": len(edits),
                "applied": 0,
                "failed": 0,
                "stage": "final_syntax",
                "reason": "batch.syntax_final",
                "results": validations,
                "error": f"Final syntax invalid ({canonical_path}): {syntax_err}",
            }

        _write_lines(abs_path, all_lines, final_newline=final_newline)
    applied = len(validations) - len(failures)
    return {
        "success": len(failures) == 0,
        "total": len(edits),
        "applied": applied,
        "failed": len(failures),
        "results": validations,
        **({"error": f"{len(failures)} edit(s) failed."} if failures else {}),
    }


def _check_parse(text: str, ext: str) -> str | None:
    """Return parse error string if text doesn't parse, None if clean."""
    if ext == ".py":
        import ast

        try:
            ast.parse(text)
            return None
        except SyntaxError as exc:
            return f"Python SyntaxError at line {exc.lineno}: {exc.msg}"
    if ext in {".js", ".ts", ".jsx", ".tsx"}:
        # Brace balance check — catches most structural tears
        opens = text.count("{") + text.count("(") + text.count("[")
        closes = text.count("}") + text.count(")") + text.count("]")
        if opens != closes:
            return f"Unbalanced brackets: {opens} opens vs {closes} closes"
        return None
    if ext in {".cs"}:
        opens = text.count("{")
        closes = text.count("}")
        if opens != closes:
            return f"Unbalanced braces: {opens} {{ vs {closes} }}"
        return None
    return None


def _edit_error(path: str, start: int, end: int, error: str) -> dict[str, object]:
    """Create a failed edit result."""
    return {
        "success": False,
        "path": path,
        "start_line": start,
        "end_line": end,
        "error": error,
    }


def str_replace(
    project_root: Path,
    path: str,
    old_str: str,
    new_str: str,
    *,
    replace_all: bool = False,
    config_edit_mode: ConfigEditMode | None = None,
    provenance: dict | None = None,
) -> dict[str, object]:
    """Replace a unique string in a file. For small, targeted edits.

    Args:
        project_root: Project root directory.
        path: Relative path to the file.
        old_str: Exact text to find (must be unique unless replace_all=True).
        new_str: Replacement text.
        replace_all: Replace every occurrence instead of requiring uniqueness.

    Returns:
        { "success": bool, "path": str, "lines_changed": int, "replacements": int, "error": str | None }

    """
    # King doctrine 2026-05-01 (bumped to 500 on 2026-05-02): str_replace
    # is capped to discourage shipping large old/new pairs. For bigger
    # edits, agents must use ai_replace(mode="anchor") (anchors-only,
    # no middle content shipped) or ai_replace(mode="symbol") (index-
    # addressed, name + new body only). Less tools with more uses =
    # happier populace.
    _moc = get_setting(
        "edit.str_replace_max_old_chars",
        project_root=project_root,
        default=1000,
    )
    try:
        max_old_chars = int(_moc) if _moc is not None else 1000
    except (TypeError, ValueError):
        max_old_chars = 1000
    # 0 = unlimited (no cap on the matched old_str length).
    if max_old_chars > 0 and len(old_str) > max_old_chars:
        return {
            "success": False,
            "path": path,
            "lines_changed": 0,
            "replacements": 0,
            "error": (
                f"old_string too long ({len(old_str)} chars, limit {max_old_chars}). Use "
                f"ai_replace(mode='anchor') for big spans or mode='symbol' for whole bodies."
            ),
        }

    try:
        _validate_project_root(project_root, write=True)
    except ValueError as exc:
        return {
            "success": False,
            "path": path,
            "lines_changed": 0,
            "replacements": 0,
            "error": str(exc),
        }
    try:
        abs_path = _resolve_path(project_root, path, write=True, config_edit_mode=config_edit_mode)
    except ValueError as exc:
        return {
            "success": False,
            "path": path,
            "lines_changed": 0,
            "replacements": 0,
            "error": str(exc),
        }
    try:
        _check_sensitive(path)
    except ValueError as exc:
        return {
            "success": False,
            "path": path,
            "lines_changed": 0,
            "replacements": 0,
            "error": str(exc),
        }

    canonical_path = _canonical_relative_path(project_root, abs_path)

    try:
        # PORTABILITY (2026-05-26): Path.read_text(newline=...) only
        # exists in Python 3.13+. Use Path.open(newline="") which is
        # 3.x-compatible and gives identical CRLF-preserving semantics.
        with abs_path.open(encoding="utf-8-sig", newline="") as _f:
            content = _f.read()
    except FileNotFoundError:
        return {
            "success": False,
            "path": path,
            "lines_changed": 0,
            "replacements": 0,
            "error": f"File not found: {path}",
        }

    has_crlf = "\r\n" in content
    if has_crlf:
        content = content.replace("\r\n", "\n")
    old_str = old_str.replace("\r\n", "\n")
    new_str = new_str.replace("\r\n", "\n")

    count = content.count(old_str)

    if count == 0:
        # Try whitespace-normalized match to give a helpful hint
        import re as _re

        normalized_pattern = _re.sub(r"\s+", r"\\s+", _re.escape(old_str.strip()))
        ws_match = _re.search(normalized_pattern, content)
        hint = ""
        if ws_match:
            match_line = content[: ws_match.start()].count("\n") + 1
            hint = f" Whitespace-normalized match found at line {match_line} — check indentation or trailing spaces."
        return {
            "success": False,
            "path": canonical_path,
            "error": f"No match found for old_str in {canonical_path}.{hint}",
        }

    if count > 1 and not replace_all:
        return {
            "success": False,
            "path": canonical_path,
            "lines_changed": 0,
            "replacements": 0,
            "error": f"Found {count} matches for old_str in {canonical_path}. Use replace_all=True or provide more context to make old_str unique.",
        }

    old_lines = content.splitlines()
    # Find first match line for preview
    first_match_line = None
    for i, line in enumerate(old_lines, 1):
        if old_str in line:
            first_match_line = i
            break

    if replace_all:
        new_content = content.replace(old_str, new_str)
        replacements = count
    else:
        new_content = content.replace(old_str, new_str, 1)
        replacements = 1

    # Syntax validation — validate full final file after edit
    err = _check_syntax(abs_path, new_content)
    if err:
        return {
            "success": False,
            "path": canonical_path,
            "lines_changed": 0,
            "replacements": 0,
            "error": f"Syntax err: {err}",
        }

    new_lines = new_content.splitlines()
    lines_changed = sum(1 for a, b in zip(old_lines, new_lines) if a != b) + abs(
        len(new_lines) - len(old_lines),
    )
    # Sentinel-removal check — refuse silently stripping the DO NOT TOUCH
    # header. Applies to EVERY caller, conductor included. Use
    # ai_protect to remove protection explicitly.
    if _would_remove_sentinel(abs_path, new_content):
        return {
            "success": False,
            "path": canonical_path,
            "lines_changed": 0,
            "replacements": 0,
            "error": (
                f"🛑 SENTINEL REMOVAL DETECTED: {canonical_path}. This "
                f"edit would remove the DO NOT TOUCH header from a "
                f"protected file. Removing the header does not grant "
                f"edit access — it's itself a protected operation. Use "
                f"ai_protect with an explicit user grant if "
                f"the intent is to unprotect, not silently strip."
            ),
        }

    # Record edit history for rollback
    try:
        from .edit_history import EditHistoryStore

        EditHistoryStore().record_edit(
            project_root,
            canonical_path,
            "str_replace",
            old_content=old_str,
            new_content=new_str,
            start_line=first_match_line,
        )
    except Exception:
        pass

    # Pre-mutation restore point (after ALL guards, before the write).
    checkpoint = _edit_checkpoint(
        project_root,
        canonical_path,
        "str_replace",
        provenance=provenance,
    )

    # Restore original line endings if file had CRLF
    if has_crlf:
        new_content = new_content.replace("\n", "\r\n")
    abs_path.write_text(new_content, encoding="utf-8", newline="")

    result: dict[str, object] = {
        "success": True,
        "path": canonical_path,
        "changed": lines_changed,
        "replacements": replacements,
        "checkpoint": checkpoint,
    }
    if first_match_line:
        result["first_match_line"] = first_match_line
    return result


def anchor_replace(
    project_root: Path,
    path: str,
    *,
    start_anchor: str,
    replacement: str,
    end_anchor: str,
    allow_partial_anchors: bool = False,
    config_edit_mode: ConfigEditMode | None = None,
    provenance: dict | None = None,
) -> dict[str, object]:
    """Anchor-only span replace (king doctrine 2026-05-01).

    Both anchors required. The pair must form exactly one unambiguous
    region in the file; the content STRICTLY BETWEEN them (anchors
    themselves preserved) is replaced with `replacement`.

    No `target` argument — the agent does not ship the old middle
    content. The two anchors are the address; the file is the source
    of truth for what lives between them. Bytes shipped: path +
    anchors + new body, independent of the replaced span's size.

    Refuses:
      - empty / missing anchor
      - anchor not found
      - end_anchor never appears after start_anchor
      - more than one (start, end) pairing possible — anchors must be
        chosen specific enough to be unambiguous
      - empty span (start_anchor immediately followed by end_anchor —
        use ai_str_replace for inserts)
        - partial-line anchors by default — each anchor must start and
          end at a line boundary (before/after is \\n, or file edge).
          Set allow_partial_anchors=True (expert/debug escape only)
          to bypass.
    """
    try:
        _validate_project_root(project_root, write=True)
        abs_path = _resolve_path(project_root, path, write=True, config_edit_mode=config_edit_mode)
        _check_sensitive(path)
        canonical_path = _canonical_relative_path(project_root, abs_path)
    except (ValueError, FileNotFoundError) as exc:
        return {"success": False, "path": path, "error": str(exc)}

    if not start_anchor or not end_anchor:
        return {
            "success": False,
            "path": canonical_path,
            "error": "both start_anchor and end_anchor are required (anchor-only mode)",
        }

    # Anchor specificity floor (2026-06-17): the PRIMARY guard is the uniqueness check
    # below (start/end must each match EXACTLY ONCE) — that already rejects a bare token
    # that hits thousands of lines. This is only a light fail-fast so such a token gives
    # an actionable "be specific" message up front. Measured on NON-WHITESPACE content
    # (so leading-indent padding can't fake length): "  );" is 2 meaningful chars and is
    # refused, while a legit short line like ") : null}" (9) or "} else {" (8) passes.
    _MIN_ANCHOR_CHARS = 3
    for _label, _anchor in (("start_anchor", start_anchor), ("end_anchor", end_anchor)):
        if len(_anchor.strip()) < _MIN_ANCHOR_CHARS:
            return {
                "success": False,
                "path": canonical_path,
                "error": (
                    f"{_label} has too little content ({len(_anchor.strip())} non-space "
                    f"chars; minimum {_MIN_ANCHOR_CHARS}). Use a distinctive full line, not a "
                    f"bare token like '  );' or '}}'. (The real guard is uniqueness — the "
                    f"anchor must match exactly once.)"
                ),
            }

    # PORTABILITY (2026-05-26): Path.read_text(newline=...) is 3.13+.
    # Use Path.open(newline="") for 3.12 compat (same CRLF-preserving
    # semantics).
    with abs_path.open(encoding="utf-8-sig", newline="") as _f:
        content = _f.read()
    has_crlf = "\r\n" in content
    if has_crlf:
        content = content.replace("\r\n", "\n")

    # Each anchor must occur EXACTLY ONCE in the file.
    start_count = content.count(start_anchor)
    if start_count == 0:
        return {
            "success": False,
            "path": canonical_path,
            "error": f"start_anchor not found in {canonical_path}",
        }
    if start_count > 1:
        return {
            "success": False,
            "path": canonical_path,
            "error": (
                f"start_anchor occurs {start_count} times in "
                f"{canonical_path}; it must be unique — choose a more "
                f"specific start_anchor that appears exactly once"
            ),
        }
    end_count = content.count(end_anchor)
    if end_count == 0:
        return {
            "success": False,
            "path": canonical_path,
            "error": f"end_anchor not found in {canonical_path}",
        }
    if end_count > 1:
        return {
            "success": False,
            "path": canonical_path,
            "error": (
                f"end_anchor occurs {end_count} times in "
                f"{canonical_path}; it must be unique — choose a more "
                f"specific end_anchor that appears exactly once"
            ),
        }

    s = content.find(start_anchor)
    e = content.find(end_anchor, s + len(start_anchor))
    if e < 0:
        return {
            "success": False,
            "path": canonical_path,
            "error": (f"end_anchor not found after start_anchor in {canonical_path}"),
        }

    # Partial-line anchor guard (2026-05-26): anchors must BOTH start
    # AND end at a line boundary. A "line boundary" means the anchor
    # starts at \\n (or file edge) AND ends at \\n, or the char after
    # it is \\n (or EOF). Refuse by default; allow_partial_anchors is
    # an expert/debug escape only.
    if not allow_partial_anchors:

        def _anchor_at_boundary(
            _c: str,
            _pos: int,
            _a: str,
        ) -> bool:
            after = _pos + len(_a)
            if after >= len(_c):
                return True
            if _a and _a[-1] == "\n":
                return True
            return _c[after] == "\n"

        if not ((s == 0) or (content[s - 1] == "\n")):
            return {
                "success": False,
                "path": canonical_path,
                "error": (
                    "partial-line anchor: start_anchor must start AND end at a "
                    "line boundary (expert/debug escape: allow_partial_anchors=True; "
                    "for inline tweaks use mode='string' instead)."
                ),
            }
        if not _anchor_at_boundary(content, s, start_anchor):
            return {
                "success": False,
                "path": canonical_path,
                "error": (
                    "partial-line anchor: start_anchor must start AND end at a "
                    "line boundary (expert/debug escape: allow_partial_anchors=True; "
                    "for inline tweaks use mode='string' instead)."
                ),
            }
        if not ((e == 0) or (content[e - 1] == "\n")):
            return {
                "success": False,
                "path": canonical_path,
                "error": (
                    "partial-line anchor: end_anchor must start AND end at a "
                    "line boundary (expert/debug escape: allow_partial_anchors=True; "
                    "for inline tweaks use mode='string' instead)."
                ),
            }
        if not _anchor_at_boundary(content, e, end_anchor):
            return {
                "success": False,
                "path": canonical_path,
                "error": (
                    "partial-line anchor: end_anchor must start AND end at a "
                    "line boundary (expert/debug escape: allow_partial_anchors=True; "
                    "for inline tweaks use mode='string' instead)."
                ),
            }

    span_left = s + len(start_anchor)
    span_right = e
    if span_left == span_right:
        return {
            "success": False,
            "path": canonical_path,
            "error": ("empty span between anchors — use ai_str_replace to insert content"),
        }

    # Foot-gun guard (2026-05-03): anchors PERSIST; replacement only
    # fills the content BETWEEN them.
    if replacement.startswith(start_anchor):
        return {
            "success": False,
            "path": canonical_path,
            "error": (
                "replacement starts with start_anchor — would double "
                "the boundary. Anchors persist; replacement fills only "
                "the BETWEEN. Drop the start_anchor from replacement, "
                "or use ai_str_replace for full-region replace."
            ),
        }
    if replacement.endswith(end_anchor):
        return {
            "success": False,
            "path": canonical_path,
            "error": (
                "replacement ends with end_anchor — would double the "
                "boundary. Anchors persist; replacement fills only the "
                "BETWEEN. Drop the end_anchor from replacement, or "
                "use ai_str_replace for full-region replace."
            ),
        }

    # Line-frame the replacement (king 2026-06-20): in default (line-aligned) mode
    # the replaced span consumed the newline AFTER start_anchor and the one BEFORE
    # end_anchor, so a replacement lacking them gets GLUED onto the anchor lines
    # (`def f():  body` -> "unexpected indent"; or a silently joined comment, which
    # really happened to a deploy-gate guard comment). Restore the framing so the
    # replacement occupies whole lines. Partial-anchor (expert) mode keeps raw
    # concatenation — there the caller owns exact byte placement.
    if allow_partial_anchors:
        framed = replacement
    else:
        # Line-frame so the replacement occupies whole lines, WITHOUT doubling a
        # newline the anchor already carries. Whether a newline exists at the LEFT
        # boundary depends on whether start_anchor included its own trailing "\n"
        # (span_left then sits AFTER it), so test the actual adjacent char — NOT
        # replacement.startswith("\n"), which doubled the newline when the anchor
        # included it (king 2026-06-20 regression; test_edit_checkpoint).
        framed = replacement
        # Add a separating newline at a boundary ONLY if there isn't already one on
        # either side of the join: the boundary content (anchor may carry its own
        # trailing/leading "\n" → span sits past it) OR the replacement itself. The
        # prior code tested only replacement.startswith/endswith, which doubled the
        # newline when the ANCHOR carried it (test_edit_checkpoint); testing only the
        # content would double it when the CALLER pre-framed (test ..._not_doubled).
        left_has_nl = span_left > 0 and content[span_left - 1] == "\n"
        right_has_nl = span_right < len(content) and content[span_right] == "\n"
        if not (left_has_nl or framed.startswith("\n")):
            framed = "\n" + framed
        if not (right_has_nl or framed.endswith("\n")):
            framed = framed + "\n"
    new_content = content[:span_left] + framed + content[span_right:]

    err = _check_syntax(abs_path, new_content)
    if err:
        return {
            "success": False,
            "path": canonical_path,
            "error": (
                f"Syntax err: {err} "
                "(anchors persist and replacement fills only the BETWEEN — "
                "a partial-line anchor or malformed replacement may have "
                "produced invalid syntax)"
            ),
        }

    if _would_remove_sentinel(abs_path, new_content):
        return {
            "success": False,
            "path": canonical_path,
            "error": (
                f"🛑 SENTINEL REMOVAL DETECTED: {canonical_path}. This edit would remove the DO NOT TOUCH header."
            ),
        }

    first_match_line = content[:s].count("\n") + 1
    old_span = content[span_left:span_right]
    try:
        from .edit_history import EditHistoryStore

        EditHistoryStore().record_edit(
            project_root,
            canonical_path,
            "anchor_replace",
            old_content=old_span,
            new_content=replacement,
            start_line=first_match_line,
        )
    except Exception:
        pass

    # Pre-mutation restore point — parity with str_replace. After ALL guards
    # (path/sensitive/anchor uniqueness/boundary/empty-span/foot-gun/syntax/
    # sentinel) and before the CRLF restore + write_text, so a refused edit
    # never creates a checkpoint and a successful edit always has one.
    checkpoint = _edit_checkpoint(
        project_root,
        canonical_path,
        "anchor_replace",
        provenance=provenance,
    )

    if has_crlf:
        new_content = new_content.replace("\n", "\r\n")
    abs_path.write_text(new_content, encoding="utf-8", newline="")
    return {
        "success": True,
        "path": canonical_path,
        "changed": 1,
        "replacements": 1,
        "first_match_line": first_match_line,
        "match_count": 1,
        "old_span": old_span,
        "checkpoint": checkpoint,
    }


def batch_str_replace(
    project_root: Path,
    edits: list[dict[str, object]],
    *,
    atomic: bool = True,
    config_edit_mode: ConfigEditMode | None = None,
    large_batch_confirm: bool = False,
) -> dict[str, object]:
    """Apply multiple string-match replacements atomically across files.

    Each edit: { "path": str, "old_str": str, "new_str": str, "replace_all": bool? }

    Args:
        project_root: Project root directory.
        edits: List of string-match edit operations.
        atomic: All-or-nothing mode (default True).

    Returns:
        { "success": bool, "total": int, "applied": int, "failed": int, "results": [...], "error": str | None }

    """
    try:
        _validate_project_root(project_root, write=True)
    except ValueError as exc:
        return {
            "success": False,
            "total": len(edits),
            "applied": 0,
            "failed": len(edits),
            "results": [],
            "error": str(exc),
        }

    if len(edits) > BATCH_CONFIRM_THRESHOLD and not large_batch_confirm:
        return {
            "success": False,
            "total": len(edits),
            "applied": 0,
            "failed": len(edits),
            "results": [],
            "error": (
                f"Batch of {len(edits)} edits exceeds safety threshold "
                f"({BATCH_CONFIRM_THRESHOLD}). Pass large_batch_confirm=True "
                f"to proceed, or split into smaller batches."
            ),
        }

    # Phase 1: validate all edits and compute replacements
    file_cache: dict[str, str] = {}
    file_paths: dict[str, Path] = {}
    file_crlf: dict[str, bool] = {}  # per-file CRLF, restored on write
    validations: list[dict[str, object]] = []

    for edit_index, edit in enumerate(edits):
        path = str(edit.get("path") or "")
        if not path:
            validations.append(
                {
                    "success": False,
                    "path": "",
                    "edit_index": edit_index,
                    "error": f"Edit #{edit_index}: path required.",
                },
            )
            continue

        # EOL tolerance: match + apply in LF space so a LF-vs-CRLF
        # mismatch never fails a match (the file's CRLF is restored on
        # write). Other whitespace — spaces / tabs / indentation — is NOT
        # normalized and still matters.
        old_str = str(edit.get("old_str") or edit.get("old_string") or "").replace("\r\n", "\n")
        new_str = str(edit.get("new_str") or edit.get("new_string") or "").replace("\r\n", "\n")
        replace_all = bool(edit.get("replace_all", False))

        try:
            abs_path = _resolve_path(
                project_root,
                path,
                write=True,
                config_edit_mode=config_edit_mode,
            )
            _check_sensitive(path)
            canonical_path = _canonical_relative_path(project_root, abs_path)

            if canonical_path not in file_cache:
                # PORTABILITY (2026-05-26): 3.12-compat read with
                # newline="" — Path.read_text gained `newline` in 3.13.
                with abs_path.open(encoding="utf-8-sig", newline="") as _f:
                    raw = _f.read()
                file_crlf[canonical_path] = "\r\n" in raw
                file_cache[canonical_path] = raw.replace("\r\n", "\n")
                file_paths[canonical_path] = abs_path

            content = file_cache[canonical_path]
            # Empty old_str would collapse every boundary in the file to
            # a match; reject explicitly so the caller sees a clear
            # reason instead of a nonsense N+1 match count.
            if not old_str:
                validations.append(
                    {
                        "success": False,
                        "path": canonical_path,
                        "edit_index": edit_index,
                        "error": f"Edit #{edit_index}: old_str empty.",
                    },
                )
                continue
            count = content.count(old_str)

            if count == 0:
                preview = old_str[:50] + ("..." if len(old_str) > 50 else "")
                validations.append(
                    {
                        "success": False,
                        "path": canonical_path,
                        "edit_index": edit_index,
                        "error": f"Edit #{edit_index}: no match for: {preview}",
                    },
                )
                continue
            if count > 1 and not replace_all:
                preview = old_str[:50] + ("..." if len(old_str) > 50 else "")
                validations.append(
                    {
                        "success": False,
                        "path": canonical_path,
                        "edit_index": edit_index,
                        "error": f"Edit #{edit_index}: {count} matches for: {preview}",
                    },
                )
                continue

            # Apply to cached content so subsequent edits on same file see prior changes
            if replace_all:
                file_cache[canonical_path] = content.replace(old_str, new_str)
            else:
                file_cache[canonical_path] = content.replace(old_str, new_str, 1)

            validations.append({"success": True, "path": canonical_path})

        except (ValueError, FileNotFoundError) as exc:
            validations.append({"success": False, "path": path, "error": str(exc)})

    failures = [v for v in validations if not v["success"]]

    if atomic and failures:
        return {
            "success": False,
            "total": len(edits),
            "applied": 0,
            "failed": len(failures),
            "results": validations,
            "error": f"{len(failures)} edits bad. atomic = no change.",
        }

    # Phase 2: validate final full-file content, then write modified files
    # Track whether this is Phase-1 (match failure) or Phase-2 (syntax failure)
    for canonical_path, content in file_cache.items():
        abs_path = file_paths[canonical_path]
        syntax_err = _check_syntax(abs_path, content)
        if syntax_err:
            return {
                "success": False,
                "total": len(edits),
                "applied": 0,
                "failed": 0,  # Not a per-edit failure — syntax of final content
                "stage": "final_syntax",  # Distinguish from Phase-1 match failures
                "reason": "batch.syntax_final",
                "results": validations,
                "error": f"Final syntax invalid ({canonical_path}): {syntax_err}",
            }
        out_text = content
        if file_crlf.get(canonical_path):
            out_text = content.replace("\n", "\r\n")  # restore file's CRLF
        abs_path.write_text(out_text, encoding="utf-8", newline="")

    applied = len(validations) - len(failures)
    return {
        "success": len(failures) == 0,
        "total": len(edits),
        "applied": applied,
        "failed": len(failures),
        "results": validations,
        **({"error": f"{len(failures)} edit(s) failed."} if failures else {}),
    }


def extract_block(
    project_root: Path,
    source_path: str,
    start_line: int,
    end_line: int,
    target_path: str,
    *,
    target_position: str = "append",
    target_line: int | None = None,
    remove_from_source: bool = True,
) -> dict[str, object]:
    """Extract a code block from source and place it in target.

    Args:
        source_path: File to extract from.
        start_line: First line of block (1-indexed, inclusive).
        end_line: Last line of block (inclusive).
        target_path: File to place block in (created if missing).
        target_position: 'append' (end of file), 'prepend' (start), or 'at_line' (use target_line).
        target_line: Insert before this line when target_position='at_line'.
        remove_from_source: If True, remove the block from source after copying.

    """
    try:
        _validate_project_root(project_root, write=True)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    try:
        src_abs = _resolve_path(project_root, source_path, write=remove_from_source)
        _check_sensitive(source_path)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    try:
        tgt_abs = _resolve_path(project_root, target_path, write=True)
        _check_sensitive(target_path)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    src_lines = _read_file_lines(src_abs)
    total = len(src_lines)

    if start_line < 1 or start_line > total:
        return {
            "success": False,
            "error": f"start_line {start_line} out of range (file has {total} lines)",
        }
    if end_line < start_line or end_line > total:
        return {"success": False, "error": f"end_line {end_line} out of range"}

    block = src_lines[start_line - 1 : end_line]

    # Build target content
    if tgt_abs.is_file():
        tgt_lines = _read_file_lines(tgt_abs)
    else:
        tgt_lines = []

    pos = target_position.strip().lower()
    if pos == "append":
        new_tgt_lines = tgt_lines + block
    elif pos == "prepend":
        new_tgt_lines = block + tgt_lines
    elif pos == "at_line" and target_line is not None:
        insert_at = max(0, min(target_line - 1, len(tgt_lines)))
        new_tgt_lines = tgt_lines[:insert_at] + block + tgt_lines[insert_at:]
    else:
        return {"success": False, "error": f"Invalid target_position: {target_position}"}
    # Validate both files parse BEFORE writing
    new_src_lines = (
        src_lines[: start_line - 1] + src_lines[end_line:] if remove_from_source else src_lines
    )
    new_tgt_text = "\n".join(new_tgt_lines)
    new_src_text = "\n".join(new_src_lines)

    src_ext = src_abs.suffix.lower()
    tgt_ext = tgt_abs.suffix.lower()

    src_parse_error = _check_parse(new_src_text, src_ext) if remove_from_source else None
    tgt_parse_error = _check_parse(new_tgt_text, tgt_ext)

    if src_parse_error:
        return {
            "success": False,
            "error": f"Extraction would break source file syntax: {src_parse_error}",
            "hint": "The block boundary may be wrong — check start_line/end_line include the complete symbol.",
        }
    if tgt_parse_error:
        return {
            "success": False,
            "error": f"Extraction would break target file syntax: {tgt_parse_error}",
            "hint": "The extracted block may be incomplete or need imports/context in the target.",
        }

    # Write target
    tgt_abs.parent.mkdir(parents=True, exist_ok=True)
    _write_lines(tgt_abs, new_tgt_lines, final_newline=True)

    # Remove from source
    lines_removed = 0
    if remove_from_source:
        _write_lines(
            src_abs,
            new_src_lines,
            final_newline=_file_ends_with_newline(src_abs) if new_src_lines else True,
        )
        lines_removed = len(block)

    src_canonical = _canonical_relative_path(project_root, src_abs)
    tgt_canonical = _canonical_relative_path(project_root, tgt_abs)

    return {
        "success": True,
        "source": src_canonical,
        "target": tgt_canonical,
        "extracted": len(block),
        "removed": lines_removed,
        "position": pos,
    }
