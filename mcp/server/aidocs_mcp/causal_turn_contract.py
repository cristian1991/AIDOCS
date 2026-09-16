"""Causal Turn Context vocabulary — contract-freeze remainder (#467, WAR AU).

CANONICAL SPEC (Emperor-sealed 2026-07-17):
    .MEMORY/specs/causal-turn-interrupt-integrity.md
Companion main spec:
    .MEMORY/specs/governance-authority-domain.md  (governance_contract.py)

This module is the Causal Turn Context's PURE skeleton, mirroring the
``governance_contract`` pattern (WAR I, #437/#453):

* stdlib only — ``enum`` + ``hashlib``;
* ZERO imports from managed_mode / gate / store code;
* ZERO I/O, ZERO process-global mutable state.

It freezes the spec's ubiquitous language VERBATIM — the TurnState machine,
InstructionKind, the CausalEdge taxonomy, SealReason, OrphanResolution and
ToolOutcomeState — plus three pure helpers the persistence layer
(``causal_turn_store``) and the executor build on:

* :func:`assert_turn_transition` — the frozen TurnState transition law;
* :func:`compute_event_merkle_root` — the per-turn seal's event-set root
  (domain-separated leaf/node hashing, second-preimage safe);
* :func:`decision_is_stale` / :func:`reject_stale_decision` — the spec's
  "executor must REJECT STALE decisions when the active instruction
  revision has changed before dispatch" law, as a pure total function.

Hash sequencing (#467 law): the row-hash fields these entities introduce
(``instruction_id``, ``instruction_revision``, ``causal_edge``) fold into
the audit row hash in ONE version bump — v5, see
``execution_index_store._compute_row_hash`` — never incrementally.
"""

from __future__ import annotations

import hashlib
from enum import Enum

__all__ = [
    "CAUSAL_EDGE_VALUES",
    "INTERRUPT_INSTRUCTION_KINDS",
    "TERMINAL_STATE_FOR_SEAL_REASON",
    "TURN_TRANSITIONS",
    "CausalEdge",
    "IllegalTurnTransition",
    "InstructionKind",
    "OrphanResolution",
    "SealReason",
    "StaleGovernanceDecision",
    "ToolOutcomeState",
    "TurnState",
    "assert_turn_transition",
    "compute_event_merkle_root",
    "decision_is_stale",
    "reject_stale_decision",
]


class TurnState(str, Enum):
    """Spec: ``TurnState = Open | Executing | InterruptPending | Interrupted
    | Completed | Abandoned | Sealed``. A turn is NOT a session, an AIDOCS
    task, an agent epoch, a model response, a transport connection, or a
    Stop-hook invocation."""

    OPEN = "open"
    EXECUTING = "executing"
    INTERRUPT_PENDING = "interrupt_pending"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    SEALED = "sealed"


#: The frozen transition law. SEALED is terminal (no outgoing edges); a
#: self-loop on INTERRUPT_PENDING admits a second, DISTINCT interrupt while
#: the first is still draining (duplicate delivery of the SAME interrupt is
#: deduplicated upstream and never re-transitions).
TURN_TRANSITIONS: dict[TurnState, frozenset[TurnState]] = {
    TurnState.OPEN: frozenset(
        {
            TurnState.EXECUTING,
            TurnState.INTERRUPT_PENDING,
            TurnState.COMPLETED,
            TurnState.ABANDONED,
        }
    ),
    TurnState.EXECUTING: frozenset(
        {
            TurnState.INTERRUPT_PENDING,
            TurnState.COMPLETED,
            TurnState.ABANDONED,
        }
    ),
    TurnState.INTERRUPT_PENDING: frozenset(
        {
            TurnState.INTERRUPT_PENDING,
            TurnState.INTERRUPTED,
            TurnState.ABANDONED,
        }
    ),
    TurnState.INTERRUPTED: frozenset({TurnState.SEALED}),
    TurnState.COMPLETED: frozenset({TurnState.SEALED}),
    TurnState.ABANDONED: frozenset({TurnState.SEALED}),
    TurnState.SEALED: frozenset(),
}


class IllegalTurnTransition(ValueError):
    """A TurnState move outside :data:`TURN_TRANSITIONS` was attempted."""


