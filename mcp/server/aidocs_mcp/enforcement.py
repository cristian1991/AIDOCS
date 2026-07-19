"""Enforcement authority helpers.

#404 excision (operator directive 2026-07-16): the in-app break-glass
surface is GONE. There is no kill-switch config key, no dev-flavor
passthrough, no enforcement bypass of any kind. Every gate enforces for
every caller; authority comes ONLY from an authenticated operator
(dashboard token / approved host-session binding) resolved through
``project_authority`` — fail-closed everywhere.
"""

from __future__ import annotations

from pathlib import Path


def managed_session_host_session_id(project_root: Path) -> str:
    """Resolve the host_session_id BOUND to this project's active managed
    session — the Session aggregate's own operator-binding key.

    This is the DDD-correct answer to "whose authority governs an action taken
    inside this managed session": it is a property OF the session, so it is read
    from the session's durable binding (the query_gate's ``last_host_session_id``,
    which the UPS hook writes for the managed session on every prompt), NOT from
    the process-global ``_calling_conductor_host_session_id`` stamp.

    Why the global must NOT be the authority (#434 root cause, 2026-07-17): that
    stamp is a single process-wide singleton in the shared daemon. Whichever
    connection stamps last wins, so it can carry a FOREIGN or stale host id that
    never bound (field finding: the global held 'c997…' — unbound — while the
    real dashboard-bound session was 'd858…'). Reading authorization off that
    shared mutable global attributes a caller's action to the wrong identity and
    refuses the true session operator. The session-scoped binding is stable and
    correct. Fail-closed to "".
    """
    try:
        # Resolve via a PURE READ of this project's aidocs.sqlite3 — never
        # get_runtime() and never a path that mutates (2026-07-17). Two traps an
        # authority check must not spring while a file op is in flight:
        #   * get_runtime() builds the process-wide RuntimeService singleton
        #     (heavy init with side effects that corrupt the in-flight op's own
        #     path resolution), and
        #   * ManagedModeService.get_mode() → AidocsManagedStore.init_db()
        #     INGESTS AND DELETES the legacy .MEMORY/config/aidocs-managed.json
        #     — i.e. it would delete the very file being edited.
        # AidocsManagedStore.get() and the query-gate read are pure reads (no
        # init_db, no ingest/delete), so this stays cheap, project-scoped, and
        # inert. Both regressed test_gate_config_protection until fixed here.
        from .aidocs_managed_store import AidocsManagedStore
        from .session_query_gate_store import SessionQueryGateStore

        pr = Path(project_root)
        mode = AidocsManagedStore().get(pr)
        if not mode.get("active"):
            return ""
        sid = str(mode.get("session_id") or "").strip()
        if not sid:
            return ""
        return str(
            SessionQueryGateStore().get_last_host_session_id(pr, sid) or "",
        ).strip()
    except Exception:
        return ""


def dev_mode_authorized(project_root: Path | None) -> bool:
    """Authority for dev_mode source-editing (editing the AIDOCS source
    itself: the aidocs_mcp package, index-language TOMLs, plugins).

    #404: no flavor term. True ONLY when BOTH hold:
      * project_root IS the canonical AIDOCS source repo (the package
        subdir exists at the well-known path), and
      * the caller holds ordinary authenticated admin authority
        (``project_authority.require_admin`` — operator token or
        approved host binding + RBAC; fail-closed).

    Source self-edit is ordinary authenticated authority like every
    other privileged surface — there is no contributor carve-out.
    Fails closed on any error.
    """
    try:
        if project_root is None:
            return False
        from .file_ops import _is_aidocs_source_repo

        if not _is_aidocs_source_repo(Path(project_root)):
            return False
        from .project_authority import require_admin

        # Resolve authority through the SESSION AGGREGATE, not the process global
        # (#434 DDD fix, 2026-07-17). require_admin honors two positive proofs of
        # an authenticated operator, in order:
        #   1. AIDOCS_OPERATOR_TOKEN (env) — checked inside require_admin
        #      regardless of host_session_id;
        #   2. the host_session_id BOUND to this managed session — resolved here
        #      from the session's own durable binding, never from the shared,
        #      last-writer-wins conductor global that caused the #434 lockout
        #      (see managed_session_host_session_id).
        # "" ⇒ no session binding ⇒ token-only (unchanged). Fail-closed: an
        # unauthenticated caller has neither a token nor a bound session operator.
        decision = require_admin(
            Path(project_root),
            operation="dev_mode_source_edit",
            host_session_id=managed_session_host_session_id(Path(project_root)),
        )
        return bool(decision.ok)
    except Exception:
        return False


def hard_protected_authority(project_root: Path | None) -> bool:
    """Does the CURRENT principal hold the ``security.hard_protected``
    authority? Single answer for the edit wall (below) and the read
    deny-list (read_pipeline).

      * the principal must be HUMAN — agents/subagents are always denied
        (the whole point is fencing autonomous editors off these files), and
      * the authority itself is answered by ``project_authority`` — the ONE
        fail-CLOSED home: an AUTHENTICATED operator (token / approved
        binding) holding the ``security.hard_protected`` grant in
        rbac_store. The audit-only identity from ``current_user`` is used
        ONLY for the principal-type wall, never as authorization (#344 —
        the old ghost ``rbac.py`` path failed OPEN on its never-populated
        ``rbac_users`` table).

    Escalation (a non-admin requesting + an admin approving) lands as a
    follow-up; until then non-admins are refused here. Fails closed.
    """
    try:
        if project_root is None:
            return False
        from .identity_resolver import current_user
        from .permission_catalog import PERM_SECURITY_HARD_PROTECTED
        from .project_authority import require_admin

        _user_id, _email, principal_type = current_user(Path(project_root))
        if principal_type != "human":
            return False
        # Same Session-aggregate resolution as dev_mode (#434): a binding-
        # authenticated admin (no env token) must be resolved from the managed
        # session's durable binding, not the process global. Without this a
        # dashboard-bound operator is refused the hard-protected authority too.
        decision = require_admin(
            Path(project_root),
            permission=PERM_SECURITY_HARD_PROTECTED,
            operation="hard_protected_authority",
            host_session_id=managed_session_host_session_id(Path(project_root)),
        )
        return bool(decision.ok)
    except Exception:
        return False


def hard_protected_edit_authorized(project_root: Path | None) -> bool:
    """Runtime authority for editing a hard-protected DATA file (non-sqlite).

    Hard-protected DATA files are the project sqlite DBs, the AIDOCS index, and
    gate-state JSON (see ``hard_protected_paths``). sqlite is NEVER file-
    editable regardless of this resolver — ``config_set`` is its only door.
    For the remaining data files the authority is ``hard_protected_authority``
    above (human-only + project_authority, fail-closed).
    """
    return hard_protected_authority(project_root)

