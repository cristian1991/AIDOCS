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
    """The CALLER's own host session id, when it may carry operator authority
    here: it holds a per-conductor managed binding in this project and its
    window is live. Otherwise "" (require_admin then degrades to token-only).

    Identity recut (2026-09-29, operator identity model + r0b1 caller/target
    split): authority for an action is the authority of the one who ACTS.
    This used to resolve "the project's active managed session" from the
    per-project singleton and hand require_admin that session's live
    conductor -- possibly a different window than the caller (confused
    deputy), keyed on last-connect state any agent's connect moves.

    Why the process-global stamp is still NOT used (#434, 2026-07-17): in the
    shared daemon it is last-writer-wins. The caller's identity comes from
    current_calling_host_session_id(), which prefers the attested REQUEST
    identity and returns the honest empty for a scoped request without one.
    Fail-closed to "".
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
        from .managed_mode_service import resolve_managed_session

        pr = Path(project_root)

        # THE AUTHORITY DOOR, FED BY THE PURE READ (#1027 phase 2). The door
        # takes any object exposing `get_mode`, so the state it judges comes
        # from the cheap `AidocsManagedStore.get()` this function is required
        # to use -- routing the DECISION through one place WITHOUT
        # reintroducing the `ManagedModeService.get_mode()` call the comment
        # above forbids (that one would init_db and delete the file being
        # edited).
        #
        # THIS SITE WAS NOT REACHED THROUGH get_mode AT ALL, and was found only
        # because it reads `.get("active")` off the same dict shape via the
        # store. Same authority decision, same ghost-session hazard, different
        # doorway -- so it belongs behind the door like every other.
        #
        # ONE HONEST COST. The door's stale check consults
        # SessionMembershipStore.is_sealed(), which calls ensure_schema() --
        # idempotent CREATE TABLE IF NOT EXISTS in the MEMBERSHIP database,
        # memoised per path. So this function is no longer strictly inert: it
        # may create membership schema. That is a different store from the
        # legacy `.MEMORY/config/aidocs-managed.json` this gate protects, the
        # store documents ensure_schema as "SAFE on read paths", and it never
        # scans the sessions tree -- so the specific hazard the comment above
        # names (ingest-and-delete of the file being edited) is untouched. Said
        # plainly rather than left for the next reader to discover.
        # ── THE CALLER'S OWN BINDING (identity recut, 2026-09-29) ─────────────
        #
        # This used to answer from the per-project SINGLETON ("the project's
        # active session") and then pick the one live conductor bound to it.
        # Under the operator's identity model that is a confused deputy: the
        # singleton is last-connect state any agent's connect moves, and "the
        # session's live conductor" can be ANOTHER window than the caller, so
        # caller X was authorised with window Y's binding. #434 kept this
        # session-scoped only because the caller identity was then a shared
        # process-wide stamp; the request identity is now attested per caller.
        #
        # So: the CALLER's own attested host session, and only when that host
        # holds a per-conductor binding AND its window is live. The pure-read
        # constraint above still holds: the per-conductor rows are read with
        # list_conductors_readonly (no init_db, no ingest/delete).
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        own = str(current_calling_host_session_id() or "").strip()
        if not own:
            return ""

        class _PureRead:
            @staticmethod
            def get_mode(_root: Path, host_session_id: str = "", **_kw) -> dict:
                for row in AidocsManagedStore().list_conductors_readonly(pr):
                    if str(row.get("cli_session_id") or "").strip() == host_session_id:
                        return {
                            "active": True,
                            "session_id": row.get("session_id"),
                            "resolved_via": "per_conductor",
                        }
                return {"active": False, "session_id": "", "resolved_via": "unresolvable_host_session"}

        sid = resolve_managed_session(_PureRead(), pr, host_session_id=own)
        if not sid:
            return ""
        from .window_binding_store import WindowBindingStore

        # LIVE, NOT MERELY RECORDED (#892): a binding whose window is gone is
        # not an operator. Pure read, as above.
        if WindowBindingStore().conversation_is_bound(pr, own) is not True:
            return ""
        return own
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
        # "" ⇒ no session binding ⇒ the binding rung is skipped, and require_admin
        # falls to the remaining LOCAL rungs: the token, THEN the machine-wide
        # login (#443). #884 corrected this comment, which used to say
        # "token-only"; the rung that answered is now recorded on the Decision
        # and its audit (auth_path). Under a GATE dispatch no box rung is
        # consulted at all (project_authority, rung 0 only). Fail-closed: an
        # unauthenticated caller has no token, no bound session operator and no
        # machine login.
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
        # #884: refuse every EXPLICIT non-human (subagent, agent, system...)
        # before the admin check. "unknown" (nothing established the caller's
        # kind) is decided by require_admin below: only an AUTHENTICATED
        # operator holding the grant passes, and an authenticated admin is a
        # human (operator ruling 2026-09-26).
        if principal_type not in ("human", "unknown"):
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

