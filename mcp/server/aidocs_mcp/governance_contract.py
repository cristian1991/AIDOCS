"""Governance contract vocabulary — Phase 0 contract pin (WAR I, #437/#453).

CANONICAL SPEC (the WHAT / invariants, Emperor-sealed 2026-07-17):
    .MEMORY/specs/governance-authority-domain.md
Companion addendum:
    .MEMORY/specs/causal-turn-interrupt-integrity.md

This module is the contract's SKELETON, pinned beside the current system
(spec Migration Strategy step 1). It is deliberately PURE:

* stdlib only — ``dataclasses`` + ``enum``;
* ZERO imports from managed_mode / gate / store code;
* ZERO I/O, ZERO process-global state, ZERO host-configuration reads;
* NO behavior change to the running system — nothing imports this yet.

Later phases (2+) implement the bounded contexts and
``GovernanceAuthority.resolve`` AGAINST these types; the property tests in
``mcp/tests/governance/`` pin the semantics so adoption cannot drift.

Ubiquitous language (spec): the terms ``managed`` / ``active`` / ``enabled``
/ ``disabled`` must not remain ambiguous authority concepts. This module
carries the replacement vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AUTHORIZING_ENFORCEMENT_HEALTH",
    "BindingState",
    "CommissionReadOutcome",
    "CommissionState",
    "DenialReason",
    "EnforcementHealth",
    "GovernanceDecision",
    "MembershipState",
    "ShadowObservation",
    "classify_commission_read",
    "evaluate_authorization",
    "observe_commission_shadow",
]


class CommissionState(str, Enum):
    """Commissioning Context — owns whether a project is governed.

    Commissioning is the SOLE project-governance authority (invariant 1).
    No hook file, MCP config, plugin, JSON document, dashboard toggle, or
    singleton may independently grant or revoke it (invariants 9, 10).
    """

    UNCOMMISSIONED = "uncommissioned"
    COMMISSIONED = "commissioned"
    INVALID_AUTHORITY = "invalid_authority"


class BindingState(str, Enum):
    """Actor Binding Context — the authenticated actor-to-session relation.

    Keyed by authenticated actor identity AND project identity. A
    project-wide "current session" singleton is NOT authority (invariant 9).
    """

    UNBOUND = "unbound"
    BOUND = "bound"
    STALE = "stale"
    IDENTITY_MISSING = "identity_missing"


class MembershipState(str, Enum):
    """Session Membership Context — does the session belong to the project?

    Session names are identifiers, not authorization credentials
    (invariant 8). Validation is fail-closed: ``UNKNOWN`` never authorizes.
    """

    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class EnforcementHealth(str, Enum):
    """Enforcement Health Context — harness-keyed operational health.

    Health is keyed on HARNESS with LOCATION as a modifier; the MODEL is
    enforcement-irrelevant (spec: three orthogonal axes). Host artifacts
    are EVIDENCE of enforcement health, never governance authority.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BROKEN = "broken"
    NOT_APPLICABLE = "not_applicable"


#: Enforcement states under which the required enforcement path counts as
#: trusted for authorization. DEGRADED and BROKEN both fail closed
#: (invariant 4; denial reasons enforcement_degraded / enforcement_broken).
AUTHORIZING_ENFORCEMENT_HEALTH = frozenset(
    {EnforcementHealth.HEALTHY, EnforcementHealth.NOT_APPLICABLE}
)


