from __future__ import annotations

import contextvars
import os
import sqlite3
from contextlib import ExitStack, contextmanager
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


# #939: WHICH invocation the gate boundary already confirmed, as (tool, args_hash).
#
# A single-use confirm handle can be verified exactly ONCE, by whoever consumes
# it. That makes #916's "second enforcement point" impossible to keep as a
# RE-CHECK of the value — by the time an impl runs, the handle is spent. So the
# second point changes KIND rather than disappearing: it stops asking "is this
# token valid?" and asks "was THIS invocation confirmed at the boundary?".
#
# That still catches #916's stated threat verbatim — "any remote path that
# reaches the impl without crossing it" arrives with no confirmation in scope
# and is refused — while removing the contradiction that broke it: the
# transport strips confirm_token before calling the impl
# (outer_gate_transport: "registry-level contract, not an arg the underlying
# handler understands"), so the impl could never have re-checked the value it
# was told to re-check.
#
# The args_hash is what stops the flag being a blanket "something was
# confirmed": a handle consumed for session A cannot green-light a call that
# arrives binding session B.
_gate_confirmation: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "_gate_confirmation",
    default=None,
)


@contextmanager
def with_gate_confirmation(tool: str, args_hash: str):
    """Mark ONE impl dispatch as already confirmed at the gate boundary.

    Set ONLY after a confirm handle has been successfully consumed, and only
    around that dispatch. Never set it because a caller asked for it — the
    whole value is that an unconfirmed path cannot produce it.
    """
    token = _gate_confirmation.set((str(tool), str(args_hash)))
    try:
        yield
    finally:
        _gate_confirmation.reset(token)


def current_gate_confirmation() -> tuple[str, str] | None:
    """The (tool, args_hash) confirmed at the boundary for this request, if any."""
    return _gate_confirmation.get()


# #935: what the WEB caller's `_meta` actually carried on THIS request.
#
# The transport composes the claims into a host session id and keeps only the
# DIGEST, which is right for everything except diagnosis: an empty host session
# is returned both when the client sent no conversation claim AND when the
# caller is unauthenticated (compose_host_session_id needs both halves). Those
# are different problems with different fixes, and the operator could not tell
# them apart — "i need whoami on web, to see if a tool call actually sends the
# binding ids".
#
# Holds webmcp_identity.attribution(), which carries its own
# `claims_are_unauthenticated: True` label. Request-scoped memory, never a
# store: a claim is the client's word and must not outlive the request that
# asserted it.
_request_web_attribution: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_request_web_attribution",
    default=None,
)


def set_request_web_attribution(attribution: dict | None):
    """Record this request's web identity attribution; returns a reset token."""
    return _request_web_attribution.set(dict(attribution) if attribution else None)


def reset_request_web_attribution(token) -> None:
    """Drop the request's attribution (always in a finally)."""
    _request_web_attribution.reset(token)


def current_request_web_attribution() -> dict | None:
    """This request's web identity attribution, or None off the web surface."""
    return _request_web_attribution.get()


# Sentinel: "do not touch the principal binding" (distinct from an explicit None,
# which deliberately CLEARS an inherited principal for the duration of the scope).
_KEEP_PRINCIPAL = object()


@contextmanager
def with_gate_execution_scope(root: Path, principal: Any = _KEEP_PRINCIPAL):
    """The ONE seam a gate-side dispatch uses to bind an impl call to a tenant.

    #714: the gate has three dispatch sites that hand a tool call down to an
    impl (``OuterGate._registry_invoke_edit``,
    ``ReadOnlyTierRExecutor.run``/``_run`` — called ``RemoteExecutor`` here
    until 2026-08-30, a name with no class behind it, which cost a reader a
    wrong import before it was corrected — and the transport's
    ``_ogt_pt_registry_dispatch``). None of them can pass a
    project root down — ``create_server`` takes none and the impls resolve their
    root per-call — so EVERY one of them must establish the scope here first.
    A dispatch site that forgets falls through to ``_last_known_for_caller()``
    and, with no host identity, to the process-wide module singleton: the
    last-binder-wins cross-tenant leak this helper exists to make impossible.

    Doctrine VI: a guard-carrying seam is SHARED, not duplicated — a fourth
    hand-rolled ``with with_target_project_root(...)`` at a dispatch site is the
    bug pattern, not the fix. Callers with a gate-resolved principal pass it so
    impls authorize on the authoritative identity; callers with none omit the
    argument (an explicit ``None`` clears any inherited principal instead).
    """
    with ExitStack() as stack:
        stack.enter_context(with_target_project_root(Path(root)))
        if principal is not _KEEP_PRINCIPAL:
            stack.enter_context(with_gate_principal(principal))
        yield


