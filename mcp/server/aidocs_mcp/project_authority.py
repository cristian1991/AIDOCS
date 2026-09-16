"""Project-boundary authority — one gate for self-escalation defense.

AIDOCS enforcement keys off *which project* governs the agent. That makes
the project boundary itself a privilege surface: if a low-trust caller can
adopt/commission a tree, init a fake sub-project inside a real one, or
silently rebind the conductor into a more-privileged project, it can turn
AIDOCS's own enforcement into an escalation (a confused deputy).

This module is the single place that answers "may this principal perform
this project-boundary operation on this root?". The model:

  * EVERY flavor → RBAC on an AUTHENTICATED operator (#404, 2026-07-16):
    the caller must present a dashboard token or an approved host-session
    binding AND hold the relevant admin permission for the project scope.
    There is no local-admin passthrough and no auto-mint — login is
    unconditionally required.

Operations:
  * require_admin            — adoption / commissioning (/aidocs).
  * assert_no_commissioned_ancestor — refuse nested project_init.
  * require_cross_project    — register / list-sessions / handoff / spawn /
    connect into a DIFFERENT project: needs the target COMMISSIONED, an
    APPROVED relation (security.approved_external_roots), AND permission.
  * require_create_handoff   — BOTH-ENDS consent for a CROSS-PROJECT handoff
    (#500): `project.create_handoff` must hold in the SOURCE project's own
    RBAC store AND in the TARGET project's own store. If one side doesn't
    allow it, there is no handoff.
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
    PERM_PROJECT_CREATE_HANDOFF,
)

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
    elevate the install). Display/audit metadata ONLY — flavor grants
    nothing (#404: the local-admin passthrough is excised). Fail-closed
    to 'corpo' (the strict flavor) on a config-read error.

    #953: delegates to distribution_flavor.effective_flavor so the `dev`
    claim is install-path-locked. #404 is untouched — that law is about
    authority over PRINCIPALS; the lock is about what an INSTALL may claim
    to be.
    """
    from .distribution_flavor import CORPO, effective_flavor

    return effective_flavor(on_error=CORPO)



def _authenticated_uid(project_root: Path, host_session_id: str = "") -> str:
    """Resolve the AUTHENTICATED operator user_id, or "" if unauthenticated.

    Authority comes ONLY from a dashboard bearer token
    (AIDOCS_OPERATOR_TOKEN), an APPROVED host-session binding, or the
    machine-level user login (#443 — the newest live password-minted
    token row in the identity DB) — all real authentications, checked in
    SPECIFICITY ORDER: most specific credential wins — explicit token >
    per-session approved binding > machine-wide login. (A later
    machine-wide login must never hijack a session already bound to
    another user.) It deliberately does NOT consult
    identity_resolver / current_user(_id): that surface is audit
    ATTRIBUTION only and falls back to env identity or a bootstrapped
    local user, so using it for authorization would let an
    unauthenticated corpo caller (with a stray env identity or a
    first-run local user) pass. Fail-closed everywhere.

    The ladder itself lives in ``_authenticated_uid_diag``, which also reports
    WHY each path declined (#557). This is the plain-string façade every
    existing caller keeps using.
    """
    return _authenticated_uid_diag(project_root, host_session_id)[0]


