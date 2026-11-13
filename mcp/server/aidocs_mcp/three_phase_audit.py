"""Three-phase audit discipline -- the generic primitive (neutral public module).

Design decision (2026-10-29): no public module imports a gate file. The local
anchor door (anchor_service.apply) audits its mutations with the same
intent / execute / result ordering the outer gate uses, so the ordering
primitive lives here and outer_gate_audit re-exports it. It is a pure
ordering helper over an injected sink: it decides nothing about who may act.

DOCTRINE (enforced here):
  1. INTENT audit BEFORE execution. If it cannot be durably recorded AND the op
     MUTATES, REFUSE and do NOT execute -- fail closed, nothing mutated. For a
     non-mutating op a failed intent-audit is tolerated: there is no state
     change a post-hoc audit could miss.
  2. The caller's own mandatory audit / authorization still gates execution --
     that stays at the call site; this helper does not replace it.
  3. RESULT audit AFTER execution. A failure is surfaced as ``audit_degraded`` --
     the mutation STANDS and is already intent-audited, so the deed is never lost
     or silently swallowed into a clean success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ThreePhaseResult:
    """Outcome of a three-phase-audited execution.

    executed       -- whether ``execute`` ran (False only when intent-audit fail-closed
                      a mutating op).
    result         -- ``execute``'s return value, or None when not executed.
    audit_degraded -- True when the RESULT audit could not be recorded; the result still
                      STANDS (it is intent-audited) but the result-phase record is missing.
    refused_reason -- "" unless the op was refused BEFORE execution (currently only
                      ``intent_audit_unrecorded``).
    """

    executed: bool
    result: Any
    audit_degraded: bool
    refused_reason: str


def _try_sink(sink: Callable[[dict], None], event: dict) -> bool:
    """Best-effort durable record. Returns True iff the sink accepted the event."""
    try:
        sink(event)
        return True
    except Exception:
        return False


def three_phase_audited_execute(
    *,
    intent_event: dict,
    execute: Callable[[], Any],
    result_event_builder: Callable[[Any], dict],
    sink: Callable[[dict], None],
    is_mutating: bool,
) -> ThreePhaseResult:
    """Run ``execute`` under the three-phase audit discipline (see module docstring).

    The caller is still responsible for its own audit_or_refuse + authorization
    BEFORE calling this -- this helper governs only the intent/result audit ORDERING
    around the execution. ``execute`` exceptions propagate AFTER the intent audit, so
    a failed attempt is always on record.
    """
    # -- Phase 1: INTENT audit, BEFORE any execution --
    intent_recorded = _try_sink(sink, intent_event)
    if not intent_recorded and is_mutating:
        # A mutation whose intent we cannot durably record must NOT run. Fail closed --
        # nothing has executed, so nothing was mutated unobserved.
        return ThreePhaseResult(
            executed=False,
            result=None,
            audit_degraded=False,
            refused_reason="intent_audit_unrecorded",
        )

    # -- Phase 2: execute (the caller's mandatory audit already gated this) --
    result = execute()

    # -- Phase 3: RESULT audit, AFTER execution -- failure is degraded, never lost --
    result_recorded = _try_sink(sink, result_event_builder(result))
    return ThreePhaseResult(
        executed=True,
        result=result,
        audit_degraded=not result_recorded,
        refused_reason="",
    )
