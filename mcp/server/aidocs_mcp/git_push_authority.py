"""`git.push` — the dedicated push capability for gate principals.

STRICTER THAN tier_m_edit. A gate `ai_git(op='push')` needs ALL of:
  * `tier_m_edit` (the existing edit tier),
  * `git.push` (this capability),
  * a selected session.

GRANT (token issuance): default-granted ONLY to an org OWNER/ADMIN (or, on a
local gate with no org, a platform admin/super_admin) — and only onto a token
that already carries `tier_m_edit`. An ordinary member receives it only when
it is explicitly requested AND the OAuth client's operator-registered scope
ceiling carries it.

ENFORCEMENT (#1060 r0b) is fail-closed and LIVE, per push. The token scope is
necessary but never sufficient (a claim is a hint, not authority):
  * the TARGET project + its org are resolved server-side at use from the
    authenticated gate selection (project_binding_resolver.resolve — the one identity door, #990);
  * the principal's CURRENT role in THAT org is looked up (codenexus
    membership) on every push — a lookup failure refuses;
  * OWNER/ADMIN ⇒ basis default_role; a MEMBER ⇒ only a still-active explicit
    grant in the per-tenant project ACL store (ProjectAclStore capability row
    bound to user + project + org + git.push) ⇒ basis explicit_grant;
  * refresh re-derives git.push the same way (derive_push_scope) and never copies it.
Authority is never inferred from the shared org GitHub credential being resolvable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .outer_gate_scopes import SCOPE_GIT_PUSH, SCOPE_TIER_M_EDIT

ISSUANCE_ADMIN_ORG_ROLES = frozenset({"OWNER", "ADMIN"})
ISSUANCE_ADMIN_PLATFORM_ROLES = frozenset({"admin", "super_admin"})


def default_grants_git_push(*, platform_role: str, org_role: str) -> bool:
    """Does this minter get `git.push` by default at issuance?"""
    o = str(org_role or "").strip().upper()
    if o:
        # A bound org's own membership row decides; a MEMBER/VIEWER row vetoes a
        # platform admin role (same comparison doctrine as org_admin_verdict).
        return o in ISSUANCE_ADMIN_ORG_ROLES
    return str(platform_role or "").strip().lower() in ISSUANCE_ADMIN_PLATFORM_ROLES

# token_push_scope_ok() lived here until 2026-09-16 (r0b a64e9e59-fe4). It asked
# whether a token carried the MINTED push path (tier_m_edit AND git.push) so
# admission could shortcut authority for service principals. There is no such
# shortcut any more: push authority is decided ONCE, per use, by
# gate_git_ops.run_gate_git_op from CURRENT role/grant, for every principal
# alike. A service principal simply passes that same check. Vulture flagged it
# the moment admission stopped deciding, and a dead predicate that still reads
# like an authority check is an invitation to re-wire one.


# `push_refusal` (the old token-scope wall) is DELETED, not deprecated: #1060 v3
# left it with no production caller, and a dead predicate that still reads like
# an authority check is the kind of thing a later reader re-wires by mistake.
# Only `push_session_refusal` (below) survives of it; the scope half went the
# same way on 2026-09-16, once admission stopped deciding authority at all.


def push_session_refusal(principal: dict | None) -> str:
    """The session guard every push path shares. '' when a session is selected.

    Separated from the scope wall (#1060 v3) because request-derived authority
    must still bind to a selected session: writes bind to a session within the
    selected project, exactly as the C2 edit guard requires.
    """
    if not isinstance(principal, dict):
        return "git push on the gate surface requires an authenticated gate principal"
    if not str(principal.get("session_id") or "").strip():
        return "git push requires a selected session"
    return ""


# ── #1060 r0b: live per-push authority ─────────────────────────────────────
BASIS_DEFAULT_ROLE = "default_role"
BASIS_EXPLICIT_GRANT = "explicit_grant"
BASIS_REFUSED = "refused"

#: A push with no selected session. Its OWN reason (#1060 v3): it is written to
#: the FORENSIC ledger as authority_reason, and reporting it as `token_scope`
#: would misattribute a session failure to a scope failure forever.
# (REASON_NO_SESSION lived here until 2026-09-16. Authority no longer answers
# the session question, so the constant had no production caller left; the
# session refusal each CALLER raises carries its own wording. See
# live_push_authority.)
REASON_NO_PRINCIPAL = "no_gate_principal"
REASON_NO_TENANT_CONTEXT = "gate_no_tenant_context"
REASON_TARGET_UNRESOLVED = "target_project_unresolved"
REASON_TARGET_TENANT_MISMATCH = "target_tenant_mismatch"
REASON_TARGET_PROJECT_HAS_NO_ORG = "target_project_has_no_org"
REASON_ORGLESS_REQUIRES_PLATFORM_ADMIN = "orgless_push_requires_platform_admin"
REASON_MEMBERSHIP_LOOKUP_FAILED = "membership_lookup_failed"
REASON_NOT_A_MEMBER = "not_a_member_of_target_tenant"
REASON_NO_ACTIVE_EXPLICIT_GRANT = "no_active_explicit_grant"
REASON_EXPLICIT_GRANT_LOOKUP_FAILED = "explicit_grant_lookup_failed"
REASON_CLOUD_GATE_REQUIRES_TENANT = "cloud_gate_push_requires_tenant"


def tenant_authority_configured() -> bool:
    """Is a live tenant membership authority configured (cloud/webmcp gate)?"""
    return bool(os.environ.get("AIDOCS_CODENEXUS_DSN", "").strip())


class MembershipLookupError(RuntimeError):
    """The live membership authority could not answer (never means 'not a member')."""


@dataclass(frozen=True)
class PushAuthority:
    ok: bool
    basis: str
    reason: str = ""
    detail: str = ""
    target_tenant: str = ""
    project_id: str = ""
    org_role: str = ""

    def as_payload(self) -> dict:
        return {
            "authority_basis": self.basis,
            "authority_reason": self.reason,
            "target_tenant": self.target_tenant,
            "target_project_id": self.project_id,
            "live_org_role": self.org_role,
        }


def _default_list_user_orgs(user_id: str) -> list:
    dsn = os.environ.get("AIDOCS_CODENEXUS_DSN", "").strip()
    if not dsn:
        raise MembershipLookupError("no codenexus membership authority configured")
    from .codenexus_identity import CodenexusPostgresResolver

    return CodenexusPostgresResolver(dsn=dsn).list_user_orgs(user_id)


def live_org_role(user_id: str, tenant_id: str, *, list_user_orgs=None) -> str:
    """CURRENT role of ``user_id`` in ``tenant_id``: ROLE, or '' when not a member.

    Raises MembershipLookupError when the authority cannot answer."""
    fn = list_user_orgs or _default_list_user_orgs
    try:
        orgs = fn(user_id)
    except MembershipLookupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MembershipLookupError(f"{type(exc).__name__}") from exc
    tid = str(tenant_id or "").strip()
    for o in orgs or []:
        if str((o or {}).get("org_id") or "") == tid:
            return str(o.get("org_role") or "").strip().upper() or "MEMBER"
    return ""


def _default_explicit_grant(home, user_id: str, project_id: str, org_id: str) -> bool:
    from .outer_gate_project_acl import ProjectAclStore

    return ProjectAclStore().has_capability(
        Path(home), user_id, project_id, SCOPE_GIT_PUSH, org_id=org_id
    )


def _default_resolve_binding(principal: dict):
    """The target project through THE ONE identity door (#990): ``resolve()``.

    ``resolve()`` answers from the dispatch's authenticated gate principal and
    its authoritative selection — never from a path. A push decided outside a
    gate dispatch, or for a principal that is not the dispatched one, resolves
    NOTHING (fail-closed); it never falls through to a local, path-derived
    answer."""
    from .project_binding_resolver import SOURCE_GATE, ProjectBinding, gate_principal, resolve

    dispatched = gate_principal()
    if dispatched is None or str(dispatched.get("user_id") or "").strip() != str(
        principal.get("user_id") or ""
    ).strip():
        return ProjectBinding(source=SOURCE_GATE, reason="gate_principal_not_dispatched")
    return resolve()


def live_push_authority(
    principal: dict | None,
    *,
    resolve_binding=None,
    list_user_orgs=None,
    explicit_grant=None,
) -> PushAuthority:
    """THE per-push authority decision. Fail-closed; every refusal names its reason."""

    def refuse(reason: str, detail: str = "", **kw) -> PushAuthority:
        return PushAuthority(False, BASIS_REFUSED, reason, detail, **kw)

    # #1060 v3 (operator + r0b, 2026-09-16): the TOKEN-SCOPE wall is no longer a
    # precondition here. Authority is CURRENT authority — owner/admin of the
    # bound tenant, or an active explicit grant on the target project — and a
    # minted git.push scope neither grants it nor is required for it. What every
    # path still shares is the session binding.
    # THE SESSION BINDING IS NOT ASKED HERE (measured live on build 287,
    # 2026-09-16). This function runs at BOTH stages, and at admission the
    # principal is not session-bound yet — asking it there refused a web actor
    # whose session demonstrably existed (ai_session(status) → ubermega,
    # staged gating session_selected: true) with
    # `insufficient_scope … (no_session_selected)`. It is the same staging
    # mistake as the admission guard, one layer up.
    #
    # Nothing is loosened: the session is enforced by BOTH callers, each from
    # the source that is true at its own stage — outer_gate._oge_scope_admission
    # guards on request.session_id, and gate_git_ops.run_gate_git_op calls
    # push_session_refusal on the bound principal immediately before this. A
    # push still cannot happen without a selected session.
    if not isinstance(principal, dict):
        return refuse(REASON_NO_PRINCIPAL, "git push requires an authenticated gate principal")
    uid = str(principal.get("user_id") or "").strip()
    from .project_binding_resolver import GATE_HOME_KEY

    home = str(principal.get(GATE_HOME_KEY) or "").strip()
    try:
        binding = (resolve_binding or _default_resolve_binding)(principal)
    except Exception as exc:  # noqa: BLE001
        return refuse(REASON_TARGET_UNRESOLVED, type(exc).__name__)
    pid = str(getattr(binding, "project_id", "") or "").strip()
    if not pid:
        return refuse(
            f"{REASON_TARGET_UNRESOLVED}:{getattr(binding, 'reason', '') or 'unknown'}"
        )
    target = str(getattr(binding, "org_id", "") or "").strip()
    principal_tenant = str(principal.get("tenant_id") or "").strip()
    if not target:
        if principal_tenant:
            return refuse(REASON_TARGET_PROJECT_HAS_NO_ORG, project_id=pid)
        # Platform-role fallback ONLY for a genuinely org-less LOCAL gate: no
        # org on the project, none on the request, AND no tenant membership
        # authority configured to consult. A cloud gate never lands here.
        if tenant_authority_configured():
            return refuse(REASON_CLOUD_GATE_REQUIRES_TENANT, project_id=pid)
        if default_grants_git_push(
            platform_role=str(principal.get("effective_role") or ""), org_role=""
        ):
            return PushAuthority(True, BASIS_DEFAULT_ROLE, project_id=pid)
        return refuse(REASON_ORGLESS_REQUIRES_PLATFORM_ADMIN, project_id=pid)
    if principal_tenant != target:
        return refuse(
            REASON_TARGET_TENANT_MISMATCH,
            f"request tenant {principal_tenant!r} is not the project's org {target!r}",
            target_tenant=target,
            project_id=pid,
        )
    ctx = {"target_tenant": target, "project_id": pid}
    try:
        role = live_org_role(uid, target, list_user_orgs=list_user_orgs)
    except MembershipLookupError as exc:
        return refuse(REASON_MEMBERSHIP_LOOKUP_FAILED, str(exc), **ctx)
    if not role:
        return refuse(REASON_NOT_A_MEMBER, **ctx)
    if role in ISSUANCE_ADMIN_ORG_ROLES:
        return PushAuthority(True, BASIS_DEFAULT_ROLE, org_role=role, **ctx)
    if not home:
        return refuse(REASON_NO_TENANT_CONTEXT, org_role=role, **ctx)
    try:
        granted = bool((explicit_grant or _default_explicit_grant)(home, uid, pid, target))
    except Exception as exc:  # noqa: BLE001
        return refuse(REASON_EXPLICIT_GRANT_LOOKUP_FAILED, type(exc).__name__, org_role=role, **ctx)
    if granted:
        return PushAuthority(True, BASIS_EXPLICIT_GRANT, org_role=role, **ctx)
    return refuse(REASON_NO_ACTIVE_EXPLICIT_GRANT, org_role=role, **ctx)


def _default_has_any_explicit(gate_root, tenant_id: str, user_id: str) -> bool:
    from .outer_gate_project_acl import ProjectAclStore
    from .outer_gate_tenancy import tenant_home

    home = tenant_home(Path(gate_root), tenant_id, create=False)
    return ProjectAclStore().has_any_capability(home, user_id, SCOPE_GIT_PUSH, org_id=tenant_id)


def derive_push_scope(
    scope,
    *,
    user_id: str,
    platform_role: str,
    tenant_id: str,
    gate_root,
    list_user_orgs=None,
    has_explicit=None,
) -> tuple[frozenset, str]:
    """THE one git.push derivation for CODE EXCHANGE and REFRESH (#1060 r0b).

    Strip git.push; re-add only on CURRENT authority (live owner/admin of the
    bound tenant, or an active explicit grant there). A requested or previously
    minted git.push counts for nothing. Returns (scope, basis-hint)."""
    s = frozenset(scope or ()) - {SCOPE_GIT_PUSH}
    if SCOPE_TIER_M_EDIT not in s:
        return s, ""
    tid = str(tenant_id or "").strip()
    if not tid:
        # Org-less LOCAL gate only; a cloud gate token without a tenant gets none.
        if tenant_authority_configured():
            return s, ""
        if default_grants_git_push(platform_role=platform_role, org_role=""):
            return s | {SCOPE_GIT_PUSH}, BASIS_DEFAULT_ROLE
        return s, ""
    try:
        role = live_org_role(user_id, tid, list_user_orgs=list_user_orgs)
    except MembershipLookupError:
        return s, ""
    if not role:
        return s, ""
    if role in ISSUANCE_ADMIN_ORG_ROLES:
        return s | {SCOPE_GIT_PUSH}, BASIS_DEFAULT_ROLE
    try:
        if (has_explicit or _default_has_any_explicit)(gate_root, tid, user_id):
            return s | {SCOPE_GIT_PUSH}, BASIS_EXPLICIT_GRANT
    except Exception:  # noqa: BLE001
        return s, ""
    return s, ""


# ── #1060 r0b: the explicit member git.push grant/revoke door ───────────────────────
EVENT_GIT_PUSH_GRANT = "git_push_grant"
EVENT_GIT_PUSH_REVOKE = "git_push_revoke"

REASON_BAD_OP = "bad_op"
REASON_NO_ORG_BOUND = "no_org_bound"
REASON_ACTOR_LOOKUP_FAILED = "actor_membership_lookup_failed"
REASON_ORG_ADMIN_REQUIRED = "org_admin_required"
REASON_UNKNOWN_PROJECT = "unknown_project"
REASON_PROJECT_NOT_IN_TENANT = "project_not_in_tenant"
REASON_MEMBER_LOOKUP_FAILED = "member_membership_lookup_failed"
REASON_CROSS_ORG_GRANT = "cross_org_grant_refused"
REASON_NO_PROJECT_ACCESS = "member_lacks_project_access"
REASON_AUDIT_UNAVAILABLE = "audit_unavailable"
REASON_SELF_GRANT_FORBIDDEN = "self_grant_forbidden"


def mutate_push_grant(
    op: str,
    *,
    home,
    actor_user_id: str,
    member_user_id: str,
    project_id: str,
    tenant_id: str,
    project_row: dict | None,
    recorder,
    list_user_orgs=None,
    store=None,
) -> dict:
    """Grant/revoke an explicit member git.push on ONE project in ONE tenant.

    Authority: the actor's CURRENT OWNER/ADMIN role in ``tenant_id`` (live
    lookup, fail-closed). The grant is bound to member + project + tenant +
    git.push. The audit row is CRITICAL and lands BEFORE the mutation; no audit
    ⇒ no mutation. Refusals are audited best-effort. ``recorder(home, **fields)``
    has ExecutionIndexStore.record_event's shape.
    """
    o = str(op or "").strip().lower()
    actor = str(actor_user_id or "").strip()
    member = str(member_user_id or "").strip()
    pid = str(project_id or "").strip()
    tid = str(tenant_id or "").strip()
    base = dict(
        source_kind="project_acl", capability_name=SCOPE_GIT_PUSH,
        action_kind=o or "unknown", target_entity=f"{tid}/{pid}/{member}",
        principal_type="user", user_id=actor,
    )
    payload = {"actor": actor, "member_user_id": member, "project_id": pid,
               "tenant_id": tid, "capability": SCOPE_GIT_PUSH}

    def emit(**fields) -> None:
        if o == "grant":
            recorder(home, event_kind=EVENT_GIT_PUSH_GRANT, **fields)
        else:
            recorder(home, event_kind=EVENT_GIT_PUSH_REVOKE, **fields)

    def refuse(reason: str, detail: str = "") -> dict:
        try:
            emit(status="refused",
                     payload={**payload, "result": "refused", "reason": reason, "detail": detail},
                     **base)
        except Exception:  # noqa: BLE001 — refusal already happened
            pass
        return {"_error": reason, "_detail": detail or reason}

    if o not in ("grant", "revoke"):
        return refuse(REASON_BAD_OP, f"op must be grant|revoke, got {op!r}")
    if not tid:
        return refuse(REASON_NO_ORG_BOUND, "explicit git.push grants are per-org")
    try:
        actor_role = live_org_role(actor, tid, list_user_orgs=list_user_orgs)
    except MembershipLookupError as exc:
        return refuse(REASON_ACTOR_LOOKUP_FAILED, str(exc))
    if actor_role not in ISSUANCE_ADMIN_ORG_ROLES:
        return refuse(REASON_ORG_ADMIN_REQUIRED,
                      f"only a current OWNER/ADMIN of {tid!r} may {o} git.push")
    if not member or not pid:
        return refuse(REASON_BAD_OP, "member_user_id and project_id are required")
    if o == "grant" and member == actor:
        # Consul ruling r0a+r0b: an OWNER/ADMIN cannot issue git.push to themself.
        return refuse(REASON_SELF_GRANT_FORBIDDEN,
                      "git.push must be granted by a different OWNER/ADMIN")
    if project_row is None:
        return refuse(REASON_UNKNOWN_PROJECT, f"no such project in this org: {pid}")
    if str(project_row.get("org_id") or "").strip() != tid:
        return refuse(REASON_PROJECT_NOT_IN_TENANT, f"project {pid} is not bound to {tid!r}")
    if store is None:
        from .outer_gate_project_acl import ProjectAclStore

        store = ProjectAclStore()
    if o == "grant":
        try:
            member_role = live_org_role(member, tid, list_user_orgs=list_user_orgs)
        except MembershipLookupError as exc:
            return refuse(REASON_MEMBER_LOOKUP_FAILED, str(exc))
        if not member_role:
            return refuse(REASON_CROSS_ORG_GRANT, f"{member} is not a member of {tid!r}")
        if pid not in store.allowed_ids(Path(home), member):
            return refuse(REASON_NO_PROJECT_ACCESS,
                          "grant project access first (project_grant_member)")
    try:
        emit(status="granted" if o == "grant" else "revoked",
                 payload={**payload, "result": "granted" if o == "grant" else "revoked",
                          "actor_org_role": actor_role}, **base)
    except Exception as exc:  # noqa: BLE001
        return {"_error": REASON_AUDIT_UNAVAILABLE,
                "_detail": f"git.push {o} refused: audit row not written ({type(exc).__name__})"}
    if o == "grant":
        store.grant_capability(Path(home), member, pid, SCOPE_GIT_PUSH, org_id=tid,
                               granted_by=actor)
        return {"ok": True, "granted": True, "capability": SCOPE_GIT_PUSH,
                "member_user_id": member, "project_id": pid}
    removed = store.revoke_capability(Path(home), member, pid, SCOPE_GIT_PUSH)
    return {"ok": True, "revoked": removed, "capability": SCOPE_GIT_PUSH,
            "member_user_id": member, "project_id": pid}


def pull_refusal(principal: dict | None) -> str:
    """'' when a GATE principal may pull (edit tier + selected session)."""
    if not isinstance(principal, dict):
        return "git pull on the gate surface requires an authenticated gate principal"
    if SCOPE_TIER_M_EDIT not in set(principal.get("scope") or []):
        return f"token lacks {SCOPE_TIER_M_EDIT} scope"
    if not str(principal.get("session_id") or "").strip():
        return "git pull requires a selected session"
    return ""
