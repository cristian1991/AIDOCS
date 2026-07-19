from __future__ import annotations

import contextvars
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_AIDOCS_ROOT_ENV_VAR = "AIDOCS_PROJECT_ROOT"
# Marker->sqlite migration: the AIDOCS-managed signal is a DELIBERATE commission
# stamp (index_meta['aidocs_commissioned']) INSIDE the sqlite index — NOT the
# deprecated .aidocs file, and NOT the db file's mere existence (any store touch
# creates the file, which would force a non-AIDOCS project managed).
_AIDOCS_INDEX_DB = Path(".MEMORY") / ".index" / "aidocs.sqlite3"
_COMMISSION_KEY = "aidocs_commissioned"
# Legacy pre-migration marker. Governance-bearing: written ONLY at commission
# (runtime_bootstrap_service + project_commission.commission), never by an
# incidental store touch — so reading it as "managed" is exactly as safe as the
# pre-migration behaviour, unlike the loose "db file exists" signal the stamp
# replaced. Read as a one-time HEAL-FORWARD bridge; retired in Stage 6 once
# existing projects are stamped forward.
_LEGACY_AIDOCS_MARKER = Path(".MEMORY") / ".aidocs" / "index.aidocs"

# Async-safe per-call override of project_root. Set via
# ``with_target_project_root(path)`` for the duration of a
# cross-project wrapped tool call; concurrent calls each see their
# own override (ContextVar scopes per-task). discover_project_root
# checks this FIRST so a wrapper's temporary binding beats the
# module-global _last_known_project_root (which is conductor-scoped).
_target_project_root_override: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_target_project_root_override",
    default=None,
)


@contextmanager
def with_target_project_root(path: Path):
    """Temporarily override project_root resolution for the current
    async task. Used by related_project_<tool> wrappers to route a
    call into a different registered project without mutating the
    conductor's managed-mode bind.
    """
    token = _target_project_root_override.set(path)
    try:
        yield
    finally:
        _target_project_root_override.reset(token)


# The GATE-RESOLVED principal for the current request (user_id, tenant_id, effective_role,
# session_id), set by OuterGate around an impl dispatch so an impl can record/authorize on the
# AUTHORITATIVE principal — never identity_resolver's local/env fallback (which is blind to the
# OAuth principal on the remote gate). Default None → impls fall back to their legacy resolution.
_gate_principal: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_gate_principal",
    default=None,
)


@contextmanager
def with_gate_principal(principal: dict | None):
    """Bind the gate-resolved principal for the duration of one impl dispatch."""
    token = _gate_principal.set(dict(principal) if principal else None)
    try:
        yield
    finally:
        _gate_principal.reset(token)


def current_gate_principal() -> dict | None:
    """The gate-resolved principal for the current request, or None outside a gate dispatch."""
    return _gate_principal.get()


class NoAidocsProjectError(RuntimeError):
    """Raised when discovery cannot locate an AIDOCS-enabled project.

    Carries the install remediation text so host adapters can surface it
    to the operator unchanged.
    """


def stamp_commissioned(project_root: Path) -> None:
    """Write the DELIBERATE commission stamp. Canonical writer, kept next to the
    reader (_has_marker) so the signal has one owning module. Called by the
    commissioning paths (project_init / repair / webmcp init via
    CodeIndexStore.mark_commissioned, which delegates the same write) and by
    test fixtures that simulate a commissioned project.
    """
    db = project_root / _AIDOCS_INDEX_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        )
        con.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            (_COMMISSION_KEY, "commissioned"),
        )
        con.commit()
    finally:
        con.close()


def _has_commission_stamp(path: Path) -> bool:
    """True iff the sqlite index carries the deliberate commission stamp.

    A db FILE alone is NOT enough — any store touch (list_sessions, memory ops,
    an incidental init_db) creates it. Require the stamp row so an incidental db
    never force-promotes a non-AIDOCS project to managed.
    """
    db = path / _AIDOCS_INDEX_DB
    if not db.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT 1 FROM index_meta WHERE key = ?",
                (_COMMISSION_KEY,),
            ).fetchone()
        finally:
            con.close()
        return row is not None
    except sqlite3.Error:
        return False


def _has_legacy_marker(path: Path) -> bool:
    """True iff the governance-bearing legacy .aidocs marker file exists."""
    return (path / _LEGACY_AIDOCS_MARKER).is_file()


def _has_marker(path: Path) -> bool:
    # Managed iff: the deliberate commission stamp (primary signal) OR the legacy
    # .aidocs marker (HEAL-FORWARD bridge — governance-bearing, commission-only;
    # a pure hard-cut here orphaned every pre-migration project, the 04:04
    # backlog outage). NOT the loose "db file exists" signal the stamp replaced.
    return _has_commission_stamp(path) or _has_legacy_marker(path)


