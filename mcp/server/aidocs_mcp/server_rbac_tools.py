"""RBAC MCP tools — Layer 9 C-2 dashboard integration.

Six tools total:
- rbac_list_pending_escalations: dashboard reads this to render the
  approval queue.
- rbac_approve_escalation / rbac_deny_escalation: admin click
  actions from the dashboard popup.
- rbac_list_users / rbac_list_roles / rbac_user_permissions: surface
  the identity + role state for the RBAC admin screen.

Every mutation is audit-logged via ExecutionIndexStore.record_event.
Approve/deny require PERM_RBAC_APPROVE_ESCALATIONS on the caller
when caller_user_id is passed; when absent, the tools assume the
caller is the dashboard itself (which runs server-side with full
privilege) — operators lock down dashboard access via the
aidocs_identity_dashboard_token system separately.
"""

from __future__ import annotations

from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as


def _resolve_calling_operator(root: Any) -> Any | None:
    """#301: derive the acting operator from the AUTHENTICATED caller.

    The approver/clearer identity MUST come from the logged-in session, NEVER a
    caller-supplied email (which any caller could set to a higher admin's
    address — a spoofing hole). Returns an OperatorContext (.user_id / .email)
    or None when the caller is not an authenticated operator. Callers fail
    CLOSED on None: no logged-in identity → no authority action.
    """
    from .mcp_server_runtime_helpers import current_calling_host_session_id
    from .operator_auth_service import OperatorAuthService

    hsid = current_calling_host_session_id()
    if not hsid:
        return None
    try:
        return OperatorAuthService().resolve_operator_context_from_host_session(hsid, root)
    except Exception:
        return None


