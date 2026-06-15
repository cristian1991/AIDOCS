"""SEC-012 (2026-04-22) — decision trace accumulator.

Thread a DecisionTrace through agent_orchestrator.check_tool so each
gate layer contributes one entry before returning. At the end of
check_tool, emit a `tool_decision_trace` execution event with the
full trace in payload_json so the dashboard's "Why?" button can
render per-layer breakdown on demand.

Opt-in: controlled by config `security.emit_decision_trace` (default
False). Trace accumulation runs always (it's a free local list
append); emission at end is the expensive part and is gated.

Input redaction: keys commonly holding secrets are scrubbed before
the payload is recorded. Allowlisted non-sensitive input keys (path,
query, command-preview) pass through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Keys we never copy into the trace. Substring match — any key whose
# lowercased name contains one of these terms gets redacted.
_REDACT_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "token",
    "credential",
    "auth",
    "session_cookie",
    "private_key",
    "ssh_key",
)


def _redact_input(tool_input: dict | None) -> dict:
    """Return a shallow copy of tool_input with sensitive keys
    masked. Non-sensitive keys stay as-is (type preserved) so the
    trace still shows what was attempted.
    """
    if not isinstance(tool_input, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in tool_input.items():
        key_lower = str(k).lower()
        if any(term in key_lower for term in _REDACT_KEY_SUBSTRINGS):
            out[k] = "***REDACTED***"
            continue
        # Truncate long strings so a 10KB command doesn't blow up
        # the payload. Keep first 200 chars as a preview.
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + f"…(truncated {len(v) - 200} chars)"
        else:
            out[k] = v
    return out


@dataclass
class DecisionTraceEntry:
    layer: str
    result: str  # "pass" | "bypass" | "block" | "skip" | "no_match" | ...
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionTrace:
    """Per-check_tool accumulator. Cheap to construct; entries append
    during the gate stack traversal; one final event emitted at the
    end of check_tool when emission is enabled.
    """

    tool_name: str
    tool_input: dict
    session_id: str = ""
    task_id: str = ""
    is_lane_agent: bool = False
    is_subagent: bool = False
    managed_mode_active: bool = False
    checks: list[DecisionTraceEntry] = field(default_factory=list)
    final_allowed: bool | None = None
    final_reason: str = ""
    final_blocked_by: str = ""

    def add(self, layer: str, result: str, **notes: Any) -> None:
        self.checks.append(DecisionTraceEntry(layer=layer, result=result, notes=notes))

    def set_final(
        self,
        *,
        allowed: bool,
        reason: str = "",
        blocked_by: str = "",
    ) -> None:
        self.final_allowed = allowed
        self.final_reason = reason
        self.final_blocked_by = blocked_by

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_input": _redact_input(self.tool_input),
            "session_id": self.session_id,
            "task_id": self.task_id,
            "is_lane_agent": self.is_lane_agent,
            "is_subagent": self.is_subagent,
            "managed_mode_active": self.managed_mode_active,
            "checks": [
                {"layer": e.layer, "result": e.result, "notes": e.notes} for e in self.checks
            ],
            "final": {
                "allowed": self.final_allowed,
                "reason": self.final_reason,
                "blocked_by": self.final_blocked_by,
            },
        }


def is_trace_enabled(project_root) -> bool:
    """Read the opt-in flag. Default False — trace accumulation still
    runs (cheap), but emission at check_tool return is gated.
    """
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "security.emit_decision_trace",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False