def heal_legacy_commission(project_root: Path) -> bool:
    """One-time migration bridge: stamp a legacy-.aidocs project forward.

    When a project carries the governance-bearing legacy marker but no commission
    stamp yet, write the stamp (idempotent). Returns True when a stamp was
    written, else False (no legacy marker, or already stamped). SAFE by
    construction: only ever stamps a root that ALREADY proved prior commission
    via the legacy marker — never a foreign folder or an incidental-db dir.
    Called from managed write-points (bootstrap/resume) so existing projects
    converge onto the stamp and the legacy read (Stage 6) can be retired.
    """
    if not _has_legacy_marker(project_root) or _has_commission_stamp(project_root):
        return False
    stamp_commissioned(project_root)
    return True


def find_aidocs_project_root(candidate: Path) -> Path | None:
    """Walk up from ``candidate`` until an AIDOCS-commissioned root appears.

    The signal is the commission stamp inside the sqlite index
    (index_meta['aidocs_commissioned']), written only by project_init — NOT the
    db file's existence (any store touch creates it), nor the deprecated
    ``.MEMORY/.aidocs/index.aidocs`` marker.
    """
    try:
        start = candidate.resolve()
    except (OSError, RuntimeError):
        start = candidate
    if start.exists() and start.is_file():
        start = start.parent
    for ancestor in (start, *start.parents):
        if _has_marker(ancestor):
            return ancestor
    return None


_GIT_MARKER = ".git"


def find_git_root(candidate: Path) -> Path | None:
    """Walk up from ``candidate`` to the nearest dir holding a ``.git`` entry
    (a directory for a normal clone, a FILE for a git worktree / submodule).

    Git is an AIDOCS hard dependency, so for any real project this is the
    stable, future-proof project-root anchor — independent of ``.MEMORY``,
    which the Postgres-backed webmcp will retire. NOTE: this identifies a
    *git project root*, NOT AIDOCS-managed status (every git repo has
    ``.git``); managed status is :func:`is_aidocs_managed`.
    """
    try:
        start = candidate.resolve()
    except (OSError, RuntimeError):
        start = candidate
    if start.exists() and start.is_file():
        start = start.parent
    for ancestor in (start, *start.parents):
        try:
            if (ancestor / _GIT_MARKER).exists():
                return ancestor
        except OSError:
            continue
    return None


def is_aidocs_managed(root: Path) -> bool:
    """Single chokepoint for "is ``root`` an AIDOCS-managed project root".

    Signal: a DELIBERATE commission stamp inside the sqlite index
    (index_meta['aidocs_commissioned']), written ONLY by project_init. The db
    file's mere existence is deliberately NOT sufficient — any store touch
    creates the file — so an incidental db never force-promotes a non-AIDOCS
    project. Routing all managed-detection through here keeps the signal a
    one-function definition.
    """
    return _has_marker(root)


def discover_project_root() -> Path:
    """Authoritative project-root resolution. Agents never pass ``root``.

    Chain (first AIDOCS project wins):
    1. Managed-mode bound root (``_last_known_project_root``).
    2. ``AIDOCS_PROJECT_ROOT`` env var, validated by marker.
    3. Walk up from current working directory.

    Raises :class:`NoAidocsProjectError` with install remediation when
    no step yields a real AIDOCS project.
    """
    # ContextVar override beats everything — set by
    # related_project_<tool> wrappers so a cross-project call
    # routes to the target without mutating the conductor's bind.
    override = _target_project_root_override.get()
    if override is not None:
        return override
    # #280 clause 3: on the multi-tenant daemon with scoping active, a request
    # that declared NO root (no override) must REFUSE rather than borrow a
    # process-global - that borrow IS the cross-tenant leak. Actionable error.
    if _multitenant_strict:
        raise NoAidocsProjectError(
            "Multi-tenant shared daemon: this request declared no project root "
            "(no ?root= / X-AIDOCS-Project-Root). Refusing to resolve via a "
            "process-global (which would cross-bind tenants). Connect via the "
            "project's SCOPED daemon URL (http://127.0.0.1:8748/mcp?root=<abs>) "
            "or run `aidocs project init` to (re)generate its .mcp.json.",
        )
    # Managed-mode bind is authoritative: the host explicitly told us this
    # IS the project, even if the marker hasn't been seeded yet (fresh
    # init flows, test fixtures). Validation applies only to the env/cwd
    # fallback paths, not to an explicit bind.
    _caller_default = _last_known_for_caller()
    if _caller_default is not None:
        return _caller_default
    env_root = _env_project_root()
    if env_root is not None:
        matched = find_aidocs_project_root(env_root)
        if matched is not None:
            return matched
    cwd_match = find_aidocs_project_root(Path.cwd())
    if cwd_match is not None:
        return cwd_match
    raise NoAidocsProjectError(
        "No AIDOCS project found. Expected an AIDOCS commission (the sqlite "
        "index stamp) in the project root or an ancestor. "
        "Run `aidocs project init` (/aidocs) in the target project, or set "
        "AIDOCS_PROJECT_ROOT.",
    )


def registered_tools(server: Any) -> list[Any]:
    components = getattr(getattr(server, "_local_provider", None), "_components", {})
    return [component for key, component in components.items() if str(key).startswith("tool:")]


def _env_project_root() -> Path | None:
    raw = os.environ.get(_AIDOCS_ROOT_ENV_VAR, "").strip()
    if not raw:
        return None
    return Path(raw)


