"""ClearFreezeService — the ONE audited primitive for lifting a freeze.

Every surface that clears a session freeze — the CLI (`aidocs admin
clear-freeze`), the MCP `admin_clear_freeze` tool, and the operator
chat-unfreeze — clears THROUGH ``clear_with_audit`` so the ordering and the
audit ledger are identical and a partial failure is never a silent
inconsistency.

Castle-grade ordering across three NON-transactional sqlite stores
(execution_index / escalation / session_freeze):

  1. write the AUDIT LEDGER first (``freeze_clear_initiated``). If even this
     fails → KEEP the freeze, escalation untouched (fully consistent).
  2. decline the linked escalation. On failure → ``freeze_clear_degraded``
     marker + KEEP the freeze (request stays pending = consistent frozen
     state).
  3. lift the lock. On failure the escalation is already declined while the
     freeze is still active — the one inconsistent edge: emit an explicit
     ``freeze_clear_repair_needed`` marker + KEEP the freeze so a repair
     pass / existing-freeze rendering reconciles it.
  4. write the surface's ``cleared`` event on success.

Callers own resolution + authorization (by id/session, RBAC / dev-flavor /
host-binding); the service owns the atomic-ish decide+clear+audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _holds_super_admin(project_root: Path, user_id: str) -> bool:
    """Fail-CLOSED org-admin check for the freeze-clear floor: does the user
    hold the rank-0 ``super_admin`` role at global scope?

    Deliberately NOT ``identity_resolver.current_effective_role`` — that helper
    is for audit stamping and fails OPEN to 'super_admin' when RBAC data is
    missing. A security floor must fail CLOSED: any miss / no-row / error →
    False, so a lookup gap never silently grants org-admin.
    """
    if not user_id:
        return False
    try:
        # #755/#756: the ONE canonical connect. This was
        # `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION
        # context manager, which commits and NEVER closes the handle --
        # with no pragmas at all. It is a pure SELECT on the RBAC store,
        # so read_only=True is the truthful mode for a security FLOOR:
        # sqlite itself refuses a write from this path.
        from ._sqlite_connect import connect as _canonical_connect

        from .rbac_store import RBACStore

        from .project_authority import project_scope_key

        store = RBACStore()
        store.init_db(project_root)
        with _canonical_connect(
            str(store.db_path(project_root)), read_only=True, row_factory=False
        ) as conn:
            # #516: the store is machine-GLOBAL (#488), so an unscoped row match
            # would let a FOREIGN org's super_admin pass this floor. Count a
            # global grant (the machine owner's break-glass) or a grant scoped
            # to THIS project — nothing else.
            row = conn.execute(
                "SELECT 1 FROM rbac_user_roles ur "
                "JOIN rbac_roles r ON r.role_id = ur.role_id "
                "WHERE ur.user_id = ? AND r.name = 'super_admin' "
                "AND (ur.scope_type = 'global' "
                "     OR (ur.scope_type = 'project' AND ur.scope_id = ?)) LIMIT 1",
                (user_id, project_scope_key(project_root)),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def freeze_clear_ladder_block(
    project_root: Path,
    *,
    approver_user_id: str,
    target_user_id: str,
) -> dict[str, Any] | None:
    """The relational floor for clearing a freeze, layered ON TOP of the
    ``PERM_ADMIN_CLEAR_FREEZE`` capability gate (which the caller checks first).

    Tiers (RBAC, fail-closed): org-admin = the ``super_admin`` role; admin =
    holds ``PERM_ADMIN_CLEAR_FREEZE``; operator = neither. Non-negotiable rules:
      * no self-clear unless the approver is an org-admin,
      * an admin's freeze can be cleared ONLY by an org-admin,
      * an operator's freeze may be cleared by any admin (the capability gate
        already passed upstream).

    Returns a refusal dict (return it verbatim) or None when the clear may
    proceed. Callers SKIP this under kill-switch / dev-flavor break-glass — the
    documented identity-less escapes where no approver identity exists.
    """
    from .permission_catalog import PERM_ADMIN_CLEAR_FREEZE
    from .rbac_store import RBACStore

    approver_uid = str(approver_user_id or "")
    target_uid = str(target_user_id or "")
    if not target_uid:
        # No attributed frozen user → no relation to enforce; capability stands.
        return None

    if _holds_super_admin(project_root, approver_uid):
        return None  # org-admin clears anything, including self.

    if target_uid == approver_uid:
        return {
            "ok": False,
            "blocked_by": "self_clear_forbidden",
            "error": (
                "you cannot clear your OWN freeze unless you are an org-admin "
                "(super_admin); ask another admin to clear it."
            ),
        }
    # Clearing someone ELSE's freeze: an admin's freeze needs an org-admin.
    try:
        from .project_authority import project_scope_key

        # #516: project scope — the tier floor must read the same scope the
        # capability gate wrote/read (global break-glass still applies).
        target_is_admin = RBACStore().has_permission(
            project_root,
            target_uid,
            PERM_ADMIN_CLEAR_FREEZE,
            scope_type="project",
            scope_id=project_scope_key(project_root),
        )
    except Exception:
        target_is_admin = True  # fail CLOSED: unknown target tier → treat as admin.
    if target_is_admin:
        return {
            "ok": False,
            "blocked_by": "tier_floor",
            "error": (
                "this freeze belongs to an admin; only an org-admin "
                "(super_admin) can clear an admin's freeze."
            ),
        }
    return None  # target is an operator → any admin may clear.


@dataclass(frozen=True)
class ClearFreezeResult:
    cleared: bool
    status: str  # cleared | audit_failed | decide_failed | repair_needed
    request_id: str
    session_id: str
    escalation_status: str
    message: str


def clear_freeze_origin_for_cli(
    approver_user_id: str | None,
    *,
    stdin_tty: bool | None = None,
) -> str:
    """Classify a CLI clear-freeze as operator vs agent self-cancel.

    operator ⇐ a REAL resolved approver identity OR a live interactive TTY
    on stdin+stdout: an agent cannot present one — ai_run, the Bash tool,
    and Claude Code `!` commands are all pipe-attached — so a TTY means a
    human at a real terminal (fix 1 of the 2026-07-16
    `! aidocs admin clear-freeze` mis-strike bug; fix 2 is the host-side
    void in SecurityViolationService.void_self_cancel_after_local_clear).
    Everything else stays agent_self: never under-strike. (#404: the
    kill-switch operator classification is gone with the kill switch.)
    """
    if approver_user_id:
        return "operator"
    if stdin_tty is None:
        try:
            import sys

            stdin_tty = bool(
                sys.stdin is not None
                and sys.stdout is not None
                and sys.stdin.isatty()
                and sys.stdout.isatty()
            )
        except Exception:
            stdin_tty = False
    return "operator" if stdin_tty else "agent_self"


def strike_reset_scope_target(freeze_scope: str, agent_context_id: str) -> str | None:
    """Resolve WHO a freeze-clear's strike reset covers — from the freeze's OWN
    declared scope (#588 ``freeze_scope``), never re-derived (#662 clause 3).

    Returns the ``security_violation_reset`` marker's ``target_entity``, or
    ``None`` meaning REFUSE TO RESET.

      * ``FREEZE_SCOPE_SESSION`` -> ``""``: the freeze bound every actor in the
        work session, so the reset must reach every actor in it. Previously the
        reset was targeted at whichever single ``agent_context_id`` the row
        happened to carry, so a session-scoped clear lifted the lock and left
        every OTHER lane at a ceiling it could not get back under — the measured
        8/3 of 2026-07-30.
      * ``FREEZE_SCOPE_ACTOR`` -> that actor's id, and ``None`` when it cannot
        be resolved. Fail closed on the GRANT (``promoted-06ad3c5f61ab``): a
        strike reset is restored margin, so an unresolvable scope REFUSES rather
        than widening to "everyone". Refusing costs the operator one more
        command; resetting the wrong scope silently forgives conduct nobody
        reviewed. #588's ``set_freeze`` already refuses to MINT this row, so it
        is only reachable as drift — which is exactly what fail-closed is for.
      * legacy (``freeze_scope=''``) -> keeps the reach that row actually had:
        session-wide only when it had no actor to key on, per #588's rule that
        legacy rows are never silently narrowed.

    The scope is READ, not inferred: an empty ``agent_context_id`` is no longer
    overloaded to mean "session-wide" on an actor-scoped row.
    """
    from .session_freeze_store import FREEZE_SCOPE_ACTOR, FREEZE_SCOPE_SESSION

    scope = str(freeze_scope or "")
    acid = str(agent_context_id or "")
    if scope == FREEZE_SCOPE_SESSION:
        return ""
    if scope == FREEZE_SCOPE_ACTOR:
        return acid or None
    return acid


def _self_cancel_actor(
    project_root: Path,
    host_session_id: str,
    *,
    conversation: bool = False,
) -> str:
    """Derive the self-cancelling agent's context id, or '' if it cannot
    be pinned. '' means UNATTRIBUTED, which the notice store refuses to
    hand to any identified sibling (#736 finding 4).

    #879 B3. Two things were wrong here. The ``host_kind`` was HARD-CODED
    to ``"claude_code"``, so a codex (or any other) host's self-cancel was
    recorded under a claude_code bucket it never belonged to and its own
    reads never used. And no ``agent_id`` was passed, so a SUBAGENT's
    self-cancel was attributed to its parent. Both axes are now resolved
    through the one freeze-identity authority, which returns honest empties.

    ``conversation=True`` returns the actor's CONVERSATION key instead — the
    same host axes with the agent axis deliberately omitted. That is the
    second key the notice row carries so it stays reachable by the only
    actor the MCP drain transport can name (see ``_owned_by``).
    """
    if not host_session_id:
        return ""
    try:
        from .agent_memory_epoch import derive_agent_context_id
        from .freeze_service import resolve_freeze_actor

        host, kind, agent = resolve_freeze_actor(
            host_session_id,
            "",
            project_root=project_root,
        )
        return derive_agent_context_id(
            host_kind=kind,
            project_root=project_root,
            host_session_id=host,
            agent_id=None if conversation else (agent or None),
        ) or ""
    except Exception:
        return ""


def _record_self_cancel_strike(
    project_root: Path,
    *,
    hub: Any,
    session_id: str,
    host_session_id: str,
    audit,
) -> None:
    """Record an agent self-cancel freeze-strike + surface it on the rail.

    Fail-quiet: never raises into the clear path (a bookkeeping failure must
    not turn a successful freeze-clear into an error).

    With a ``hub`` the strike goes through SecurityViolationService.
    record_and_escalate — the ONE recorder that owns scope-key shaping,
    severity, the freeze-at-threshold escalation (so the 3rd self-cancel
    re-freezes) AND the notification-rail enqueue. Without a hub (e.g. a
    hub-less CLI caller) we can't run that recorder, so we emit a best-effort
    ``security_violation_strike`` audit row for the trail plus a rail notice
    directly.
    """
    try:
        if hub is not None:
            from .security_violation_service import SecurityViolationService

            # record_and_escalate enqueues the rail notice itself — do NOT
            # enqueue again here (that would double-count the surface).
            SecurityViolationService(hub).record_and_escalate(
                project_root,
                session_id=session_id,
                family="self_cancel",
                actor="agent",
                host_session_id=host_session_id,
            )
            return
    except Exception:
        return
    # Hub-less fallback: best-effort strike row + rail notice (no count/freeze).
    try:
        audit(
            "security_violation_strike",
            "strike",
            {"family": "self_cancel", "note": "agent self-cancel (hub-less path)"},
        )
    except Exception:
        pass
    try:
        from . import freeze_strike_notice_store as _fsn

        _fsn.enqueue_strike_notice(
            project_root,
            session_id,
            count=1,
            threshold=3,
            family="self_cancel",
            # #736 finding 4: attribute the notice to the agent that
            # self-cancelled whenever its host session can be resolved.
            # If it cannot, the notice stays UNATTRIBUTED — and an
            # unattributed notice is no longer session-broadcast, so it
            # can never be handed to a sibling that shares this work
            # session (freeze_strike_notice_store._owned_by).
            agent_context_id=_self_cancel_actor(project_root, host_session_id),
            # #879 B3: also record the actor's own CONVERSATION key so the
            # row cannot become one the unreachable prune keeps forever.
            conversation_agent_context_id=_self_cancel_actor(
                project_root, host_session_id, conversation=True,
            ),
            origin="self_cancel",
        )
    except Exception:
        pass



#: #663 — the ONE table that says whether an escalation verdict forgives the
#: conduct that produced the freeze. The axis is the VERDICT, not the surface
#: and not ``clear_origin``:
#:   approve -> the operator said YES to the action, so the strike that caused
#:     the freeze is settled. Reset (audited). Not resetting here is the real
#:     user harm: the agent leaves the freeze already carrying the strikes that
#:     put it there, so the next minor infraction re-freezes it instantly and
#:     the approval bought one action instead of a clean slate.
#:   deny / cancel -> the action was REFUSED. The lock drops so work can
#:     continue, but a denial is not absolution — the strikes STAND.
#: A verdict that is not in this table REFUSES (ValueError) rather than
#: defaulting either way: a strike must never be removed without an operator
#: decision behind it, and a genuine decision must never be silently downgraded.
ESCALATION_DECISION_RESETS_STRIKES: dict[str, bool] = {
    "approve": True,
    "deny": False,
    "cancel": False,
}


def lift_freeze_for_escalation_decision(
    project_root: Path,
    request_id: str,
    *,
    decision: str,
    approver_user_id: str | None,
    approver_label: str,
    reason: str = "",
    source_kind: str = "escalation_decision",
    hub: Any = None,
) -> int:
    """THE chokepoint for lifting a freeze as part of deciding an escalation.

    Every approve/deny surface — the CLI ``admin approve-escalation`` /
    ``deny-escalation``, the MCP ``rbac_approve_escalation`` /
    ``rbac_deny_escalation`` tools, and the gate's
    ``outer_gate_escalation_ops`` pair — used to call
    ``SessionFreezeStore.clear_freeze_by_request`` directly. Six rival lift
    paths, none of which settled the strike ledger (#663 / #662 clause 3).
    They all route here now, so the reset-or-record decision is made ONCE.

    Returns the number of freeze rows lifted (0 when there was no freeze —
    an escalation decided while nothing was frozen is normal, not an error).
    """
    from .session_freeze_store import SessionFreezeStore

    resets = ESCALATION_DECISION_RESETS_STRIKES.get(str(decision))
    if resets is None:
        # Fail closed on the GRANT: an unknown verdict buys neither a reset
        # nor a silent pass. Adding a verdict means deciding, explicitly,
        # whether it forgives the conduct.
        raise ValueError(
            f"unknown escalation decision {decision!r}; "
            f"expected one of {sorted(ESCALATION_DECISION_RESETS_STRIKES)}",
        )

    store = SessionFreezeStore()
    target = store.get_active_freeze_by_id(project_root, request_id)
    if target is None:
        # Nothing active to lift. Still sweep the row by request id so a
        # decided escalation never leaves a stale/expired lock behind — there
        # is no ledger decision to make when there was no live freeze.
        try:
            return int(store.clear_freeze_by_request(project_root, request_id))
        except Exception:  # noqa: BLE001 — lock cleanup is best-effort
            return 0

    result = ClearFreezeService().clear_with_audit(
        project_root,
        target_freeze=target,
        reason=reason or f"escalation {decision}",
        approver_label=approver_label,
        approver_user_id=approver_user_id,
        source_kind=source_kind,
        cleared_event_kind="freeze_cleared_escalation_decision",
        clear_origin="operator" if resets else "operator_no_reset",
        extra_payload={"escalation_decision": str(decision)},
        hub=hub,
    )
    return 1 if result.cleared else 0


class ClearFreezeService:
    """Hub-independent: writes audit via ExecutionIndexStore directly so the
    CLI (no hub), the MCP tool, and the chat path all share one primitive.
    """

    def clear_with_audit(
        self,
        project_root: Path,
        *,
        target_freeze: Any,
        reason: str,
        approver_label: str,
        source_kind: str,
        cleared_event_kind: str,
        approver_user_id: str | None = None,
        permission_name: str | None = None,
        extra_payload: dict | None = None,
        clear_origin: str = "operator",
        hub: Any = None,
    ) -> ClearFreezeResult:
        # clear_origin (operator directive 2026-07-15):
        #   "operator"   — a genuine external operator cleared it (human "cancel"
        #                  prompt, dashboard, real RBAC token). Emits the
        #                  security_violation_reset marker: trust decision, the
        #                  session starts fresh. (default — preserves history.)
        #   "agent_self" — the AGENT cleared its OWN freeze (MCP admin_clear_freeze
        #                  tool / dev-flavor CLI). This is a self-cancel: it must
        #                  ADD a freeze-strike (teaching the agent), NOT reset. The
        #                  strike is recorded via record_and_escalate when a ``hub``
        #                  is supplied (so the 3rd self-cancel re-freezes), and a
        #                  5-surface notice is enqueued on the notification rail.
        # Fail-safe sanitize: an UNRECOGNIZED origin must NOT buy a free reset —
        # reset is the privileged operator-only outcome. The param DEFAULT is
        # "operator" (legit omitted callers), but any explicit value that isn't
        # exactly "operator"/"agent_self" falls toward "agent_self" (strike, no
        # reset). Worst case we over-strike; we never under-strike. Adversarial:
        # blocks a garbage-origin bypass if attacker data ever reaches this arg.
        #   "operator_no_reset" — an OPERATOR decided, the lock lifts, but the
        #                  verdict does NOT forgive the conduct (#663: a DENIED
        #                  or CANCELLED escalation). No reset AND no strike: the
        #                  ledger stands exactly as it was, and the non-reset is
        #                  RECORDED (``freeze_clear_no_reset``) so "not reset" is
        #                  a decision on the trail rather than an omission
        #                  indistinguishable from the bypass bug it replaces.
        if clear_origin not in ("operator", "agent_self", "operator_no_reset"):
            clear_origin = "agent_self"
        request_id = str(getattr(target_freeze, "request_id", "") or "")
        session_id = str(getattr(target_freeze, "session_id", "") or "")
        host_session_id_fz = str(getattr(target_freeze, "host_session_id", "") or "")
        agent_context_id_fz = str(getattr(target_freeze, "agent_context_id", "") or "")
        aidocs_session_id_fz = str(getattr(target_freeze, "aidocs_session_id", "") or "")
        freeze_kind = str(getattr(target_freeze, "kind", "") or "")
        # #588 D1's declared scope — the AUTHORITY for how wide the strike
        # reset reaches (#662 clause 3). Never re-derive it here.
        freeze_scope_fz = str(getattr(target_freeze, "freeze_scope", "") or "")
        fingerprint = str(getattr(target_freeze, "fingerprint_phrase", "") or "")
        base = {
            "request_id": request_id,
            "session_id": session_id,
            "agent_context_id": agent_context_id_fz,
            "aidocs_session_id": aidocs_session_id_fz,
            "host_session_id": host_session_id_fz,
            "freeze_kind": freeze_kind,
            "approver_label": approver_label,
            "approver_user_id": approver_user_id,
            "reason": reason,
        }

        def _audit(
            kind: str,
            status: str,
            extra: dict | None = None,
            *,
            perm: str | None = None,
            target: str | None = None,
        ) -> bool:
            try:
                from .execution_index_store import ExecutionIndexStore

                kwargs: dict = {}
                if approver_user_id:
                    kwargs["user_id"] = approver_user_id
                if perm:
                    kwargs["permission_name"] = perm
                ExecutionIndexStore().record_event(
                    project_root,
                    event_kind=kind,
                    source_kind=source_kind,
                    session_id=session_id,
                    capability_name="clear_freeze",
                    action_kind="clear",
                    target_entity=(target if target is not None else request_id),
                    status=status,
                    payload={**base, **(extra or {})},
                    **kwargs,
                )
                return True
            except Exception:
                return False

        # 1. Durable intent ledger FIRST.
        if not _audit("freeze_clear_initiated", "initiated"):
            return ClearFreezeResult(
                False,
                "audit_failed",
                request_id,
                session_id,
                "no_change",
                "freeze clear could not be recorded (audit write failed); freeze KEPT.",
            )

        # 2. Decline the linked escalation.
        escalation_status = "no_change"
        try:
            from .escalation_store import EscalationStore

            decided = EscalationStore().decide(
                project_root,
                request_id,
                approve=False,
                approver_user_id=approver_user_id,
                approver_label=approver_label,
                reason=f"clear-freeze ({source_kind}): {reason}".strip(),
            )
            escalation_status = getattr(decided, "status", "no_change") if decided else "no_change"
        except Exception:
            _audit("freeze_clear_degraded", "degraded", {"stage": "escalation_decide"})
            return ClearFreezeResult(
                False,
                "decide_failed",
                request_id,
                session_id,
                "no_change",
                "escalation decline failed; freeze KEPT (request pending).",
            )

        # 3. Lift the lock.
        try:
            from .session_freeze_store import SessionFreezeStore

            cleared = (
                SessionFreezeStore().clear_freeze_by_request(
                    project_root,
                    request_id,
                )
                > 0
            )
        except Exception:
            _audit(
                "freeze_clear_repair_needed",
                "degraded",
                {
                    "note": "escalation declined but freeze still active",
                    "escalation_status": escalation_status,
                },
            )
            return ClearFreezeResult(
                False,
                "repair_needed",
                request_id,
                session_id,
                escalation_status,
                "escalation declined but lock not lifted — flagged for repair.",
            )

        # 4a. clear_freeze returned False (no exception, but ZERO rows
        #     deleted) → NOT cleared. Never herald a false success: emit a
        #     durable repair marker and report the truth.
        if not cleared:
            _audit(
                "freeze_clear_repair_needed",
                "degraded",
                {
                    "note": "clear_freeze returned false; no row deleted",
                    "escalation_status": escalation_status,
                },
            )
            return ClearFreezeResult(
                False,
                "not_cleared",
                request_id,
                session_id,
                escalation_status,
                "escalation declined but no freeze row was deleted -- flagged for repair.",
            )

        # Reconcile the shared conductor blocker only when it names this
        # exact freeze request. Other actors' blockers remain authoritative.
        if request_id:
            try:
                from .session_store import SessionStore

                sessions = SessionStore(
                    templates_root=project_root / ".MEMORY" / ".aidocs",
                )
                session = sessions.read_session(project_root, session_id)
                blockers = list(session.sections.get("Blockers", []))
                remaining = [line for line in blockers if request_id not in str(line)]
                if remaining != blockers:
                    sessions.update_session(
                        project_root,
                        session_id,
                        {"Blockers": remaining or ["-"]},
                    )
            except Exception:
                pass

        # 4b. STRIKE RESET (2026-06-11): the operator clearing a freeze is an
        # 4b. STRIKE vs RESET (operator directive 2026-07-15). Who cleared it
        # decides whether the strike ledger resets or ratchets:
        #   operator  → RESET (2026-06-11): a genuine external operator clearing
        #     a freeze is an explicit trust decision — "we start fresh." Emit a
        #     session-scoped security_violation_reset marker that _count_strikes
        #     honours (counts only strikes observed AFTER it), so a stable
        #     session id can't ratchet days-old strikes into an instant re-freeze.
        #   agent_self → STRIKE: the agent cleared its OWN freeze — a self-cancel,
        #     i.e. a mistake to learn from. Record a freeze-strike (no reset), so
        #     repeated self-cancels ratchet toward the uncancelable 3-strike
        #     freeze, and surface it on the notification rail.
        # MUST precede the cleared surface event below: the ledger contract
        # (test_clear_freeze_service.test_happy_path_ledger_first) requires
        # cleared_event_kind to be the LAST event in the chain.
        # SCOPE (#662 clause 3): the reset covers exactly what the FREEZE
        # covered — read from #588's ``freeze_scope`` column, never re-derived
        # from whether ``agent_context_id`` happens to be empty. See
        # strike_reset_scope_target for the three cases and the refusal.
        if clear_origin == "operator":
            reset_target = strike_reset_scope_target(
                freeze_scope_fz,
                agent_context_id_fz,
            )
            if reset_target is None:
                # Fail closed on the GRANT: the lock is lifted (mercy on the
                # blockage) but the strikes are NOT forgiven (mercy on the
                # conduct) because we cannot prove whose they are. Audited on
                # this same clear record so the operator can see it and re-issue.
                _audit(
                    "freeze_clear_reset_refused",
                    "refused",
                    {
                        "note": (
                            "actor-scoped freeze with no resolvable actor — strike "
                            "reset REFUSED (unresolvable scope must not widen); the "
                            "lock WAS lifted"
                        ),
                        "freeze_scope": freeze_scope_fz,
                    },
                )
            else:
                _audit(
                    "security_violation_reset",
                    "reset",
                    {
                        "note": "freeze cleared by operator/admin — strike count reset",
                        "freeze_scope": freeze_scope_fz,
                        "reset_scope": "session" if reset_target == "" else "actor",
                        # #663: the reset must be RECORDED with WHO decided
                        # (approver_user_id/label, already in ``base``), WHICH
                        # escalation (request_id, ditto) and WHICH verdict —
                        # the caller's decision rides here. An unrecorded reset
                        # is indistinguishable from tampering.
                        **(extra_payload or {}),
                    },
                    target=reset_target,
                )
        elif clear_origin == "operator_no_reset":
            # #663: an operator decision that REFUSED the action (deny/cancel).
            # The lock drops so work can continue, but a denial is not
            # absolution — the strikes STAND. Neither reset nor strike; the
            # non-decision is itself recorded so the audit trail distinguishes
            # "deliberately not reset" from "nobody ever reset it".
            _audit(
                "freeze_clear_no_reset",
                "no_reset",
                {
                    "note": (
                        "freeze lifted by an operator decision that did NOT "
                        "forgive the conduct — strike ledger left intact"
                    ),
                    "freeze_scope": freeze_scope_fz,
                    **(extra_payload or {}),
                },
            )
        else:
            _record_self_cancel_strike(
                project_root,
                hub=hub,
                session_id=session_id,
                host_session_id=host_session_id_fz,
                audit=_audit,
            )

        # 4c. Cleared for real → surface-specific cleared event. TERMINAL
        # surface signal — must remain the last audit row in the chain.
        _audit(
            cleared_event_kind,
            "cleared",
            {
                "fingerprint_phrase": fingerprint,
                "escalation_status": escalation_status,
                **(extra_payload or {}),
            },
            perm=permission_name,
        )
        return ClearFreezeResult(
            True,
            "cleared",
            request_id,
            session_id,
            escalation_status,
            f"freeze cleared by {approver_label} (reason: {reason}).",
        )