class NoAidocsProjectError(RuntimeError):
    """Raised when discovery cannot locate an AIDOCS-enabled project.

    Carries the install remediation text so host adapters can surface it
    to the operator unchanged.
    """


class UnresolvedProjectRoot(Path):
    """A ``Path`` handed back by ``resolve_project_root()`` when discovery has
    PROVEN the process cwd is inside no AIDOCS project (backlog #761).

    Caller analysis (2026-08-19, ai_find mode='references'): resolve_project_root
    has 100+ in-process call sites, none of which handle ``None`` or an
    exception — they all do ``root = resolve_project_root()`` and use the Path
    immediately. Making the fallback raise was MEASURED on Gate 2b
    (2026-08-13): 32 failures across 12 files, four of them this function's own
    pinned session-bind contract, which expects a VALUE when no bind applies.
    So this stays value-compatible: still ``Path.cwd()``, equal to a plain
    Path, usable everywhere a Path is — but tagged, so a caller that cares
    (a write path, a gate decision) can ``isinstance()``-check and refuse
    instead of silently trusting a directory that was just disproved as a
    project root. A caller that wants the raw cwd unconditionally can still
    get it via ``Path.cwd()`` directly and own that choice explicitly.
    """


def stamp_commissioned(project_root: Path) -> None:
    """Write the DELIBERATE commission stamp. Canonical writer, kept next to the
    reader (_has_marker) so the signal has one owning module. Called by the
    commissioning paths (project_init / repair / webmcp init via
    CodeIndexStore.mark_commissioned, which delegates the same write) and by
    test fixtures that simulate a commissioned project.
    """
    from ._sqlite_connect import Durability, connect

    db = project_root / _AIDOCS_INDEX_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    # AUDIT. The commission stamp is a DELIBERATE act, not derived state — this
    # function's own docstring calls it that, and _has_commission_stamp refuses
    # to promote a project to managed without it. Nothing re-derives it: a
    # power cut that un-writes the row leaves a project that an operator
    # commissioned reading as un-commissioned, i.e. ungoverned. Absence after a
    # crash is itself the finding, which is the whole test Durability.AUDIT
    # exists for. Cold path (commissioning happens once), so FULL costs nothing.
    con = connect(db, durability=Durability.AUDIT, row_factory=False)
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
        # #822: read_only=True is the canonical connect's `file:...?mode=ro`,
        # with the `?`/`#` percent-encoding done ONCE in the helper. Hand-built
        # here, a project directory containing either character was parsed as
        # URI syntax and silently resolved to a different db (or none) — and
        # `path` is a PROJECT ROOT, which is operator-chosen.
        from ._sqlite_connect import connect as _canonical_connect

        con = _canonical_connect(db, read_only=True, row_factory=False)
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
            # `aidocs project init` is not a command — cli.COMMANDS has `init`
            # and `project-registry`, never `project`. Law 311bf3e6: a named
            # remedy must exist.
            "or run `aidocs init` to (re)generate its .mcp.json.",
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
        # `aidocs project init` is not a command (cli.COMMANDS has `init`).
        # `/aidocs` is correct HERE and only here: this branch really did test
        # "this project is not commissioned", which is the one condition the
        # commissioning door answers.
        "Run `aidocs init` (/aidocs) in the target project, or set "
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
    if _request_host_session_id.get() or _request_identity_scoped.get():
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
# #672 fail-open 2. A request scope may legitimately know NO host session (the
# stateless-HTTP transport token is NOT one). "Scoped with an empty identity"
# must be distinguishable from "never scoped": the first MASKS the shared
# process global (reading it would hand the caller another actor), the second
# still uses it (one-child-per-window stdio hosts). Without this flag the only
# two options were to fabricate an id or to silently inherit a foreign one.
_request_identity_scoped: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_request_identity_scoped",
    default=False,
)
# The SUBAGENT axis (measured 2026-08-22, Claude Code 2.1.239). A subagent's
# hook payload carries its PARENT's `session_id` AND its parent's
# `transcript_path`; the ONLY field that differs between a parent and its
# children, and between two sibling children, is `agent_id`. It rides here
# beside the other two axes because the alternative is an `agent_id` parameter
# on every signature between a hook entrypoint and the strike ledger.
#
# THE TRANSPORT CANNOT SET THIS, and that is a real limit, not an oversight:
# `stdio_shim.build_forward_headers` forwards X-Aidocs-Host-Session /
# -Host-Kind / -Host-Entrypoint / -Project-Root and nothing else, and one shim
# process serves a whole window, so an MCP tool call from a subagent is
# indistinguishable from its parent's. Only an in-process HOOK handler, which
# holds the payload, can stamp it — so consumers must keep working, byte for
# byte, when it is unset.
_request_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_request_agent_id",
    default=None,
)
# The FastMCP per-request transport isolation token — its OWN axis. It is a
# connection handle, never an actor identity, and nothing derives
# agent_context_id from it.
_request_transport_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_request_transport_session_id",
    default=None,
)


