"""Backlog admission — the FIRST CONSUMER of the general approval authority.

WHY THIS FILE EXISTS AT ALL. `approval_authority` shipped as a complete two-party
primitive with ten tests and exactly one production reader -- inside a path its
own docstring calls "deliberately unreachable today". Its writers had no caller
but a test, so vulture was RIGHT to fail them: the defect was never "the helpers
are unwired", it was that the first consumer was never built. This is it. Seat
succession became a legitimate SECOND use that arrived before the first.

THE OPERATOR'S TWO LANES, AND THE ONE THIS IS. Verbatim: "user or co-co approval
(add a function for co-co aproval, maybe we'll use it for other stuff too, co-co
aproval goes through xaacp, either freetext or structured text, your choirce,
this is A2A comms, user has a higher level interaction flow". The OPERATOR lane
-- the higher-level human flow -- is NOT built here and must not be. This is the
BETA lane: agent-to-agent over XAACP, requested AUTOMATICALLY by the admission
path itself, because an obligation an agent has to remember to create is an
obligation that does not exist.

=== GATE ADMISSION, DO NOT ADD STATE ===

There is NO `proposed` status, and that is a measured decision, not an omission.
`project_backlog_store.add()` writes the status into the sync event,
`hydrate_from_events` trusts it and re-INSERTs it, and hydrate runs
FOLD-ON-READ inside `list_backlog`. So an unknown status value makes EVERY
backlog read throw -- the ledger stops answering entirely. The `status` CHECK
constraint is spelled three times and changing it needs a full table rebuild
under the #242 crash window, and `BacklogTodoPage.tsx` renders an unknown status
as the FIRST option, so the operator would see a pending item labelled OPEN.

An unapproved add is therefore REFUSED, not stored-as-untrusted. The operator's
words are that an item "must be APPROVED before it enters the trusted ledger":
nothing untrusted enters at all, every existing count and listing surface stays
correct, and no migration is needed.

=== THE SEVEN WAYS THIS COULD HAVE BEEN THEATRE, AND WHERE EACH IS KILLED ===

1. PAYLOAD BINDING. The approval's subject_id IS a digest of the exact payload
   being admitted (`admission_subject_id`). Approve proposal A, mutate content,
   tags, priority, kind, difficulty or status, and the retry hashes to a
   DIFFERENT subject -- for which no approval exists. A bound approval cannot be
   spent on a different row.
2. ACTOR BINDING. `_require_beta_verdict` re-resolves BETA from the durable seat
   authority AT CONSUMPTION TIME and demands that the recorded decider be that
   actor. An ALPHA verdict, a spawned child's verdict, or an unmapped actor's
   verdict is a verdict from the wrong principal and admits nothing.
3. SCOPE BINDING. Project root, session and purpose are all INSIDE the digest,
   and the requester identity is compared against the row. A different project,
   session, item or purpose hashes elsewhere or fails the comparison.
4. LIFECYCLE. Only `state == 'approved'` admits. Pending, rejected, blocked and
   expired each refuse by name, and `expire_due_approvals` is called FIRST so an
   aged-out obligation is recorded as expired rather than read as merely pending.
5. NO SILENT TRUST. If no BETA route can be resolved, the add is REFUSED with
   `beta_route_unavailable`. It never degrades to "admit it anyway" -- an
   unreachable approver is the exact case where fail-open would be invisible.
6. CROSS-DOMAIN ISOLATION. The purpose is `backlog_admission`, and the seat path
   uses `xaacp_seat`. `approval_authority.is_approved` keys every read on the
   purpose and refuses a kind it cannot shape-check, so neither domain's verdict
   can ever answer the other's question. This is what makes one general store
   safe to share.
7. PRODUCTION CALL GRAPH. `request_approval`, `decide_approval` and
   `expire_due_approvals` are each reached from this module, which is reached
   from `ai_backlog`. The door and the consumer are both production.

=== THE HOLE THIS DOES NOT CLOSE, NAMED RATHER THAN LEFT TO BE DISCOVERED ===

`ai_backlog(mode='update')` IS STILL UNGATED, and it can defeat this gate in two
calls. `_BACKLOG_TASK_GATED_MODES = {remove, merge, unmerge}`
(`server_todo_backlog_tools.py`) deliberately omits `update` -- attribution is "a
FACT RECORDED, not a PRECONDITION" -- and nothing else stands between an agent and
an arbitrary `status` or `content` on an existing row. So:

    1. get ONE honest payload approved; it lands as row #N
    2. ai_backlog(mode='update', id=N, content='<anything at all>')

`content=` REPLACES the whole body and RE-DERIVES the title, with no history and
no undo, so the admitted row becomes a row nobody approved. The same call can set
any `status`, which #1055's own seam map already records as a LIVE defect
independent of this gate: "today an agent can reopen anything the operator closed,
or set any status on any row."

IT IS NOT CLOSED HERE, AND THAT IS A DECISION, NOT AN OVERSIGHT. Gating `update`
is a SECOND consumer with its own shape -- an update's approval subject is
(row id, new payload), not (new payload) -- and the operator's ruling about add
said nothing about it. A wrong gate on the mutation path is worse than a named
hole: it would make every status correction, every appended dated section and
every stale-title fix need a consul, which is how agents learn to route around
the mechanism entirely. The gate on ADMISSION is what was asked for and what is
built; the mutation path is its own ruling to make.

What this gate DOES still buy with the hole open: a brand-new row cannot appear
without a verdict, so the ledger's growth is approved even though an existing
row's content is not yet immutable. Step 1 above is a real cost to an attacker
(it needs a genuine approval from the other consul), and every update is a
separate, attributable act on an identified row rather than an anonymous new one.

=== IDENTITY IS DERIVED, NEVER CLAIMED ===

No public function here takes a parameter naming the requester or the decider.
The requester comes from `xaacp_resolve_caller_route` (inside
`approval_authority`), and the decider comes from the same resolver on the other
side. `resolve_beta_approver` resolves the APPROVER SEAT -- a role, not a claim
-- through the durable seat authority, so a caller cannot nominate its own
friend. `test_backlog_admission_1055.py` pins that with `inspect.signature`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from . import approval_authority, conductor_comms, project_backlog_store

logger = logging.getLogger("aidocs.backlog_admission")

# THE PURPOSE. Distinct from `conductor_comms.XAACP_SEAT_SUCCESSION_SUBJECT_KIND`
# ('xaacp_seat') on purpose and by test: an approval minted for one purpose must
# never satisfy the other, and `subject_kind` is the field every read is keyed on.
BACKLOG_ADMISSION_PURPOSE = "backlog_admission"

# WHAT THE APPROVAL IS FOR, carried in the XAACP envelope's `requested_action`.
ADMISSION_ACTION = "admit_to_trusted_ledger"

# THE APPROVING PRINCIPAL, AS POLICY -- a ROLE, never a hard-coded actor id.
# BETA/r0b is a SEAT: whoever holds it answers, and when nobody holds it the
# admission is refused rather than admitted. This is the one place the principal
# is named, and it is named as data/policy exactly as r0b's ruling (iii) requires.
BETA_APPROVER_ROLE = "co_conductor"

# NOTE: a `DEFAULT_PRIORITY` constant lived here and is deliberately GONE.
# It was the first fix for a real defect -- the add path defaulted `priority` to
# "normal" while the diagnosis door saw only what the caller typed, so the two
# digests differed and the obligation a refusal NAMED was invisible to the door
# it named. The second pass superseded it properly: normalization now lives in
# `project_backlog_store.canonical_add_payload`, which `add()` itself calls and
# whose OUTPUT the digest hashes. So there is no default to restate here, and
# restating one would re-create the rival definition the extraction removed.
# Keeping the constant "for documentation" is how a second source of truth is
# born; the store's normalizer is the one home. (Found by gate 1d, which is the
# same structural law that catches coded-but-unwired anywhere else.)

# The tiers that admit as they do today. Taken from the SAME authority
# `tool_gate_service._resolve_caller_role_names` reads (authenticated uid ->
# RBAC roles), which fails closed to () -- so an unauthenticated or unresolvable
# caller is NOT an admin. `_caller_has_admin_hint_authority` is deliberately not
# reused: its docstring calls itself presentation-only, and a verdict must not be
# read out of a function that promises it never decides anything.
ADMIN_ADMISSION_ROLES: frozenset[str] = frozenset(
    {"super_admin", "superadmin", "admin"}
)

# The refusal's named reasons. Spelled once so the refusal text, the structured
# status and the tests cannot drift apart.
REFUSAL_BETA_UNAVAILABLE = "beta_route_unavailable"
REFUSAL_AWAITING = "awaiting_beta_approval"
REFUSAL_REJECTED = "beta_rejected"
REFUSAL_BLOCKED = "beta_blocked"
REFUSAL_EXPIRED = "beta_approval_expired"
REFUSAL_WRONG_ACTOR = "approval_from_wrong_actor"
REFUSAL_UNIDENTIFIED = "caller_unidentified"
REFUSAL_INVALID_PAYLOAD = "invalid_payload"
REFUSAL_IN_FLIGHT = "admission_in_flight"

# THE TWO DOORS, spelled once (sealed diagnosis/discharge law). A refusal that
# names an obligation without naming how to SEE it and how to DISCHARGE it is
# half a remedy, and a remedy with no door is the defect this project keeps
# paying for. Both doors are modes of the tool the caller is already holding.
DIAGNOSIS_DOOR = "ai_backlog(mode='approval_status')"
DISCHARGE_DOOR = (
    "ai_backlog(mode='approve', approval_id='{approval_id}', "
    "decision='accepted'|'rejected', reason='<why>')"
)


def canonical_admission_payload(
    *,
    content: str,
    priority: str = "",
    kind: str = "",
    difficulty: Any = 0,
    tags: Any = None,
    status: str = "",
) -> tuple[dict | None, str | None]:
    """The add payload AS THE STORE WILL WRITE IT, or the store's own refusal.

    DELEGATED, NOT REIMPLEMENTED. `project_backlog_store.canonical_add_payload`
    is the same function `add()` itself calls, so the bytes this digest covers
    are provably the bytes that land. The translation layer here is only the
    tool-boundary SENTINEL convention -- `''` and `0` mean "not passed", which
    the store spells as `None` -- and nothing else, because anything else done
    here would be a second normalizer and the whole point is that there is one.
    """
    return project_backlog_store.canonical_add_payload(
        content=content,
        # The store's own default. Passed through as its default rather than
        # pre-applied here, so a change to it cannot leave the digest behind.
        priority=(priority or "normal"),
        kind=(kind or None),
        difficulty=(difficulty or None),
        tags=tags,
        status=(status or None),
    )


def admission_subject_id(
    *,
    project_root: Path,
    session_id: str,
    canonical: dict,
) -> str:
    """The approval subject: a digest of the exact row the add will write.

    THIS IS THE PAYLOAD BINDING, and it is the survivor a naive "is there an
    approval for this agent" check leaves alive. Without it an agent gets one
    honest proposal approved and then admits anything at all under that verdict
    -- precisely the "fake, bullshit, no-op, untrustable" row the ruling exists
    to stop, now wearing an audit trail that says "approved".

    IT DIGESTS THE POST-NORMALIZATION PAYLOAD, WHICH IS NOT A DETAIL. The store
    rewrites `content` through the #684 prose screen, folds `priority` aliases
    ('medium' -> 'normal'), turns a missing `kind` into `KIND_UNSET`, and
    preserves tag ORDER AND DUPLICATES verbatim. A digest over raw caller text
    would therefore approve a preimage that differs from the row that lands --
    the approved item and the admitted item would be two different things, which
    is the failure this whole mechanism exists to prevent. So `canonical` comes
    from `canonical_admission_payload`, i.e. from the store's own normalizer, and
    this function adds only SCOPE: which project, which session, which purpose.

    A NEW FIELD ON THE ADD PATH IS COVERED AUTOMATICALLY, because the canonical
    payload is whatever the store says the row is -- not a list maintained here
    that could silently fall behind. That is the structural reason this is keyed
    on the store's output and not on an explicit field list.
    """
    payload = {
        "purpose": BACKLOG_ADMISSION_PURPOSE,
        "project": str(Path(project_root).resolve()).replace("\\", "/").lower(),
        "session_id": str(session_id or "").strip(),
        "row": canonical,
    }
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def caller_roles(project_root: Path) -> tuple[str, ...]:
    """The caller's RBAC roles, from the one authority, failing closed to ().

    Delegated rather than re-derived: a second copy of "who is an admin" is the
    rival-definition breach this project keeps paying for, and this one is the
    resolver the gate surface itself reads.
    """
    try:
        from .tool_gate_service import _resolve_caller_role_names

        return tuple(_resolve_caller_role_names(project_root))
    except Exception:
        logger.exception("admission role resolution failed; treating as NON-admin")
        return ()


def gate_principal_is_operator() -> bool:
    """Is this call arriving through the outer gate as an ORG ADMIN?

    THE REGRESSION THIS EXISTS TO PREVENT, traced at source. `caller_roles` reads
    `tool_gate_service._resolve_caller_role_names`, which reads
    `project_authority._authenticated_uid`, whose own docstring enumerates the
    ONLY three credentials it accepts: an `AIDOCS_OPERATOR_TOKEN` bearer token,
    an APPROVED host-session binding, or the machine-level password login. It
    deliberately does NOT consult the gate-resolved OAuth principal -- and
    `outer_gate.py` says why in the other direction: the identity_resolver
    fallback "is blind to the OAuth principal on the remote gate".

    So an operator arriving over the remote gate / WebMCP resolves to NO uid,
    hence NO roles, hence -- without this -- "not an admin", and WOULD HAVE BEEN
    GATED ON THEIR OWN BACKLOG by the mechanism they ordered. That is not a
    hypothetical: `_BACKLOG_WRITE_MODES` in `outer_gate.py` already admits their
    `add` on `tier_m_edit` scope, so they would pass the gate and then be refused
    underneath it, by a check that had silently reclassified them as an agent.

    The fix is to ask the authority that CAN see them. `current_gate_principal()`
    is the gate-resolved principal for the current request, and `is_org_admin` is
    the single resolver ~20 gate call sites already use for exactly this
    question. Outside a gate dispatch the principal is None and this is simply
    False -- it adds a rung, it never removes one.
    """
    try:
        from .mcp_server_runtime_helpers import current_gate_principal
        from .outer_gate_project_acl import is_org_admin

        principal = current_gate_principal()
        if not principal:
            return False
        return bool(is_org_admin(principal))
    except Exception:
        logger.exception("gate principal authority read failed; no operator rung")
        return False


def caller_admits_unapproved(project_root: Path) -> bool:
    """May this caller admit to the trusted ledger on its own word?

    Admin/operator tier admits as it does today -- the 2026-07-29 ruling that
    left recording ungated is preserved for exactly the caller the gate refusals
    point at `ai_backlog`. `tool_gate_service.false_positive_affordance` already
    routes NON-admins to `ai_issues` instead, so the named remedy a normal agent
    is handed was never this tool and nothing downstream of it breaks.

    TWO AUTHORITIES, BECAUSE THERE ARE TWO ARRIVAL PATHS. The local credential
    ladder cannot see a remote OAuth principal and the gate principal does not
    exist for a local stdio caller; asking only one of them would gate whichever
    operator came the other way. Neither is a fallback for the other -- both are
    positive proofs, and the absence of both is handled by
    `admission_authority_is_determinable`, NOT by assuming "agent".
    """
    if gate_principal_is_operator():
        return True
    return any(role in ADMIN_ADMISSION_ROLES for role in caller_roles(project_root))


def resolve_beta_approver(project_root: Path, session_id: str) -> dict[str, str]:
    """The actor holding the BETA seat on this session, or {} -- never a guess.

    Resolved through `conductor_comms._xaacp_resolve_target_route`, the ONE
    resolver XAACP already uses for role-name addressing. That matters three
    times: it refuses a role with no registered seat (so a phantom approver is
    impossible), it refuses an AMBIGUOUS role, and it grades the route on the
    same liveness truth the directory shows -- including failing closed when
    liveness itself cannot be read. A seat nobody holds resolves to {}, and {}
    becomes a REFUSAL upstream, never an admission.
    """
    try:
        route = conductor_comms._xaacp_resolve_target_route(
            project_root,
            session_id=str(session_id or "").strip(),
            target_actor_id=BETA_APPROVER_ROLE,
            lane_id="",
        )
    except Exception:
        logger.exception("BETA approver resolution failed; no route")
        return {}
    actor_id = str((route or {}).get("actor_id") or "").strip()
    role = str((route or {}).get("role") or "").strip().lower()
    # THE ROLE MUST COME BACK AS BETA. That resolver also answers actor-id
    # lookups, so accepting any non-empty route here would let a seatless agent
    # that happened to resolve stand in for the approving principal.
    if not actor_id or role != BETA_APPROVER_ROLE:
        return {}
    return {"actor_id": actor_id, "role": role}


def _standing(project_root: Path, subject_id: str) -> dict:
    """The subject's approval record, purpose-scoped, after explicit expiry.

    `expire_due_approvals` runs FIRST and is the production caller of that
    writer. It is not bookkeeping: an obligation that aged out must read as
    `expired` with an audit row, because "nobody ruled in time" and "nobody ever
    asked" are different facts and a refusal that confuses them sends the agent
    to the wrong door.
    """
    try:
        approval_authority.expire_due_approvals(project_root)
    except Exception:
        logger.exception("approval expiry sweep failed; reading standing as-is")
    return approval_authority.approval_status(
        project_root,
        subject_kind=BACKLOG_ADMISSION_PURPOSE,
        subject_id=subject_id,
    )


def _refuse(status: str, error: str, **extra: Any) -> dict:
    return {"ok": False, "status": status, "error": error, **extra}


def _bounded_obligation_line(record: dict) -> str:
    """Name THIS obligation and nothing else.

    The refusal must be BOUNDED: dumping every pending approval turns a precise
    refusal into a report the reader skims, and the one id that matters is the
    one they would then fail to find.
    """
    approval_id = str(record.get("approval_id") or "")
    return (
        f"approval_id={approval_id} "
        f"[{BACKLOG_ADMISSION_PURPOSE}:{record.get('subject_id')}] "
        f"state={record.get('state')}"
    )


def _doors(record: dict) -> str:
    approval_id = str(record.get("approval_id") or "")
    return (
        f"DIAGNOSIS: {DIAGNOSIS_DOOR} to see this obligation's state. "
        f"DISCHARGE: {DISCHARGE_DOOR.format(approval_id=approval_id)} "
        f"— only the BETA/{BETA_APPROVER_ROLE} seat may rule, and it cannot be "
        "the actor that asked."
    )


def evaluate_backlog_admission(
    project_root: Path,
    *,
    content: str,
    priority: str = "",
    kind: str = "",
    difficulty: Any = 0,
    tags: Any = None,
    status: str = "",
    session_id: str = "",
) -> dict:
    """May THIS EXACT payload enter the trusted ledger, and if not, ask BETA.

    Returns `{"ok": True, ...}` when the add may proceed, and a refusal carrying
    `status` + `error` otherwise. The refusal is the whole product: it names the
    obligation, both doors, and what would discharge it.

    THE OBLIGATION IS CREATED BY THIS PATH, NOT BY AN AGENT REMEMBERING TO ASK
    (operator, today: "the r0b approval function should run automatically on
    backlog via xaacp"). So the first refused add is also the request.
    """
    # THE PAYLOAD IS CANONICALIZED AND VALIDATED FIRST, by the store's own
    # normalizer. Two reasons, both load-bearing:
    #   * the digest must cover the bytes that LAND (see `admission_subject_id`);
    #   * a malformed payload must be refused with the STORE's precise diagnosis
    #     (e.g. #818's "status is not settable on add — use mode='update'") and
    #     never masked by a generic admission refusal. A gate that hides a better
    #     diagnosis downstream of it has made the system less answerable, and it
    #     would also spend a Consul's attention on a typo.
    canonical, payload_err = canonical_admission_payload(
        content=content,
        priority=priority,
        kind=kind,
        difficulty=difficulty,
        tags=tags,
        status=status,
    )
    if payload_err is not None or canonical is None:
        return _refuse(
            REFUSAL_INVALID_PAYLOAD,
            payload_err or "invalid backlog payload",
        )

    caller = approval_authority._caller_actor(project_root)
    caller_actor_id = str(caller.get("actor_id") or "") if caller else ""
    effective_session = (
        str(session_id or "").strip()
        or str((caller or {}).get("session_id") or "").strip()
    )

    subject_id = admission_subject_id(
        project_root=project_root,
        session_id=effective_session,
        canonical=canonical,
    )

    # ADMIN TIER FIRST, and on purpose: an operator-tier add must not pay for a
    # BETA route that may not exist, and the 2026-07-29 ruling that keeps the
    # named remedy reachable applies to exactly this caller.
    if caller_admits_unapproved(project_root):
        return {
            "ok": True,
            "admitted_by": "admin_tier",
            "subject_id": subject_id,
            "roles": list(caller_roles(project_root)),
        }

    if not caller_actor_id:
        # UNDETERMINABLE IS ITS OWN ANSWER, AND IT IS NOT "NON-ADMIN".
        #
        # Three authority surfaces have now been consulted and all three
        # declined: the gate-resolved OAuth principal (absent -> not a remote
        # operator), the local credential ladder (no uid -> no RBAC roles), and
        # the XAACP route (no actor -> not a derivable agent either). Treating
        # that as "an agent, then" would be the same laundering this gate exists
        # to stop, just pointing the other way: unknown silently promoted into a
        # rung. `scratch/drt16_permission_inventory.md` records `()` from the role
        # resolver as one of THREE SPELLINGS OF "NO RUNG" in this codebase, which
        # is precisely why an empty role set must not be read as a classification.
        #
        # So this refusal says what actually happened. It also cannot be reached
        # by a normal agent: an agent HAS a derived XAACP actor, which is what
        # makes it classifiable as one.
        return _refuse(
            REFUSAL_UNIDENTIFIED,
            "backlog admission refused: this caller's ADMISSION AUTHORITY COULD "
            "NOT BE DETERMINED — not proven operator, and not a derivable agent "
            "either. You have NOT been classified as a non-admin; you have not "
            "been classified at all, and an unknown is never laundered into a "
            "rung in either direction. Concretely: no gate-resolved operator "
            "principal, no authenticated local credential, and no XAACP actor "
            "route — so this add cannot be bound to a requester, and an approval "
            "that cannot be bound to a requester is one anyone could spend. "
            "Identity is DERIVED and never claimed, so there is no parameter to "
            "supply it. Bind this host with ai_session(mode='connect') and "
            "retry; ai_whoami reports which of the three surfaces can see you. "
            "Or file the observation with ai_issues(mode='file'), which is "
            "deliberately role-free, task-free and approval-free.",
            subject_id=subject_id,
        )

    record = _standing(project_root, subject_id)
    state = str(record.get("state") or "unknown")

    if state == approval_authority.STATE_APPROVED:
        verdict = _require_beta_verdict(
            project_root,
            record=record,
            caller_actor_id=caller_actor_id,
            session_id=effective_session,
        )
        if verdict is not None:
            return verdict

        # ── EXACTLY ONCE, SETTLED BY THE STORE AND NOT BY THE CALLER ──
        # An approval that stays spendable turns a retry into a second trusted
        # row; an approval burnt on read leaves the caller holding a REFUSAL for
        # work that landed (the row commits, then the response dies on the way
        # back). The authoritative record decides which of the three situations
        # this is, because the caller's memory provably cannot.
        claim = approval_authority.claim_for_consumption(
            project_root, approval_id=str(record.get("approval_id") or "")
        )
        if claim.get("status") == "already_consumed":
            # THE ANSWER TO "ROW LANDED, RESPONSE DIED, CALLER RETRIED". Not a
            # denial -- the work exists, and this names it -- and not a second
            # row either. The retry is told what its earlier attempt produced.
            return {
                "ok": True,
                "admitted_by": "beta_approval",
                "already_admitted": True,
                "backlog_id": claim.get("consumed_ref"),
                "approval_id": record.get("approval_id"),
                "subject_id": subject_id,
                "decided_by_actor_id": record.get("decided_by_actor_id"),
            }
        if not claim.get("ok"):
            if claim.get("status") == "consumption_in_flight":
                return _refuse(
                    REFUSAL_IN_FLIGHT,
                    "backlog admission refused: another caller is MID-ADMISSION "
                    "on this exact approved payload and holds the consumption "
                    "claim, so proceeding would turn one approval into two "
                    "trusted rows. This is a refusal to DUPLICATE, not a refusal "
                    "of the work: re-read with "
                    f"{DIAGNOSIS_DOOR} — once the other caller finishes, the "
                    "approval reports the row it produced and a retry returns "
                    "that same row instead of adding another. "
                    f"{_bounded_obligation_line(record)}",
                    approval_id=record.get("approval_id"),
                    subject_id=subject_id,
                )
            return _refuse(
                REFUSAL_WRONG_ACTOR,
                "backlog admission refused: the approval could not be claimed "
                f"for consumption ({claim.get('status')}). An approval that "
                "cannot be claimed exactly once is not spent at all — nothing "
                "was admitted. "
                f"{_bounded_obligation_line(record)}. {_doors(record)}",
                approval_id=record.get("approval_id"),
                subject_id=subject_id,
            )
        return {
            "ok": True,
            "admitted_by": "beta_approval",
            "already_admitted": False,
            # THE CALLER MUST NOW EITHER record_admitted_row OR
            # release_admission_claim. Holding a claim and doing neither is the
            # one state that blocks a retry, and it is bounded rather than
            # permanent (CLAIM_STALE_SECONDS) precisely so a crashed consumer
            # cannot strand an approval forever.
            "consumption_claimed": True,
            "approval_id": record.get("approval_id"),
            "subject_id": subject_id,
            "decided_by_actor_id": record.get("decided_by_actor_id"),
        }

    if state in ("rejected", "blocked"):
        named = REFUSAL_REJECTED if state == "rejected" else REFUSAL_BLOCKED
        return _refuse(
            named,
            f"backlog admission refused: BETA ruled {state} on this exact "
            f"payload. {_bounded_obligation_line(record)}. BETA's rationale: "
            f"{record.get('rationale') or '(none recorded)'}. A verdict is "
            "ONE-SHOT and is never overturned by re-asking: change the payload "
            "(which opens a new obligation, because the approval is bound to a "
            "digest of the content, tags, priority, kind, difficulty and status) "
            "or file it with ai_issues(mode='file') instead. " + _doors(record),
            approval_id=record.get("approval_id"),
            subject_id=subject_id,
        )

    if state == approval_authority.STATE_EXPIRED:
        # AN AGED-OUT OBLIGATION IS NOT A FRESH ONE, and it is not approval
        # either. Re-requesting is allowed precisely because nobody ruled.
        requested = _request_beta_approval(
            project_root,
            subject_id=subject_id,
            session_id=effective_session,
            content=content,
        )
        if not requested.get("ok"):
            return requested
        return _refuse(
            REFUSAL_EXPIRED,
            "backlog admission refused: the previous approval obligation aged "
            "out with nobody ruling on it, which is recorded as EXPIRED and is "
            "not a yes. A fresh obligation has been dispatched to the BETA/"
            f"{BETA_APPROVER_ROLE} seat over XAACP. "
            f"{_bounded_obligation_line(requested['record'])}. "
            + _doors(requested["record"]),
            approval_id=requested["record"].get("approval_id"),
            subject_id=subject_id,
        )

    # UNKNOWN or PENDING: create-or-find the obligation and refuse. Creating it
    # is idempotent at the database level (a partial unique index), so a retry
    # finds the same obligation and does NOT re-announce it.
    requested = _request_beta_approval(
        project_root,
        subject_id=subject_id,
        session_id=effective_session,
        content=content,
    )
    if not requested.get("ok"):
        return requested
    record = requested["record"]
    return _refuse(
        REFUSAL_AWAITING,
        "backlog admission refused: an item enters the trusted ledger only once "
        f"the BETA/{BETA_APPROVER_ROLE} seat has approved THIS EXACT payload "
        "(Empire ruling 2026-09-10, backlog #1055 — the ledger lost its "
        "authority because agent assertions were admitted as facts). The "
        "obligation has been created and dispatched over XAACP automatically; "
        "you do not have to ask for it. "
        f"{_bounded_obligation_line(record)}. Retry the IDENTICAL add once it is "
        "approved — the approval is bound to a digest of content, tags, "
        "priority, kind, difficulty and status, so any edit voids it and opens a "
        "new obligation. Nothing is stored in the meantime: an unapproved item "
        "is refused, never parked. " + _doors(record),
        approval_id=record.get("approval_id"),
        subject_id=subject_id,
    )


def record_admitted_row(project_root: Path, *, approval_id: str, backlog_id: Any) -> dict:
    """Close the loop: this approval produced THIS backlog row. Call immediately.

    "Immediately" is the whole contract. It must run after the store returns and
    BEFORE the tool's response is built, because the failure this defends against
    is the response never arriving -- so the record has to exist by the time the
    caller could possibly retry. Conditional on `consumed_ref IS NULL`, so the
    first row an approval produced stays the row it produced.
    """
    if not str(approval_id or "").strip():
        return {"ok": False, "status": "invalid"}
    return approval_authority.record_consumption(
        project_root,
        approval_id=str(approval_id).strip(),
        consumed_ref=str(backlog_id),
    )


def release_admission_claim(project_root: Path, *, approval_id: str) -> dict:
    """Give the claim back when the row did NOT land, so a retry is not blocked.

    The honest other half of a claim. Without it, an add that fails AFTER the
    claim leaves the approval looking busy for the whole stale window, and the
    agent is refused for a reason that is no longer true.
    """
    if not str(approval_id or "").strip():
        return {"ok": False, "status": "invalid"}
    return approval_authority.release_consumption_claim(
        project_root, approval_id=str(approval_id).strip()
    )


def _request_beta_approval(
    project_root: Path,
    *,
    subject_id: str,
    session_id: str,
    content: str,
) -> dict:
    """Create-or-find the obligation and dispatch it to BETA over XAACP.

    Returns `{"ok": True, "record": <approval row>}` or a REFUSAL. There is
    deliberately no third outcome: if the obligation cannot be created or cannot
    be delivered, the add is refused by name. That is survivor 5 -- "no BETA
    route" must never silently produce a trusted admission.
    """
    beta = resolve_beta_approver(project_root, session_id)
    if not beta:
        return _refuse(
            REFUSAL_BETA_UNAVAILABLE,
            "backlog admission refused: no BETA/"
            f"{BETA_APPROVER_ROLE} seat could be resolved for session "
            f"{session_id!r}, so the approval this add requires cannot be "
            "requested, let alone granted. THIS IS NOT A PASS: an unreachable "
            "approver leaves the item UNAPPROVED, because absence of a no is not "
            "a yes. Either the seat is unoccupied, it is occupied by an actor "
            "the liveness audit cannot prove, or the liveness audit itself could "
            "not be read — ai_seat(mode='status') and ai_whoami show which. "
            "Meanwhile ai_issues(mode='file') records the observation immutably "
            "with no role and no approval, which is where a normal agent's "
            "findings are supposed to go.",
            subject_id=subject_id,
        )

    first_line = str(content or "").strip().splitlines()
    rationale = (
        "A non-admin actor asks to admit a new item to the trusted backlog "
        "ledger. Rule on the CONTENT, not on the asking: the ruling this gate "
        "implements exists because fake, no-op and untrustable rows were "
        "admitted on an agent's own word. First line of the proposed item: "
        f"{first_line[0][:200]!r}"
        if first_line
        else "A non-admin actor asks to admit a new backlog item."
    )
    requested = approval_authority.request_approval(
        project_root,
        subject_kind=BACKLOG_ADMISSION_PURPOSE,
        subject_id=subject_id,
        approver_actor_id=beta["actor_id"],
        requested_action=ADMISSION_ACTION,
        rationale=rationale,
    )
    if requested.get("ok"):
        return {"ok": True, "record": requested}

    # REQUEST FAILED. Every branch here is a refusal; none is an admission.
    status = str(requested.get("status") or "request_failed")
    if status == "self_approval_refused":
        # The caller IS the BETA seat. A seat that holds the approving principal
        # does not need a second party for its own row -- but it must not be
        # silently admitted here either, because that decision belongs to the
        # tier check above, not to a fallback inside a failed request.
        return _refuse(
            REFUSAL_WRONG_ACTOR,
            "backlog admission refused: this caller resolves to the BETA/"
            f"{BETA_APPROVER_ROLE} seat itself, and the requesting actor can "
            "never be the deciding actor — self-approval is what would make the "
            "whole ruling theatre. Ask the other consul, or add under an "
            "operator-tier identity.",
            subject_id=subject_id,
        )
    if status == "unreachable_approver":
        return _refuse(
            REFUSAL_BETA_UNAVAILABLE,
            "backlog admission refused: the BETA/"
            f"{BETA_APPROVER_ROLE} seat resolved but the XAACP dispatch of the "
            f"approval obligation failed ({requested.get('error')!r}). No "
            "obligation was left behind and nothing was admitted: a request "
            "nobody will ever be notified about would read as 'awaiting "
            "approval' forever. Retry, or use ai_issues(mode='file').",
            subject_id=subject_id,
        )
    return _refuse(
        REFUSAL_AWAITING,
        "backlog admission refused: the approval obligation this add requires "
        f"could not be opened ({status}: {requested.get('error')!r}), so the "
        "item stays out of the trusted ledger. Nothing was admitted. "
        f"DIAGNOSIS: {DIAGNOSIS_DOOR}. Or record it with "
        "ai_issues(mode='file'), which needs no approval.",
        subject_id=subject_id,
    )


def _require_beta_verdict(
    project_root: Path,
    *,
    record: dict,
    caller_actor_id: str,
    session_id: str,
) -> dict | None:
    """Is this standing `approved` row REALLY BETA's verdict, on THIS caller's ask?

    Returns None when the verdict holds, or a refusal when it does not. This is
    survivors 2 and 3, and it is the check a "state == approved" test would miss:
    the store is general and shared, so a row reaching `approved` proves only
    that SOME two parties agreed about SOME subject. It must additionally be
    proven that the decider is the BETA seat as resolved NOW, that the requester
    is this caller, and that the scope is this session.
    """
    approval_id = record.get("approval_id")
    if str(record.get("subject_kind") or "") != BACKLOG_ADMISSION_PURPOSE:
        # Belt and braces over `approval_status`'s own keying: a purpose
        # mismatch here would mean a seat approval had answered a backlog
        # question, which is the one failure that makes a shared store unsafe.
        return _refuse(
            REFUSAL_WRONG_ACTOR,
            "backlog admission refused: the standing approval is for purpose "
            f"{record.get('subject_kind')!r}, not {BACKLOG_ADMISSION_PURPOSE!r}. "
            "An approval is purpose-scoped and cannot be spent in another "
            "domain.",
            approval_id=approval_id,
        )
    if str(record.get("requester_actor_id") or "") != caller_actor_id:
        return _refuse(
            REFUSAL_WRONG_ACTOR,
            "backlog admission refused: the standing approval was requested by "
            f"{record.get('requester_actor_id')!r}, not by this caller. An "
            "approval is bound to the actor that asked for it; it is not a "
            "bearer token another agent may spend. "
            f"{_bounded_obligation_line(record)}. {_doors(record)}",
            approval_id=approval_id,
        )
    if str(record.get("session_id") or "") != str(session_id or "").strip():
        return _refuse(
            REFUSAL_WRONG_ACTOR,
            "backlog admission refused: the standing approval is scoped to "
            f"session {record.get('session_id')!r}, not {session_id!r}. Scope "
            "mismatch is a refusal, not a detail.",
            approval_id=approval_id,
        )
    # ── THE ROLE AT DECISION TIME, READ FROM THE RECORD AND NOT RE-DERIVED ──
    #
    # The obvious implementation -- resolve BETA now, require the recorded
    # decider to equal that actor -- is WRONG, and measurably so. Seats succeed.
    # `ai_seat(mode='succeed')` hands BETA to a different actor id, and under
    # that rule every approval the PREVIOUS BETA had properly rendered would
    # silently stop admitting: a verdict that evaporates because the host
    # conversation changed. The operator's own ruling is that succession must not
    # invalidate work, and a ChatGPT conversation identity is exactly the
    # transient thing that must not become permanent authority.
    #
    # The inverse error is just as real: caching the approver's actor id as
    # authority would let a DISPLACED BETA keep deciding. So authority is
    # resolved LIVE at DECISION TIME (`decide_backlog_admission` proves the
    # decider holds the seat before the verdict is recorded) and the result is
    # STAMPED (`decided_by_role`). Consumption then reads the stamp. Both halves
    # hold: a pending obligation survives succession because the new holder can
    # decide it, and a displaced holder cannot, because the live check happens
    # when they try to rule -- not when someone later reads the result.
    decided_by_role = str(record.get("decided_by_role") or "")
    if decided_by_role != BETA_APPROVER_ROLE:
        decided_by = str(record.get("decided_by_actor_id") or "")
        return _refuse(
            REFUSAL_WRONG_ACTOR,
            "backlog admission refused: the verdict was recorded by "
            f"{decided_by!r} holding role {decided_by_role or '(none)'!r}, not "
            f"the BETA/{BETA_APPROVER_ROLE} seat. An approval from ALPHA, from a "
            "spawned child agent, or from any actor that did not hold the "
            "approving seat AT THE MOMENT IT RULED authorizes nothing — and a "
            "role that was never recorded is not a role that was held.",
            approval_id=approval_id,
        )
    return None


def decide_backlog_admission(
    project_root: Path,
    *,
    approval_id: str,
    decision: str,
    body: str = "",
) -> dict:
    """BETA's door: rule on ONE admission obligation. Identity is DERIVED.

    A thin, purpose-checked pass-through to `approval_authority.decide_approval`
    -- thin on purpose, because every rule worth having (only the addressed
    approver rules, no self-approval, a verdict is one-shot, a bodiless verdict
    is malformed, the verdict IS the XAACP reply) already lives there and a
    second copy would drift. What this adds is the PURPOSE CHECK: this door must
    not become a way to rule on a seat succession, which has its own authority
    ladder and its own refusal.
    """
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return {
            "ok": False,
            "status": "invalid",
            "error": (
                "approval_id is required — get it from the refusal that created "
                f"the obligation, or from {DIAGNOSIS_DOOR}"
            ),
        }
    standing = approval_authority.approval_by_id(project_root, approval_id)
    if not standing.get("ok"):
        return standing
    if str(standing.get("subject_kind") or "") != BACKLOG_ADMISSION_PURPOSE:
        return {
            "ok": False,
            "status": "wrong_purpose",
            "error": (
                f"approval {approval_id!r} is for purpose "
                f"{standing.get('subject_kind')!r}, not "
                f"{BACKLOG_ADMISSION_PURPOSE!r}. This door rules on backlog "
                "admission only — another domain's obligation is ruled on "
                "through that domain's own surface, which may impose authority "
                "checks this one does not."
            ),
            "subject_kind": standing.get("subject_kind"),
        }

    # ── BETA AUTHORITY IS RESOLVED LIVE, HERE, AT DECISION TIME ──
    # The seat is the authority; the actor id on the row is only a routing
    # address, and addresses go stale when a seat succeeds. So the caller is
    # checked against the seat AS IT IS NOW. This is what refuses a DISPLACED
    # BETA (it no longer holds the seat) while letting a NEW BETA rule on an
    # obligation addressed to its predecessor (the rebind below follows the
    # role). Neither behaviour is available if the actor id is treated as
    # permanent authority.
    caller = approval_authority._caller_actor(project_root)
    caller_actor_id = str((caller or {}).get("actor_id") or "")
    if not caller_actor_id:
        return {
            "ok": False,
            "status": "unidentified",
            "error": (
                "the deciding actor could not be derived; a verdict from nobody "
                "is not a verdict"
            ),
        }
    session_id = str(standing.get("session_id") or "")
    beta = resolve_beta_approver(project_root, session_id)
    if not beta:
        return {
            "ok": False,
            "status": REFUSAL_BETA_UNAVAILABLE,
            "error": (
                f"no BETA/{BETA_APPROVER_ROLE} seat can be resolved for session "
                f"{session_id!r} right now, so it cannot be PROVEN that this "
                "caller holds the approving seat. Unverifiable is not verified, "
                "and a verdict recorded without that proof would be a verdict "
                "from an unranked actor. ai_seat(mode='status') shows the seat."
            ),
        }
    if caller_actor_id != beta["actor_id"]:
        return {
            "ok": False,
            "status": REFUSAL_WRONG_ACTOR,
            "error": (
                f"only the actor currently holding the BETA/{BETA_APPROVER_ROLE} "
                "seat may rule on a backlog admission. This caller is "
                f"{caller_actor_id!r}; the seat is held by {beta['actor_id']!r}. "
                "If you held this seat before a succession, you no longer hold "
                "the authority that went with it — a displaced seat-holder keeps "
                "no approval power."
            ),
        }
    # FOLLOW THE ROLE. The obligation was addressed to whoever held the seat when
    # it was created; it is decided by whoever holds it now. The actor id passed
    # here is DERIVED from the seat resolver two lines above, never from a
    # parameter, and the rebind itself refuses to make the requester its own
    # approver.
    rebound = approval_authority.rebind_approver_to_seat_holder(
        project_root,
        approval_id=approval_id,
        approver_actor_id=beta["actor_id"],
        approver_role=beta["role"],
    )
    if not rebound.get("ok"):
        return rebound
    return approval_authority.decide_approval(
        project_root,
        approval_id=approval_id,
        decision=decision,
        body=body,
    )


def backlog_admission_standing(
    project_root: Path,
    *,
    approval_id: str = "",
    content: str = "",
    priority: str = "",
    kind: str = "",
    difficulty: Any = 0,
    tags: Any = None,
    status: str = "",
    session_id: str = "",
) -> dict:
    """The DIAGNOSIS door: the state of ONE obligation, by id or by payload.

    BOUNDED BY CONSTRUCTION -- it answers about one obligation and never lists
    the queue. A refusal that pointed at a dump would hand the reader everything
    except the row they were refused on.
    """
    try:
        approval_authority.expire_due_approvals(project_root)
    except Exception:
        logger.exception("approval expiry sweep failed; reading standing as-is")
    approval_id = str(approval_id or "").strip()
    if approval_id:
        return approval_authority.approval_by_id(project_root, approval_id)
    if not str(content or "").strip():
        return {
            "ok": False,
            "status": "invalid",
            "error": (
                "pass approval_id=<id from the refusal>, or the IDENTICAL add "
                "payload (content plus any priority/kind/difficulty/tags) whose "
                "obligation you want to see — the obligation is keyed on a "
                "digest of that payload, so a paraphrase is a different subject"
            ),
        }
    canonical, payload_err = canonical_admission_payload(
        content=content,
        priority=priority,
        kind=kind,
        difficulty=difficulty,
        tags=tags,
        status=status,
    )
    if payload_err is not None or canonical is None:
        return {
            "ok": False,
            "status": REFUSAL_INVALID_PAYLOAD,
            "error": payload_err or "invalid backlog payload",
        }
    caller = approval_authority._caller_actor(project_root)
    effective_session = (
        str(session_id or "").strip()
        or str((caller or {}).get("session_id") or "").strip()
    )
    # THE SAME DIGEST THE ADD PATH TAKES, through the same canonicalizer. This is
    # the door the refusal NAMES, so if it computed the subject even slightly
    # differently it would report "unknown" for an obligation that exists -- a
    # remedy with no door, which this code has already been caught doing once.
    subject_id = admission_subject_id(
        project_root=project_root,
        session_id=effective_session,
        canonical=canonical,
    )
    return approval_authority.approval_status(
        project_root,
        subject_kind=BACKLOG_ADMISSION_PURPOSE,
        subject_id=subject_id,
    )
