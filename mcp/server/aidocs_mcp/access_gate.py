"""Unified access gate — single decision engine for all file access control.

6-level cascade, first match wins:
    Level 1: Managed Mode Gate         — block raw file tools when managed
    Level 2: Infrastructure Protection — block writes to AIDOCS config/source
    Level 3: Sensitive File Protection — block .env, credentials, keys
    Level 4: Memory Path Gate          — .MEMORY/ reads free, writes intent-gated
    Level 5: Read Gate                 — per-file discovery, known_exact_path bypass
    Level 6: Edit Gate                 — requires prior read/discovery
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .query_gate import QueryGateStore


@dataclass(slots=True)
class GateContext:
    """Caller builds this from managed mode, session, and gate state."""

    managed: bool
    session_id: str | None
    dev_mode: bool  # unlocks AIDOCS source editing
    allow_config_edit: bool  # unlocks aidocs.toml editing
    gate_enforce: bool  # tool gates active (bash allowlist, raw tools, destructive)
    gate_state: dict[str, Any]
    # RBAC-resolved unlock for hard-protected DATA files (index.aidocs,
    # gate-state JSON). Set upstream after a role check (+ escalate). Does
    # NOT unlock sqlite — those are config_set-only, never file-writable.
    # Defaulted so existing GateContext constructions stay valid.
    allow_hard_protected_edit: bool = False


@dataclass(slots=True)
class GateDecision:
    allowed: bool
    level: str
    reason: str | None = None
    # Marks a pass-through that should still be recorded in the event
    # stream for audit — used when an operator-deliberate unblock
    # (e.g. security.allow_raw_edits) lets a tool through that would
    # otherwise have been blocked at tier-0.
    advisory: bool = False


# ── Constants ──

_BLOCKED_RAW_FILE_TOOLS: set[str] = {
    "read",
    "grep",
    "glob",
    "edit",
    "update",
    "write",
    "patch",
    "apply_patch",
    "multiedit",
}

# Raw edit tools that ALSO bypass the AIDOCS index and leave stale
# snapshots behind. Unlike raw reads (which waste tokens but are
# recoverable), raw edits are a silent correctness hazard: other lanes
# read stale code, edit_history loses the change, and audit trails
# break. "edit"/"write" overlap with everyday English verbs, so NLP
# user-grants are not enough signal — unlock is dashboard-only via
# `security.allow_raw_edits` config, which is operator-deliberate,
# persisted, and survives restarts.
_RAW_EDIT_TOOLS: set[str] = {
    "edit",
    "update",
    "write",
    "patch",
    "apply_patch",
    "multiedit",
    "notebookedit",
    "str_replace_based_edit_tool",
}

# Raw shell tools that route around `ai_run`'s journal audit trail.
# The Claude Code VSCode extension names this tool "Bash"; some CLI
# variants expose it as "update" — both must block identically so
# host-dependent behavior doesn't emerge. Two unblock paths:
#   - `security.allow_raw_shell` dashboard flag: persistent operator
#     unblock, survives restarts.
#   - Per-turn NLP grant ("I allow bash"): one-turn bypass for cases
#     where ai_run can't run the command (e.g. test retry loop).
# Both unblock paths still flow through bash_policy (allow/deny table)
# and the heuristic judge downstream, so destructive commands stay
# blocked regardless of grant.
# 2026-04-27: extended from {"bash"} to cover every shell-equivalent
# tool surface. Pre-fix, hosts that exposed PowerShell / pwsh / cmd /
# wsl / Monitor as separate tools bypassed the raw-shell gate entirely
# because their tool names weren't in this set. Confirmed via red-team
# probes 7-12 + 14b (backlog #61): PowerShell `Set-Content` against
# `mcp/server/aidocs_mcp/_probe.py` with dev_mode=off succeeded silently.
# Same root cause as the heuristic-judge `_SHELL_EQUIVALENT_TOOL_NAMES`
# fix earlier 2026-04-26 — every gate that has a tool-name allowlist
# needs to know about all shell-shape tool surfaces.
# Names are lowercased here because access_gate normalizes tool names
# to lowercase before the membership check (see check_raw_shell).
_RAW_SHELL_TOOLS: set[str] = {
    "bash",
    "powershell",
    "pwsh",
    "cmd",
    "wsl",
    "monitor",
}


def _gate_msg(key: str, **kwargs: str) -> str:
    """Load gate message from gate_messages TOML with variable substitution."""
    from .config import render_interaction_text

    return render_interaction_text(f"interaction.gate_messages.{key}", **kwargs)


# Path-extraction slot map. Each slot is one semantic target — its
# aliases must agree, but slots in different rows are independent.
#
# Rationale (co-conductor 2026-04-30): conflicting aliases for the
# SAME slot are ambiguous input that lets one gate see "src/app.py"
# while another sees "../outside.py". But many tools legitimately
# send TWO independent paths (move/copy: source_path + dest_path;
# rename: old_path + new_path; batch_edit: each edit has its own
# path). A naive cross-key comparison would refuse those.
#
# So: aliases are grouped by slot. Within a slot, aliases must
# normalize to the same value. Across slots, no comparison is made.
# Tools that act on multiple paths walk each slot or each list entry
# independently.
_PATH_SLOT_ALIASES: dict[str, tuple[str, ...]] = {
    # Primary target — what the tool reads/writes/edits/searches.
    # `file_path` is the Claude Code/MCP canonical; `filePath` is the
    # OpenCode host convention; `path` is the legacy short form still
    # used by ai_get_lines, ai_text_search, etc.
    "target_path": ("file_path", "filePath", "path"),
    # Notebook target — used by NotebookEdit. Distinct slot because a
    # notebook call may legitimately reference both the notebook AND
    # a sidecar file; the notebook is the live target.
    "notebook_target": ("notebook_path", "notebookPath"),
}

# Pattern keys (Glob/Grep). Patterns are NOT paths — they describe
# search criteria, not a target file. Kept separate so the discovery
# branch can still consult them when no real path slot is set.
_PATTERN_INPUT_KEYS: tuple[str, ...] = ("pattern",)


class PathInputConflict(ValueError):
    """Slot-internal alias conflict — two or more aliases for the same
    semantic target slot resolve to distinct paths after light
    normalization. Refused at the trust boundary because silently
    picking precedence would let one gate decide on one path while
    another decides on a different one within the same call.

    Distinct slots (e.g. `file_path` vs `notebook_path`) are NOT
    compared and never raise this; tools that legitimately use both
    walk each slot independently.

    Attributes:
        slot: the slot name whose aliases disagree.
        values: dict[alias_key, normalized_value] of the conflict.

    """

    def __init__(self, slot: str, values: dict[str, str]) -> None:
        self.slot = slot
        self.values = dict(values)
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(values.items()))
        super().__init__(
            f"conflicting aliases for slot {slot!r}: {rendered}. "
            f"Aliases for the same slot must agree after normalization.",
        )


def _is_windows_host() -> bool:
    import sys as _sys

    return _sys.platform == "win32"


# Sentinel for traversal-rejected values. Distinct from "" (absent)
# because a caller who SENDS `../x` is not the same as a caller who
# omits the slot — the former should fail loudly when paired with a
# real path (the alias disagreement is real), while the latter should
# leave the slot empty.
_TRAVERSAL_REJECTED = "<<traversal-rejected>>"


def _normalize_path_value(value: object) -> str:
    """Light, deterministic normalization for slot-equality comparison.

    Steps (in order):
      - trim leading/trailing whitespace
      - return "" on empty
      - normalize separators: backslash -> forward slash
      - reject ``..`` segments anywhere in the path (BEFORE any
        normpath collapse — `src/../outside.py` is just as suspect
        as `../outside.py` and must not be silently rewritten to
        `outside.py`). Returns the traversal sentinel so a paired
        clean path triggers conflict refusal rather than being
        silently accepted.
      - collapse ``./`` and redundant slashes (posixpath.normpath)
      - casefold on Windows (NTFS is case-insensitive); leave POSIX
        case-sensitive

    Does NOT resolve(), realpath(), or canonicalize against the
    filesystem — gates that need that decide explicitly. Equality
    here is purely string-level after the listed steps.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    # Reject traversal segments before normpath collapses them.
    # `src/../outside.py` would normpath to `outside.py`, hiding the
    # original intent — refuse loudly instead.
    if ".." in text.split("/"):
        return _TRAVERSAL_REJECTED
    # Collapse `./` and redundant slashes via posixpath only — never
    # os.path, since we've already forced forward slashes and don't
    # want host-platform behavior leaking back in.
    import posixpath as _pp

    text = _pp.normpath(text)
    if _is_windows_host():
        text = text.casefold()
    return text


def _collect_slot_values(
    tool_input: dict[str, Any] | None,
    aliases: tuple[str, ...],
) -> dict[str, str]:
    """Return {alias: normalized_value} for present aliases.

    Includes the traversal sentinel so the caller can detect a
    `..`-bearing alias and refuse loudly when paired with a clean
    path. Empty/absent aliases are skipped.
    """
    if not tool_input:
        return {}
    found: dict[str, str] = {}
    for key in aliases:
        if key in tool_input:
            normalized = _normalize_path_value(tool_input.get(key))
            if normalized:  # includes _TRAVERSAL_REJECTED sentinel
                found[key] = normalized
    return found


