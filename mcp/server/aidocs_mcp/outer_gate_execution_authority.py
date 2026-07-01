"""Tier-M execution authority — WHO may execute a MUTATING (Tier-M / Tier-A)
tool on the remote WebMCP surface (Phase 2b, king 2026-06-21).

This is the PRINCIPAL layer of the Tier-M execution stack. The full stack, once
execution opens:

  1. TOOL eligibility  — ``entry.remote_eligible`` (binding ∈ PROVEN_BINDINGS).
     Already enforced by the gate; a non-eligible tool never reaches here.
  2. PRINCIPAL authority — THIS module: is the authenticated caller allowed to
     run a mutating tool in the bound tenant?
  3. AUDIT ordering     — outer_gate_audit (intent-before / result-after).
  4. DESTRUCTIVE floor  — refuses dangerous shapes (rm -rf / curl|sh) even for a
     fully-authorized operator (a later phase).

THE BAR (king choice 2026-06-21): a DEDICATED org role — ``OPERATOR`` — may
execute Tier-M, in ADDITION to org OWNER / ADMIN and the platform super_admin.
OPERATOR may NOT perform org-admin control-plane (config_set / membership
grant-revoke / session_delete): those stay ``is_org_admin``-gated, so an operator
can RUN tools but cannot re-shape the org or its membership. The role is assigned
in codenexus (``TeamMember.role``) — the gate recognizes + authorizes it; the
resolver passes the role string through verbatim (codenexus_identity:list_user_orgs).

FORGE RESISTANCE: this is a PURE function of the SERVER-RESOLVED principal (the
bearer-token-validated identity + codenexus membership the transport built). It
reads ONLY ``authenticated`` / ``effective_role`` / ``org_role`` / ``tenant_id``
— never agent-supplied tool arguments. An agent cannot grant itself authority by
putting ``"role": "OWNER"`` (or any field) in its call arguments, because those
arguments never become the principal.

This module is PURE and side-effect-free so it can be unit-pinned in isolation,
and is consulted at the gate's execution decision only when the door opens.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import outer_gate_manifest as manifest
from .outer_gate_project_acl import is_org_admin, org_role

# Org roles permitted to EXECUTE a Tier-M / Tier-A tool. A superset of the admin
# roles (an admin can do everything an operator can) PLUS the dedicated OPERATOR.
# Control-plane authority is the STRICTER ``is_org_admin`` (OWNER/ADMIN/super_admin)
# — OPERATOR is deliberately absent there.
EXECUTION_ROLES = frozenset({"OWNER", "ADMIN", "OPERATOR"})


@dataclass
class ExecAuthz:
    """Outcome of the Tier-M execution authority check.

    allowed — True iff the principal may execute a mutating tool in the tenant.
    reason  — "" when allowed; a stable refusal code (``tier_m_*``) otherwise, so
              the gate's audit records WHY a mutation was refused.
    """

    allowed: bool
    reason: str = ""


def tier_m_execution_authorized(principal: dict | None, *, exec_tenant_id: str = "") -> ExecAuthz:
    """Decide whether ``principal`` may execute a MUTATING tool bound to
    ``exec_tenant_id``. Pure; reads only server-resolved principal fields.
    """
    # 1. A real, server-validated, AUTHENTICATED identity. Agent-driven / local /
    #    unauthenticated calls carry no ``authenticated`` flag → DENIED. The
    #    forge-proof signal is the bearer-token validation the transport already
    #    performed, NOT a field a caller can set in arguments.
    if not isinstance(principal, dict) or not principal.get("authenticated"):
        return ExecAuthz(False, "tier_m_requires_authenticated_operator")

    # 2. A resolvable remote identity (fail-closed: needs user_id + role; a
    #    super_admin must be authenticated — already true past step 1).
    resolved = manifest.resolve_remote_principal(principal)
    if not resolved["ok"]:
        return ExecAuthz(False, f"tier_m_{resolved['reason']}")

    # 3. RBAC role: the dedicated OPERATOR, an org admin (OWNER/ADMIN), or the
    #    platform super_admin. MEMBER / VIEWER / unknown → refused.
    if org_role(principal) not in EXECUTION_ROLES and not is_org_admin(principal):
        return ExecAuthz(False, "tier_m_role_not_permitted")

    # 4. Tenant isolation: the operator executes WITHIN the bound tenant. A
    #    principal tenant that disagrees with the execution tenant is a cross-tenant
    #    misroute → BLOCKED. (Both-empty = local/legacy single-tenant, reachable
    #    only by a super_admin past step 3 — no tenant to cross.)
    principal_tenant = str(principal.get("tenant_id") or "")
    exec_tenant = str(exec_tenant_id or "")
    if exec_tenant and principal_tenant and exec_tenant != principal_tenant:
        return ExecAuthz(False, "tier_m_cross_tenant_blocked")

    return ExecAuthz(True, "")
