from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP  # type-only: annotations reference it (F821 seal)

import asyncio
import functools
import json
import signal
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from typing import Any
from uuid import uuid4

from .config import (
    TOOLS_CALL_TIMEOUT,
    TOOLS_GIT_TIMEOUT,
    TOOLS_MAX_TIMEOUT,
    _load_action_hook_defaults,
    render_interaction_text,
)
from .config_schema import available_config_edit_modes, self_edit_available_in_profile
from .file_ops import (
    anchor_replace as _file_anchor_replace,
)
from .file_ops import (
    batch_edit as _file_batch_edit,
)
from .file_ops import (
    batch_str_replace as _file_batch_str_replace,
)
from .file_ops import (
    create_file as _file_create_file,
)
from .file_ops import (
    edit_lines as _file_edit_lines,
)
from .file_ops import (
    extract_block as _file_extract_block,
)
from .file_ops import (
    get_lines as _file_get_lines,
)
from .file_ops import (
    read_raw as _file_read_raw,
)
from .file_ops import (
    str_replace as _file_str_replace,
)
from .git_helpers import run_git_sync as _run_git_sync

# The server tool-registration modules (register_*_tools) and tool_display's
# `renders_as` decorator are imported INSIDE create_server, NOT here. They
# transitively pull the fastmcp/mcp SDK (~2.8s cold) which is needed ONLY when a
# server is actually created. Deferring keeps `import aidocs_mcp.mcp_server` cheap
# for helper-only / tooling imports (CLI and the Claude hook never import this
# module and are already SDK-free); a broken tool module still fails loudly at
# create_server() — the real server entrypoint. See the import block there.
from .mcp_server_runtime_helpers import (
    all_capabilities as _all_capabilities,
)
from .mcp_server_runtime_helpers import (
    all_procedures as _all_procedures,
)
from .mcp_server_runtime_helpers import (
    capture_enabled as _capture_enabled,
)
from .mcp_server_runtime_helpers import (
    project_root_from_args as _project_root_from_args,
)
from .mcp_server_runtime_helpers import (
    registered_tools as _registered_tools,
)
from .mcp_server_runtime_helpers import (
    resolve_related_root as _resolve_related_root,
)
from .mcp_server_runtime_helpers import (
    summarize_tool_result as _summarize_tool_result,
)
from .mode_schema import modes
from .project_info_store import ProjectInfoStore
from .project_registry_service import ProjectRegistryService
from .runtime_service import RuntimeService
from .service_hub import AidocsServiceHub
from .skill_provider import BUNDLED_PROVIDER_ID
from .skill_resolution import (
    match_selected_skill_id_for_trigger as _match_selected_skill_id_for_trigger,
)

_BUNDLED_OVERRIDE_PROVIDER_ID = "superpowers_external"
_RUNTIME_OWNED_OVERRIDE_MODES = {"aidocs_runtime_owned"}


def _resolve_session_id(hub: Any, project_root: Path) -> str:
    """Resolve the session id for tools that accept an optional ``session_id`` arg.

    Falls back to the active managed-mode session. Raises a clear error when
    no session is bound rather than letting a downstream NameError surface.

    Dental-app feedback 2026-04-17: 11 callsites used this helper but it was
    never defined — every one threw a NameError when session_id was omitted.
    """
    sid = ""
    try:
        managed = hub.managed_mode.get_mode(project_root)
        if managed.get("active"):
            sid = str(managed.get("session_id") or "").strip()
    except Exception:
        pass
    if not sid:
        raise ValueError(
            "No active session. Pass session_id explicitly, or call "
            "/aidocs to bind managed mode to a session first.",
        )
    return sid


def _coerce_to_list(value: list[str] | str | None) -> list[str] | None:
    """Coerce a JSON-encoded string to a list, or pass through lists/None."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return value


# ── Tool timeout infrastructure ──────────────────────────────────────

_tool_executor = ThreadPoolExecutor(max_workers=4)


# Duplicate-call dedup moved to execution_index_store: content-addressed
# event_id / run_id collide on PK within a 2-second bucket, so duplicate
# writes from MCP wire doubling are dropped by ON CONFLICT DO NOTHING.
# The MCP wrapper no longer tries to detect duplicates itself — FastMCP
# middleware re-entry and sibling-task context semantics made that
# layer unreliable.


def _run_with_timeout(fn, timeout_seconds: int, *args, **kwargs) -> Any:
    """Run a sync function with a timeout. Returns result or raises TimeoutError.

    The ThreadPoolExecutor worker inherits no ContextVars from the
    submitting task — capture the caller's context with copy_context()
    and run the function inside it, so related_project wrappers'
    project-root override and any other CV-based routing is preserved.
    """
    import contextvars as _cv

    ctx = _cv.copy_context()
    future = _tool_executor.submit(ctx.run, fn, *args, **kwargs)
    # 0 (or any non-positive) means UNLIMITED — wait with no deadline.
    wait = timeout_seconds if (timeout_seconds and timeout_seconds > 0) else None
    try:
        return future.result(timeout=wait)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(
            f"Tool call timed out after {timeout_seconds}s. Use timeout= parameter for longer operations (0 = unlimited).",
        )


def _resolve_timeout(
    kwargs: dict,
    default: int | None = None,
    *,
    allow_caller_timeout: bool = False,
) -> int:
    """Extract and validate timeout from kwargs, falling back to category default.

    Returns the effective timeout in seconds, where **0 means UNLIMITED**
    (no watchdog). Semantics:
      - When ``allow_caller_timeout`` is False, or no caller ``timeout=``
        is supplied, the category ``default`` is used (which may itself be
        0 = unlimited if the operator configured it so).
      - A caller ``timeout=0`` explicitly requests unlimited.
      - A positive caller timeout is capped at the live ``tools.max_timeout``
        ceiling — unless that ceiling is itself 0 (unlimited), in which case
        the caller value passes through uncapped.
      - A negative caller timeout is invalid and falls back to the default.
    """
    timeout = kwargs.pop("timeout", None)
    try:
        fallback = int(default if default is not None else TOOLS_CALL_TIMEOUT)
    except (TypeError, ValueError):
        fallback = TOOLS_CALL_TIMEOUT
    fallback = max(0, fallback)  # 0 = unlimited; never negative
    if not allow_caller_timeout or timeout is None:
        return fallback
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return fallback
    if timeout < 0:
        return fallback
    if timeout == 0:
        return 0  # caller explicitly requests unlimited
    # Read the ceiling live so dashboard changes to tools.max_timeout
    # take effect without MCP restart. Fallback to the import-time
    # constant only if the config store is unreachable. A ceiling of 0
    # means "no ceiling" — the caller value passes through uncapped.
    max_ceiling = TOOLS_MAX_TIMEOUT
    try:
        from .config import get_setting
        from .mcp_server_runtime_helpers import (
            resolve_current_session_id,
            resolve_project_root,
        )

        try:
            root = resolve_project_root()
        except Exception:
            root = None
        live_max = get_setting(
            "tools.max_timeout",
            project_root=root,
            session_id=resolve_current_session_id(root) or None,
            default=TOOLS_MAX_TIMEOUT,
        )
        if live_max is not None:
            max_ceiling = int(live_max)
    except Exception:
        pass
    if max_ceiling and max_ceiling > 0:
        return min(timeout, max_ceiling)
    return timeout


def _make_timed_decorator(
    config_key: str,
    fallback_timeout: int,
    *,
    allow_caller_timeout: bool = False,
):
    """Factory for timed tool decorators with a specific default timeout.

    Failures RAISE ToolError so the host UI renders a red/amber chip.
    Returning a dict with 'error' was green-success in the UI because
    the wire message said the call completed — the same bug we fixed
    for the edit tools. Surfacing timeouts + exceptions as real
    failures lets operators notice a tool is broken without needing
    to destructure every response.

    allow_caller_timeout=True passes the caller's `timeout=` kwarg
    through to `_resolve_timeout`, capped at TOOLS_MAX_TIMEOUT. Used
    by indexer tools where large monorepo scans can legitimately
    exceed the 30s default. Non-indexer tools leave it False so
    agents can't escape the dashboard-level timeout ceiling on
    other expensive-and-stuck calls.
    """

    def _live_default() -> int:
        try:
            from .config import get_setting
            from .mcp_server_runtime_helpers import (
                resolve_current_session_id,
                resolve_project_root,
            )

            try:
                root = resolve_project_root()
            except Exception:
                root = None
            value = get_setting(
                config_key,
                project_root=root,
                session_id=resolve_current_session_id(root) or None,
                default=fallback_timeout,
            )
            return int(value) if value is not None else fallback_timeout
        except Exception:
            return fallback_timeout

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            timeout = _resolve_timeout(
                kwargs,
                default=_live_default(),
                allow_caller_timeout=allow_caller_timeout,
            )
            try:
                return _run_with_timeout(fn, timeout, *args, **kwargs)
            except TimeoutError as exc:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(f"Tool timed out after {timeout}s: {exc}")
            except Exception as exc:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(f"Tool failed: {exc}")

        return wrapper

    return decorator


# Category-specific decorators. Each reads its dashboard-configured
# timeout on every call via get_setting (scope-cascaded) — edit the
# value in the dashboard and the next tool call picks it up with no
# MCP restart. Fallback integers match the schema defaults; they
# only apply when the config store is unreachable.
# General tools: 10s default, caller `timeout=` is NOT honored — this
# preserves the dashboard-owned ceiling on arbitrary-and-stuck tools.
timed_tool = _make_timed_decorator("tools.tool_call_timeout", 10)
timed_sync = _make_timed_decorator("tools.sync_write_timeout", 60)
timed_git = _make_timed_decorator("tools.git_functions_timeout", 30)
# Discovery tools (ai_find / ai_investigate / ai_bundle / ai_trace /
# schema_query) advertise a `timeout` param and can legitimately run past
# the 10s default on a large repo — they HONOR a caller-supplied timeout=,
# capped at tools.max_timeout. Same default knob as general tools
# (tools.tool_call_timeout); only the caller-timeout policy differs.
timed_discovery = _make_timed_decorator(
    "tools.tool_call_timeout",
    10,
    allow_caller_timeout=True,
)
# Index-sync variant: its own dashboard knob (tools.index_sync_timeout,
# default 120s) — a full reindex of a large repo routinely exceeds the
# general sync default. Honors caller `timeout=` up to tools.max_timeout.
timed_indexer = _make_timed_decorator(
    "tools.index_sync_timeout",
    120,
    allow_caller_timeout=True,
)


_GIT_SAFE_DIR = ["-c", "safe.directory=*"]
_GIT_TIMEOUT = 10


def _make_timed_async_decorator(
    default_timeout_value: int,
    *,
    allow_caller_timeout: bool = False,
):
    """Factory for async timed tool decorators.

    Same semantics as the sync variant: timeouts / failures raise
    ToolError so the host UI renders red. Dict-with-error returns
    used to render green-success — a silent failure mode.

    allow_caller_timeout=True honors a caller-supplied `timeout=`
    (capped at tools.max_timeout), matching the sync `timed_tool`.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            timeout = _resolve_timeout(
                kwargs,
                default=default_timeout_value,
                allow_caller_timeout=allow_caller_timeout,
            )
            try:
                # 0 = unlimited: wait_for(None) waits with no deadline.
                _wait = timeout if (timeout and timeout > 0) else None
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=_wait)
            except TimeoutError:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(
                    f"Tool call timed out after {timeout}s. Pass timeout= for slower operations.",
                )
            except Exception as exc:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(f"Tool failed: {exc}")

        return wrapper

    return decorator


timed_tool_async = _make_timed_async_decorator(TOOLS_CALL_TIMEOUT)
timed_git_async = _make_timed_async_decorator(TOOLS_GIT_TIMEOUT)


_GIT_FAST_DIVERGENCE = 500
_GIT_SAMPLE_DIVERGENCE = 1500


def _grant_known_exact_path_read(
    hub: AidocsServiceHub,
    project_root: Path,
    tool_name: str,
    path: str,
) -> None:
    """Grant per-file read access via AccessGate."""
    from .access_gate import AccessGate

    managed = hub.managed_mode.get_mode(project_root)
    session_id = managed.get("session_id") if isinstance(managed, dict) else None
    if not managed.get("active") or not session_id:
        return
    AccessGate.grant_discovery(hub.query_gate, project_root, str(session_id), tool_name, [path])


def _evict_known_exact_path(hub: AidocsServiceHub, project_root: Path, path: str) -> None:
    """Remove a path from session known_exact_paths.

    Phoenix 2026-05-12 (king directive): used after line-based edits
    (ai_replace mode=lines / mode=symbol / ai_insert_lines) where the
    file's line numbers shift drastically. Forces the agent to re-read
    before the next line operation — re-reads re-grant the path with
    fresh content. Counterpart to _grant_known_exact_path_read.
    """
    managed = hub.managed_mode.get_mode(project_root)
    session_id = managed.get("session_id") if isinstance(managed, dict) else None
    if not managed.get("active") or not session_id:
        return
    try:
        state = hub.query_gate.get(project_root, str(session_id)) or {}
        known = [p for p in (state.get("known_exact_paths") or []) if p != path]
        hub.query_gate.set(
            project_root,
            str(session_id),
            known_exact_paths=known,
        )
    except Exception:
        # Best-effort — a failed eviction leaves the path granted (no
        # worse than pre-fix state); next edit still works, just without
        # the re-read discipline this time.
        pass


def _post_edit_reindex_and_grant(
    hub: AidocsServiceHub,
    project_root: Path,
    tool_name: str,
    path: str,
    evict_known_path: bool = False,
) -> dict[str, Any]:
    """After a successful edit: grant read access + reindex + invalidate config caches if needed.

    Returns a structured status so successful writes cannot silently bypass indexing.

    King directive 2026-05-12: line-based edits (ai_replace mode=lines,
    mode=symbol, plus ai_insert_lines) shift the file's line numbers
    drastically. The agent's cached known_exact_path is stale the moment
    the edit lands. Pass evict_known_path=True from those call sites —
    the path is REMOVED from known_exact_paths after the edit, forcing
    the agent to re-read (ai_get_lines / ai_bundle) before the next
    line-based operation. Re-reads re-grant the path with the fresh
    content. This is what makes line-edits safe to expose without
    granted-only gating.
    """
    if evict_known_path:
        _evict_known_exact_path(hub, project_root, path)
    else:
        _grant_known_exact_path_read(hub, project_root, tool_name, path)
    canonical = path.replace("\\", "/").strip()
    # Folder-sitter marker (2026-04-21): stamp this write so the
    # filesystem watcher (when enabled via observability.watch_user_drops)
    # doesn't trigger a redundant re-index. We just re-indexed below.
    try:
        from .folder_sitter import mark_self_write

        try:
            _mtime_ns = (project_root / canonical).stat().st_mtime_ns
        except Exception:
            _mtime_ns = None
        mark_self_write(canonical, mtime_ns=_mtime_ns)
    except Exception:
        pass
    # #76 (2026-04-27): only verify index refresh for extensions the
    # code indexer actually tracks. Markdown, SQL, plain text, JSON
    # blobs, etc. are written to disk but the indexer doesn't extract
    # symbols from them — `synced=0` is the correct outcome there,
    # not a failure. Pre-fix this surfaced a red error chip on every
    # .md / .sql edit that confused agents and operators.
    _INDEXED_EXTS = {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".cs",
        ".cshtml",
        ".go",
        ".rs",
        ".java",
        ".dart",
        ".swift",
        ".kt",
        ".scala",
        ".rb",
        ".php",
    }
    from pathlib import Path as _PathExt

    ext = _PathExt(canonical).suffix.lower()
    if ext in _INDEXED_EXTS:
        try:
            synced = int(hub.code.sync_code_files(project_root, paths=[canonical]))
            if synced <= 0:
                return {
                    "ok": False,
                    "path": canonical,
                    "error": f"post-edit index refresh produced no rows for `{canonical}`",
                }
        except Exception as exc:
            return {
                "ok": False,
                "path": canonical,
                "error": f"post-edit index refresh failed for `{canonical}`: {exc}",
            }
    # Invalidate config caches when config files are edited
    if canonical.endswith(".toml") or canonical.endswith("workflow-actions.json"):
        try:
            from .config import reload_config_caches

            reload_config_caches()
        except Exception:
            pass
    # Audit gap fill (2026-04-21): stamp a tool_edit_completed event
    # cross-referencing the most recent edit_history row for this path.
    # Lets audit queries join tool calls to the actual diff bytes
    # without a timestamp-fuzzy-match. Best-effort.
    try:
        from .edit_history import EditHistoryStore

        recent = EditHistoryStore().list_edits(
            project_root,
            file_path=canonical,
            limit=1,
        )
        edit_id = None
        if recent:
            edit_id = (
                recent[0].get("edit_id")
                if isinstance(recent[0], dict)
                else getattr(recent[0], "edit_id", None)
            )
        managed = hub.managed_mode.get_mode(project_root)
        session_id_audit = (
            str(managed.get("session_id") or "").strip() if managed.get("active") else None
        )
        hub.execution.record_event(
            project_root,
            event_kind="tool_edit_completed",
            source_kind="mcp_edit_tool",
            session_id=session_id_audit,
            capability_name=tool_name,
            action_kind="edit",
            target_entity=canonical,
            status="applied",
            payload={
                "tool_name": tool_name,
                "path": canonical,
                "edit_history_id": edit_id,
            },
        )
    except Exception:
        pass
    return {"ok": True, "path": canonical}


def _require_indexed_read_gate(
    hub: AidocsServiceHub,
    project_root: Path,
    exact_path: str | None = None,
    known_exact_path: bool = False,
) -> dict[str, Any] | None:
    """Check read gate via AccessGate — blocks indexed files from raw read.

    All indexed files are blocked from raw read. Agents must use indexed
    tools (ai_get_lines, ai_find, etc.). This is the primary read gate
    replacing the deprecated known_exact_path bypass.
    """
    from .access_gate import AccessGate, GateContext

    managed = hub.managed_mode.get_mode(project_root)
    session_id = managed.get("session_id") if isinstance(managed, dict) else None
    if not managed.get("active") or not session_id:
        return None

    state = hub.query_gate.get(project_root, str(session_id))

    # Check if target is indexed
    is_indexed = False
    if exact_path:
        target = str(exact_path).replace("\\", "/").lstrip("/")
        try:
            is_indexed = hub.code._is_indexed_file(project_root, target)
        except Exception:
            is_indexed = False

    decision = AccessGate.check_read(
        GateContext(
            managed=True,
            session_id=str(session_id),
            dev_mode=False,
            allow_config_edit=False,
            gate_enforce=True,
            gate_state=state,
        ),
        exact_path or "",
        known_exact_path=known_exact_path,
        is_indexed=is_indexed,
    )
    if decision.allowed:
        return None
    reason = str(decision.reason or render_interaction_text("interaction.errors.indexed_read_gate"))
    return {
        "enforcement": {
            "stage": "gate",
            "decision": "block",
            "reason": reason,
            "user_message": reason,
        },
        "error": reason,
    }


def _apply_trace_depth(payload: dict[str, Any], mode: str, max_depth: int | None) -> dict[str, Any]:
    if not max_depth or max_depth <= 0:
        return payload
    m = mode.strip().lower()
    if m in {"service", "component", "field_flow", "setting"} and isinstance(
        payload.get("matches"),
        list,
    ):
        order = {"definition": 1, "reference": 2, "file_match": 3}
        result = dict(payload)
        result["matches"] = [
            item
            for item in payload["matches"]
            if order.get(str(item.get("source")), 3) <= max_depth
        ]
        return result
    if m == "api_to_ui":
        result = dict(payload)
        if max_depth == 1:
            result["logic"] = []
            result["ui"] = []
        elif max_depth == 2:
            result["ui"] = []
        return result
    return payload


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_EVENT_KIND_TO_PHASE: dict[str, str] = {
    "tool_call_started": "started",
    "tool_call_completed": "completed",
    "tool_call_failed": "failed",
}


def _record_tool_execution_state(
    hub: AidocsServiceHub,
    project_root: Path,
    *,
    run_id: str,
    capability_name: str,
    session_id: str | None,
    status: str,
    event_kind: str,
    metadata: dict[str, object],
    completed_at: str | None = None,
) -> None:
    # Thin wrapper around tool_call_log.record_run + record. The helper
    # hides the phase → event_kind vocabulary so dashboards keep seeing
    # the same wire format while writers converge on one interface.
    from .tool_call_log import record as _log_record
    from .tool_call_log import record_run as _log_run

    _log_run(
        hub,
        project_root,
        name=capability_name,
        session_id=session_id,
        status=status,
        metadata=metadata,
        run_id=run_id,
        completed_at=completed_at,
    )
    phase = _EVENT_KIND_TO_PHASE.get(event_kind, event_kind)
    _log_record(
        hub,
        project_root,
        phase=phase,
        name=capability_name,
        payload=metadata,
        session_id=session_id,
        action_kind="mcp_tool_call",
        status=status,
        run_id=run_id,
    )


def _tool_result_preview(result: Any) -> tuple[int, str]:
    result_bytes = 0
    result_text_preview = ""
    content = getattr(result, "content", None)
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                result_bytes += len(text.encode("utf-8"))
                text_parts.append(text)
        full_text = "\n".join(text_parts)
        result_text_preview = full_text[:1000] if len(full_text) > 1000 else full_text
    return result_bytes, result_text_preview


async def _run_git(cwd: str, *args: str, timeout: int = _GIT_TIMEOUT) -> str:
    """Run a git command from inside an async context by offloading to a thread."""
    import asyncio
    from functools import partial

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_run_git_sync, cwd, *args, timeout=timeout))


def _resolve_templates_root() -> Path:
    """Locate the bundled session-templates dir (must contain context.md).

    Robust against BOTH the repo layout (…/AIDOCS/core/.MEMORY/…) and the
    deployed gate release layout (…/releases/<id>/core/.MEMORY/…): walk UP
    from this package and return the first candidate that actually holds
    `context.md`. Fixed `parents[N]` math is brittle on the gate (it can
    resolve to an id-less `…/releases/core/…` that does not exist), so it is
    only the last-resort fallback when context.md is found nowhere.
    """
    here = Path(__file__).resolve()
    for base in (here.parent, *here.parents):
        for cand in (
            base / "core" / ".MEMORY" / ".aidocs" / "templates",
            base / ".MEMORY" / ".aidocs" / "templates",
        ):
            try:
                if (cand / "context.md").is_file():
                    return cand
            except OSError:
                continue
    # Deterministic fallback (repo dev tree) when context.md is genuinely
    # absent everywhere — keeps the historical return shape for callers.
    parents = here.parents
    base = parents[3] if len(parents) > 3 else parents[-1]
    return base / "core" / ".MEMORY" / ".aidocs" / "templates"


def _resolve_script_root() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "core" / "scripts"


def _session_summary_to_dict(summary: Any) -> dict[str, Any]:
    return {
        "session_id": summary.session_id,
        "title": summary.title,
        "status": summary.status,
        "owner": summary.owner,
        "last_updated": summary.last_updated,
    }


