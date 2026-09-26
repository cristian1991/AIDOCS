"""WHERE a canonical project id lives -- a LOCATOR, not an identity judge.

8d84 phase 3 (r0b2 ruling 644959cc-d65, D2 corrected). A cross-project XAACP
send names its target by canonical ``ProjectBinding.project_id``. This module
turns that id into the ROOT whose store holds the target's mailbox, and does
nothing else. It never decides WHICH project a caller is in (that is
``project_binding_resolver``, the one identity authority) and it never
authorizes anything (that is ``project_authority.require_cross_project_session``).

ONE LOCATOR, TWO SURFACES -- chosen by which surface is calling, exactly as
the resolver chooses, never by trying both:

  GATE   a gate principal is in scope. The tenant registry
         ``GateProjectStore.get(gate_home, project_id)`` IS the canonical
         project_id -> root map for that tenant, so it is read directly.
         Roots are NOT scanned through ``resolve()``: on the gate, resolve()
         takes no path by design and answers the caller's CURRENT selection
         for every candidate, so a scan would locate the caller's own project
         under any id -- a locator built on the wrong authority. The caller's
         selection is never switched either.
  LOCAL  no gate principal. ``KnownProjectsStore`` is INVENTORY only: for
         each commissioned candidate root, the one resolver is asked for its
         canonical id, and EXACTLY ONE match is accepted. Zero matches and
         more than one both refuse. That is legitimate here because the local
         resolver answers from the candidate's own registration.

NOTHING IS SYNTHESISED. A blank id, a directory name, a repo key or a path is
never turned into a location. Every refusal carries a discriminated INTERNAL
reason; callers facing the outside world collapse them into one
non-enumerating answer and audit the true reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SOURCE_GATE_REGISTRY = "gate_registry"
SOURCE_LOCAL_INVENTORY = "local_inventory"

REASON_BLANK_PROJECT_ID = "blank_project_id"
REASON_UNKNOWN_PROJECT = "unknown_project"
REASON_AMBIGUOUS_PROJECT = "ambiguous_project"
REASON_LOCATOR_REGISTRY_ERROR = "locator_registry_error"
REASON_GATE_UNAUTHENTICATED = "gate_unauthenticated"
REASON_GATE_NO_TENANT_CONTEXT = "gate_no_tenant_context"


@dataclass(frozen=True)
class ProjectLocation:
    """Where ``project_id`` lives, or why it could not be located.

    ``root`` is a LOCATOR only: the directory whose store holds the project's
    mailbox. It is never an identity; identity is ``project_id``, which is
    echoed back exactly as asked.
    """

    project_id: str
    root: str | None = None
    source: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return self.root is not None


def _is_commissioned(root: Path) -> bool:
    from .mcp_server_runtime_helpers import _has_marker

    try:
        return root.is_dir() and _has_marker(root)
    except Exception:  # noqa: BLE001 -- unreadable is not commissioned
        return False


def _locate_on_gate(project_id: str, principal: dict) -> ProjectLocation:
    from . import project_binding_resolver as pbr

    if not principal.get("authenticated") or not str(principal.get("user_id") or "").strip():
        return ProjectLocation(project_id, source=SOURCE_GATE_REGISTRY, reason=REASON_GATE_UNAUTHENTICATED)
    home = str(principal.get(pbr.GATE_HOME_KEY) or "").strip()
    if not home:
        return ProjectLocation(project_id, source=SOURCE_GATE_REGISTRY, reason=REASON_GATE_NO_TENANT_CONTEXT)
    try:
        from .outer_gate_projects import GateProjectStore

        row = GateProjectStore().get(Path(home), project_id)
    except Exception:  # noqa: BLE001 -- an unreadable registry locates NOTHING
        return ProjectLocation(project_id, source=SOURCE_GATE_REGISTRY, reason=REASON_LOCATOR_REGISTRY_ERROR)
    root = str((row or {}).get("root") or "").strip()
    if not root:
        return ProjectLocation(project_id, source=SOURCE_GATE_REGISTRY, reason=REASON_UNKNOWN_PROJECT)
    return ProjectLocation(project_id, root=root, source=SOURCE_GATE_REGISTRY)


def _locate_locally(project_id: str) -> ProjectLocation:
    from . import project_binding_resolver as pbr
    from .known_projects_store import KnownProjectsStore

    try:
        inventory = KnownProjectsStore().list_projects()
    except Exception:  # noqa: BLE001
        return ProjectLocation(project_id, source=SOURCE_LOCAL_INVENTORY, reason=REASON_LOCATOR_REGISTRY_ERROR)
    matches: dict[str, str] = {}
    for row in inventory:
        candidate = Path(str(row.get("project_root") or ""))
        if not str(candidate) or not _is_commissioned(candidate):
            continue
        binding = pbr.resolve(candidate)
        if binding.project_id and binding.project_id == project_id:
            matches[pbr.norm_root(candidate)] = str(candidate)
    if not matches:
        return ProjectLocation(project_id, source=SOURCE_LOCAL_INVENTORY, reason=REASON_UNKNOWN_PROJECT)
    if len(matches) > 1:
        return ProjectLocation(project_id, source=SOURCE_LOCAL_INVENTORY, reason=REASON_AMBIGUOUS_PROJECT)
    (root,) = matches.values()
    return ProjectLocation(project_id, root=root, source=SOURCE_LOCAL_INVENTORY)


def locate_project(project_id: str) -> ProjectLocation:
    """The root holding canonical ``project_id``'s store, or a refusal reason.

    Exactly one surface answers: the gate registry when a gate principal is in
    scope, the local inventory + resolver otherwise. A gate call that cannot
    locate STOPS; it never falls through to the local inventory, which would
    hand a gate call a path-derived answer.
    """
    pid = str(project_id or "").strip()
    if not pid:
        return ProjectLocation("", reason=REASON_BLANK_PROJECT_ID)
    from . import project_binding_resolver as pbr

    principal = pbr.gate_principal()
    if principal is not None:
        return _locate_on_gate(pid, principal)
    return _locate_locally(pid)
