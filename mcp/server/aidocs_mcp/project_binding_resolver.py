"""THE one resolver for "which cloud project and org is this call operating on?".

WHY THIS MODULE EXISTS (#972, operator ruling 2026-08-30)
─────────────────────────────────────────────────────────
It did not exist, and TWO mechanisms answered that question independently:

  * the GATE, from an authenticated principal and an authoritative selected
    project (``GateProjectStore`` in the per-tenant home, #516);
  * ``backlog_hub_client.registered_binding``, from a FILESYSTEM PATH — it
    opened the MACHINE-GLOBAL identity DB (``identity_db_path``, which ignores
    the project_root it is handed) and scanned ``gate_projects`` for a row whose
    ``root`` matched.

MEASURED on the gate: ``ai_whoami`` reported project cristian1991/AIDOCS_PRIVATE
selected, project_id ogp_e605f72a10516ab9, both selection resolvers agreeing —
while ``ai_backlog(mode='cutover_status')`` on that same surface answered
``unbound_project``. The second mechanism was reading a ``gate_projects`` table
in a file that #516 had deliberately emptied of it.

THE FIX IS NOT "TEACH THE SECOND MECHANISM WHERE THE FILE MOVED TO".
Operator, verbatim: "Separate tenant DB FILES are correct isolation. Separate
ANSWERS to 'what project/org am I operating on?' are not." Repointing the read
would have fixed the symptom and preserved the defect — still two authorities,
still able to disagree, with nothing designating a winner.

FILESYSTEM SHAPE IS NOT TENANT AUTHORITY, and the reason is ARITY not access.
Operator, verbatim: "filesystem location is not tenant authority. Even if
directories are perfectly protected, deriving org identity from path creates a
SECOND AUTHORITY beside authenticated gate selection." So a path-derived answer
is refused EVEN WHEN IT WOULD BE CORRECT, and it may not return as a fallback,
a cross-check or a verification either — each of those is the same second
authority under a friendlier name. When the authoritative context is
unavailable this reports a DISCRIMINATED reason and stops.

THE SHAPE
─────────
  ``resolve(project_root)`` → ``ProjectBinding(org_id, project_id, source, reason)``

  GATE implementation   the authenticated principal + the authoritative
                        selected project. Takes NO path and cannot be moved by
                        one — pinned by
                        ``test_no_gate_answer_changes_when_the_filesystem_path_changes``.
  LOCAL implementation  the local registration for this project root, read from
                        the PER-HOME tier via ``GateProjectStore.db_path`` —
                        the same file the registration writer writes.

                        This used to say "byte-for-byte the behaviour
                        ``registered_binding`` had, moved here unchanged so the
                        local surface is untouched", and that sentence is why
                        local identity stayed broken through #516 AND #972: the
                        behaviour preserved so carefully was the machine-global
                        scan named twelve lines above as the measured bug.
                        UNTOUCHED MEANT UNCHANGED, AND WHAT WAS UNCHANGED WAS
                        ALREADY BROKEN (local backlog 986).

``GateProjectStore`` REMAINS the persistence behind gate selection. What changes
is that SELECTION/CONTEXT RESOLUTION — not the store — is the authority exposed
to consumers, and consumers (backlog among them) ask here instead of opening a
registry of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._sqlite_connect import connect as _canonical_connect

#: WHICH implementation answered. Reported on every binding so a reader never
#: has to guess which surface produced it (and so a wrong-surface answer is
#: visible rather than inferred).
SOURCE_GATE = "gate_selection"
SOURCE_LOCAL = "local_registration"

#: The principal key the GATE stamps with the AUTHORITATIVE tenant home
#: (``outer_gate_transport._ogt_tenant_bind``, the one place that resolves it
#: from the validated principal + server-side membership). It travels ON the
#: authenticated principal precisely so no consumer has to reconstruct a home —
#: the reconstruction is what would create the second authority.
GATE_HOME_KEY = "gate_home"

# ── WHY the context is unknown — discriminated, never one label ──────────────
# Same law the backlog's REASON_* set was built under (operator 2026-08-30:
# "Need explicit reason ... Otherwise new label still hides cause"). These have
# DIFFERENT REPAIRS — sign in, select a project, ask for access, fix a store —
# and a reader who cannot tell them apart can act on none of them.
REASON_GATE_UNAUTHENTICATED = "gate_unauthenticated"
REASON_GATE_NO_TENANT_CONTEXT = "gate_no_tenant_context"
REASON_GATE_NO_SELECTION = "gate_no_project_selected"
REASON_GATE_NOT_ALLOWLISTED = "gate_project_not_allowlisted"
REASON_GATE_REGISTRY_ERROR = "gate_registry_error"
REASON_LOCAL_UNREGISTERED = "local_unregistered"
REASON_LOCAL_REGISTRY_ERROR = "local_registry_error"

#: The remedy for each, so a reason names an ACT rather than a condition
#: (law 311bf3e6 — a named remedy must be reachable).
REASON_REMEDY: dict[str, str] = {
    REASON_GATE_UNAUTHENTICATED: (
        "this gate request carries no authenticated principal — sign in; "
        "nothing may stand in for one"
    ),
    REASON_GATE_NO_TENANT_CONTEXT: (
        "the gate did not stamp the authoritative tenant home on this request "
        f"(principal[{GATE_HOME_KEY!r}]) — a transport/dispatch fault, not an "
        "operator one; it is NOT repaired by guessing a home from a path"
    ),
    REASON_GATE_NO_SELECTION: (
        "no project is selected on this gate session — select one "
        "(project_select), which is the act that establishes WHICH project"
    ),
    REASON_GATE_NOT_ALLOWLISTED: (
        "the selected project is not visible to this principal — an org admin "
        "must allowlist it, or select a project you have access to"
    ),
    REASON_GATE_REGISTRY_ERROR: (
        "the tenant project registry could not be read — a LOCAL storage fault "
        "on the gate, not a selection or permission problem"
    ),
    REASON_LOCAL_UNREGISTERED: (
        "this directory is not connected to a cloud project — register/connect "
        "it; nothing identifies WHICH project it is"
    ),
    REASON_LOCAL_REGISTRY_ERROR: (
        "the local project registry could not be read — the fault is LOCAL "
        "storage, and 'unreadable' is not 'unregistered'"
    ),
}


@dataclass(frozen=True)
class ProjectBinding:
    """The resolved cloud identity of this call, and WHO said so.

    ``source`` names the implementation that answered; ``reason`` is "" on a
    resolved binding and a discriminated ``REASON_*`` otherwise. Both are
    carried even on success, so a caller can tell a gate answer from a local
    one instead of inferring it from its own surroundings.
    """

    org_id: str = ""
    project_id: str = ""
    source: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        """Truthy only on a project id — an org alone identifies no project."""
        return bool(self.project_id)

    def as_tuple(self) -> tuple[str, str]:
        """``(org_id, project_id)`` — the shape existing consumers speak."""
        return (self.org_id, self.project_id)

    def remedy(self) -> str:
        """The act that would repair ``reason``, or "" when there is nothing to
        repair. A reason with no reachable remedy is half a diagnosis."""
        return REASON_REMEDY.get(self.reason, "")


def gate_principal() -> dict | None:
    """The AUTHENTICATED-OR-NOT gate principal for this call, or None when no
    gate dispatch is in scope.

    None is the ONLY signal that means "this is the local surface". A principal
    that is present but not authenticated is still a GATE call, and is answered
    by the gate implementation (with a refusal) rather than being handed to the
    local one — a fall-through there would be the second authority, restored.
    """
    try:
        from .mcp_server_runtime_helpers import current_gate_principal

        gp = current_gate_principal()
    except Exception:  # noqa: BLE001 — no gate machinery ⇒ no gate dispatch
        return None
    return gp if isinstance(gp, dict) else None


def _resolve_gate(principal: dict) -> ProjectBinding:
    """The GATE implementation: the authenticated principal + the authoritative
    selected project.

    TAKES NO PATH, BY SIGNATURE. That is the structural half of constraint #4 —
    a path cannot influence what it is never given, so the guarantee does not
    rest on nobody remembering to ignore an argument.

    ``current_if_visible`` is the canonical selection seam (it resolves a
    tenant-bound principal through the fail-safe ``resolve_selection``, which
    never substitutes the default project, and applies the intra-org allowlist).
    We consume it rather than reading ``gate_projects`` ourselves: the store is
    the PERSISTENCE behind selection, and selection — not the store — is the
    authority exposed to consumers.

    The org comes from the SELECTED ROW, not from ``principal['tenant_id']``. A
    registration created without an org must keep presenting as org-less so the
    next blocker is reported honestly; substituting the caller's tenant here
    would manufacture a bound-looking answer out of a row that carries none.
    """
    if not principal.get("authenticated"):
        return ProjectBinding(source=SOURCE_GATE, reason=REASON_GATE_UNAUTHENTICATED)
    user_id = str(principal.get("user_id") or "").strip()
    if not user_id:
        return ProjectBinding(source=SOURCE_GATE, reason=REASON_GATE_UNAUTHENTICATED)
    home = str(principal.get(GATE_HOME_KEY) or "").strip()
    if not home:
        return ProjectBinding(source=SOURCE_GATE, reason=REASON_GATE_NO_TENANT_CONTEXT)
    try:
        from .outer_gate_projects import GateProjectStore

        status, proj = GateProjectStore().current_if_visible(Path(home), user_id, principal)
    except Exception:  # noqa: BLE001 — an unreadable registry resolves NOTHING
        return ProjectBinding(source=SOURCE_GATE, reason=REASON_GATE_REGISTRY_ERROR)
    if status == "project_not_allowlisted":
        return ProjectBinding(source=SOURCE_GATE, reason=REASON_GATE_NOT_ALLOWLISTED)
    pid = str((proj or {}).get("project_id") or "").strip()
    if status != "ok" or not pid:
        return ProjectBinding(source=SOURCE_GATE, reason=REASON_GATE_NO_SELECTION)
    return ProjectBinding(
        org_id=str((proj or {}).get("org_id") or "").strip(),
        project_id=pid,
        source=SOURCE_GATE,
    )


def norm_root(root: Any) -> str:
    """Case/symlink-folded path key. LOCAL-TIER ONLY.

    This is the local registration's matching key and nothing else. It is
    deliberately unreachable from ``_resolve_gate``: on the gate a path is not
    an identity, and normalising one would not make it into one.
    """
    try:
        return str(Path(str(root)).resolve()).rstrip("\\/").casefold()
    except Exception:  # noqa: BLE001
        return str(root or "").rstrip("\\/").casefold()


def _resolve_local(project_root: Any) -> ProjectBinding:
    """The LOCAL implementation: this box's own project registration.

    Reads the PER-HOME registration store (#516 tier), which is where
    ``GateProjectStore.register`` actually writes. Matching is on the resolved,
    case-folded path, so a drive-case or symlink difference between how a
    project was registered and how it is opened cannot silently unbind it.

    It was previously "moved verbatim" from
    ``backlog_hub_client.registered_binding`` — including that function's
    machine-global ``db_path`` call, which #516 had already made unreadable.
    See the module docstring (local backlog 986).

    STILL NOT WIRED END-TO-END, and this is deliberate rather than overlooked:
    no PRODUCTION caller registers the local box's own project. Both writers
    (``register`` via ``register_from_github_url``, and ``ensure_default``) hang
    off the gate transport, so on a purely local box this correctly answers
    ``local_unregistered`` — the honest answer for a project nothing has ever
    registered. What changed is that a registration performed through the real
    writer is now FOUND. Wiring a local registration act is its own decision,
    with its own question about who may assert a cloud project id.

    A path IS the local key, legitimately: there is no authenticated tenant on
    this surface to be a rival to it, so there is only ever one answer. The
    prohibition is on a path answering for the GATE, where an authenticated
    selection already does.
    """
    try:
        from . import store_migrations
        from .outer_gate_projects import GateProjectStore

        # THE PATH COMES FROM THE WRITER'S OWN db_path (local backlog 986).
        #
        # This read `IdentityStore().db_path(root)` — the MACHINE-GLOBAL
        # identity file, which by its own docstring "accepts (and ignores)" the
        # project_root. #516 moved `gate_projects` OUT of that file into the
        # per-home tier, so this scan searched a file the table is guaranteed
        # not to be in and could only ever answer `local_unregistered`.
        #
        # #972 KNEW: it named this exact scan as the measured bug, ruled
        # correctly that the GATE must not be repointed at a tenant file — and
        # then carried the reader here "byte-for-byte ... so the local surface
        # is untouched". Untouched meant unchanged, and what was unchanged was
        # already broken. The local surface had never worked.
        #
        # Taking the path from `GateProjectStore.db_path` rather than respelling
        # `tenant_home_db_path` here is the actual repair: reader and writer now
        # have ONE answer to "where does gate_projects live", so the next time
        # that tier moves, both move together. A second spelling is how this bug
        # was built.
        #
        # NOT `store.list(home)`, deliberately: that calls `init_db`, and this
        # resolver runs on every backlog and XAACP read. Creating a sqlite file
        # as a side effect of asking a question is a write-on-read, and for an
        # unregistered project it would manufacture the very store whose absence
        # is the honest answer.
        db = GateProjectStore().db_path(Path(project_root))
        if not db.is_file():
            return ProjectBinding(source=SOURCE_LOCAL, reason=REASON_LOCAL_UNREGISTERED)
        want = norm_root(project_root)
        with _canonical_connect(str(db), row_factory=False) as conn:
            if not store_migrations.table_exists(conn, "gate_projects"):
                return ProjectBinding(source=SOURCE_LOCAL, reason=REASON_LOCAL_UNREGISTERED)
            rows = conn.execute("SELECT project_id, org_id, root FROM gate_projects").fetchall()
    except Exception:  # noqa: BLE001 — registry trouble ⇒ behave as local-only
        return ProjectBinding(source=SOURCE_LOCAL, reason=REASON_LOCAL_REGISTRY_ERROR)
    for pid, org, root in rows:
        if norm_root(root) == want:
            return ProjectBinding(
                org_id=str(org or "").strip(),
                project_id=str(pid or "").strip(),
                source=SOURCE_LOCAL,
            )
    return ProjectBinding(source=SOURCE_LOCAL, reason=REASON_LOCAL_UNREGISTERED)


def resolve(project_root: Any = None) -> ProjectBinding:
    """WHICH cloud project and org is THIS call operating on. The one answer.

    Exactly one implementation answers, chosen by WHICH SURFACE is calling —
    never by trying both and preferring one:

      GATE   a gate dispatch is in scope (``current_gate_principal()`` is set,
             which only ``OuterGate`` does, only after authentication). The
             answer comes from the authenticated principal + the authoritative
             selected project. ``project_root`` is NOT PASSED to it and cannot
             influence it.
      LOCAL  no gate dispatch. The answer comes from the local registration for
             this project root, exactly as it always has.

    A gate dispatch that cannot resolve returns a DISCRIMINATED reason and
    STOPS. It does not fall through to the local implementation: doing so would
    hand a gate call a path-derived answer, which is the second authority this
    module exists to remove — and it would be wrong even on the occasions it
    happened to be right.
    """
    principal = gate_principal()
    if principal is not None:
        return _resolve_gate(principal)
    return _resolve_local(project_root)