def assert_turn_transition(current: TurnState, new: TurnState) -> None:
    """Raise :class:`IllegalTurnTransition` unless ``current -> new`` is a
    legal edge of the frozen machine. Pure and total."""
    allowed = TURN_TRANSITIONS.get(TurnState(current), frozenset())
    if TurnState(new) not in allowed:
        raise IllegalTurnTransition(
            f"illegal turn transition {TurnState(current).value!r} -> "
            f"{TurnState(new).value!r}; allowed: "
            f"{sorted(s.value for s in allowed)}"
        )


class InstructionKind(str, Enum):
    """Spec: ``InstructionKind = UserPrompt | CoConductorInterrupt |
    OperatorInterrupt | OperatorOverride | SystemConstraint | Confirmation
    | RecoveryDirective``."""

    USER_PROMPT = "user_prompt"
    CO_CONDUCTOR_INTERRUPT = "co_conductor_interrupt"
    OPERATOR_INTERRUPT = "operator_interrupt"
    OPERATOR_OVERRIDE = "operator_override"
    SYSTEM_CONSTRAINT = "system_constraint"
    CONFIRMATION = "confirmation"
    RECOVERY_DIRECTIVE = "recovery_directive"


#: Instruction kinds that ARE interrupts (spec Interrupt Semantics): they
#: advance the instruction revision AND move the turn to InterruptPending.
INTERRUPT_INSTRUCTION_KINDS = frozenset(
    {InstructionKind.CO_CONDUCTOR_INTERRUPT, InstructionKind.OPERATOR_INTERRUPT}
)


class CausalEdge(str, Enum):
    """Spec: ``CausalEdge = DirectUserRequest | DerivedPlanStep |
    RequiredVerification | GovernanceRequired | RepairOrRecovery |
    CoConductorRedirect | OperatorOverride``. The ITS-frozen taxonomy —
    proves WHY a lower-level action occurred without falsely claiming the
    user literally named every tool call."""

    DIRECT_USER_REQUEST = "direct_user_request"
    DERIVED_PLAN_STEP = "derived_plan_step"
    REQUIRED_VERIFICATION = "required_verification"
    GOVERNANCE_REQUIRED = "governance_required"
    REPAIR_OR_RECOVERY = "repair_or_recovery"
    CO_CONDUCTOR_REDIRECT = "co_conductor_redirect"
    OPERATOR_OVERRIDE = "operator_override"


#: The valid stored spellings for ``execution_events.causal_edge``. A value
#: outside this set is a CLAIM the store refuses to persist (coerced to '')
#: — adversarial invariant: garbage taxonomy never enters the hash-bound row.
CAUSAL_EDGE_VALUES = frozenset(m.value for m in CausalEdge)


class SealReason(str, Enum):
    """Spec: ``SealReason = NormalCompletion | UserInterrupt |
    CoConductorInterrupt | OperatorAbort | ClientDisconnect | AgentCrash |
    ServerRecovery | LeaseExpiry``."""

    NORMAL_COMPLETION = "normal_completion"
    USER_INTERRUPT = "user_interrupt"
    CO_CONDUCTOR_INTERRUPT = "co_conductor_interrupt"
    OPERATOR_ABORT = "operator_abort"
    CLIENT_DISCONNECT = "client_disconnect"
    AGENT_CRASH = "agent_crash"
    SERVER_RECOVERY = "server_recovery"
    LEASE_EXPIRY = "lease_expiry"


#: Which terminal TurnState a seal reason drives the turn through before
#: SEALED. Interrupt-flavored reasons terminate as INTERRUPTED; lifecycle
#: losses (disconnect/crash/recovery/lease) terminate as ABANDONED.
TERMINAL_STATE_FOR_SEAL_REASON: dict[SealReason, TurnState] = {
    SealReason.NORMAL_COMPLETION: TurnState.COMPLETED,
    SealReason.USER_INTERRUPT: TurnState.INTERRUPTED,
    SealReason.CO_CONDUCTOR_INTERRUPT: TurnState.INTERRUPTED,
    SealReason.OPERATOR_ABORT: TurnState.INTERRUPTED,
    SealReason.CLIENT_DISCONNECT: TurnState.ABANDONED,
    SealReason.AGENT_CRASH: TurnState.ABANDONED,
    SealReason.SERVER_RECOVERY: TurnState.ABANDONED,
    SealReason.LEASE_EXPIRY: TurnState.ABANDONED,
}


