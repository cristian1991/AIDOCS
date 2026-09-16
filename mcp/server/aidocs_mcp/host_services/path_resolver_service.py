"""Path resolution for host adapters — THE canonical templates/scripts resolver.

Lifted from claude_hook.py 2026-05-27 (Phase 2 of the thinning campaign).
Pre-extraction these functions lived at module level in claude_hook.py
as helpers used by ClaudeHookHandler.__init__. The opencode plugin
has an equivalent ``resolveAidocsRuntimeSourceRoot()`` in JS that
should also collapse onto this surface in a future PR.

CONVERGENCE 2026-07-30 (#628, same family as #627). Three rival
templates-root resolvers existed, each with its own path math:

  * this module's ``resolve_templates_root`` — fixed ``parents[4]``, NO
    existence gate at all;
  * ``mcp_server._resolve_templates_root`` — gated walk-up with an
    UNGATED ``parents[3]`` last-resort fallback;
  * ``outer_gate_transport._resolve_session_templates_root`` — gated
    walk-up returning ``None`` (the only well-behaved one).

In an INSTALLED layout (``<runtime>/venv/Lib/site-packages/aidocs_mcp/``)
the first two both anchor on the venv directory and append
``core/.MEMORY/.aidocs/templates`` — a path nothing lives at, because a
wheel's package-data cannot reach ``core/.MEMORY/`` (see
``mcp/pyproject.toml [tool.setuptools.package-data]``: package-dir is
``server/``). Session creation then died machine-wide on a bare ENOENT
naming a fabricated path. All three now delegate to
``find_templates_root`` here, which NEVER returns a path that does not
hold ``context.md``.

What's host-agnostic about path resolution:
  - The repo layout (mcp/server/aidocs_mcp/<file>.py → repo root is
    parents[3]) is the same regardless of which host is calling.
  - Templates live at <repo>/core/.MEMORY/.aidocs/templates/.
  - Scripts live at <repo>/core/scripts/.

What's NOT here (kept host-specific in the hook):
  - Reading host-specific env vars (AIDOCS_PROJECT_ROOT,
    AIDOCS_EXPERT_LANE_ID) — those belong with the adapter that
    speaks the host's envelope shape.
"""

from __future__ import annotations

import os
from pathlib import Path

# The file whose presence PROVES a candidate is a real template tree. Kept
# local (not imported from constants) so this module stays import-cheap for
# the hook path.
_CONTEXT_TEMPLATE_NAME = "context.md"

# Env var an operator can point at a source checkout. Already honored as the
# AIDOCS source root by runtime_project_support_service — the same lever must
# reach the templates resolver, because on a box whose install carries no
# template tree it is the ONLY lever there is.
_SOURCE_ROOT_ENV = "AIDOCS_PATH"


class TemplatesRootUnresolved(RuntimeError):
    """No real template tree could be found.

    Raised INSTEAD of letting a fabricated path reach a file read: the
    message names every directory probed and the remedies, so the failure
    is actionable where it happens rather than far from the resolver.
    """


def _tree_candidates(base: Path):
    """Template-tree shapes seen under a single anchor directory.

    ``core/.MEMORY/...``  — repo + deployed gate release layout
    ``.MEMORY/...``       — a project's own bundled tree
    ``data``              — package-data payload (wheel-shippable)
    """
    yield base / "core" / ".MEMORY" / ".aidocs" / "templates"
    yield base / ".MEMORY" / ".aidocs" / "templates"
    yield base / "data"


def _is_real(candidate: Path) -> bool:
    try:
        return (candidate / _CONTEXT_TEMPLATE_NAME).is_file()
    except OSError:
        return False


