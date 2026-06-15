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
    # MCP read tools (all ai_get_*, ai_find, ai_trace, etc.)
    "ai_get_lines",
    "ai_get_outline",
    "ai_get_symbol_snippet",
    "ai_get_symbol_info",
    "ai_get_dependencies",
    "ai_get_modules",
    "ai_get_module_files",
    "ai_find",
    "ai_trace",
    "ai_search",
    "ai_text_search",
    "ai_slop",
    "planning_docs_list",
    "ai_investigate",
    "ai_bundle",
    "ai_index_sync",
    "ai_index_status",
    "schema_query",
    "schema_index_sync",
    "index_sync",
    "index_status",
    # Memory read
    "memory_read",
    "memory_search",
    # Session read
    "session_list",
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
    "classify_prompt",
    "route_prompt",
    "handle_prompt",
    "orchestrate",
    "mode_get",
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
# No hardcoded tokens — all loaded from intent_tokens/*.toml at runtime.
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

# Map tool names to their intent keyword category
TOOL_INTENT_MAP: dict[str, str] = {
    "edit": "edit",
    "write": "write",
    "ai_edit_lines": "edit",
    "ai_replace": "edit",
    "ai_batch_edit": "edit",
    "ai_insert_lines": "edit",
    "ai_slop": "edit",
    "planning_step_mark": "edit",
    "memory_capture": "memory_capture",
    "ai_task": "edit",
    "ai_session": "memory_capture",
    # session_start MCP tool removed 2026-04-30; entry was a no-op
    # for current callers. project_bootstrap_or_resume is the new
    # first-prompt entry point.
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
        name = name.removeprefix(prefix)
    return name


def _resolve_intent_tokens_dir() -> Path:
    """Deprecated shim — intent vocabulary lives in the empire
    intent-tokens store (sqlite) per king-directive 2026-05-13.
    Returns the package-bundled seed dir
    (mcp/server/aidocs_mcp/seed/intent_tokens) only so a few
    file-path-shaped callers keep working. The TOMLs at that path are
    no longer read by any active consumer post-Phase 5b.
    """
    return Path(__file__).resolve().parent / "seed" / "intent_tokens"


def _invalidate_intent_token_cache() -> None:
    """No-op shim — intent tokens now read directly from sqlite (no in-process cache)."""
    return


def classify_action(text: str) -> dict[str, str]:
    """Classify a user prompt into an action kind. Used by OpenCode plugin via subprocess.

    Returns: {"action_kind": "edit", "why": "matched:edit"}

    Phase 6d uplift (2026-05-14): context-aware filtering. The previous
    naive substring scan picked up incidental tokens (e.g. "even before
    you sync" classified as git_pull because of "sync"). Now uses
    spaCy parse + the same noun-context and first-person-agent
    suppressors detect_grant uses. Order of iteration preserved
    (insertion order from _load_intent_token_lists).
    """
    lower = text.strip().lower()
    if re.match(r"^(investigate|debug|diagnose|dig into)\b", lower):
        return {"action_kind": "investigate", "why": "prefix:investigate"}
    if re.match(r"^(inspect|examine|audit)\b", lower):
        return {"action_kind": "inspect", "why": "prefix:inspect"}

    tokens = _load_intent_token_lists()

    # Lazy spaCy parse — only paid when at least one action_kind matches.
    _doc_state: list = [False, None]

    def _parse():
        if _doc_state[0]:
            return _doc_state[1]
        try:
            from pathlib import Path

            from .aidocs_nlp.service import get_service

            _doc_state[1] = get_service(Path.cwd(), {}).analyze(text)
        except Exception:
            _doc_state[1] = None
        _doc_state[0] = True
        return _doc_state[1]

    # English first/second person sets are good enough here — the
    # action_token universe used by classify_action is overwhelmingly
    # English verb tokens. Non-English prompts that reach this function
    # still benefit from the noun-context DET check (which is spaCy
    # Universal POS, language-agnostic when a per-lang model is loaded).
    try:
        from .intent_grant_detector import (
            _EN_FALLBACK,
            _alias_first_person_agent,
            _alias_in_noun_context,
        )

        _first_person = _EN_FALLBACK.first_person
        _second_person = _EN_FALLBACK.second_person
    except Exception:
        _alias_in_noun_context = None
        _alias_first_person_agent = None
        _first_person = frozenset()
        _second_person = frozenset()

    for action_kind, token_list in tokens.items():
        if action_kind.startswith("__"):
            continue
        if not isinstance(token_list, list):
            continue
        for token in token_list:
            if not isinstance(token, str) or not token:
                continue
            if token not in lower:
                continue
            if _alias_in_noun_context is not None:
                doc = _parse()
                if doc is not None:
                    # For multi-word tokens, check context using the
                    # first word of the phrase (the head verb of an
                    # imperative). Single-word: check the token itself.
                    head_word = token.split(" ", 1)[0] if " " in token else token
                    if " " not in token and _alias_in_noun_context(doc, head_word):
                        continue
                    if _alias_first_person_agent(
                        doc,
                        head_word,
                        _first_person,
                        _second_person,
                    ):
                        continue
            return {"action_kind": action_kind, "why": f"matched:{action_kind}:via:{token}"}
    return {"action_kind": "understand", "why": "default:understand"}


_DEFAULT_TOKEN_LANGS: tuple[str, ...] = ("en", "de", "es", "it", "pt", "ja")


def _load_intent_token_lists(
    directory: Path | None = None,
    enabled_languages: str = "all",
) -> dict[str, list[str] | dict]:
    """Load intent token lists from the intent_tokens_store sqlite table.

    The `directory` parameter is retained for API compatibility but ignored —
    tokens now live in the empire db, not TOML files.
    """
    from . import intent_tokens_store as _store

    enabled = str(enabled_languages or "all").lower().strip()
    if enabled == "all" or not enabled:
        langs: tuple[str, ...] = _DEFAULT_TOKEN_LANGS
    else:
        langs = tuple(part.strip() for part in enabled.split(",") if part.strip())

    merged: dict[str, list[str] | dict] = {}

    def _append_unique(key: str, token: str) -> None:
        if not token:
            return
        existing = merged.get(key)
        if existing is None:
            merged[key] = [token]
            return
        if isinstance(existing, list) and token not in existing:
            existing.append(token)

    for lang in langs:
        for row in _store.get_rows_by_kind(lang, "action_token"):
            _append_unique(str(row.get("parent_key") or ""), str(row.get("token") or ""))

        for row in _store.get_rows_by_kind(lang, "intent_guard"):
            parent = str(row.get("parent_key") or "")
            if not parent:
                continue
            _append_unique(f"__intent_guard_{parent}", str(row.get("token") or ""))

        for row in _store.get_rows_by_kind(lang, "plan_vague_pattern"):
            _append_unique("__plan_validation_vague_patterns", str(row.get("token") or ""))

        # skill_trigger rows: parent_key=skill_name, parent_mode in
        # {intent, workflow}. Emit as __skill_trigger_<name_underscored>_<mode>
        # so runtime_service._configured_skill_trigger_rule can read them.
        # Loader-only consumers (aidocs_nlp.consumers.skill_trigger) still
        # read directly from get_rows_by_kind via load_skill_trigger_tokens;
        # this exposure is for the runtime token-list path.
        for row in _store.get_rows_by_kind(lang, "skill_trigger"):
            parent = str(row.get("parent_key") or "")
            mode = str(row.get("parent_mode") or "")
            if not parent or mode not in ("intent", "workflow"):
                continue
            token_key = parent.replace("-", "_")
            _append_unique(
                f"__skill_trigger_{token_key}_{mode}",
                str(row.get("token") or ""),
            )

        for row in _store.get_rows_by_kind(lang, "intent_phrase", include_attrs=True):
            parent = str(row.get("parent_key") or "")
            if not parent:
                continue
            key = f"__intent_phrases.{parent}"
            attrs = row.get("attrs") or {}
            token = str(row.get("token") or "")
            bucket = merged.get(key)
            if not isinstance(bucket, dict):
                bucket = {}
                for attr_key, attr_val in attrs.items():
                    if attr_key != "phrases":
                        bucket[attr_key] = attr_val
                bucket["phrases"] = []
                merged[key] = bucket
            else:
                for attr_key, attr_val in attrs.items():
                    if attr_key != "phrases" and attr_key not in bucket:
                        bucket[attr_key] = attr_val
            phrases = bucket.setdefault("phrases", [])
            if isinstance(phrases, list) and token and token not in phrases:
                phrases.append(token)

    return merged


# _merge_toml_tokens removed 2026-05-14 — TOML reader had zero callers
# after Phase 3 (workflow_action_service was the last; it now delegates
# to _load_intent_token_lists which reads sqlite). Migration notes for
# the section shapes _merge_toml_tokens used to parse live in
# `_migrate_tomls.py::migrate_intent_tokens_file`.


def load_memory_routing_config(
    directory: Path | None = None,
) -> list[dict[str, object]]:
    """Load memory routing rules from intent_tokens_store sqlite.

    Returns list of {target: str, tokens: list[str]} dicts. Rows whose
    `attrs['target']` matches are merged across languages; tokens are
    lowercased and deduped per target.
    """
    from . import intent_tokens_store as _store

    by_target: dict[str, list[str]] = {}
    order: list[str] = []
    for lang in _DEFAULT_TOKEN_LANGS:
        for row in _store.get_rows_by_kind(lang, "memory_route", include_attrs=True):
            attrs = row.get("attrs") or {}
            target = attrs.get("target")
            token = row.get("token")
            if not isinstance(target, str) or not token:
                continue
            tok_lower = str(token).strip().lower()
            if not tok_lower:
                continue
            bucket = by_target.get(target)
            if bucket is None:
                bucket = []
                by_target[target] = bucket
                order.append(target)
            if tok_lower not in bucket:
                bucket.append(tok_lower)
    return [{"target": t, "tokens": by_target[t]} for t in order]


def _intent_keywords() -> dict[str, set[str]]:
    token_lists = _load_intent_token_lists()
    resolved = {key: set(value) for key, value in INTENT_KEYWORDS.items()}
    for category, token_key in _CANONICAL_INTENT_TOKEN_KEYS.items():
        values = token_lists.get(token_key, [])
        if values:
            resolved[category] = {str(item).strip().lower() for item in values if str(item).strip()}
    for category, token_key in _INTENT_GUARD_TOKEN_KEYS.items():
        values = token_lists.get(token_key, [])
        if values:
            resolved[category] = {str(item).strip().lower() for item in values if str(item).strip()}
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

    Delegates to output_guard for injection detection + adds code-content
    specific patterns (eval, exec, curl, rm -rf, powershell).
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
                    },
                )
    return findings


# Eager load at module import — no first-call penalty
_load_intent_token_lists()