def set_request_transport_session_id(transport_session_id: str):
    """Bind the per-request transport token on its own axis. Reset in finally."""
    return _request_transport_session_id.set(
        (transport_session_id or "").strip() or None,
    )


def reset_request_transport_session_id(token) -> None:
    try:
        _request_transport_session_id.reset(token)
    except Exception:
        pass


def current_request_transport_session_id() -> str:
    """The transport token for this request, or "". NOT an identity."""
    return _request_transport_session_id.get() or ""


# ── THE WINDOW (#876 phase 1) ─────────────────────────────────────────────
#
# WHICH HOST WINDOW is this request coming from — a FOURTH axis, and the only
# one of the four that does not rotate. The three identity axes above all name a
# CONVERSATION, and a conversation is a lease on a window, not the window
# itself: measured 2026-08-23, `/resume` rotated it, `/clear` rotated it again,
# and `/mcp` respawned the shim onto a third value, while the Claude Code
# process — and therefore the window key — stayed identical throughout.
#
# CONTEXTVAR, NOT A PROCESS GLOBAL, for the reason #672 already established one
# axis up: on a multi-tenant daemon a process global is A DIFFERENT ACTOR. The
# window is per-request or it is a lie.
#
# NO FALLBACK LADDER, deliberately, and this is the point of contrast with
# `current_calling_host_session_id` below (which falls back header -> process
# stamp). Operator law 2026-08-23: "fallbacks can stamp wrong data and we cannot
# tell from where. identity has no fallback." A request that carried no window
# header HAS no window; it does not borrow one from the conversation, the
# transport token, or the last request that happened to run on this worker.
#
# PHASE 1 IS ADDITIVE. Nothing reads this to make a decision — no resolution
# path, no gate, no authority check. That is phase 2 (#880) and it carries the
# lockout risk. tests/runtime/test_daemon_records_the_window_876.py contains a
# tripwire that fails when a second reader appears.
_request_window_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_request_window_key",
    default=None,
)

# The wire name, lowercased once. The daemon lowercases every incoming header
# before lookup (headers are case-insensitive on the wire), so the comparison
# key belongs here rather than at each callsite.
_WINDOW_HEADER_LOWER = "x-aidocs-window"


def set_request_window_key(window_key: str):
    """Bind the calling WINDOW for this request. Reset in finally.

    Blank / whitespace is stored as ABSENT. A window key of "" would compare
    unequal to every real window AND unequal to "no window", so phase 2 could
    neither match it nor recognise it as missing — the worst of both.
    """
    return _request_window_key.set((window_key or "").strip() or None)


def reset_request_window_key(token) -> None:
    """Release the binding. Never raises: it runs in a `finally` on the hot path.

    A leaked binding would hand the NEXT request on this worker the PREVIOUS
    window, which is the staleness shape (#876/#859) this work is undoing.
    """
    try:
        _request_window_key.reset(token)
    except Exception:
        pass


def current_request_window_key() -> str:
    """The window THIS REQUEST named, or "" — never a substitute.

    "" means "this request did not prove which window it came from", which is a
    complete and correct answer: a differently-launched host, a wrapper, a
    remote session, a non-win32 box, or a direct HTTP client that speaks to the
    daemon without a shim.
    """
    return _request_window_key.get() or ""