def _resolve_slot(
    tool_input: dict[str, Any] | None,
    slot: str,
) -> str:
    """Return the canonical normalized value for one slot.

    - 0 aliases present -> "".
    - 1 alias present, or N aliases all normalizing equal ->
      return that canonical value.
    - N aliases with distinct normalized values -> raise
      PathInputConflict(slot, values).
    - Single alias with a `..` traversal segment -> raise
      PathInputConflict(slot, values) with the sentinel marker so
      the caller refuses the call instead of silently treating the
      slot as empty.
    """
    aliases = _PATH_SLOT_ALIASES.get(slot)
    if not aliases:
        return ""
    found = _collect_slot_values(tool_input, aliases)
    if not found:
        return ""
    distinct = set(found.values())
    if len(distinct) == 1:
        canonical = next(iter(distinct))
        if canonical == _TRAVERSAL_REJECTED:
            raise PathInputConflict(slot, found)
        return canonical
    # Multiple distinct values — conflict regardless of whether one
    # of them is the traversal sentinel. The sentinel paired with a
    # clean path is exactly the case we want to fail loudly on.
    raise PathInputConflict(slot, found)


def _extract_path(tool_input: dict[str, Any] | None) -> str:
    """Return the single canonical target path from `tool_input`.

    Slot-aware: checks `target_path` first, falls back to
    `notebook_target` when the primary slot is empty. Each slot's
    aliases are checked for internal conflict; cross-slot values are
    never compared.

    Raises PathInputConflict on slot-internal disagreement (after
    normalization). Returns "" when no path-slot alias is present.
    """
    primary = _resolve_slot(tool_input, "target_path")
    if primary:
        return primary
    return _resolve_slot(tool_input, "notebook_target")


def _extract_path_or_pattern(tool_input: dict[str, Any] | None) -> str:
    """Like _extract_path but also accepts pattern keys (Glob/Grep).

    Path slots take precedence over pattern keys: a Grep call with
    both `file_path` and `pattern` is treated as a path call. The
    pattern key is consulted only when no path slot is set.
    """
    direct = _extract_path(tool_input)
    if direct:
        return direct
    if not tool_input:
        return ""
    for key in _PATTERN_INPUT_KEYS:
        value = tool_input.get(key)
        if value:
            normalized = _normalize_path_value(value)
            if normalized:
                return normalized
    return ""


def _get_raw_tool_replacement(tool: str) -> str:
    from .config import render_interaction_text

    text = render_interaction_text(f"interaction.raw_tool_replacements.{tool}")
    if text and not text.startswith("{"):
        return text
    return f"Use the equivalent AIDOCS MCP tool instead of `{tool}`."


def _build_reroute_call(tool: str, tool_input: dict[str, Any] | None) -> str:
    """Build an exact MCP call suggestion from the intercepted raw tool args.

    Pure UX helper — runs only after a gate has already decided to
    block. If path inputs conflict (PathInputConflict), the caller's
    block message stands on its own; we just skip the reroute hint.
    """
    if not tool_input:
        return ""
    tool = tool.strip().lower()

    try:
        canonical_path = _extract_path(tool_input)
    except PathInputConflict:
        return ""

    if tool == "read":
        path = canonical_path
        if path:
            offset = tool_input.get("offset") or tool_input.get("start_line") or 1
            limit = tool_input.get("limit") or tool_input.get("count") or 100
            return f'Use instead: `ai_get_lines(path="{path}", start_line={offset}, count={limit}, known_exact_path=true)`'

    if tool == "grep":
        pattern = tool_input.get("pattern") or ""
        path = tool_input.get("path") or tool_input.get("include") or ""
        if pattern:
            args = f'query="{pattern}"'
            if path:
                args += f', root="{path}"'
            return f"Use instead: `ai_text_search({args})`"

    if tool == "glob":
        pattern = tool_input.get("pattern") or ""
        if pattern:
            return f'Use instead: `ai_search(query="{pattern}")`'

    if tool in ("edit", "update", "multiedit", "patch", "apply_patch"):
        path = canonical_path
        if path:
            return f'Use instead: `ai_str_replace(path="{path}", old_str=..., new_str=...)` or `ai_edit_lines(path="{path}", ...)`'

    if tool == "write":
        path = canonical_path
        if path:
            return f'Use instead: `ai_create_file(path="{path}", content=...)`'

    return ""


# Infrastructure protection — always blocked for writes
_PROTECTED_CONFIG_FILES: set[str] = {"aidocs.toml", "aidocs-plugin.json"}

# Infrastructure paths — blocked unless dev_mode
_INFRASTRUCTURE_PREFIXES: tuple[str, ...] = ("mcp/server/aidocs_mcp/",)

# Sensitive file patterns
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|[/-])\.env(\.|$)", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"\.(key|pem|pfx|p12|keystore|jks)$", re.IGNORECASE),
    # SSH material — id_rsa / id_ed25519 / id_ecdsa / id_dsa and anything
    # under a .ssh/ directory. None of these match key/secret/credential
    # by name, so they need their own pattern.
    re.compile(r"(^|[/-])\.ssh/", re.IGNORECASE),
    re.compile(r"(^|[/-])id_(rsa|dsa|ecdsa|ed25519)(\.|$)", re.IGNORECASE),
    # AWS / cloud credential stores.
    re.compile(r"(^|[/-])\.aws/credentials$", re.IGNORECASE),
    re.compile(r"(^|[/-])\.npmrc$", re.IGNORECASE),
    # Install-wide AIDOCS global config DB — lives in the user home dir,
    # holds settings that affect every project for this user.
    re.compile(r"(^|/)\.aidocs/config\.sqlite3(-journal|-wal|-shm)?$", re.IGNORECASE),
    # Cloud / service credential files whose NAME carries no env/key/secret
    # token but which routinely hold private keys or tokens. Scoped tightly
    # (extension- or path-anchored) so ordinary source like
    # `service_account_manager.py` is NOT swept in — those reach the indexed
    # gate instead. The CONTENT backstop below catches the rest.
    re.compile(r"service[_-]?account[^/]*\.json$", re.IGNORECASE),
    re.compile(r"[-_]adminsdk[^/]*\.json$", re.IGNORECASE),  # firebase admin sdk
    re.compile(r"(^|[/-])\.netrc$", re.IGNORECASE),
    re.compile(r"(^|[/-])\.pgpass$", re.IGNORECASE),
    re.compile(r"(^|[/-])\.htpasswd$", re.IGNORECASE),
    re.compile(r"(^|[/-])\.dockercfg$", re.IGNORECASE),
    re.compile(r"(^|/)\.docker/config\.json$", re.IGNORECASE),
    re.compile(r"(^|/)\.kube/config$", re.IGNORECASE),
    re.compile(r"\.(p8|ovpn|kdbx|asc|gpg)$", re.IGNORECASE),
)

# Source-code extensions that ARE covered by the AIDOCS index. Host Read
# of these must go through discovery (ai_find/ai_bundle/ai_get_lines) or
# carry a known_exact/lane grant — they're not free-form artifacts. Any
# OTHER extension (logs, csv, config text, assets, structured-binary) is
# treated as a non-indexed artifact that the host Read viewer may open.
_CODE_SOURCE_SUFFIXES: tuple[str, ...] = (
    ".py",
    ".pyi",
    ".pyx",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".cs",
    ".fs",
    ".vb",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".hh",
    ".swift",
    ".m",
    ".mm",
    ".vue",
    ".svelte",
    ".dart",
    ".lua",
    ".sql",
)

# Global host bootstrap files — agents must NEVER mutate these.
# They define per-user routing rules that every AIDOCS session relies
# on; silent drift here creates cascading confusion across projects.
# Only the AIDOCS installer touches them (via its own managed section
# markers). Tier-0 block: no grant phrase, no dev_mode, no gate_enforce
# override flips this. Operator fix-up = edit manually in your editor.
# (Regression guard 2026-04-20 after agent slipped through the gate.)
_GLOBAL_BOOTSTRAP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/|\\)\.claude[/\\]CLAUDE\.md$", re.IGNORECASE),
    re.compile(r"(^|/|\\)\.claude[/\\]AGENTS\.md$", re.IGNORECASE),
    re.compile(r"(^|/|\\)\.config[/\\]opencode[/\\]AGENTS\.md$", re.IGNORECASE),
    re.compile(r"(^|/|\\)\.claude[/\\]settings\.json$", re.IGNORECASE),
    re.compile(r"(^|/|\\)\.config[/\\]opencode[/\\]opencode\.jsonc?$", re.IGNORECASE),
)


def _is_global_bootstrap(path: str) -> bool:
    """True when path points at a per-user AIDOCS bootstrap file."""
    normalized = path.replace("\\", "/")
    return any(p.search(normalized) for p in _GLOBAL_BOOTSTRAP_PATTERNS)


