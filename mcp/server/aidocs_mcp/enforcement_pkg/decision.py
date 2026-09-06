"""Decision shape — what the controller returns.

Adapters take this and shape it into a host-native response. The
Decision is host-shape-agnostic: the same Decision can be wrapped
into a CC `hookSpecificOutput`, an MCP refusal dict, an OpenCode
verdict, etc.

Trace is always-on (not debug-only): operators chasing a refusal
need it. Cheap to populate; cheaper than re-running with a flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    ALLOW = "allow"
    REFUSE = "refuse"
    CONFIRMABLE = "confirmable"


@dataclass
class GateStep:
    """One gate's contribution to the Decision."""

    gate_name: str  # e.g. "freeze" / "judge" / "managed_mode"
    verdict: str  # gate's local verdict
    reason: str  # short reason code or message
    elapsed_ms: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class DegradedState:
    """Set when a validator/store is unavailable mid-decision.

    Per doctrine (Q3 pending Empire resolution): default policy is
    REFUSE on degraded. The struct still surfaces so audit knows
    which gate degraded.
    """

    gate_name: str
    error_kind: str  # e.g. "validator_unavailable"
    error_detail: str = ""


@dataclass
class HostAction:
    """Side-effect the host should perform after consuming the
    Decision. E.g. "show this confirmation prompt" / "emit this
    audit row" / "mint this grant".
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditEvent:
    """An audit event the controller wants emitted. The controller
    emits these via ExecutionIndexStore in finalize; adapters
    don't have to.
    """

    event_kind: str
    source_kind: str
    capability_name: str
    action_kind: str
    target_entity: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """Canonical controller verdict."""

    request_id: str  # echoed
    verdict: Verdict
    reason_code: str  # stable taxonomy
    user_message: str  # operator-facing
    enforcement: dict[str, Any] = field(default_factory=dict)
    host_actions: list[HostAction] = field(default_factory=list)
    audit_events: list[AuditEvent] = field(default_factory=list)
    trace: list[GateStep] = field(default_factory=list)  # always-on

    # Bypass / skip taxonomy (split per doctrine § IV.1)
    bypassed_by_override: list[str] = field(default_factory=list)
    skipped_after_decision: list[str] = field(default_factory=list)

    # Resolved identity at decision time
    session_id: str = ""
    conductor_id: str = ""

    # Freeze / grants
    freeze_id: str | None = None
    grants_consumed: list[str] = field(default_factory=list)

    # Degraded state
    degraded_state: DegradedState | None = None

    # Path normalization echo
    normalized_targets: list[str] = field(default_factory=list)
