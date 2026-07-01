"""Shared reason-validator for destructive/audit-critical tool calls.

Rule: `len(reason.strip()) >= min_chars`. Nothing more. Determinism over
semantic validation — a denylist of bad words is still a heuristic shim
that gets gamed. Audit visibility is how low-quality reasons get caught
at review time, not at input time.

Used by: ai_kill, ai_todo remove, ai_backlog remove, and any
future tool whose audit trail benefits from a human-written reason.
"""

from __future__ import annotations

DEFAULT_MIN_REASON_CHARS = 8


def validate_reason(
    reason: str | None,
    *,
    min_chars: int = DEFAULT_MIN_REASON_CHARS,
) -> dict[str, object] | None:
    """Return None on pass, else a structured refusal dict.

    Refusal shape:
        {"ok": False, "error": "<message>", "code": "reason_too_short" | "reason_missing"}
    """
    if reason is None:
        return {
            "ok": False,
            "code": "reason_missing",
            "error": "reason required (non-empty string)",
        }
    stripped = reason.strip()
    if not stripped:
        return {
            "ok": False,
            "code": "reason_missing",
            "error": "reason required (non-empty after trim)",
        }
    if len(stripped) < min_chars:
        return {
            "ok": False,
            "code": "reason_too_short",
            "error": (
                f"reason too short ({len(stripped)} chars after trim; "
                f"min {min_chars}). Explain WHY, not just acknowledge."
            ),
        }
    return None
