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


def _has_marker(path: Path) -> bool:
    # A db FILE is not enough — any store touch (list_sessions, memory ops, an
    # incidental init_db) creates it. Require the deliberate commission stamp row
    # so an incidental db never force-promotes a non-AIDOCS project to managed.
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
    # Managed-mode bind is authoritative: the host explicitly told us this
    # IS the project, even if the marker hasn't been seeded yet (fresh
    # init flows, test fixtures). Validation applies only to the env/cwd
    # fallback paths, not to an explicit bind.
    if _last_known_project_root is not None:
        return _last_known_project_root
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


def set_calling_conductor_host_session_id(host_session_id: str) -> None:
    """Stamp this MCP process's calling-conductor host_session_id.

    Called once on first session_connect with host_session_id
    provided. Stable for the life of the process — each MCP server
    child belongs to one host window. Re-stamps are a no-op when the
    value matches; a different value indicates a process re-binding
    (rare; could happen if a host window swaps its session via
    /clear-style restart) and we honor the new value.
    """
    global _calling_conductor_host_session_id
    sid = (host_session_id or "").strip()
    if sid:
        _calling_conductor_host_session_id = sid


def current_calling_host_session_id() -> str:
    """Return the calling conductor's host_session_id, or '' when unknown.

    Empty string means callers should fall back to the deprecated
    singleton path on managed_mode reads (legacy contract).
    """
    return _calling_conductor_host_session_id or ""


def set_default_project_root(root: Path) -> None:
    """Called when managed mode activates to set the session default."""
    global _last_known_project_root
    _last_known_project_root = root


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
        "ai_todo",
        "ai_backlog",
        # Host probes — cheap inspection that hosts run on startup or for
        # status display. Gating them would freeze the host UI.
        "aidocs_mode_get",
        "aidocs_route_prompt",
        # CC's hardcoded MCP startup probe alias — must always succeed or
        # CC marks the server unhealthy and stops calling it.
        "session_start",
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

    Doctrine 2026-05-29 (king re-seal — shell-egress strictness):
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
    if env not in ("0", "false", "no", "off"):
        try:
            from .config import get_setting

            if bool(
                get_setting(
                    "audit.dev_mode_bypass",
                    project_root=project_root,
                    default=False,
                ),
            ):
                return None
        except Exception:
            # Config lookup failure for the dev-mode bypass is not
            # a validator failure — the bypass simply doesn't apply.
            # Fall through to the real check.
            pass
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
    """Universal task gate (2026-05-17, king directive): every MCP tool
    call — read, write, shell — must have an active task open in a
    managed session. No exceptions outside the bootstrap/lifecycle set
    in ``_TASK_GATE_EXEMPT``.

    Returns a structured rejection dict when blocked, else None (allow).
    Exemptions:

    - Unmanaged projects — no session, no task ceremony. Fresh repos and
      non-AIDOCS projects pass unchanged.
    - DEV_MODE via ``AIDOCS_AUDIT_DEV_MODE`` env var or
      ``audit.dev_mode_bypass`` config — dev-box escape hatch, same
      pattern as RBAC DEV_MODE.
    - Tools in ``_TASK_GATE_EXEMPT``: task/session lifecycle, host
      probes, bootstrap entries. Listed explicitly so the surface is
      auditable.

    Pre-2026-05-17 history: the gate fired only on mutating tools
    ("audit hardening A"). Read tools and ``ai_run`` skipped it,
    leaving an attribution-and-bypass hole — an agent could read or
    spawn shell processes without an active task, and any work it did
    via those channels was unattributable. King directive widened the
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
    if env not in ("0", "false", "no", "off"):
        try:
            from .config import get_setting

            if bool(
                get_setting(
                    "audit.dev_mode_bypass",
                    project_root=project_root,
                    default=False,
                ),
            ):
                return None
        except Exception:
            pass
    try:
        managed = hub.managed_mode.get_mode(project_root)
        if not managed.get("active"):
            return None
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            return None
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
    _raise_tool_error(f"no active task (call task_begin before {tool_name})")
    return None  # unreachable — _raise_tool_error always raises