def _normalize_root_path(raw: str) -> Path:
    """Walk a tool-supplied ``root`` to a directory when it's actually a file.

    Agents frequently pass a file path as ``root`` (e.g. ``src/foo.py``)
    expecting file-scoping. Downstream code then tries ``mkdir
    <root>/.MEMORY/.index/`` and crashes with WinError 183 on Windows or
    NotADirectoryError on Linux. This helper normalizes:

    * absolute file path → its parent directory
    * relative file path that exists in cwd → its parent directory
    * relative file path that exists under the managed/env project root →
      that project root (so the call still has real indexed data to work on)
    * anything else → the path as-given (caller decides what to do)

    Used by both ``project_root_from_args`` (middleware preflight) and
    ``resolve_project_root`` (tool body). Keep them consistent.
    """
    candidate = Path(raw)
    # Probe managed/env-root mappings BEFORE the cwd-relative candidate.
    # Otherwise "src/foo.py" relative to a cwd that IS the project root
    # resolves to "<cwd>/src/foo.py" first and returns the FILE'S PARENT
    # DIR (a subdir of the project) — which mints stray .MEMORY/.index/
    # under that subdir. Probing managed-root first maps the supplied
    # path to the project root when it lives there. (2026-05-03 probe-
    # order fix; see test_relative_file_path_resolved_against_managed_root.)
    probes: list[Path] = []
    if not candidate.is_absolute():
        if _last_known_project_root is not None:
            probes.append(_last_known_project_root / candidate)
        env_root = _env_project_root()
        if env_root is not None:
            probes.append(env_root / candidate)
    probes.append(candidate)
    for probe in probes:
        if probe.exists() and probe.is_file():
            if probe is candidate:
                return candidate.parent
            if (
                _last_known_project_root is not None
                and probe == _last_known_project_root / candidate
            ):
                return _last_known_project_root
            env_root = _env_project_root()
            if env_root is not None and probe == env_root / candidate:
                return env_root
            return probe.parent
    return candidate


def project_root_from_args(arguments: dict[str, Any] | None) -> Path | None:
    if not isinstance(arguments, dict):
        # No args means no agent-supplied hint; discovery is the only source.
        try:
            return discover_project_root()
        except NoAidocsProjectError:
            return None
    raw = arguments.get("root") or arguments.get("project_root")
    if isinstance(raw, str) and raw.strip():
        # Agent-supplied root is only honored when it resolves to a real
        # AIDOCS project marker. Bogus subdir paths (pollution source)
        # are dropped in favor of discovery so the middleware never
        # mints .MEMORY/.index/ beneath a non-project path.
        normalized = _normalize_root_path(raw)
        if find_aidocs_project_root(normalized) is not None:
            return normalized
    try:
        return discover_project_root()
    except NoAidocsProjectError:
        return None


_last_known_project_root: Path | None = None

# #280 clause 3: multi-tenant strict mode. When the shared HTTP daemon runs with
# per-connection scoping active (URLs carry ?root=), a request that resolves NO
# declared root (no override) is UNATTRIBUTABLE - it must REFUSE, never default
# to a process-global (_last_known_project_root / daemon cwd / the managed
# singleton), which is the cross-tenant leak. OFF by default so stdio and the
# pre-activation shared daemon are unchanged; the daemon boot turns it on from
# config (mcp.multitenant_strict) ONLY after the ?root= URLs are live.
_multitenant_strict: bool = False


def set_multitenant_strict(value: bool) -> None:
    global _multitenant_strict
    _multitenant_strict = bool(value)


def multitenant_strict_enabled() -> bool:
    return _multitenant_strict

# #58 conductor identity stamp (canonical 2026-04-26; renamed
# 2026-05-01 to match the agent_memory_epoch.py identity contract,
# which calls this value `host_session_id` — host-agnostic name for
# the raw per-instance value the host (Claude/OpenCode/Codex) hands
# us. Old `cli_session_id` aliases are kept for in-flight callers and
# will be dropped after one release).
#
# Each Claude Code window spawns its own MCP server child process, so
# every tool call this server sees comes from exactly one conductor
# (one host_session_id). The hook captures host_session_id from the UPS
# payload and stamps it here on first session_connect. Subsequent
# tool sites read it via current_calling_host_session_id() and resolve
# managed-mode state via the per-conductor mapping.
#
# See security-gates.md §0.5 #50/#54 sub-clause "conductor-bound state
# keying" for the resolution-precedence contract.
_calling_conductor_host_session_id: str | None = None
_calling_conductor_host_kind: str | None = None


def _normalize_host_kind(host_kind: str) -> str:
    return (host_kind or "").strip().lower() or "unknown"


def set_calling_conductor_host_session_id(
    host_session_id: str,
    *,
    host_kind: str = "",
) -> None:
    """Stamp identity only for process-scoped adapters such as stdio.

    Request-scoped transports carry their own ContextVar identity. A call made
    while that identity is active must never leak its actor into the shared
    process fallback.
    """
    if _request_host_session_id.get():
        return
    global _calling_conductor_host_session_id, _calling_conductor_host_kind
    sid = (host_session_id or "").strip()
    if sid:
        _calling_conductor_host_session_id = sid
    if host_kind:
        _calling_conductor_host_kind = _normalize_host_kind(host_kind)



