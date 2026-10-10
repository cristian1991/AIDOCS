"""Rule-based action_kind router — fallback when the LLM classifier is
uncertain or unavailable.

The canonical action_kind classifier is `classify_prompt` (runtime_service)
backed by the LLM. This router is the insurance policy: a fast, offline,
regex-driven mapping from common prompt shapes to canonical action_kinds.
It never replaces the LLM path; it just answers when the LLM path can't.

Layer 5 NLP deliverable (2026-04-19). Paired with grant-detection +
plan-mode detection; each owns its phrases without cross-contamination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Rule:
    """One router rule. `pattern` is a compiled regex; `action_kind` is
    the canonical label to emit on match. Keep labels in lockstep with
    classify_prompt's known action_kinds so downstream code doesn't
    care which path produced the value.
    """

    pattern: re.Pattern[str]
    action_kind: str


# Rule order matters: earlier rules win when multiple match. The
# ordering reflects "specific first, general last" — git verbs beat
# generic "commit", tests beat generic "run", archive beats generic
# "done", etc. Each rule is case-insensitive against lowercased prompt.
_RULES: tuple[_Rule, ...] = (
    # git_commit — explicit commit phrasing
    _Rule(
        re.compile(r"\b(?:git\s+)?commit\b|\bstage\s+(?:and\s+)?commit\b|\bcreate\s+a?\s*commit\b"),
        "git_commit",
    ),
    # test — "run tests", "pytest", "npm test", "test this", explicit
    _Rule(
        re.compile(r"\b(?:run\s+)?(?:the\s+)?tests?\b|\bpytest\b|\bnpm\s+test\b|\btest\s+suite\b"),
        "test",
    ),
    # archive — "archive the session", "close session", "wrap up"
    _Rule(
        re.compile(r"\barchive\b|\bclose\s+(?:the\s+)?session\b|\bwrap\s+up\b"),
        "archive",
    ),
    # edit — "edit X", "modify X", "refactor X", "change X"
    _Rule(
        re.compile(
            r"\bedit\s+\S|\bmodify\s+\S|\brefactor\b|\bchange\s+(?:the\s+)?(?:code|file|function)\b",
        ),
        "edit",
    ),
    # understand — "what does X do", "explain", "why", "how does"
    # Kept broad because it's the default fallback tier.
    _Rule(
        re.compile(r"\bwhat\s+does\b|\bexplain\b|\bwhy\b|\bhow\s+does\b|\bwhat's\b|\bwhat\s+is\b"),
        "understand",
    ),
)


DEFAULT_ACTION_KIND = "understand"


def classify_fallback(prompt: str) -> str:
    """Return the first matching action_kind, or DEFAULT_ACTION_KIND.

    Intended for the path where the LLM classifier timed out or returned
    empty. Never raises — treats every input as text.
    """
    if not prompt:
        return DEFAULT_ACTION_KIND
    text = str(prompt).lower().strip()
    if not text:
        return DEFAULT_ACTION_KIND
    for rule in _RULES:
        if rule.pattern.search(text):
            return rule.action_kind
    return DEFAULT_ACTION_KIND


def known_action_kinds() -> tuple[str, ...]:
    """Expose the action_kinds this router can emit. Useful for tests
    and for dashboard filters that want the canonical label set.
    """
    labels = {rule.action_kind for rule in _RULES}
    labels.add(DEFAULT_ACTION_KIND)
    return tuple(sorted(labels))
