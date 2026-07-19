from __future__ import annotations

from pathlib import Path

from .access_gate import AccessGate
from .action_surface_service import ActionSurfaceService
from .capability_index_store import CapabilityIndexStore
from .code_index_store import CodeIndexStore
from .execution_index_store import ExecutionIndexStore
from .index_store import IndexStore
from .legacy_migration_service import LegacyMigrationService
from .managed_mode_service import ManagedModeService
from .memory_store import MemoryStore
from .policy_service import PolicyService
from .procedure_capability_link_store import ProcedureCapabilityLinkStore
from .procedure_index_store import ProcedureIndexStore
from .query_gate import QueryGateStore
from .rbac_store import RBACStore
from .related_project_service import RelatedProjectService
from .schema_index_store import SchemaIndexStore
from .session_store import SessionStore
from .skill_store import SkillStore
from .updater_service import UpdaterService
from .workflow_action_service import WorkflowActionService


class AidocsServiceHub:
    """Small composition root for the first MCP-backed AIDOCS services."""

    def __init__(self, templates_root: Path, script_root: Path | None = None) -> None:
        self.managed_mode = ManagedModeService()
        self.sessions = SessionStore(templates_root=templates_root)
        self.memory = MemoryStore()
        self.index = IndexStore(session_store=self.sessions)
        self.capabilities = CapabilityIndexStore()
        self.code = CodeIndexStore(session_store=self.sessions)
        # Memory-loop seal (2026-07-09): production hub.code_index — the
        # RFC-4 unit surface ai_recall clusters through, previously only
        # ever attached by tests. Delegates unknown attrs to hub.code.
        from .code_index_adapter import CodeIndexAdapter

        self.code_index = CodeIndexAdapter(self.code)
        self.execution = ExecutionIndexStore()
        self.procedures = ProcedureIndexStore()
        self.procedure_links = ProcedureCapabilityLinkStore()
        self.schema = SchemaIndexStore()
        self.skills = SkillStore()
        self.query_gate = QueryGateStore()
        self.access_gate = AccessGate()
        self.rbac = RBACStore()
        self.updater = UpdaterService(
            script_root=script_root or templates_root.parents[2] / "scripts",
        )
        self.legacy = LegacyMigrationService()
        self.related = RelatedProjectService()
        self.policy = PolicyService(self)

        self.action_surface = ActionSurfaceService(self)
        self.workflow = WorkflowActionService()

    def require_permission(
        self,
        project_root: Path,
        permission_name: str,
        *,
        scope_type: str = "global",
        scope_id: str | None = None,
        tool_name: str | None = None,
        extra_payload: dict | None = None,
    ) -> dict:
        """RBAC enforcement helper called from every MCP tool gate site.

        Returns a dict: {'ok': True} when allowed, {'ok': False, 'error':
        str, 'blocked_by': 'rbac'} when refused. Refusal path ALSO emits
        an rbac_denied event stamped with the acting user + permission +
        scope so compliance queries can list "who tried what and got
        blocked."

        Resolves the acting user via identity_resolver (per-process
        cached). Allow path still logs nothing — the successful tool
        call's own PostToolUse event provides the audit trail.

        Distribution flavor 'dev' + rbac.dev_mode_bypass env var both
        short-circuit to allow — pre-existing RBACStore behavior.
        """
        from .identity_resolver import (
            current_effective_role,
            current_user,
        )

        uid, _email, principal_type = current_user(project_root)
        effective_role = current_effective_role(project_root, uid)

        allowed = self.rbac.has_permission(
            project_root,
            uid,
            permission_name,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        if allowed:
            return {"ok": True}

        # Resolve the managed session id once up-front so both the
        # break-glass audit path below and the rbac_denied emitter
        # further down can attribute events to the active session.
        managed = self.managed_mode.get_mode(project_root)
        session_id = str(managed.get("session_id") or "").strip() if managed.get("active") else None

        # Pre-RBAC break-glass: if the project has no RBAC seed at all
        # (no roles, no users), there's nothing to enforce against —
        # allow and let bootstrap_local_superadmin run. A seeded
        # install with a real user record but no assignment still hits
        # the refusal path; this only yields on the truly fresh case.
        # This path is loud-audited: every bypass emits a
        # `rbac_bootstrap_bypass` event so an auditor can distinguish
        # "normal RBAC allow" from "no RBAC yet, allowed for bootstrap."
        try:
            from .identity_store import IdentityStore

            roles_empty = not self.rbac.list_roles(project_root)
            users_empty = not IdentityStore().list_users(project_root)
            if roles_empty and users_empty:
                try:
                    self.execution.record_event(
                        project_root,
                        event_kind="rbac_bootstrap_bypass",
                        source_kind="rbac_enforcement",
                        session_id=session_id,
                        capability_name=tool_name or "require_permission",
                        action_kind="rbac_bootstrap",
                        target_entity=permission_name,
                        status="allowed",
                        payload={
                            "tool_name": tool_name or "",
                            "required_permission": permission_name,
                            "acting_user": uid,
                            "effective_role": effective_role,
                            "scope_type": scope_type,
                            "scope_id": scope_id,
                            "reason": (
                                "RBAC store is empty (no roles, no users). "
                                "Bypass allowed once to let "
                                "bootstrap_local_superadmin seed the initial "
                                "super_admin. Every call after the first "
                                "bootstrap hits the normal RBAC path."
                            ),
                        },
                        user_id=uid,
                        effective_role=effective_role,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        permission_name=permission_name,
                        principal_type=principal_type,
                    )
                except Exception:
                    pass
                return {"ok": True, "pre_rbac_bootstrap": True}
        except Exception:
            pass

        payload = {
            "tool_name": tool_name or "",
            "required_permission": permission_name,
            "acting_user": uid,
            "effective_role": effective_role,
            "scope_type": scope_type,
            "scope_id": scope_id,
        }
        if extra_payload:
            payload.update(extra_payload)
        try:
            self.execution.record_event(
                project_root,
                event_kind="rbac_denied",
                source_kind="rbac_enforcement",
                session_id=session_id,
                capability_name=tool_name or "require_permission",
                action_kind="rbac_check",
                target_entity=permission_name,
                status="refused",
                payload=payload,
                user_id=uid,
                effective_role=effective_role,
                scope_type=scope_type,
                scope_id=scope_id,
                permission_name=permission_name,
                principal_type=principal_type,
            )
        except Exception:
            pass
        return {
            "ok": False,
            "error": (
                f"RBAC denied: user {uid!r} (role={effective_role}) "
                f"lacks {permission_name!r} at scope "
                f"{scope_type}:{scope_id or '-'}. "
                f"Ask an admin to grant the permission or assign a "
                f"stronger role."
            ),
            "blocked_by": "rbac",
            "required_permission": permission_name,
            "acting_user": uid,
            "effective_role": effective_role,
        }