def _authenticated_uid_diag(
    project_root: Path,
    host_session_id: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """The auth ladder, plus a REPORT of why each path declined.

    WHY THIS EXISTS (#557). The three paths each swallowed Exception and fell
    through, so "no credential presented", "identity DB missing", "DB locked"
    and "schema error" all collapsed into "" — and the operator was told
    "AIDOCS requires login" for what may be an infrastructure fault. That cost
    hours on a live bug: a hook process resolving a different HOME opens a
    different machine-global ~/.aidocs/aidocs_identity.sqlite3, so the
    per-project binding WRITE succeeded while the identity READ failed, and the
    refusal was indistinguishable from a logout. An auth path that cannot
    explain itself turns a five-minute fix into an unfalsifiable loop.

    Entries are {path, outcome, ...} where outcome is:
      ok      — authenticated here (LAST entry; the ladder short-circuits)
      absent  — no credential of this kind (a clean "not signed in")
      error   — the CHECK failed; `detail` names the exception kind
    The machine entry also reports `identity_db` + `exists`, because the store's
    identity was the actual fault line.

    This NEVER authenticates and never widens authority: same ladder, same
    specificity order, same fail-closed default.
    """
    import os

    diag: list[dict[str, Any]] = []

    # (0) THE GATE-RESOLVED OAuth PRINCIPAL — #906. FIRST, because under a gate
    # dispatch it IS the caller, and the rungs below describe the BOX rather than
    # whoever is making this request: the daemon's env token belongs to the host,
    # a host-session binding is local, and the machine login is whoever last
    # signed in on that machine. Most-specific-credential-wins, applied honestly.
    #
    # WHY THIS IS NOT THE WIDENING THIS FUNCTION REFUSES. The docstring rejects
    # identity_resolver / current_user because those FALL BACK to env identity or
    # a bootstrapped local user, so an unauthenticated caller could ride in on a
    # stray value. `_gate_principal` is the opposite: OuterGate sets it only after
    # real OAuth authentication, and its own comment says it exists "so an impl
    # can record/AUTHORIZE on the AUTHORITATIVE principal -- never
    # identity_resolver's local/env fallback (which is blind to the OAuth
    # principal on the remote gate)". Outside a gate dispatch it is None and this
    # rung is inert, so every local caller is byte-identical to before.
    #
    # MEASURED 2026-08-25, and it is why this rung exists: a web connector with
    # org role OWNER, WebMCP entitlement true and an explicit org selection was
    # refused `operator_auth` on ai_session(connect). It could never pass -- the
    # three rungs below are all LOCAL, and a remote principal is none of them. The
    # refusal named a permission, so the remedy looked like a grant; there was no
    # uid for a grant to attach to. The pairing flow was equally useless: it binds
    # a LOCAL host session, and on a VPS-hosted tenant project there is no local
    # operator to pair with.
    #
    # FAIL-CLOSED, THREE WAYS: not a dict, not `authenticated`, or no user_id ⇒
    # absent. A principal that merely EXISTS authenticates nobody; only one that
    # says it was authenticated AND names who.
    try:
        from .mcp_server_runtime_helpers import current_gate_principal

        gp = current_gate_principal()
        if not isinstance(gp, dict):
            diag.append(
                {"path": "gate_principal", "outcome": "absent", "detail": "no gate dispatch"}
            )
        elif not gp.get("authenticated"):
            diag.append(
                {
                    "path": "gate_principal",
                    "outcome": "absent",
                    "detail": "gate principal present but NOT authenticated",
                }
            )
        else:
            _gp_uid = str(gp.get("user_id") or "").strip()
            if _gp_uid:
                diag.append({"path": "gate_principal", "outcome": "ok"})
                return _gp_uid, diag
            diag.append(
                {
                    "path": "gate_principal",
                    "outcome": "absent",
                    "detail": "authenticated gate principal carries no user_id",
                }
            )
    except Exception as exc:  # noqa: BLE001 — an error NEVER authenticates
        diag.append({"path": "gate_principal", "outcome": "error", "detail": repr(exc)[:200]})

    token = os.environ.get("AIDOCS_OPERATOR_TOKEN", "").strip()
    if not token:
        diag.append({"path": "token", "outcome": "absent", "detail": "no AIDOCS_OPERATOR_TOKEN"})
    else:
        try:
            from .operator_auth_service import OperatorAuthService

            ctx = OperatorAuthService().authenticate(token, project_root, source="dashboard")
            if ctx is not None and getattr(ctx, "user_id", ""):
                diag.append({"path": "token", "outcome": "ok"})
                return ctx.user_id, diag
            diag.append({"path": "token", "outcome": "absent", "detail": "token not accepted"})
        except Exception as exc:  # noqa: BLE001 — an error NEVER authenticates
            diag.append({"path": "token", "outcome": "error", "detail": repr(exc)[:200]})

    # (b) per-session APPROVED host binding — session-specific, so it outranks
    # the machine-wide login. The binding row is not enough: its user must still
    # exist and remain enabled on every check.
    if not host_session_id:
        diag.append({"path": "binding", "outcome": "absent", "detail": "no host_session_id"})
    else:
        try:
            from .operator_auth_service import OperatorAuthService

            ctx = OperatorAuthService().resolve_operator_context_from_host_session(
                host_session_id,
                project_root,
            )
            if ctx is not None and getattr(ctx, "user_id", ""):
                diag.append({"path": "binding", "outcome": "ok"})
                return ctx.user_id, diag
            diag.append(
                {
                    "path": "binding",
                    "outcome": "absent",
                    "detail": "no approved binding for this host session",
                }
            )
        except Exception as exc:  # noqa: BLE001
            diag.append({"path": "binding", "outcome": "error", "detail": repr(exc)[:200]})

    # (c) #443 machine-wide FALLBACK. The store it opens is reported because
    # THAT is what diverged in #557.
    entry: dict[str, Any] = {"path": "machine"}
    try:
        from .identity_store import IdentityStore

        db = IdentityStore().db_path(project_root)
        entry["identity_db"] = str(db)
        entry["exists"] = bool(Path(db).is_file())
    except Exception as exc:  # noqa: BLE001 — reporting must never break auth
        entry["identity_db"] = f"<unresolved: {type(exc).__name__}>"
        entry["exists"] = False
    try:
        from .operator_auth_service import OperatorAuthService

        uid = OperatorAuthService().resolve_machine_login(project_root)
        if uid:
            entry["outcome"] = "ok"
            diag.append(entry)
            return uid, diag
        entry["outcome"] = "absent"
        entry["detail"] = "no live machine-login token"
    except Exception as exc:  # noqa: BLE001
        entry["outcome"] = "error"
        entry["detail"] = repr(exc)[:200]
    diag.append(entry)
    return "", diag


def _has_perm(
    project_root: Path,
    permission: str,
    host_session_id: str = "",
) -> bool:
    """Corpo RBAC check for the AUTHENTICATED caller only. No authenticated
    operator (token / approved binding) ⇒ False, regardless of any env
    audit identity or bootstrapped local user. Fail-closed on error.

    #512 SECURITY FIX (2026-07-25): this question is asked at **PROJECT
    scope**, not global scope. #488 moved the RBAC store to a machine-global
    ``~/.aidocs/identity.sqlite3`` ("the STORE is global, the GRANT need not
    be"), so a global-scope lookup resolves IDENTICALLY for every project on
    the box. That silently defeated the TARGET-side half of the cross-project
    ladder: ``require_session`` stage 1 against the target could not tell the
    target apart from the source, so a SOURCE-only grant authorized the
    target too (``require_cross_project_session`` returned
    ok=True/stage=cross_session).

    Project scope is a SUPERSET of global scope in the resolver
    (``effective_permissions`` walks the chain global → project, and a role
    assigned at global scope applies at every narrower lookup), so this does
    NOT revoke authority from principals holding global-only grants — it only
    lets a project-scoped row (grant OR deny) additionally apply, narrower
    wins, FOR THAT PROJECT ONLY. That is the expressiveness a per-project
    decision needs, and it is the same pattern #500 landed in
    ``require_create_handoff`` / ``_has_perm_project_scoped``; the two
    surfaces are deliberately one implementation so they cannot drift.
    """
    return _has_perm_project_scoped(project_root, permission, host_session_id)


def project_scope_key(project_root: Path) -> str:
    """Canonical scope_id for a PROJECT-scoped RBAC row (same spelling the
    escalation/global-law ladders use: forward-slashed project root).

    PUBLIC (#516): tenant grant WRITES and the freeze-clear read sites must
    spell the scope exactly the way #500's reads do, so there is one spelling
    in the codebase and never two.
    """
    return str(project_root).replace("\\", "/")


def _project_scope_key(project_root: Path) -> str:
    """Back-compat alias for #500's internal callers."""
    return project_scope_key(project_root)


def _has_perm_project_scoped(
    project_root: Path,
    permission: str,
    host_session_id: str = "",
) -> bool:
    """Like ``_has_perm`` but resolved at PROJECT scope for THIS project.

    #488 made the RBAC store machine-GLOBAL ("the STORE is global, the GRANT
    need not be"): scope-bearing rows still carry scope_type/scope_id, so a
    per-project decision must be asked at project scope — a global-scope
    lookup would be identical for every project and could never express
    "project B declines". Narrower-wins resolution means a project-scoped
    DENY row overrides a global role grant for that project only.

    Fail-closed: unauthenticated caller or any lookup error ⇒ False.
    """
    uid = _authenticated_uid(project_root, host_session_id)
    if not uid:
        return False
    try:
        from .rbac_store import RBACStore

        return RBACStore().has_permission(
            project_root,
            uid,
            permission,
            scope_type="project",
            scope_id=_project_scope_key(project_root),
        )
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
        # Fallback: the single is_aidocs_managed chokepoint (commission stamp
        # in the sqlite index), not the deprecated .aidocs marker.
        from .mcp_server_runtime_helpers import is_aidocs_managed

        return is_aidocs_managed(root)


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


def _owning_project(root: Path) -> Path | None:
    """The commissioned project that OWNS a boundary decision about ``root``.

    ``root`` itself when commissioned, else its nearest commissioned ancestor,
    else None.

    A refusal must never be audited into the path it refuses. ``audit()``
    writes through ExecutionIndexStore, which provisions the index it writes
    to — so recording "this is NOT a project" under the rejected root CREATED
    ``<root>/.MEMORY/.index/aidocs.sqlite3``, the write-side twin of the
    mkdir-on-read defect (#553). Every CLI mode name that reached the root
    resolver in a path position became a phantom project root holding nothing
    but the store its own refusal had just built.

    Routing to the ancestor is also the semantically correct owner: a
    nested-init attempt is news for the REAL project, and that is the ledger
    the dashboard reads.
    """
    try:
        cur = Path(root).resolve()
        if _is_commissioned(cur):
            return cur
        for ancestor in cur.parents:
            if _is_commissioned(ancestor):
                return ancestor
    except Exception:
        return None
    return None


def _audit_boundary(
    root: Path,
    *,
    operation: str,
    target: str,
    status: str,
    reason: str = "",
) -> None:
    """Audit a decision about ``root`` into whichever project owns it.

    Silently drops the record when no commissioned project owns the path —
    there is no ledger to write to, and fabricating one is the bug this
    function exists to prevent. The Decision is returned to the caller
    regardless, so enforcement never depends on the audit landing.
    """
    owner = _owning_project(root)
    if owner is None:
        return
    audit(owner, operation=operation, target=target, status=status, reason=reason)


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
    """Adoption / commissioning gate. EVERY flavor → RBAC on an
    AUTHENTICATED operator (token / approved binding) — never audit
    identity, never a flavor passthrough (#404).
    """
    flavor = _flavor()
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

    # #906 -- TWO DIFFERENT FAILURES WORE ONE MESSAGE, AND IT SENT THE OPERATOR
    # TO A REMEDY THAT COULD NEVER WORK.
    #
    # `_has_perm` is False for BOTH "nobody is authenticated here" and
    # "authenticated, but this principal lacks the grant". The old reason --
    # "requires {permission}. Sign in as an operator with that grant." --
    # describes only the SECOND. Measured 2026-08-25 against the web connector:
    # org role OWNER, WebMCP entitlement true, org selection valid and explicit,
    # and still `operator_auth`. Reading that message the natural move is to go
    # grant admin.manage_sessions, WHICH CANNOT HELP: there is no authenticated
    # uid for a grant to attach to. Naming a remedy that cannot succeed is the
    # dead end law 311bf3e6 exists to prevent -- reachable but ineffective is
    # still a dead end.
    uid = ""
    try:
        uid = _authenticated_uid(project_root, host_session_id)
    except Exception:  # noqa: BLE001 -- a resolver fault never authorizes
        uid = ""

    if uid:
        # AUTHENTICATED, MISSING THE GRANT. The original message is correct here
        # and only here; it now also names WHO is missing it, so an admin can
        # act without first having to work out which principal to grant.
        return Decision(
            ok=False,
            flavor=flavor,
            reason=(
                f"requires {permission}, and the authenticated operator "
                f"'{uid}' does not hold it. REMEDY: have an admin grant "
                f"'{permission}' to '{uid}' at project scope "
                f"(scope_id='{project_scope_key(Path(project_root))}')."
            ),
            blocked_by="operator_auth",
        )

    # NOT AUTHENTICATED AT ALL -- the web connector's case. The remedy is a
    # PAIRING, not a grant, so stage one: mint (or reuse) a pending binding the
    # dashboard can approve, and hand back its code. Best-effort and
    # fail-quiet -- it never authenticates anyone and must never turn a refusal
    # into a crash.
    hint = ""
    try:
        from .login_gate import stage_pairing_for
        from .mcp_server_runtime_helpers import current_calling_host_kind

        # The REQUEST's own host kind, so a web connector's pending row is
        # visibly a web connector in the dashboard rather than masquerading as a
        # local window. "unknown" is a real answer here (#672's shape) and is
        # passed through -- stage_pairing_for falls back only on empty.
        hint = stage_pairing_for(
            project_root,
            host_session_id,
            host_kind=current_calling_host_kind(),
        )
    except Exception:  # noqa: BLE001 -- a hint must never break the refusal
        hint = ""

    return Decision(
        ok=False,
        flavor=flavor,
        reason=(
            f"NO AUTHENTICATED OPERATOR on this request, so {permission} "
            "cannot be evaluated -- there is no identity for a grant to attach "
            "to, and granting it would change nothing. Authority comes from an "
            "operator token, an APPROVED host-session binding, or a machine "
            "login." + hint
        ),
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
        _audit_boundary(
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
                # Into the ANCESTOR, naming the rejected path as the target:
                # auditing into `root` would build the .MEMORY tree this very
                # decision refuses. See _owning_project.
                audit(
                    ancestor,
                    operation="project_init",
                    target=str(root),
                    status="refused",
                    reason=f"nested_under_commissioned:{ancestor}",
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
        _audit_boundary(
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
    (approved_external_roots), and permission (authenticated RBAC —
    every flavor, #404). Same-project targets are not cross-project → allow.
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
    if not _has_perm(
        conductor_root,
        permission,
        host_session_id,
    ):
        return _refuse(
            f"cross-project access requires {permission} — "
            "authenticated operator token or approved host-session binding",
            "operator_auth",
        )
    audit(
        conductor_root,
        operation=operation,
        target=str(target_root),
        status="allowed",
        reason="rbac_grant",
    )
    return Decision(
        ok=True,
        flavor=flavor,
        target=str(target_root),
        reason="cross_project_authorized",
    )


def require_create_handoff(
    source_root: Path,
    target_root: Path,
    *,
    host_session_id: str = "",
    operation: str = "handoff_create",
) -> Decision:
    """BOTH-ENDS consent for a CROSS-PROJECT handoff (#500).

    A handoff moves work context OUT of the source project and INTO the
    target project — an EGRESS of one tree's context into another. The
    operator's ruling: "if one project doesn't allow, no handoff". So
    ``project.create_handoff`` must hold for the acting principal in the
    SOURCE project AND in the TARGET project. Since #488 the RBAC STORE is
    machine-global while GRANTS stay scope-bearing, so both ends are asked at
    PROJECT scope (``_has_perm_project_scoped``) — that is what lets one
    project decline: a project-scoped DENY row beats a global role grant for
    that project only, and the handoff dies with it.

    This is IN ADDITION to ``require_cross_project_session`` (commissioning +
    approved relation + target session membership/RBAC), never a replacement:
    that ladder proves the caller may reach the target at all; this one asks
    each project whether it consents to participate in a handoff.

    Fail-closed: no authenticated operator, no grant, or any lookup error ⇒
    refused. The refusal NAMES the denying side (``stage`` = source|target,
    ``blocked_by`` = handoff_not_permitted_by_<side>) so an operator knows
    which store to fix. Both outcomes are audited.
    """
    flavor = _flavor()
    same = _norm(source_root) == _norm(target_root)

    def _refuse(side: str, root: Path) -> Decision:
        blocked_by = f"handoff_not_permitted_by_{side}"
        reason = (
            f"cross-project handoff refused: the {side} project `{root}` does "
            f"not grant {PERM_PROJECT_CREATE_HANDOFF} to the authenticated "
            f"operator. A handoff needs BOTH ends to permit it — grant it in "
            f"that project's RBAC store (or accept the refusal)."
        )
        audit(
            root,
            operation=f"{operation}:{side}",
            target=str(target_root),
            status="refused",
            reason=blocked_by,
        )
        return Decision(
            ok=False,
            flavor=flavor,
            stage=side,
            blocked_by=blocked_by,
            reason=reason,
            target=str(target_root),
            denied_by=str(root),
        )

    # SOURCE consent — the project whose context is leaving.
    try:
        source_ok = _has_perm_project_scoped(
            source_root, PERM_PROJECT_CREATE_HANDOFF, host_session_id
        )
    except Exception:  # noqa: BLE001 — a lookup error never grants
        source_ok = False
    if not source_ok:
        return _refuse("source", Path(source_root))

    # TARGET consent — the project receiving foreign context. Asked at the
    # TARGET's own project scope, so a source-side grant cannot answer for it.
    # A same-root call already asked the identical scope above, so skip the
    # duplicate lookup rather than double-auditing it.
    if not same:
        try:
            target_ok = _has_perm_project_scoped(
                target_root, PERM_PROJECT_CREATE_HANDOFF, host_session_id
            )
        except Exception:  # noqa: BLE001
            target_ok = False
        if not target_ok:
            return _refuse("target", Path(target_root))

    audit(
        source_root,
        operation=operation,
        target=str(target_root),
        status="allowed",
        reason="handoff_both_ends_consent",
    )
    return Decision(
        ok=True,
        flavor=flavor,
        stage="both_ends",
        target=str(target_root),
        reason="handoff_both_ends_consent",
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
        authority (authenticated operator + project RBAC — every flavor,
        #404). Reuses require_admin.
      Stage 2 — SESSION authority: the session must MEMBER this project
        (session_belongs — a session_id/name match is NEVER authority and
        a grant can never escape its project), and the authenticated
        operator must additionally hold a session-scoped grant.

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

    # Stage 2b: session-level RBAC (#404: no flavor passthrough —
    # session RBAC applies to everyone).
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
            reason=(
                f"session-level grant ({session_permission}) required for "
                f"session '{sid}'. #518: a grant on ANOTHER project no longer "
                f"widens into this session — a project-scoped grant reaches a "
                f"session only when it names the project that OWNS it. REMEDY: "
                f"ask an admin to grant you '{session_permission}' at session "
                f"scope (scope_id='{sid}'), or at project scope "
                f"(scope_id='{project_scope_key(Path(project_root))}') which "
                f"covers every session of this project"
            ),
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

    Same-project targets degrade to a plain require_session. Membership +
    approved relation are enforced for everyone (#404: no flavor
    passthrough). Fail-closed; audited.
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
