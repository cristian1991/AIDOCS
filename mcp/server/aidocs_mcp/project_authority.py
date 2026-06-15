"""Project-boundary authority — one gate for self-escalation defense.

AIDOCS enforcement keys off *which project* governs the agent. That makes
the project boundary itself a privilege surface: if a low-trust caller can
adopt/commission a tree, init a fake sub-project inside a real one, or
silently rebind the conductor into a more-privileged project, it can turn
AIDOCS's own enforcement into an escalation (a confused deputy).

This module is the single place that answers "may this principal perform
this project-boundary operation on this root?". The model:

  * solo / dev flavor → LOCAL-ADMIN passthrough. The physically-present
    operator on their own machine is the admin (mirrors operator_auth's
    local-token auto-mint: corpo requires login, solo/dev do not).
  * corpo flavor → RBAC: the caller must hold the relevant admin
    permission for the project scope.

Operations:
  * require_admin            — adoption / commissioning (/aidocs).
  * assert_no_commissioned_ancestor — refuse nested project_init.
  * require_cross_project    — register / list-sessions / handoff / spawn /
    connect into a DIFFERENT project: needs the target COMMISSIONED, an
    APPROVED relation (security.approved_external_roots), AND permission.
  * session_belongs          — a local bind must target a session that
    actually lives in the active project.

Plain project *listing* stays open (low-friction, names only) — the
boundary is on adopt / cross / bind, not enumeration. Every gated
decision is audited.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .permission_catalog import (
    PERM_ADMIN_MANAGE_CONFIG,
    PERM_ADMIN_MANAGE_SESSIONS,
)

# Flavors whose physically-present operator IS the admin (no login).
LOCAL_ADMIN_FLAVORS = frozenset({"dev", "solo"})

_MARKER_REL = Path(".MEMORY") / ".aidocs" / "index.aidocs"


class Decision(dict):
    """{ok, reason, flavor, ...} — truthy on ``ok``; keys also readable as
    attributes (``d.ok``, ``d.reason``).
    """

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return bool(self.get("ok"))

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _flavor() -> str:
    """Install flavor, read GLOBAL-only (a project/session row can never
    elevate the install). Fail-closed to 'corpo' (the strict flavor) so a
    config-read error never silently grants the local-admin passthrough.
    """
    try:
        from .config import get_setting

        return str(get_setting("distribution.flavor", default="solo") or "").strip().lower()
    except Exception:
        return "corpo"


def is_local_admin_flavor() -> bool:
    return _flavor() in LOCAL_ADMIN_FLAVORS


def _authenticated_uid(project_root: Path, host_session_id: str = "") -> str:
    """Resolve the AUTHENTICATED operator user_id, or "" if unauthenticated.

    Authority comes ONLY from a dashboard bearer token
    (AIDOCS_OPERATOR_TOKEN) or an APPROVED host-session binding — both
    real authentications. It deliberately does NOT consult
    identity_resolver / current_user(_id): that surface is audit
    ATTRIBUTION only and falls back to env identity or a bootstrapped
    local user, so using it for authorization would let an
    unauthenticated corpo caller (with a stray env identity or a
    first-run local user) pass. Fail-closed everywhere.
    """
    import os

    try:
        from .operator_auth_service import OperatorAuthService

        token = os.environ.get("AIDOCS_OPERATOR_TOKEN", "").strip()
        if token:
            ctx = OperatorAuthService().authenticate(token, project_root, source="dashboard")
            if ctx is not None and ctx.user_id:
                return ctx.user_id
    except Exception:
        pass
    try:
        if host_session_id:
            from .host_operator_binding_store import HostOperatorBindingStore

            uid = HostOperatorBindingStore().resolve_operator(project_root, host_session_id)
            if uid:
                return uid
    except Exception:
        pass
    return ""


def _has_perm(
    project_root: Path,
    permission: str,
    host_session_id: str = "",
) -> bool:
    """Corpo RBAC check for the AUTHENTICATED caller only. No authenticated
    operator (token / approved binding) ⇒ False, regardless of any env
    audit identity or bootstrapped local user. Fail-closed on error.
    """
    uid = _authenticated_uid(project_root, host_session_id)
    if not uid:
        return False
    try:
        from .rbac_store import RBACStore

        return RBACStore().has_permission(project_root, uid, permission)
    except Exception:
        return False


def _norm(p: Path) -> str:
    try:
        return str(Path(p).resolve()).replace("\\", "/").rstrip("/").lower()
    except Exception:
        return str(p).replace("\\", "/").rstrip("/").lower()


def _is_commissioned(root: Path) -> bool:
    try:
        from .project_commission import is_commissioned

        return is_commissioned(root)
    except Exception:
        return (root / _MARKER_REL).is_file()


def _approved_roots(project_root: Path) -> list[Path]:
    """security.approved_external_roots (operator-curated). Empty by
    default → no cross-project relation is approved (fail closed).
    """
    raw: Any = None
    try:
        from .config import get_setting

        raw = get_setting(
            "security.approved_external_roots",
            project_root=project_root,
            default=None,
        )
    except Exception:
        raw = None
    out: list[Path] = []
    if isinstance(raw, str):
        raw = [c for c in raw.replace(";", ",").split(",") if c.strip()]
    if isinstance(raw, (list, tuple)):
        for c in raw:
            s = str(c).strip()
            if s:
                out.append(Path(s))
    return out


def _under(target: Path, root: Path) -> bool:
    t, r = _norm(target), _norm(root)
    return t == r or t.startswith(r + "/")


# ── audit ───────────────────────────────────────────────────────────


def audit(
    project_root: Path,
    *,
    operation: str,
    target: str,
    status: str,
    reason: str = "",
) -> None:
    """Record a project-boundary decision in the execution index.
    Best-effort — never breaks the caller.
    """
    try:
        from .execution_index_store import ExecutionIndexStore
        from .identity_resolver import current_user

        uid, _email, ptype = current_user(project_root)
        ExecutionIndexStore().record_event(
            project_root,
            event_kind="project_boundary",
            source_kind="project_authority",
            capability_name=operation,
            action_kind="boundary_decision",
            target_entity=target,
            status=status,
            user_id=uid or None,
            principal_type=ptype,
            payload={
                "operation": operation,
                "target": target,
                "status": status,
                "reason": reason,
                "flavor": _flavor(),
            },
        )
    except Exception:
        pass


# ── gates ───────────────────────────────────────────────────────────


def require_admin(
    project_root: Path,
    *,
    permission: str = PERM_ADMIN_MANAGE_CONFIG,
    operation: str = "adopt",
    host_session_id: str = "",
) -> Decision:
    """Adoption / commissioning gate. solo/dev → allow; corpo → RBAC on an
    AUTHENTICATED operator (token / approved binding) — never audit
    identity.
    """
    flavor = _flavor()
    if flavor in LOCAL_ADMIN_FLAVORS:
        audit(
            project_root,
            operation=operation,
            target=str(project_root),
            status="allowed",
            reason=f"local_admin_flavor:{flavor}",
        )
        return Decision(ok=True, reason=f"local_admin_flavor:{flavor}", flavor=flavor)
    ok = _has_perm(project_root, permission, host_session_id)
    audit(
        project_root,
        operation=operation,
        target=str(project_root),
        status="allowed" if ok else "refused",
        reason=("rbac_grant" if ok else f"missing_permission:{permission}"),
    )
    if ok:
        return Decision(ok=True, reason="rbac_grant", flavor=flavor)
    return Decision(
        ok=False,
        flavor=flavor,
        reason=f"requires {permission} (corpo). Sign in as an operator with that grant.",
        blocked_by="operator_auth",
    )


def assert_no_commissioned_ancestor(root: Path) -> Decision:
    """Refuse initializing an AIDOCS project inside an existing one.
    Walks STRICT ancestors (not root itself — re-init/repair of root is
    fine); refuses if any ancestor is commissioned/marked. All flavors.

    FAIL-CLOSED: any error resolving/walking ancestors returns a refusal
    (blocked_by=ancestor_check_error), never a silent allow — an
    unverifiable ancestor must not let a nested init through.
    """
    try:
        cur = Path(root).resolve()
        ancestors = list(cur.parents)
    except Exception as exc:
        audit(
            root,
            operation="project_init",
            target=str(root),
            status="refused",
            reason=f"ancestor_check_error:{exc!r}",
        )
        return Decision(
            ok=False,
            flavor=_flavor(),
            blocked_by="ancestor_check_error",
            reason=f"could not verify ancestors of {root}: {exc!r}",
        )
    try:
        for ancestor in ancestors:
            if _is_commissioned(ancestor):
                audit(
                    root,
                    operation="project_init",
                    target=str(ancestor),
                    status="refused",
                    reason="nested_under_commissioned",
                )
                return Decision(
                    ok=False,
                    flavor=_flavor(),
                    ancestor=str(ancestor),
                    blocked_by="nested_init",
                    reason=(
                        "cannot initialize an AIDOCS project inside an "
                        f"existing AIDOCS project at `{ancestor}`"
                    ),
                )
    except Exception as exc:
        audit(
            root,
            operation="project_init",
            target=str(root),
            status="refused",
            reason=f"ancestor_check_error:{exc!r}",
        )
        return Decision(
            ok=False,
            flavor=_flavor(),
            blocked_by="ancestor_check_error",
            reason=f"ancestor commission check failed: {exc!r}",
        )
    return Decision(ok=True, flavor=_flavor(), reason="no_commissioned_ancestor")


def require_cross_project(
    conductor_root: Path,
    target_root: Path,
    *,
    permission: str = PERM_ADMIN_MANAGE_SESSIONS,
    operation: str = "cross_project",
    host_session_id: str = "",
) -> Decision:
    """Gate any operation that reaches a DIFFERENT project's tree.
    Requires ALL of: target commissioned, an APPROVED relation
    (approved_external_roots), and permission (solo/dev passthrough or
    corpo RBAC). Same-project targets are not cross-project → allow.
    """
    flavor = _flavor()
    if _norm(conductor_root) == _norm(target_root):
        return Decision(ok=True, flavor=flavor, reason="same_project")

    def _refuse(reason: str, blocked_by: str) -> Decision:
        audit(
            conductor_root,
            operation=operation,
            target=str(target_root),
            status="refused",
            reason=reason,
        )
        return Decision(
            ok=False,
            flavor=flavor,
            reason=reason,
            blocked_by=blocked_by,
            target=str(target_root),
        )

    if not _is_commissioned(target_root):
        return _refuse(
            f"target project `{target_root}` is not a commissioned AIDOCS project",
            "target_not_commissioned",
        )
    if not any(_under(target_root, r) for r in _approved_roots(conductor_root)):
        return _refuse(
            f"`{target_root}` is not in security.approved_external_roots — "
            "add it (dashboard) to permit cross-project access",
            "relation_not_approved",
        )
    if flavor not in LOCAL_ADMIN_FLAVORS and not _has_perm(
        conductor_root,
        permission,
        host_session_id,
    ):
        return _refuse(
            f"cross-project access requires {permission} (corpo) — "
            "authenticated operator token or approved host-session binding",
            "operator_auth",
        )
    audit(
        conductor_root,
        operation=operation,
        target=str(target_root),
        status="allowed",
        reason=("local_admin_flavor" if flavor in LOCAL_ADMIN_FLAVORS else "rbac_grant"),
    )
    return Decision(
        ok=True,
        flavor=flavor,
        target=str(target_root),
        reason="cross_project_authorized",
    )


def session_belongs(project_root: Path, session_id: str) -> bool:
    """True iff the session physically lives in this project. A local
    bind must never bind a session_id that isn't the active project's
    (defeats session-name-collision rebinding).
    """
    sid = (session_id or "").strip()
    if not sid:
        return False
    # Canonical membership lives in SQLite (session_membership table), NOT
    # in the presence of a SESSION.md file. The file is the exported
    # verbatim record; it is not authority. A bare folder is rejected (no
    # row), and so is a stray/unregistered SESSION.md — only create_session
    # (or the EXPLICIT legacy migration: migrate-control-authority / the
    # bounded commission phase) mints membership; this read never ingests a
    # file. Deleting SESSION.md does not revoke it. Fail-closed: any store
    # error denies membership.
    try:
        from .session_membership_store import SessionMembershipStore

        return SessionMembershipStore().is_member(project_root, sid)
    except Exception:
        return False


def _session_perm(
    project_root: Path,
    uid: str,
    permission: str,
    session_id: str,
) -> bool:
    try:
        from .rbac_store import RBACStore

        return RBACStore().has_permission(
            project_root,
            uid,
            permission,
            scope_type="session",
            scope_id=session_id,
        )
    except Exception:
        return False


def require_session(
    project_root: Path,
    session_id: str,
    *,
    host_session_id: str = "",
    project_permission: str = PERM_ADMIN_MANAGE_SESSIONS,
    session_permission: str = PERM_ADMIN_MANAGE_SESSIONS,
    operation: str = "session_op",
) -> Decision:
    """TWO-STAGE authority for a session-scoped border op (connect, bind,
    memory write, approval, handoff, lane spawn).

      Stage 1 — PROJECT authority: the caller must hold project-level
        authority (solo/dev passthrough; corpo = authenticated operator +
        project RBAC). Reuses require_admin.
      Stage 2 — SESSION authority: the session must MEMBER this project
        (session_belongs — a session_id/name match is NEVER authority and
        a grant can never escape its project), and in corpo the
        authenticated operator must additionally hold a session-scoped
        grant.

    Fail-closed; every decision audited via require_admin + here.
    """
    flavor = _flavor()
    sid = (session_id or "").strip()

    # Stage 1: project-level authority.
    proj = require_admin(
        project_root,
        permission=project_permission,
        operation=f"{operation}:project",
        host_session_id=host_session_id,
    )
    if not proj.ok:
        return Decision(
            ok=False,
            flavor=flavor,
            stage="project",
            blocked_by=proj.get("blocked_by") or "operator_auth",
            reason="project authority required — " + str(proj.get("reason") or ""),
        )

    # Stage 2a: session MEMBERSHIP (never a name match; never cross-project).
    if not session_belongs(project_root, sid):
        audit(
            project_root,
            operation=f"{operation}:session",
            target=sid,
            status="refused",
            reason="session_not_in_project",
        )
        return Decision(
            ok=False,
            flavor=flavor,
            stage="session",
            blocked_by="session_not_in_project",
            reason=(
                f"session '{sid}' is not a member of project "
                f"{project_root} (session-name match is not authority)"
            ),
        )

    # Stage 2b: session-level RBAC (corpo). solo/dev passthrough.
    if flavor not in LOCAL_ADMIN_FLAVORS:
        uid = _authenticated_uid(project_root, host_session_id)
        if not uid or not _session_perm(
            project_root,
            uid,
            session_permission,
            sid,
        ):
            audit(
                project_root,
                operation=f"{operation}:session",
                target=sid,
                status="refused",
                reason="session_rbac",
            )
            return Decision(
                ok=False,
                flavor=flavor,
                stage="session",
                blocked_by="session_rbac",
                reason=f"session-level grant ({session_permission}) required",
            )

    audit(
        project_root,
        operation=f"{operation}:session",
        target=sid,
        status="allowed",
        reason="two_stage_authorized",
    )
    return Decision(ok=True, flavor=flavor, stage="session", reason="two_stage_authorized")


def require_cross_project_session(
    conductor_root: Path,
    target_root: Path,
    target_session_id: str,
    *,
    host_session_id: str = "",
    operation: str = "cross_project_session",
) -> Decision:
    """Full target-side authority for a cross-project session WRITE/BIND
    (connect / handoff / lane spawn into another project's session).

    Requires BOTH sides — source authority alone is never enough:
      1. SOURCE authority + APPROVED relation + source permission
         (require_cross_project).
      2. TARGET-project authority + TARGET-session membership + TARGET
         session RBAC (require_session against the TARGET root/session).

    Same-project targets degrade to a plain require_session. solo/dev
    passes the authority stages but membership + approved relation are
    still enforced. Fail-closed; audited.
    """
    flavor = _flavor()
    # 1. source authority + approved relation + source permission.
    xp = require_cross_project(
        conductor_root,
        target_root,
        operation=operation,
        host_session_id=host_session_id,
    )
    if not xp.ok:
        return Decision(
            ok=False,
            flavor=flavor,
            stage="source_or_relation",
            blocked_by=xp.get("blocked_by"),
            reason="source/relation: " + str(xp.get("reason") or ""),
            target=str(target_root),
        )
    # 2. TARGET-side two-stage (project authority + session membership/RBAC),
    #    evaluated against the TARGET root — a source grant cannot satisfy it.
    ts = require_session(
        target_root,
        target_session_id,
        operation=f"{operation}:target",
        host_session_id=host_session_id,
    )
    if not ts.ok:
        return Decision(
            ok=False,
            flavor=flavor,
            stage=f"target_{ts.get('stage')}",
            blocked_by=ts.get("blocked_by"),
            reason="target: " + str(ts.get("reason") or ""),
            target=str(target_root),
        )
    return Decision(
        ok=True,
        flavor=flavor,
        stage="cross_session",
        target=str(target_root),
        reason="cross_project_session_authorized",
    )
