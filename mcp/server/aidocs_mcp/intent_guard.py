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

import os
import re
from dataclasses import dataclass
from pathlib import Path


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
    "read",
    "glob",
    "grep",
    # MCP read tools (all code_get_*, code_find, code_trace, etc.)
    "code_get_lines",
    "code_get_outline",
    "code_get_symbol_snippet",
    "code_get_method_signature",
    "code_get_method_signatures",
    "code_get_constructor_params",
    "code_get_constructor_params_batch",
    "code_get_enum_values",
    "code_get_entity_properties",
    "code_get_service_api",
    "code_get_dependencies",
    "code_get_modules",
    "code_get_module_files",
    "code_find",
    "code_trace",
    "code_search",
    "code_investigate",
    "code_bundle",
    "code_index_sync",
    "code_index_status",
    "schema_query",
    "schema_index_sync",
    "index_sync",
    "index_status",
    # Memory read
    "memory_read",
    "memory_search",
    # Session read
    "session_list",
    "session_read",
    "session_select",
    "session_journal_read",
    "session_resume_bundle",
    # Status/diagnostic
    "project_status",
    "project_check",
    "git_diag",
    "index_language_descriptors_get",
    "index_language_descriptors_validate",
    "capability_definitions_get",
    "capability_index_status",
    # Orchestration
    "aidocs_classify_prompt",
    "aidocs_route_prompt",
    "aidocs_handle_prompt",
    "aidocs_orchestrate",
    "aidocs_mode_get",
    "action_surface_assess",
    "action_surface_compare",
    "action_surface_status_bundle",
    "action_surface_session_bundle",
    "action_surface_current_session_bundle",
    "runtime_preflight",
    "execution_events_get",
    "execution_runs_get",
    "execution_query_last",
    "execution_query_summary",
    "execution_query_compliance",
    # Shell execution is allowed by default; protected-target and destructive
    # safeguards are enforced elsewhere in host/runtime policy.
    "bash",
}

# CONFIRMATION: Always requires explicit user confirmation
CONFIRMATION_TOOLS: set[str] = {
    # These bypass normal agent guardrails and need explicit user request
}

# Keywords in user prompt that justify specific tool categories
# No hardcoded tokens — all loaded from action_tokens/*.toml at runtime.
# Categories that must exist in TOML for intent guard to work:
#   edit, __intent_guard_write, __intent_guard_bash, __intent_guard_destructive,
#   write_memory, git_commit, git_push, git_pull
INTENT_KEYWORDS: dict[str, set[str]] = {}

_INTENT_GUARD_TOKEN_KEYS: dict[str, str] = {
    "write": "__intent_guard_write",
    "bash": "__intent_guard_bash",
    "destructive": "__intent_guard_destructive",
}

_CANONICAL_INTENT_TOKEN_KEYS: dict[str, str] = {
    "edit": "edit",
    "memory_capture": "write_memory",
    "git_commit": "git_commit",
    "git_push": "git_push",
    "git_pull": "git_pull",
}

_CANONICAL_INTENT_TOKEN_KEYS: dict[str, str] = {
    "edit": "edit",
    "memory_capture": "write_memory",
    "git_commit": "git_commit",
    "git_push": "git_push",
    "git_pull": "git_pull",
}

