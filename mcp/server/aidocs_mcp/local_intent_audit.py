"""Interrupt-safe pre-execution audit for the LOCAL stdio tool chokepoint.

#441 (causal-turn-interrupt-integrity spec) — the stdio twin of the WebMCP
three-phase discipline in ``outer_gate_audit.three_phase_audited_execute``
(#93 phase 1). The wrapper in ``mcp_server._real_instrumented_call_tool``
already writes ``tool_call_started`` BEFORE the tool executes; this module
formalizes that row as the durable TOOL-ATTEMPT (intent) record and pins
the spec's ordering law around it:

  1. INTENT audit BEFORE execution. If the intent row cannot be durably
     recorded AND the tool is MUTATING-tier, REFUSE and do NOT execute —
     fail closed, nothing mutated. A mid-execution interrupt (process
     kill, ^C, host disconnect) can therefore never yield an
     executed-but-unaudited mutation: either the intent row is on disk
     first, or the side-effect boundary is never crossed.
  2. For a non-mutating (read-tier) tool a failed intent audit is
     tolerated — there is no state change a post-hoc audit could miss.
  3. RESULT audit AFTER execution. A failure there is AUDIT_DEGRADED —
     the mutation STANDS and is already intent-audited, so the deed is
     never lost and never retroactively fail-closed.

Tier resolution: ``tool_is_mutating`` consults the declared tool contract
(``tool_interface._TOOLS`` — tier M/A or class edit/run/import/admin) and
falls back to ``tool_gate_service.classify_tool_action`` buckets for
undeclared/external names. Unknown read-shaped tools resolve non-mutating;
declared metadata always wins.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

# Mutating tiers per the WebMCP manifest vocabulary (outer_gate_manifest):
# Tier M (surgical edit) and Tier A (admin). Tier R is read-only; Tier L
# is selector/list. Classes that imply side effects regardless of tier.
_MUTATING_TIERS = frozenset({"M", "A"})
_MUTATING_CLASSES = frozenset({"edit", "run", "import", "admin"})
# classify_tool_action buckets that imply side effects for tools with no
# declared ToolSpec (native/external names reaching the local wrapper).
_MUTATING_BUCKETS = frozenset({"edit", "run", "agent"})


class IntentAuditRefused(RuntimeError):
    """A MUTATING-tier tool call was refused because its pre-execution
    intent audit could not be durably recorded (fail closed; nothing
    executed, nothing mutated)."""


def tool_is_mutating(tool_name: str) -> bool:
    """Resolve whether ``tool_name`` is mutating-tier for the intent gate.

    Declared contract first (tool_interface), coarse action bucket as the
    fallback. Best-effort on lookup errors — an unresolvable name falls
    back to the bucket classifier, never raises.
    """
    bare = str(tool_name or "").strip()
    for prefix in ("mcp__aidocs__", "mcp__playwright__", "mcp__"):
        if bare.lower().startswith(prefix):
            bare = bare[len(prefix) :]
            break
    try:
        from .tool_interface import _TOOLS

        spec = _TOOLS.get(bare) or _TOOLS.get(bare.lower())
        if spec is not None:
            return (
                str(spec.tier).strip().upper() in _MUTATING_TIERS
                or str(spec.cls).strip().lower() in _MUTATING_CLASSES
            )
    except Exception:
        pass
    try:
        from .tool_gate_service import classify_tool_action

        return classify_tool_action(bare) in _MUTATING_BUCKETS
    except Exception:
        # Cannot classify at all → treat as mutating (fail-closed bias:
        # an unclassifiable tool must not dodge the intent gate).
        return True


def intent_audit_or_refuse(
    record_intent: Callable[[], Any],
    *,
    is_mutating: bool,
    tool_name: str,
) -> bool:
    """Phase 1 — durable INTENT record BEFORE the side-effect boundary.

    Returns True when the intent row landed. When ``record_intent``
    raises: a MUTATING tool is refused via :class:`IntentAuditRefused`
    (fail closed — the caller must not execute); a non-mutating tool
    proceeds un-intent-audited (returns False, doctrine rule 2).
    """
    try:
        record_intent()
        return True
    except Exception as exc:
        if is_mutating:
            raise IntentAuditRefused(
                f"intent_audit_unrecorded: refusing to execute mutating tool "
                f"'{tool_name}' — its pre-execution audit row could not be "
                f"durably recorded ({type(exc).__name__}: {exc}). Nothing "
                f"was executed. Restore the audit store and retry."
            ) from exc
        return False


def result_audit_degraded(
    record_result: Callable[[], Any],
    *,
    tool_name: str,
) -> bool:
    """Phase 3 — RESULT audit AFTER execution; failure degrades, never
    raises. Returns True when the result audit FAILED (audit_degraded):
    the executed deed stands (it is intent-audited) and the caller must
    still return the tool result. A stderr note keeps the gap observable.
    """
    try:
        record_result()
        return False
    except Exception as exc:
        try:
            sys.stderr.write(
                f"[aidocs audit] RESULT audit degraded for '{tool_name}' "
                f"(intent row already durable; result stands): "
                f"{type(exc).__name__}: {exc}\n"
            )
        except Exception:
            pass
        return True