class DenialReason(str, Enum):
    """The spec's denial-reason enumeration — every denial must identify
    the failed domain condition. No authorization-bearing consumer may
    branch on a generic ``active`` boolean."""

    PROJECT_UNCOMMISSIONED = "project_uncommissioned"
    PROJECT_AUTHORITY_INVALID = "project_authority_invalid"
    ACTOR_UNBOUND = "actor_unbound"
    ACTOR_IDENTITY_MISSING = "actor_identity_missing"
    BINDING_STALE = "binding_stale"
    SESSION_NOT_IN_PROJECT = "session_not_in_project"
    ENFORCEMENT_DEGRADED = "enforcement_degraded"
    ENFORCEMENT_BROKEN = "enforcement_broken"


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    """The pure-domain slice of the spec's GovernanceDecision.

    #467 (causal-turn addendum, Required Governance Decision Extension):
    the identity / causal fields are now part of the frozen shape —
    ``decision_id``, ``project_identity``, ``actor_identity``,
    ``session_identity``, ``turn_id``, ``instruction_id``,
    ``instruction_revision``, ``authority_revision``, ``decided_at``.
    They default to the UNBOUND shape (''/0) because every current caller
    is pre-causal; the executor-side staleness law lives in
    ``causal_turn_contract.decision_is_stale`` / ``reject_stale_decision``
    (a decision whose instruction revision is superseded before dispatch
    must be rejected). Populating them requires I/O and server-attested
    state and is owned by the phase that adopts this contract at the
    dispatch chokepoint — nothing here performs I/O.
    """

    commission_state: CommissionState
    binding_state: BindingState
    membership_state: MembershipState
    enforcement_health: EnforcementHealth
    governed: bool
    authorization_ready: bool
    bootstrap_allowed: bool
    repair_allowed: bool
    reasons: tuple[DenialReason, ...]
    # ── #467 causal extension (server-attested when populated) ──
    decision_id: str = ""
    project_identity: str = ""
    actor_identity: str = ""
    session_identity: str = ""
    turn_id: str = ""
    instruction_id: str = ""
    instruction_revision: int = 0
    authority_revision: int = 0
    decided_at: str = ""

    @property
    def stale_binding(self) -> bool:
        """Spec: a Stale Binding is one whose session membership or actor
        identity is no longer valid — an explicitly STALE binding, or a
        binding whose membership can no longer be proven VALID."""
        return self.binding_state is BindingState.STALE or (
            self.binding_state is BindingState.BOUND
            and self.membership_state is not MembershipState.VALID
        )