# #280 clause 2: request-scoped host identity. The module globals are valid
# for one-child-per-window hosts; the shared daemon must stamp both axes per
# request so neither the session id nor host kind leaks between connections.
_request_host_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_request_host_session_id",
    default=None,
)
_request_host_kind: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_request_host_kind",
    default=None,
)


def set_request_host_session_id(
    host_session_id: str,
    *,
    host_kind: str = "unknown",
):
    """Bind the complete request identity for prompt/adapter entrypoints.

    The historical name is retained for callers, but the token carries both
    identity axes so request attribution never falls back to an unrelated
    process-global host kind.
    """
    return set_request_host_identity(host_session_id, host_kind=host_kind)


def reset_request_host_session_id(token) -> None:
    """Reset the complete identity bound by set_request_host_session_id."""
    reset_request_host_identity(token)


def set_request_host_identity(host_session_id: str, *, host_kind: str):
    """Set the complete request-scoped host identity.

    Returns opaque ContextVar tokens that must be reset in finally.
    """
    return (
        _request_host_session_id.set((host_session_id or "").strip() or None),
        _request_host_kind.set(_normalize_host_kind(host_kind)),
    )


def reset_request_host_identity(token) -> None:
    try:
        session_token, kind_token = token
        _request_host_kind.reset(kind_token)
        _request_host_session_id.reset(session_token)
    except Exception:
        pass


def current_calling_host_session_id() -> str:
    """Return the request host_session_id, then the process stamp."""
    req = _request_host_session_id.get()
    if req:
        return req
    return _calling_conductor_host_session_id or ""


def current_calling_host_kind() -> str:
    """Return the host kind carried by the same identity scope."""
    req = _request_host_kind.get()
    if req:
        return req
    return _calling_conductor_host_kind or "unknown"


def current_calling_agent_context_id(project_root: Path | str) -> str:
    """Canonical durable actor id for the calling host in this project."""
    from .agent_memory_epoch import derive_agent_context_id

    return derive_agent_context_id(
        host_kind=current_calling_host_kind(),
        project_root=project_root,
        host_session_id=current_calling_host_session_id(),
    )


def current_calling_aidocs_session_id(
    project_root: Path | str,
    *,
    session_uuid: str,
) -> str:
    """Canonical actor/work-session id for the calling host."""
    from .agent_memory_epoch import derive_aidocs_session_id

    return derive_aidocs_session_id(
        host_kind=current_calling_host_kind(),
        project_root=project_root,
        host_session_id=current_calling_host_session_id(),
        session_uuid=session_uuid,
    )


# #267/#270: the managed-mode default is PER-HOST-SESSION. On a multi-tenant
# server a single module-global let the hot session's root leak to every other
# session — contaminating cross-project reads AND durable writes (a DentalClinic
# agent's ai_backlog(add) landing in AIDOCS's ledger under the wrong host).
_last_known_project_root_by_host: dict[str, Path] = {}


def set_default_project_root(root: Path, host_session_id: str = "") -> None:
    """Called when managed mode activates to set the session default.

    Records the default keyed by the ACTIVATING host_session (#267/#270) so each
    tenant resolves to its OWN project; the module singleton is kept only as a
    legacy fallback for callers with no host identity (single-session hosts)."""
    global _last_known_project_root
    _last_known_project_root = root
    hid = (host_session_id or _calling_conductor_host_session_id or "").strip()
    if hid:
        _last_known_project_root_by_host[hid] = root


def reset_conductor_bind_state() -> None:
    """Reset all process-wide conductor bind and identity state."""
    global _last_known_project_root
    global _calling_conductor_host_session_id, _calling_conductor_host_kind
    _last_known_project_root = None
    _calling_conductor_host_session_id = None
    _calling_conductor_host_kind = None
    _last_known_project_root_by_host.clear()



def _last_known_for_caller() -> Path | None:
    """The managed-mode default for the CALLING host_session (#267/#270).

    Returns the caller's OWN recorded root when known; a host with no recorded
    root falls back to the singleton (legacy/best-guess). Two sessions that have
    each activated resolve to their own root — the hot session no longer leaks
    its root into the other."""
    hid = (_calling_conductor_host_session_id or "").strip()
    if hid:
        # TIER -INFINITY isolation (2026-07-09): a caller WITH a host identity
        # resolves to its OWN recorded root or NOTHING — it must NEVER inherit
        # another session's root from the module global. On the shared HTTP
        # daemon (#249: one daemon serves every host window) the old
        # `return _last_known_project_root` fallback leaked the HOT session's
        # project (e.g. AIDOCS) into a FOREIGN session (e.g. DentalApp),
        # cross-binding it — durable writes landing in the wrong ledger. A host
        # with no recorded root falls through to env/cwd discovery instead.
        return _last_known_project_root_by_host.get(hid)
    # Only a truly host-identity-less caller (legacy single-session stdio host)
    # may use the module singleton.
    return _last_known_project_root