def _env_source_root() -> Path | None:
    raw = (os.environ.get(_SOURCE_ROOT_ENV) or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return None


def _source_checkout_root() -> Path | None:
    """Repo root of the SOURCE CHECKOUT this process runs from, or None.

    Delegates to ``runtime_provisioner.local_source_root`` (#552/#627) —
    the marker-gated (``pyproject.toml`` + ``server/aidocs_mcp/``) source
    root — rather than re-deriving checkout detection a fourth time. It
    returns the ``mcp/`` dir, so the repo root is its parent.
    """
    try:
        from ..runtime_provisioner import local_source_root
    except Exception:
        return None
    try:
        mcp_dir = local_source_root()
    except Exception:
        return None
    return mcp_dir.parent if mcp_dir is not None else None


def _package_walkup_root(package_file: str | Path | None = None) -> Path | None:
    """Walk UP from a package file, returning the first REAL tree.

    Handles the repo layout, the deployed gate release layout
    (``…/releases/<id>/core/…`` — fixed ``parents[N]`` math resolves to an
    id-less path that does not exist), and the installed-wheel layout
    (where it correctly finds NOTHING instead of inventing
    ``<venv>/core/…``).
    """
    here = Path(package_file or __file__).resolve()
    bases = (here.parent, *here.parents)
    # TWO PASSES, and the order matters (#656). A REAL tree anywhere up the
    # chain beats the package-data payload, even though `data` sits at a
    # SHALLOWER base than the repo root.
    #
    # Single-pass walk-up was correct only while nothing shipped in `data`.
    # Once #656 put context.md there, `<pkg>/data` was reached at the
    # aidocs_mcp base and won before the walk ever climbed to the repo's
    # core/.MEMORY/.aidocs/templates — so a checkout resolved to its own
    # SHIPPED COPY. Byte-identical today, so nothing visibly broke; but a
    # developer editing the source template would silently keep scaffolding
    # from the stale copy until they re-ran the generator. Gate 2b caught it
    # (test_templates_root_under_core_memory_aidocs).
    for base in bases:
        for cand in (
            base / "core" / ".MEMORY" / ".aidocs" / "templates",
            base / ".MEMORY" / ".aidocs" / "templates",
        ):
            if _is_real(cand):
                return cand
    # Only now the wheel's own payload — the installed layout, where no `core/`
    # tree exists anywhere above the package.
    for base in bases:
        cand = base / "data"
        if _is_real(cand):
            return cand
    return None


def find_templates_root(
    project_root: Path | str | None = None,
    package_file: str | Path | None = None,
) -> Path | None:
    """THE templates-root resolver. Returns a directory that provably
    holds ``context.md``, or ``None``. Never fabricates.

    Probe order — the ARTIFACT YOU ARE RUNNING wins, then the recovery
    levers for an install that ships no tree of its own:
      1. walk-up from the calling package (repo + gate release layouts —
         a deployed release must resolve to ITS OWN templates, never to a
         stray env pointing at some other checkout)
      2. ``$AIDOCS_PATH`` (explicit operator-set source root)
      3. the source checkout this process runs from (#552 marker-gated)
      4. the PROJECT's own ``.MEMORY/.aidocs/templates`` — the last real
         candidate on an installed box, and the one no rival ever probed
    """
    walked = _package_walkup_root(package_file)
    if walked is not None:
        return walked

    env_root = _env_source_root()
    if env_root is not None:
        for cand in _tree_candidates(env_root):
            if _is_real(cand):
                return cand

    checkout = _source_checkout_root()
    if checkout is not None:
        for cand in _tree_candidates(checkout):
            if _is_real(cand):
                return cand

    if project_root is not None:
        for cand in _tree_candidates(Path(project_root)):
            if _is_real(cand):
                return cand
    return None


def probed_locations(
    project_root: Path | str | None = None,
    package_file: str | Path | None = None,
) -> list[str]:
    """Every directory ``find_templates_root`` would consider — for the
    error message, so an operator sees where it looked."""
    seen: list[str] = []
    anchors: list[Path] = []
    env_root = _env_source_root()
    if env_root is not None:
        anchors.append(env_root)
    here = Path(package_file or __file__).resolve()
    anchors.extend([here.parent, *list(here.parents)[:5]])
    checkout = _source_checkout_root()
    if checkout is not None:
        anchors.append(checkout)
    if project_root is not None:
        anchors.append(Path(project_root))
    for base in anchors:
        for cand in _tree_candidates(base):
            text = str(cand)
            if text not in seen:
                seen.append(text)
    return seen


def resolve_templates_root(
    project_root: Path | str | None = None,
    package_file: str | Path | None = None,
) -> Path:
    """Best-effort Path for CONSTRUCTION-time consumers (AidocsServiceHub
    wants a Path at import time, before any project is known, and derives
    ``script_root`` from it).

    Prefers a real tree; when none is resolvable it returns the
    repo-SHAPED guess so hub construction and script-root derivation keep
    working — but that value is never used for a template READ. Reads go
    through :func:`require_context_template`, which re-resolves with the
    project root in hand and raises :class:`TemplatesRootUnresolved`
    rather than opening a path nothing lives at.
    """
    found = find_templates_root(project_root=project_root, package_file=package_file)
    if found is not None:
        return found
    parents = Path(package_file or __file__).resolve().parents
    base = parents[4] if len(parents) > 4 else parents[-1]
    return base / "core" / ".MEMORY" / ".aidocs" / "templates"


def require_context_template(
    templates_root: Path | str | None,
    project_root: Path | str | None = None,
) -> Path:
    """The ``context.md`` a session scaffold must read.

    Honors a templates_root that is REAL; otherwise re-resolves (the root
    may be a construction-time guess made before the project was known).
    Raises :class:`TemplatesRootUnresolved` when no tree exists — never
    hands back a path for the caller to ENOENT on.
    """
    if templates_root is not None:
        candidate = Path(templates_root)
        if _is_real(candidate):
            return candidate / _CONTEXT_TEMPLATE_NAME
    found = find_templates_root(project_root=project_root)
    if found is not None:
        return found / _CONTEXT_TEMPLATE_NAME
    probed = probed_locations(project_root=project_root)
    raise TemplatesRootUnresolved(
        f"AIDOCS session templates ({_CONTEXT_TEMPLATE_NAME}) not found. "
        f"Probed: {', '.join(probed)}. "
        f"Remedies: set {_SOURCE_ROOT_ENV} to an AIDOCS source checkout, "
        "run the MCP from a checkout, or redeploy a runtime that ships "
        "core/.MEMORY/.aidocs/templates/.",
    )


def resolve_script_root() -> Path:
    """Resolve the AIDOCS scripts directory at ``<repo>/core/scripts/``."""
    return Path(__file__).resolve().parents[4] / "core" / "scripts"
