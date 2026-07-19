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
        import sqlite3

        from .rbac_store import RBACStore

        store = RBACStore()
        store.init_db(project_root)
        with sqlite3.connect(str(store.db_path(project_root))) as conn:
            row = conn.execute(
                "SELECT 1 FROM rbac_user_roles ur "
                "JOIN rbac_roles r ON r.role_id = ur.role_id "
                "WHERE ur.user_id = ? AND r.name = 'super_admin' LIMIT 1",
                (user_id,),
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
        target_is_admin = RBACStore().has_permission(
            project_root, target_uid, PERM_ADMIN_CLEAR_FREEZE
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
            origin="self_cancel",
        )
    except Exception:
        pass



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
        if clear_origin not in ("operator", "agent_self"):
            clear_origin = "agent_self"
        request_id = str(getattr(target_freeze, "request_id", "") or "")
        session_id = str(getattr(target_freeze, "session_id", "") or "")
        host_session_id_fz = str(getattr(target_freeze, "host_session_id", "") or "")
        agent_context_id_fz = str(getattr(target_freeze, "agent_context_id", "") or "")
        aidocs_session_id_fz = str(getattr(target_freeze, "aidocs_session_id", "") or "")
        freeze_kind = str(getattr(target_freeze, "kind", "") or "")
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
        if clear_origin == "operator":
            _audit(
                "security_violation_reset",
                "reset",
                {"note": "freeze cleared by operator/admin — strike count reset"},
                target=agent_context_id_fz,
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
