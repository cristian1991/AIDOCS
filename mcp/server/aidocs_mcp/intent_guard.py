"""Intent guard — verifies agent actions trace back to user intent.

Every tool call is classified into one of three categories:
    FREE            — agent can do without asking (read, search, trace, index)
    INTENT_REQUIRED — agent must be able to cite user prompt keywords (edit, create, delete)
    CONFIRMATION    — agent must surface a choice to the user (push, pull, destructive ops)

For INTENT_REQUIRED operations, the guard checks if the user's prompt contains
keywords that justify the operation. If not, the action is blocked with a
message telling the agent to ask the user first.

For CONFIRMATION operations, the action is always blocked — the agent must
explicitly ask the user and get approval.

This module is stateless and deterministic — no AI inference, just keyword matching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class GuardResult:
    """Result of an intent check."""
    allowed: bool
    category: str  # "free", "intent_required", "confirmation"
    reason: str | None = None
    matched_keywords: list[str] | None = None


# ── Tool classification ──

# FREE: Agent can always use these without justification
FREE_TOOLS: set[str] = {
    # Read/search operations
    "read", "glob", "grep",
    # MCP read tools (all code_get_*, code_find, code_trace, etc.)
    "code_get_lines", "code_get_outline", "code_get_symbol_snippet",
    "code_get_method_signature", "code_get_method_signatures",
    "code_get_constructor_params", "code_get_constructor_params_batch",
    "code_get_enum_values", "code_get_entity_properties",
    "code_get_service_api", "code_get_dependencies", "code_get_modules",
    "code_get_module_files",
    "code_find", "code_trace", "code_search", "code_investigate",
    "code_bundle", "code_index_sync", "code_index_status",
    "schema_query", "schema_index_sync",
    "index_sync", "index_status",
    # Memory read
    "memory_read", "memory_search",
    # Session read
    "session_list", "session_read", "session_select",
    "session_journal_read", "session_resume_bundle",
    # Status/diagnostic
    "project_status", "project_check", "git_diag",
    "index_language_descriptors_get", "index_language_descriptors_validate",
    "capability_definitions_get", "capability_index_status",
    # Orchestration
    "aidocs_classify_prompt", "aidocs_route_prompt", "aidocs_handle_prompt",
    "aidocs_orchestrate", "aidocs_mode_get",
    "action_surface_assess", "action_surface_compare",
    "action_surface_status_bundle", "action_surface_session_bundle",
    "action_surface_current_session_bundle",
    "runtime_preflight",
    "execution_events_get", "execution_runs_get",
    "execution_query_last", "execution_query_summary", "execution_query_compliance",
}

# CONFIRMATION: Always requires explicit user confirmation
CONFIRMATION_TOOLS: set[str] = {
    # These bypass normal agent guardrails and need explicit user request
}

# Keywords in user prompt that justify specific tool categories
INTENT_KEYWORDS: dict[str, set[str]] = {
    # File editing
    "edit": {"edit", "fix", "change", "update", "modify", "replace", "rename",
             "refactor", "clean", "improve", "add", "remove", "delete", "move",
             "create", "build", "implement", "write", "set", "configure", "adjust",
             "patch", "correct", "convert", "migrate", "restructure", "redesign",
             "rewrite", "rework", "optimize", "simplify"},

    # File creation
    "write": {"create", "add", "generate", "scaffold", "new", "init", "setup",
              "build", "implement", "write", "make", "fix", "update"},

    # Git operations
    "git_commit": {"commit", "save changes", "check in"},
    "git_push": {"push", "deploy", "ship", "publish", "release"},
    "git_pull": {"pull", "fetch", "sync", "update from"},

    # Shell execution
    "bash": {"run", "execute", "test", "build", "compile", "install", "start",
             "stop", "restart", "deploy", "lint", "format", "check",
             "npm", "dotnet", "pip", "cargo", "make", "docker"},

    # Memory writes
    "memory_capture": {"remember", "save", "note", "record", "log", "capture",
                       "store", "persist", "memorize", "keep"},

    # Destructive operations (always need explicit keywords)
    "destructive": {"force push", "reset hard", "delete branch", "drop table",
                    "rm -rf", "clean", "purge", "wipe", "destroy", "nuke"},
}

# Map tool names to their intent keyword category
TOOL_INTENT_MAP: dict[str, str] = {
    "edit": "edit",
    "write": "write",
    "code_edit_lines": "edit",
    "code_batch_edit": "edit",
    "bash": "bash",
    "memory_capture": "memory_capture",
    "session_journal_log": "memory_capture",
    "task_begin": "edit",
    "task_complete": "edit",
    "task_update": "edit",
    "session_update": "memory_capture",
    "session_create": "memory_capture",
    "session_start": "memory_capture",
    "project_init": "write",
    "project_fix": "edit",
}

# Prompt injection patterns — these should never appear as "instructions" in code content
INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:curl|wget|fetch)\s+https?://", re.IGNORECASE),
    re.compile(r"(?:eval|exec|system|popen)\s*\(", re.IGNORECASE),
    re.compile(r"base64\s+(?:-d|--decode)", re.IGNORECASE),
    re.compile(r"\bpasswd\b.*\bshadow\b", re.IGNORECASE),
    re.compile(r"(?:rm\s+-rf|del\s+/[sfq])\s+[/\\]", re.IGNORECASE),
    re.compile(r"powershell\s+-(?:enc|e|command)", re.IGNORECASE),
    re.compile(r"cmd\s+/c\s+", re.IGNORECASE),
    re.compile(r"IMPORTANT:\s*(?:run|execute|call|download|install)", re.IGNORECASE),
    re.compile(r"TODO:\s*(?:run|execute|curl|wget|pip install)", re.IGNORECASE),
    re.compile(r"CRITICAL:\s*(?:Before|First|Must)\s+(?:run|execute)", re.IGNORECASE),
]


def _normalize_tool_name(tool_name: str) -> str:
    """Normalize tool name — strip mcp__aidocs__ prefix if present."""
    name = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__playwright__"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


def _extract_prompt_keywords(prompt: str) -> set[str]:
    """Extract action-relevant keywords from user prompt (lowercase)."""
    if not prompt:
        return set()
    lower = prompt.lower()
    words = set(re.findall(r"[a-z]{2,}", lower))
    # Also check for multi-word phrases
    phrases = set()
    for phrase_set in INTENT_KEYWORDS.values():
        for phrase in phrase_set:
            if " " in phrase and phrase in lower:
                phrases.add(phrase)
    return words | phrases


def check_intent(
    tool_name: str,
    user_prompt: str,
    tool_input: dict[str, object] | None = None,
) -> GuardResult:
    """Check if a tool call is justified by the user's prompt.

    Args:
        tool_name: The tool being called (may include mcp__aidocs__ prefix).
        user_prompt: The original user prompt for this turn.
        tool_input: The tool's input parameters (for context-aware checks).

    Returns:
        GuardResult with allowed=True/False and reason.
    """
    normalized = _normalize_tool_name(tool_name)

    # FREE tools — always allowed
    if normalized in FREE_TOOLS:
        return GuardResult(allowed=True, category="free")

    # CONFIRMATION tools — always blocked, agent must ask user
    if normalized in CONFIRMATION_TOOLS:
        return GuardResult(
            allowed=False,
            category="confirmation",
            reason=f"Tool `{normalized}` requires explicit user confirmation. Ask the user before proceeding.",
        )

    # INTENT_REQUIRED tools — check if user prompt justifies it
    intent_category = TOOL_INTENT_MAP.get(normalized)
    if intent_category is None:
        # Unknown tool — allow by default (fail open for unknown tools)
        return GuardResult(allowed=True, category="free")

    required_keywords = INTENT_KEYWORDS.get(intent_category, set())
    if not required_keywords:
        return GuardResult(allowed=True, category="free")

    prompt_keywords = _extract_prompt_keywords(user_prompt)
    matched = prompt_keywords & required_keywords

    if matched:
        return GuardResult(
            allowed=True,
            category="intent_required",
            matched_keywords=sorted(matched),
        )

    # Special case: destructive operations need EXACT phrase matches
    if intent_category == "destructive":
        return GuardResult(
            allowed=False,
            category="confirmation",
            reason=f"Destructive operation `{normalized}` not found in user prompt. "
                   f"The user must explicitly request this action.",
        )

    # Intent not found — block with helpful message
    example_keywords = sorted(required_keywords)[:5]
    return GuardResult(
        allowed=False,
        category="intent_required",
        reason=f"Tool `{normalized}` requires user intent. "
               f"The user prompt does not contain keywords like: {', '.join(example_keywords)}. "
               f"Ask the user if they want to {intent_category}.",
    )


def scan_for_injection(content: str) -> list[dict[str, str]]:
    """Scan content for potential prompt injection patterns.

    Call this on content the agent reads from external sources (files, web)
    BEFORE the agent acts on instructions found in that content.

    Returns a list of detected patterns with their location and description.
    """
    if not content:
        return []

    findings: list[dict[str, str]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        for pattern in INJECTION_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append({
                    "line": line_number,
                    "matched": match.group(0),
                    "pattern": pattern.pattern,
                    "line_content": line.strip()[:200],
                    "severity": "high" if any(kw in pattern.pattern.lower() for kw in ("curl", "eval", "exec", "rm", "powershell")) else "medium",
                })
    return findings
