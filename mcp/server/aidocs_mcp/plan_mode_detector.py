"""Plan-mode phrase detection — recognize when the user wants the agent
to think/design before acting, and when they flip back to execute.

Layer 5 NLP deliverable (2026-04-19). Complements the existing
grant-detection in claude_hook (which handles `/allow` phrases) and
the intent_router (which classifies action_kinds); this module owns
the plan↔execute mode toggle.

Kept as a standalone module so the grant-detector lane and the
plan-mode lane don't fight for claude_hook.py ownership.
"""

from __future__ import annotations

import re

# Phrases that request plan mode (stop coding, think first).
# Matched case-insensitively against the lowercased prompt.
_PLAN_MODE_ON_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\blet'?s\s+plan\b",
        r"\bplan\s+(?:this|it)\s+out\b",
        r"\bplan\s+mode\b",
        r"\bthink\s+before\s+(?:acting|coding|writing)\b",
        r"\bstop\s+and\s+design\b",
        r"\bno\s+code\s+yet\b",
        r"\bdon'?t\s+code\s+yet\b",
        r"\bdesign\s+first\b",
        r"\bdraft\s+a\s+plan\b",
        r"\boutline\s+(?:the\s+)?(?:approach|plan|design)\b",
    )
)

# Phrases that switch plan mode off (resume execution).
_PLAN_MODE_OFF_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bok\s+(?:now\s+)?implement\b",
        r"\bok\s+go\b",
        r"\bproceed\b",
        r"\bexecute\b",
        r"\bstart\s+implementing\b",
        r"\bexit\s+plan\s+mode\b",
        r"\bexit\s+planning\b",
        r"\bgo\s+ahead\b",
        r"\bship\s+it\b",
    )
)


def detect_plan_mode_signal(prompt: str) -> str | None:
    """Return "on", "off", or None.

    "on" — prompt asks to enter plan mode.
    "off" — prompt asks to exit plan mode / execute.
    None — prompt carries neither signal; mode stays as-is.

    Both directions are checked; on wins the tie (a prompt saying both
    "let's plan but then execute" goes plan-first — the operator can
    follow up with an explicit "exit plan mode"). Never raises.
    """
    if not prompt:
        return None
    text = str(prompt).strip()
    if not text:
        return None
    # OFF patterns checked first — `exit plan mode` literally contains
    # `plan mode` which an ON pattern would otherwise win. Off signals
    # are more specific (they always name the exit action) so they
    # earn priority.
    for pattern in _PLAN_MODE_OFF_PATTERNS:
        if pattern.search(text):
            return "off"
    for pattern in _PLAN_MODE_ON_PATTERNS:
        if pattern.search(text):
            return "on"
    return None


def is_plan_mode_on_phrase(prompt: str) -> bool:
    """Pure classifier — True iff the prompt requests plan mode."""
    return detect_plan_mode_signal(prompt) == "on"


def is_plan_mode_off_phrase(prompt: str) -> bool:
    """Pure classifier — True iff the prompt requests execute mode."""
    return detect_plan_mode_signal(prompt) == "off"