def resolve_current_session_id(project_root: Path | None = None) -> str:
    """Resolve the calling conductor's active managed session_id, or "".

    Used to thread session scope into runtime config reads (e.g. the
    session-scoped timeout settings) so a per-session dashboard override is
    honored. Resolution: the calling conductor's host_session_id
    (thread-local) → ManagedModeService.get_mode → managed session_id.
    Returns "" when no managed session is active (config then cascades
    project > global > factory as before). Fail-open: never raises.
    """
    try:
        root = project_root if project_root is not None else resolve_project_root()
        from .managed_mode_service import ManagedModeService

        mode = ManagedModeService().get_mode(
            root,
            host_session_id=current_calling_host_session_id(),
        )
        if mode.get("active"):
            return str(mode.get("session_id") or "").strip()
    except Exception:
        pass
    return ""


def resolve_authoritative_session_id(project_root: Path | None = None) -> str:
    """Session id for CROSS-SESSION-VISIBLE attribution — resolved ONLY from an
    authoritative calling host-session identity, NEVER borrowed from the
    project's managed-mode singleton.

    Host-agnostic CORE (2026-07-09). Contrast with ``resolve_current_session_id``,
    which MAY fall through to the singleton because its consumer (local
    config-scope reads) is harmless if it borrows. Attribution is NOT harmless:
    in the shared HTTP daemon (one process, many host windows) borrowing the
    singleton cross-stamps one tenant's activity onto another — the #267/#270
    leak class, where a hooks-off DentalClinic agent's run-completion / durable
    write surfaces under AIDOCS's host because its empty host_session_id fell
    through to get_mode's singleton_fallback.

    When the caller's host-session identity is unknown (empty host_session_id),
    returns '' — the attribution is left UNATTRIBUTED, which every downstream
    isolation (run_notifications #50 filtered drain; per-host ledger keying)
    treats as 'not mine' and never surfaces into another session's context.
    Fail-open: never raises.
    """
    try:
        sid = current_calling_host_session_id()
        if not sid:
            return ""
        root = project_root if project_root is not None else resolve_project_root()
        from .managed_mode_service import ManagedModeService

        mode = ManagedModeService().get_mode(root, host_session_id=sid)
        if mode.get("active"):
            return str(mode.get("session_id") or "").strip()
    except Exception:
        pass
    return ""


def _resolve_session_project_bind() -> Path | None:
    """Return the project bound to the CALLING host session via
    ``ai_project(mode="bind")``, or None when there's no live bind.

    Doctrine (2026-05-31): this is the LOCAL mirror of the outer-gate's
    ``project_select`` — a persistent, operator-chosen project binding,
    keyed by ``host_session_id`` (so it's per-session, never process-
    global → cross-user separation for free) and idle-TTL'd. It is only
    ever WRITTEN through the authorized ``ai_project`` bind path (gated by
    ``project_authority.require_cross_project``), so consulting it here is
    reflecting an already-authorized choice, not granting one.

    Read-only + fail-open: any error, no host session, or an expired bind
    returns None and resolution falls through to normal cwd-discovery —
    so a session with no bind behaves EXACTLY as before (zero change).
    Activity-refresh (TTL keepalive) is done once per tool call at the
    server boundary, NOT here, to keep this hot-path read write-free.
    """
    try:
        sid = current_calling_host_session_id()
        if not sid:
            return None
        from .session_project_bind_store import SessionProjectBindStore

        # refresh=True drives the idle-TTL keepalive (throttled write, so the
        # hot path stays cheap): any tool call that resolves the bind keeps
        # it alive; once idle past the TTL it expires and we fall through.
        bound = SessionProjectBindStore().resolve(sid, refresh=True)
        if bound:
            return Path(bound)
    except Exception:
        return None
    return None


def resolve_project_root(root: str | None = None) -> Path:
    """Resolve the project root for a tool call.

    Post-Beat-2 the agent-facing schemas no longer carry ``root``. Every
    call to this function now has no meaningful argument and the body
    runs the discovery chain. The legacy signature remains so older
    passing code keeps compiling while we finish the sweep; a
    pollution-tainted string passed in is honored only when it resolves
    to a real AIDOCS project marker, otherwise discovery wins.
    """
    # ContextVar override wins over caller-supplied root — the wrapper
    # is explicitly rerouting; callers shouldn't accidentally escape it.
    override = _target_project_root_override.get()
    if override is not None:
        return override
    # Host-session project bind (ai_project bind) — a deliberate, authorized
    # operator binding that beats cwd-discovery so it STICKS across tool
    # calls even when CC runs in a different directory. Unset by default,
    # so this is a no-op (and identical legacy behavior) for any session
    # that hasn't explicitly bound.
    session_bind = _resolve_session_project_bind()
    if session_bind is not None:
        return session_bind
    if root and root.strip():
        normalized = _normalize_root_path(root)
        if find_aidocs_project_root(normalized) is not None:
            return normalized
    try:
        return discover_project_root()
    except NoAidocsProjectError:
        # #280 clause 3: under multi-tenant strict mode a rootless request must
        # REFUSE, not borrow a process-global (the cross-tenant leak). discover_
        # project_root already raised for the right reason — propagate it.
        if _multitenant_strict:
            raise
        if _last_known_project_root is not None:
            return _last_known_project_root
        env_root = _env_project_root()
        if env_root is not None:
            return env_root
        return Path.cwd()