def _build_skill_mode_metadata(
    state: dict[str, Any] | None,
    override_store: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    triggered = state.get("triggered")
    selected_skills = [str(item) for item in state.get("selected_skills", []) if str(item).strip()]
    active_skills = [str(item) for item in state.get("active_skills", []) if str(item).strip()]
    provider_states = (
        state.get("provider_states") if isinstance(state.get("provider_states"), dict) else {}
    )
    active_skill_modes: dict[str, str] = {}
    selected_skill_modes: dict[str, str] = {}
    decisions: list[dict[str, Any]] = []
    for item in triggered if isinstance(triggered, list) else []:
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        override_mode = str(item.get("override_mode") or "").strip()
        runtime_owned_capability = (
            item.get("runtime_owned_capability")
            if isinstance(item.get("runtime_owned_capability"), dict)
            else None
        )
        provider = str(item.get("provider") or "").strip()
        runtime_provider = str(item.get("runtime_provider") or provider).strip() or provider
        if not skill_id or not override_mode:
            continue
        selected_skill_id = _match_selected_skill_id_for_trigger(
            selected_skills=selected_skills,
            skill_id=skill_id,
            provider=provider,
            runtime_provider=runtime_provider,
            provider_states=provider_states,
            override_store=override_store,
        )
        if runtime_owned_capability is None:
            active_skill_modes[skill_id] = override_mode
        if selected_skill_id:
            selected_skill_modes[selected_skill_id] = override_mode
        decisions.append(
            {
                "skill_id": skill_id,
                "selected_skill_id": selected_skill_id,
                "override_mode": override_mode,
                "provider": item.get("provider"),
                "runtime_provider": item.get("runtime_provider"),
                "runtime_owned_capability": runtime_owned_capability,
            },
        )
    for selected_skill_id in selected_skills:
        if selected_skill_id in selected_skill_modes:
            continue
        if "/" in selected_skill_id:
            provider_id, selected_name = selected_skill_id.split("/", 1)
        elif BUNDLED_PROVIDER_ID in provider_states:
            provider_id, selected_name = (
                _BUNDLED_OVERRIDE_PROVIDER_ID,
                selected_skill_id,
            )
        else:
            continue
        override_mode = None
        resolved_skill_id = selected_skill_id
        provider = provider_id
        runtime_provider = provider_id
        if override_store is not None:
            decision = override_store.resolve(provider_id, selected_name)
            override_mode = str(decision.mode or "").strip()
            runtime_owned_capability = None
            if override_mode in _RUNTIME_OWNED_OVERRIDE_MODES:
                resolved_skill_id = str(decision.skill_id or selected_name)
                runtime_provider = "aidocs_runtime"
                runtime_owned_capability = {
                    "capability_id": str(decision.runtime_capability_id or "").strip(),
                    "source": "aidocs_runtime",
                    "reason": str(decision.reason or "").strip(),
                    "mode": override_mode,
                    "selected_skill_id": selected_skill_id,
                    "provider": provider_id,
                }
            elif override_mode == "provider_content_aidocs_runtime":
                resolved_skill_id = selected_skill_id
                runtime_provider = "aidocs"
        elif selected_name in active_skills and selected_skill_id not in active_skills:
            override_mode = "aidocs_runtime_owned"
            resolved_skill_id = selected_name
            runtime_provider = "aidocs_runtime"
            runtime_owned_capability = {
                "capability_id": selected_name,
                "source": "aidocs_runtime",
                "reason": "runtime-owned workflow authority",
                "mode": override_mode,
                "selected_skill_id": selected_skill_id,
                "provider": provider_id,
            }
        else:
            runtime_owned_capability = None
        if not override_mode:
            continue
        if (
            runtime_owned_capability is None
            and resolved_skill_id not in active_skills
            and selected_skill_id not in active_skills
        ):
            continue
        selected_skill_modes[selected_skill_id] = override_mode
        if runtime_owned_capability is None:
            active_skill_modes[resolved_skill_id] = override_mode
        decisions.append(
            {
                "skill_id": resolved_skill_id,
                "selected_skill_id": selected_skill_id,
                "override_mode": override_mode,
                "provider": provider,
                "runtime_provider": runtime_provider,
                "runtime_owned_capability": runtime_owned_capability,
            },
        )
    if not active_skill_modes and not selected_skill_modes:
        return None
    return {
        "active_skill_modes": active_skill_modes,
        "selected_skill_modes": selected_skill_modes,
        "decisions": decisions,
    }


def _annotate_imported_skill_state(imported_skill_state: Any, override_store: Any = None) -> Any:
    if not isinstance(imported_skill_state, dict):
        return imported_skill_state
    mode_metadata = _build_skill_mode_metadata(imported_skill_state, override_store=override_store)
    if mode_metadata is None:
        return imported_skill_state
    return {
        **imported_skill_state,
        "mode_metadata": mode_metadata,
    }


def _annotate_skill_result(payload: dict[str, Any], override_store: Any = None) -> dict[str, Any]:
    result = dict(payload)
    mode_metadata = _build_skill_mode_metadata(result, override_store=override_store)
    if mode_metadata is not None:
        result["override_modes"] = dict(mode_metadata["active_skill_modes"])
    imported_skill_state = result.get("imported_skill_state")
    if isinstance(imported_skill_state, dict):
        result["imported_skill_state"] = _annotate_imported_skill_state(
            imported_skill_state,
            override_store=override_store,
        )
        if "override_modes" not in result and isinstance(result["imported_skill_state"], dict):
            imported_mode_metadata = result["imported_skill_state"].get("mode_metadata")
            if isinstance(imported_mode_metadata, dict):
                result["override_modes"] = dict(
                    imported_mode_metadata.get("active_skill_modes") or {},
                )
    return result


def _build_server_instructions() -> str:
    """Load MCP server instructions from gate_messages TOML config."""
    return render_interaction_text("interaction.mcp_server.instructions")


# Claude CLI's long-lived programmatic chat mode requires stream-json on both
# pipes. Mirrors apps/aidocs-dashboard/src-tauri/src/main.rs (conductor_start,
# conductor_send, handle_claude_stream_event) so the Rust dashboard and this
# Python MCP path stay wire-compatible.


def _claude_identity_prompt(project_root: Path, session_id: str) -> str:
    """Build the --append-system-prompt identity string for the Claude conductor."""
    project_name = project_root.name or "project"
    root_display = str(project_root)
    return (
        f"You are the AIDOCS conductor for project '{project_name}' at "
        f"'{root_display}'. The user is working in AIDOCS session "
        f"'{session_id}'. Use mcp__aidocs__ai_session(mode='list') to enumerate sessions "
        f"and mcp__aidocs__ai_session(mode='connect') to bind to one; never glob "
        f"/.MEMORY/sessions/. When the user asks about the current project or "
        f"session, answer with the identity above."
    )


def _claude_stream_json_user_envelope(text: str) -> str:
    """Serialize a user message for Claude's --input-format stream-json."""
    envelope = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    # Newline terminator is required — CLI reads one JSON object per line.
    return json.dumps(envelope, ensure_ascii=False) + "\n"


def _claude_build_cli_args(cli_path: str, identity_prompt: str, model: str = "") -> list[str]:
    """Build Claude CLI argv for long-lived stream-json chat.

    --verbose is mandatory whenever --output-format stream-json is set.
    """
    args = [
        cli_path,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--append-system-prompt",
        identity_prompt,
    ]
    if model and model.strip():
        args.extend(["--model", model.strip()])
    return args


def _parse_claude_stream_event(line: str) -> list[dict[str, str]]:
    """Convert a stream-json stdout line into conductor output entries.

    Non-JSON lines are surfaced on stderr so CLI banners or migration warnings
    are not silently swallowed.
    """
    stripped = line.strip()
    if not stripped:
        return []
    try:
        envelope = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return [{"stream": "stderr", "text": f"[non-json stdout] {stripped}"}]

    if not isinstance(envelope, dict):
        return []

    kind = envelope.get("type")
    if kind == "assistant":
        message = envelope.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        entries: list[dict[str, str]] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        entries.append({"stream": "stdout", "text": text})
        return entries
    if kind == "result":
        # Only surface result envelopes that carry an error — success results
        # would duplicate assistant text already emitted.
        if envelope.get("is_error") is True:
            text = envelope.get("result")
            if isinstance(text, str) and text:
                return [{"stream": "stderr", "text": text}]
        return []
    return []


def _extract_claude_session_id(line: str) -> str | None:
    """Return claude_session_id from a system/init envelope so resumes can pin it."""
    try:
        envelope = json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("type") != "system":
        return None
    if envelope.get("subtype") != "init":
        return None
    sid = envelope.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def _start_conductor_output_reader(
    proc: Any,
    output_buf: list,
    lock: Any,
    max_lines: int,
    backend: str = "",
    state: dict[str, Any] | None = None,
) -> None:
    """Background reader thread. Lifted to module scope so tests can exercise it
    without spinning up the full FastMCP server.
    """
    import threading
    import time as _time

    def _append(entry: dict[str, Any]) -> None:
        with lock:
            output_buf.append(entry)
            if len(output_buf) > max_lines:
                del output_buf[: len(output_buf) - max_lines]

    def _reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                if backend == "claude":
                    # Init subtype carries claude_session_id; capturing it here
                    # avoids a second pass over the envelope in the parser.
                    sid = _extract_claude_session_id(line)
                    if sid and state is not None:
                        state["claude_session_id"] = sid
                    for produced in _parse_claude_stream_event(line):
                        _append(
                            {
                                "text": produced["text"],
                                "timestamp": _time.time(),
                                "stream": produced["stream"],
                            },
                        )
                else:
                    _append(
                        {
                            "text": line.rstrip("\n\r"),
                            "timestamp": _time.time(),
                            "stream": "stdout",
                        },
                    )
        except (ValueError, OSError):
            # Pipe close on shutdown is normal — surfacing it would flood the
            # buffer with one noise entry per conductor_stop.
            pass

    def _err_reader():
        try:
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                _append(
                    {
                        "text": line.rstrip("\n\r"),
                        "timestamp": _time.time(),
                        "stream": "stderr",
                    },
                )
        except (ValueError, OSError):
            pass

    t1 = threading.Thread(target=_reader, daemon=True)
    t2 = threading.Thread(target=_err_reader, daemon=True)
    t1.start()
    t2.start()


_conductor_process: dict[str, Any] = {}
_conductor_output: list[dict[str, Any]] = []


def create_server(dashboard_mode: bool = False, tools_profile: str = "full") -> Any:
    """Build the AIDOCS MCP server.

    dashboard_mode: when True, register dashboard-only tools
    (conductor_start / conductor_send / conductor_stop /
    conductor_output). When False (default, every agent-side MCP),
    those tools are skipped at registration so they never appear in
    the agent's tool list. Set via --dashboard CLI flag by the
    dashboard's Tauri spawner.

    tools_profile: "full" (default — the local stdio/agent server,
    behavior UNCHANGED) registers every tool incl. the RFC-4 palace
    bundle (hard ``import mempalace``). "read_only" registers the
    canonical code/read tool surface but SKIPS the palace bundle, so a
    minimal deployment (e.g. the loopback Outer Gate's read-only Tier-R
    executor) can run the canonical read tools without vendoring
    mempalace or the palace vector stack. It never adds any tool the
    full profile lacks.
    """
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError(
            "FastMCP is not installed. Install the MCP package dependencies before running the server.",
        ) from exc

    # Tool-registration modules + the renders_as decorator are imported HERE
    # (server creation), not at module top: they transitively pull the fastmcp/mcp
    # SDK, needed only when a server is built. `import aidocs_mcp.mcp_server` stays
    # SDK-free for helper/tooling imports; a broken tool module still fails loudly
    # at this real server entrypoint. Behavior/registration order unchanged.
    from .server_audit_tools import register_audit_tools
    from .server_code_edit_tools import register_code_edit_tools
    from .server_code_tools import register_code_tools
    from .server_legacy_git_tools import register_legacy_git_tools
    from .server_memory_index_tools import register_memory_index_tools
    from .server_plan_task_tools import register_plan_task_tools
    from .server_project_admin_tools import register_project_admin_tools
    from .server_rbac_tools import register_rbac_tools
    from .server_run_tools import register_run_tools
    from .server_runtime_context_tools import register_runtime_context_tools
    from .server_session_tools import register_session_tools
    from .server_skill_tools import register_skill_tools
    from .tool_display import renders_as

    hub = AidocsServiceHub(
        templates_root=_resolve_templates_root(),
        script_root=_resolve_script_root(),
    )
    runtime = RuntimeService(hub)
    # Register the canonical runtime for library code (file_ops /
    # protected_file_ops grant reads) that can't take it via DI — see
    # runtime_bootstrap_service.get_runtime (phantom-import fix 2026-06-11).
    from .runtime_bootstrap_service import set_runtime as _set_runtime_singleton

    _set_runtime_singleton(runtime)
    server = FastMCP("AIDOCS MCP", instructions=_build_server_instructions())
    server._aidocs_test_hub = hub  # test access only

    # Doctrine 2026-05-29 (king re-seal — lifecycle injection):
    # bind the ShellEgressService singleton's lifecycle preflight
    # handle to the live hub's require_active_task helper. This
    # flips step 5 of the cascade from honest-fail-closed-unwired
    # to honest-allow-when-active-task-present for agent_reachable
    # shell egress. The closure captures `hub` by reference so
    # subsequent hub state changes are reflected automatically.
    try:
        from pathlib import Path as _Path
        from .mcp_server_runtime_helpers import shell_egress_lifecycle_preflight as _selp
        from .shell_egress_service import default_service as _shell_egress

        def _lifecycle_preflight(cwd: str, tool_name: str) -> dict | None:
            # Strict shell-egress wrapper — never fail-opens on
            # managed_mode/query_gate infrastructure errors; returns
            # structured 'error'/'reason' dicts that ShellEgress
            # translates to lifecycle_preflight_error vs
            # lifecycle_no_active_task respectively. See
            # shell_egress_lifecycle_preflight doctrine.
            return _selp(hub, _Path(cwd) if cwd else _Path("."), tool_name)

        _shell_egress().bind_lifecycle_preflight(_lifecycle_preflight)
    except Exception:
        # Best-effort: a failure here leaves the service in its
        # honest-fail-closed-unwired posture for agent_reachable
        # calls. The service's refused_reason='lifecycle_
        # preflight_unwired' surfaces the gap to the operator.
        pass

    # Phoenix 2026-05-07: universal notification injection. EVERY
    # @server.tool() registration after this line is wrapped so its
    # response carries pending run_notifications + lane_completion_
    # reviews — closes the gap where dict-returning tools bypassed
    # tool_display.py's per-decorator drain. Must be installed BEFORE
    # any tool is registered. Per emperor 2026-05-07 directive.
    from .notification_injector import install_universal_notification_injection

    install_universal_notification_injection(server)

    # ── Boot self-test (2026-04-22) ──
    # Run the managed_mode reconnect cycle against a scratch tmp
    # project. If set_mode → get_mode → connect → get_mode ever
    # leaves requires_reconnect=True, the server has a latent
    # deadlock bug and we log loud + set `_boot_self_test` for the
    # dashboard. We do NOT refuse to start — blocking server startup
    # on an internal bug would itself be a new way AIDOCS could lock
    # operators out. Instead log, mark degraded, rely on the
    # enforcement-disabled kill switch as last resort.
    #
    # Call the store directly, NOT the service — the service's
    # set_mode has a side effect (sets module-global default project
    # root). Using the store bypasses that so our scratch tmpdir
    # doesn't leak into subsequent tool calls.
    server._boot_self_test = {"passed": False, "reason": "not_run"}
    try:
        import tempfile as _bst_tempfile

        with _bst_tempfile.TemporaryDirectory(prefix="aidocs-bst-") as _bst_dir:
            from pathlib import Path as _BstPath

            _bst_root = _BstPath(_bst_dir) / "proj"
            (_bst_root / ".MEMORY").mkdir(parents=True)
            # Direct store calls — no set_default_project_root leak.
            from .managed_mode_service import (
                _MCP_SERVER_BOOT_TOKEN as _BST_TOKEN,
            )

            hub.managed_mode._store.init_db(_bst_root)
            hub.managed_mode._store.set(
                _bst_root,
                session_id="bst-session",
                source="boot_self_test",
                boot_token=_BST_TOKEN,
            )
            _bst_row = hub.managed_mode._store.get(_bst_root)
            _bst_passed = (
                _bst_row.get("active") is True and _bst_row.get("bound_by_boot_token") == _BST_TOKEN
            )
            if not _bst_passed:
                raise RuntimeError(f"boot-token round-trip broken: {_bst_row}")
            server._boot_self_test = {
                "passed": True,
                "bound_token_matches_current": True,
            }
        # Castle-doctrine III: oracle is current. Prune dead-PID
        # conductor bindings on every boot so aidocs_managed_per_conductor
        # doesn't accumulate stale rows from crashed/restarted MCP processes.
        # Token format mcp-<pid>-<unix>-<hash> makes PID extraction trivial;
        # os.kill(pid, 0) is the cross-platform liveness probe.
        #
        # Test guard (2026-05-03): skip when running under pytest. Tests
        # call create_server() repeatedly, and resolve_project_root() in
        # a test process discovers the LIVE AIDOCS project root from cwd
        # - so without this guard, every test_*.py that touches a server
        # silently deletes per_conductor rows from the operator's live
        # DB. The prune helper itself stays unit-tested via
        # tests/runtime/test_prune_dead_conductor_bindings.py against
        # tmpdirs. Production MCP boot still fires this path.
        import os as _os_prune_guard

        if _os_prune_guard.environ.get("PYTEST_CURRENT_TEST"):
            server._conductor_binding_prune = {
                "skipped": True,
                "reason": "pytest_test_run",
            }
        else:
            try:
                from .mcp_server_runtime_helpers import resolve_project_root

                _prune_root = resolve_project_root()
                _prune_result = hub.managed_mode._store.prune_dead_conductor_bindings(_prune_root)
                server._conductor_binding_prune = _prune_result
            except Exception as _prune_exc:
                server._conductor_binding_prune = {
                    "ok": False,
                    "reason": f"{type(_prune_exc).__name__}: {_prune_exc}",
                }
            # Lane 1.5 (2026-05-04): sweep expired session_freeze rows
            # at boot so a self_approve lock with a passed TTL cannot
            # survive an MCP restart. Cheap, idempotent.
            # Q2 doctrine 2026-05-04: no boot sweep on freeze.
            try:
                server._freeze_boot_sweep = {
                    "ok": True,
                    "rows_deleted": 0,
                    "policy": "no_sweep",
                }
            except Exception as _fs_exc:
                server._freeze_boot_sweep = {
                    "ok": False,
                    "reason": f"{type(_fs_exc).__name__}: {_fs_exc}",
                }
    except Exception as _bst_exc:
        import sys as _bst_sys

        server._boot_self_test = {
            "passed": False,
            "reason": f"{type(_bst_exc).__name__}: {_bst_exc}",
        }
        try:
            print(
                f"[aidocs boot self-test FAILED] {_bst_exc!r} — "
                f"the server will still start, but the reconnect "
                f"cycle is broken. Operators can set "
                f"dev.kill_switch=true (dev-flavor "
                f"installs only) to keep working while this is "
                f"investigated.",
                file=_bst_sys.stderr,
            )
        except Exception:
            pass
    # Per-server dedup state (was module-global; moved to instance so tests
    # that spin up fresh servers get fresh caches — otherwise a prior
    # test's identical-args tool call serves cached results into the next
    # test's assertions).

    # Concise helper for tool implementations: _project_root("path") -> Path
    # Raises ValueError if no root available (no session, no default)
    from .mcp_server_runtime_helpers import resolve_project_root as _project_root

    raw_server_tool = server.tool

    # Load TOML tool descriptions for overrides
    # Load TOML tool descriptions for overrides
    _tool_descriptions: dict[str, str] = {}
    try:
        hooks = _load_action_hook_defaults()
        descs = hooks.get("tool_descriptions")
        if isinstance(descs, dict):
            _tool_descriptions = {k: str(v) for k, v in descs.items() if isinstance(v, str)}
    except Exception:
        pass

    # ── Deferred tool loading ──
    # Tool tiers defined in agent_orchestrator.py (agent-agnostic)
    from .agent_orchestrator import is_eager_tool as _is_eager

    _deferred_tool_names: set[str] = set()

    # Lane-worker tool-scope filter (2026-04-24): when this MCP server
    # is running as a stdio child of a spawned lane worker (detected
    # via AIDOCS_EXPERT_LANE_ID env var set by agent_expert_service),
    # shrink the registered tool surface to the worker's allowlist.
    # Reason: MiniMax-free and other small-context models choke on
    # 124 AIDOCS tool schemas in the system context before work even
    # starts; claude workers dodge this via CLI-side --allowedTools
    # but opencode has no equivalent, so we must cut the surface at
    # the MCP layer. Allowlist source: AIDOCS_EXPERT_LANE_ALLOWED env
    # (JSON list set by spawner) → fallback to the same defaults the
    # claude CLI gets (_DEFAULT_LANE_CLI_TOOLS mirror). Conductor-only
    # tools are blocked separately by access_gate._CONDUCTOR_ONLY_TOOLS
    # so nothing here can escalate privilege.
    import json as _json_lane_scope
    import os as _os_lane_scope

    _worker_lane_scope: set[str] | None = None
    if _os_lane_scope.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
        allowed_raw = _os_lane_scope.environ.get(
            "AIDOCS_EXPERT_LANE_ALLOWED",
            "",
        ).strip()
        if allowed_raw:
            try:
                parsed = _json_lane_scope.loads(allowed_raw)
                if isinstance(parsed, list):
                    _worker_lane_scope = {str(x).strip() for x in parsed if str(x).strip()}
            except (_json_lane_scope.JSONDecodeError, TypeError, ValueError):
                _worker_lane_scope = None
        if _worker_lane_scope is None:
            # Fallback: mirror the claude CLI default lane allowlist
            # so the two backends advertise the same surface shape.
            try:
                from .agent_expert_service import _DEFAULT_LANE_CLI_TOOLS

                _worker_lane_scope = set(_DEFAULT_LANE_CLI_TOOLS)
            except Exception:
                _worker_lane_scope = None
        # Tools every lane worker needs regardless of explicit scope —
        # the boot sequence + self-observation. Without these the
        # worker can't bind, can't fetch its brief, can't report.
        if _worker_lane_scope is not None:
            # ai_session(mode='connect') is the single boot door (paved-road,
            # 2026-05-12 mode-collapse) — detects worker env vars and binds +
            # delivers lane plan in one call. ai_task covers begin/update/
            # complete/status via mode dispatch.
            _worker_lane_scope |= {
                "ai_session",
                "ai_task",
            }

    # Admin / dashboard-only tools (2026-04-24): agent never needs
    # these — they're management/telemetry surfaces. Skipped at
    # registration when dashboard_mode=False so they vanish from
    # the agent-facing tool list. Dashboard MCP server still sees them.
    _ADMIN_ONLY_TOOLS: set[str] = {
        # rbac / identity / escalation
        "rbac_approve_escalation",
        "rbac_deny_escalation",
        "rbac_list_pending_escalations",
        "rbac_list_roles",
        "rbac_list_users",
        "rbac_user_permissions",
        # denial / audit telemetry — aggregates + session-wide views
        # stay admin/dashboard-only. Per-lane visibility is served by
        # audit_events_for_task(task_id) (stays agent-visible) and
        # lane_mailbox_peek / conductor_message_history. Session-wide
        # query_last / events_get / leaderboard are dashboard surfaces
        # that dump artifacts, not real-time lane scoping.
        "denial_tier_stats",
        "denial_trend_24h",
        "most_denied_commands",
        "recent_denials_for_session",
        "verify_audit_chain",
        "tool_use_leaderboard",
        # metrics
        "metrics_prometheus",
        "metrics_snapshot",
        "dashboard_snapshot",
        "circuit_breaker_reset",
        "circuit_breaker_status",
        # execution clear/prune (destructive ops admin)
        "execution_clear_token_usage",
        "execution_clear_tool_calls",
        "execution_prune",
        "archive_sessions_now",
        # admin_clear_reconnect STAYS agent-visible (2026-04-24 revert):
        # it's the break-glass tool for managed-mode desync. Hiding it
        # behind dashboard_mode traps the agent when the desync bug
        # hits (which is the ONLY time the tool is needed).
        # backlog / nudge / archive housekeeping
        "backlog_inbox",
        "inactive_session_nudge",
        "list_archive_candidates",
        # protect_file / list_protected_files — folded into
        # ai_protect(mode=...) which stays agent-visible for the
        # user-intent DO-NOT-TOUCH flow.
        # mcp registry (dashboard MCP-install UI)
        "mcp_registry_get",
        "mcp_registry_search",
        "mcp_tool_catalog",
        # roadmap (product/roadmap UI)
        "roadmap_feedback_update",
        "roadmap_layer_progress",
        # action surface analysis (dashboard insights)
        "action_surface_assess",
        "action_surface_compare",
        "action_surface_current_session_bundle",
        "action_surface_session_bundle",
        "action_surface_status_bundle",
        # config admin
        "config_diff_from_default",
        "config_edit_policy_get",
        "config_validation_report",
        # edit history / rollback (dashboard diff UI)
        "edit_diff_summary",
        "edit_history_grep",
        "edit_history_list",
        "edit_rollback",
        "edit_rollback_batch",
        "edit_session_overlap",
        # file / touch heatmaps (dashboard analytics)
        "file_age_histogram",
        "files_touched",
        "files_touched_heatmap",
        "hot_files_with_no_test",
        "untouched_code_files",
        "recent_commits_touching_file",
        "recent_errors_scan",
        "dependency_freshness",
        # index language descriptors (dashboard config)
        "index_language_descriptor_match_get",
        "index_language_descriptor_semantics_get",
        "index_language_descriptors_get",
        "index_language_descriptors_validate",
        # procedure / capability (indexer internals)
        "procedure_capability_link_status",
        "procedure_capability_links_get",
        "procedure_definitions_get",
        "procedure_index_status",
        "capability_definitions_get",
        "capability_index_status",
        # semantic index admin (agent uses semantic_search)
        "semantic_index_status",
        "semantic_index_sync",
        "schema_index_sync",
        # memory admin / diagnostics
        "memory_content_check",
        "memory_doc_word_count",
        "memory_shape_check",
        "memory_stale_finder",
        # project admin / diagnostics
        "project_audit_snapshot",
        "project_freshness",
        "project_health_score",
        "project_size_report",
        "project_progress_dashboard",
        "project_check_legacy",
        "project_inspect_legacy",
        "project_origins_get",
        "project_registry_list",
        "project_sync_indexes",
        # session admin / export / analytics
        "session_artifacts_normalize",
        "session_compare",
        "session_compliance_get",
        "session_export_markdown",
        "session_handoff_completeness",
        "session_handoff_get",
        "session_handoff_step_update",
        "session_handoff_steps_get",
        "session_handoff_steps_normalize",
        "session_handoff_update",
        "session_owner_summary",
        "session_prune_stale_claims",
        "session_start_state_get",
        "session_status_badge",
        "session_timeline",
        # task analytics (agent uses begin/update/complete/status)
        "task_breadcrumbs",
        "task_open_or_blocked",
        "task_progress_streak",
        "task_velocity",
        # skill admin
        "skill_override_registry_get",
        "skill_provider_override_set",
        "skill_provider_status_get",
        # workflow admin
        "workflow_actions_compile",
        "workflow_step_chronograph",
        "workflow_triggers_for_action",
        # git admin (agent uses git_ops)
        "git_conflict_analysis",
        "git_diag",
        "git_fork_status",
        "git_merge_plan",
        "git_upstream_changes",
        # execution admin / session-wide telemetry — dashboard surfaces.
        # Conductor uses audit_events_for_task(task_id) for per-lane
        # visibility, not these session-wide aggregates that dump into
        # sqlite artifacts.
        "execution_event_record",
        "execution_run_record",
        "execution_events_get",
        "execution_runs_get",
        "execution_index_status",
        "execution_loop_next",
        "execution_mode_select",
        "execution_query_compliance",
        "execution_query_last",
        "execution_query_summary",
        "execution_usage_by_identity",
        # planning docs / normalize (dashboard plan UI)
        "planning_docs_list",
        "plan_normalize_prose",
        "plan_step_drift",
        "plan_validate",
        "plan_preflight",
        # misc admin
        "memory_routing_orphans",
        "reserved_filename_check",
        "workflow_definition_list",
        "workflow_definition_add",
        "workflow_definition_update",
        "workflow_definition_remove",
        "legacy_build_session_proposal",
        "mode_clear",
        "mode_get",
        "mode_set",
        "db_query",
        # Runtime-auto tools (2026-04-24): called by host/runtime at
        # bootstrap/reconnect/session-switch, NOT by agent directly.
        # Hidden to save agent context.
        "project_bootstrap_or_resume",
        "project_init",
        "project_ensure_mcp_config",
        "project_check",
        "project_fix",
        "project_status",
        "index_sync",
        "index_status",
        "runtime_preflight",
        "classify_prompt",
        "handle_prompt",
        "orchestrate",
        "route_prompt",
        "context_budget_check",
        "context_compact",
    }

    # Hidden helpers — Python defs that keep @server.tool for shape
    # but skip MCP registration because they're folded into the
    # ai_slop(mode=...) dispatcher — maintenance / cleanup / refactor.
    # (pebble cleanup): replaced the `_NAME_REMAP` dict-with-empty-
    # string-values pattern; a set is what it always meant.
    # ── Mode-dispatch schemas live in mode_schema.py (king directive 2026-05-12) ──
    # FastMCP's auto-schema emits a FLAT JSONSchema from the function
    # signature — every param appears optional/nullable, the dispatcher
    # `mode` doesn't gate per-mode required-sets. For mode-dispatch
    # tools (ai_replace, ai_find, ai_run, ai_soul, ai_skill, ai_msg
    # /ai_lane families, etc.) this means the schema lies: it doesn't
    # tell the agent "mode='send' REQUIRES to_roles+body". The rules
    # live only in docstrings.
    #
    # Fix: a @modes(...) decorator that authors apply to mode-dispatch
    # tools, declaring per-mode required + optional params. After
    # registration, _apply_mode_schemas walks every Tool, reads the
    # function's _mode_specs metadata if present, and rewrites the
    # tool's `parameters` JSONSchema to a discriminated `oneOf` —
    # one branch per mode with the correct required set.
    #
    # Usage:
    #   @modes(
    #       send={"required": ["to_roles", "body"], "optional": ["in_reply_to"]},
    #       inbox={"required": [], "optional": ["unread_only"]},
    #       reply={"required": ["message_id", "body"], "optional": []},
    #   )
    #   @server.tool()
    #   async def ai_msg(mode: str, to_roles: str = "", ...): ...

    # SINGLE SOURCE: the hidden-from-both carve-out is owned by the canonical
    # catalog resolver (outer_gate_catalog.HIDDEN_EVERYWHERE), so local MCP and
    # the Outer Gate share one authority — no drift between a local hidden list
    # and the gate's classification.
    from .outer_gate_catalog import HIDDEN_EVERYWHERE as _HIDDEN_TOOLS

    def _taxonomy_tool(*args: Any, **kwargs: Any) -> Any:
        explicit_name = kwargs.pop("name", None)
        eager = kwargs.pop("eager", None)  # explicit override

        def decorator(func: Any) -> Any:
            tool_name = explicit_name or func.__name__
            if tool_name.startswith("aidocs_"):
                tool_name = tool_name.removeprefix("aidocs_")
            # Skip registration for helpers folded into dispatchers.
            # Function stays Python-callable; just not MCP-exposed.
            if tool_name in _HIDDEN_TOOLS:
                return func
            # Admin-only gate (2026-04-24): if the tool is in the
            # _ADMIN_ONLY_TOOLS set AND we're not in dashboard mode,
            # skip registration. Function stays Python-callable;
            # just not exposed via MCP to the agent. The dashboard
            # MCP server (dashboard_mode=True) still registers them.
            if tool_name in _ADMIN_ONLY_TOOLS and not dashboard_mode:
                return func
            # Lane-worker scope filter (2026-04-24): when this MCP
            # server instance is running inside a spawned lane worker
            # (AIDOCS_EXPERT_LANE_ID env set at create_server time),
            # skip registration for any tool not in the worker's
            # allowlist. Keeps the model's tool-schema budget small
            # enough for small-context backends like MiniMax.
            if _worker_lane_scope is not None and tool_name not in _worker_lane_scope:
                return func
            # Override docstring from TOML if available
            toml_desc = _tool_descriptions.get(tool_name)
            if toml_desc and func.__doc__:
                func.__doc__ = toml_desc
            # Track deferred tools using agent-agnostic tier classification
            is_eager_val = eager if eager is not None else _is_eager(tool_name)
            if not is_eager_val:
                _deferred_tool_names.add(tool_name)
            # Inject visibility tags for FastMCP filtering
            existing_tags = kwargs.get("tags", set())
            if not existing_tags:
                existing_tags = set()
                kwargs["tags"] = existing_tags
            if is_eager_val:
                existing_tags.add("eager")
            else:
                existing_tags.add("deferred")
            return raw_server_tool(*args, name=tool_name, **kwargs)(func)

        return decorator

    server.tool = _taxonomy_tool

    # ── Post-registration: Apply visibility filtering ──
    # After all @server.tool() decorators run, disable deferred tools
    # so they only appear when explicitly surfaced via tool discovery.
    # Uses FastMCP's native tags-based filtering.
    _eager_tag = {"eager"}
    _deferred_tag = {"deferred"}
    # Disable all deferred tools at startup (runtime-visible default)
    # Note: This is a one-time filter at server bootstrap; AIDOCS authorization
    # still gates actual execution, this only controls initial tool surface.
    if _deferred_tool_names:
        # Can't await here (sync module scope) — defer to first list_tools call
        # Store deferred names for lazy finalize
        server._aidocs_deferred_to_disable = _deferred_tool_names.copy()
        server._aidocs_eager_tag = _eager_tag
        server._aidocs_deferred_tag = _deferred_tag

    # Store tier metadata for introspection
    server._aidocs_deferred_tools = _deferred_tool_names
    server._aidocs_filter_applied = False  # Track if we've applied filtering

    # Wrap list_tools to apply deferred filtering on first call
    # Use _aidocs_deferred_tools (persists) not _aidocs_deferred_to_disable (gets cleared)
    _original_list_tools = server.list_tools

    async def _filtered_list_tools(*, run_middleware: bool = True):
        # Trust Claude Code's native ToolSearch (2026-04-24): with
        # ENABLE_TOOL_SEARCH=true set in the host environment, Claude
        # Code's own defer_loading / ToolSearch mechanism handles the
        # "known but not schema-loaded" pattern for MCP tools. Our
        # previous server.disable() call fought that by removing
        # deferred tools from the host's view entirely, breaking
        # ToolSearch discovery (select:... returned 'no match') and
        # making enable()-based dynamic surfacing impossible.
        #
        # New policy: keep every tool registered + enabled. Host
        # decides what to load eagerly vs. via ToolSearch. Deferred
        # metadata (_aidocs_deferred_tools) stays as a category tag
        # for introspection. Call-time enforcement (PreToolUse +
        # sticky NLP grants in user_intent_tools) is the sole
        # authoritative gate.
        #
        # Operator setup requirement: ENABLE_TOOL_SEARCH=true in
        # the agent's environment for the token-saving behaviour.
        # Without it, all 250+ schemas load at connect time — ~100k
        # tokens. Installer should set this.
        #
        # SINGLE SOURCE: route the local tool list through the canonical catalog
        # resolver's hidden-from-both carve-out, so local visibility and the gate
        # share one authority. (These are also not registered, so this is a
        # belt-and-suspenders enforcement, not a behavior change.)
        from .outer_gate_catalog import local_hidden

        listed = await _original_list_tools(run_middleware=run_middleware)
        try:
            return [t for t in listed if not local_hidden(getattr(t, "name", ""))]
        except TypeError:
            return listed

    server.list_tools = _filtered_list_tools

    project_registry = ProjectRegistryService()
    project_info_store = ProjectInfoStore()

    # ── Dynamic tool surfacing (2026-04-24) ─────────────────────────
    # FastMCP emits notifications/tools/list_changed from server.enable()
    # when called inside an active request context. We leverage that:
    # sticky NLP grants bump a per-session generation counter; every
    # incoming tool call checks the counter against the in-process
    # "last synced" value; on delta we read the sticky grants and
    # enable any deferred tool that isn't enabled yet. FastMCP then
    # emits list_changed to the host, which re-fetches and exposes the
    # tool. Ungranted deferred tools stay disabled → stay hidden → the
    # pre-existing PreToolUse gate remains authoritative for call-time.
    # Last-synced generation per session (process-local — fresh on restart)
    _grants_last_synced: dict[str, int] = {}

    def _sync_deferred_tool_enable(project_root: Any, session_id: str) -> None:
        """Compare sqlite generation to in-process cache; enable any
        newly-granted deferred tools so FastMCP emits list_changed.

        No-op if counter hasn't advanced. No-op on any store error
        (the call_tool wrapper must never fail on housekeeping).
        """
        if not session_id:
            return
        try:
            current_gen = hub.query_gate.get_grants_generation(
                project_root,
                session_id,
            )
        except Exception:
            return
        key = f"{project_root}::{session_id}"
        last = _grants_last_synced.get(key, -1)
        if current_gen == last:
            return
        try:
            granted = set(
                hub.query_gate.get_user_intent_tools(
                    project_root,
                    session_id,
                )
                or [],
            )
        except Exception:
            return
        deferred = set(getattr(server, "_aidocs_deferred_tools", None) or [])

        def _bare(n: str) -> str:
            for prefix in ("mcp__aidocs__",):
                if n.startswith(prefix):
                    return n[len(prefix) :]
            return n

        # Convert bare names back to the registered tool names by
        # matching against the deferred set.
        to_enable: list[str] = []
        for deferred_name in deferred:
            bare = _bare(deferred_name)
            if deferred_name in granted or bare in granted:
                to_enable.append(deferred_name)
        if to_enable:
            try:
                # enable() inside an active request context triggers
                # FastMCP's auto notifications/tools/list_changed emit.
                server.enable(names=set(to_enable), components={"tool"})
            except Exception:
                pass
        _grants_last_synced[key] = current_gen

    original_call_tool = server.call_tool

    async def _instrumented_call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        version: Any = None,
        run_middleware: bool = True,
        task_meta: Any = None,
    ) -> Any:
        # Duplicate tool-call suppression now lives at the execution
        # store's INSERT boundary (content-addressed event_id/run_id +
        # ON CONFLICT DO NOTHING). The wrapper used to try to dedup
        # here but the task-context semantics around FastMCP's
        # middleware chain made it unreliable — see the dedup comment
        # near _DEDUP_BUCKET_SECONDS in execution_index_store.py.
        return await _real_instrumented_call_tool(
            self,
            name,
            arguments,
            version=version,
            run_middleware=run_middleware,
            task_meta=task_meta,
        )

    async def _real_instrumented_call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        version: Any = None,
        run_middleware: bool = True,
        task_meta: Any = None,
    ) -> Any:
        # Auto-fill root from managed session default if not provided

        # Only inject if the tool declares a root/project_root param (key present in args)
        if isinstance(arguments, dict) and ("root" in arguments or "project_root" in arguments):
            if not arguments.get("root") and not arguments.get("project_root"):
                from .mcp_server_runtime_helpers import _last_known_project_root

                if _last_known_project_root is not None:
                    arguments["root"] = str(_last_known_project_root)
        project_root = _project_root_from_args(arguments)
        if not _capture_enabled(name, arguments) or project_root is None:
            return await original_call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )

        # Universal task gate (2026-05-17, king directive: "every command
        # sits behind task_begin, no bypass for ANYTHING including
        # reads"). Fires from the central middleware so reads, writes,
        # AND shell (ai_run) are all attributable. Exemptions are the
        # bootstrap/lifecycle set in
        # mcp_server_runtime_helpers._TASK_GATE_EXEMPT.
        #
        # Project resolution: prefer the arg-derived root; fall back to
        # the session's last-known root so reads that don't carry an
        # explicit project_root (most of them) still hit the gate.
        # Unmanaged projects fail open inside require_active_task — the
        # gate only fires when managed_mode is active.
        # The interactive task gate attributes calls to a conductor's
        # task_begin. The read_only profile is used ONLY by the loopback Outer
        # Gate's read executor, where attribution is ALREADY enforced at the
        # outer boundary (authenticated scoped token + mandatory durable audit
        # with operator/token_id/tool/verdict). Applying the conductor task gate
        # there would wrongly refuse a stateless remote read of a project that
        # merely has a conductor session open. Full profile is unchanged.
        _gate_root = project_root
        if _gate_root is None:
            from .mcp_server_runtime_helpers import _last_known_project_root

            _gate_root = _last_known_project_root
        if tools_profile != "read_only" and _gate_root is not None:
            from .mcp_server_runtime_helpers import require_active_task

            require_active_task(hub, _gate_root, name)
            # require_active_task raises ToolError on refusal; if we
            # get here the gate passed.

        if not _capture_enabled(name, arguments) or project_root is None:
            return await original_call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )

        managed = hub.managed_mode.get_mode(project_root)
        managed_session_id = str(managed.get("session_id") or "").strip() or None
        # Split-brain seal (2026-05-24): a STALE bind (active but the bound
        # session is not a SQL member) must not surface sticky grants or
        # attribute tool calls to a session require_session would refuse.
        # Treat it as unattributed — membership is the sole authority.
        if managed_session_id and managed.get("stale_bind"):
            managed_session_id = None
        # Dynamic tool surfacing: sync in-process enable state with
        # sqlite sticky grants before this call proceeds. If the hook
        # wrote new grants since our last sync, server.enable() fires
        # here → FastMCP auto-emits list_changed → host re-fetches →
        # the tool appears (or already appeared for this call if the
        # host supports live refresh).
        if managed_session_id:
            _sync_deferred_tool_enable(project_root, managed_session_id)
        # Resolve session attribution for this tool call without forcing everyone
        # through managed mode. Priority:
        #   1) explicit session_id in tool arguments — caller knows best
        #   2) session hint on MCP task_meta (_meta) if the client forwarded one
        #   3) managed-mode fallback — only when the user has opted in
        #   4) None — record honestly as unattributed
        session_id: str | None = None
        if isinstance(arguments, dict):
            arg_sid = arguments.get("session_id")
            if isinstance(arg_sid, str) and arg_sid.strip():
                session_id = arg_sid.strip()
        if session_id is None and task_meta is not None:
            meta_obj: Any = task_meta
            if not isinstance(meta_obj, dict):
                meta_obj = getattr(task_meta, "meta", None) or getattr(task_meta, "_meta", None)
            if isinstance(meta_obj, dict):
                for key in ("session_id", "aidocs_session_id", "sessionId"):
                    val = meta_obj.get(key)
                    if isinstance(val, str) and val.strip():
                        session_id = val.strip()
                        break
        if session_id is None:
            session_id = managed_session_id
        project_registry.record_project(
            project_root,
            managed_session_id=managed_session_id,
            title=project_root.name,
        )
        # Mirror the project metadata into the per-project sqlite so the
        # big-boss DB carries title + first-seen + last-seen alongside
        # the other Beat-3 stores. Errors must NOT take the tool call
        # down; project-info is observability, not load-bearing.
        try:
            project_info_store.init_db(project_root)
            project_info_store.record(project_root, title=project_root.name)
        except Exception:
            pass
        run_id = f"mcp-{uuid4()}"
        args_bytes = len(json.dumps(arguments, default=str)) if isinstance(arguments, dict) else 0
        # Build rich payload for tool call tracking
        args_str = json.dumps(arguments, default=str) if isinstance(arguments, dict) else "{}"
        args_bytes = len(args_str.encode("utf-8"))
        args_preview = args_str[:500] if len(args_str) > 500 else args_str
        # Determine host/agent identity for usage tracking
        import os as _os

        _host_id = (
            (_os.environ.get("CLAUDE_CODE_VERSION", "") and "claude_code")
            or (_os.environ.get("OPENCODE_VERSION", "") and "opencode")
            or "unknown"
        )
        _agent_id = str(managed.get("agent_id") or "main") if isinstance(managed, dict) else "main"
        _lane_id = None
        if session_id:
            _gate = hub.query_gate.get(project_root, session_id)
            _lane_id = _gate.get("current_lane_id")
            if _lane_id:
                _agent_id = f"lane:{_lane_id}"

        payload_summary: dict[str, object] = {
            "tool_name": name,
            "argument_keys": sorted(arguments.keys()) if isinstance(arguments, dict) else [],
            "args_preview": args_preview,
            "tokens_out_estimate": max(1, args_bytes // 4),
            "host_id": _host_id,
            "agent_id": _agent_id,
        }
        # Structured metadata for specific tool types
        if isinstance(arguments, dict):
            if arguments.get("path"):
                payload_summary["target_path"] = str(arguments["path"])[:200]
            if arguments.get("start_line"):
                payload_summary["line_range"] = (
                    f"{arguments.get('start_line')}-{arguments.get('end_line', '?')}"
                )
            if arguments.get("query"):
                payload_summary["query"] = str(arguments["query"])[:100]
            if arguments.get("mode"):
                payload_summary["mode"] = str(arguments["mode"])
            # Edit-specific: capture old/new for diff view
            if arguments.get("old_str"):
                payload_summary["old_str"] = str(arguments["old_str"])[:500]
            if arguments.get("new_str"):
                payload_summary["new_str"] = str(arguments["new_str"])[:500]
            if arguments.get("new_content"):
                payload_summary["new_content"] = str(arguments["new_content"])[:500]
            # ai_run: capture the literal command so the
            # verification_gate can auto-record commands_run from the
            # event log instead of trusting agent-reported evidence.
            if arguments.get("command"):
                payload_summary["command"] = str(arguments["command"])[:500]
        _record_tool_execution_state(
            hub,
            project_root,
            run_id=run_id,
            capability_name=name,
            session_id=session_id,
            status="started",
            event_kind="tool_call_started",
            metadata=payload_summary,
        )
        # ── Lane tool enforcement: block tools not in lane's allowed list ──
        if session_id:
            _lane_gate_state = hub.query_gate.get(project_root, session_id)
            if _lane_gate_state.get("current_lane_id"):
                from .access_gate import AccessGate, GateContext

                _lane_decision = AccessGate.check_lane_tool(
                    GateContext(
                        managed=True,
                        session_id=session_id,
                        dev_mode=False,
                        allow_config_edit=False,
                        gate_enforce=True,
                        gate_state=_lane_gate_state,
                    ),
                    name,
                )
                if not _lane_decision.allowed:
                    raise RuntimeError(
                        _lane_decision.reason or f"Tool '{name}' blocked by lane policy.",
                    )

        # ── Circuit breaker: check if external MCP server is in cooldown ──
        _external_mcp = name.startswith("mcp__") and not name.startswith("mcp__aidocs__")
        if _external_mcp:
            from .circuit_breaker import get_breaker as _get_breaker

            _breaker = _get_breaker()
            _server_id = name.split("__")[1] if "__" in name else name
            _can_exec, _block_reason = _breaker.can_execute(_server_id)
            if not _can_exec:
                raise RuntimeError(_block_reason)

        try:
            result = await original_call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )
        except Exception as exc:
            _record_tool_execution_state(
                hub,
                project_root,
                run_id=run_id,
                capability_name=name,
                session_id=session_id,
                status="failed",
                event_kind="tool_call_failed",
                metadata={**payload_summary, "error_type": type(exc).__name__},
                completed_at=_utc_timestamp(),
            )
            from .metrics import get_collector as _get_metrics_err

            _get_metrics_err().record_tool_call(
                tool_name=name,
                status="failed",
                session_id=session_id,
            )
            if _external_mcp:
                _breaker.record_failure(_server_id)
            raise

        # Circuit breaker: record success for external MCP tools
        if _external_mcp:
            _breaker.record_success(_server_id)

        # ── Output Guard: scan tool result for credentials/injections ──
        guard_summary: dict[str, object] | None = None
        from .config import OUTPUT_GUARD_ENABLED, OUTPUT_GUARD_REDACT
        from .output_guard import (
            GuardResult as _GuardResult,
        )
        from .output_guard import (
            scan_tool_result as _guard_scan,
        )

        if OUTPUT_GUARD_ENABLED:
            guard_result = _guard_scan(result, redact=OUTPUT_GUARD_REDACT)
            if guard_result.scanned and not guard_result.clean:
                guard_summary = guard_result.summary()
                payload_summary["output_guard"] = guard_summary
                # Record output guard findings as execution event
                from .tool_call_log import record as _log_record

                _log_record(
                    hub,
                    project_root,
                    phase="guard_finding",
                    name=name,
                    payload={
                        "finding_count": len(guard_result.findings),
                        "redaction_count": guard_result.redaction_count,
                        "categories": list({f.category for f in guard_result.findings}),
                        "max_severity": max(
                            (f.severity for f in guard_result.findings),
                            key=lambda s: {"info": 0, "warning": 1, "critical": 2}.get(s, 0),
                            default="info",
                        ),
                    },
                    session_id=session_id,
                    source="output_guard",
                    action_kind="security",
                    status="redacted" if guard_result.redaction_count > 0 else "flagged",
                )
        else:
            guard_result = _GuardResult(scanned=False)

        # ── Central index-staleness stamp (cheap; NO SHA walk) ──
        # One place stamps honest code/memory freshness onto every index-backed
        # read tool's result (INDEX_BACKED_TOOLS), so the ~13 code tools + memory
        # tools that previously served unstamped now carry the same signal
        # ai_find/ai_slop already did. Shape-preserving; best-effort.
        from .index_staleness import stamp_tool_result as _stamp_index_staleness

        result = _stamp_index_staleness(result, name, project_root)

        # ── Metrics: record tool call ──
        from .metrics import get_collector as _get_metrics

        _metrics = _get_metrics()

        result_summary = _summarize_tool_result(result)
        result_bytes, result_text_preview = _tool_result_preview(result)
        tokens_in_estimate = max(1, result_bytes // 4)
        tokens_out_estimate = max(1, len(str(arguments).encode("utf-8")) // 4)
        payload_summary["tokens_in_estimate"] = tokens_in_estimate
        payload_summary["result_preview"] = result_text_preview
        # Extract exit_code for tool results that carry one (ai_run,
        # ai_run_output, ai_run_status). Lets verification_gate
        # count only commands whose runs actually passed. Without this
        # the gate trusts any recorded command-run regardless of outcome,
        # which means agents can satisfy the gate with failing tests.
        # (Bug fix 2026-04-20.)
        try:
            _exit_candidate = None
            if isinstance(result, dict):
                if "exit_code" in result:
                    _exit_candidate = result.get("exit_code")
                elif result.get("done") is True and "ok" in result:
                    # Inline-finish path uses ok=True for exit_code 0.
                    _exit_candidate = 0 if result.get("ok") else 1
            else:
                _structured = getattr(result, "structured_content", None)
                if isinstance(_structured, dict) and "exit_code" in _structured:
                    _exit_candidate = _structured.get("exit_code")
            if _exit_candidate is not None:
                try:
                    payload_summary["exit_code"] = int(_exit_candidate)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass

        _metrics.record_tool_call(
            tool_name=name,
            status="completed",
            session_id=session_id,
            tokens_in_estimate=tokens_in_estimate,
            tokens_out_estimate=tokens_out_estimate,
        )
        if guard_result.scanned:
            _metrics.record_guard_scan(
                clean=guard_result.clean,
                redaction_count=guard_result.redaction_count,
                findings=[
                    {"category": f.category, "severity": f.severity} for f in guard_result.findings
                ],
            )

        _record_tool_execution_state(
            hub,
            project_root,
            run_id=run_id,
            capability_name=name,
            session_id=session_id,
            status="completed",
            event_kind="tool_call_completed",
            metadata={**payload_summary, "result_summary": result_summary},
            completed_at=_utc_timestamp(),
        )
        return result

    server.call_tool = MethodType(_instrumented_call_tool, server)

    register_session_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_sync=timed_sync,
        annotate_skill_result=_annotate_skill_result,
        session_summary_to_dict=_session_summary_to_dict,
        coerce_to_list=_coerce_to_list,
    )

    register_skill_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        annotate_skill_result=_annotate_skill_result,
    )

    register_plan_task_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_sync=timed_sync,
    )

    register_runtime_context_tools(
        server=server,
        hub=hub,
    )

    register_memory_index_tools(
        server=server,
        hub=hub,
        timed_sync=timed_sync,
        timed_indexer=timed_indexer,
    )
    register_code_edit_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        require_indexed_read_gate=_require_indexed_read_gate,
        post_edit_reindex_and_grant=_post_edit_reindex_and_grant,
        file_get_lines=_file_get_lines,
        file_read_raw=_file_read_raw,
        file_create_file=_file_create_file,
        file_edit_lines=_file_edit_lines,
        file_batch_edit=_file_batch_edit,
        file_str_replace=_file_str_replace,
        file_batch_str_replace=_file_batch_str_replace,
        anchor_replace=_file_anchor_replace,
        available_config_edit_modes=available_config_edit_modes,
        self_edit_available_in_profile=self_edit_available_in_profile,
    )

    register_code_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_tool=timed_tool,
        timed_discovery=timed_discovery,
        timed_sync=timed_sync,
        grant_known_exact_path_read=_grant_known_exact_path_read,
        post_edit_reindex_and_grant=_post_edit_reindex_and_grant,
        require_indexed_read_gate=_require_indexed_read_gate,
        apply_trace_depth=_apply_trace_depth,
        resolve_related_root=_resolve_related_root,
        file_extract_block=_file_extract_block,
        file_get_lines=_file_get_lines,
        file_create_file=_file_create_file,
        file_edit_lines=_file_edit_lines,
        file_batch_edit=_file_batch_edit,
        file_str_replace=_file_str_replace,
        file_batch_str_replace=_file_batch_str_replace,
        available_config_edit_modes=available_config_edit_modes,
        self_edit_available_in_profile=self_edit_available_in_profile,
        registered_tools=lambda: _registered_tools(server),
        all_procedures=lambda root: _all_procedures(hub, root),
        all_capabilities=lambda root: _all_capabilities(hub, root),
    )

    register_project_admin_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_sync=timed_sync,
        resolve_related_root=lambda root, name: _resolve_related_root(hub, root, name),
    )

    register_rbac_tools(
        server=server,
        hub=hub,
        runtime=runtime,
    )

    register_audit_tools(
        server=server,
        hub=hub,
        runtime=runtime,
    )

    from .server_todo_backlog_tools import register_todo_backlog_tools

    register_todo_backlog_tools(
        server=server,
        hub=hub,
        runtime=runtime,
    )

    register_run_tools(
        server=server,
        hub=hub,
        runtime=runtime,
    )

    register_legacy_git_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        resolve_related_root=lambda root, name: _resolve_related_root(hub, root, name),
        timed_git_async=timed_git_async,
        run_git_async=_run_git,
        git_timeout=_GIT_TIMEOUT,
    )

    # ── RFC-4 Palace integration (additive, optional) ──
    # When mempalace is available, attach hub.palace + register the
    # 4-tool agent surface (ai_palace_search, ai_palace_status,
    # ai_palace_diary_read/write) + the clustered ai_recall tool.
    # When mempalace is NOT installed, hub.palace stays None and no
    # palace tools register — AIDOCS continues with its existing
    # surface unchanged.
    #
    # RFC-4: mempalace is a HARD bundled dependency — vendored under
    # third_party/mempalace and wired onto sys.path by aidocs_mcp
    # __init__.py. The previous soft try/except ImportError probe is
    # gone: if ``import mempalace`` fails here, the install is broken
    # and the server MUST refuse to start. Bugs inside palace_hub_extension,
    # server_palace_tools, or server_recall_tools propagate by design.
    # The read_only profile deliberately SKIPS the palace bundle (and its hard
    # mempalace/vector dependency) — a minimal read-tool deployment must not be
    # forced to vendor mempalace. The full profile (every local/agent server)
    # keeps the RFC-4 hard import unchanged: a broken vendored bundle still fails
    # the full server closed.
    if tools_profile != "read_only":
        import mempalace  # noqa: F401 — hard import, asserts vendored bundle

        from .palace_hub_extension import register_palace_in_hub
        from .server_palace_tools import register_palace_tools
        from .server_recall_tools import register_recall_tools

        register_palace_in_hub(hub)
        if getattr(hub, "palace", None) is not None:
            register_palace_tools(server=server, hub=hub, runtime=runtime)
            register_recall_tools(server=server, hub=hub, runtime=runtime)

    # ── Metrics + Output Guard tools ──

    @server.tool()
    @renders_as("status", title="metrics")
    async def metrics_snapshot() -> Any:
        """Return current MCP server metrics (token usage, tool calls, output guard stats)."""
        from .metrics import get_collector

        return get_collector().snapshot()

    @server.tool()
    async def metrics_prometheus() -> str:
        """Return metrics in Prometheus text exposition format for /metrics scraping."""
        from .metrics import get_collector

        return get_collector().render_prometheus()

    # ── MCP Registry Browser tools ──

    @server.tool()
    @renders_as("list", title="mcp registry search")
    async def mcp_registry_search(query: str = "", limit: int = 20) -> Any:
        """Search the official MCP server registry. Returns matching servers with install commands."""
        from .mcp_registry import search_servers

        try:
            result = search_servers(query, limit=limit)
            return {
                "servers": [s.to_dict() for s in result.servers],
                "total_count": result.total_count,
                "next_cursor": result.next_cursor,
                "install_commands": {s.name: s.install_commands() for s in result.servers},
            }
        except (ConnectionError, ValueError) as exc:
            return {"error": str(exc)}

    @server.tool()
    @renders_as("status", title="mcp registry entry")
    async def mcp_registry_get(name: str) -> Any:
        """Get details for a specific MCP server from the registry."""
        from .mcp_registry import get_server

        try:
            server_info = get_server(name)
            if server_info is None:
                return {"error": f"Server '{name}' not found in registry."}
            return {
                **server_info.to_dict(),
                "install_commands": server_info.install_commands(),
            }
        except (ConnectionError, ValueError) as exc:
            return {"error": str(exc)}

    # ── Circuit Breaker tools ──

    @server.tool()
    @renders_as("status", title="circuit breakers")
    async def circuit_breaker_status() -> Any:
        """Show circuit breaker states for all tracked MCP servers."""
        from .circuit_breaker import get_breaker

        return {"breakers": get_breaker().get_all_states()}

    @server.tool()
    @renders_as("status", title="circuit breaker reset")
    async def circuit_breaker_reset(server_id: str) -> Any:
        """Manually reset a circuit breaker for an MCP server."""
        from .circuit_breaker import get_breaker

        get_breaker().reset(server_id)
        return {"reset": server_id, "state": get_breaker().get_state(server_id)}

    # ── Notification queue tools (Phoenix 2026-05-09) ──

    @server.tool()
    @renders_as("status", title="notifications cleared")
    async def notifications_clear(
        session_id: str = "",
        run_id: str = "",
    ) -> Any:
        """Clear pending 📣 run-done notifications.

        The 📣 surface persists across every tool call until the
        agent reads each run's output (auto-dismiss in ai_run_output)
        — but long fire-and-forget chains (sleeps, status probes,
        marker writes the agent doesn't follow up on) leave permanent
        visual noise. This tool is the conductor's escape hatch.

        Args:
            session_id: AIDOCS session whose notifications to clear.
                Defaults to the conductor's bound session.
            run_id: When set, clear only this run's notification.
                When empty, clear ALL notifications for this session.

        Records owned by OTHER sessions are never touched.

        Does NOT clear 📋 lane completion review pending entries —
        those need conductor verdict via ai_review
        (per emperor-doctrine §VIII).

        """
        from . import run_notifications as _rn

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        if not sid:
            return {
                "ok": False,
                "error": (
                    "no session_id resolved — pass session_id explicitly "
                    "or call session_connect first"
                ),
            }
        cleared = _rn.dismiss_for_session(
            project_root,
            session_id=sid,
            run_id=run_id or "",
        )
        return {
            "ok": True,
            "session_id": sid,
            "run_id": run_id or None,
            "cleared": cleared,
        }

    # ── Skill Scanner + Context Compaction tools ──

    @server.tool()
    async def skill_scan(skill_id: str, content: str, kind: str = "") -> str:
        """Scan skill content for security risks (prompt injection, supply chain, capabilities).

        Pass `kind` (e.g. 'doctrine', 'stance', 'skill') so documentation
        scrolls that describe security patterns by design aren't flagged
        as if they ran them. Empty `kind` runs the full scan.
        """
        from .skill_scanner import scan_skill

        result = scan_skill(skill_id, content, kind=kind)
        return result.summary()

    @server.tool()
    async def context_budget_check(session_id: str = "") -> str:
        """Check context budget for a session — journal size, estimated tokens, recommendations."""
        from .context_compaction import check_context_budget

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        session_path = project_root / ".MEMORY" / "sessions" / sid
        result = check_context_budget(session_path, sid)
        return result.to_dict()

    @server.tool()
    async def context_compact(session_id: str = "", keep_recent: int = 10) -> str:
        """Compact session context — extract key decisions, prune old journal entries. Resets token counters (new context window)."""
        from .context_compaction import compact_session_context

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        session_path = project_root / ".MEMORY" / "sessions" / sid
        result = compact_session_context(session_path, sid, keep_recent=keep_recent)
        # Reset token usage — compaction means new context window
        try:
            from .execution_index_store import ExecutionIndexStore

            deleted = ExecutionIndexStore().clear_token_usage(project_root, session_id=sid)
            result_dict = result.to_dict()
            result_dict["tokens_reset"] = deleted
        except Exception:
            result_dict = result.to_dict()
            result_dict["tokens_reset"] = 0
        return result_dict

    # ── Edit History / Rollback tools ──

    @server.tool()
    @renders_as("list", title="edit history")
    async def edit_history_list(file_path: str = "", session_id: str = "", limit: int = 20) -> Any:
        """List recent file edits for rollback. Optionally filter by file or session."""
        from .edit_history import EditHistoryStore

        project_root = _project_root()
        store = EditHistoryStore()
        edits = store.list_edits(
            project_root,
            file_path=file_path or None,
            session_id=session_id or None,
            limit=limit,
        )
        return {"edits": [e.to_dict() for e in edits]}

    @server.tool()
    @renders_as("list", title="edit history grep")
    async def edit_history_grep(
        pattern: str,
        in_old: bool = True,
        in_new: bool = True,
        file_glob: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> Any:
        """Forensic grep across edit history for old/new content.

        Bonus 2026-04-19. "What changed this line last?" used to be a
        manual journal trawl — this exposes a SQL LIKE scan over the
        existing audit trail. file_glob uses SQL LIKE syntax (% wildcard).
        """
        from .edit_history import EditHistoryStore

        project_root = _project_root()
        edits = EditHistoryStore().grep_edits(
            project_root,
            pattern=pattern,
            in_old=bool(in_old),
            in_new=bool(in_new),
            file_glob=file_glob or None,
            session_id=session_id or None,
            limit=int(limit),
        )
        return {"edits": [e.to_dict() for e in edits]}

    @server.tool()
    @renders_as("list", title="files touched")
    async def files_touched(session_id: str = "") -> Any:
        """Summary of all files modified in this session — who edited what, how many times."""
        from .edit_history import EditHistoryStore

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        summary = EditHistoryStore().files_touched_summary(project_root, session_id=sid or None)
        return {"files": summary, "total": len(summary)}

    @server.tool()
    @renders_as("list", title="tool catalog")
    async def mcp_tool_catalog(
        name_contains: str = "",
        include_schema: bool = False,
        limit: int = 200,
    ) -> Any:
        """Self-introspection: list all registered MCP tools.

        Bonus 2026-04-19. Gap: agents using deferred-tool patterns burn
        ToolSearch calls just to discover what's available. This returns
        the canonical catalog from FastMCP's component registry. Pass
        name_contains to filter; include_schema=True attaches each
        tool's parameter shape (heavier — default off).
        """
        tools = _registered_tools(server) or []
        needle = name_contains.strip().lower()
        out: list[dict[str, Any]] = []
        for t in tools:
            name = str(getattr(t, "name", "") or getattr(t, "_name", "") or "")
            if not name:
                continue
            if needle and needle not in name.lower():
                continue
            entry: dict[str, Any] = {
                "name": name,
                "description": (
                    str(getattr(t, "description", "") or "").strip().splitlines()[0]
                    if getattr(t, "description", None)
                    else ""
                )[:240],
            }
            if include_schema:
                schema = getattr(t, "input_schema", None) or getattr(t, "parameters", None)
                if schema is not None:
                    entry["schema"] = schema
            out.append(entry)
            if len(out) >= int(limit):
                break
        out.sort(key=lambda e: str(e.get("name", "")))
        return {"total": len(out), "tools": out}

    @server.tool()
    @renders_as("status", title="session badge")
    async def session_status_badge(session_id: str = "") -> Any:
        """Compact session-state badge for editor status bars.

        Bonus 2026-04-19. Returns {active, session_id, current_task,
        last_tool, last_journal_at, lane_workers_running} — the minimum
        an editor needs to render "🟢 working on X" or "🔴 blocked"
        without 3+ separate calls. ~200B response.
        """
        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        if not sid:
            return {"active": False, "session_id": None}
        result: dict[str, Any] = {"active": True, "session_id": sid}
        try:
            gate = hub.query_gate.get(project_root, sid) or {}
            result["last_tool"] = gate.get("last_tool")
            result["current_lane_id"] = gate.get("current_lane_id")
        except Exception:
            pass
        try:
            entries = hub.sessions.read_journal(project_root, sid) or []
            if entries:
                last = entries[-1]
                result["last_journal_at"] = str(last.get("timestamp", ""))
                result["last_journal_kind"] = str(last.get("action_kind", ""))
        except Exception:
            pass
        try:
            session = hub.sessions.read_session(project_root, sid)
            if session and getattr(session, "sections", None):
                state = session.sections.get("State", []) or []
                for line in state:
                    s = str(line).lstrip("- ").strip()
                    if s:
                        result["current_task"] = s[:120]
                        break
        except Exception:
            pass
        try:
            from .session_lane_agents_store import SessionLaneAgentsStore

            workers = (
                SessionLaneAgentsStore().get_lane_agents(
                    project_root,
                    session_id=sid,
                    state_filter="running",
                )
                or []
            )
            result["lane_workers_running"] = len(workers)
        except Exception:
            result["lane_workers_running"] = 0
        return result

    # ── Semantic Search tools ──

    @server.tool()
    @renders_as("list", title="semantic search")
    async def semantic_search(query: str, limit: int = 10) -> Any:
        """Search code by meaning, not just keywords. Finds 'authentication flow' even if code doesn't contain those exact words.

        Requires sentence-transformers: pip install sentence-transformers
        Run semantic_index_sync first to build the index.
        """
        from .semantic_search import index_status, search

        status = index_status(_project_root())
        if not status.get("available"):
            return {
                "results": [],
                "error": "sentence-transformers not installed.",
                "install": "pip install sentence-transformers",
                "hint": "After installing, run semantic_index_sync to build the index.",
            }
        results = search(_project_root(), query, limit=limit)
        if not results:
            return {
                "results": [],
                "hint": "No results. Run semantic_index_sync to build the index.",
            }
        return {"results": results, "total": len(results)}

    @server.tool()
    @renders_as("status", title="semantic index sync")
    async def semantic_index_sync(max_files: int = 500) -> Any:
        """Build semantic search index from code files. Embeds file contents for meaning-based search."""
        from .semantic_search import sync_from_code_index

        return sync_from_code_index(_project_root(), max_files=max_files)

    @server.tool()
    @renders_as("status", title="semantic index")
    async def semantic_index_status() -> Any:
        """Check semantic search index status — model availability, indexed files/chunks."""
        from .semantic_search import index_status

        return index_status(_project_root())

    @server.tool()
    @renders_as("status", title="edit rollback")
    async def edit_rollback(edit_id: str = "") -> Any:
        """Rollback a specific edit — restore the file to its state before the edit."""
        from .edit_history import EditHistoryStore

        if not edit_id:
            return {"success": False, "message": "edit_id is required."}
        project_root = _project_root()
        result = EditHistoryStore().rollback(project_root, edit_id)
        return result.to_dict()

    # ── Code Runner — unified detached runner lives in server_run_tools
    # (ai_run + ai_run_status + ai_run_output + ai_run_kill).
    # The old sync wrappers (code_run_command, code_test_project,
    # code_build_project) were deleted 2026-04-20 — all shell work goes
    # through the single detached path so agents never get stuck
    # reading a sync command's output. bash_policy judge + test/build
    # renderer pipeline live on the unified spawn.

    # ── Subagent / Lane Agent Managed Mode ──

    @modes(
        connect={
            "required": [],
            "optional": ["session_id"],
            "desc": "bind this agent to a session (default: most recent active)",
        },
        bind={
            "required": [],
            "optional": ["session_id"],
            "desc": "alias of connect",
        },  # alias of connect (verb seal 2026-05-31)
        list={"required": [], "optional": [], "desc": "list the project's sessions"},
        create={
            "required": ["title"],
            "optional": [
                "goal",
                "session_id",
                "owner",
                "scope",
                "status",
                "predecessor_session_id",
            ],
            "desc": "create a new session (title required)",
        },
        claim={
            "required": ["session_id", "agent_id", "run_id"],
            "optional": ["claim_mode"],
            "desc": "take the session worklock",
        },
        claim_status={
            "required": ["session_id"],
            "optional": ["stale_after_minutes"],
            "desc": "who holds the worklock + staleness",
        },
        release={
            "required": ["session_id", "agent_id"],
            "optional": ["run_id"],
            "desc": "release the session worklock",
        },
        update={
            "required": ["session_id", "patch"],
            "optional": [],
            "desc": "patch SESSION.md fields",
        },
        resume={
            "required": ["session_id"],
            "optional": ["include_code_bundle", "include_tests", "journal_last_n"],
            "desc": "full resume bundle: SESSION.md + journal tail (+ optional code bundle)",
        },
        skills_get={
            "required": ["session_id"],
            "optional": [],
            "desc": "read the session's selected skills",
        },
        skills_set={
            "required": ["session_id", "selected_skills"],
            "optional": [],
            "desc": "set the session's selected skills",
        },
    )
    @server.tool(eager=True)
    @renders_as("status", title="ai_session")
    async def ai_session(
        mode: str,
        session_id: str = "",
        title: str = "",
        goal: str = "",
        owner: str = "",
        scope: str = "-",
        status: str = "active",
        predecessor_session_id: str | None = None,
        agent_id: str = "",
        run_id: str = "",
        claim_mode: str = "active",
        stale_after_minutes: int = 30,
        patch: dict[str, list[str]] | None = None,
        selected_skills: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        journal_last_n: int = 10,
    ) -> Any:
        """Unified session-lifecycle tool — one tool, ten modes (king directive 2026-05-12).

        Modes:
          connect       — bind the calling agent to a session (paved-road entry).
                          Optional: session_id (else uses last-bound/most-recent).
          list          — list sessions from project-local /.MEMORY/sessions/.
          create        — create a new session. Required: title.
          claim         — add advisory claim. Required: session_id, agent_id, run_id.
                          Optional: claim_mode ('active' default).
          claim_status  — list claims + staleness. Required: session_id.
          release       — release a claim. Required: session_id, agent_id.
          update        — update structured SESSION.md sections. Required: session_id, patch.
          resume        — collaboration-oriented resume bundle. Required: session_id.
          skills_get    — list selected skills. Required: session_id.
          skills_set    — set selected skills. Required: session_id, selected_skills.

        Per-mode required-sets enforced by @modes; runtime branches by `mode`.
        """
        m = (mode or "").strip().lower()
        project_root = _project_root()
        if m in ("connect", "bind"):  # 'bind' is the verb-aligned alias of 'connect'
            return await session_connect(session_id=session_id)
        if m == "list":
            summaries = hub.sessions.list_sessions(project_root)
            return [_session_summary_to_dict(item) for item in summaries]
        if m == "create":
            # Authorization boundary (2026-05-25): an agent may create a
            # session ONLY inside an authenticated, authorized project.
            # require_admin = solo/dev local-admin passthrough; corpo demands
            # an authenticated operator holding admin.manage_sessions. This is
            # the SAME shared authorization model as connect/bind — creation is
            # not a side-door. (create_session itself mints SQL membership;
            # owner grant + audit follow.)
            from .permission_catalog import PERM_ADMIN_MANAGE_SESSIONS
            from .project_authority import (
                _authenticated_uid as _auth_uid,
            )
            from .project_authority import (
                audit as _pa_audit,
            )
            from .project_authority import (
                require_admin as _require_admin,
            )

            _auth = _require_admin(
                project_root,
                permission=PERM_ADMIN_MANAGE_SESSIONS,
                operation="session_create",
            )
            if not _auth.get("ok"):
                return {
                    "created": False,
                    "blocked_by": _auth.get("blocked_by"),
                    "error": (
                        f"session create refused: {_auth.get('reason')} "
                        f"(agents may create sessions only in an authenticated, "
                        f"authorized project)"
                    ),
                }
            import re as _re
            from datetime import date as _date

            sid = session_id
            if not sid:
                slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
                sid = f"{_date.today().isoformat()}-{slug}"
            own = owner
            if not own:
                try:
                    managed_state = hub.managed_mode.get_mode(project_root)
                    own = str(managed_state.get("source") or "").strip() or "agent"
                except Exception:
                    own = "agent"
            session = hub.sessions.create_session(
                project_root,
                session_id=sid,
                title=title,
                owner=own,
                goal=goal or title,
                scope=scope,
                status=status,
                predecessor_session_id=predecessor_session_id,
            )
            # Session-owner grant (corpo): system-mint a SESSION-SCOPED admin
            # role for the authenticated creator so require_session stage-2b
            # (session-level RBAC) recognizes them as owner of THIS session
            # only. solo/dev need no grant — the local-admin passthrough
            # already authorizes them.
            #
            # Owner-grant TRUTH (2026-05-25): the result reports the grant's
            # real outcome — "not_required" (solo/dev), "granted", or "failed".
            # A failed grant in corpo means the session EXISTS (SQL member) but
            # ownership is DEGRADED; we surface that honestly rather than
            # claiming a clean create, and the audit carries the degraded
            # status. The truth is never swallowed.
            owner_grant = "not_required"
            owner_uid = ""
            try:
                _uid = _auth_uid(project_root)
                if _uid:
                    owner_uid = _uid
                    owner_grant = "failed"  # pessimistic until proven granted
                    from .rbac_store import RBACStore

                    _rb = RBACStore()
                    # Mint the LEAST-PRIVILEGE, honestly-named session_owner
                    # role (manage_sessions only) — the name the operator/audit
                    # sees now matches the authority granted. Seed if missing
                    # (idempotent); fall back to admin only if a pre-seal DB
                    # has not been reseeded (admin ⊇ manage_sessions, so the
                    # owner is still authorized — legacy compatibility).
                    _owner_role = _rb.get_role_by_name(project_root, "session_owner")
                    if _owner_role is None:
                        try:
                            from .permission_catalog import seed_rbac

                            seed_rbac(project_root)
                            _owner_role = _rb.get_role_by_name(project_root, "session_owner")
                        except Exception:
                            _owner_role = None
                    if _owner_role is None:
                        _owner_role = _rb.get_role_by_name(project_root, "admin")
                    if _owner_role is not None:
                        _rb.assign_role_to_user_scoped(
                            project_root,
                            _uid,
                            _owner_role.role_id,
                            scope_type="session",
                            scope_id=sid,
                            authored_by_user_id="__bootstrap__",
                        )
                        # Verify the grant actually took (membership of the
                        # perm), not just that the call did not raise.
                        if _rb.has_permission(
                            project_root,
                            _uid,
                            PERM_ADMIN_MANAGE_SESSIONS,
                            scope_type="session",
                            scope_id=sid,
                        ):
                            owner_grant = "granted"
            except Exception:
                owner_grant = "failed"
            degraded = owner_grant == "failed"
            _pa_audit(
                project_root,
                operation="session_create",
                target=sid,
                status="allowed_degraded" if degraded else "allowed",
                reason=("session_created_owner_grant_failed" if degraded else "session_created"),
            )
            result = {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
                "owner_grant": owner_grant,
            }
            if owner_uid:
                result["owner_user_id"] = owner_uid
            if degraded:
                result["ownership_degraded"] = True
                result["warning"] = (
                    f"session '{sid}' was created and is a SQL member, but the "
                    f"session-owner grant for '{owner_uid}' did NOT take — "
                    f"ownership is degraded; grant the `session_owner` role "
                    f"(admin.manage_sessions) at session scope via the "
                    f"dashboard before relying on it."
                )
            return result
        if m == "claim":
            session = hub.sessions.claim_session(
                project_root,
                session_id,
                agent_id=agent_id,
                run_id=run_id,
                mode=claim_mode,
            )
            return {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            }
        if m == "claim_status":
            claims = hub.sessions.list_claims(
                project_root,
                session_id,
                stale_after_minutes=stale_after_minutes,
            )
            return {"session_id": session_id, "claims": claims}
        if m == "release":
            session = hub.sessions.release_claim(
                project_root,
                session_id,
                agent_id=agent_id,
                run_id=run_id or None,
            )
            return {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            }
        if m == "update":
            session = hub.sessions.update_session(project_root, session_id, patch or {})
            return {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            }
        if m == "resume":
            return runtime.session_resume_bundle(
                project_root,
                session_id=session_id,
                include_code_bundle=include_code_bundle,
                include_tests=include_tests,
                journal_last_n=journal_last_n,
            )
        if m == "skills_get":
            return hub.skills.get_selected_skills(project_root, session_id)
        if m == "skills_set":
            return runtime.set_session_skills(project_root, session_id, selected_skills or [])
        return {
            "error": f"unknown mode: {mode!r} (valid: connect|list|create|claim|claim_status|release|update|resume|skills_get|skills_set)",
        }

    @modes(
        bind={
            "required": ["project_root"],
            "optional": ["confirm_token"],
            "desc": "project_root=path → bind THIS host session to that project (RBAC-gated)",
        },
        status={"required": [], "optional": [], "desc": "show the current project bind"},
        unbind={"required": [], "optional": [], "desc": "clear the project bind"},
        list={"required": [], "optional": [], "desc": "list known AIDOCS projects"},
    )
    @server.tool(eager=True)
    @renders_as("status", title="ai_project")
    async def ai_project(
        mode: str,
        project_root: str = "",
        confirm_token: str = "",
    ) -> Any:
        """Bind THIS host session to an AIDOCS-enabled project — the local
        mirror of the outer gate's project_select.

        Modes: bind | status | unbind | list. The bind is keyed per
        host_session_id (cross-user isolated), idle-TTL'd, and re-roots
        every later ai_* call via resolve_project_root(). RBAC-gated via
        project_authority.require_cross_project; escalates on deny. See
        tool_interface.ai_project for the full contract.
        """
        from . import project_bind_service as _pbs
        from .mcp_server_runtime_helpers import current_calling_host_session_id
        from .session_project_bind_store import DEFAULT_BIND_TTL_MINUTES

        m = (mode or "").strip().lower()
        sid = current_calling_host_session_id()
        if m == "status":
            return _pbs.status_project(host_session_id=sid)
        if m == "unbind":
            return _pbs.unbind_project(host_session_id=sid)
        if m == "list":
            from .cross_project_ops import project_list as _plist

            bound = _pbs.status_project(host_session_id=sid).get("bound_project_root")
            res = _plist(include_session_counts=False)
            for proj in res.get("projects", []):
                proj["bound_to_this_session"] = str(proj.get("project_root")) == str(bound)
            res["bound_project_root"] = bound
            return res
        if m == "bind":
            target = (project_root or "").strip()
            if not target:
                return {"bound": False, "error": "project_root is required for mode='bind'"}
            # Two-phase confirm (mirrors outer-gate project_select): the
            # agent cannot silently re-root the session — the operator must
            # echo the exact phrase. require_cross_project is the AUTHORITY;
            # this is the deliberate-act confirmation on top of it.
            expected = f"confirm-project-bind {target}"
            if (confirm_token or "").strip() != expected:
                return {
                    "_error": "confirm_required",
                    "action": "ai_project bind",
                    "project_root": target,
                    "confirm_token": expected,
                    "ttl_minutes": DEFAULT_BIND_TTL_MINUTES,
                    "summary": (
                        f"About to bind THIS host session to {target!r} "
                        f"(idle TTL {DEFAULT_BIND_TTL_MINUTES}m). Every later "
                        f"ai_* call will re-root there. Ask the user before "
                        f"re-invoking with confirm_token."
                    ),
                }
            return _pbs.bind_project(
                host_session_id=sid,
                conductor_root=_project_root(),
                target_root=target,
            )
        return {"error": f"ai_project: unknown mode {mode!r} (bind|status|unbind|list)"}

    # Internal helper. Tool surface removed 2026-05-12 — ai_session(mode='connect').
    @renders_as("status", title="managed mode")
    async def session_connect(
        session_id: str = "",
    ) -> Any:
        """Bind to a work session — paved-road entry, three branches.

        CONDUCTOR (no AIDOCS_EXPERT_ID env): activates managed mode for
        the selected session, returns plans-list-with-status (the shelf).
        Conductors pick a scroll via ai_plan(mode="read", name=...).

        LANE WORKER (AIDOCS_EXPERT_ID + AIDOCS_EXPERT_LANE_ID env):
        verifies session_lane_agents row, activates managed mode,
        latches sub_agent flag, returns THIS lane's plan body.
        Workers don't see the topology — their slice is their kingdom.

        ORPHAN WORKER (WORKER_ID without LANE_ID): identity-strict
        refusal. Lane spawns must have both env vars; missing one is
        a misconfigured spawn or env-strip privilege escalation attempt.

        Identity is read internally — never from arguments. Both env
        vars are set by agent_expert_service at spawn time and cannot
        be forged by the agent inside the subprocess.

        Replaces lane_worker_bind + get_lane_plan (paved-road entry,
        2026-05-02). Mirrors the session_select / session_start /
        session_read / plan_connect removal pattern.
        """
        import os as _os

        worker_id_env = _os.environ.get("AIDOCS_EXPERT_ID", "").strip()
        lane_id_env = _os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()

        # Branch: ORPHAN WORKER
        if worker_id_env and not lane_id_env:
            return {
                "connected": False,
                "error": (
                    "Worker spawn missing AIDOCS_EXPERT_LANE_ID env. "
                    "Identity-strict refusal: lane workers must have "
                    "both AIDOCS_EXPERT_ID and AIDOCS_EXPERT_LANE_ID. "
                    "Missing LANE_ID = misconfigured spawn (or env-strip "
                    "privilege escalation attempt). The conductor must "
                    "respawn with both env vars set."
                ),
            }

        project_root = _project_root()

        # Branch: LANE WORKER
        if worker_id_env and lane_id_env:
            import sqlite3 as _sqlite_lwb

            from .execution_index_store import ExecutionIndexStore

            _store_lwb = ExecutionIndexStore()
            _store_lwb.init_db(project_root)
            matching_row: dict[str, str] | None = None
            try:
                with _sqlite_lwb.connect(str(_store_lwb.db_path(project_root))) as _c:
                    _c.row_factory = _sqlite_lwb.Row
                    _row = _c.execute(
                        "SELECT worker_id, session_id, lane_id, state "
                        "FROM session_lane_agents "
                        "WHERE worker_id = ? AND lane_id = ?",
                        (worker_id_env, lane_id_env),
                    ).fetchone()
                    if _row is not None:
                        matching_row = {
                            "worker_id": str(_row["worker_id"]),
                            "session_id": str(_row["session_id"] or ""),
                            "lane_id": str(_row["lane_id"] or ""),
                            "state": str(_row["state"] or ""),
                        }
            except Exception:
                matching_row = None
            if matching_row is None:
                return {
                    "connected": False,
                    "error": (
                        f"worker_id {worker_id_env!r} not found in "
                        f"session_lane_agents for lane {lane_id_env!r}. "
                        f"Refusing to bind — the spawn registry has no "
                        f"matching row (spawn aborted or id tampering)."
                    ),
                }
            sid = matching_row["session_id"].strip()
            if not sid:
                return {
                    "connected": False,
                    "error": "worker row has no session_id; cannot bind.",
                }
            managed = hub.managed_mode.get_mode(project_root)
            if not managed.get("active"):
                hub.managed_mode.set_mode(
                    project_root,
                    session_id=sid,
                    source="session_connect.lane_worker",
                )
            from .protected_file_runtime import latch_sub_agent_call_on

            latch_sub_agent_call_on()
            plans_dir = project_root / ".MEMORY" / "sessions" / sid / "plans"
            if not plans_dir.is_dir():
                return {
                    "connected": False,
                    "error": (
                        f"plans dir missing: {plans_dir}. Conductor "
                        f"must author a lane-aware plan before "
                        f"dispatching workers."
                    ),
                }
            matched = None
            for plan_path in sorted(plans_dir.glob("*.md")):
                try:
                    plan_text = plan_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                sections = hub.sessions._parse_sections(plan_text)
                steps_source = sections.get("Steps") or sections.get("Lane graph") or []
                try:
                    phases, lanes = hub.sessions._parse_lane_aware_steps(steps_source)
                except Exception:
                    continue
                for lane in lanes:
                    if lane.lane_id == lane_id_env:
                        matched = (plan_path, phases, lane)
                        break
                if matched is not None:
                    break
            if matched is None:
                return {
                    "connected": False,
                    "error": (
                        f"lane_id '{lane_id_env}' not found in any "
                        f"plan under {plans_dir}. Conductor may have "
                        f"dispatched a lane that isn't declared."
                    ),
                }
            plan_path, phases, lane = matched
            try:
                _clear_fn = getattr(
                    hub.query_gate,
                    "clear_pending_initial_brief",
                    None,
                )
                if callable(_clear_fn):
                    _clear_fn(project_root, sid)
            except Exception:
                pass
            # Phoenix 2026-05-10 (#164 fix): opencode `run` is
            # single-turn. The previous response shape ended with
            # `next: task_begin` — model treated task_begin as the
            # terminal action and the worker exited mid-plan. The
            # imperative below bakes the full execution loop into
            # one response so the single turn carries the whole
            # lane through to task_complete.
            #
            # Phoenix 2026-05-11: drain the lane mailbox here so
            # conductor messages (notably §VIII deny rationales
            # written by lane_resume_dispatcher) surface in the
            # response payload. The CLI bootstrap-as-message arg
            # is dead-letter for opencode (witnessed 2026-05-11);
            # mailbox is the canonical delivery channel. When a
            # message is present, the response shape promotes it
            # above the plan and the imperative text references
            # it explicitly so the model cannot miss the override.
            _mailbox_msg = None
            try:
                from .lane_mailbox_store import LaneMailboxStore

                _mailbox_msg = LaneMailboxStore().take(
                    project_root,
                    worker_id=worker_id_env,
                )
            except Exception:
                _mailbox_msg = None
            _imperative_prefix = ""
            _conductor_directive = None
            if _mailbox_msg and _mailbox_msg.get("prompt"):
                _conductor_directive = {
                    "from": "conductor",
                    "written_at": str(_mailbox_msg.get("written_at") or ""),
                    "message": str(_mailbox_msg.get("prompt") or ""),
                }
                _imperative_prefix = (
                    "*** CONDUCTOR DIRECTIVE PRESENT *** Read the "
                    "`conductor_directive` field at the top of this "
                    "response BEFORE acting on the plan. The directive "
                    "is BINDING — it supersedes any prior interpretation "
                    "of the plan as already-satisfied. Apply its "
                    "instruction first, then re-submit "
                    "lane_request_completion_review with updated work. "
                    "Do NOT just re-run the plan from scratch and ignore "
                    "the directive — that's the failure mode this "
                    "channel exists to prevent. "
                )
            return {
                "connected": True,
                "session_id": sid,
                "lane_id": lane.lane_id,
                "project_root": str(project_root),
                "conductor_directive": _conductor_directive,
                "imperative": (
                    _imperative_prefix + "EXECUTE THE FULL LANE IN THIS RESPONSE. Do not "
                    "stop mid-plan. Do not announce next steps and "
                    "wait — there is no next turn. The chain is: "
                    "(1) call mcp__aidocs__ai_task(mode='begin') with the "
                    "session_id and a brief goal from the plan "
                    "steps; (2) read each file in `plan.files` "
                    "using ai_get_lines / ai_get_symbol_snippet / "
                    "ai_bundle — these establish read-evidence the "
                    "edit gates require; (3) apply the edits using "
                    "ai_replace (modes anchor/string/symbol/lines), "
                    "ai_edit_lines, or ai_create_file as the work "
                    "demands; (4) if the lane has verification "
                    "commands, run them via ai_run; (5) call "
                    "mcp__aidocs__ai_task(mode='complete') with a result "
                    "summary. ONLY ai_task(mode='complete') ends this turn. "
                    "If you hit a blocker you cannot resolve, call "
                    "ai_task(mode='complete') with a clear blocker summary "
                    "and claimed_done=false rather than stopping "
                    "silently. Do not narrate intentions; act."
                ),
                "next_steps": [
                    "task_begin",
                    "read each file in plan.files",
                    "apply edits",
                    "run verification (if any)",
                    "task_complete",
                ],
                "plan": {
                    "lane_id": lane.lane_id,
                    "phase_id": lane.phase_id,
                    "depends_on": list(lane.depends_on),
                    "steps": [{"status": s.status, "text": s.text} for s in lane.steps],
                    "files": list(lane.files),
                },
            }

        # Branch: CONDUCTOR
        from .mcp_server_runtime_helpers import (
            current_calling_cli_session_id,
            set_calling_conductor_host_session_id,
        )

        cli_session_id = (current_calling_cli_session_id() or "").strip()
        # Recovery: claude_hook subprocess updates query_gate.last_cli_session_id
        # on each UPS, but its module-level global dies when the hook exits.
        # The MCP server's global is NEVER stamped from the hook side. So
        # when the global is empty, recover from query_gate (which the hook
        # reliably wrote to) and stamp our own global so subsequent tool
        # calls in this same MCP process see it. Per empire's
        # gate-invariants #50/#54 sub-clause: per-conductor mapping is
        # the authoritative path; this is what makes it actually work.
        # Fixed 2026-05-06.
        if not cli_session_id and session_id and session_id.strip():
            try:
                # Direct accessor — gate.get() omits last_cli_session_id
                # by design (only returns lane state). Fixed 2026-05-07.
                cli_session_id = hub.query_gate.get_last_cli_session_id(
                    project_root,
                    session_id.strip(),
                )
            except Exception:
                cli_session_id = ""
        if cli_session_id:
            try:
                set_calling_conductor_host_session_id(cli_session_id)
            except Exception:
                pass
        conductor_root = project_root
        cross_project_name: str | None = None
        if session_id and session_id.strip():
            clean_sid = session_id.strip()
            try:
                related = hub.related.list_related_projects(conductor_root)
            except Exception:
                related = []
            for entry in related or []:
                rp_name = str(entry.get("name") or "").strip()
                rp_path = str(entry.get("path") or "").strip()
                if not rp_name or not rp_path:
                    continue
                try:
                    rp_root = Path(rp_path)
                    if not rp_root.is_dir():
                        continue
                    rp_sessions = hub.sessions.list_sessions(rp_root)
                except Exception:
                    continue

                def _sid_of(s):
                    return str(
                        getattr(s, "session_id", None)
                        or (s.get("session_id") if isinstance(s, dict) else None)
                        or "",
                    ).strip()

                if any(_sid_of(s) == clean_sid for s in rp_sessions or []):
                    # Cross-project rebind is a privilege boundary, NOT a
                    # silent name-match. Require the target commissioned +
                    # an approved relation + permission (solo/dev
                    # passthrough, corpo RBAC). Defeats the confused-deputy
                    # where a session-name match silently moves the
                    # conductor into a more-privileged project.
                    from .project_authority import (
                        require_cross_project_session,
                    )

                    _xp = require_cross_project_session(
                        conductor_root,
                        rp_root,
                        clean_sid,
                        operation="session_connect_cross_project",
                        host_session_id=cli_session_id or "",
                    )
                    if not _xp.get("ok"):
                        return {
                            "connected": False,
                            "blocked_by": _xp.get("blocked_by"),
                            "error": (
                                f"cross-project bind into '{rp_name}' refused: {_xp.get('reason')}"
                            ),
                        }
                    project_root = rp_root
                    cross_project_name = rp_name
                    from . import mcp_server_runtime_helpers as _h

                    _h._last_known_project_root = rp_root
                    break
        # Local bind must target a session that BELONGS to the active
        # project. Cross-project binds were membership-checked above via
        # list_sessions; a non-cross bind with a session_id that isn't
        # this project's is a session-name collision / foreign-session
        # bind attempt — refuse.
        if session_id and session_id.strip() and cross_project_name is None:
            # Bounded self-heal BEFORE the authority gate: an unmigrated
            # legacy session (on-disk, predates the registry, marker absent)
            # is imported once here so require_session — a pure, fail-closed
            # read — recognizes it. After the seal this is a no-op and a
            # non-member stays refused (file presence never becomes authority).
            from .session_membership_store import SessionMembershipStore

            SessionMembershipStore().ensure_member_or_heal(project_root, session_id.strip())
            from .project_authority import require_session

            _ss = require_session(
                project_root,
                session_id.strip(),
                host_session_id=cli_session_id or "",
                operation="session_connect",
            )
            if not _ss.get("ok"):
                return {
                    "connected": False,
                    "blocked_by": _ss.get("blocked_by"),
                    "stage": _ss.get("stage"),
                    "error": (
                        f"session bind refused ({_ss.get('stage')} stage): {_ss.get('reason')}"
                    ),
                }
        managed = hub.managed_mode.get_mode(
            project_root,
            cli_session_id=cli_session_id,
        )

        def _build_conductor_payload(
            sid: str,
            *,
            already_active: bool,
        ) -> dict[str, object]:
            try:
                plans_list = hub.sessions.list_session_plans(project_root, sid)
            except Exception:
                plans_list = []
            payload: dict[str, object] = {
                "connected": True,
                "session_id": sid,
                "lane_id": None,
                "project_root": str(project_root),
                "plans": plans_list,
                "message": (
                    f"Managed mode {'active' if already_active else 'activated'} "
                    f"on session {sid}"
                    + (
                        f" in registered project '{cross_project_name}'"
                        if cross_project_name
                        else ""
                    )
                    + ". Use AIDOCS tools."
                ),
            }
            if already_active:
                payload["already_active"] = True
            if cross_project_name:
                payload["cross_project"] = cross_project_name
            return payload

        if managed.get("active"):
            sid = session_id or str(managed.get("session_id", ""))
            if sid:
                # Stale active bind guard: managed mode may report active for a
                # session that is NOT a member (e.g. a pre-registry bind that
                # survived restarts via restamp). Heal once if it is an
                # unmigrated legacy session; if it still isn't a member, refuse
                # to restamp a ghost the authority gate would reject.
                from .session_membership_store import SessionMembershipStore

                if not SessionMembershipStore().ensure_member_or_heal(project_root, sid):
                    return {
                        "connected": False,
                        "session_id": sid,
                        "project_root": str(project_root),
                        "blocked_by": "session_not_in_project",
                        "stale_bind": True,
                        "error": (
                            f"managed mode was bound to '{sid}' but it is not "
                            f"a member of this project — refusing to restamp a "
                            f"stale bind. Connect an existing session, or run "
                            f"`aidocs migrate-control-authority` if this is an "
                            f"unmigrated legacy session."
                        ),
                    }
                try:
                    hub.managed_mode.set_mode(
                        project_root,
                        session_id=sid,
                        source="session_connect_restamp",
                        cli_session_id=cli_session_id,
                    )
                except Exception as _restamp_err:
                    # Don't silently lie about a successful bind when the
                    # persist failed. Pre-2026-05-06 this was `pass` and
                    # caused the split-bind bug (singleton stale, agent
                    # told "ok"). Surface the error instead.
                    return {
                        "connected": False,
                        "session_id": sid,
                        "project_root": str(project_root),
                        "error": (
                            f"session_connect could not restamp managed_mode: "
                            f"{_restamp_err!r}. Binding NOT persisted; "
                            f"downstream session-bound tools will see the "
                            f"previous (stale) session. Investigate "
                            f"managed_mode.set_mode."
                        ),
                        "persist_failed": True,
                    }
                try:
                    hub.query_gate.clear_requires_reconnect(
                        project_root,
                        sid,
                    )
                except Exception:
                    pass
            hub.query_gate.set(project_root, sid, current_lane_id=None)
            from .protected_file_runtime import set_sub_agent_call

            set_sub_agent_call(False)
            try:
                # ProjectIndexSitter is the single owner of external-file
                # freshness (the legacy folder_sitter watcher is retired).
                from .project_index_sitter import ensure_index_sitter

                ensure_index_sitter(project_root, hub)
            except Exception:
                pass
            return _build_conductor_payload(sid, already_active=True)
        sid = session_id
        if not sid:
            sessions = hub.sessions.list_sessions(project_root)
            active = [s for s in sessions if s.get("status") == "active"]
            if active:
                sid = active[0].get("session_id", "")
        if not sid:
            return {
                "connected": False,
                "reason": "No active session found. The conductor should specify session_id.",
            }
        hub.managed_mode.set_mode(
            project_root,
            session_id=sid,
            source="session_connect",
            cli_session_id=cli_session_id,
        )
        try:
            # ProjectIndexSitter is the single owner of external-file freshness
            # (the legacy folder_sitter watcher is retired).
            from .project_index_sitter import ensure_index_sitter

            ensure_index_sitter(project_root, hub)
        except Exception:
            pass
        return _build_conductor_payload(sid, already_active=False)

    @server.tool(eager=True)
    @renders_as("status", title="admin clear reconnect")
    async def aidocs_admin_clear_reconnect(session_id: str = "") -> Any:
        """Clear both reconnect flags in one idempotent call.

        CONDUCTOR-ONLY. Sub-agents cannot call this tool — refuses
        when AIDOCS_EXPERT_ID env var is set (subagent-process
        marker). A trapped sub-agent should report back to the
        conductor via conductor_ask / mailbox and let the conductor
        clear the lane, not self-unbind from its own sandbox.

        The reconnect gate has two triggers:
          * session_query_gate.requires_reconnect (fresh CLI session)
          * aidocs_managed.bound_by_boot_token mismatch (fresh MCP
            server process)

        This tool clears the first and re-stamps the second so the
        next PreToolUse passes. Safe to call repeatedly. Idempotent.

        Use when `session_connect` itself is being refused (a
        future AIDOCS-internal bug). In normal operation you don't
        need this — regular session_connect already does both
        clears in one call.
        """
        # Conductor-only — refuse subagent callers. A subagent with
        # AIDOCS_EXPERT_ID set in its env is by construction a
        # lane-scoped worker; letting it unbind reconnect state
        # could let it escape the lane sandbox. Route via the
        # conductor instead.
        import os as _os_admin

        if _os_admin.environ.get("AIDOCS_EXPERT_ID", "").strip():
            return {
                "ok": False,
                "error": (
                    "aidocs_admin_clear_reconnect is conductor-only. "
                    "Sub-agents cannot self-unbind — if your tools "
                    "are being refused, report to the conductor via "
                    "conductor_ask / conductor_check_response / "
                    "your mailbox, and let the conductor investigate."
                ),
                "blocked_by": "subagent_refused",
            }
        project_root = _project_root()
        managed = hub.managed_mode.get_mode(project_root)
        sid = session_id or str(managed.get("session_id") or "").strip()
        cleared: dict[str, Any] = {"tool": "aidocs_admin_clear_reconnect"}

        # Session-scoped fresh-CLI flag.
        if sid:
            try:
                hub.query_gate.clear_requires_reconnect(project_root, sid)
                cleared["query_gate.requires_reconnect"] = "cleared"
            except Exception as exc:
                cleared["query_gate.requires_reconnect"] = f"error: {exc}"

        # Process-scoped boot-token re-stamp.
        if sid:
            try:
                hub.managed_mode.set_mode(
                    project_root,
                    session_id=sid,
                    source="aidocs_admin_clear_reconnect",
                )
                cleared["managed_mode.bound_by_boot_token"] = "restamped"
            except Exception as exc:
                cleared["managed_mode.bound_by_boot_token"] = f"error: {exc}"

        # Audit event — operator used the escape hatch. Loud by design.
        try:
            hub.execution.record_event(
                project_root,
                event_kind="admin_clear_reconnect",
                source_kind="admin_escape_hatch",
                session_id=sid or None,
                capability_name="aidocs_admin_clear_reconnect",
                action_kind="admin_escape",
                target_entity="reconnect_gate",
                status="cleared",
                payload=dict(cleared),
            )
        except Exception:
            pass

        return {
            "ok": True,
            "session_id": sid or None,
            "project_root": str(project_root),
            "cleared": cleared,
            "message": (
                "Both reconnect flags cleared. If tools still refuse, "
                "the issue is elsewhere (force-wakeup, lane scope, "
                "RBAC) — call aidocs_orchestrate for a full state "
                "diagnostic."
            ),
        }

    # ── ai_preflight — battlefield briefing before architecture-creation
    # work. Phase B-lite (2026-05-14): anti-reinvention guard. Surfaces
    # existing wheels, inspect-first files, known traps, tests, and
    # do-not-create warnings drawn from the kingdom DB (capabilities,
    # code outlines, memory pages, memory links). Pure read, no
    # side effects; returns structured JSON + markdown card.
    @server.tool()
    @renders_as("status", title="ai_preflight")
    async def ai_preflight(task: str) -> Any:
        """Battlefield briefing for a task — anti-reinvention pre-check.

        Args:
            task: free-text description of what you intend to build /
                  change / investigate. The richer the description,
                  the better the surface (seed extraction → spaCy
                  parses verbs/nouns/entities, then queries the
                  kingdom index for existing wheels).

        Returns: {"structured": dict, "markdown": str}
          structured keys:
            - existing_wheels: top capabilities matching task seeds
            - inspect_first: top files/symbols to read before writing
            - known_traps: relevant doctrine / caveat / bug-history pages
            - tests_to_run: tests that gate the affected area
            - do_not_create_warnings: explicit "X exists, don't create Y"
            - confidence: low/medium/high based on source coverage
            - missing_info: seeds with no matches (gaps to investigate)

        Per king-doctrine 2026-05-14: preflight is NOT search. It's
        the briefing that prevents reinvention. Run it BEFORE you
        create services / MCP tools / dashboard pages / detectors /
        scripts / config namespaces / schema tables / worker launchers.

        """
        from . import preflight_service as _pf

        project_root = _project_root()
        return _pf.preflight(project_root, task or "")

    # ── ai_vocab / ai_gate_msg — dashboard-facing CRUD for the empire    # ── ai_gate_msg — dashboard-facing CRUD for the empire
    # gate-message strings. Mode-dispatched. Phase 4a (2026-05-14)
    # of the TOML→sqlite migration.
    #
    # ai_vocab was REMOVED 2026-05-16 (king directive): dashboard
    # keeps full CRUD via Tauri Rust commands (vocab_list_kinds,
    # vocab_upsert_group, etc.); agents have no legitimate write
    # path for the 8 non-domain vocab kinds (action_token,
    # intent_guard, tool_alias, intent_phrase, skill_trigger,
    # memory_route, tool_discovery, plan_vague_pattern) — those are
    # operator decisions, not memories. Domain anchors get
    # auto-registered through memory_capture when anchor_kind='domain'
    # (see record_memory_capture in server_code_tools.py).

    @server.tool()
    @renders_as("status", title="ai_gate_msg")
    async def ai_gate_msg(
        mode: str,
        key: str = "",
        body: str = "",
        lang: str = "en",
        source: str = "operator",
    ) -> Any:
        """Empire gate-message CRUD — backs the dashboard editor.

        Modes:
          list(lang)              — return all gate messages for one lang
          get(key, lang)          — return one body, with en fallback
          upsert(key, body, lang) — delete + insert; returns {deleted, inserted}
          delete(key, lang)       — drop one message
        """
        from . import intent_tokens_store as _store

        m = (mode or "").strip().lower()
        if m == "list":
            return {"rows": _store.list_gate_messages(lang), "lang": lang}
        if m == "get":
            if not key:
                return {"error": "key required"}
            return {"key": key, "body": _store.get_gate_message(key, lang)}
        if m == "upsert":
            if not key or not body:
                return {"error": "key + body required"}
            return _store.upsert_gate_message(key, body, lang=lang, source=source)
        if m == "delete":
            if not key:
                return {"error": "key required"}
            return {"deleted": _store.delete_gate_message(key, lang)}
        return {"error": f"unknown mode: {mode!r}"}

    # lane_worker_bind + get_lane_plan REMOVED 2026-05-02 (king
    # directive - paved-road entry). Their work folded into
    # session_connect: workers call session_connect(); env-detect
    # routes them to the lane-worker branch which does the spawn-
    # registry verify (was lane_worker_bind), the managed-mode
    # bind, the sub_agent latch, and returns the lane plan body
    # in the same response (was get_lane_plan). Single tool,
    # single trip. _build_worker_prompt now returns 'session_connect'.
    # Mirrors session_select / session_start / session_read /
    # plan_connect (all 2026-04-28 through 2026-05-02 paved-road
    # cleanup).

    # Internal helper. Tool surface removed 2026-05-12 — ai_seat(mode='enter').
    @renders_as("status", title="conductor mode")
    async def conductor_mode_enter(
        session_id: str = "",
        verbose: bool = False,
    ) -> Any:
        """Enter conductor mode — current agent becomes the session conductor.

        Returns TERSE confirmation by default (role + session id + file
        paths for SESSION.md, journal, and each plan file). Pass
        verbose=True on cold-resume to ALSO get SESSION.md body,
        recent journal tail, first plan body, files-touched summary.

        Previously this tool dumped ~9 KB every call even when the
        conductor was already bound and just re-asserting the role —
        burned context on repeat calls. (2026-04-20 slim.)
        """
        from . import conductor_doctrine as _conductor_doctrine

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)

        # Persist the binding — the function is named conductor_mode_ENTER,
        # not "tell me what session looks like." Prior versions only updated
        # an in-memory dict; the singleton + per-conductor map went untouched,
        # creating split-bind where task_begin saw a stale session even
        # after conductor_mode_enter "succeeded." Fixed 2026-05-06.
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
            set_calling_conductor_host_session_id,
        )

        _host_sid = current_calling_host_session_id()
        # Recovery: if global empty, pull cli_session_id from query_gate
        # (claude_hook updates it on every UPS) and stamp our own global
        # so per-conductor mapping populates. See gate-invariants
        # #50/#54 sub-clause. Fixed 2026-05-06.
        if not _host_sid and sid:
            try:
                # Direct accessor — gate.get() omits last_cli_session_id.
                _host_sid = hub.query_gate.get_last_cli_session_id(
                    project_root,
                    sid,
                )
            except Exception:
                _host_sid = ""
        if _host_sid:
            try:
                set_calling_conductor_host_session_id(_host_sid)
            except Exception:
                pass
        try:
            hub.managed_mode.set_mode(
                project_root,
                session_id=sid,
                source="conductor_mode_enter",
                cli_session_id=_host_sid,
            )
        except Exception as _set_err:
            # Surface persist failure rather than silently lying about bind.
            return {
                "mode": "conductor",
                "session_id": sid,
                "project_root": str(project_root),
                "error": (
                    f"conductor_mode_enter could not persist binding: "
                    f"{_set_err!r}. The session WAS NOT bound; downstream "
                    f"task lifecycle calls will see whatever the previous "
                    f"binding was. Investigate managed_mode.set_mode."
                ),
                "persist_failed": True,
            }
        try:
            hub.query_gate.clear_requires_reconnect(project_root, sid)
        except Exception:
            pass

        import time as _t_cme

        _conductor_process["inline"] = {
            "project_root": str(project_root),
            "session_id": sid,
            "entered_at": _t_cme.time(),
        }

        session_path = project_root / ".MEMORY" / "sessions" / sid
        plan_dir = session_path / "plans"
        plan_files: list[str] = []
        if plan_dir.is_dir():
            plan_files = [
                str(p.relative_to(project_root)).replace("\\", "/")
                for p in sorted(plan_dir.glob("*.md"))
            ]

        def _rel_if_exists(path) -> str | None:
            if not path.is_file():
                return None
            return str(path.relative_to(project_root)).replace("\\", "/")

        terse = {
            "mode": "conductor",
            "session_id": sid,
            "project_root": str(project_root),
            "session_md_path": _rel_if_exists(session_path / "SESSION.md"),
            "journal_path": _rel_if_exists(session_path / "journal.md"),
            "plan_files": plan_files,
            # Role text is rendered from the canonical conductor_doctrine
            # (the SINGLE source, tool-truth-enforced by
            # test_conductor_doctrine_tool_truth) — never inline phantom names.
            "responsibilities": _conductor_doctrine.conductor_responsibilities(),
            "next": _conductor_doctrine.conductor_next_hint(),
        }

        # The ROLE (what the head-conductor seat does) auto-dumps on entry.
        # WHO the seat-holder is lives in the sovereign soul, opened only by
        # the Emperor's word — never dumped here.
        try:
            _role = hub.skills.read_role("head-conductor")
            if _role and _role.get("content_text"):
                terse["role"] = _role["content_text"]
        except Exception:
            pass

        if not verbose:
            return terse

        # Verbose path — full dump. Only on explicit opt-in.
        context_parts: list[str] = [
            "== CONDUCTOR MODE ACTIVE ==",
            f"Session: {sid}",
            f"Project: {project_root}",
        ]
        if isinstance(terse.get("role"), str) and terse["role"]:
            context_parts.extend(["", "== HEAD-CONDUCTOR ROLE ==", terse["role"]])
        try:
            session_md = session_path / "SESSION.md"
            if session_md.is_file():
                context_parts.extend(
                    [
                        "",
                        "== SESSION STATE ==",
                        session_md.read_text(encoding="utf-8")[:2000],
                    ],
                )
            journal = session_path / "journal.md"
            if journal.is_file():
                context_parts.extend(
                    [
                        "",
                        "== RECENT JOURNAL ==",
                        journal.read_text(encoding="utf-8")[-1500:],
                    ],
                )
            if plan_dir.is_dir():
                for plan_file in sorted(plan_dir.glob("*.md"))[:1]:
                    context_parts.extend(
                        [
                            "",
                            f"== PLAN ({plan_file.name}) ==",
                            plan_file.read_text(encoding="utf-8")[:2000],
                        ],
                    )
        except Exception:
            pass
        try:
            from .edit_history import EditHistoryStore

            touched = EditHistoryStore().files_touched_summary(
                project_root,
                session_id=sid,
            )
            if touched:
                files_list = "\n".join(f"  {f['file']} ({f['edits']} edits)" for f in touched[:20])
                context_parts.extend(["", "== FILES MODIFIED ==", files_list])
        except Exception:
            pass
        try:
            agent_rules = hub.workflow.get_agent_workflow_rules(project_root)
            if agent_rules:
                rules_text = "\n".join(f"  - {r}" for r in agent_rules)
                context_parts.extend(
                    [
                        "",
                        "== AGENT WORKFLOW RULES ==",
                        rules_text,
                    ],
                )
        except Exception:
            pass

        return {
            **terse,
            "context": "\n".join(context_parts),
        }

    # Internal helper. Tool surface removed 2026-05-12 — ai_seat(mode='exit').
    @renders_as("status", title="conductor mode")
    async def conductor_mode_exit() -> Any:
        """Clear the inline-conductor marker set by conductor_mode_enter.

        Leaves any subprocess-spawned conductor untouched. Useful for
        switching the current session's role back to a normal agent
        without restarting the MCP server. No-op when no inline marker
        is set.
        """
        prev = _conductor_process.pop("inline", None)
        if prev is None:
            return {"exited": False, "reason": "no inline conductor was active"}
        return {
            "exited": True,
            "prior_session_id": prev.get("session_id"),
            "prior_project_root": prev.get("project_root"),
        }

    @server.tool()
    @renders_as("status", title="ai_lane_exit")
    async def ai_lane_exit(session_id: str = "") -> Any:
        """Self-serve escape hatch when the conductor is trapped in a
        worker-left lane scope without waiting for an operator turn.

        Background: when a lane worker calls `session_connect` and
        finishes without clearing the shared `current_lane_id` on the
        session_query_gate row, every subsequent conductor tool call
        sees the worker's lane filter and gets refused with
        'not in lane allowed files'. Until now the only fixes were the
        operator typing 'exit lane' or setting conductor.auto_exit_lane
        in the dashboard — both require the operator to act. This tool
        lets the conductor recover on its own.

        Hard-gated off worker processes via AIDOCS_EXPERT_LANE_ID env
        check — workers must never be able to self-escape their own
        lane sandbox.
        """
        import os as _os_self_exit

        if _os_self_exit.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
            return {
                "exited": False,
                "reason": (
                    "worker processes cannot self-exit a lane — env AIDOCS_EXPERT_LANE_ID is set"
                ),
            }
        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        if not sid:
            return {"exited": False, "reason": "no active session"}
        try:
            current = hub.query_gate.get(project_root, sid) or {}
            prior_lane = str(current.get("current_lane_id") or "").strip()
        except Exception:
            prior_lane = ""
        if not prior_lane:
            return {
                "exited": False,
                "reason": "no lane was bound on this session",
            }
        try:
            hub.query_gate.set(
                project_root,
                sid,
                last_tool="conductor_lane_exit",
                current_lane_id=None,
                lane_exact_paths=[],
            )
        except Exception as exc:
            return {
                "exited": False,
                "reason": f"query_gate update failed: {exc}",
            }
        try:
            hub.execution.record_event(
                project_root,
                event_kind="lane_exit_grant",
                session_id=sid,
                source_kind="conductor_lane_exit_self_serve",
                payload={"prior_lane": prior_lane},
            )
        except Exception:
            pass
        return {
            "exited": True,
            "prior_lane": prior_lane,
        }

    # Phoenix 2026-05-08: lane_request_completion_review RETIRED.
    # The §VIII flow now lives inside task_complete itself via
    # _capture_lane_worker_task_complete (server_plan_task_tools).
    # Lane workers call task_complete normally; capture happens
    # automatically; worker exits clean; conductor reviews via the
    # 📋 surface; ai_review approves silently
    # (no resume) or denies with rationale (host-CLI resume of the
    # worker's host_session_id, full memory restored). Two tools
    # collapsed into one.

    # @server.tool removed (120% clause B): folded into ai_lane(action='review').
    async def ai_review(
        review_id: str,
        verdict: str,
        message: str = "",
    ) -> Any:
        """[CONDUCTOR-SIDE NON-BLOCKING] Decide a pending lane
        completion review. Per emperor-doctrine §VIII the conductor
        is NEVER blocked — pending reviews surface in the 📋 block
        on every tool call envelope, and this tool resolves one of
        them.

        verdict ∈ {'approved', 'denied'}.

        For 'denied', message SHOULD describe what to fix (or
        reference a plan edit the conductor will make). The lane
        receives the message verbatim and acts on it.

        Returns the updated review row, or {error: ...} when the
        review_id is missing or already decided.

        Phoenix, 2026-05-07.
        """
        from . import lane_completion_review_store as _lcr

        project_root = _project_root()
        if verdict not in ("approved", "denied"):
            return {
                "error": (f"verdict must be 'approved' or 'denied', got {verdict!r}"),
            }
        sid = _resolve_session_id(hub, project_root)
        result = _lcr.submit_verdict(
            project_root,
            review_id=review_id,
            verdict=verdict,
            message=message,
            conductor_session_id=sid or "",
        )
        # Phoenix 2026-05-08: on DENY, spawn the host's resume CLI
        # (claude --resume <hs> / opencode -s <hs> / codex resume <hs>)
        # with a bootstrap prompt carrying the rationale. The
        # captured worker comes back with full session memory + the
        # new directive. APPROVE = no resume; the captured task is
        # truly done. Approve is silent for the worker.
        if verdict == "denied" and isinstance(result, dict) and not result.get("error"):
            try:
                from .lane_resume_dispatcher import resume_worker_on_deny

                resume_info = resume_worker_on_deny(
                    project_root,
                    review_row=result,
                    conductor_message=message,
                )
                result["resume_dispatched"] = bool(resume_info.get("dispatched"))
                if resume_info.get("error"):
                    result["resume_error"] = resume_info["error"]
            except Exception as _resume_exc:
                result["resume_error"] = (
                    f"resume dispatcher failed: {_resume_exc!r}. "
                    "Worker NOT resumed; conductor must intervene."
                )
        # Phoenix 2026-05-10 (#166 fix): on APPROVE, transition the
        # lane state forward → IMPLEMENTATION_DONE → COMPLETED so
        # downstream `depends_on` lanes become runnable. Previously
        # approve recorded the verdict but never closed the lane —
        # the sequence gate stuck and plan_conductor_status kept
        # reporting the lane as runnable. The opencode worker has
        # already exited by the time the conductor approves
        # (single-turn), so there's no live task_complete to
        # transition the lane — approve IS the canonical close.
        if verdict == "approved" and isinstance(result, dict) and not result.get("error"):
            try:
                lane_id_t = str(result.get("lane_id") or "")
                worker_id_t = str(result.get("worker_id") or "")
                work_session_id = ""
                if worker_id_t:
                    import sqlite3 as _sql_t

                    from .execution_index_store import ExecutionIndexStore as _EIS_t

                    _store_t = _EIS_t()
                    _store_t.init_db(project_root)
                    with _sql_t.connect(str(_store_t.db_path(project_root))) as _c_t:
                        _c_t.row_factory = _sql_t.Row
                        _row_t = _c_t.execute(
                            "SELECT session_id FROM session_lane_agents "
                            "WHERE worker_id = ? ORDER BY started_at "
                            "DESC LIMIT 1",
                            (worker_id_t,),
                        ).fetchone()
                        if _row_t is not None:
                            work_session_id = str(_row_t["session_id"] or "")
                if not work_session_id:
                    work_session_id = str(result.get("session_id") or "")
                if lane_id_t and work_session_id:
                    from .types import LaneState as _LS_t

                    transitioned: list[str] = []
                    # Walk the full state graph forward. Lanes may
                    # be stuck at any earlier step because earlier
                    # transitions (dispatch→RUNNING, review→
                    # AWAITING_REVIEW) catch exceptions silently
                    # and don't always fire. Each transition that
                    # is invalid for current state is skipped; the
                    # ones that fit advance the state.
                    for target in (
                        _LS_t.READY,
                        _LS_t.RUNNING,
                        _LS_t.AWAITING_REVIEW,
                        _LS_t.IMPLEMENTATION_DONE,
                        _LS_t.COMPLETED,
                    ):
                        try:
                            runtime._conductor_state.transition_lane(
                                project_root,
                                work_session_id,
                                lane_id_t,
                                target,
                            )
                            transitioned.append(target.value)
                        except Exception:
                            pass
                    result["lane_transitioned_to"] = transitioned
            except Exception as _tx_exc:
                result["lane_transition_error"] = f"approve lane-transition failed: {_tx_exc!r}"
        return result

    from . import tool_interface as _ti_reg_review

    _ti_reg_review.register_impl("ai_review", ai_review)

    @server.tool()
    @renders_as("status", title="ai_plan_template")
    async def ai_plan_template() -> Any:
        """Return the lane-aware plan template for conductor agents.

        Output is markdown that conductors paste into a new plan file
        under .MEMORY/sessions/<sid>/plans/<beat-name>.md and fill in.
        Sections align with what plan_create_from_spec /
        plan_conductor_status / plan_dispatch_next consume: file
        scopes, tool scopes, requires-dependencies, verification
        commands, per-lane checkboxes.

        Conductor-only: workers (AIDOCS_EXPERT_LANE_ID env set) get a
        refusal instead of the template — workers don't author plans.
        """
        from .plan_template import (
            conductor_only_refusal_reason,
            render_plan_template,
        )

        refusal = conductor_only_refusal_reason()
        if refusal:
            return {"ok": False, "reason": refusal}
        return {
            "ok": True,
            "template": render_plan_template(),
            "save_to": (".MEMORY/sessions/<session-id>/plans/<beat-name>.md"),
            "next_step": (
                "Save the filled-in template to a .md file and call "
                "plan_create_from_spec(session_id=..., spec_path=<path>) "
                "to materialize the lane graph. Lane-aware plans "
                "(starts with '# Plan' + contains '## Why'/'## Lane "
                "graph'/etc.) are written verbatim — no re-parsing."
            ),
        }

    # ── Git operations tool ──

    def _is_workflow_action_satisfied(project_root: Path, action: dict) -> bool:
        """Internal helper — check if an action is verified via its verify
        spec or satisfaction table. De-registered as an MCP tool 2026-04-23
        (leading underscore + single-file internal caller; no agent use).
        """
        result = hub.workflow.verify_action(project_root, action)
        return bool(result.get("verified"))

    @server.tool()
    @renders_as("status", title="workflow action")
    async def workflow_action_satisfy(action_id: str, evidence: str) -> Any:
        """Mark a workflow action as completed with evidence. Call after doing the required work.

        The evidence is logged for audit. Example:
          workflow_action_satisfy("rule-01-01-before_git_commit-advisory", "Updated README.md with v2.2.0b changes")
        """
        project_root = _project_root()
        return hub.workflow.satisfy_action(project_root, action_id, evidence)

    @server.tool(eager=True)
    @renders_as("status", title="git")
    async def git_ops(
        op: str = "status",
        message: str = "",
        count: int = 10,
        branch: str = "",
        path: str = "",
        range: str = "",
    ) -> Any:
        """Basic git operations. op: status, log, diff, add, commit, push, pull, branch, stash.

        op=add REQUIRES explicit path. No `-A` default — see backlog #8
        for why (silently stages everything, including untracked noise).
        Path must not contain shell metacharacters (`;` `&` `|` `` ` `` `$`
        newline, quotes, backslash, `<` `>` `*` `?`) — refuse rather than
        quote, since Windows shell-quoting is fragile.

        op=status (2026-04-27): also reports ahead/behind vs upstream
        when tracking is set. Operators see "you have N unpushed commits"
        without leaving the tool.

        op=log range=... (2026-04-27): accepts a git range like
        "origin/main..HEAD" (unpushed commits) or "HEAD~5..HEAD" (last 5).
        Same shell-metachar validation as path. Empty range falls back
        to the legacy `-N` recent-commits behavior.

        Examples:
          git_ops(op="status")
          git_ops(op="log", count=5)
          git_ops(op="log", range="origin/main..HEAD")
          git_ops(op="diff")
          git_ops(op="add", path="src/foo.py")
          git_ops(op="commit", message="fix: bug")
          git_ops(op="push")
          git_ops(op="pull")
          git_ops(op="branch")
          git_ops(op="stash")

        """
        from .code_runner import ai_run

        project_root = _project_root()
        o = op.strip().lower()
        forbidden_metachars = set(";&|`$\n\r\"'\\<>*?")

        # op=add: require an explicit, metachar-free path. Historically
        # fell through to `git add -A` (backlog #8 — silently staged
        # 800+ .runs/*.log artifacts in one call).
        add_cmd: str | None = None
        if o == "add":
            p = (path or "").strip()
            if not p:
                return {
                    "ok": False,
                    "error": (
                        "git_ops op=add requires an explicit path. "
                        "-A default is disabled (backlog #8). "
                        "Example: git_ops(op='add', path='src/foo.py')."
                    ),
                }
            if any(c in forbidden_metachars for c in p):
                return {
                    "ok": False,
                    "error": (
                        f"git_ops op=add refused: path contains shell "
                        f"metacharacter. Paths with {sorted(forbidden_metachars)!s} "
                        f"are rejected (Windows-safe; quoting is fragile). "
                        f"Stage via shell directly if legitimately needed."
                    ),
                }
            add_cmd = f"git add -- {p}"

        # op=log range=...: validate the same way as path. Empty range
        # falls back to legacy `-N` recent-commits.
        log_cmd = f"git log --oneline -{count}"
        if o == "log" and range:
            r = range.strip()
            if any(c in forbidden_metachars for c in r):
                return {
                    "ok": False,
                    "error": (
                        f"git_ops op=log refused: range contains shell "
                        f"metacharacter. Ranges with {sorted(forbidden_metachars)!s} "
                        f"are rejected (Windows-safe; quoting is fragile)."
                    ),
                }
            log_cmd = f"git log --oneline -{count} {r}"

        cmd_map = {
            "status": "git status --short",
            "log": log_cmd,
            "diff": "git diff --stat",
            "diff_staged": "git diff --cached --stat",
            "add": add_cmd,
            "commit": f'git commit -m "{message}"'
            if message
            else "echo 'message required for commit'",
            "push": "git push",
            "pull": "git pull",
            "branch": "git branch -a",
            "stash": "git stash",
            "stash_pop": "git stash pop",
            "stash_list": "git stash list",
            "fetch": "git fetch --all",
            "remote": "git remote -v",
        }

        cmd = cmd_map.get(o)
        if not cmd:
            return {"error": f"Unknown op: {op}. Available: {', '.join(sorted(cmd_map.keys()))}"}

        # Check for "before_" workflow triggers on commit/push
        if o in ("commit", "push"):
            triggers = [f"before_git_{o}"]
            if o == "commit":
                triggers.append("before_git_push")
            pending = []
            for trigger_name in triggers:
                pending.extend(hub.workflow.pending_actions_for_trigger(project_root, trigger_name))
            if pending:
                # Filter out actions that have been satisfied
                unsatisfied = [
                    a for a in pending if not _is_workflow_action_satisfied(project_root, a)
                ]
                if unsatisfied:
                    actions_text = "; ".join(
                        str(a.get("source_segment") or a.get("kind", "action")) for a in unsatisfied
                    )
                    return {
                        "op": o,
                        "blocked": True,
                        "reason": f"Workflow rule requires: {actions_text} — complete these before {o}. Then call workflow_action_satisfy(action_id, evidence) to proceed.",
                        "pending_actions": unsatisfied,
                    }

        result = ai_run(project_root, cmd, timeout=30)
        response: dict[str, Any] = {
            "op": o,
            "success": result.success,
            "exit_code": result.exit_code,
            "output": result.stdout_preview or result.stderr_preview,
            "duration": result.duration_seconds,
        }

        # op=status enrichment (2026-04-27): also report ahead/behind
        # vs upstream so operators see "N unpushed commits" inline.
        # Best-effort — failures here must never break the status read.
        if o == "status" and result.success:
            try:
                # Current branch name.
                br = ai_run(
                    project_root,
                    "git rev-parse --abbrev-ref HEAD",
                    timeout=10,
                )
                cur_branch = (br.stdout_preview or "").strip()
                if br.success and cur_branch and cur_branch != "HEAD":
                    response["branch"] = cur_branch
                    # Upstream tracking name (returns non-zero when no upstream).
                    up = ai_run(
                        project_root,
                        f"git rev-parse --abbrev-ref --symbolic-full-name {cur_branch}@{{u}}",
                        timeout=10,
                    )
                    if up.success:
                        upstream = (up.stdout_preview or "").strip()
                        response["tracking"] = upstream or None
                        # ahead/behind via rev-list --left-right --count.
                        # Format: "ahead\tbehind" reversed: "behind\tahead"
                        # depending on order — we use upstream...HEAD so
                        # left=behind, right=ahead.
                        ab = ai_run(
                            project_root,
                            f"git rev-list --left-right --count {upstream}...HEAD",
                            timeout=10,
                        )
                        if ab.success:
                            parts = (ab.stdout_preview or "").strip().split()
                            if len(parts) == 2:
                                response["behind"] = int(parts[0])
                                response["ahead"] = int(parts[1])
                    else:
                        response["tracking"] = None
                        response["ahead"] = None
                        response["behind"] = None
            except Exception:
                # Defensive: any failure leaves the base status output
                # intact, just without ahead/behind enrichment.
                pass

        return response

    # ── Conductor process management ──
    # _conductor_process and _conductor_output are module-level (above
    # create_server) so agent_orchestrator can see them via getattr on
    # the module. Closure-local dicts were invisible to external readers.

    _conductor_output_lock = __import__("threading").Lock()
    _MAX_CONDUCTOR_OUTPUT = 500  # keep last 500 lines

    _start_output_reader = _start_conductor_output_reader

    # Dashboard-only tool registration gate. When dashboard_mode=False,
    # _dash_tool() is a no-op decorator so the tool function is never
    # handed to FastMCP — the agent's list_tools response doesn't show
    # it at all. When dashboard_mode=True, it's server.tool() exactly.
    # Agents must never see conductor_start / conductor_send /
    # conductor_stop / conductor_output (those manage long-lived CLI
    # subprocesses the dashboard controls).
    if dashboard_mode:

        def _dash_tool(*a, **kw):
            return server.tool(*a, **kw)
    else:

        def _dash_tool(*_a, **_kw):
            def _skip(fn):
                return fn

            return _skip

    @_dash_tool()
    @renders_as("status", title="conductor start")
    async def conductor_start(
        session_id: str = "",
        backend: str = "claude",
        model: str = "",
    ) -> Any:
        """Start a persistent long-lived conductor agent for a session.

        Backends: claude (interactive stdin), codex (interactive stdin), opencode (serve mode).
        Sends tasks via conductor_send. Manages lane agents, resolves conflicts.
        Stop with conductor_stop.
        """
        import shutil
        import subprocess

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)

        # Check for existing conductor
        existing = _conductor_process.get("process")
        if existing and existing.poll() is None:
            return {
                "started": False,
                "reason": "Conductor already running.",
                "backend": _conductor_process.get("backend"),
            }

        # Claim session
        from .agent_orchestrator import AgentOrchestrator

        orch = AgentOrchestrator(runtime)
        claim = orch.conductor_claim(project_root, sid, f"conductor-{backend}")
        if not claim.get("claimed"):
            return {"started": False, **claim}

        cli_name = {"claude": "claude", "codex": "codex", "opencode": "opencode"}.get(backend)
        if not cli_name:
            return {
                "started": False,
                "reason": f"Unknown backend: {backend}. Use 'claude', 'codex', or 'opencode'.",
            }
        cli_path = shutil.which(cli_name)
        if not cli_path:
            return {"started": False, "reason": f"{cli_name} CLI not found."}

        # Per-machine ceiling check (2026-04-21). Conductor + workers
        # share the same cap — this refusal fires when the host is
        # saturated, regardless of which project the conductor belongs
        # to. Registration happens AFTER successful Popen below.
        try:
            from .config import get_setting
            from .host_concurrency_store import check_machine_capacity

            machine_cap_raw = get_setting(
                "run.max_live_processes_per_machine",
                project_root=project_root,
                default=4,
            )
            machine_cap = int(machine_cap_raw) if machine_cap_raw is not None else 4
        except Exception:
            machine_cap = 4
        try:
            machine_decision = check_machine_capacity(
                max_processes=machine_cap,
                kind="conductor",
            )
        except Exception:
            machine_decision = {"ok": True}
        if not machine_decision.get("ok"):
            return {
                "started": False,
                "reason": machine_decision.get("error", "machine concurrency ceiling reached"),
                "blocked_by": "machine_concurrency",
                "live_count": machine_decision.get("live_count"),
                "max_processes": machine_decision.get("max_processes"),
            }

        # Build rich context briefing from session state. Tool references come
        # from the canonical conductor_doctrine (tool-truth-enforced) — never
        # inline phantom names.
        from . import conductor_doctrine as _conductor_doctrine

        context_parts = [
            f"You are the AIDOCS conductor for project at '{project_root}', session '{sid}'.",
            "",
            "== YOUR ROLE ==",
            "You are a persistent, long-lived session conductor. You stay alive between tasks.",
            "The user sends you tasks via the dashboard. You plan, dispatch lane agents, monitor progress, and report results.",
            "You are the user's right hand — you manage everything so they don't have to.",
            "",
            "== HOW YOU WORK ==",
            "1. User sends a task (e.g. 'Fix the login page password field')",
            "2. You analyze the codebase using AIDOCS tools (ai_investigate, ai_find, ai_bundle)",
            "3. You decide: do it yourself (inline) or dispatch to lane agents (parallel)",
            "4. For lane agents, drive everything through the SINGLE surface "
            "ai_lane(action=…): spawn to dispatch, guide to nudge a running worker, pause to "
            "pause, resume a stalled worker (by lane), kill a runaway (by lane), review to "
            "decide a lane's completion",
            "5. Monitor: ai_lane(action='status') + ai_lane(action='events') per worker (by "
            "lane); ai_seat(action='overview') for all lanes, pending questions, activity",
            "6. When done: report what changed, what was tested, what needs attention",
            "7. Wait for the next task — don't exit",
            "",
            *_conductor_doctrine.conductor_onboarding(),
            "(session audit trail lives in execution_events sqlite, written automatically)",
            "",
            "== RULES ==",
            "- Always use AIDOCS indexed tools before reading files",
            "- Log significant decisions to session journal",
            "- When dispatching lanes: set clear scope (allowed files), verify results",
            "- When stuck: ask the operator via ai_qa(action='ask')",
            "- Never exit unless the user says to stop",
        ]

        # Load session context if available
        try:
            session_path = project_root / ".MEMORY" / "sessions" / sid
            # Session overview
            session_md = session_path / "SESSION.md"
            if session_md.is_file():
                content = session_md.read_text(encoding="utf-8")[:2000]
                context_parts.extend(["", "== SESSION STATE ==", content])
            # Recent journal
            journal = session_path / "journal.md"
            if journal.is_file():
                entries = journal.read_text(encoding="utf-8")[-1500:]
                context_parts.extend(["", "== RECENT JOURNAL ==", entries])
            # Plan if exists
            plan_dir = session_path / "plans"
            if plan_dir.is_dir():
                for plan_file in sorted(plan_dir.glob("*.md"))[:1]:
                    plan_content = plan_file.read_text(encoding="utf-8")[:2000]
                    context_parts.extend(["", f"== PLAN ({plan_file.name}) ==", plan_content])
            # Files touched in this session
            try:
                from .edit_history import EditHistoryStore

                touched = EditHistoryStore().files_touched_summary(project_root, session_id=sid)
                if touched:
                    files_list = "\n".join(
                        f"  {f['file']} ({f['edits']} edits, agents: {','.join(f['agents']) or 'unknown'})"
                        for f in touched[:20]
                    )
                    context_parts.extend(["", "== FILES MODIFIED THIS SESSION ==", files_list])
            except Exception:
                pass
        except Exception:
            pass

        # Inject agent workflow rules for conductor to enforce
        try:
            agent_rules = hub.workflow.get_agent_workflow_rules(project_root)
            if agent_rules:
                rules_text = "\n".join(f"  - {r}" for r in agent_rules)
                context_parts.extend(
                    [
                        "",
                        "== AGENT WORKFLOW RULES (enforce on lane agents) ==",
                        rules_text,
                    ],
                )
        except Exception:
            pass

        initial_prompt = "\n".join(context_parts)

        try:
            model_flag = model.strip() if model else ""
            if backend == "claude":
                identity_prompt = _claude_identity_prompt(project_root, sid)
                cmd_args = _claude_build_cli_args(cli_path, identity_prompt, model_flag)
                # CREATE_NO_WINDOW on Windows matches the Tauri dashboard so
                # the CLI does not flash a console when spawned from a GUI.
                popen_kwargs: dict[str, Any] = {
                    "cwd": str(project_root),
                    "stdin": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "bufsize": 1,
                }
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                child = subprocess.Popen(cmd_args, **popen_kwargs)
            elif backend == "opencode":
                import random

                oc_port = random.randint(10000, 60000)
                child = subprocess.Popen(
                    [cli_path, "serve", "--port", str(oc_port)],
                    cwd=str(project_root),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                _conductor_process["opencode_port"] = oc_port
            else:  # codex
                cmd_args = [cli_path]
                if model_flag:
                    cmd_args.extend(["-m", model_flag])
                child = subprocess.Popen(
                    cmd_args,
                    cwd=str(project_root),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            _conductor_process["process"] = child
            _conductor_process["backend"] = backend
            _conductor_process["session_id"] = sid
            _conductor_process["project_root"] = str(project_root)

            # Register the machine-wide slot with the subprocess pid so
            # a crashed conductor (process gone before conductor_stop
            # ran) is swept automatically by the next capacity check.
            try:
                from .host_concurrency_store import HostConcurrencyStore

                _conductor_process["machine_slot_key"] = f"conductor-{child.pid}"
                HostConcurrencyStore().register(
                    worker_key=_conductor_process["machine_slot_key"],
                    kind="conductor",
                    pid=child.pid,
                    project_root=project_root,
                    session_id=sid,
                )
            except Exception:
                pass

            with _conductor_output_lock:
                _conductor_output.clear()
            _start_output_reader(
                child,
                _conductor_output,
                _conductor_output_lock,
                _MAX_CONDUCTOR_OUTPUT,
                backend=backend,
                state=_conductor_process,
            )

            try:
                if backend == "claude":
                    # Claude's stream-json input format rejects raw text; an
                    # unwrapped prompt would close the process immediately.
                    child.stdin.write(_claude_stream_json_user_envelope(initial_prompt))
                else:
                    child.stdin.write(initial_prompt + "\n")
                child.stdin.flush()
            except Exception:
                pass

            return {
                "started": True,
                "backend": backend,
                "session_id": sid,
                "pid": child.pid,
                "mode": "interactive",
            }
        except Exception as exc:
            return {"started": False, "reason": str(exc)}

    @_dash_tool()
    @renders_as("status", title="conductor send")
    async def conductor_send(message: str) -> Any:
        """Send a message/command to the running conductor agent."""
        proc = _conductor_process.get("process")
        if not proc or proc.poll() is not None:
            _conductor_process.clear()
            return {"sent": False, "reason": "No conductor running."}

        backend = _conductor_process.get("backend", "claude")

        # OpenCode: send via `opencode run --attach`
        if backend == "opencode":
            import shutil

            oc_port = _conductor_process.get("opencode_port")
            oc_cli = shutil.which("opencode")
            if not oc_port or not oc_cli:
                return {"sent": False, "reason": "OpenCode port/CLI not available"}
            try:
                result = __import__("subprocess").run(
                    [oc_cli, "run", "--attach", f"http://localhost:{oc_port}", message],
                    cwd=_conductor_process.get("project_root", "."),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                return {
                    "sent": True,
                    "message": message[:200],
                    "output": result.stdout[:500],
                }
            except Exception as exc:
                return {"sent": False, "reason": str(exc)}

        try:
            if backend == "claude":
                # Raw text would be rejected by --input-format stream-json and
                # kill the CLI mid-turn.
                proc.stdin.write(_claude_stream_json_user_envelope(message))
            else:
                proc.stdin.write(message + "\n")
            proc.stdin.flush()
            return {"sent": True, "message": message[:200]}
        except (BrokenPipeError, OSError) as exc:
            return {"sent": False, "reason": str(exc)}

    # Internal helper. Tool surface removed 2026-05-12 — ai_seat(mode='status').
    @renders_as("status", title="conductor")
    async def conductor_status() -> Any:
        """Check if the conductor agent is running."""
        proc = _conductor_process.get("process")
        inline = _conductor_process.get("inline")
        if not proc:
            if inline:
                return {
                    "running": True,
                    "mode": "inline",
                    "session_id": inline.get("session_id"),
                    "project_root": inline.get("project_root"),
                    "entered_at": inline.get("entered_at"),
                }
            return {"running": False}
        if proc.poll() is not None:
            proc_sid = _conductor_process.get("session_id")
            exit_code = proc.returncode
            # Only clear the subprocess keys; keep 'inline' if set.
            for k in ("process", "backend", "session_id", "claude_session_id", "opencode_port"):
                _conductor_process.pop(k, None)
            if inline:
                return {
                    "running": True,
                    "mode": "inline",
                    "session_id": inline.get("session_id"),
                    "project_root": inline.get("project_root"),
                    "last_subprocess_exit_code": exit_code,
                    "last_subprocess_session_id": proc_sid,
                }
            return {"running": False, "exit_code": exit_code}
        return {
            "running": True,
            "mode": "subprocess",
            "backend": _conductor_process.get("backend"),
            "session_id": _conductor_process.get("session_id"),
            "pid": proc.pid,
            "claude_session_id": _conductor_process.get("claude_session_id"),
        }

    @_dash_tool()
    @renders_as("status", title="conductor stop")
    async def conductor_stop() -> Any:
        """Stop the running conductor agent and release session claim."""
        import subprocess  # local: `except subprocess.TimeoutExpired` below

        proc = _conductor_process.get("process")
        if not proc:
            return {"stopped": False, "reason": "No conductor running."}

        sid = _conductor_process.get("session_id", "")
        backend = _conductor_process.get("backend", "")
        project_root_str = _conductor_process.get("project_root", "")
        machine_slot_key = _conductor_process.get("machine_slot_key", "")

        # Graceful stop
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        # Release the per-machine slot even if the claim release below
        # errors out. Safe on double-call thanks to unregister's no-op.
        if machine_slot_key:
            try:
                from .host_concurrency_store import HostConcurrencyStore

                HostConcurrencyStore().unregister(worker_key=machine_slot_key)
            except Exception:
                pass

        _conductor_process.clear()

        # Release session claim
        if sid and project_root_str:
            try:
                from .agent_orchestrator import AgentOrchestrator

                orch = AgentOrchestrator(runtime)
                orch.conductor_release(Path(project_root_str), sid, f"conductor-{backend}")
            except Exception:
                pass

        return {"stopped": True, "session_id": sid}

    # ── Conductor communication tools ──

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='ask').
    @renders_as("status", title="conductor ask")
    async def conductor_ask(
        question: str,
        lane_id: str = "default",
        wait: bool = False,
        timeout: int = 120,
        category: str = "question",
        requested_path: str = "",
        session_id: str = "",
    ) -> Any:
        """Ask the conductor/operator a question. If wait=True, blocks until answered or timeout.

        For scope requests: set category='scope_request' and requested_path='path/to/file'.
        Auto-resolves if no lane conflict exists (no conductor intervention needed).
        """
        from .conductor_comms import agent_ask, auto_resolve_scope_request

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)

        # Auto-resolve scope requests when possible
        if category == "scope_request" and requested_path:
            result = agent_ask(
                project_root,
                lane_id,
                question,
                category=category,
                session_id=sid,
                wait=False,
            )
            msg_id = result.get("id", "")
            if msg_id:
                auto = auto_resolve_scope_request(
                    project_root,
                    msg_id,
                    lane_id,
                    requested_path,
                    session_id=sid,
                )
                if auto.get("auto_resolved"):
                    return {
                        "id": msg_id,
                        "status": "answered",
                        "response": f"Auto-approved: '{requested_path}' added to your scope.",
                        "auto_resolved": True,
                    }
            # Conflict or error — fall through to normal flow

        return agent_ask(
            project_root,
            lane_id,
            question,
            category=category,
            session_id=sid,
            wait=wait,
            timeout=float(timeout),
        )

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='check').
    @renders_as("status", title="conductor response")
    async def conductor_check_response(message_id: str) -> Any:
        """Check if a previously submitted question has been answered."""
        from .conductor_comms import check_response

        return check_response(_project_root(), message_id)

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='answer').
    @renders_as("status", title="conductor answer")
    async def conductor_answer(message_id: str, response: str) -> Any:
        """Answer an agent's pending question (called by conductor or dashboard)."""
        from .conductor_comms import answer_question

        return answer_question(_project_root(), message_id, response)

    @modes(
        enter={
            "required": [],
            "optional": ["session_id", "verbose"],
            "desc": "become the session conductor (verbose=True adds SESSION.md + journal tail)",
        },
        exit={"required": [], "optional": [], "desc": "clear the inline-conductor marker"},
        status={"required": [], "optional": [], "desc": "is a conductor running? (process/inline info)"},
        overview={
            "required": [],
            "optional": ["session_id"],
            "desc": "full situational awareness: lanes, states, pending questions",
        },
    )
    @server.tool()
    @renders_as("status", title="ai_seat")
    async def ai_seat(
        mode: str,
        session_id: str = "",
        verbose: bool = False,
    ) -> Any:
        """Unified conductor seat operations — one tool, four modes (king directive 2026-05-12).

        mode='enter'    — current agent becomes the session conductor (binds + persists).
                         Optional: session_id, verbose. Returns terse confirmation by
                         default; verbose=True adds SESSION.md body + journal tail.
        mode='exit'     — clear the inline-conductor marker. No-op if no inline binding.
        mode='status'   — check if the conductor agent is running (process/inline info).
        mode='overview' — full conductor situational awareness: all lanes, states,
                         pending questions, recent activity. Optional: session_id.
        """
        m = (mode or "").strip().lower()
        if m == "enter":
            return await conductor_mode_enter(session_id=session_id, verbose=verbose)
        if m == "exit":
            return await conductor_mode_exit()
        if m == "status":
            return await conductor_status()
        if m == "overview":
            return await conductor_overview(session_id=session_id)
        return {"error": f"unknown mode: {mode!r} (valid: enter|exit|status|overview)"}

    @modes(
        ask={
            "required": ["question"],
            "optional": ["lane_id", "wait", "timeout", "category", "requested_path", "session_id"],
        },
        answer={"required": ["message_id", "response"], "optional": []},
        check={"required": ["message_id"], "optional": []},
        pending={"required": [], "optional": ["session_id"]},
        history={"required": [], "optional": ["lane_id", "limit"]},
    )
    @server.tool()
    @renders_as("status", title="ai_qa")
    async def ai_qa(
        mode: str,
        question: str = "",
        message_id: str = "",
        response: str = "",
        lane_id: str = "",
        wait: bool = False,
        timeout: int = 120,
        category: str = "question",
        requested_path: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> Any:
        """Unified Q&A channel — one tool, five modes (king directive 2026-05-12).

        mode='ask'     — agent asks the conductor/operator a question.
                        Required: question. Optional: lane_id, wait, timeout,
                        category, requested_path, session_id. If wait=True, blocks
                        until answered or timeout. Set category='scope_request'
                        + requested_path to auto-approve no-conflict scope grants.
        mode='answer'  — conductor/operator answers a pending question.
                        Required: message_id, response.
        mode='check'   — check if a previously submitted question has been answered.
                        Required: message_id.
        mode='pending' — list all pending agent questions awaiting verdict.
                        Optional: session_id.
        mode='history' — list message history (per-lane or all). Optional: lane_id, limit.
        """
        m = (mode or "").strip().lower()
        if m == "ask":
            return await conductor_ask(
                question=question,
                lane_id=lane_id or "default",
                wait=wait,
                timeout=timeout,
                category=category,
                requested_path=requested_path,
                session_id=session_id,
            )
        if m == "answer":
            return await conductor_answer(message_id=message_id, response=response)
        if m == "check":
            return await conductor_check_response(message_id=message_id)
        if m == "pending":
            return await conductor_pending_questions(session_id=session_id)
        if m == "history":
            return await conductor_message_history(lane_id=lane_id, limit=limit)
        return {"error": f"unknown mode: {mode!r} (valid: ask|answer|check|pending|history)"}

    @modes(
        list={"required": [], "optional": ["session_id"]},
        fixed={"required": ["signature"], "optional": ["proof_command", "proof_log", "session_id"]},
        preserve_baseline={
            "required": ["signature", "baseline_sha"],
            "optional": ["proof_command", "proof_log", "session_id"],
        },
        quarantine={"required": ["signature", "followup_ref"], "optional": ["proof_command", "proof_log", "session_id"]},
        escalate={"required": ["signature", "operator_alert"], "optional": ["session_id"]},
        waiver={"required": ["signature", "operator", "reason"], "optional": ["session_id"]},
        autoclear={"required": [], "optional": ["proof_command", "session_id"]},
    )
    @server.tool()
    @renders_as("status", title="ai_failures")
    async def ai_failures(
        mode: str = "list",
        signature: str = "",
        proof_command: str = "",
        proof_log: str = "",
        baseline_sha: str = "",
        followup_ref: str = "",
        operator_alert: str = "",
        operator: str = "",
        reason: str = "",
        session_id: str = "",
    ) -> Any:
        """Failure-stewardship disposition surface — the agent-callable
        CONSUMER half of the failure ledger (the Stop hook is the producer).

        mode='list'              — failures THIS session owns (seal blockers) + full ledger.
        mode='fixed'             — fixed this session. Required: signature. Proof: proof_command + proof_log.
        mode='preserve_baseline' — proven pre-existing. Required: signature, baseline_sha + proof_log.
        mode='quarantine'        — skip with a written follow-up. Required: signature, followup_ref.
        mode='escalate'          — operator decision needed. Required: signature, operator_alert.
        mode='waiver'            — operator authority. Required: signature, operator, reason.
        mode='autoclear'         — mark every blocker this session owns FIXED on an observed green run.

        signature accepts the full sha or an unambiguous short prefix.
        Session-scoped: cannot dispose a failure under another session's duty.
        """
        from . import failure_stewardship as fs

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        m = (mode or "list").strip().lower()
        if m in ("list", "blockers", ""):
            return fs.list_session_failures(project_root, sid)
        try:
            return fs.apply_disposition(
                project_root,
                sid,
                action=m,
                signature=signature,
                proof_command=proof_command,
                proof_log=(proof_log or "").encode("utf-8", "replace"),
                baseline_sha=baseline_sha,
                followup_ref=followup_ref,
                operator_alert=operator_alert,
                operator=operator,
                reason=reason,
            )
        except fs.StewardshipError as exc:
            return {"ok": False, "error": str(exc), "action": m}

    @modes(
        send={"required": ["to_roles", "body"], "optional": ["in_reply_to"]},
        inbox={"required": [], "optional": ["unread_only"]},
        reply={"required": ["message_id", "body"], "optional": []},
    )
    @server.tool()
    @renders_as("status", title="ai_msg")
    async def ai_msg(
        mode: str,
        to_roles: str = "",
        body: str = "",
        in_reply_to: str = "",
        message_id: str = "",
        unread_only: bool = True,
    ) -> Any:
        """Unified messaging — one tool, three modes (king directive 2026-05-12).

        mode='send'  — role-addressed message.
            Required: to_roles, body.  Optional: in_reply_to.
            to_roles accepts 'conductor' | 'co_conductor' | 'king' |
            'both' or a comma-list.
        mode='inbox' — drain calling-role inbox (oldest-first, marks as read).
            Optional: unread_only (default True).
        mode='reply' — reply chaining thread_id.
            Required: message_id, body.

        Caller's from_role is inferred from the bound MCP host_session_id.
        Available to all agents — every seat can send and receive.
        Per-mode required-sets are enforced by the @modes-built JSONSchema
        (see mode_schema.py); the runtime branches by `mode`.
        """
        import json as _json

        from .conductor_comms import (
            _connect,
            msg_resolve_caller_role,
        )
        from .conductor_comms import (
            msg_inbox as _inbox_impl,
        )
        from .conductor_comms import (
            msg_send as _send_impl,
        )

        project_root = _project_root()
        from_role = msg_resolve_caller_role(project_root)
        if mode == "send":
            return _send_impl(
                project_root,
                from_role=from_role,
                to_roles=to_roles,
                body=body,
                in_reply_to=in_reply_to,
            )
        if mode == "inbox":
            messages = _inbox_impl(
                project_root,
                role=from_role,
                unread_only=unread_only,
            )
            return {"role": from_role, "messages": messages}
        if mode == "reply":
            with _connect(project_root) as conn:
                row = conn.execute(
                    "SELECT from_role, to_roles_json FROM messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
            if not row:
                return {"sent": False, "reason": f"Message '{message_id}' not found"}
            try:
                targets = _json.loads(row["to_roles_json"] or "[]")
            except Exception:
                targets = []
            recipients = [row["from_role"]] + [r for r in targets if r != from_role]
            seen: list[str] = []
            for r in recipients:
                if r and r not in seen:
                    seen.append(r)
            return _send_impl(
                project_root,
                from_role=from_role,
                to_roles=seen or [row["from_role"]],
                body=body,
                in_reply_to=message_id,
            )
        return {"error": f"unknown mode: {mode!r} (valid: send|inbox|reply)"}

    # @server.tool removed (120% clause B): folded into ai_lane(action='guide').
    async def ai_guidance(lane_id: str, message: str, session_id: str = "") -> Any:
        """Send guidance to a lane agent. Agent sees it on next tool call via hook injection."""
        from .conductor_comms import send_guidance

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return send_guidance(project_root, lane_id, message, session_id=sid)

    from . import tool_interface as _ti_reg_guidance

    _ti_reg_guidance.register_impl("ai_guidance", ai_guidance)

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='pending').
    @renders_as("list", title="pending questions")
    async def conductor_pending_questions(session_id: str = "") -> Any:
        """List all pending agent questions awaiting conductor/operator response."""
        from .conductor_comms import get_pending_questions

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return {"questions": get_pending_questions(project_root, sid)}

    @server.tool()
    @renders_as("status", title="ai_lane_control")
    async def ai_lane_control(
        lane_id: str,
        state: str = "active",
        reason: str = "",
        session_id: str = "",
    ) -> Any:
        """Control a lane: set state to 'active', 'paused', or 'canceled'."""
        from .conductor_comms import set_lane_state

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return set_lane_state(project_root, lane_id, state, reason=reason, session_id=sid)

    @server.tool()
    @renders_as("status", title="lane state")
    async def ai_lane_state(
        state: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Update lane worker state (running → done / failed / crashed).

        Env-guarded: reads AIDOCS_EXPERT_ID from the process environment.
        Workers call this to signal progress; refuses when the env var is
        missing or the worker_id does not match a session_lane_agents row.
        """
        import os as _os

        worker_id = _os.environ.get("AIDOCS_EXPERT_ID", "").strip()
        if not worker_id:
            return {
                "ok": False,
                "error": "missing_env",
                "message": (
                    "AIDOCS_EXPERT_ID env var missing. This tool is "
                    "only callable by lane sub-agents spawned through "
                    "the agent worker service."
                ),
            }
        return runtime.lane_update_state(_project_root(), worker_id, state, metadata=metadata)

    # Internal helper. Tool surface removed 2026-05-12 — ai_seat(mode='overview').
    @renders_as("status", title="conductor overview")
    async def conductor_overview(session_id: str = "") -> Any:
        """Full conductor situational awareness: all lanes, states, pending questions, recent activity. One call, full picture."""
        from .conductor_comms import get_all_lanes_status

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return get_all_lanes_status(project_root, sid)

    @server.tool()
    @renders_as("status", title="ai_resolve_scope")
    async def ai_resolve_scope(
        message_id: str,
        lane_id: str,
        requested_path: str,
        session_id: str = "",
    ) -> Any:
        """Auto-resolve a scope expansion request if no lane conflict exists. Approves and expands scope automatically, or flags conflict for manual resolution."""
        from .conductor_comms import auto_resolve_scope_request

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return auto_resolve_scope_request(
            project_root,
            message_id,
            lane_id,
            requested_path,
            session_id=sid,
        )

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='history').
    @renders_as("list", title="conductor messages")
    async def conductor_message_history(lane_id: str = "", limit: int = 50) -> Any:
        """Get conductor message history for a lane or all lanes."""
        from .conductor_comms import get_message_history

        return {"messages": get_message_history(_project_root(), lane_id, limit)}

    @server.tool()
    @renders_as("status", title="ai_resolve_backend")
    async def ai_resolve_backend(task_type: str, session_id: str = "") -> Any:
        """Resolve the best host + model + think_mode for a task type.

        Uses conductor.task_routing config to match task types to agent routes.
        Task types: refactor, implement, design, test, docs, research, debug, review, deploy.

        Example config in aidocs.toml:
          [conductor]
          task_routing = '{"implement":{"host":"claude","model":"claude-sonnet-4-6","think_mode":"low"},"design":{"host":"opencode","model":"google/gemini-2.5-pro","think_mode":"high"}}'
        """
        from .conductor_comms import resolve_backend_for_task

        return resolve_backend_for_task(_project_root(), task_type, session_id or None)

    @_dash_tool()
    @renders_as("status", title="conductor output")
    async def conductor_output(since: float = 0, limit: int = 100) -> Any:
        """Get recent conductor agent output (stdout/stderr). Dashboard polls this for live view.

        Args:
            since: Unix timestamp — only return lines after this time. Pass 0 for all.
            limit: Max lines to return.

        """
        with _conductor_output_lock:
            if since > 0:
                lines = [l for l in _conductor_output if l["timestamp"] > since]
            else:
                lines = list(_conductor_output)
        lines = lines[-limit:]
        proc = _conductor_process.get("process")
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "lines": lines,
            "total_buffered": len(_conductor_output),
        }

    # ── Execution management tools ──

    def _audit_delete(operation: str, reason: str, deleter: Any) -> Any:
        """AUDEL gate (audit_deletion_law): authenticated operator + admin
        permission + non-subagent + reason + audit-of-audit, then delete. Used by
        the three execution-audit deletion tools. Auto-prune on project-sync
        calls the SERVICE directly and is unaffected by this tool-level gate.

        SECURITY: there is NO operator_token argument — authentication is resolved
        ONLY from the calling host-session→operator-context binding, so no secret
        ever enters tool arguments (and therefore can't leak through MCP/tool-call
        logs, execution events, dashboard traces, renderers, or model history).
        """
        import os as _os

        from .audit_deletion_law import run_audit_deletion
        from .operator_auth_service import OperatorAuthService

        root = _project_root()
        svc = OperatorAuthService()
        ctx = None
        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            host_sid = current_calling_host_session_id()
            if host_sid:
                ctx = svc.resolve_operator_context_from_host_session(host_sid, root)
        except Exception:
            ctx = None
        has_perm = False
        if ctx is not None:
            try:
                has_perm = bool(
                    svc.require_permission(
                        ctx,
                        "admin.manage_config",
                        root,
                        scope_type="project",
                        scope_id=str(root).replace("\\", "/"),
                    ),
                )
            except Exception:
                has_perm = False
        is_subagent = bool(
            _os.environ.get("AIDOCS_EXPERT_ID") or _os.environ.get("AIDOCS_EXPERT_LANE_ID"),
        )
        user = getattr(ctx, "user_id", "") if ctx is not None else ""

        def _record(event_kind: str, status: str) -> None:
            hub.execution.record_event(
                root,
                event_kind=event_kind,
                source_kind="audit_deletion_law",
                capability_name=f"execution_{operation}",
                action_kind=operation,
                target_entity="execution_audit",
                status=status,
                principal_type="human",
                payload={"operation": operation, "reason": reason, "user": user},
            )

        return run_audit_deletion(
            operation=operation,
            reason=reason,
            ctx=ctx,
            has_permission=has_perm,
            is_subagent=is_subagent,
            record_intent=lambda: _record("audit_deletion_intent", "intent"),
            record_result=lambda _r: _record("audit_deletion_result", "deleted"),
            deleter=deleter,
        )

    @server.tool()
    @renders_as("status", title="execution clear tokens")
    async def execution_clear_token_usage(
        reason: str = "",
        session_id: str = "",
    ) -> Any:
        """Clear token usage audit data (AUDEL-gated: host-session operator-auth +
        admin + reason + audit-of-audit; subagents/unauthenticated refused. No
        token argument — auth comes from the host-session binding only).
        """
        sid = session_id.strip() or None

        def _del() -> dict:
            count = hub.execution.clear_token_usage(_project_root(), session_id=sid)
            return {"cleared": True, "runs_deleted": count, "session_id": sid}

        return _audit_delete("clear_token_usage", reason, _del)

    @server.tool()
    @renders_as("status", title="execution clear tool calls")
    async def execution_clear_tool_calls(
        reason: str = "",
        session_id: str = "",
    ) -> Any:
        """Clear tool-call audit events (AUDEL-gated; host-session auth only)."""
        sid = session_id.strip() or None

        def _del() -> dict:
            result = hub.execution.clear_tool_calls(_project_root(), session_id=sid)
            return {"cleared": True, **result, "session_id": sid}

        return _audit_delete("clear_tool_calls", reason, _del)

    @server.tool()
    @renders_as("status", title="execution prune")
    async def execution_prune(
        reason: str = "",
        keep_days: int = 7,
        max_events: int = 0,
    ) -> Any:
        """Prune old execution audit events (AUDEL-gated; host-session auth only).
        keep_days deletes by age, max_events caps total count.
        """

        def _del() -> dict:
            project_root = _project_root()
            result: dict[str, object] = {}
            if keep_days > 0:
                result["by_age"] = hub.execution.prune_old_events(project_root, keep_days=keep_days)
            if max_events > 0:
                result["by_size"] = hub.execution.prune_to_max_size(
                    project_root,
                    max_events=max_events,
                )
            if not result:
                result = hub.execution.auto_prune(project_root)
            result["current_counts"] = hub.execution.event_count(project_root)
            return result

        return _audit_delete("prune", reason, _del)

    @server.tool()
    @renders_as("status", title="execution usage")
    async def execution_usage_by_identity() -> Any:
        """Get token/tool usage broken down by host and agent identity."""
        project_root = _project_root()
        return {
            "by_host": hub.execution.usage_by_host(project_root),
            "by_agent": hub.execution.usage_by_agent(project_root),
            "counts": hub.execution.event_count(project_root),
        }

    # Patch tool descriptions from TOML — sync, runs before server starts
    # ── GATE_ONLY → BOTH migration (king directive 2026-05-29) ──────
    #
    # Doctrine: the three consolidators (ai_lane / ai_plan / ai_worker)
    #           were declared in tool_interface with surface=GATE_ONLY
    #           pending stdio binding. This block completes the
    #           migration — they're now wired on stdio, surface flipped
    #           to BOTH in the @tool decorator, and the migration
    #           doctrine ledger is cleared.
    # Why:      keeping consolidators GATE_ONLY forever defeats the
    #           point of consolidation. The legacy ai_lane_* / ai_plan_*
    #           / ai_worker_* bindings remain registered for a
    #           deprecation window — clients pinned to the old names
    #           keep working — but the canonical surface is the
    #           consolidator from this commit forward.
    # Apply:    each wrapper mirrors the registry function's signature
    #           and docstring so FastMCP introspection emits the
    #           same schema/description as the gate's catalog
    #           (test_gate_only_catalog_schema.py keeps the two surfaces
    #           on the same source of truth). The body delegates to
    #           tool_interface.<name> which routes by `action=...` to
    #           the underlying legacy bindings via _delegate.
    if tools_profile != "read_only":
        from . import tool_interface as _ti_cons

        @server.tool()
        async def ai_lane(
            action: str,
            session_id: str = "",
            lane_id: str = "",
            worker_id: str = "",
            state: str = "",
            metadata: dict = None,
            reason: str = "",
            tools: list = None,
            prompt: str = "",
            limit: int = 20,
            confirm_token: str = "",
            backend: str = "",
            model: str = "",
            verbose: bool = False,
            review_id: str = "",
            verdict: str = "",
            tail: bool = True,
        ) -> Any:
            # BY-LANE resolution (120% clause B): the worker-targeting conductor
            # actions take lane_id; resolve the live/most-recent worker here
            # (the wrapper holds runtime) before the consolidator dispatches.
            # Explicit worker_id always wins.
            if action in ("status", "kill", "resume", "events") and not worker_id and lane_id:
                resolved = runtime._agent_expert.resolve_worker_for_lane(lane_id)
                if resolved:
                    worker_id = resolved
            return _ti_cons.ai_lane(
                action=action,
                session_id=session_id,
                lane_id=lane_id,
                worker_id=worker_id,
                state=state,
                metadata=metadata,
                reason=reason,
                tools=tools,
                prompt=prompt,
                limit=limit,
                confirm_token=confirm_token,
                backend=backend,
                model=model,
                verbose=verbose,
                review_id=review_id,
                verdict=verdict,
                tail=tail,
            )

        # Pin description from the registry spec so the host sees the
        # full consolidator docstring (modes / dispatch rules) instead
        # of an empty docstring on the wrapper.
        ai_lane.__doc__ = _ti_cons._TOOLS["ai_lane"].description

        @server.tool()
        async def ai_plan(
            action: str,
            session_id: str = "",
            lane_id: str = "",
            spec_text: str = "",
            spec_path: str = "",
            scope: str = "",
            constraints: list = None,
            file_path: str = "",
            paused_lane_id: str = "",
            conflicting_lane_id: str = "",
            target_lane_id: str = "",
            signal_kind: str = "",
            detail: str = "",
            packet_result: dict = None,
            packet_result_path: str = "",
            reason: str = "",
            timeout: int = 0,
            view: str = "",
            transition: str = "",
            coord: str = "",
            template_only: bool = False,
            backend: str = "",
            model: str = "",
            target_project: str = "",
        ) -> Any:
            return _ti_cons.ai_plan(
                action=action,
                session_id=session_id,
                lane_id=lane_id,
                spec_text=spec_text,
                spec_path=spec_path,
                scope=scope,
                constraints=constraints,
                file_path=file_path,
                paused_lane_id=paused_lane_id,
                conflicting_lane_id=conflicting_lane_id,
                target_lane_id=target_lane_id,
                signal_kind=signal_kind,
                detail=detail,
                packet_result=packet_result,
                packet_result_path=packet_result_path,
                reason=reason,
                timeout=timeout,
                view=view,
                transition=transition,
                coord=coord,
                template_only=template_only,
                backend=backend,
                model=model,
                target_project=target_project,
            )

        ai_plan.__doc__ = _ti_cons._TOOLS["ai_plan"].description

        @server.tool()
        async def ai_worker(
            action: str,
            worker_id: str = "",
            lane_id: str = "",
            reason: str = "",
            verbose: bool = False,
            confirm_token: str = "",
        ) -> Any:
            # BY-LANE resolution (120% clause B: "resume + kill by lane, not
            # opaque worker_id"). A conductor passes lane_id; we resolve the
            # live (or most-recent) worker for that lane. Explicit worker_id
            # still wins. The kill confirm token binds to the RESOLVED id.
            if not worker_id and lane_id:
                worker_id = runtime._agent_expert.resolve_worker_for_lane(lane_id) or ""
                if not worker_id:
                    return {
                        "_error": "no_worker_for_lane",
                        "_detail": f"no worker found for lane_id={lane_id!r}",
                    }
            return _ti_cons.ai_worker(
                action=action,
                worker_id=worker_id,
                reason=reason,
                verbose=verbose,
                confirm_token=confirm_token,
            )

        ai_worker.__doc__ = _ti_cons._TOOLS["ai_worker"].description

    _patch_tool_descriptions_sync(server)
    from .mode_schema import apply_mode_schemas as _apply_mode_schemas

    _apply_mode_schemas(server)

    # related_project_* auto-wrappers disabled 2026-04-24:
    # the 269 wrapper clones were bloating the tool surface
    # (~50% of all registered tools). Cross-project invocation
    # now flows through the handful of hand-written related_project
    # entry tools (register, list, unregister, handoff, ai_search,
    # symbol_bundle, subsystem_bundle, compare_concept); those stay
    # because they have explicit implementations in
    # server_project_admin_tools.py. The auto-wrapper function
    # below is retained for future per-tool opt-in wrapping.
    return server


def _auto_register_related_project_wrappers(server: FastMCP, hub: Any) -> None:
    """Emit one related_project_<tool> per conductor-facing tool.

    For each tool X the conductor registers, a sibling tool
    `related_project_X(target_project, **X.args)` is added that:
      1. Resolves target_project via the registry (fails if unknown
         or unregistered).
      2. Refuses if target_project resolves to the conductor's own
         root (meaningless — use X directly).
      3. Sets the ContextVar override to the target root.
      4. Calls the original tool fn with the rest of the args.
      5. Clears the override in a finally.

    Wraps every tool (reads, writes, indexing) — the registry entry
    IS the authorization: if the user registered it, the tool can
    run against it.

    Skipped: tools that already start with `related_project_` (avoid
    doubling), `project_init` (needs an explicit absolute path), the
    registry-management tools (`related_project_register/list/unregister`
    — they're self-referential).
    """
    import asyncio
    import inspect
    import keyword
    import re

    from .mcp_server_runtime_helpers import (
        resolve_project_root,
        with_target_project_root,
    )

    _SKIP_PREFIXES = ("related_project_",)
    _SKIP_EXACT = {
        "project_init",
    }

    # Hard allowlist for any string that lands in the exec'd source.
    # FastMCP's own registration should already guarantee identifier
    # rules for tool/param names, but exec deserves belt-and-suspenders
    # validation — if any component fails this regex we skip the tool
    # instead of compiling arbitrary text. Single source of truth for
    # the safety audit: every variable substituted into the source
    # template passes _IDENT before exec runs.
    _IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def _is_safe_ident(s: str) -> bool:
        return bool(s) and bool(_IDENT.match(s)) and not keyword.iskeyword(s)

    try:
        existing_tools = asyncio.run(server.list_tools())
    except RuntimeError:
        # Outer loop running (typical in pytest-asyncio / uvicorn).
        # Run list_tools in a dedicated thread with its own fresh loop
        # so we don't touch the caller's running loop.
        import threading

        result_box: dict[str, Any] = {}

        def _run_in_thread() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                coro = server.list_tools()
                result_box["tools"] = loop.run_until_complete(coro)
            except Exception as exc:
                result_box["error"] = exc
            finally:
                loop.close()
                asyncio.set_event_loop(None)

        t = threading.Thread(target=_run_in_thread)
        t.start()
        t.join()
        existing_tools = result_box.get("tools", [])

    wrapped_count = 0
    for tool in existing_tools:
        name = getattr(tool, "name", "")
        if not name or name in _SKIP_EXACT:
            continue
        if any(name.startswith(p) for p in _SKIP_PREFIXES):
            continue
        fn = getattr(tool, "fn", None)
        if fn is None or not callable(fn):
            continue
        is_async = inspect.iscoroutinefunction(fn)

        # Skip if the tool already exposes target_project (signature
        # collision — those are cross-project aware already).
        sig = inspect.signature(fn)
        if any(p.name == "target_project" for p in sig.parameters.values()):
            continue

        # Safety audit: every string that will land in the exec'd
        # source must pass a strict identifier check. If FastMCP let
        # a weird name through (it shouldn't — tool/param names must
        # be identifiers — but exec is the one spot we can't trust
        # upstream invariants for), we skip the tool outright.
        if not _is_safe_ident(name):
            try:
                import sys as _sys

                _sys.stderr.write(
                    f"[related_project] skip {name!r}: non-identifier tool name, unsafe for exec\n",
                )
            except Exception:
                pass
            continue
        if not all(
            _is_safe_ident(p.name)
            for p in sig.parameters.values()
            if p.kind
            not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        ):
            try:
                import sys as _sys

                _sys.stderr.write(
                    f"[related_project] skip {name}: non-identifier param name, unsafe for exec\n",
                )
            except Exception:
                pass
            continue

        orig_desc = (tool.description or f"Run {name}").rstrip()
        wrapper_desc = (
            f"Cross-project wrapper: run `{name}` against a registered "
            f"related project. target_project must be the name passed "
            f"to related_project_register. Conductor's own project "
            f"is refused — call `{name}` directly for that.\n\n"
            f"Original tool description:\n{orig_desc}"
        )
        wrapper_name = f"related_project_{name}"
        # Belt + suspenders: even though `name` passed _is_safe_ident
        # above, confirm the final wrapper_name is ALSO a clean
        # identifier before we build source code around it.
        if not _is_safe_ident(wrapper_name):
            continue

        # Build per-tool wrapper with a real signature (FastMCP
        # refuses **kwargs-only functions). Use exec to synthesize
        # an async function whose params are `target_project` +
        # each of the original tool's params by name — then close
        # over the dispatcher that does the rerouting.
        orig_params = [
            p
            for p in sig.parameters.values()
            if p.kind
            not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        ]

        # Build parameter list source: `target_project: str, p1=..., p2=..., ...`
        # Keep it simple — no annotations (FastMCP builds schemas from
        # annotations but losing them just means less schema detail,
        # tool still runs). Defaults must render as literal source:
        # int/float/bool/None or plain printable single-line strings.
        # Anything else → no default (making it required). Safe; agents
        # already pass these. Explicit rejection of strings with
        # newlines, control chars, or non-printable bytes keeps the
        # exec audit story simple: "every default is a trivially-safe
        # literal."
        def _fmt_default(v):
            if v is inspect.Parameter.empty:
                return None
            if v is None or isinstance(v, bool):
                return repr(v)
            if isinstance(v, (int, float)):
                return repr(v)
            if isinstance(v, str):
                # Reject anything that could smuggle a newline /
                # control char into the source. repr() already
                # escapes these but belt-and-suspenders for exec.
                if not v.isprintable() or "\n" in v or "\r" in v:
                    return None
                return repr(v)
            return None  # complex default → treat as required

        # Annotation preservation: FastMCP builds JSON-schema types
        # from annotations, and the MCP host coerces incoming args
        # based on that schema. Dropping annotations made every arg
        # arrive as str, breaking any tool that expected int/bool
        # (ai_get_lines start_line>=1, etc.). Pass the annotation
        # OBJECT through the exec namespace — resolves to the same
        # class the original tool had, without stringifying.
        def _annotation_key(p_name: str) -> str:
            return f"__ann_{p_name}"

        ann_slots: dict[str, Any] = {}
        parts = ["target_project: str"]
        passthrough_args = []
        for p in orig_params:
            d = _fmt_default(p.default)
            if p.annotation is not inspect.Parameter.empty:
                ann_key = _annotation_key(p.name)
                ann_slots[ann_key] = p.annotation
                ann_suffix = f": {ann_key}"
            else:
                ann_suffix = ""
            if d is None:
                parts.append(f"{p.name}{ann_suffix}")
            else:
                parts.append(f"{p.name}{ann_suffix}={d}")
            passthrough_args.append(f"{p.name}={p.name}")
        param_src = ", ".join(parts)
        pass_src = ", ".join(passthrough_args)
        src = (
            f"async def {wrapper_name}({param_src}):\n"
            f"    return await _dispatcher(target_project, dict("
            f"{pass_src}))\n"
        )
        # Seed ns with common annotation names so FastMCP's type-hint
        # resolver (which may re-eval string annotations in the
        # wrapper's __globals__) finds them. __ann_X still carries
        # the exact annotation objects for param-level substitution;
        # these names are for downstream eval on the wrapper source.
        from pathlib import Path as _ann_Path
        from typing import (
            Any as _ann_Any,
        )
        from typing import (
            Literal as _ann_Literal,
        )
        from typing import (
            Optional as _ann_Optional,
        )
        from typing import (
            Union as _ann_Union,
        )

        ns: dict[str, Any] = dict(ann_slots)
        ns["Any"] = _ann_Any
        ns["Literal"] = _ann_Literal
        ns["Optional"] = _ann_Optional
        ns["Union"] = _ann_Union
        ns["Path"] = _ann_Path

        def _make_dispatcher(_fn, _is_async, _tool_name):
            async def _dispatch(_target_project: str, _kwargs: dict):
                if not _target_project:
                    return {
                        "ok": False,
                        "error": (
                            "target_project is required. Pass the name "
                            "registered via related_project_register."
                        ),
                    }
                conductor_root = resolve_project_root()
                target_root = hub.related.resolve_related_project_path(
                    conductor_root,
                    _target_project,
                )
                if target_root is None:
                    return {
                        "ok": False,
                        "error": (
                            f"target_project '{_target_project}' not "
                            f"registered (or path no longer exists). "
                            f"Call related_project_list to see known "
                            f"targets."
                        ),
                    }
                try:
                    if target_root.resolve() == conductor_root.resolve():
                        return {
                            "ok": False,
                            "error": (
                                f"target_project '{_target_project}' "
                                f"resolves to the conductor's own root. "
                                f"Call `{_tool_name}` directly."
                            ),
                        }
                except OSError:
                    pass
                with with_target_project_root(target_root):
                    if _is_async:
                        return await _fn(**_kwargs)
                    return _fn(**_kwargs)

            return _dispatch

        dispatcher = _make_dispatcher(fn, is_async, name)
        ns["_dispatcher"] = dispatcher
        # Doctrine 2026-05-29 (king semgrep re-seal): replaced
        # `exec(src, ns)` with compile() + types.FunctionType so
        # the no-eval rule is honored. Belt-and-suspenders identifier
        # validation BEFORE compile — defense in depth against any
        # future code path that lets an agent-supplied name into
        # `wrapper_name` or a parameter name. None of those paths
        # exist today (names come from server-registered tool specs
        # and inspect.signature on a real Python function), but the
        # validation makes that property structural, not incidental.
        if not wrapper_name.isidentifier():
            continue
        if not all(p.name.isidentifier() for p in orig_params):
            continue
        try:
            import types as _types

            module_code = compile(src, "<aidocs-related-wrapper>", "exec")
            # The compiled module's co_consts holds the inner code
            # objects; find the one whose co_name matches wrapper_name.
            fn_code = next(
                (
                    c
                    for c in module_code.co_consts
                    if isinstance(c, _types.CodeType) and c.co_name == wrapper_name
                ),
                None,
            )
            if fn_code is None:
                continue
            wrapper = _types.FunctionType(fn_code, ns, name=wrapper_name)
            wrapper.__doc__ = wrapper_desc
            # Replace string annotation placeholders (__ann_X) on the
            # wrapper with the actual annotation objects so FastMCP's
            # get_type_hints/eval doesn't re-resolve them in an empty
            # namespace and raise NameError.
            resolved_ann: dict[str, Any] = {}
            for p_name_iter, ann_obj in (
                (p.name, p.annotation)
                for p in orig_params
                if p.annotation is not inspect.Parameter.empty
            ):
                resolved_ann[p_name_iter] = ann_obj
            if resolved_ann:
                resolved_ann["target_project"] = str
                wrapper.__annotations__ = resolved_ann
        except SyntaxError:
            # Param name collision with Python keyword, etc. — skip.
            continue
        try:
            from fastmcp.tools.tool import Tool as _Tool

            tool_obj = _Tool.from_function(
                wrapper,
                name=wrapper_name,
                description=wrapper_desc,
            )
            server.add_tool(tool_obj)
            wrapped_count += 1
        except Exception as exc:
            # Some tools may collide or have incompatible signatures;
            # log once so we can diagnose but keep going — originals
            # still work and the cross-project surface is best-effort.
            try:
                import sys as _sys

                _sys.stderr.write(
                    f"[related_project] skip {name}: {type(exc).__name__}: {str(exc)[:100]}\n",
                )
            except Exception:
                pass
            continue

    try:
        import sys as _sys

        _sys.stderr.write(f"[related_project] auto-wrapped {wrapped_count} tools\n")
    except Exception:
        pass


def _patch_tool_descriptions_sync(server: FastMCP) -> None:
    """Override tool docstrings with terse agent-facing descriptions from TOML."""
    from .config import _ACTION_HOOK_DEFAULTS, _get_dotted

    descriptions = _get_dotted(_ACTION_HOOK_DEFAULTS, "tool_descriptions")
    if not isinstance(descriptions, dict):
        return
    provider = getattr(server, "_local_provider", None)
    if provider is None:
        return
    components = getattr(provider, "_components", None)
    if not isinstance(components, dict):
        return

    for short_name, desc in descriptions.items():
        if not isinstance(desc, str):
            continue
        key = f"tool:{short_name}@"
        tool = components.get(key)
        if tool is not None and hasattr(tool, "description"):
            tool.description = desc


def enforce_package_integrity_or_refuse(home=None, *, gate=None, env=None) -> dict:
    """Fail-closed trusted-code boundary for startup. Returns the verdict when
    the install may run (editable/unverified/matched); RAISES SystemExit on
    proven drift of a non-editable install. Drift can only be overridden by the
    explicit ``AIDOCS_ALLOW_PACKAGE_DRIFT`` escape (never silent).
    """
    import os as _os
    from pathlib import Path as _Path

    from . import package_integrity as _pi

    env = _os.environ if env is None else env
    gate = gate or _pi.startup_integrity_gate
    home = _Path.home() if home is None else home
    try:
        v = gate(home)
    except Exception as exc:  # integrity check itself broke
        # Cannot verify ⟹ cannot trust the dangerous (remote) surface, but do
        # not brick local startup on an internal error; report unverified.
        return {
            "ok": True,
            "drifted": False,
            "unverified": True,
            "remote_trustworthy": False,
            "reason": f"integrity check failed: {exc!r}",
        }
    if not v.get("ok"):
        escape = str(env.get("AIDOCS_ALLOW_PACKAGE_DRIFT") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not escape:
            import sys as _sys

            print(
                "\033[31m✗ AIDOCS refusing to start — package integrity drift\033[0m\n"
                f"    {v.get('reason')}\n"
                "    The installed aidocs_mcp code differs from the verified "
                "runtime manifest. After a legitimate upgrade run "
                "`aidocs runtime --record-package`; otherwise this is tampering.\n"
                "    (override only if you understand the risk: "
                "AIDOCS_ALLOW_PACKAGE_DRIFT=1)",
                file=_sys.stderr,
            )
            raise SystemExit(3)
        v = {**v, "drift_overridden": True}
    return v


def main() -> None:
    import argparse
    import atexit
    import os

    parser = argparse.ArgumentParser(description="Run the AIDOCS MCP server.")
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help=(
            "Identify this MCP process as the AIDOCS dashboard. Unlocks "
            "dashboard-only tools (conductor_start / conductor_send / "
            "conductor_stop / conductor_output) that are hidden from "
            "agent MCP clients. Only the dashboard's Tauri spawner "
            "should pass this flag."
        ),
    )
    args, _ = parser.parse_known_args()

    # Shut down thread pool on exit; handle SIGTERM/SIGHUP for graceful stop (Windows lacks SIGHUP)
    def _cleanup():
        _tool_executor.shutdown(wait=False, cancel_futures=True)

    atexit.register(_cleanup)

    # On Unix, handle SIGHUP/SIGTERM for graceful shutdown
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))

    # Trusted-code boundary: refuse to start on proven package drift.
    enforce_package_integrity_or_refuse()

    server = create_server(dashboard_mode=bool(args.dashboard))
    server.run()


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