def register_rbac_tools(*, server: Any, hub: Any, runtime: Any) -> None:

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "List Pending Escalations",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("list", title="pending escalations")
    def rbac_list_pending_escalations(
        session_id: str = "",
    ) -> Any:
        """List pending RBAC escalation requests for the dashboard
        approval queue. Each row carries enough context (session,
        task, plan_path, gate_permission, gate_phrase) for the admin
        to decide without leaving the dashboard.
        """
        from .escalation_store import EscalationStore

        root = resolve_project_root()
        store = EscalationStore()
        rows = store.list_pending(
            root,
            session_id=session_id or None,
        )
        return {
            "count": len(rows),
            "requests": [
                {
                    "request_id": r.request_id,
                    "requester_label": r.requester_label,
                    "requester_user_id": r.requester_user_id,
                    "session_id": r.session_id,
                    "task_id": r.task_id,
                    "plan_path": r.plan_path,
                    "gate_permission": r.gate_permission,
                    "gate_phrase": r.gate_phrase,
                    "sticky": r.sticky,
                    "created_at": r.created_at,
                    "expires_at": r.expires_at,
                    "extra": r.extra,
                }
                for r in rows
            ],
        }

    @server.tool(
        annotations={
            "destructiveHint": False,
            "openWorldHint": False,
            "title": "Approve Escalation",
        },
    )
    def rbac_approve_escalation(
        request_id: str,
        reason: str = "",
        grant_ttl_seconds: int = 300,
        grant_max_uses: int = 1,
        strict_command_match: bool = False,
    ) -> dict[str, Any]:
        """Approve a pending escalation request by its request_id.

        #301: the approver is DERIVED from the authenticated caller (the
        logged-in operator), never a caller-supplied email — an unprivileged
        caller must not be able to approve by naming a higher admin. The
        resolved operator must hold rbac.approve_escalations plus the gate
        permission being unlocked (you cannot approve what you couldn't grant).

        Approval does TWO things (2026-04-22):
          1. Flips the request row to status=approved (historical record).
          2. Mints a separate rbac_escalation_grants row scoped to
             (requester_user_id, machine_id, session_id, permission) with
             its own TTL + max_uses. The gate looks up THIS table before
             re-bubbling, so the grant is the actually-consumable artifact.

        grant_ttl_seconds: how long the grant stays live (default 300s).
        grant_max_uses: how many times the requester can consume it.
        strict_command_match: if True, hash the original command snippet
          onto the grant so only that exact command satisfies it.
          False (default) = permission-bound, accepts any command
          for that permission.
        """
        import hashlib

        from .escalation_store import EscalationStore
        from .permission_catalog import PERM_RBAC_APPROVE_ESCALATIONS
        from .rbac_store import RBACStore

        root = resolve_project_root()
        rbac = RBACStore()
        escalations = EscalationStore()

        target = escalations.get(root, request_id)
        if target is None:
            return {"ok": False, "error": f"unknown request: {request_id}"}
        # #301: derive the approver from the AUTHENTICATED caller — never a
        # caller-supplied email. Fail-closed when there is no logged-in operator.
        approver = _resolve_calling_operator(root)
        if approver is None or not getattr(approver, "user_id", ""):
            return {
                "ok": False,
                "blocked_by": "approver_unauthenticated",
                "error": (
                    "escalation approval requires an authenticated operator "
                    "identity (log in via the dashboard). The approver is taken "
                    "from the logged-in session, not a supplied email."
                ),
            }
        # Permission 1: approver must hold rbac.approve_escalations.
        if not rbac.has_permission(
            root,
            approver.user_id,
            PERM_RBAC_APPROVE_ESCALATIONS,
        ):
            try:
                hub.execution.record_event(
                    root,
                    event_kind="rbac_denied",
                    source_kind="rbac_enforcement",
                    capability_name="rbac_approve_escalation",
                    action_kind="rbac_check",
                    target_entity=PERM_RBAC_APPROVE_ESCALATIONS,
                    status="refused",
                    payload={
                        "approver_user_id": approver.user_id,
                        "approver_email": getattr(approver, "email", ""),
                        "request_id": request_id,
                    },
                    user_id=approver.user_id,
                    permission_name=PERM_RBAC_APPROVE_ESCALATIONS,
                )
            except Exception:
                pass
            return {
                "ok": False,
                "error": "approver lacks rbac.approve_escalations",
            }
        # Permission 2: approver must also hold the gate perm being
        # unlocked. Retained for back-compat with the existing check.
        perms = rbac.effective_permissions(root, approver.user_id)
        if target.gate_permission not in perms:
            return {
                "ok": False,
                "error": (f"approver lacks the target gate permission ({target.gate_permission})"),
            }
        # Expired? decide() would also catch it, but we want a specific
        # error so the dashboard renders "request expired" instead of
        # "not pending."
        import time as _t
        from datetime import datetime as _dt

        try:
            expires_epoch = _dt.fromisoformat(target.expires_at.replace("Z", "+00:00")).timestamp()
        except Exception:
            expires_epoch = 0
        if expires_epoch and expires_epoch <= _t.time():
            return {
                "ok": False,
                "error": "request has expired",
                "expires_at": target.expires_at,
            }
        decided = escalations.decide(
            root,
            request_id,
            approve=True,
            approver_user_id=approver.user_id,
            approver_label=approver.email,
            reason=reason,
        )
        if decided is None:
            return {
                "ok": False,
                "error": "request is not pending (already decided or expired)",
            }
        # Mint the scoped grant. All binding fields come from the
        # original request row so an approver can't silently retarget
        # the grant to a different user/machine/session.
        command_hash: str | None = None
        if strict_command_match and target.command_snippet:
            command_hash = hashlib.sha256(target.command_snippet.encode("utf-8")).hexdigest()
        grant = None
        if decided.session_id and decided.requester_user_id:
            try:
                grant = escalations.create_grant(
                    root,
                    request_id=request_id,
                    user_id=decided.requester_user_id,
                    machine_id=decided.machine_id or "",
                    session_id=decided.session_id or "",
                    permission_name=decided.gate_permission,
                    approved_by_user_id=approver.user_id,
                    ttl_seconds=grant_ttl_seconds,
                    max_uses=grant_max_uses,
                    command_hash=command_hash,
                )
            except Exception:
                grant = None
        try:
            hub.execution.record_event(
                root,
                event_kind="escalation_approved",
                source_kind="escalation_hook",
                session_id=target.session_id,
                capability_name="rbac_approve_escalation",
                action_kind="escalation",
                target_entity=target.gate_permission,
                status="approved",
                payload={
                    "request_id": request_id,
                    "approver_user_id": approver.user_id,
                    "approver_email": approver.email,
                    "gate_permission": target.gate_permission,
                    "session_id": target.session_id,
                    "machine_id": target.machine_id,
                    "requester_user_id": target.requester_user_id,
                    "grant_id": grant.grant_id if grant else None,
                    "grant_ttl_seconds": grant_ttl_seconds,
                    "grant_max_uses": grant_max_uses,
                    "strict_command_match": bool(strict_command_match),
                    "reason": reason,
                },
                user_id=approver.user_id,
                permission_name=target.gate_permission,
            )
        except Exception:
            pass
        # Clear any session freeze pointing at this request — once
        # the escalation is approved + grant minted, the freeze is
        # done. Doctrine: remedial_freeze_management. Best-effort
        # so a freeze-store glitch doesn't block the approval.
        # #663: an APPROVAL forgives the conduct that caused the freeze, so the
        # chokepoint lifts the lock AND resets the strike ledger (audited, at
        # the freeze's own #588 scope) in one act.
        freeze_cleared = 0
        try:
            from .clear_freeze_service import lift_freeze_for_escalation_decision

            freeze_cleared = lift_freeze_for_escalation_decision(
                root,
                request_id,
                decision="approve",
                approver_user_id=approver.user_id,
                approver_label=approver.email,
                reason=reason,
                source_kind="rbac_approve_escalation",
                hub=hub,
            )
        except Exception:
            freeze_cleared = 0
        return {
            "ok": True,
            "status": decided.status,
            "request_id": decided.request_id,
            "decided_at": decided.decided_at,
            "grant_id": grant.grant_id if grant else None,
            "grant_expires_at": grant.expires_at if grant else None,
            "grant_max_uses": grant.max_uses if grant else None,
            "freeze_cleared": int(freeze_cleared),
        }

    @server.tool(
        annotations={
            "destructiveHint": False,
            "openWorldHint": False,
            "title": "Deny Escalation",
        },
    )
    def rbac_deny_escalation(
        request_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Deny a pending escalation request.

        AUTH PARITY (2026-05-26): requires authenticated approver
        identity AND rbac.approve_escalations permission, matching
        rbac_approve_escalation. The earlier "deny accepts anonymous"
        path silently allowed an unprivileged caller to dismiss real
        escalations (clearing the freeze + closing the record) without
        identity. Denying is an authority decision, not a cleanup —
        a denier without approve authority could otherwise mass-dismiss
        legitimate requests.

        #301: the denier identity is DERIVED from the authenticated caller
        (logged-in operator), never a caller-supplied email.
        """
        from .escalation_store import EscalationStore
        from .permission_catalog import PERM_RBAC_APPROVE_ESCALATIONS
        from .rbac_store import RBACStore

        root = resolve_project_root()
        rbac = RBACStore()
        escalations = EscalationStore()
        # #301: derive the denier from the AUTHENTICATED caller; fail-closed.
        approver = _resolve_calling_operator(root)
        if approver is None or not getattr(approver, "user_id", ""):
            return {
                "ok": False,
                "blocked_by": "approver_unauthenticated",
                "error": (
                    "escalation denial requires an authenticated operator "
                    "identity (log in via the dashboard). The denier is taken "
                    "from the logged-in session, not a supplied email."
                ),
            }
        if not rbac.has_permission(
            root,
            approver.user_id,
            PERM_RBAC_APPROVE_ESCALATIONS,
        ):
            try:
                hub.execution.record_event(
                    root,
                    event_kind="rbac_denied",
                    source_kind="rbac_enforcement",
                    capability_name="rbac_deny_escalation",
                    action_kind="rbac_check",
                    target_entity=PERM_RBAC_APPROVE_ESCALATIONS,
                    status="refused",
                    payload={
                        "approver_user_id": approver.user_id,
                        "approver_email": getattr(approver, "email", ""),
                        "request_id": request_id,
                    },
                    user_id=approver.user_id,
                    permission_name=PERM_RBAC_APPROVE_ESCALATIONS,
                )
            except Exception:
                pass
            return {
                "ok": False,
                "error": "approver lacks rbac.approve_escalations",
            }
        approver_user_id: str | None = approver.user_id
        approver_label = approver.email
        decided = escalations.decide(
            root,
            request_id,
            approve=False,
            approver_user_id=approver_user_id,
            approver_label=approver_label,
            reason=reason,
        )
        if decided is None:
            return {"ok": False, "error": "request not pending"}
        try:
            hub.execution.record_event(
                root,
                event_kind="escalation_denied",
                source_kind="escalation_hook",
                session_id=decided.session_id,
                capability_name="rbac_deny_escalation",
                action_kind="escalation",
                target_entity=decided.gate_permission,
                status="denied",
                payload={
                    "request_id": request_id,
                    "approver_user_id": approver_user_id,
                    "approver_label": approver_label,
                    "gate_permission": decided.gate_permission,
                    "session_id": decided.session_id,
                    "machine_id": decided.machine_id,
                    "requester_user_id": decided.requester_user_id,
                    "reason": reason,
                },
                user_id=approver_user_id,
                permission_name=decided.gate_permission,
            )
        except Exception:
            pass
        # Same remedial-cleanup: clear the freeze pointing at this
        # request. Denial means the operator chose not to approve;
        # the lock should drop, not persist.
        # #663: but a denial is NOT absolution — the chokepoint lifts the lock
        # and leaves the strike ledger standing, recording that choice.
        freeze_cleared = 0
        try:
            from .clear_freeze_service import lift_freeze_for_escalation_decision

            freeze_cleared = lift_freeze_for_escalation_decision(
                root,
                request_id,
                decision="deny",
                approver_user_id=approver_user_id,
                approver_label=approver_label,
                reason=reason,
                source_kind="rbac_deny_escalation",
                hub=hub,
            )
        except Exception:
            freeze_cleared = 0
        return {
            "ok": True,
            "status": decided.status,
            "request_id": decided.request_id,
            "decided_at": decided.decided_at,
            "freeze_cleared": int(freeze_cleared),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "List Users",
        },
    )
    @renders_as("list", title="users")
    def rbac_list_users() -> Any:
        """List every identity user + their assigned roles. Feeds the
        RBAC admin screen on the dashboard.
        """
        from .identity_store import IdentityStore
        from .rbac_store import RBACStore

        root = resolve_project_root()
        identity = IdentityStore()
        rbac = RBACStore()
        users = identity.list_users(root)
        out = []
        for u in users:
            up = rbac.get_user_permissions(root, u.user_id)
            out.append(
                {
                    "user_id": u.user_id,
                    "email": u.email,
                    "role_tag": u.role,
                    "disabled": u.disabled,
                    "created_at": u.created_at,
                    "assigned_roles": list(up.roles),
                    "permissions": sorted(up.permissions),
                },
            )
        return {"count": len(out), "users": out}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "List Roles",
        },
    )
    @renders_as("list", title="roles")
    def rbac_list_roles() -> Any:
        """List every role + the permissions it carries."""
        from .rbac_store import RBACStore

        root = resolve_project_root()
        rbac = RBACStore()
        roles = rbac.list_roles(root)
        return {
            "count": len(roles),
            "roles": [
                {
                    "role_id": r.role_id,
                    "name": r.name,
                    "description": r.description,
                    "is_system": r.is_system,
                    "created_at": r.created_at,
                    "permissions": sorted(rbac.get_role_permissions(root, r.role_id)),
                }
                for r in roles
            ],
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "User Permissions",
        },
    )
    def rbac_user_permissions(email: str) -> dict[str, Any]:
        """Resolve the flattened permission set for a user by email.
        Useful for 'can user X do Y?' debugging.
        """
        from .identity_store import IdentityStore
        from .rbac_store import RBACStore

        root = resolve_project_root()
        identity = IdentityStore()
        user = identity.get_user_by_email(root, email)
        if user is None:
            return {"ok": False, "error": "unknown user"}
        rbac = RBACStore()
        up = rbac.get_user_permissions(root, user.user_id)
        return {
            "ok": True,
            "user_id": user.user_id,
            "email": user.email,
            "roles": list(up.roles),
            "permissions": sorted(up.permissions),
        }

    # C.20: hidden implementation binding only; ToolSpec owns metadata.
    def admin_clear_freeze(
        freeze_id: str = "",
        session_id: str = "",
        reason: str = "",
        confirm_token: str = "",
    ) -> dict[str, Any]:
        """Break-glass: clear a session freeze WITHOUT minting a grant.

        Operation class: `remedial_freeze_management` (hardcoded in
        operation_classes.py). The freeze gate is skipped by doctrine
        — this tool is how the operator EXITS a freeze.

        Auth: the clearer identity is DERIVED from the authenticated caller
        (#301 — never a caller-supplied email). The resolved operator must hold
        `rbac.admin_clear_freeze` permission — there is no break-glass
        alternative (#404). Without it the call is refused with audit.

        Resolution priority:
          - freeze_id (preferred): unambiguous lookup by request_id.
          - session_id: only valid if exactly ONE active freeze
            exists for that session.
          - both: freeze_id wins; session_id is checked for
            consistency and refused if it points elsewhere.
          - neither: refused.
          - ambiguous: refused with audit.

        Side effects:
          - escalation_store.decide(approve=False) — request flips to
            denied with reason "admin break-glass: <reason>".
          - SessionFreezeStore.clear_freeze — lock dissolves.
          - `freeze_admin_cleared` audit event.
          - Does NOT mint a grant; operator retries the original
            action through normal channels.
        """
        # Two-phase confirm (registry-driven; see
        # tool_interface.REGISTRY["admin_clear_freeze"]). Even locally,
        # a break-glass freeze clear must echo the deterministic phrase.
        # Builds the same expected phrase the registry advertises.
        if not freeze_id and not session_id:
            return {
                "ok": False,
                "blocked_by": "no_target",
                "error": ("admin_clear_freeze requires freeze_id (preferred) or session_id"),
            }
        # Voice-friendly (2026-06-21): a speakable, action-bound phrase (no
        # freeze_id/session_id to spell aloud — the target stays in the human
        # summary, never in the spoken token), matched voice-tolerantly.
        from .tool_interface import _normalize_voice as _nv

        expected_confirm = "confirm clear freeze"
        if _nv(confirm_token) != _nv(expected_confirm):
            return {
                "ok": False,
                "blocked_by": "confirm_required",
                "error": (
                    "admin_clear_freeze is confirmation-gated. "
                    "Re-invoke with confirm_token set to the "
                    "phrase below — only the operator who can "
                    "see this message should accept."
                ),
                "confirm_token": expected_confirm,
                "summary": (
                    f"About to clear freeze "
                    f"{'freeze_id=' + freeze_id if freeze_id else 'session_id=' + session_id}"
                    + (f" (reason: {reason!r})" if reason else "")
                    + ". This is the break-glass exit from a lockdown "
                    "— make sure this is the right freeze before "
                    "confirming."
                ),
            }

        from .permission_catalog import PERM_ADMIN_CLEAR_FREEZE
        from .rbac_store import RBACStore
        from .session_freeze_store import SessionFreezeStore

        root = resolve_project_root()

        # #301 + #404: the clearer is ALWAYS derived from the AUTHENTICATED
        # caller (logged-in operator), never a supplied email — and there is
        # no break-glass alternative. Fail-closed when unauthenticated.
        approver = _resolve_calling_operator(root)
        if approver is None or not getattr(approver, "user_id", ""):
            return {
                "ok": False,
                "blocked_by": "no_admin_identity",
                "error": (
                    "admin_clear_freeze requires an authenticated operator "
                    "identity (log in via the dashboard). The clearer is "
                    "taken from the logged-in session, not a supplied "
                    "email. Refusing."
                ),
            }
        rbac = RBACStore()
        if not rbac.has_permission(
            root,
            approver.user_id,
            PERM_ADMIN_CLEAR_FREEZE,
        ):
            try:
                hub.execution.record_event(
                    root,
                    event_kind="rbac_denied",
                    source_kind="admin_clear_freeze",
                    capability_name="admin_clear_freeze",
                    action_kind="rbac_check",
                    target_entity=PERM_ADMIN_CLEAR_FREEZE,
                    status="refused",
                    payload={
                        "approver_user_id": approver.user_id,
                        "approver_email": getattr(approver, "email", ""),
                        "freeze_id": freeze_id,
                        "session_id": session_id,
                    },
                    user_id=approver.user_id,
                    permission_name=PERM_ADMIN_CLEAR_FREEZE,
                )
            except Exception:
                pass
            return {
                "ok": False,
                "blocked_by": "missing_permission",
                "error": (f"approver lacks {PERM_ADMIN_CLEAR_FREEZE}"),
            }
        approver_user_id: str | None = approver.user_id
        approver_label = approver.email

        if not freeze_id and not session_id:
            return {
                "ok": False,
                "blocked_by": "no_target",
                "error": ("Provide freeze_id (preferred) or session_id."),
            }

        sfs = SessionFreezeStore()
        target = None

        if freeze_id:
            target = sfs.get_active_freeze_by_id(root, freeze_id)
            if target is None:
                return {
                    "ok": False,
                    "blocked_by": "no_active_freeze",
                    "error": f"no active freeze with id {freeze_id}",
                }
            if session_id and session_id != target.session_id:
                return {
                    "ok": False,
                    "blocked_by": "freeze_session_mismatch",
                    "error": (
                        f"freeze_id {freeze_id} is bound to session "
                        f"{target.session_id}, not {session_id}"
                    ),
                }
        else:
            candidates = sfs.list_active_freezes(
                root,
                session_id=session_id,
            )
            if not candidates:
                return {
                    "ok": False,
                    "blocked_by": "no_active_freeze",
                    "error": (f"no active freeze for session {session_id}"),
                }
            if len(candidates) > 1:
                try:
                    hub.execution.record_event(
                        root,
                        event_kind="freeze_admin_clear_ambiguous",
                        source_kind="admin_clear_freeze",
                        session_id=session_id,
                        capability_name="admin_clear_freeze",
                        action_kind="ambiguity_refusal",
                        target_entity=session_id,
                        status="refused",
                        payload={
                            "candidate_count": len(candidates),
                            "candidate_freeze_ids": [c.request_id for c in candidates],
                            "approver_user_id": approver_user_id,
                            "approver_label": approver_label,
                        },
                    )
                except Exception:
                    pass
                return {
                    "ok": False,
                    "blocked_by": "ambiguous_freeze",
                    "error": (
                        f"session {session_id} has "
                        f"{len(candidates)} active freezes; "
                        f"specify freeze_id"
                    ),
                    "candidate_freeze_ids": [c.request_id for c in candidates],
                }
            target = candidates[0]

        # Relational floor (the "ladder"), layered ON TOP of the capability gate
        # checked above: a permission can hold yet the tier relation still
        # governs WHOSE freeze may be cleared — no self-clear unless org-admin;
        # an admin's freeze only by an org-admin; an operator's by any admin.
        # Tiers resolve FAIL-CLOSED — a lookup gap never grants.
        from .clear_freeze_service import freeze_clear_ladder_block

        _floor = freeze_clear_ladder_block(
            root,
            approver_user_id=approver_user_id or "",
            target_user_id=str(getattr(target, "user_id", "") or ""),
        )
        if _floor is not None:
            try:
                hub.execution.record_event(
                    root,
                    event_kind="freeze_admin_clear_tier_refused",
                    source_kind="admin_clear_freeze",
                    session_id=target.session_id,
                    capability_name="admin_clear_freeze",
                    action_kind="tier_floor",
                    target_entity=target.request_id,
                    status="refused",
                    payload={
                        "approver_user_id": approver_user_id,
                        "target_user_id": str(getattr(target, "user_id", "") or ""),
                        "blocked_by": _floor["blocked_by"],
                    },
                    user_id=approver_user_id,
                )
            except Exception:
                pass
            return _floor

        # Clear through the ONE audited primitive (ledger-first ordering;
        # no decide/clear/audit split-brain). See ClearFreezeService.
        from .clear_freeze_service import ClearFreezeService

        result = ClearFreezeService().clear_with_audit(
            root,
            target_freeze=target,
            reason=reason,
            approver_label=approver_label,
            approver_user_id=approver_user_id,
            source_kind="admin_clear_freeze",
            cleared_event_kind="freeze_admin_cleared",
            permission_name=PERM_ADMIN_CLEAR_FREEZE,
            # This MCP tool is the AGENT's own unfreeze surface — an agent
            # clearing its own freeze is a self-cancel, so it STRIKES (not
            # resets). hub is in scope → the strike routes through
            # record_and_escalate (freeze-at-threshold intact). Operator
            # directive 2026-07-15.
            clear_origin="agent_self",
            hub=hub,
        )

        return {
            "ok": result.cleared and result.status == "cleared",
            "freeze_id": result.request_id,
            "session_id": result.session_id,
            "cleared": result.cleared,
            "escalation_status": result.escalation_status,
            "minted_grant": False,
            "status": result.status,
            "message": result.message,
        }

    from . import tool_interface as _ti_c20

    _ti_c20.register_impl("admin_clear_freeze", admin_clear_freeze)
