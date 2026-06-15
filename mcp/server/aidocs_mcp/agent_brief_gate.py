"""Agent dispatch brief gate.

Pure evaluator that decides whether a sub-agent dispatch brief is
research/inspection (conductor-only — should refuse) or implementation
(agent-appropriate — allowed).

Operator rule: AIDOCS has indexed retrieval tools (ai_find,
ai_investigate, ai_trace, ai_bundle, etc.) that surface only the
needed content per call. The conductor — the agent with the user's full
conversation context — always reads + researches. Sub-agents are for
writing code per concrete briefs (file path + signature + decision
already made).

This module exposes one pure function. The PreToolUse hook calls it
when `tool_name == "Task"` (Claude Code's underlying name for what's
surfaced as `Agent`) and refuses dispatches whose brief contains a
research verb without an operator override.
"""

from __future__ import annotations

import re
from typing import Any

# Verb patterns matched as whole words (word boundaries) so the noun
# "research" inside "research-paper-summarizer" doesn't trigger.
# Patterns are lowercased; brief is lowercased before matching.
_RESEARCH_VERB_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bresearch\s+(?:the|a|an|how|why|where|whether|what)?\b"),
    re.compile(r"\binvestigate\b"),
    re.compile(r"\baudit\b"),
    re.compile(r"\bexplore\b"),
    re.compile(r"\banalyze\b"),
    re.compile(r"\banalyse\b"),
    re.compile(r"\bread\s+(?:the|these|those|this|all|every)?\s*(?:files?|code|module|source)\b"),
    re.compile(r"\blook\s+at\b"),
    re.compile(r"\bcheck\s+how\b"),
    re.compile(r"\bfind\s+out\b"),
    re.compile(r"\bfigure\s+out\b"),
    re.compile(r"\bdetermine\b"),
    re.compile(r"\bdiscover\b"),
    re.compile(r"\bsummariz[es]\b"),
    re.compile(r"\bsummariz?ation\b"),
    re.compile(r"\bdig\s+into\b"),
    re.compile(r"\bunderstand\s+how\b"),
    re.compile(r"\breport\s+(?:findings|on|back)\b"),
)


def evaluate_agent_brief(brief: str, override_phrase_present: bool) -> dict[str, Any]:
    """Decide whether an agent dispatch brief is research-shaped.

    Args:
        brief: Full sub-agent prompt text.
        override_phrase_present: True when the operator's current prompt
            contains an explicit override phrase (e.g. "delegate research")
            that lifts the research-block for this turn.

    Returns:
        {
            "allowed": bool,
            "reason": str,           # human-readable, suitable for tool-decision
            "matched_pattern": str,  # which pattern triggered (empty when allowed)
        }

    """
    if not brief or not brief.strip():
        return {
            "allowed": False,
            "reason": "Empty agent dispatch brief — refuse malformed dispatch.",
            "matched_pattern": "empty_brief",
        }

    if override_phrase_present:
        return {
            "allowed": True,
            "reason": (
                "Operator override grant active for this turn — "
                "research/inspection delegation permitted."
            ),
            "matched_pattern": "",
        }

    text = brief.lower()
    for pattern in _RESEARCH_VERB_PATTERNS:
        m = pattern.search(text)
        if m:
            return {
                "allowed": False,
                "reason": (
                    "Sub-agent dispatch brief contains research/inspection "
                    f"language (matched: '{m.group(0)}'). The conductor — "
                    "the agent with full conversation context — must do "
                    "research and code reading via indexed tools "
                    "(ai_find, ai_investigate, ai_trace, ai_bundle, "
                    "ai_get_lines). Sub-agents are for writing code per "
                    "concrete briefs (file path, signature, decision already "
                    "made). To override for this turn, the operator can "
                    "include 'delegate research' in their prompt."
                ),
                "matched_pattern": m.group(0),
            }

    return {
        "allowed": True,
        "reason": "Brief reads as implementation-only — dispatch permitted.",
        "matched_pattern": "",
    }