def stamp_request_window(headers, *, header: str = "") -> object:
    """Lift the window header off an already-lowercased header map.

    ``header`` lets the caller pass ``stdio_shim.HEADER_WINDOW`` so the wire
    name has ONE definition at both ends of the wire; it is lowercased here
    because header names are case-insensitive on the wire.

    Returns the reset token unconditionally — including when the header is
    absent — so the caller's `finally` has exactly one shape and cannot leak a
    binding by forgetting the None case.

    Takes the header map rather than reading it at the callsite so the wire
    contract is testable without standing up a server: what the daemon records
    is exactly what this function does.
    """
    name = (header or _WINDOW_HEADER_LOWER).strip().lower()
    try:
        value = (headers or {}).get(name) or ""
    except Exception:  # noqa: BLE001 -- a hostile mapping must not break a call
        value = ""
    return set_request_window_key(str(value))


def set_request_host_session_id(
    host_session_id: str,
    *,
    host_kind: str = "unknown",
    agent_id: str = "",
):
    """Bind the complete request identity for prompt/adapter entrypoints.

    The historical name is retained for callers, but the token carries both
    identity axes so request attribution never falls back to an unrelated
    process-global host kind.
    """
    return set_request_host_identity(
        host_session_id,
        host_kind=host_kind,
        agent_id=agent_id,
    )


def reset_request_host_session_id(token) -> None:
    """Reset the complete identity bound by set_request_host_session_id."""
    reset_request_host_identity(token)


def set_request_host_identity(
    host_session_id: str,
    *,
    host_kind: str,
    agent_id: str = "",
):
    """Set the complete request-scoped host identity.

    Returns opaque ContextVar tokens that must be reset in finally.

    ``agent_id`` — the SUBAGENT axis, and the ONE axis no transport delivers.
    A caller that holds a hook payload (the only place it exists) stamps it
    here; every other caller omits it and the whole identity stack behaves
    exactly as before, byte for byte. Blank/whitespace is stored as absent,
    because some hosts send "" for the main thread and that must land on the
    same identity as sending nothing — otherwise the conductor forks in two
    depending on which channel spoke last.

    The agent token is appended LAST so a caller written against the previous
    3-tuple shape still unpacks in order.
    """
    return (
        _request_host_session_id.set((host_session_id or "").strip() or None),
        _request_host_kind.set(_normalize_host_kind(host_kind)),
        _request_identity_scoped.set(True),
        _request_agent_id.set(str(agent_id or "").strip() or None),
    )


def reset_request_host_identity(token) -> None:
    try:
        session_token, kind_token, scoped_token, *rest = token
        if rest:
            _request_agent_id.reset(rest[0])
        _request_identity_scoped.reset(scoped_token)
        _request_host_kind.reset(kind_token)
        _request_host_session_id.reset(session_token)
    except Exception:
        pass


def current_calling_agent_id() -> str:
    """The SUBAGENT id this request carried, or "" — never a fallback.

    Deliberately has NO process-global rung, unlike host_session_id. That
    fallback exists for one-child-per-window stdio hosts, where the process IS
    the window; a subagent has no process of its own, so a process-global agent
    id could only ever name a stale sibling. An unstamped call is honestly
    agent-less, which is exactly what the main thread is.
    """
    return _request_agent_id.get() or ""


def current_calling_host_session_id() -> str:
    """Return the request host_session_id, then the process stamp.

    #672: a request that IS identity-scoped but knows no host session returns
    the honest "" — it does NOT fall through to the shared process stamp, which
    on the multi-tenant daemon is a DIFFERENT actor. Callers already treat ""
    as "cannot prove identity" and refuse.
    """
    req = _request_host_session_id.get()
    if req:
        return req
    if _request_identity_scoped.get():
        return ""
    return _calling_conductor_host_session_id or ""