_MEMORY_PREFIX = ".MEMORY/"


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def _is_memory_path(path: str) -> bool:
    """Match `.MEMORY/` anywhere in a normalized path, case-insensitive.

    Accepts both relative paths (`.MEMORY/...`) and absolute paths
    (`D:/.../project/.MEMORY/...`). Case-insensitive because Windows
    filesystems and tool inputs are inconsistent.
    """
    normalized = _normalize_path(path).lower()
    memory_lc = _MEMORY_PREFIX.lower()
    return (
        normalized.startswith(memory_lc)
        or normalized == ".memory"
        or f"/{memory_lc}" in normalized
        or normalized.endswith("/.memory")
    )


# .MEMORY/ paths where writes affect code execution — require user intent
_PROTECTED_MEMORY_PREFIXES: tuple[str, ...] = (
    ".memory/rules/workflow",
    ".memory/rules/security",
    ".memory/config/",
    ".memory/.index/",  # SQLite config/execution store + derived index files
)
# Gate/state files that agents must never modify directly
_INFRASTRUCTURE_MEMORY_PATTERNS: tuple[str, ...] = (
    "query-gate.json",
    "aidocs-managed.json",
    "conductor-state.json",
    "plan_conductor_state.json",
    "skills.json",
    "aidocs.sqlite3",
    "aidocs.sqlite3-journal",
    "aidocs.sqlite3-wal",
    "aidocs.sqlite3-shm",
)


def _is_protected_memory_path(path: str) -> bool:
    """Workflow rules, security rules, config, and gate state files are protected."""
    normalized = _normalize_path(path).lower()
    if any(normalized.startswith(prefix) for prefix in _PROTECTED_MEMORY_PREFIXES):
        return True
    filename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    return filename in _INFRASTRUCTURE_MEMORY_PATTERNS


def _is_sensitive(path: str) -> bool:
    normalized = _normalize_path(path)
    if any(p.search(normalized) for p in _SENSITIVE_PATTERNS):
        return True
    # Check configurable protected patterns from aidocs.toml
    import fnmatch

    from .config import GATE_PROTECTED_PATTERNS

    filename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    return any(fnmatch.fnmatch(filename.lower(), pat.lower()) for pat in GATE_PROTECTED_PATTERNS)


# Bytes sniffed from a to-be-allowed artifact before a host read. Credential
# material lives at the top of key/token/env files; a bounded head keeps the
# gate cheap while catching the secrets a NAME-only classifier misses.
_SECRET_SNIFF_BYTES = 65536


def _abs_for_content_sniff(gate_state: dict[str, Any], normalized: str) -> str | None:
    """Resolve the on-disk path to sniff for an artifact-level read.

    External/absolute targets are already filesystem paths; project-internal
    relative targets are joined onto ``gate_state['project_root']`` (the same
    root the Read-tool gate stamps). Returns None when no usable path exists
    (the content backstop is then skipped — name/zone rules still apply).
    """
    if _is_external_path(normalized):
        return normalized
    root = gate_state.get("project_root")
    if not root:
        return None
    base = _normalize_path(str(root)).rstrip("/")
    return f"{base}/{normalized}" if base else None


def _content_is_secret(abs_path: str | None) -> bool:
    """True when an artifact's CONTENT carries credential material.

    Reuses ``output_guard.scan_text`` (the same detector the egress guard and
    write-side judge share) on a bounded head, and blocks ONLY on
    ``credential:*`` findings — high-confidence key formats (PEM/OpenSSH/RSA
    private keys, GitHub/OpenAI/Anthropic/Stripe/Slack keys, JWTs) and quoted
    ``api_key=``/``password=``/``secret=`` assignments. The broad
    ``sensitive:*`` env/ssh heuristics are intentionally excluded here to keep
    false positives off Makefiles / .properties (real ``.env`` files are
    name-blocked upstream).

    Fail-safe: a missing / unreadable / empty file → not secret (it either
    cannot be read at all, or is governed by the name + zone rules).
    """
    if not abs_path:
        return False
    try:
        import os

        if not os.path.isfile(abs_path):
            return False
        with open(abs_path, "rb") as fh:
            head = fh.read(_SECRET_SNIFF_BYTES)
    except (OSError, ValueError):
        return False
    if not head:
        return False
    text = head.decode("utf-8", errors="replace")
    try:
        from .output_guard import scan_text

        result = scan_text(text, redact=False)
    except Exception:
        return False
    return any(f.category.startswith("credential:") for f in result.findings)


def _secret_content_block(normalized: str) -> GateDecision:
    return GateDecision(
        allowed=False,
        level="sensitive_file_protection",
        reason=(
            _gate_msg("sensitive_file_blocked", path=normalized)
            + " Credential material detected in file content "
            "(name-based classification missed it)."
        ),
    )


def _is_protected_config(path: str) -> bool:
    normalized = _normalize_path(path)
    filename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    return filename.lower() in _PROTECTED_CONFIG_FILES


def _is_infrastructure(path: str) -> bool:
    normalized = _normalize_path(path).lower()
    return any(normalized.startswith(prefix) for prefix in _INFRASTRUCTURE_PREFIXES)


def _is_hard_protected_data(path: str) -> bool:
    """True iff the path is a hard-protected project-internal DATA file
    (sqlite DB, AIDOCS index, gate-state JSON, or an operator-configured
    extra). Single source: hard_protected_paths.is_hard_protected, unioned
    with the configured EXTRA globs. See that module for the doctrine.
    """
    from .config import GATE_HARD_PROTECTED_PATTERNS
    from .hard_protected_paths import is_hard_protected

    return is_hard_protected(_normalize_path(path), extra_patterns=GATE_HARD_PROTECTED_PATTERNS)


def _is_sqlite_data(path: str) -> bool:
    """True iff the path is a sqlite database (or -wal/-shm/-journal sidecar).
    These are config_set-only; a direct file-write is never permitted.
    """
    base = _normalize_path(path).rsplit("/", 1)[-1].lower()
    return ".sqlite3" in base


def _is_safe_grantable_path(path: str) -> bool:
    """Paths that can be added to known_exact_paths.

    Only filters path-shape hazards (absolute paths, traversal). Infrastructure
    and protected-config checks belong at the write gate — silently dropping
    read grants here left AIDOCS-repo files unreadable via indexed tools
    despite discovery tools (ai_bundle, ai_find, ai_text_search) having
    just returned the same path.
    """
    normalized = _normalize_path(path)
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return False
    if ".." in normalized.split("/"):
        return False
    return True


def _is_path_granted(gate_state: dict[str, Any], path: str) -> bool:
    """Check if path appears in known_exact_paths or lane_exact_paths."""
    normalized = _normalize_path(path)
    known = gate_state.get("known_exact_paths")
    if isinstance(known, list) and normalized in known:
        return True
    lane = gate_state.get("lane_exact_paths")
    if isinstance(lane, list) and normalized in lane:
        return True
    return False


def _is_external_path(normalized: str) -> bool:
    """True for an absolute path / home-relative path that points OUTSIDE
    the current project's relative tree.
    """
    if not normalized:
        return False
    if normalized.startswith("/") or normalized.startswith("~"):
        return True
    if re.match(r"^[A-Za-z]:/", normalized):  # Windows drive
        return True
    return False


def _under_approved_external_root(gate_state: dict[str, Any], normalized: str) -> bool:
    roots = gate_state.get("approved_external_roots")
    if not isinstance(roots, list):
        return False
    target = normalized.lower()
    for root in roots:
        r = _normalize_path(str(root)).lower().rstrip("/")
        if r and (target == r or target.startswith(r + "/")):
            return True
    return False


def _is_recorded_artifact(gate_state: dict[str, Any], normalized: str) -> bool:
    arts = gate_state.get("session_artifact_paths")
    if not isinstance(arts, list):
        return False
    target = normalized.lower()
    return any(_normalize_path(str(a)).lower() == target for a in arts)


def _is_session_task_output(gate_state: dict[str, Any], normalized: str) -> bool:
    """True for THIS session's fresh task/deploy output capture.

    Replaces the earlier broad ``<TEMP>/claude/**/tasks/*`` SHAPE exemption
    (any session, any project). Delegates to
    ``session_artifact.is_session_task_artifact``: the path must sit under
    ``<TEMP>/claude/<project-slug>/<session-uuid>/tasks/`` with a safe capture
    extension, the slug must match the CURRENT project, the session-UUID must
    be one of the current session ids (``host_session_ids`` in gate_state —
    the hook-stamped host id and/or the managed session id), and the file must
    be FRESH (mtime within the TTL). Another session's UUID, another project's
    slug, a stale capture, a secret-named file, a ``..`` traversal, or an
    arbitrary TEMP path all return False. Consulted AFTER the sensitive/secret
    and ``..`` floors, so it can never expose a secret or a ``..`` escape.
    """
    try:
        from .session_artifact import is_session_task_artifact

        return is_session_task_artifact(
            normalized,
            project_root=gate_state.get("project_root"),
            host_session_ids=gate_state.get("host_session_ids"),
        )
    except Exception:
        return False


