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
    dev_mode: bool          # unlocks AIDOCS source editing
    allow_config_edit: bool # unlocks aidocs.toml editing
    gate_enforce: bool      # tool gates active (bash allowlist, raw tools, destructive)
    gate_state: dict[str, Any]


@dataclass(slots=True)
class GateDecision:
    allowed: bool
    level: str
    reason: str | None = None


# ── Constants ──

_BLOCKED_RAW_FILE_TOOLS: set[str] = {"read", "grep", "glob", "edit", "write"}

def _gate_msg(key: str, **kwargs: str) -> str:
    """Load gate message from action_hooks TOML with variable substitution."""
    from .config import render_interaction_text
    return render_interaction_text(f"interaction.gate_messages.{key}", **kwargs)


def _get_raw_tool_replacement(tool: str) -> str:
    from .config import render_interaction_text
    text = render_interaction_text(f"interaction.raw_tool_replacements.{tool}")
    if text and not text.startswith("{"):
        return text
    return f"Use the equivalent AIDOCS MCP tool instead of `{tool}`."

# Infrastructure protection — always blocked for writes
_PROTECTED_CONFIG_FILES: set[str] = {"aidocs.toml", "aidocs-plugin.json"}

# Infrastructure paths — blocked unless dev_mode
_INFRASTRUCTURE_PREFIXES: tuple[str, ...] = ("mcp/server/aidocs_mcp/",)

# Sensitive file patterns
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)\.env(\.|$)", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"\.(key|pem|pfx)$", re.IGNORECASE),
)

_MEMORY_PREFIX = ".MEMORY/"


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def _is_memory_path(path: str) -> bool:
    normalized = _normalize_path(path)
    return normalized.startswith(_MEMORY_PREFIX) or normalized == ".MEMORY"