def resolve_conductor_key(principal: dict | None = None) -> tuple[str, str]:
    """THE per-conductor managed-mode row key -- ONE HOME, for readers AND writers.

    Returns ``(key, rung)``. The rung is not decoration: it is what makes a
    writer/reader disagreement visible instead of silent, which is the entire
    reason this function exists.

    #906, MEASURED 2026-08-25 ON THE VPS SMOKE. Managed mode is stored
    per-conductor under one string, and THREE DIFFERENT PLACES DERIVED THAT
    STRING THREE DIFFERENT WAYS:

      writer A  session_select over the gate -> bind_selected_webmcp_session()
                passed the BARE OAuth user_id.
      writer B  ai_session(connect) -> set_mode(current_calling_host_session_id()),
                which on the web surface is the COMPOSED id
                web-<sha256(user_id + conversation)>.
      reader    the agent_orchestrator tool gate -> whichever ONE of those two
                it happened to be written against.

    A single reader can only ever match a single writer, so pointing it at
    writer B's key fixed the web agent and broke the gate smoke (15 checks, all
    `managed_mode_inactive`); pointing it back would restore the smoke and
    re-break the web agent. THE READER WAS NEVER THE BUG -- two writers spelling
    one key two ways was. So the key is derived HERE, once, and every side calls
    this (Doctrine XXII: one logic, one home).

    THE RUNGS, AND WHY THE SECOND IS NOT AN IDENTITY FALLBACK. compose_host_session_id
    returns "" unless the host sent a conversation claim in params._meta: the
    Claude web connector sends one, the gate's own smoke harness does not. So:

      host_session   a stamped request id -- the local shim's window id, or the
                     composed web id. Per-conversation, the narrowest key.
      gate_principal an authenticated OAuth principal with NO conversation claim.
                     Conductor granularity is account-wide on such a transport,
                     because the transport genuinely did not say which
                     conversation this is -- naming the principal is the honest
                     answer to that, not a guess at a better one.

    Identity does not move between those rungs: BOTH are the same authenticated
    principal, and the composed id CONTAINS that principal's user_id, so #253
    SXIX -- one tenant's activation must never authorize another tenant's tools
    -- holds on either. What changes is only how finely one principal's own
    conductors are told apart. That is why this may fall through and the auth
    ladder may not.

    THERE IS EXACTLY ONE RUNG, AND THAT IS THE POINT. An earlier draft of this
    function fell back to the authenticated principal's ``user_id`` when no
    conversation claim had arrived, because the deploy smoke's JSON-RPC harness
    sends none and every gated check went red without it. THAT FALLBACK IS
    FORBIDDEN, by two rules written before I got here:

      * #614/G, from the operator's brief: "NEVER substitute: user_id as
        host_session_id". One user is not one host session -- a user with two
        live WebAgent connections collapses into a single managed-mode bucket.
      * _ogt_mcp_under_host_identity, which stamps "" DELIBERATELY on a
        claim-less request: "An honest empty is the correct answer; a borrowed
        one is the bug."

    A claim-less caller is genuinely not identifiable as a conductor, and the
    honest answer to "which conductor is this?" is nobody. So the smoke was
    taught to send the claim the real web connector already sends, rather than
    the product being taught to guess -- a harness that exercises a transport
    production does not have is not evidence.

    ``principal`` is accepted so a caller that already holds the authenticated
    principal need not depend on the ambient ContextVar. It is used ONLY to
    decide whether there is an authenticated caller at all, never as the key.

    FAIL-CLOSED: no stamp yields "", which every caller resolves as INACTIVE.
    """
    hsid = current_calling_host_session_id()
    if hsid:
        return hsid, "host_session"
    if not isinstance(principal, dict):
        principal = current_gate_principal()
    if isinstance(principal, dict) and str(principal.get("user_id") or "").strip():
        # An authenticated caller whose request carried no conversation claim.
        # Named distinctly from "nobody at all" so the refusal can say WHICH of
        # the two happened -- they need different remedies, and reporting one as
        # the other is what made #906 take a live session to diagnose.
        return "", "no_conversation_claim"
    return "", "none"


def request_scoped_host_session_id() -> str:
    """The host session id THIS REQUEST carried, or "", never the process stamp.

    ``current_calling_host_session_id`` deliberately falls back to the shared
    process stamp so legacy single-window stdio hosts keep working. That makes
    it the right answer for "who should I attribute this to", and the WRONG
    answer for "will the caller still be this next time": the stamp is
    process-global, so on a shared daemon it can name a different window
    entirely, and it does not travel with the caller.

    This accessor exposes the distinction WITHOUT changing that fallback, so a
    caller that needs the PROVENANCE of an identity can ask for it. #816: a bind
    founded on the process stamp verifies perfectly and still leaves the next
    tool call refused, because the next call resolves its own identity.
    """
    return _request_host_session_id.get() or ""