class OrphanResolution(str, Enum):
    """Spec: ``OrphanResolution = ProvenNotExecuted | ProvenSucceeded |
    ProvenFailed | SafelyRetried | Indeterminate``. A durable attempt
    without an outcome is never deleted or assumed unsuccessful — it is
    reconciled into exactly one of these."""

    PROVEN_NOT_EXECUTED = "proven_not_executed"
    PROVEN_SUCCEEDED = "proven_succeeded"
    PROVEN_FAILED = "proven_failed"
    SAFELY_RETRIED = "safely_retried"
    INDETERMINATE = "indeterminate"


class ToolOutcomeState(str, Enum):
    """Spec: the immutable result vocabulary for one attempt. Denied
    attempts are audit events too."""

    ALLOWED_AND_SUCCEEDED = "allowed_and_succeeded"
    ALLOWED_AND_FAILED = "allowed_and_failed"
    DENIED = "denied"
    CANCELED_BEFORE_EXECUTION = "canceled_before_execution"
    INTERRUPTED_DURING_EXECUTION = "interrupted_during_execution"
    TIMED_OUT = "timed_out"
    CONNECTION_LOST = "connection_lost"
    INDETERMINATE = "indeterminate"


# ── Turn seal: event-set Merkle root ─────────────────────────────────


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


#: Root of the empty event set — a turn can legally seal with zero events
#: (e.g. an interrupted turn that never authorized a tool). Domain-fixed
#: constant so "no events" is distinguishable from "hash of empty string".
_EMPTY_MERKLE_ROOT = hashlib.sha256(b"aidocs-causal-turn-merkle-empty").hexdigest()


def compute_event_merkle_root(leaf_hashes: list[str] | tuple[str, ...]) -> str:
    """Merkle root over an ORDERED turn event set.

    ``leaf_hashes`` are the per-row audit hashes (the same
    ``_row_hash_from_stored_row`` values the session Merkle chain uses), in
    chain_seq order. Leaves and interior nodes are domain-separated
    (``leaf|`` / ``node|``) so a leaf can never be replayed as an interior
    node (second-preimage hardening). Odd nodes promote (carry up
    unchanged). Pure and deterministic.
    """
    level = [_sha256_hex(f"leaf|{h}") for h in leaf_hashes]
    if not level:
        return _EMPTY_MERKLE_ROOT
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(_sha256_hex(f"node|{level[i]}|{level[i + 1]}"))
        if len(level) % 2 == 1:
            nxt.append(level[-1])
        level = nxt
    return level[0]


# ── Stale-decision rejection (Required Governance Decision Extension) ──


class StaleGovernanceDecision(RuntimeError):
    """A governance decision was rejected at dispatch because the active
    instruction revision changed after it was decided (spec: "The executor
    must REJECT STALE decisions when the active instruction revision has
    changed before dispatch")."""


def decision_is_stale(
    *,
    decision_instruction_id: str,
    decision_instruction_revision: int,
    active_instruction_id: str,
    active_instruction_revision: int,
) -> bool:
    """True when a decision's causal binding is superseded.

    A decision bound to instruction (id, revision) is stale when the ACTIVE
    instruction differs — a newer revision on the same turn, or a different
    instruction entirely. A decision with NO causal binding (empty id AND
    revision 0 — the pre-causal shape every current caller produces) is NOT
    stale: staleness requires a binding to have gone stale. Pure and total.
    """
    if not decision_instruction_id and int(decision_instruction_revision) <= 0:
        return False
    if str(decision_instruction_id) != str(active_instruction_id):
        return True
    return int(decision_instruction_revision) < int(active_instruction_revision)


def reject_stale_decision(
    *,
    decision_instruction_id: str,
    decision_instruction_revision: int,
    active_instruction_id: str,
    active_instruction_revision: int,
) -> None:
    """Raise :class:`StaleGovernanceDecision` when the binding is stale —
    the executor-side chokepoint helper. No-op when fresh or unbound."""
    if decision_is_stale(
        decision_instruction_id=decision_instruction_id,
        decision_instruction_revision=decision_instruction_revision,
        active_instruction_id=active_instruction_id,
        active_instruction_revision=active_instruction_revision,
    ):
        raise StaleGovernanceDecision(
            f"stale governance decision: decided under instruction "
            f"{decision_instruction_id!r} rev {int(decision_instruction_revision)}, "
            f"but the active instruction is {active_instruction_id!r} rev "
            f"{int(active_instruction_revision)} — re-authorize under the "
            f"current instruction revision before dispatch"
        )