def capture_enabled(name: str, arguments: dict[str, Any] | None) -> bool:
    if name in {"execution_run_record", "execution_event_record"}:
        return False
    return project_root_from_args(arguments) is not None


def summarize_tool_result(result: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "result_type": type(result).__name__,
    }
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        summary["structured_keys"] = sorted(str(key) for key in structured.keys())[:10]
        result_value = structured.get("result")
        if isinstance(result_value, list) or isinstance(result_value, dict):
            summary["result_length"] = len(result_value)
        elif result_value is not None:
            summary["result_scalar_type"] = type(result_value).__name__
    content = getattr(result, "content", None)
    if isinstance(content, list):
        summary["content_items"] = len(content)
        summary["content_types"] = [type(item).__name__ for item in content[:5]]
    return summary


def all_capabilities(hub: Any, project_root: Path) -> list[dict[str, Any]]:
    return hub.capabilities.find_capabilities(project_root, query=None, limit=1000)


def all_procedures(hub: Any, project_root: Path) -> list[dict[str, Any]]:
    return hub.procedures.find_procedures(project_root, query=None, limit=1000)


def resolve_related_root(hub: Any, root: str | Path, name: str) -> Path:
    resolved = hub.related.resolve_related_project_path(Path(root), name)
    if resolved is None:
        raise FileNotFoundError(
            f"Related project '{name}' is not configured or its path does not exist.",
        )
    return resolved


# Keys that are NEVER echoed on success — either they came from the
# caller's own arguments (path, old_string, etc.) or they're metrics
# the audit chain already records (bytes_written, duration_seconds,
# content_length, mode, etc.). Pure context burn.
_NEVER_ECHO_ON_SUCCESS: frozenset[str] = frozenset(
    {
        # Caller arguments (already in the tool call).
        "path",
        "file_path",
        "file",
        "target_hint",
        "old_string",
        "new_string",
        "old_str",
        "new_str",
        "content",
        "source",
        "target",
        # Redundant status echoes.
        "command",
        "mode",
        "config_edit_mode",
        "dry_run",
        "success",
        # Metrics that live in execution_events.
        "bytes_written",
        "old_bytes",
        "new_bytes",
        "content_length",
        "duration_seconds",
        "duration_ms",
        "stdout_lines",
        "stderr_lines",
    },
)


def _raise_tool_error(message: str) -> None:
    """Raise the MCP tool error that renders as a red/amber FAIL
    status in the host UI (Claude Code, OpenCode, Cursor, etc.).

    FastMCP's ToolError maps to an MCP protocol-level error response
    that the host displays as "tool failed". Returning a dict with
    `err` was green-success in the UI because the wire message said
    the call completed.

    Falls back to RuntimeError if FastMCP isn't importable — still
    breaks out of the tool body, still surfaces as a fail to callers
    that catch it at the MCP boundary.
    """
    try:
        from fastmcp.exceptions import ToolError  # type: ignore
    except Exception:
        try:
            from fastmcp import ToolError  # type: ignore
        except Exception:
            raise RuntimeError(message)
    raise ToolError(message)


def terse_tool_output(
    verbose_result: dict[str, Any],
    *,
    keep_keys: tuple[str, ...] = (),
    project_root: Path | None = None,
) -> dict[str, Any] | str:
    """Collapse a verbose tool-result dict into the minimum useful
    envelope the agent needs.

    Success → bare string "ok" when there's nothing new to return
    (agent already knows inputs from the tool call). When keep_keys
    carries a genuinely new id/count/etc., upgrades to
    {"ok": true, ...kept}. Anything in _NEVER_ECHO_ON_SUCCESS is
    dropped even if keep_keys asks for it — input echoes are always
    slop.

    Failure → RAISES ToolError so the host UI renders the call as a
    red/amber fail, not green success. Message carries the reason.
    Agents that want structured error handling can still try/except.

    Always terse (2026-04-23): no verbose-mode toggle. Audit chain
    captures the full payload via task_id regardless of what the
    agent sees in the tool return.
    """
    if not isinstance(verbose_result, dict):
        return verbose_result
    if verbose_result.get("success") is False or verbose_result.get("ok") is False:
        err = verbose_result.get("error") or verbose_result.get("err") or "failed"
        _raise_tool_error(str(err))
    # FastMCP's structured_content channel rejects bare strings
    # (enforces dict-or-None per the tool's output_schema). So
    # "ok" lives as the shortest legal dict: {"ok": true}.
    kept: dict[str, Any] = {"ok": True}
    for key in keep_keys:
        if key in _NEVER_ECHO_ON_SUCCESS:
            continue
        value = verbose_result.get(key)
        if value not in (None, "", [], {}):
            kept[key] = value
    return kept