def evaluate_authorization(
    commission_state: CommissionState,
    binding_state: BindingState,
    membership_state: MembershipState,
    enforcement_health: EnforcementHealth,
    *,
    # #467 causal extension: server-attested identity/causal context, passed
    # through verbatim into the decision. Defaults keep every existing
    # caller pre-causal (unbound shape) — purity and determinism unchanged.
    decision_id: str = "",
    project_identity: str = "",
    actor_identity: str = "",
    session_identity: str = "",
    turn_id: str = "",
    instruction_id: str = "",
    instruction_revision: int = 0,
    authority_revision: int = 0,
    decided_at: str = "",
) -> GovernanceDecision:
    """The AuthorizationReady pure function (spec Authorization Context).

    AuthorizationReady =
        Commissioned AND actor identity valid AND actor binding valid
        AND session membership valid AND required enforcement path trusted

    Pure and total: a deterministic function of its four arguments and
    nothing else (invariant 15's hot-path shape; no parsing, no I/O).
    Fail-closed everywhere: every non-authorizing input contributes an
    explicit :class:`DenialReason` (Required Failure Behavior table).
    """
    reasons: list[DenialReason] = []

    if commission_state is CommissionState.UNCOMMISSIONED:
        # AIDOCS governance is not asserted for uncommissioned projects.
        reasons.append(DenialReason.PROJECT_UNCOMMISSIONED)
    elif commission_state is CommissionState.INVALID_AUTHORITY:
        # Corrupt authority store: fail closed; operator repair required.
        reasons.append(DenialReason.PROJECT_AUTHORITY_INVALID)

    if binding_state is BindingState.UNBOUND:
        reasons.append(DenialReason.ACTOR_UNBOUND)
    elif binding_state is BindingState.IDENTITY_MISSING:
        reasons.append(DenialReason.ACTOR_IDENTITY_MISSING)
    elif binding_state is BindingState.STALE:
        reasons.append(DenialReason.BINDING_STALE)

    if membership_state is not MembershipState.VALID:
        # Fail-closed membership: INVALID and UNKNOWN both deny
        # (a session ID alone cannot authorize work — invariant 8).
        reasons.append(DenialReason.SESSION_NOT_IN_PROJECT)

    if enforcement_health is EnforcementHealth.DEGRADED:
        reasons.append(DenialReason.ENFORCEMENT_DEGRADED)
    elif enforcement_health is EnforcementHealth.BROKEN:
        reasons.append(DenialReason.ENFORCEMENT_BROKEN)

    governed = commission_state is not CommissionState.UNCOMMISSIONED
    ready = not reasons
    # Failure matrix: commissioned + unbound ⇒ bootstrap tools allowed,
    # governed work blocked. InvalidAuthority fails closed entirely.
    bootstrap_allowed = commission_state is CommissionState.COMMISSIONED
    # Failure matrix: hooks/MCP missing ⇒ repair path allowed; corrupt
    # authority store ⇒ operator repair required.
    repair_allowed = (
        commission_state is CommissionState.COMMISSIONED
        and enforcement_health in (EnforcementHealth.DEGRADED, EnforcementHealth.BROKEN)
    ) or commission_state is CommissionState.INVALID_AUTHORITY

    return GovernanceDecision(
        commission_state=commission_state,
        binding_state=binding_state,
        membership_state=membership_state,
        enforcement_health=enforcement_health,
        governed=governed,
        authorization_ready=ready,
        bootstrap_allowed=bootstrap_allowed,
        repair_allowed=repair_allowed,
        reasons=tuple(reasons),
        decision_id=decision_id,
        project_identity=project_identity,
        actor_identity=actor_identity,
        session_identity=session_identity,
        turn_id=turn_id,
        instruction_id=instruction_id,
        instruction_revision=instruction_revision,
        authority_revision=authority_revision,
        decided_at=decided_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# #440 — THE COMMISSION-READ CARRIER, and the shadow observation that
# precedes flipping it into force.
#
# THE DISEASE (general form, named 2026-07-30): a carrier with fewer states
# than the outcome it reports, defaulting to the permissive value.
#
# THE INSTANCE: ``mcp_server_runtime_helpers._has_commission_stamp`` is typed
# ``-> bool``. It reports THREE outcomes — stamped, unstamped, and "the store
# did not answer" (``sqlite3.Error``) — and the third collapses to ``False``.
# ``False`` means uncommissioned means ungoverned, so an unreadable authority
# store GRANTS by silence. A two-state boolean cannot say "unknown", so the
# fix belongs in the CARRIER, not at the call site: patching the ``except``
# arm only moves the failure to the next consumer that reads the bool.
#
# These functions are the PURE half of that repair. They perform no I/O and
# read no store — the caller performs the read and reports its OUTCOME here.
# That split is deliberate: it keeps this module stdlib-pure (pinned by
# tests/governance/test_governance_vocabulary.py TestModulePurity) and makes
# the authority classification unit-testable without a filesystem.
# ─────────────────────────────────────────────────────────────────────────────


class CommissionReadOutcome(str, Enum):
    """What a commission-stamp READ actually observed.

    Four outcomes, three authority meanings. ``STAMPED`` and
    ``LEGACY_MARKER`` both mean *commissioned* — that collapse is lawful
    because it loses no authority information (the legacy marker is the
    heal-forward bridge, governance-bearing by construction).

    ``STORE_UNREADABLE`` is the state the old ``bool`` could not express:
    the store exists but did not answer. It is NOT ``ABSENT``.
    """

    #: index_meta carries the deliberate commission stamp.
    STAMPED = "stamped"
    #: No stamp, but the governance-bearing legacy ``.aidocs`` marker exists.
    LEGACY_MARKER = "legacy_marker"
    #: The store answered, and this project is genuinely not commissioned.
    ABSENT = "absent"
    #: The store did NOT answer — corrupt db, unreadable file, sqlite error.
    #: The honest "unknown". Never a decision.
    STORE_UNREADABLE = "store_unreadable"


def classify_commission_read(outcome: CommissionReadOutcome) -> CommissionState:
    """Map an observed read outcome to its authority state. Pure and TOTAL.

    The load-bearing line is the last one: an unreadable store is
    ``INVALID_AUTHORITY``, never ``UNCOMMISSIONED``. Under the failure
    matrix that keeps the project GOVERNED (the gate stays on) while
    ``authorization_ready`` is False and ``repair_allowed`` is True — fail
    closed on the GRANT, but explicitly NOT a lockout, because the operator
    must always retain the path that fixes the store.
    """
    if outcome is CommissionReadOutcome.STAMPED:
        return CommissionState.COMMISSIONED
    if outcome is CommissionReadOutcome.LEGACY_MARKER:
        return CommissionState.COMMISSIONED
    if outcome is CommissionReadOutcome.ABSENT:
        return CommissionState.UNCOMMISSIONED
    # STORE_UNREADABLE — the fail-open this contract exists to close.
    return CommissionState.INVALID_AUTHORITY


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    """One shadow-mode datapoint: what the flip WOULD change, unflipped.

    Shadow mode is mandatory here rather than stylistic. This gate decides
    whether the whole managed surface answers, so a wrong verdict is a total
    outage, not a narrow denial. So: observe, publish the mismatch rate,
    and flip only on zero unexplained mismatches (#440's own phased plan).

    Every field is a bool / str / enum by contract — an observation is a
    verdict SHAPE, never a store dump. No path, row, token or seed rides
    in an audit record.
    """

    #: The verdict production returned. Shadow mode NEVER alters this.
    in_force_verdict: bool
    #: What the read observed.
    outcome: CommissionReadOutcome
    #: The tri-state the carrier now derives from that outcome.
    commission_state: CommissionState
    #: The verdict enforcement WOULD return once the flip lands.
    would_be_verdict: bool
    #: True iff the flip changes this call's answer.
    enforcement_would_change: bool
    #: Named cause, always non-empty. A mismatch with no reason is unauditable.
    reason: str


def observe_commission_shadow(
    *,
    legacy_verdict: bool,
    outcome: CommissionReadOutcome,
) -> ShadowObservation:
    """Build the shadow record for one commission read. Pure; records nothing.

    The caller owns the sink (audit row / counter). This function owns the
    QUESTION: given what the store did, does today's boolean disagree with
    the contract's tri-state?

    WHAT THE FLIP CHANGES — stated here so it is never a surprise:
    exactly one case moves. When the store is UNREADABLE, today's verdict is
    ``False`` (ungoverned, gate off, fail-OPEN) and the contract's verdict is
    ``True`` (governed, gate on, governed work denied with
    ``project_authority_invalid``, repair path open). Every outcome whose
    store ANSWERS is bit-identical before and after, which is the property
    that makes the flip a tightening rather than an outage — pinned by
    tests/governance/test_commission_carrier.py.
    """
    state = classify_commission_read(outcome)
    # Under the contract, "governed" is the non-UNCOMMISSIONED set: a
    # commissioned project and a corrupt-authority project are BOTH governed.
    # This mirrors evaluate_authorization's own ``governed`` derivation.
    would_be = state is not CommissionState.UNCOMMISSIONED
    changed = would_be != legacy_verdict
    if changed:
        reason = (
            f"shadow_mismatch: outcome={outcome.value} "
            f"state={state.value} in_force={legacy_verdict} would_be={would_be}"
        )
    else:
        reason = f"shadow_agreement: outcome={outcome.value} state={state.value}"
    return ShadowObservation(
        in_force_verdict=legacy_verdict,
        outcome=outcome,
        commission_state=state,
        would_be_verdict=would_be,
        enforcement_would_change=changed,
        reason=reason,
    )