def host_read_hard_block(path: str) -> GateDecision | None:
    """Return a blocking GateDecision for paths that NO exemption, lane
    grant, or approved root may ever open: secrets/sensitive, global
    bootstrap/config, and parent-traversal escapes. Returns None when the
    path is not hard-blocked.

    This is the floor the read gate runs BEFORE consulting
    ``gate.exempt_paths`` / ``gate.exempt_extensions`` or lane raw-read
    grants, so a broad exemption can never expose ``.env``, ``*.pem``,
    ``.ssh/id_rsa``, or a ``..`` escape. (Mirrors steps 1–2 of
    ``host_read_decision``.)
    """
    normalized = _normalize_path(path)
    if not normalized:
        return None
    if _is_sensitive(normalized) or _is_global_bootstrap(normalized):
        return GateDecision(
            allowed=False,
            level="sensitive_file_protection",
            reason=_gate_msg("sensitive_file_blocked", path=normalized),
        )
    if ".." in normalized.split("/"):
        return GateDecision(
            allowed=False,
            level="read_gate",
            reason=(
                f"Read blocked: '{normalized}' escapes the project via '..'. "
                f"Reads are confined to the project tree (or an approved "
                f"external root)."
            ),
        )
    return None


def host_read_decision(gate_state: dict[str, Any], path: str) -> GateDecision:
    """Decide whether the NORMAL HOST Read tool may open ``path`` in
    managed mode. Host Read is the operator's artifact viewer — distinct
    from raw EDIT (always blocked) and raw shell. See the read-gate law
    in claude_hook/access docs.

    Order (block beats allow):
      1. sensitive / secrets / SSH / global-bootstrap   → BLOCK
      2. parent-traversal escape (..)                   → BLOCK
      3. explicit grant (known_exact / lane_exact)      → ALLOW
      4. external path: approved root / recorded
         artifact → ALLOW, otherwise                    → BLOCK
      5. memory-internal                                → ALLOW
      6. protected config (aidocs.toml…) w/o grant      → BLOCK
      7. indexed source-code extension w/o grant        → BLOCK (discover)
      8. any other project-internal, non-sensitive file → ALLOW (artifact)
    """
    normalized = _normalize_path(path)
    if not normalized:
        return GateDecision(
            allowed=False,
            level="read_gate",
            reason="Read blocked: no path supplied.",
        )

    # 0. Relativize an absolute path that lives INSIDE the project, so the
    #    indexed-source / artifact / grant rules apply (otherwise an
    #    absolute in-project path would be misread as unknown-external).
    #    Callers that know the project root pass it via gate_state.
    proj = gate_state.get("project_root") if isinstance(gate_state, dict) else None
    if proj:
        proj_norm = _normalize_path(str(proj)).rstrip("/")
        if proj_norm and normalized.lower().startswith((proj_norm + "/").lower()):
            normalized = normalized[len(proj_norm) + 1 :]

    # 1. Secrets / sensitive / bootstrap — extension never exempts these.
    if _is_sensitive(normalized) or _is_global_bootstrap(normalized):
        return GateDecision(
            allowed=False,
            level="sensitive_file_protection",
            reason=_gate_msg("sensitive_file_blocked", path=normalized),
        )

    # 2. Traversal escape.
    if ".." in normalized.split("/"):
        return GateDecision(
            allowed=False,
            level="read_gate",
            reason=(
                f"Read blocked: '{normalized}' escapes the project via '..'. "
                f"Reads are confined to the project tree (or an approved "
                f"external root)."
            ),
        )

    # 3. Discovered / granted exact path (covers indexed source the agent
    #    already surfaced via ai_find/ai_bundle/etc.).
    if _is_path_granted(gate_state, normalized):
        # A grant unblocks raw read — but a granted NON-source data file
        # (config.json / *.yaml / *.dat surfaced via ai_bundle / ai_text_search
        # / ai_create_file) can still hold credentials, and the grant would
        # otherwise skip the step-8 content backstop. Sniff those. Source
        # files keep the fast path: their hardcoded-secret risk is a write-side
        # concern, and content-sniffing every code read false-positives on
        # fixtures / pattern files.
        if not normalized.lower().endswith(_CODE_SOURCE_SUFFIXES) and _content_is_secret(
            _abs_for_content_sniff(gate_state, normalized),
        ):
            return _secret_content_block(normalized)
        return GateDecision(allowed=True, level="raw_tool_discovery_grant")

    # 4. External paths: only approved roots or recorded artifacts.
    if _is_external_path(normalized):
        if _under_approved_external_root(gate_state, normalized):
            if _content_is_secret(_abs_for_content_sniff(gate_state, normalized)):
                return _secret_content_block(normalized)
            return GateDecision(allowed=True, level="approved_external_workspace")
        if _is_recorded_artifact(gate_state, normalized):
            if _content_is_secret(_abs_for_content_sniff(gate_state, normalized)):
                return _secret_content_block(normalized)
            return GateDecision(allowed=True, level="session_artifact")
        if _is_session_task_output(gate_state, normalized):
            # THIS session's OWN fresh task/deploy output (sensitive +
            # traversal floors already ran above). Bound to project_root +
            # session-UUID + freshness, not a blanket TEMP shape.
            return GateDecision(allowed=True, level="claude_session_task_artifact")
        return GateDecision(
            allowed=False,
            level="read_gate",
            reason=(
                f"Read blocked: '{normalized}' is outside the project and "
                f"not an approved external root or recorded artifact. Add an "
                f"approved external root or get an explicit grant."
            ),
        )

    # 5. .MEMORY/ internal — always readable (non-sensitive already checked).
    if _is_memory_path(normalized):
        return GateDecision(allowed=True, level="memory_path_exemption")

    # 6. Protected config files need an explicit discovery grant.
    basename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    if basename in {"aidocs.toml", "aidocs-plugin.json"}:
        return GateDecision(
            allowed=False,
            level="read_gate",
            reason=_gate_msg("read_gate_blocked", path=normalized),
        )

    # 7. Indexed source code → push to discovery-first tools.
    if normalized.lower().endswith(_CODE_SOURCE_SUFFIXES):
        return GateDecision(
            allowed=False,
            level="indexed_file_gate",
            reason=(
                f"Read blocked: '{normalized}' is indexed source. Use "
                f"ai_find / ai_bundle / ai_get_lines (after discovery the "
                f"raw read is allowed)."
            ),
        )

    # 8. Any other project-internal, non-sensitive file is a host artifact
    #    (logs, csv, config text, PDFs, images, structured-binary, …) — BUT
    #    a name-based classifier misses secrets hidden in innocuously named,
    #    non-source files (serviceAccount.json, tokens.dat, config.json with
    #    an embedded key). Sniff the content before admitting it.
    if _content_is_secret(_abs_for_content_sniff(gate_state, normalized)):
        return _secret_content_block(normalized)
    return GateDecision(allowed=True, level="host_read_artifact")


def _extract_bash_commands(tool_input: dict[str, Any]) -> list[str]:
    """Extract all base commands from a Bash tool_input (handles && and || chains)."""
    import shlex

    command = tool_input.get("command")
    if not command or not isinstance(command, str):
        return []
    cmd_str = command.strip()
    # Split on && and || only (not ; which appears inside quoted strings)
    import re as _re

    segments = _re.split(r"\s*(?:&&|\|\|)\s*", cmd_str)
    results: list[str] = []
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        # Take first command in pipe chain (but not inside quotes)
        # Simple heuristic: split on | not preceded/followed by quotes
        pipe_first = (
            segment.split("|")[0].strip()
            if "|" in segment and '"' not in segment.split("|")[0]
            else segment
        )
        # Strip heredoc content
        pipe_first = pipe_first.split("<<")[0].strip()
        try:
            parts = shlex.split(pipe_first)
        except ValueError:
            parts = pipe_first.split()
        if not parts:
            continue
        # Skip env var assignments
        for part in parts:
            if "=" in part and not part.startswith("-"):
                continue
            # Normalize fully-qualified invocations to the bare binary name:
            # "C:/Program Files/PostgreSQL/17/bin/pg_dump.exe" → "pg_dump"
            # "/usr/local/bin/python3" → "python3"
            # Agents often get exact paths from shells like Git Bash; the
            # allowlist holds basenames only. Without this, the gate blocks
            # any invocation that doesn't happen to be on PATH.
            import os as _os

            normalized = _os.path.basename(part).lower()
            normalized = normalized.removesuffix(".exe")
            if normalized:
                results.append(normalized)
            else:
                results.append(part.lower())
            break
    return results