_TASK_GATE_EXEMPT: frozenset[str] = frozenset(
    {
        # Task / session lifecycle — otherwise the gate bootstraps into
        # infinite recursion (task_begin can't fire without a prior
        # task_begin; ai_session must run before any task can exist).
        "ai_task",
        "ai_session",
        # Filing surfaces — reading todos/backlog (and capturing project
        # backlog) must not require an active task, and must never auto-create
        # one. Their task-owned writes self-gate in-handler where needed.
        # (#83: todo filing now rides ai_task, already exempt above.)
        "ai_backlog",
        # Host probes — cheap inspection that hosts run on startup or for
        # status display. Gating them would freeze the host UI.
        "aidocs_mode_get",
        "aidocs_route_prompt",
        # CC's hardcoded MCP startup probe alias — must always succeed or
        # CC marks the server unhealthy and stops calling it.
        "session_start",
        # Benign read-only build-version probe — no side effects, no project
        # data read, nothing to attribute (docstring: "Benign, read-only; no
        # org/project selection needed"). The 2026-07 attribution-widening
        # (last-known-project-root fallback in the central middleware) began
        # catching it, making a version check refuse with "no active task".
        # Same class as the host probes above. Pinned by
        # test_mutation_gate::test_ai_version_is_exempt_benign_probe.
        "ai_version",
        # Bootstrap entry points. Fresh projects have no managed session
        # yet, so they pass via the unmanaged-mode fail-open below; this
        # listing is defensive for the case where managed mode is on but
        # the operator is re-running bootstrap on a partially-initialised
        # project.
        "project_init",
        "project_bootstrap_or_resume",
    },
)