# Map tool names to their intent keyword category
TOOL_INTENT_MAP: dict[str, str] = {
    "edit": "edit",
    "write": "write",
    "code_edit_lines": "edit",
    "code_batch_edit": "edit",
    "code_str_replace": "edit",
    "code_batch_str_replace": "edit",
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
            name = name[len(prefix) :]
    return name


def _resolve_action_tokens_dir() -> Path:
    candidates = []
    env_path = os.environ.get("AIDOCS_PATH")
    if env_path:
        candidates.append(Path(env_path) / "action_tokens")
    candidates.extend(
        [
            Path(__file__).resolve().parents[3] / "action_tokens",
            Path(__file__).resolve().parents[2] / "action_tokens",
            Path.cwd() / "action_tokens",
        ]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


_action_token_cache: dict[str, list[str]] | None = None


def _load_action_token_lists(directory: Path | None = None) -> dict[str, list[str]]:
    global _action_token_cache
    if _action_token_cache is not None and directory is None:
        return _action_token_cache
    root = directory or _resolve_action_tokens_dir()
    if not root.is_dir():
        return {}
    merged: dict[str, list[str]] = {}

    # TOML files take precedence — track which stems have been loaded
    loaded_stems: set[str] = set()

    for toml_file in sorted(root.glob("*.toml")):
        loaded_stems.add(toml_file.stem)
        try:
            _merge_toml_tokens(toml_file, merged)
        except Exception:
            continue

    for yaml_file in sorted(root.glob("*.yaml")):
        if yaml_file.stem in loaded_stems:
            continue
        current_key: str | None = None
        try:
            for raw_line in yaml_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                key_match = re.match(r"^(\w[\w_]*):\s*$", line)
                if key_match:
                    current_key = key_match.group(1)
                    continue
                item_match = re.match(r"^\s+-\s+(.+)$", line)
                if item_match and current_key:
                    token = item_match.group(1).strip()
                    if token:
                        merged.setdefault(current_key, []).append(token)
        except Exception:
            continue
    if directory is None:
        _action_token_cache = merged
    return merged


def _merge_toml_tokens(path: Path, merged: dict[str, list[str]]) -> None:
    """Load a TOML action token file and merge into the flat token dict."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return

    data = tomllib.loads(path.read_text(encoding="utf-8"))

    for key, value in data.items():
        if key == "memory_routing":
            # Nested: memory_routing.communication = {target, tokens}
            if isinstance(value, dict):
                for route_key, route_val in value.items():
                    if isinstance(route_val, dict):
                        tokens = route_val.get("tokens")
                        if isinstance(tokens, list):
                            merged_key = f"__memory_routing_{route_key}"
                            merged.setdefault(merged_key, []).extend(
                                str(t) for t in tokens if str(t).strip()
                            )
            continue

        if key.startswith("__skill_trigger_") and isinstance(value, dict):
            # Nested: __skill_trigger_X = {intent = [...], workflow = [...]}
            for sub_key, sub_val in value.items():
                if isinstance(sub_val, list):
                    merged_key = f"{key}_{sub_key}"
                    merged.setdefault(merged_key, []).extend(
                        str(t) for t in sub_val if str(t).strip()
                    )
            continue

        if isinstance(value, list):
            merged.setdefault(key, []).extend(
                str(t) for t in value if str(t).strip()
            )


def load_memory_routing_config(
    directory: Path | None = None,
) -> list[dict[str, object]]:
    """Load memory routing rules from TOML action token files.

    Returns list of {target: str, tokens: list[str]} dicts for each routing category.
    """
    root = directory or _resolve_action_tokens_dir()
    if not root.is_dir():
        return []

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return []

    routes: list[dict[str, object]] = []
    for toml_file in sorted(root.glob("*.toml")):
        try:
            data = tomllib.loads(toml_file.read_text(encoding="utf-8"))
            routing = data.get("memory_routing")
            if not isinstance(routing, dict):
                continue
            for _key, entry in routing.items():
                if isinstance(entry, dict):
                    target = entry.get("target")
                    tokens = entry.get("tokens")
                    if isinstance(target, str) and isinstance(tokens, list):
                        routes.append({
                            "target": target,
                            "tokens": [str(t).lower() for t in tokens if str(t).strip()],
                        })
        except Exception:
            continue
    return routes



def _intent_keywords() -> dict[str, set[str]]:
    token_lists = _load_action_token_lists()
    resolved = {key: set(value) for key, value in INTENT_KEYWORDS.items()}
    for category, token_key in _CANONICAL_INTENT_TOKEN_KEYS.items():
        values = token_lists.get(token_key, [])
        if values:
            resolved[category] = {
                str(item).strip().lower() for item in values if str(item).strip()
            }
    for category, token_key in _INTENT_GUARD_TOKEN_KEYS.items():
        values = token_lists.get(token_key, [])
        if values:
            resolved[category] = {
                str(item).strip().lower() for item in values if str(item).strip()
            }
    return resolved


def _extract_prompt_keywords(prompt: str) -> set[str]:
    """Extract action-relevant keywords from user prompt (lowercase)."""
    if not prompt:
        return set()
    lower = prompt.lower()
    words = set(re.findall(r"[a-z]{2,}", lower))
    # Also check for multi-word phrases
    phrases = set()
    for phrase_set in _intent_keywords().values():
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

    required_keywords = _intent_keywords().get(intent_category, set())
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
                findings.append(
                    {
                        "line": line_number,
                        "matched": match.group(0),
                        "pattern": pattern.pattern,
                        "line_content": line.strip()[:200],
                        "severity": "high"
                        if any(
                            kw in pattern.pattern.lower()
                            for kw in ("curl", "eval", "exec", "rm", "powershell")
                        )
                        else "medium",
                    }
                )
    return findings