# .MEMORY/ paths where writes affect code execution — require user intent
_PROTECTED_MEMORY_PREFIXES: tuple[str, ...] = (
    ".memory/rules/workflow",
    ".memory/rules/security",
    ".memory/config/",
)
# Gate/state files that agents must never modify directly
_INFRASTRUCTURE_MEMORY_PATTERNS: tuple[str, ...] = (
    "query-gate.json",
    "aidocs-managed.json",
    "conductor-state.json",
    "skills.json",
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
    from .config import GATE_PROTECTED_PATTERNS
    import fnmatch
    filename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    return any(fnmatch.fnmatch(filename.lower(), pat.lower()) for pat in GATE_PROTECTED_PATTERNS)

def _is_sensitive(path: str) -> bool:
    normalized = _normalize_path(path)
    return any(p.search(normalized) for p in _SENSITIVE_PATTERNS)


def _is_protected_config(path: str) -> bool:
    normalized = _normalize_path(path)
    filename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    return filename.lower() in _PROTECTED_CONFIG_FILES


def _is_infrastructure(path: str) -> bool:
    normalized = _normalize_path(path).lower()
    return any(normalized.startswith(prefix) for prefix in _INFRASTRUCTURE_PREFIXES)


def _is_safe_grantable_path(path: str) -> bool:
    """Paths that can be added to known_exact_paths (excludes protected files)."""
    normalized = _normalize_path(path)
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return False
    if ".." in normalized.split("/"):
        return False
    if _is_protected_config(normalized):
        return False
    if _is_infrastructure(normalized):
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


def _extract_bash_commands(tool_input: dict[str, Any]) -> list[str]:
    """Extract all base commands from a Bash tool_input (handles && and || chains)."""
    import shlex
    command = tool_input.get("command")
    if not command or not isinstance(command, str):
        return []
    cmd_str = command.strip()
    # Split on && and || only (not ; which appears inside quoted strings)
    import re as _re
    segments = _re.split(r'\s*(?:&&|\|\|)\s*', cmd_str)
    results: list[str] = []
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        # Take first command in pipe chain (but not inside quotes)
        # Simple heuristic: split on | not preceded/followed by quotes
        pipe_first = segment.split("|")[0].strip() if "|" in segment and '"' not in segment.split("|")[0] else segment
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
            results.append(part.lower())
            break
    return results


def check_bash_allowed(tool_input: dict[str, Any]) -> GateDecision:
    """Check if all bash commands in a chain are in the allowlist. Block-all-permit-some."""
    from .config import GATE_BASH_ALLOWED
    commands = _extract_bash_commands(tool_input)
    if not commands:
        return GateDecision(
            allowed=False,
            level="bash_gate",
            reason="Bash command could not be parsed.",
        )
    cmd_str = str(tool_input.get("command") or "").strip().lower()
    for base_cmd in commands:
        allowed = False
        for allow_entry in GATE_BASH_ALLOWED:
            if base_cmd == allow_entry or f"{base_cmd} " in cmd_str and cmd_str.find(base_cmd) >= 0:
                # Also check multi-word entries like "python -m pytest"
                if " " in allow_entry and allow_entry in cmd_str:
                    allowed = True
                    break
                elif " " not in allow_entry and base_cmd == allow_entry:
                    allowed = True
                    break
        if not allowed:
            alt = ""
            if base_cmd == "git":
                alt = " Use the git_ops tool instead (e.g. git_ops(op='status'))."
            elif base_cmd in ("cat", "head", "tail"):
                alt = " Use code_get_lines instead."
            elif base_cmd in ("grep", "rg"):
                alt = " Use code_text_search or code_find instead."
            elif base_cmd in ("find", "ls"):
                alt = " Use code_search or code_get_module_files instead."
            return GateDecision(
                allowed=False,
                level="bash_gate",
                reason=f"Bash command `{base_cmd}` is not in the allowlist.{alt} Add it to [gate].bash_allowed in aidocs.toml, or ask the user to run it.",
            )
    return GateDecision(allowed=True, level="bash_gate")

def _extract_file_path(tool_input: dict[str, Any]) -> str | None:
    """Extract file path from tool_input regardless of parameter name."""
    for key in ("file_path", "path", "pattern", "command"):
        val = tool_input.get(key)
        if val and isinstance(val, str):
            return val.strip()
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

        if normalized_tool in _BLOCKED_RAW_FILE_TOOLS:
            # Check file path exemptions before blocking
            if tool_input and _is_gate_exempt(tool_input):
                return GateDecision(allowed=True, level="managed_mode_gate")
            replacement = _get_raw_tool_replacement(normalized_tool)
            return GateDecision(
                allowed=False,
                level="managed_mode_gate",
                reason=_gate_msg("raw_tool_blocked", tool=normalized_tool, replacement=replacement),
            )

        return GateDecision(allowed=True, level="managed_mode_gate")

    # ── Level 2+3+4+5: Read path checks ──

    @staticmethod
    def check_read(
        ctx: GateContext,
        path: str,
        *,
        known_exact_path: bool = False,
    ) -> GateDecision:
        """Check if a file read is allowed through the cascade."""
        normalized = _normalize_path(path)

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

    @staticmethod
    def check_lane_tool(ctx: GateContext, tool_name: str) -> GateDecision:
        """Check if a tool is allowed in the current lane scope.

        When a conductor lane is active, only tools matching the lane's
        allowed tool patterns are permitted. Configured via:
            conductor.lane_allowed_tools = ["code_*", "session_*", ...]
            conductor.lane_extra_tools = ["mcp__custom__tool", ...]
        """
        current_lane = ctx.gate_state.get("current_lane_id")
        if not current_lane:
            return GateDecision(allowed=True, level="no_lane")

        # Normalize tool name
        name = tool_name.strip().lower()
        for prefix in ("mcp__aidocs__",):
            if name.startswith(prefix):
                name = name[len(prefix):]

        # System tools always allowed in lanes
        _LANE_SYSTEM_TOOLS = {
            "task_begin", "task_update", "task_complete",
            "plan_dispatch_report", "plan_conductor_record_lane_signal",
            "plan_conductor_expand_scope",
            "session_journal_log", "memory_read", "memory_search",
            "code_get_lines", "code_get_symbol_snippet",
            "metrics_snapshot",
        }
        if name in _LANE_SYSTEM_TOOLS:
            return GateDecision(allowed=True, level="lane_system_tool")

        # Load allowed patterns from config
        allowed_patterns = ctx.gate_state.get("lane_allowed_tools") or []
        extra_patterns = ctx.gate_state.get("lane_extra_tools") or []
        all_patterns = list(allowed_patterns) + list(extra_patterns)

        if not all_patterns:
            # No patterns configured — use default (all AIDOCS tools, no bash)
            all_patterns = [
                "code_*", "session_*", "memory_*", "schema_*", "index_*",
                "plan_*", "execution_*", "task_*", "verification_*",
                "code_build_project", "code_test_project", "code_run_command",
                "skill_*", "context_*", "edit_history_*",
            ]

        import fnmatch
        for pattern in all_patterns:
            if fnmatch.fnmatch(name, pattern.lower()):
                return GateDecision(allowed=True, level="lane_tool_allowed")

        # Also check full tool name (for mcp__custom__tool patterns)
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
        normalized = _normalize_path(path)

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
            # Non-protected memory paths — allow freely
            return GateDecision(allowed=True, level="memory_path_exemption")
            # Session files, journals, domains, etc. — freely writable
            return GateDecision(allowed=True, level="memory_path_exemption")

        return GateDecision(allowed=True, level="allowed")

    # ── Discovery grants ──

    @staticmethod
    def grant_discovery(
        store: "QueryGateStore",
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
            str(item) for item in state.get("known_exact_paths", [])
            if isinstance(item, str)
        ]
        merged = list(dict.fromkeys(existing + safe_paths))

        store.set(
            project_root,
            session_id,
            last_tool=f"discovery:{tool_name}",
            known_exact_paths=merged,
        )