def shell_egress_lifecycle_preflight(
    hub: Any,
    project_root: Path,
    tool_name: str,
) -> dict[str, Any] | None:
    """Strict shell-egress-specific lifecycle preflight.

    Doctrine 2026-05-29 (Empire re-seal — shell-egress strictness):
    `require_active_task` fails OPEN on managed_mode / query_gate
    infrastructure errors so a sqlite hiccup doesn't wedge the
    whole MCP server. That contract is RIGHT for read-tool gating;
    it is WRONG for shell egress, where a wedged validator should
    refuse rather than silently allow a potentially-destructive
    command to land. This wrapper applies strict semantics for
    the shell-egress path specifically:

      - dev-mode bypass: allow (env / config) — same as
        `require_active_task`.
      - tool in `_TASK_GATE_EXEMPT`: allow — same.
      - infrastructure error reading managed_mode OR query_gate:
        REFUSE with structured dict `{"error":
        "lifecycle_validator_failure", "detail": "<exc>"}`.
        ShellEgressService translates that to
        `refused_reason="lifecycle_preflight_error"`.
      - unmanaged project: allow (no session → no task ceremony).
      - active session but no active task: REFUSE with dict
        `{"reason": "no_active_task", "detail":
        "open a task before <tool_name>"}`.
        ShellEgressService translates to
        `refused_reason="lifecycle_no_active_task"`.
      - active task present: allow.

    Returns None (allow) or a structured dict (refuse). NEVER
    raises a ToolError — the shell-egress caller wants to
    distinguish refusal reasons in the audit record, not catch
    an exception.
    """
    if tool_name in _TASK_GATE_EXEMPT:
        return None
    import os as _os

    env = _os.environ.get("AIDOCS_AUDIT_DEV_MODE", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return None
    # (#404: config-driven audit bypass removed — env var only.)
    try:
        managed = hub.managed_mode.get_mode(project_root)
    except Exception as exc:
        return {
            "error": "lifecycle_validator_failure",
            "detail": f"managed_mode.get_mode raised: {exc!r}",
        }
    if not managed.get("active"):
        return None
    session_id = str(managed.get("session_id") or "").strip()
    if not session_id:
        return None
    try:
        task_id = hub.query_gate.get_current_task_id(project_root, session_id)
    except Exception as exc:
        return {
            "error": "lifecycle_validator_failure",
            "detail": f"query_gate.get_current_task_id raised: {exc!r}",
        }
    if task_id:
        return None
    return {
        "reason": "no_active_task",
        "detail": f"call task_begin before {tool_name}",
    }


def require_active_task(
    hub: Any,
    project_root: Path,
    tool_name: str,
) -> dict[str, Any] | None:
    """Universal task gate (2026-05-17, Empire directive): every MCP tool
    call — read, write, shell — must have an active task open in a
    managed session. No exceptions outside the bootstrap/lifecycle set
    in ``_TASK_GATE_EXEMPT``.

    Returns a structured rejection dict when blocked, else None (allow).
    Exemptions:

    - Unmanaged projects — no session, no task ceremony. Fresh repos and
      non-AIDOCS projects pass unchanged.
    - DEV_MODE via ``AIDOCS_AUDIT_DEV_MODE`` env var — harness escape
      hatch (#404 removed the config-driven bypass).
    - Tools in ``_TASK_GATE_EXEMPT``: task/session lifecycle, host
      probes, bootstrap entries. Listed explicitly so the surface is
      auditable.

    Pre-2026-05-17 history: the gate fired only on mutating tools
    ("audit hardening A"). Read tools and ``ai_run`` skipped it,
    leaving an attribution-and-bypass hole — an agent could read or
    spawn shell processes without an active task, and any work it did
    via those channels was unattributable. Empire directive widened the
    contract to "every command sits behind task_begin." The check is
    still cheap (one indexed read on session_query_gate) and now runs
    from the central MCP middleware so policy lives in one place.

    Note: this closes attribution. It does NOT close the orthogonal
    bypass where a shell process spawned via ``ai_run`` writes files
    directly via Python ``open()`` / cat redirect / etc. The
    heuristic_judge regex pattern for shell-mediated file writes is
    the matching fix on that surface.
    """
    if tool_name in _TASK_GATE_EXEMPT:
        return None
    # DEV_MODE escape hatch. Env wins (fastest), then config setting.
    import os as _os

    env = _os.environ.get("AIDOCS_AUDIT_DEV_MODE", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return None
    # (#404: the `audit.dev_mode_bypass` config escape is removed; the
    # env var above is deployment/test harness plumbing, not app config.)
    try:
        # Identity doctrine (2026-07-16): resolve the managed session
        # with the CALLING HOST identity so the gate reads the same
        # host-derived key task_begin wrote under (per-conductor
        # mapping first; singleton only when no host identity exists).
        managed = hub.managed_mode.get_mode(
            project_root,
            host_session_id=current_calling_host_session_id(),
        )
        if not managed.get("active"):
            return None
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            return None
        # #463 per-actor slots, extended by #483 to EVERY host-derived
        # actor identity: the caller's OWN task slot is read first
        # (workers key on their lane, non-workers on lane_id=""), so
        # another actor's task_complete on the shared session-level
        # current_task_id can never refuse THIS caller's verified begin.
        # An actor whose slot is empty falls through to the session slot
        # (actor-less legacy callers; pre-#463 workers riding a
        # conductor-opened task; begins that predate the actor-slot
        # write, e.g. the UPS auto-task).
        try:
            from .task_actor_identity import resolve_slot_actor
            from .todo_state_store import ActorTaskStateStore

            actor_id, lane_id, _is_worker = resolve_slot_actor(project_root)
            if actor_id:
                actor_task = ActorTaskStateStore().get(
                    project_root,
                    session_id,
                    actor_id,
                    lane_id,
                )
                if (
                    actor_task
                    and str(actor_task.get("status") or "") == "active"
                    and str(actor_task.get("task_id") or "")
                ):
                    return None
        except Exception:
            pass
        task_id = hub.query_gate.get_current_task_id(
            project_root,
            session_id,
        )
    except Exception:
        # Fail open on infrastructure errors — we'd rather let a call
        # land than wedge the whole server on a transient sqlite hiccup.
        return None
    if task_id:
        return None
    # Raise ToolError so the host UI renders this as a fail (not a
    # green-success dict return). Message is terse by default — the
    # full explanation lives in workflow rules memory.
    #
    # #474 lifecycle truth (extended by War FF, begin-race): the refusal
    # must name when/why the session's task vanished — rule_id, the
    # last-known task + status + timestamp, and the next action. Two
    # snapshot shapes:
    #   - status "active": the task vanished WITHOUT a recorded complete
    #     (expired / cleared out-of-band) — say so loudly.
    #   - any closed status with a task id: a task_complete cleared the
    #     session-level slot. current_task_id is ONE slot per session
    #     shared by all non-worker actors, so another agent's complete
    #     legitimately wipes this caller's begin (the sighted
    #     "begin ok → next call refuses" race). Name the closed task,
    #     status, and timestamp so the clobber is visible, not spooky.
    # Best-effort, fail-quiet: no snapshot → terse refusal (still with
    # rule_id + next_action).
    _expiry_hint = ""
    try:
        from .session_response_ledger import get_lifecycle as _srl_lifecycle

        _snap = _srl_lifecycle(project_root, session_id) or {}
        _snap_task = str(_snap.get("task_id") or "")
        _snap_status = str(_snap.get("status") or "")
        _snap_at = str(_snap.get("updated_at") or "?")
        if _snap_task and _snap_status == "active":
            _expiry_hint = (
                f" NOTE: task '{_snap_task}' was ACTIVE for this session "
                f"as of {_snap_at} but is no longer present "
                f"— it expired or was completed/cleared elsewhere. "
                f"Call task_begin to open a new task."
            )
        elif _snap_task:
            _expiry_hint = (
                f" NOTE: the session's last task '{_snap_task}' was closed "
                f"with status '{_snap_status}' at {_snap_at}. If YOU did not "
                f"close it, another agent sharing session '{session_id}' did — "
                f"task_complete clears the session's single task slot for "
                f"every actor on it. Call task_begin to open a new task."
            )
    except Exception:
        _expiry_hint = ""
    _raise_tool_error(
        f"no active task (call task_begin before {tool_name}) "
        f"[rule_id=no_active_task]{_expiry_hint}"
    )
    return None  # unreachable — _raise_tool_error always raises