def check_bash_allowed(
    tool_input: dict[str, Any],
    *,
    project_root: Path | None = None,
    session_id: str | None = None,
) -> GateDecision:
    """REMOVED 2026-04-25 — use bash_policy.evaluate_bash_policy.

    Legacy `security.bash_allowed` substring allowlist was retired with
    the [bash] / [raw_bash] declarative tables. This shim now hard-fails
    so any third-party caller still importing `check_bash_allowed`
    surfaces a clear error rather than silently degrading. Remove your
    call site and switch to `bash_policy.evaluate_bash_policy`.
    """
    raise NotImplementedError(
        "check_bash_allowed was removed 2026-04-25. Use "
        "bash_policy.evaluate_bash_policy with the `bash` or `raw_bash` "
        "config table. See .MEMORY/system/security-gates.md §15.",
    )


def _extract_file_path(tool_input: dict[str, Any]) -> str | None:
    """Extract file path from tool_input regardless of parameter name.

    Used by the gate-exempt-extension/-path check, which fires after a
    raw-tool block decision is being considered. Accepts the path-key
    set (with conflict detection — see _extract_path) and falls back
    to pattern/command when no path key is present. PathInputConflict
    bubbles up to the caller; the exempt check loses its grounds and
    the block stands.
    """
    direct = _extract_path(tool_input)
    if direct:
        return direct
    for key in ("pattern", "command"):
        val = tool_input.get(key)
        if val and isinstance(val, str):
            stripped = val.strip()
            if stripped:
                return stripped
    return None


def _is_gate_exempt(tool_input: dict[str, Any]) -> bool:
    """Check if the tool target file is exempt from gate blocking.

    Exempt files (by extension or explicit path) are allowed through
    the raw tool gate even in managed mode. Configured via aidocs.toml:
        [gate]
        exempt_extensions = [".output", ".log", ".txt", ...]
        exempt_paths = ["some/specific/file.dat"]
    """
    from .config import GATE_EXEMPT_EXTENSIONS, GATE_EXEMPT_PATHS

    file_path = _extract_file_path(tool_input)
    if not file_path:
        return False

    normalized = _normalize_path(file_path).lower()

    # Check extension exemptions
    for ext in GATE_EXEMPT_EXTENSIONS:
        if normalized.endswith(ext.lower()):
            return True

    # Check explicit path exemptions
    for exempt in GATE_EXEMPT_PATHS:
        exempt_norm = _normalize_path(exempt).lower()
        if normalized == exempt_norm or normalized.endswith("/" + exempt_norm):
            return True

    return False


