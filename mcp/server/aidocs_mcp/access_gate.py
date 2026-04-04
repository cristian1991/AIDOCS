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
    dev_mode: bool
    gate_state: dict[str, Any]


@dataclass(slots=True)
class GateDecision:
    allowed: bool
    level: str
    reason: str | None = None


# ── Constants ──

_BLOCKED_RAW_FILE_TOOLS: set[str] = {"read", "grep", "glob", "edit", "write"}

_RAW_TOOL_REPLACEMENTS: dict[str, str] = {
    "read": "Use `code_get_lines`, `code_get_symbol_snippet`, or `code_bundle` instead.",
    "grep": "Use `code_find`, `code_search`, `code_trace`, or `schema_query` instead.",
    "glob": "Use `code_search` or `code_find(mode=\"partial_group\")` instead.",
    "edit": "Use `code_edit_lines` or `code_batch_edit` instead.",
    "write": "Use `code_create_file` instead.",
}

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
    ".memory/config/workflow-actions",
)


def _is_protected_memory_path(path: str) -> bool:
    """Workflow rules, security rules, and compiled workflows are user-intent-only."""
    normalized = _normalize_path(path).lower()
    return any(normalized.startswith(prefix) for prefix in _PROTECTED_MEMORY_PREFIXES)



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


class AccessGate:
    """Unified access gate — all callers delegate here."""

    # ── Level 1: Managed Mode Gate ──

    @staticmethod
    def check_raw_tool(
        ctx: GateContext,
        tool_name: str,
        *,
        allow_subagents: bool = True,
    ) -> GateDecision:
        """Block raw file tools when managed mode is active."""
        normalized_tool = tool_name.strip().lower()

        # Agent blocking is independent of managed mode file gating
        if normalized_tool == "agent" and not allow_subagents:
            return GateDecision(
                allowed=False,
                level="managed_mode_gate",
                reason=(
                    "Agent delegation is disabled. Use AIDOCS indexed tools directly "
                    "— code_investigate, code_find, code_trace can answer most "
                    "questions in 5-10 calls."
                ),
            )

        if not ctx.managed:
            return GateDecision(allowed=True, level="managed_mode_gate")

        if normalized_tool in _BLOCKED_RAW_FILE_TOOLS:
            replacement = _RAW_TOOL_REPLACEMENTS.get(normalized_tool, "")
            return GateDecision(
                allowed=False,
                level="managed_mode_gate",
                reason=f"AIDOCS managed mode is active — raw `{normalized_tool}` is blocked. {replacement}",
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
                reason=f"Access to sensitive file blocked: {normalized}",
            )

        # Level 2: Protected config reads — block always
        if _is_protected_config(normalized):
            return GateDecision(
                allowed=False,
                level="infrastructure_protection",
                reason=f"Read access to AIDOCS config file blocked: {normalized}",
            )

        # Level 4: .MEMORY/ reads — always allowed
        if _is_memory_path(normalized):
            return GateDecision(allowed=True, level="memory_path_exemption")

        # Unmanaged mode — no read gate
        if not ctx.managed:
            return GateDecision(allowed=True, level="unmanaged")

        # Level 5: Read gate — per-file discovery
        if known_exact_path:
            return GateDecision(allowed=True, level="read_gate")

        if _is_path_granted(ctx.gate_state, normalized):
            return GateDecision(allowed=True, level="read_gate")

        return GateDecision(
            allowed=False,
            level="read_gate",
            reason=(
                f"Indexed-read gate: \"{normalized}\" has not been discovered. "
                "Use code_investigate, code_find, code_trace, or code_bundle first, "
                "or pass known_exact_path=true if you know the exact path."
            ),
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

        # Level 6: Edit gate — file must be previously discovered
        if _is_path_granted(ctx.gate_state, normalized):
            return GateDecision(allowed=True, level="edit_gate")

        return GateDecision(
            allowed=False,
            level="edit_gate",
            reason=(
                f"Edit gate: \"{normalized}\" has not been read/discovered. "
                "Read the file with code_get_lines first."
            ),
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
                reason=f"Write access to sensitive file blocked: {normalized}",
            )

        # Level 2: Infrastructure protection — config files always blocked
        if _is_protected_config(normalized):
            return GateDecision(
                allowed=False,
                level="infrastructure_protection",
                reason=f"Write access to AIDOCS config file blocked: {normalized}",
            )

        # Level 2: Infrastructure paths — blocked unless dev_mode
        if _is_infrastructure(normalized) and not ctx.dev_mode:
            return GateDecision(
                allowed=False,
                level="infrastructure_protection",
                reason=f"Write access to AIDOCS infrastructure blocked (dev_mode required): {normalized}",
            )

        # Level 4: .MEMORY/ writes — workflow/security rules need user intent
        if _is_memory_path(normalized):
            if ctx.dev_mode:
                return GateDecision(allowed=True, level="memory_path_exemption")
            if _is_protected_memory_path(normalized):
                if has_intent:
                    return GateDecision(allowed=True, level="memory_write_intent_gate")
                return GateDecision(
                    allowed=False,
                    level="memory_write_intent_gate",
                    reason=(
                        f"Write to workflow/security rules requires user intent. "
                        f"These files affect how AIDOCS orchestrates code execution: {normalized}"
                    ),
                )
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
            allow_read=False,
            last_tool=f"discovery:{tool_name}",
            known_exact_paths=merged,
        )
