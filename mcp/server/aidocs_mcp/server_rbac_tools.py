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
        approver_email: str,
        reason: str = "",
        grant_ttl_seconds: int = 300,
        grant_max_uses: int = 1,
        strict_command_match: bool = False,
    ) -> dict[str, Any]:
        """Approve a pending escalation request by its request_id.

        The approver must already exist in identity_store and hold
        rbac.approve_escalations plus the gate permission being
        unlocked (you cannot approve what you couldn't grant).

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
        from .identity_store import IdentityStore
        from .permission_catalog import PERM_RBAC_APPROVE_ESCALATIONS
        from .rbac_store import RBACStore

        root = resolve_project_root()
        identity = IdentityStore()
        rbac = RBACStore()
        escalations = EscalationStore()

        target = escalations.get(root, request_id)
        if target is None:
            return {"ok": False, "error": f"unknown request: {request_id}"}
        approver = identity.get_user_by_email(root, approver_email)
        if approver is None:
            return {"ok": False, "error": "unknown approver"}
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
                        "approver_email": approver_email,
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
        freeze_cleared = 0
        try:
            from .session_freeze_store import SessionFreezeStore

            freeze_cleared = SessionFreezeStore().clear_freeze_by_request(
                root,
                request_id,
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
        approver_email: str,
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
        """
        from .escalation_store import EscalationStore
        from .identity_store import IdentityStore
        from .permission_catalog import PERM_RBAC_APPROVE_ESCALATIONS
        from .rbac_store import RBACStore

        root = resolve_project_root()
        identity = IdentityStore()
        rbac = RBACStore()
        escalations = EscalationStore()
        if not approver_email:
            return {"ok": False, "error": "approver_email required"}
        approver = identity.get_user_by_email(root, approver_email)
        if approver is None:
            return {"ok": False, "error": "unknown approver"}
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
                        "approver_email": approver_email,
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
        freeze_cleared = 0
        try:
            from .session_freeze_store import SessionFreezeStore

            freeze_cleared = SessionFreezeStore().clear_freeze_by_request(
                root,
                request_id,
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

    @server.tool(
        annotations={
            "destructiveHint": False,
            "openWorldHint": False,
            "title": "Admin Clear Freeze (Break-Glass)",
        },
    )
    def admin_clear_freeze(
        freeze_id: str = "",
        session_id: str = "",
        approver_email: str = "",
        reason: str = "",
        confirm_token: str = "",
    ) -> dict[str, Any]:
        """Break-glass: clear a session freeze WITHOUT minting a grant.

        Operation class: `remedial_freeze_management` (hardcoded in
        operation_classes.py). The freeze gate is skipped by doctrine
        — this tool is how the operator EXITS a freeze.

        Auth: caller must hold `rbac.admin_clear_freeze` permission
        OR the dev kill_switch must be active. Without either the
        call is refused with audit.

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
        expected_confirm = (
            f"confirm-clear-freeze {freeze_id}"
            if freeze_id
            else f"confirm-clear-freeze session:{session_id}"
        )
        if (confirm_token or "").strip() != expected_confirm:
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

        from .enforcement import is_kill_switch_active
        from .identity_store import IdentityStore
        from .permission_catalog import PERM_ADMIN_CLEAR_FREEZE
        from .rbac_store import RBACStore
        from .session_freeze_store import SessionFreezeStore

        root = resolve_project_root()

        kill_switch_on = is_kill_switch_active(root)
        approver_user_id: str | None = None
        approver_label = "admin-clear-freeze"
        if not kill_switch_on:
            if not approver_email:
                return {
                    "ok": False,
                    "blocked_by": "no_admin_identity",
                    "error": (
                        "admin_clear_freeze requires approver_email "
                        "(or dev.kill_switch=true). Refusing."
                    ),
                }
            identity = IdentityStore()
            approver = identity.get_user_by_email(root, approver_email)
            if approver is None:
                return {
                    "ok": False,
                    "blocked_by": "unknown_approver",
                    "error": f"unknown approver: {approver_email}",
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
                            "approver_email": approver_email,
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
            approver_user_id = approver.user_id
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
            permission_name=(PERM_ADMIN_CLEAR_FREEZE if not kill_switch_on else None),
            kill_switch_bypass=kill_switch_on,
        )

        return {
            "ok": result.cleared and result.status == "cleared",
            "freeze_id": result.request_id,
            "session_id": result.session_id,
            "cleared": result.cleared,
            "escalation_status": result.escalation_status,
            "minted_grant": False,
            "kill_switch_bypass": kill_switch_on,
            "status": result.status,
            "message": result.message,
        }