class AccessGate:
    """Unified access gate — all callers delegate here."""

    # ── Tier-0: Raw Shell Redirect ──
    #
    # Fires before every other gate. Raw Bash routes around
    # `ai_run`'s bash_policy evaluation (allow/deny tables)
    # and the audit trail written to execution_events + the session
    # journal. The redirect message names the canonical MCP tool so
    # agents migrate without guessing.

    @staticmethod
    def check_raw_shell(
        ctx: GateContext,
        tool_name: str,
        *,
        allow_raw_shell: bool = False,
        user_granted: bool = False,
    ) -> GateDecision:
        """Block raw shell tools on AIDOCS-managed projects (tier-0).

        Per Invariant #38 (security-gates.md, canonical 2026-04-29
        Batch B live): in managed AIDOCS sessions, native Bash and
        peer host shells (PowerShell, pwsh, cmd, wsl, monitor) are
        permanently T0-blocked. NO NLP unlock, NO sticky/session
        grant, NO lane delegation, NO dashboard flag lifts this. The
        ``user_granted`` and ``allow_raw_shell`` parameters are
        ACCEPTED for back-compat with non-managed-mode callers but
        IGNORED for native Bash in managed mode. Use ai_run instead.

        Behavior:
          - Tool not in _RAW_SHELL_TOOLS: allow (not applicable).
          - Unmanaged session: allow (T0 only fires in managed mode).
          - Managed session, native Bash: hard-deny — both unblock
            kwargs ignored. Doctrine-enforced by Invariant #38.
          - Edge case: if a future doctrine amendment legitimately
            adds a managed-mode unblock for a non-bash raw shell,
            that path can re-introduce conditional advisory bypass.
            Today no such doctrine exists; the kwargs are dead-letter.

        The kwargs were the legacy "two unblock paths" dating from
        before Invariant #38 hardened the contract. They are kept in
        the signature so existing callers compile without churn but
        are observably no-ops in the managed/native-Bash case.
        """
        normalized_tool = tool_name.strip().lower()
        if normalized_tool not in _RAW_SHELL_TOOLS:
            return GateDecision(
                allowed=True,
                level="raw_shell_not_applicable",
            )
        if not ctx.managed:
            return GateDecision(allowed=True, level="raw_shell_unmanaged")
        # Invariant #38 hard floor: managed-mode native shell is
        # T0-blocked regardless of user_granted/allow_raw_shell.
        return GateDecision(
            allowed=False,
            level="raw_shell_blocked",
            reason=(
                f"`{tool_name}` blocked. Use mcp__aidocs__ai_run "
                "instead. (Invariant #38: native shell tools are "
                "T0-blocked in managed AIDOCS sessions; no flag or "
                "grant lifts this.)"
            ),
        )

    # ── Tier-0: Edit Redirect ──
    #
    # Fires BEFORE every other gate. Raw edit tools would leave the
    # index stale — other lanes/sessions read old content, edit_history
    # loses the change, audits break. Everyday-English verbs like
    # "edit" mean NLP user-grants are unsafe here, so the only unblock
    # path is the dashboard-set `security.allow_raw_edits` config.

    @staticmethod
    def check_edit_redirect(
        ctx: GateContext,
        tool_name: str,
        *,
        allow_raw_edits: bool = False,
        tool_input: dict[str, Any] | None = None,
        project_root: Any = None,
    ) -> GateDecision:
        """Block raw edit tools on AIDOCS-managed projects (tier-0).

        Scope is the AIDOCS-managed project tree. Edits targeting paths
        outside that tree (sibling repos, /tmp, /var/log, absolute paths
        the user named explicitly) fall through — the AIDOCS index
        doesn't cover them so there's no staleness risk, and raw Edit
        is genuinely the right tool there.

        `allow_raw_edits` comes from `security.allow_raw_edits` config —
        dashboard-only, persistent, ignored by user_granted / dev_mode /
        gate_enforce overrides. When True, returns advisory=True so the
        event stream still records the bypass for audit.
        """
        normalized_tool = tool_name.strip().lower()
        if normalized_tool not in _RAW_EDIT_TOOLS:
            return GateDecision(allowed=True, level="edit_redirect_not_applicable")
        if not ctx.managed:
            return GateDecision(allowed=True, level="edit_redirect_unmanaged")

        # External-path exemption: if the target is outside the managed
        # project's tree AND not under any other AIDOCS project, raw
        # Edit/Write is the correct tool there.
        #
        # Cross-project leak fix (2026-04-27): pre-fix the exemption
        # fired on any path outside THIS project's tree, including
        # paths inside OTHER AIDOCS projects — letting raw Edit/Write
        # bypass those projects' gates entirely. Now: walk up from the
        # target looking for an `.MEMORY/.aidocs/index.aidocs` marker;
        # if found, target is inside some AIDOCS project (cross-project
        # case) and raw Edit/Write is refused with the same redirect
        # message. Walk only fires when target is OUTSIDE the current
        # project — common in-project case stays a fast 2-comparison
        # check.
        if project_root is not None and tool_input:
            try:
                target_path = _extract_path(tool_input)
            except PathInputConflict as conflict:
                return GateDecision(
                    allowed=False,
                    level="edit_redirect_path_conflict",
                    reason=(f"`{tool_name}` blocked: {conflict}"),
                )
            if target_path:
                from pathlib import Path as _Path

                try:
                    resolved = _Path(target_path).expanduser().resolve()
                    root_resolved = _Path(project_root).resolve()
                    in_project = resolved == root_resolved or root_resolved in resolved.parents
                except (OSError, ValueError):
                    in_project = True
                if not in_project:
                    # Check for any AIDOCS marker on ancestors. Cheap:
                    # capped by filesystem depth (~10 stat calls max).
                    from .mcp_server_runtime_helpers import is_aidocs_managed

                    in_other_aidocs = False
                    try:
                        for ancestor in (resolved, *resolved.parents):
                            if is_aidocs_managed(ancestor):
                                in_other_aidocs = True
                                break
                    except (OSError, ValueError):
                        in_other_aidocs = False
                    if not in_other_aidocs:
                        return GateDecision(
                            allowed=True,
                            level="edit_redirect_external_path",
                        )
                    # Falls through to the redirect-blocked return below.

        if allow_raw_edits:
            # Audit breadcrumb — operator has deliberately unblocked, but
            # the event stream records every pass-through so the change
            # remains visible after the fact.
            return GateDecision(
                allowed=True,
                level="edit_redirect_operator_unblocked",
                advisory=True,
            )

        # Route to the right AIDOCS alternative based on the tool's
        # intent family. create → ai_create_file, edit → ai_str_replace
        # or ai_edit_lines, notebook → ai_create_file for .ipynb.
        if normalized_tool == "write":
            suggestion = "Use ai_create_file."
        elif normalized_tool == "notebookedit":
            suggestion = "Use ai_create_file for .ipynb."
        else:
            suggestion = "Use ai_str_replace or ai_edit_lines."
        return GateDecision(
            allowed=False,
            level="edit_redirect_blocked",
            reason=f"`{tool_name}` blocked. {suggestion}",
        )

    # ── Level 1: Managed Mode Gate ──

    @staticmethod
    def check_raw_tool(
        ctx: GateContext,
        tool_name: str,
        *,
        allow_subagents: bool = True,
        tool_input: dict[str, Any] | None = None,
    ) -> GateDecision:
        """Block raw file tools when managed mode is active.

        Exempt files by extension or explicit path list (configured in aidocs.toml
        under [gate].exempt_extensions and [gate].exempt_paths).
        """
        normalized_tool = tool_name.strip().lower()

        # Agent blocking is independent of managed mode file gating
        if normalized_tool == "agent" and not allow_subagents:
            return GateDecision(
                allowed=False,
                level="managed_mode_gate",
                reason=_gate_msg("agent_disabled"),
            )

        if not ctx.managed:
            return GateDecision(allowed=True, level="managed_mode_gate")

        if not ctx.gate_enforce:
            return GateDecision(allowed=True, level="managed_mode_gate")

        # ── Host READ is special (read-gate law) ──────────────────────
        # The normal host Read tool is the operator's artifact viewer, NOT
        # an edit surface. It must open safe non-code / non-indexed /
        # artifact files (PDFs, images, logs, CSVs, config text, approved
        # external artifacts) while still blocking indexed source without
        # discovery, secrets, and unknown external paths. Split it out
        # from the generic raw-file block so it never inherits edit policy.
        if normalized_tool == "read":
            try:
                _conflict = _extract_path(tool_input or {})
                del _conflict
            except PathInputConflict as conflict:
                return GateDecision(
                    allowed=False,
                    level="path_input_conflict",
                    reason=f"`{tool_name}` blocked: {conflict}",
                )
            target_path = _extract_path(tool_input or {}) if tool_input else ""
            # HARD BLOCKS FIRST (one-law goal 2026-05-20): secrets,
            # global bootstrap/config, and traversal can never be lifted by
            # an exemption or a lane grant. Run them before any allow path.
            hard = host_read_hard_block(target_path)
            if hard is not None:
                return hard
            # Operator-configured [gate].exempt_paths/extensions may allow
            # harmless artifacts — but only AFTER the hard-block floor above.
            if tool_input and _is_gate_exempt(tool_input):
                return GateDecision(allowed=True, level="managed_mode_gate")
            # Conductor→lane raw-read grant (lane explicitly delegated read).
            current_lane = ctx.gate_state.get("current_lane_id") if ctx.gate_state else None
            lane_grants = ctx.gate_state.get("lane_raw_tools_granted") if ctx.gate_state else None
            if isinstance(current_lane, str) and current_lane and isinstance(lane_grants, dict):
                granted_for_lane = lane_grants.get(current_lane)
                if isinstance(granted_for_lane, list) and "read" in granted_for_lane:
                    return GateDecision(allowed=True, level="lane_raw_tool_grant")
            return host_read_decision(ctx.gate_state or {}, target_path)

        if normalized_tool in _BLOCKED_RAW_FILE_TOOLS:
            # Path-input conflict (e.g. `file_path` + `filePath` with
            # different values) is refused before any exemption,
            # discovery, or lane-grant branch can fire. Silent
            # precedence selection would let a caller route different
            # paths to different gates — co-conductor 2026-04-30.
            try:
                _conflict_check_path = _extract_path(tool_input or {})
                _conflict_check_pattern = _extract_path_or_pattern(tool_input or {})
                del _conflict_check_path, _conflict_check_pattern
            except PathInputConflict as conflict:
                return GateDecision(
                    allowed=False,
                    level="path_input_conflict",
                    reason=(f"`{tool_name}` blocked: {conflict}"),
                )
            # Extension/path exemptions are only for non-mutating raw file tools.
            # Raw edits must stay blocked because they bypass indexing,
            # edit_history, and audit/rollback surfaces.
            if (
                normalized_tool not in _RAW_EDIT_TOOLS
                and tool_input
                and _is_gate_exempt(tool_input)
            ):
                return GateDecision(allowed=True, level="managed_mode_gate")
            # Discovery-grant for read-only raw tools (canonical
            # 2026-04-30): if the file path is in known_exact_paths
            # or lane_exact_paths, the operator/agent has discovered
            # it via AIDOCS indexed tools (ai_find/ai_investigate/
            # ai_get_symbol_snippet/etc.) and the follow-up raw read
            # is the natural flow — especially on hosts (OpenCode)
            # where the agent's primary read surface IS the raw
            # `read` tool. Edits stay hard-blocked because they
            # bypass indexing/audit; reads after discovery don't.
            #
            # The discovery grant only applies to read-only tools
            # in _BLOCKED_RAW_FILE_TOOLS - _RAW_EDIT_TOOLS (i.e.
            # read, grep, glob).
            if normalized_tool not in _RAW_EDIT_TOOLS and tool_input and ctx.gate_state:
                target_path = _extract_path_or_pattern(tool_input)
                if target_path and _is_path_granted(
                    ctx.gate_state,
                    target_path,
                ):
                    return GateDecision(
                        allowed=True,
                        level="raw_tool_discovery_grant",
                    )
            # Conductor-to-lane raw-tool grant: when the caller is executing
            # inside a lane AND the conductor has explicitly delegated this
            # raw tool to this lane, unblock it. The lane-tool gate still
            # runs separately; this only lifts the raw-file-tool block.
            current_lane = ctx.gate_state.get("current_lane_id") if ctx.gate_state else None
            lane_grants = ctx.gate_state.get("lane_raw_tools_granted") if ctx.gate_state else None
            if isinstance(current_lane, str) and current_lane and isinstance(lane_grants, dict):
                granted_for_lane = lane_grants.get(current_lane)
                if isinstance(granted_for_lane, list) and normalized_tool in granted_for_lane:
                    return GateDecision(
                        allowed=True,
                        level="lane_raw_tool_grant",
                    )
            replacement = _get_raw_tool_replacement(normalized_tool)
            reroute = _build_reroute_call(normalized_tool, tool_input)
            reason = _gate_msg("raw_tool_blocked", tool=normalized_tool, replacement=replacement)
            if reroute:
                reason = f"{reason} {reroute}"
            return GateDecision(
                allowed=False,
                level="managed_mode_gate",
                reason=reason,
            )

        return GateDecision(allowed=True, level="managed_mode_gate")

    # ── Level 2+3+4+5: Read path checks ──

    @staticmethod
    def check_read(
        ctx: GateContext,
        path: str,
        *,
        known_exact_path: bool = False,
        is_indexed: bool = False,
    ) -> GateDecision:
        """Check if a file read is allowed through the cascade."""
        normalized = _normalize_path(path)

        # Indexed files: block raw read UNLESS the caller has discovered
        # the path (grant via ai_find/snippet/etc) or passed
        # known_exact_path=True. ai_get_lines after a valid discovery
        # is the sanctioned indexed-read path; blocking it was the bug.
        if is_indexed and not known_exact_path and not _is_path_granted(ctx.gate_state, normalized):
            return GateDecision(
                allowed=False,
                level="indexed_file_gate",
                reason=_gate_msg("read_gate_blocked", path=normalized),
            )

        # Level 3: Sensitive file protection
        if _is_sensitive(normalized):
            return GateDecision(
                allowed=False,
                level="sensitive_file_protection",
                reason=_gate_msg("sensitive_file_blocked", path=normalized),
            )

        # Config files are readable — agents need to inspect settings
        # Write protection is handled separately in check_write()

        # Level 4: .MEMORY/ reads — always allowed
        if _is_memory_path(normalized):
            return GateDecision(allowed=True, level="memory_path_exemption")

        # Unmanaged mode — no read gate
        if not ctx.managed:
            return GateDecision(allowed=True, level="unmanaged")
        # Lane isolation: when a conductor lane is active, restrict to lane files
        current_lane = ctx.gate_state.get("current_lane_id")
        if current_lane:
            lane_paths = {
                str(item).replace("\\", "/").strip()
                for item in (ctx.gate_state.get("lane_exact_paths") or [])
                if str(item).strip()
            }
            if normalized in lane_paths:
                return GateDecision(allowed=True, level="lane_isolation")
            # .MEMORY/ is always readable even in lane isolation
            if _is_memory_path(normalized):
                return GateDecision(allowed=True, level="memory_path_exemption")
            return GateDecision(
                allowed=False,
                level="lane_isolation",
                reason=f"Lane isolation: '{normalized}' is not in lane '{current_lane}' allowed files. Use expand_lane_scope to add it.",
            )

        # Protected config files — known_exact_path alone is not enough;
        # must be explicitly granted via indexed discovery
        basename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
        if basename in {"aidocs.toml", "aidocs-plugin.json"}:
            if _is_path_granted(ctx.gate_state, normalized):
                return GateDecision(allowed=True, level="read_gate")
            return GateDecision(
                allowed=False,
                level="read_gate",
                reason=_gate_msg("read_gate_blocked", path=normalized),
            )

        # Asset / structured-binary exemption (2026-04-21): files with
        # dedicated parser tools (ai_read_pdf/excel/docx/sqlite/jsonl)
        # AND multimodal assets (images, rendered via Read) are ALWAYS
        # readable. The indexed-read gate exists to push agents toward
        # discovery-first workflows for source code; it has no value
        # for user-dropped PDFs, spreadsheets, or screenshots — those
        # files don't have cross-references to discover. Operator-
        # confirmed: never gate PDFs/images.
        #
        # Plain-text assets (.txt/.log/.csv/.tsv/.md) are NOT on this
        # list — they flow through the normal gate so discovery-first
        # workflows still apply to code-adjacent text. Agents with a
        # legit need can pass known_exact_path=True.
        lower = normalized.lower()
        _ALWAYS_READABLE_SUFFIXES = (
            # Structured-binary with dedicated parser tools
            ".pdf",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
            ".xlsm",
            ".sqlite",
            ".sqlite3",
            ".db",
            ".jsonl",
            ".ndjson",
            # Multimodal assets (Read tool renders them visually)
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".svg",
            ".bmp",
            ".tiff",
            ".heic",
            # Markdown (indexed but low-outline-quality, allow raw read)
            ".md",
            ".mdx",
            ".markdown",
        )
        if lower.endswith(_ALWAYS_READABLE_SUFFIXES):
            return GateDecision(allowed=True, level="asset_exemption")

        # Level 5: Read gate — per-file discovery
        if known_exact_path:
            return GateDecision(allowed=True, level="read_gate")

        if _is_path_granted(ctx.gate_state, normalized):
            return GateDecision(allowed=True, level="read_gate")

        return GateDecision(
            allowed=False,
            level="read_gate",
            reason=_gate_msg("read_gate_blocked", path=normalized),
        )

    # ── Lane tool enforcement ──

    # Conductor-only tools: broad `session_*` / `code_*` allowlist patterns
    # would otherwise let a lane call these and escape isolation. They must
    # be reachable only by the main conductor (no current_lane_id set).
    _CONDUCTOR_ONLY_TOOLS: set[str] = {
        # Conductor→lane delegation — conductor-only; a lane must not
        # be able to grant raw tools to itself or to a peer lane. The
        # tool further refuses any grant the current turn's user intent
        # did not authorize (see runtime_service.grant_raw_tools_for_lane).
        "lane_grant_raw_tools",
    }

    # Default lane allowlist — Expert (white head) baseline per king's
    # role doctrine 2026-05-01. Explicit names, narrow globs only.
    # Globs `ai_*`/`session_*`/`plan_*`/`memory_*`/`skill_*` were too loose:
    # they matched ai_index_sync (admin), session_create (admin),
    # plan_create_from_spec (conductor), memory_capture (king-grant),
    # session_skills_set (admin) — letting Experts reach into doctrine
    # writes and global index mutation. Tightened to:
    #   read + write + run + lifecycle + worker plumbing + read-only inspection.
    # Conductor grants extras via lane_extra_tools (additive, not replacement).
    # King grants via NLP intent / sticky_grants_store.
    _DEFAULT_LANE_ALLOWLIST: tuple[str, ...] = (
        # Read — code-output investigation tools.
        "ai_find",
        "ai_investigate",
        "ai_trace",
        "ai_bundle",
        "ai_search",
        "ai_text_search",
        "ai_get_lines",
        "ai_get_symbol_snippet",
        "ai_get_symbol_info",
        "ai_get_outline",
        "ai_get_dependencies",
        "ai_get_module_files",
        "ai_get_modules",
        "schema_query",
        "ai_read_*",
        # Write / edit — king doctrine 2026-05-01: ai_replace(mode=…)
        # is the canonical unified replace tool. Modes: string (≤200
        # char old_string), anchor (start/end anchors, no middle
        # content shipped), symbol (index-resolved body rewrite),
        # lines (no longer granted-only — Phoenix 2026-05-12: line-
        # based edits now evict the file from known_exact_paths,
        # forcing a re-read before the next line op. Re-read discipline
        # replaces conductor grant). Less tools with more uses.
        # ai_str_replace / ai_anchor_replace / ai_edit_lines were
        # deregistered as MCP tools 2026-05-12 — ai_replace is the
        # only door. Bodies remain as private helpers.
        "ai_replace",
        "ai_batch_edit",
        "ai_create_file",
        "ai_insert_lines",
        # Run / test — lane workers verify via the SUBAGENT-SAFE ai_test
        # (language-agnostic; argv-form, shell=False). Raw ai_run is
        # DELIBERATELY withheld from lane agents (2026-06-13): a worker with
        # raw shell can write to / evade SELF_MOD_GATE_CODE-protected gate
        # code (observed repeatedly). A conductor that genuinely needs to
        # grant a lane raw shell does so explicitly via lane_allowed_tools
        # (merged with this default below) — so ai_run is conductor-grantable,
        # not lane-default. Raw Bash stays tier-0.
        "ai_test",
        # Lifecycle and worker plumbing — boot sequence + progress.
        # ai_session(mode='connect') is the single boot door (paved-road,
        # 2026-05-12 mode-collapse); detects worker env vars and binds +
        # delivers lane plan in one call. ai_task covers begin/update/
        # complete/status via mode dispatch.
        "ai_session",
        "ai_task",
        "verification_gate",
        "ai_plan_report",
        "ai_plan_signal",
        # Conductor messaging (worker → conductor channel).
        "ai_lane_inbox",
        "ai_lane_send",
        "ai_lane_state",
        # Read-only memory — capture stays out of Expert scope (doctrine
        # writes go through king grant or conductor, not Experts).
        "memory_read",
        "memory_search",
        # Read-only inspection.
        "ai_index_status",
        "index_status",
        "ai_status",
        "ai_jobs",
    )

    @staticmethod
    def check_lane_tool(ctx: GateContext, tool_name: str) -> GateDecision:
        """Check if a tool is allowed in the current lane scope.

        When a conductor lane is active, only tools matching the lane's
        allowed tool patterns are permitted. Configured via:
            conductor.lane_allowed_tools = ["code_*", "session_*", ...]
              — REPLACES the default allowlist entirely (narrow/override).
            conductor.lane_extra_tools = ["mcp__custom__tool", ...]
              — EXTENDS whatever base allowlist is in effect (default or
                lane_allowed_tools). Use this to grant one extra tool
                without redefining the whole list.

        Conductor-only tools (lane_grant_raw_tools) are blocked regardless
        of allowlist — a lane must not be able to grant raw tools to itself
        or to a peer lane.
        """
        current_lane = ctx.gate_state.get("current_lane_id")
        if not current_lane:
            return GateDecision(allowed=True, level="no_lane")

        # Normalize tool name — strip MCP namespace prefixes so patterns
        # match both bare and prefixed forms. Two-step: try the full
        # `mcp__aidocs__` first (preserves longest-match), then fall
        # back to the generic `mcp__` prefix to cover custom MCP servers
        # (`mcp__custom__*`, `mcp__playwright__*`, etc.). 2026-04-25
        # audit: previous version only stripped `mcp__aidocs__`,
        # leaving custom MCP names un-normalized.
        name = tool_name.strip().lower()
        for prefix in ("mcp__aidocs__", "mcp__"):
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        # Conductor-only tools: hard-block before any allowlist match.
        # These must remain unreachable from a lane even when the lane's
        # allowlist would otherwise permit them via a broad wildcard.
        if name in AccessGate._CONDUCTOR_ONLY_TOOLS:
            return GateDecision(
                allowed=False,
                level="lane_tool_blocked",
                reason=(
                    f"Tool '{tool_name}' is conductor-only and cannot be "
                    f"called from lane '{current_lane}'. Granting raw-tool "
                    f"access from inside a lane would defeat lane isolation; "
                    f"ask the conductor to expand tool permissions instead."
                ),
            )

        # Lane-system tools: always allowed regardless of allowlist. Lanes
        # need these to read context and report progress. Also includes
        # conductor-self-rescue tools — a conductor accidentally trapped
        # in a worker's lane scope MUST be able to call conductor_lane_exit
        # etc. without the gate blocking the one tool that fixes the trap
        # (chicken-and-egg bug reported 2026-04-20 by dental session).
        # Tools that should remain worker-refused despite being in this
        # allowlist env-gate off AIDOCS_EXPERT_LANE_ID internally.
        _LANE_SYSTEM_TOOLS = {
            # Task + session lifecycle (mode-dispatched, 2026-05-12 collapse).
            "ai_task",
            "ai_session",
            # Plan / conductor progress reporting + claim-next-lane.
            # ai_plan_dispatch is the agent's lane-claim entry — without
            # it in the system-tool set, a worker can't pick up its own
            # next lane packet without an explicit allowlist grant
            # (2026-05-16 fix; was a silent gap during the plan-tool
            # surface consolidation).
            "ai_plan_dispatch",
            "ai_plan_report",
            "ai_plan_signal",
            "ai_plan_expand",
            # Read-only session + memory.
            "memory_read",
            "memory_search",
            "ai_get_lines",
            "ai_get_symbol_snippet",
            "metrics_snapshot",
            # Conductor-self-rescue (env-gated off workers by the
            # tool itself). Without these, a trapped conductor has no
            # escape path from a sticky lane id left by a worker.
            "ai_lane_exit",
            "ai_seat",
            # Read-only worker monitoring — a conductor MUST be able
            # to see worker status even when its own session was
            # lane-bound by the lane's worker.
            "ai_status",
            "ai_jobs",
            # Read-only mode inspection. mode_set / mode_clear stay
            # blocked so a worker can't self-elevate; mode_get is read.
            "mode_get",
            # Host-native CLI tools — AIDOCS lane gate has no jurisdiction
            # over them (CLI's --allowedTools enforces).
            "toolsearch",
        }
        if name in _LANE_SYSTEM_TOOLS:
            return GateDecision(allowed=True, level="lane_system_tool")

        # Build the effective allowlist.
        allowed_patterns = list(ctx.gate_state.get("lane_allowed_tools") or [])
        extra_patterns = list(ctx.gate_state.get("lane_extra_tools") or [])

        if allowed_patterns:
            # Explicit allowlist present — full REPLACE of the default,
            # plus any extras on top.
            all_patterns = allowed_patterns + extra_patterns
        else:
            # No explicit allowlist — use default, plus any extras on top.
            # This makes lane_extra_tools an EXTENSION, not a replacement,
            # so a conductor can grant one extra tool without losing the
            # default AIDOCS surface.
            all_patterns = list(AccessGate._DEFAULT_LANE_ALLOWLIST) + extra_patterns

        import fnmatch

        for pattern in all_patterns:
            if fnmatch.fnmatch(name, pattern.lower()):
                return GateDecision(allowed=True, level="lane_tool_allowed")

        # Also check full tool name (for mcp__custom__tool patterns).
        full_name = tool_name.strip().lower()
        for pattern in all_patterns:
            if fnmatch.fnmatch(full_name, pattern.lower()):
                return GateDecision(allowed=True, level="lane_tool_allowed")

        return GateDecision(
            allowed=False,
            level="lane_tool_blocked",
            reason=f"Tool '{tool_name}' is not in the allowed tool list for lane '{current_lane}'. "
            f"Use AIDOCS MCP tools instead, or ask the conductor to expand tool permissions.",
        )

    # ── Level 2+3+4+6: Edit path checks ──

    @staticmethod
    def check_edit(ctx: GateContext, path: str) -> GateDecision:
        """Check if a file edit is allowed — requires prior discovery."""
        normalized = _normalize_path(path)

        # Level 3: Sensitive file protection
        if _is_sensitive(normalized):
            return GateDecision(
                allowed=False,
                level="sensitive_file_protection",
                reason=f"Edit access to sensitive file blocked: {normalized}",
            )

        # Level 2: Infrastructure protection
        if _is_protected_config(normalized):
            return GateDecision(
                allowed=False,
                level="infrastructure_protection",
                reason=f"Edit access to AIDOCS config file blocked: {normalized}",
            )

        # Unmanaged mode — no edit gate
        if not ctx.managed:
            return GateDecision(allowed=True, level="unmanaged")

        # Lane isolation: when a conductor lane is active, restrict to lane files
        current_lane = ctx.gate_state.get("current_lane_id")
        if current_lane:
            lane_paths = {
                str(item).replace("\\", "/").strip()
                for item in (ctx.gate_state.get("lane_exact_paths") or [])
                if str(item).strip()
            }
            if normalized in lane_paths:
                return GateDecision(allowed=True, level="lane_isolation")
            return GateDecision(
                allowed=False,
                level="lane_isolation",
                reason=f"Lane isolation: '{normalized}' is not in lane '{current_lane}' allowed files.",
            )

        # Level 6: Edit gate — file must be previously discovered
        if _is_path_granted(ctx.gate_state, normalized):
            return GateDecision(allowed=True, level="edit_gate")

        return GateDecision(
            allowed=False,
            level="edit_gate",
            reason=_gate_msg("edit_gate_blocked", path=normalized),
        )

    # ── Level 2+3+4: Write path checks (new file creation) ──

    @staticmethod
    def check_write(
        ctx: GateContext,
        path: str,
        *,
        config_edit_mode: str | None = None,
        has_intent: bool = False,
    ) -> GateDecision:
        """Check if a file write/create is allowed."""
        from .config import GATE_HARD_PROTECTED_PATTERNS
        from .hard_protected_paths import hard_protected_reason

        normalized = _normalize_path(path)

        # Tier 0: global host bootstrap files (e.g. ~/.claude/CLAUDE.md).
        # NEVER agent-writable — not via dev_mode, not via user-intent,
        # not via gate_enforce=False. Only the AIDOCS installer touches
        # these, and it runs outside the agent gate entirely.
        if _is_global_bootstrap(normalized):
            return GateDecision(
                allowed=False,
                level="global_bootstrap_tier0",
                reason=(
                    f"Global host bootstrap file is never agent-writable: "
                    f"{normalized}. Edit manually in your editor; only "
                    f"the AIDOCS installer mutates this path."
                ),
            )

        # Tier 0b: Hard-protected project-internal DATA files — sqlite DBs,
        # AIDOCS index, gate-state JSON. A structural fence ABOVE the
        # dev_mode/infrastructure tier and immune to gate_enforce=False.
        #   * sqlite → ALWAYS denied. The only write path is config_set
        #     (canonical ConfigStore service), which never routes through
        #     check_write. No flag/role unlocks a direct file-write.
        #   * other data → denied unless allow_hard_protected_edit (the
        #     RBAC-resolved unlock: role check + escalate, set upstream).
        if _is_hard_protected_data(normalized):
            if _is_sqlite_data(normalized):
                return GateDecision(
                    allowed=False,
                    level="hard_protected_data",
                    reason=(
                        f"'{normalized}' is a project sqlite database. It is "
                        f"writable ONLY via config_set (the canonical store) — "
                        f"never by a direct file edit. No override unlocks this."
                    ),
                )
            if ctx.allow_hard_protected_edit:
                return GateDecision(allowed=True, level="hard_protected_data")
            return GateDecision(
                allowed=False,
                level="hard_protected_data",
                reason=(
                    hard_protected_reason(normalized, extra_patterns=GATE_HARD_PROTECTED_PATTERNS)
                    or f"'{normalized}' is a hard-protected project data file."
                ),
            )

        # Level 3: Sensitive file protection
        if _is_sensitive(normalized):
            return GateDecision(
                allowed=False,
                level="sensitive_file_protection",
                reason=_gate_msg("sensitive_file_blocked", path=normalized),
            )

        # Level 2a: Config files — blocked unless allow_config_edit
        if _is_protected_config(normalized):
            if ctx.allow_config_edit:
                return GateDecision(allowed=True, level="infrastructure_protection")
            return GateDecision(
                allowed=False,
                level="infrastructure_protection",
                reason=_gate_msg("infrastructure_config_blocked", path=normalized),
            )

        # Level 2b: Infrastructure paths — blocked unless dev_mode
        if _is_infrastructure(normalized) and not ctx.dev_mode:
            return GateDecision(
                allowed=False,
                level="infrastructure_protection",
                reason=_gate_msg("infrastructure_source_blocked", path=normalized),
            )

        # Level 4: .MEMORY/ writes — workflow/security rules ALWAYS need user intent
        # (gate_enforce=False bypasses other gates but NOT workflow/security rules)
        if _is_memory_path(normalized):
            if _is_protected_memory_path(normalized):
                # Always intent-gated — these files control execution behavior
                if has_intent:
                    return GateDecision(allowed=True, level="memory_write_intent_gate")
                return GateDecision(
                    allowed=False,
                    level="memory_write_intent_gate",
                    reason=_gate_msg("memory_write_blocked", path=normalized),
                )
            # Non-protected memory paths (session files, journals, domains) — freely writable
            return GateDecision(allowed=True, level="memory_path_exemption")

        return GateDecision(allowed=True, level="allowed")

    # ── Discovery grants ──

    @staticmethod
    def grant_discovery(
        store: QueryGateStore,
        project_root: Path,
        session_id: str,
        tool_name: str,
        paths: list[str],
    ) -> None:
        """Grant per-file read access for discovered paths."""
        safe_paths = [_normalize_path(p) for p in paths if _is_safe_grantable_path(p)]
        if not safe_paths:
            return

        state = store.get(project_root, session_id)
        existing = [
            str(item) for item in state.get("known_exact_paths", []) if isinstance(item, str)
        ]
        merged = list(dict.fromkeys(existing + safe_paths))

        store.set(
            project_root,
            session_id,
            last_tool=f"discovery:{tool_name}",
            known_exact_paths=merged,
        )