def request_identity_is_scoped() -> bool:
    """True iff this call is running inside a REQUEST identity scope.

    The distinction this exposes is "a request arrived and presented no
    identity" versus "there is no request identity scope at all", which are
    otherwise indistinguishable — both resolve to an empty actor id.

    It matters wherever an empty actor id is about to be treated as a
    decision (#599). A request that arrived carrying "unknown" is a REAL agent
    on a host that does not stamp identity; refusing it as though it were a
    different agent is the measured live lockout in which no agent could
    complete its own task. A call with no request scope at all did not come
    through that path and has no such claim.

    Cheap: one ContextVar read, no I/O.
    """
    return bool(_request_identity_scoped.get())


def current_calling_host_kind() -> str:
    """Return the host kind carried by the same identity scope."""
    req = _request_host_kind.get()
    if req:
        return req
    if _request_identity_scoped.get():
        return "unknown"
    return _calling_conductor_host_kind or "unknown"


def current_calling_agent_context_id(project_root: Path | str) -> str:
    """Canonical durable actor id for the calling host in this project.

    Resolves through ``resolve_host_identity`` — THE single authority — rather
    than reading the request stamp directly. Two measured reasons (#599):

      * the direct read substitutes the ``"unknown"`` placeholder whenever
        nothing was stamped, and hands it to ``derive_agent_context_id`` as a
        NON-EMPTY string, walking straight past the refusal that function exists
        to enforce. ``normalize_host_kind`` strips the placeholder; going
        through the authority is what applies it;
      * the direct read cannot reach the durable #587-A record, so a request
        that knows only WHO (the session id) could not recover WHAT HOST a
        previous request had already written down.

    Together those ROTATED this id between two consecutive requests of one
    agent — ``claude_code`` on the stamped one, ``unknown`` on the next — so an
    ownership check keyed on it refused an agent the task it had just opened.

    Still returns an HONEST "" when nothing resolves: recovery must not become
    invention. The caller refuses; it never keys on a fabricated bucket.
    """
    from .agent_memory_epoch import derive_agent_context_id, resolve_host_identity

    host_kind, host_session_id = resolve_host_identity(project_root=project_root)
    return derive_agent_context_id(
        host_kind=host_kind,
        project_root=project_root,
        host_session_id=host_session_id,
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
        from .managed_mode_service import (
            ManagedModeService,
            resolve_managed_session,
        )

        return resolve_managed_session(
            ManagedModeService(),
            root,
            host_session_id=current_calling_host_session_id(),
        )
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
        from .managed_mode_service import (
            ManagedModeService,
            resolve_managed_session,
        )

        return resolve_managed_session(ManagedModeService(), root, host_session_id=sid)
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
        # FIXED — backlog #761. This branch used to answer with a plain
        # Path.cwd(), a directory discover_project_root() has just PROVEN is
        # not a project; that lie is what let the daemon try to create
        # C:\Windows\System32\.MEMORY on 2026-08-13. Raising here instead was
        # MEASURED on Gate 2b (2026-08-13): 32 failures across 12 files,
        # including this function's OWN contract tests
        # (test_resolve_project_root_session_bind: "falls_through", "is_noop"),
        # which pin a VALUE as intended behaviour when no bind applies — and
        # ai_find mode='references' turned up 100+ in-process call sites, none
        # of which handle None/an exception. So the value stays (every caller
        # keeps working, byte for byte) but is now tagged UnresolvedProjectRoot
        # so a caller that cares (a write path, a gate decision) can
        # isinstance()-check and refuse instead of silently trusting it.
        return UnresolvedProjectRoot.cwd()


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
        # THE SEAT IS THE OTHER HALF OF THAT LIFECYCLE (#1021, measured
        # 2026-09-04 over a real gate). `ai_seat` refused with
        #   "no active task (call task_begin before ai_seat)"
        # and ai_task(mode='begin') on that same surface did not satisfy it —
        # the #732 unfollowable-remedy shape: the refusal names a remedy the
        # refused caller cannot reach.
        #
        # It is the same bootstrap recursion the two lines above exist to
        # break, one step further out. A task is owned by an ACTOR: the slot
        # this very gate reads is keyed on `resolve_slot_actor`, and seating is
        # what establishes the conductor/co-conductor actor identity in the
        # first place. Demanding a task before the seat asks the caller to
        # attribute work to an identity it has not been allowed to assume yet.
        #
        # NOTHING IS UNGATED BY THIS. `ai_seat` keeps every authority check it
        # had: the outer gate's scope wall (`_oge_scope_seat` — xaacp_write for
        # enter/co-enter/exit), the managed-mode binding check, and the
        # conductor-vs-subagent guard. This set only governs task ceremony,
        # and a seat writes no source byte to attribute.
        "ai_seat",
        # Filing surfaces — reading todos/backlog (and capturing project
        # backlog) must not require an active task, and must never auto-create
        # one. Their task-owned writes self-gate in-handler where needed.
        # (#83: todo filing now rides ai_task, already exempt above.)
        "ai_backlog",
        # THE REFUSAL-REPORT CHANNEL (#601). ai_issues' own docstring says
        # "Deliberately requires NO active task: this is the refusal-report
        # channel for callers the gate just refused" — but it was never listed
        # here, so the universal gate refused it like anything else: the same
        # documented-not-enforced defect as ai_backlog's, inverted. It became
        # load-bearing when #601 made backlog WRITES genuinely refuse, since
        # every gate refusal footer tells the refused caller to file through
        # ai_backlog(mode='add'). Without this line a caller with no task has
        # no way to report anything at all.
        "ai_issues",
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
        # Same class, same reason, and it MUST work with no task open: ai_whoami
        # exists to debug the state in which a caller can open nothing (the
        # /clear lockout, 2026-08-21). It reads request headers + two
        # query-gate columns and mutates nothing.
        "ai_whoami",
        # Same class again (2026-08-25): ai_gate_explain answers "what did that
        # refusal cost me" from code tables alone. A frozen or unbound caller
        # cannot open a task, and the refusal it is asking about is the reason.
        "ai_gate_explain",
        # Bootstrap entry points. Fresh projects have no managed session
        # yet, so they pass via the unmanaged-mode fail-open below; this
        # listing is defensive for the case where managed mode is on but
        # the operator is re-running bootstrap on a partially-initialised
        # project.
        "project_init",
        "project_bootstrap_or_resume",
        # THE ESCAPE HATCH MAY NOT REQUIRE A TASK IT CANNOT OPEN (#786).
        # admin_clear_reconnect was already exempted from the HOOK gate
        # (tool_gate_service.BOOTSTRAP_EXEMPT, #782) precisely so a locked-out
        # caller could reach it -- and then this gate, a DIFFERENT one on the
        # MCP side, refused it with no_active_task. Opening a task needs
        # ai_task(mode='begin'), which the hook gate refuses for being unbound.
        # A needs B, B needs C, C needs A.
        #
        # MEASURED 2026-08-17: a fresh agent on a fresh project reported
        # "There is no path out from inside the agent" -- connect answered
        # green, every tool refused managed_mode_not_active, and all three
        # named remedies were themselves refused. Exempting one gate while the
        # other still fires is how a hatch stays welded shut; both must agree
        # or neither is a hatch.
        #
        # It is CONDUCTOR-ONLY (refuses when AIDOCS_EXPERT_ID marks a subagent)
        # and clears two flags on a host session that by construction is not
        # bound yet -- it binds nothing and grants no authority, so requiring a
        # task of it protects nothing and costs the operator their session.
        "admin_clear_reconnect",
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
    from .managed_mode_service import resolve_managed_session

    try:
        session_id = resolve_managed_session(hub.managed_mode, project_root)
    except Exception as exc:
        return {
            "error": "lifecycle_validator_failure",
            "detail": f"managed_mode.get_mode raised: {exc!r}",
        }
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


def require_active_task_strict(
    hub: Any,
    project_root: Path,
    tool_name: str,
) -> dict[str, Any] | None:
    """The universal task gate with the NAME-EXEMPTION step skipped (#601).

    `_TASK_GATE_EXEMPT` is keyed on TOOL NAME, so a tool whose READS must stay
    task-free (ai_backlog: an agent with no task must still be able to read the
    backlog) cannot express "…but my WRITES are gated" through it. Before #601
    the update/remove branches of `ai_backlog` tried to, by calling
    `require_active_task` with their own — exempt — name. That call is a no-op,
    and it read as enforcement to every reviewer who saw it, including the
    docstring that promised "add: requires an active task (#82)". A guard that is
    called and does nothing is not a guard (law 183074ae).

    This entry point is for a handler that means to gate ITSELF, per mode. The
    ONLY thing it changes is the exempt-set lookup. Every other fail-open of the
    gate is deliberately preserved: dev mode, an unmanaged project (no session,
    no task ceremony), and an infrastructure error reading managed_mode or the
    query gate — that last one fails QUIET in the SAFE direction on purpose, so
    a transient sqlite fault cannot cost an agent the ability to file work.
    Refusal raises ToolError, exactly as the universal gate does.
    """
    return require_active_task(
        hub,
        project_root,
        tool_name,
        honor_name_exemption=False,
    )


def require_active_task(
    hub: Any,
    project_root: Path,
    tool_name: str,
    *,
    honor_name_exemption: bool = True,
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
    if honor_name_exemption and tool_name in _TASK_GATE_EXEMPT:
        return None
    # #650: THE DECLARED GATE CLASS. Reads and reporting surfaces are not
    # task-gated — see tool_gate_class for the membership rule and the reason
    # (measured: one actor's task_complete clears the session's single task
    # slot, and every other actor on that session went blind AND mute).
    # Consulted only on the universal path: `honor_name_exemption=False`
    # (require_active_task_strict, the #601 self-gating entry point) must keep
    # refusing, so ai_backlog/ai_task WRITES stay gated. The shell-egress
    # preflight never calls this function and is unaffected. Fail closed:
    # tool_gate_class returns "gated" for anything undeclared.
    if honor_name_exemption:
        try:
            from .tool_gate_class import tool_is_task_gate_free

            if tool_is_task_gate_free(tool_name):
                return None
        except Exception:
            pass  # classification unavailable → stay gated (fail closed)
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
        from .managed_mode_service import resolve_managed_session

        session_id = resolve_managed_session(
            hub.managed_mode,
            project_root,
            host_session_id=current_calling_host_session_id(),
        )
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
        #
        # #599: an actor whose OWN slot exists but is CLOSED does not fall
        # through. The session slot is now a holder set (it keeps a peer's
        # still-open task instead of being drained), so the fallback would
        # otherwise let an actor that just completed its own task ride a
        # sibling's begin. Fall-through survives only for callers with NO
        # slot row at all — actor-less legacy callers, pre-#463 workers on a
        # conductor-opened task, and begins that predate the actor-slot write
        # (the UPS auto-task).
        _own_slot_closed = False
        try:
            from .task_actor_identity import resolve_slot_actor
            from .todo_state_store import ActorTaskStateStore

            actor_id, lane_id, _is_worker = resolve_slot_actor(project_root)
            if actor_id:
                # ANY LANE first (#599). Whether a caller resolves as a lane
                # worker rides a per-PROCESS latch, so the SAME agent can
                # present lane "L" on one request and "" on the next. A
                # lane-EXACT read then misses the agent's own open task, the
                # gate falls through to the shared session slot, and the agent
                # is told it has no active task while holding one — the "my own
                # task went invisible" half of #599. Ownership is per ACTOR;
                # the lane is presentation. Mirrors the readers in
                # task_actor_identity and task_complete, which already probe
                # active_row_for_actor before the lane-keyed get.
                _store = ActorTaskStateStore()
                actor_task = _store.active_row_for_actor(
                    project_root,
                    session_id,
                    actor_id,
                )
                if actor_task is None:
                    actor_task = _store.get(
                        project_root,
                        session_id,
                        actor_id,
                        lane_id,
                    )
                if actor_task:
                    if (
                        str(actor_task.get("status") or "") == "active"
                        and str(actor_task.get("task_id") or "")
                    ):
                        return None
                    _own_slot_closed = True
        except Exception:
            _own_slot_closed = False
        task_id = (
            ""
            if _own_slot_closed
            else hub.query_gate.get_current_task_id(
                project_root,
                session_id,
            )
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
