from __future__ import annotations

import asyncio
import json
import functools
import signal
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any
from uuid import uuid4

from .config import (
    TOOLS_CALL_TIMEOUT,
    TOOLS_SYNC_TIMEOUT,
    TOOLS_GIT_TIMEOUT,
    TOOLS_MAX_TIMEOUT,
    render_interaction_text,
    _load_action_hook_defaults,
)
from .config_schema import available_config_edit_modes, self_edit_available_in_profile
from .file_ops import (
    get_lines as _file_get_lines,
    edit_lines as _file_edit_lines,
    batch_edit as _file_batch_edit,
    create_file as _file_create_file,
    str_replace as _file_str_replace,
    batch_str_replace as _file_batch_str_replace,
    extract_block as _file_extract_block,
)
from .git_helpers import run_git_sync as _run_git_sync
from .project_registry_service import ProjectRegistryService
from .runtime_service import RuntimeService
from .server_code_edit_tools import register_code_edit_tools
from .server_code_tools import register_code_tools
from .server_legacy_git_tools import register_legacy_git_tools
from .server_memory_index_tools import register_memory_index_tools
from .server_plan_task_tools import register_plan_task_tools
from .server_project_admin_tools import register_project_admin_tools
from .server_runtime_context_tools import register_runtime_context_tools
from .server_session_tools import register_session_tools
from .server_skill_tools import register_skill_tools
from .mcp_server_runtime_helpers import (
    all_capabilities as _all_capabilities,
    all_procedures as _all_procedures,
    capture_enabled as _capture_enabled,
    project_root_from_args as _project_root_from_args,
    registered_tools as _registered_tools,
    resolve_related_root as _resolve_related_root,
    summarize_tool_result as _summarize_tool_result,
)
from .service_hub import AidocsServiceHub
from .skill_resolution import (
    match_selected_skill_id_for_trigger as _match_selected_skill_id_for_trigger,
)
from .skill_provider import BUNDLED_PROVIDER_ID


_BUNDLED_OVERRIDE_PROVIDER_ID = "superpowers_external"
_RUNTIME_OWNED_OVERRIDE_MODES = {"aidocs_runtime_owned"}


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


def _run_with_timeout(fn, timeout_seconds: int, *args, **kwargs) -> Any:
    """Run a sync function with a timeout. Returns result or raises TimeoutError."""
    future = _tool_executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(
            f"Tool call timed out after {timeout_seconds}s. Use timeout= parameter for longer operations."
        )


def _resolve_timeout(kwargs: dict, default: int | None = None) -> int:
    """Extract and validate timeout from kwargs, falling back to category default."""
    timeout = kwargs.pop("timeout", None)
    fallback = default or TOOLS_CALL_TIMEOUT
    if timeout is None:
        return fallback
    timeout = int(timeout)
    if timeout <= 0:
        return fallback
    return min(timeout, TOOLS_MAX_TIMEOUT)


def _make_timed_decorator(default_timeout_value: int):
    """Factory for timed tool decorators with a specific default timeout."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            timeout = _resolve_timeout(kwargs, default=default_timeout_value)
            try:
                return _run_with_timeout(fn, timeout, *args, **kwargs)
            except TimeoutError as exc:
                return {"error": str(exc), "timeout": timeout}
            except Exception as exc:
                return {"error": f"Tool failed: {exc}"}

        return wrapper

    return decorator


# Category-specific decorators
timed_tool = _make_timed_decorator(TOOLS_CALL_TIMEOUT)  # 10s default
timed_sync = _make_timed_decorator(TOOLS_SYNC_TIMEOUT)  # 30s default
timed_git = _make_timed_decorator(TOOLS_GIT_TIMEOUT)  # 30s default


_GIT_SAFE_DIR = ["-c", "safe.directory=*"]
_GIT_TIMEOUT = 10


def _make_timed_async_decorator(default_timeout_value: int):
    """Factory for async timed tool decorators."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            timeout = _resolve_timeout(kwargs, default=default_timeout_value)
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
            except asyncio.TimeoutError:
                return {
                    "error": f"Tool call timed out after {timeout}s. Use timeout= parameter for slower operations.",
                    "timeout": timeout,
                }
            except Exception as exc:
                return {"error": f"Tool failed: {exc}"}

        return wrapper

    return decorator


timed_tool_async = _make_timed_async_decorator(TOOLS_CALL_TIMEOUT)
timed_git_async = _make_timed_async_decorator(TOOLS_GIT_TIMEOUT)


_GIT_FAST_DIVERGENCE = 500
_GIT_SAMPLE_DIVERGENCE = 1500



def _grant_known_exact_path_read(
    hub: AidocsServiceHub, project_root: Path, tool_name: str, path: str
) -> None:
    """Grant per-file read access via AccessGate."""
    from .access_gate import AccessGate

    managed = hub.managed_mode.get_mode(project_root)
    session_id = managed.get("session_id") if isinstance(managed, dict) else None
    if not managed.get("active") or not session_id:
        return
    AccessGate.grant_discovery(
        hub.query_gate, project_root, str(session_id), tool_name, [path]
    )


def _post_edit_reindex_and_grant(
    hub: AidocsServiceHub, project_root: Path, tool_name: str, path: str
) -> None:
    """After a successful edit: grant read access + reindex + invalidate config caches if needed."""
    _grant_known_exact_path_read(hub, project_root, tool_name, path)
    canonical = path.replace("\\", "/").strip()
    try:
        hub.code.sync_code_files(project_root, paths=[canonical])
    except Exception:
        pass
    # Invalidate config caches when config files are edited
    if canonical.endswith(".toml") or canonical.endswith("workflow-actions.json"):
        try:
            from .config import reload_config_caches
            reload_config_caches()
        except Exception:
            pass


def _require_indexed_read_gate(
    hub: AidocsServiceHub,
    project_root: Path,
    exact_path: str | None = None,
    known_exact_path: bool = False,
) -> dict[str, Any] | None:
    """Check read gate via AccessGate — blocks undiscovered files."""
    from .access_gate import AccessGate, GateContext

    managed = hub.managed_mode.get_mode(project_root)
    session_id = managed.get("session_id") if isinstance(managed, dict) else None
    if not managed.get("active") or not session_id:
        return None
    state = hub.query_gate.get(project_root, str(session_id))
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
    )
    if decision.allowed:
        return None
    return {
        "error": render_interaction_text("interaction.errors.indexed_read_gate"),
    }


def _apply_trace_depth(
    payload: dict[str, Any], mode: str, max_depth: int | None
) -> dict[str, Any]:
    if not max_depth or max_depth <= 0:
        return payload
    m = mode.strip().lower()
    if m in {"service", "component", "field_flow", "setting"} and isinstance(
        payload.get("matches"), list
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


async def _run_git(cwd: str, *args: str, timeout: int = _GIT_TIMEOUT) -> str:
    """Run a git command from inside an async context by offloading to a thread."""
    import asyncio
    from functools import partial

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, partial(_run_git_sync, cwd, *args, timeout=timeout)
    )




def _resolve_templates_root() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "core" / ".MEMORY" / ".aidocs" / "templates"


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
    state: dict[str, Any] | None, override_store: Any = None
) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    triggered = state.get("triggered")
    selected_skills = [
        str(item) for item in state.get("selected_skills", []) if str(item).strip()
    ]
    active_skills = [
        str(item) for item in state.get("active_skills", []) if str(item).strip()
    ]
    provider_states = (
        state.get("provider_states")
        if isinstance(state.get("provider_states"), dict)
        else {}
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
            runtime_provider = (
                str(item.get("runtime_provider") or provider).strip() or provider
            )
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
                }
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
            }
        )
    if not active_skill_modes and not selected_skill_modes:
        return None
    return {
        "active_skill_modes": active_skill_modes,
        "selected_skill_modes": selected_skill_modes,
        "decisions": decisions,
    }


def _annotate_imported_skill_state(
    imported_skill_state: Any, override_store: Any = None
) -> Any:
    if not isinstance(imported_skill_state, dict):
        return imported_skill_state
    mode_metadata = _build_skill_mode_metadata(
        imported_skill_state, override_store=override_store
    )
    if mode_metadata is None:
        return imported_skill_state
    return {
        **imported_skill_state,
        "mode_metadata": mode_metadata,
    }


def _annotate_skill_result(
    payload: dict[str, Any], override_store: Any = None
) -> dict[str, Any]:
    result = dict(payload)
    mode_metadata = _build_skill_mode_metadata(result, override_store=override_store)
    if mode_metadata is not None:
        result["override_modes"] = dict(mode_metadata["active_skill_modes"])
    imported_skill_state = result.get("imported_skill_state")
    if isinstance(imported_skill_state, dict):
        result["imported_skill_state"] = _annotate_imported_skill_state(
            imported_skill_state, override_store=override_store
        )
        if "override_modes" not in result and isinstance(
            result["imported_skill_state"], dict
        ):
            imported_mode_metadata = result["imported_skill_state"].get("mode_metadata")
            if isinstance(imported_mode_metadata, dict):
                result["override_modes"] = dict(
                    imported_mode_metadata.get("active_skill_modes") or {}
                )
    return result


def _build_server_instructions() -> str:
    """Load MCP server instructions from action_hooks TOML config."""
    return render_interaction_text("interaction.mcp_server.instructions")


def create_server() -> Any:
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError(
            "FastMCP is not installed. Install the MCP package dependencies before running the server."
        ) from exc

    hub = AidocsServiceHub(
        templates_root=_resolve_templates_root(), script_root=_resolve_script_root()
    )
    runtime = RuntimeService(hub)
    server = FastMCP("AIDOCS MCP", instructions=_build_server_instructions())
    server._aidocs_test_hub = hub  # test access only

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

    def _taxonomy_tool(*args: Any, **kwargs: Any) -> Any:
        explicit_name = kwargs.pop("name", None)
        eager = kwargs.pop("eager", None)  # explicit override

        def decorator(func: Any) -> Any:
            tool_name = explicit_name or func.__name__
            if tool_name.startswith("aidocs_"):
                tool_name = tool_name.removeprefix("aidocs_")
            # Override docstring from TOML if available
            toml_desc = _tool_descriptions.get(tool_name)
            if toml_desc and func.__doc__:
                func.__doc__ = toml_desc
            # Track deferred tools using agent-agnostic tier classification
            is_eager_val = eager if eager is not None else _is_eager(tool_name)
            if not is_eager_val:
                _deferred_tool_names.add(tool_name)
            return raw_server_tool(*args, name=tool_name, **kwargs)(func)

        return decorator

    server.tool = _taxonomy_tool

    # Store tier metadata for introspection
    server._aidocs_deferred_tools = _deferred_tool_names

    def _timestamp() -> str:
        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    project_registry = ProjectRegistryService()

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

        managed = hub.managed_mode.get_mode(project_root)
        session_id = str(managed.get("session_id") or "").strip() or None
        project_registry.record_project(
            project_root,
            managed_session_id=session_id,
            title=project_root.name,
        )
        run_id = f"mcp-{uuid4()}"
        args_bytes = len(json.dumps(arguments, default=str)) if isinstance(arguments, dict) else 0
        # Build rich payload for tool call tracking
        args_str = json.dumps(arguments, default=str) if isinstance(arguments, dict) else "{}"
        args_bytes = len(args_str.encode("utf-8"))
        args_preview = args_str[:500] if len(args_str) > 500 else args_str
        # Determine host/agent identity for usage tracking
        import os as _os
        _host_id = _os.environ.get("CLAUDE_CODE_VERSION", "") and "claude_code" or _os.environ.get("OPENCODE_VERSION", "") and "opencode" or "unknown"
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
                payload_summary["line_range"] = f"{arguments.get('start_line')}-{arguments.get('end_line', '?')}"
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
        hub.execution.record_run(
            project_root,
            run_kind="mcp_tool_invocation",
            source_kind="mcp_call",
            session_id=session_id,
            capability_name=name,
            status="started",
            ad_hoc=True,
            metadata=payload_summary,
            run_id=run_id,
        )
        hub.execution.record_event(
            project_root,
            event_kind="tool_call_started",
            source_kind="mcp_call",
            session_id=session_id,
            capability_name=name,
            action_kind="mcp_tool_call",
            status="started",
            payload=payload_summary,
            run_id=run_id,
        )
        # ── Lane tool enforcement: block tools not in lane's allowed list ──
        if session_id:
            _lane_gate_state = hub.query_gate.get(project_root, session_id)
            if _lane_gate_state.get("current_lane_id"):
                from .access_gate import AccessGate, GateContext
                _lane_decision = AccessGate.check_lane_tool(
                    GateContext(
                        managed=True, session_id=session_id,
                        dev_mode=False, allow_config_edit=False,
                        gate_enforce=True, gate_state=_lane_gate_state,
                    ),
                    name,
                )
                if not _lane_decision.allowed:
                    raise RuntimeError(_lane_decision.reason or f"Tool '{name}' blocked by lane policy.")

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
            hub.execution.record_run(
                project_root,
                run_kind="mcp_tool_invocation",
                source_kind="mcp_call",
                session_id=session_id,
                capability_name=name,
                status="failed",
                ad_hoc=True,
                metadata={**payload_summary, "error_type": type(exc).__name__},
                run_id=run_id,
                completed_at=_timestamp(),
            )
            hub.execution.record_event(
                project_root,
                event_kind="tool_call_failed",
                source_kind="mcp_call",
                session_id=session_id,
                capability_name=name,
                action_kind="mcp_tool_call",
                status="failed",
                payload={**payload_summary, "error_type": type(exc).__name__},
                run_id=run_id,
            )
            from .metrics import get_collector as _get_metrics_err
            _get_metrics_err().record_tool_call(tool_name=name, status="failed", session_id=session_id)
            if _external_mcp:
                _breaker.record_failure(_server_id)
            raise

        # Circuit breaker: record success for external MCP tools
        if _external_mcp:
            _breaker.record_success(_server_id)

        # ── Output Guard: scan tool result for credentials/injections ──
        guard_summary: dict[str, object] | None = None
        from .config import OUTPUT_GUARD_ENABLED, OUTPUT_GUARD_REDACT
        from .output_guard import scan_tool_result as _guard_scan, GuardResult as _GuardResult
        if OUTPUT_GUARD_ENABLED:
            guard_result = _guard_scan(result, redact=OUTPUT_GUARD_REDACT)
            if guard_result.scanned and not guard_result.clean:
                guard_summary = guard_result.summary()
                payload_summary["output_guard"] = guard_summary
                # Record output guard findings as execution event
                hub.execution.record_event(
                    project_root,
                    event_kind="output_guard_finding",
                    source_kind="output_guard",
                    session_id=session_id,
                    capability_name=name,
                    action_kind="security",
                    status="redacted" if guard_result.redaction_count > 0 else "flagged",
                    payload={
                        "finding_count": len(guard_result.findings),
                        "redaction_count": guard_result.redaction_count,
                        "categories": list({f.category for f in guard_result.findings}),
                        "max_severity": max((f.severity for f in guard_result.findings), key=lambda s: {"info": 0, "warning": 1, "critical": 2}.get(s, 0), default="info"),
                    },
                )
        else:
            guard_result = _GuardResult(scanned=False)

        # ── Metrics: record tool call ──
        from .metrics import get_collector as _get_metrics
        _metrics = _get_metrics()

        result_summary = _summarize_tool_result(result)
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
        tokens_in_estimate = max(1, result_bytes // 4)
        tokens_out_estimate = max(1, len(str(arguments).encode("utf-8")) // 4)
        payload_summary["tokens_in_estimate"] = tokens_in_estimate
        payload_summary["result_preview"] = result_text_preview

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
                    {"category": f.category, "severity": f.severity}
                    for f in guard_result.findings
                ],
            )

        hub.execution.record_run(
            project_root,
            run_kind="mcp_tool_invocation",
            source_kind="mcp_call",
            session_id=session_id,
            capability_name=name,
            status="completed",
            ad_hoc=True,
            metadata={**payload_summary, "result_summary": result_summary},
            run_id=run_id,
            completed_at=_timestamp(),
        )
        hub.execution.record_event(
            project_root,
            event_kind="tool_call_completed",
            source_kind="mcp_call",
            session_id=session_id,
            capability_name=name,
            action_kind="mcp_tool_call",
            status="completed",
            payload={**payload_summary, "result_summary": result_summary},
            run_id=run_id,
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
    )
    register_code_edit_tools(
        server=server,
        hub=hub,
        require_indexed_read_gate=_require_indexed_read_gate,
        post_edit_reindex_and_grant=_post_edit_reindex_and_grant,
        file_get_lines=_file_get_lines,
        file_create_file=_file_create_file,
        file_edit_lines=_file_edit_lines,
        file_batch_edit=_file_batch_edit,
        file_str_replace=_file_str_replace,
        file_batch_str_replace=_file_batch_str_replace,
        available_config_edit_modes=available_config_edit_modes,
        self_edit_available_in_profile=self_edit_available_in_profile,
    )

    register_code_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_tool=timed_tool,
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
    
    register_legacy_git_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        resolve_related_root=lambda root, name: _resolve_related_root(hub, root, name),
        timed_git_async=timed_git_async,
        run_git_async=_run_git,
        git_timeout=_GIT_TIMEOUT,
    )

    # ── Metrics + Output Guard tools ──

    @server.tool()
    async def metrics_snapshot() -> str:
        """Return current MCP server metrics (token usage, tool calls, output guard stats)."""
        from .metrics import get_collector
        return json.dumps(get_collector().snapshot(), indent=2)

    @server.tool()
    async def metrics_prometheus() -> str:
        """Return metrics in Prometheus text exposition format for /metrics scraping."""
        from .metrics import get_collector
        return get_collector().render_prometheus()

    # ── MCP Registry Browser tools ──

    @server.tool()
    async def mcp_registry_search(query: str = "", limit: int = 20) -> str:
        """Search the official MCP server registry. Returns matching servers with install commands."""
        from .mcp_registry import search_servers
        try:
            result = search_servers(query, limit=limit)
            return json.dumps({
                "servers": [s.to_dict() for s in result.servers],
                "total_count": result.total_count,
                "next_cursor": result.next_cursor,
                "install_commands": {s.name: s.install_commands() for s in result.servers},
            }, indent=2)
        except (ConnectionError, ValueError) as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def mcp_registry_get(name: str) -> str:
        """Get details for a specific MCP server from the registry."""
        from .mcp_registry import get_server
        try:
            server_info = get_server(name)
            if server_info is None:
                return json.dumps({"error": f"Server '{name}' not found in registry."})
            return json.dumps({
                **server_info.to_dict(),
                "install_commands": server_info.install_commands(),
            }, indent=2)
        except (ConnectionError, ValueError) as exc:
            return json.dumps({"error": str(exc)})

    # ── Circuit Breaker tools ──

    @server.tool()
    async def circuit_breaker_status() -> str:
        """Show circuit breaker states for all tracked MCP servers."""
        from .circuit_breaker import get_breaker
        return json.dumps({"breakers": get_breaker().get_all_states()}, indent=2)

    @server.tool()
    async def circuit_breaker_reset(server_id: str) -> str:
        """Manually reset a circuit breaker for an MCP server."""
        from .circuit_breaker import get_breaker
        get_breaker().reset(server_id)
        return json.dumps({"reset": server_id, "state": get_breaker().get_state(server_id)})

    # ── Skill Scanner + Context Compaction tools ──

    @server.tool()
    async def skill_scan(skill_id: str, content: str) -> str:
        """Scan skill content for security risks (prompt injection, supply chain, capabilities)."""
        from .skill_scanner import scan_skill
        result = scan_skill(skill_id, content)
        return json.dumps(result.summary(), indent=2)

    @server.tool()
    async def context_budget_check(root: str = "", session_id: str = "") -> str:
        """Check context budget for a session — journal size, estimated tokens, recommendations."""
        from .context_compaction import check_context_budget
        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)
        session_path = project_root / ".MEMORY" / "sessions" / sid
        result = check_context_budget(session_path, sid)
        return json.dumps(result.to_dict(), indent=2)

    @server.tool()
    async def context_compact(root: str = "", session_id: str = "", keep_recent: int = 10) -> str:
        """Compact session context — extract key decisions, prune old journal entries. Resets token counters (new context window)."""
        from .context_compaction import compact_session_context
        project_root = _project_root(root)
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
        return json.dumps(result_dict, indent=2)

    # ── Edit History / Rollback tools ──

    @server.tool()
    async def edit_history_list(root: str = "", file_path: str = "", session_id: str = "", limit: int = 20) -> str:
        """List recent file edits for rollback. Optionally filter by file or session."""
        from .edit_history import EditHistoryStore
        project_root = _project_root(root)
        store = EditHistoryStore()
        edits = store.list_edits(
            project_root,
            file_path=file_path or None,
            session_id=session_id or None,
            limit=limit,
        )
        return json.dumps({"edits": [e.to_dict() for e in edits]}, indent=2)

    @server.tool()
    async def files_touched(root: str = "", session_id: str = "") -> str:
        """Summary of all files modified in this session — who edited what, how many times."""
        from .edit_history import EditHistoryStore
        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)
        summary = EditHistoryStore().files_touched_summary(project_root, session_id=sid or None)
        return json.dumps({"files": summary, "total": len(summary)}, indent=2)

    # ── Semantic Search tools ──

    @server.tool()
    async def semantic_search(query: str, limit: int = 10, root: str = "") -> str:
        """Search code by meaning, not just keywords. Finds 'authentication flow' even if code doesn't contain those exact words.
        
        Requires sentence-transformers: pip install sentence-transformers
        Run semantic_index_sync first to build the index.
        """
        from .semantic_search import search
        results = search(_project_root(root), query, limit=limit)
        if not results:
            return json.dumps({"results": [], "hint": "No results. Run semantic_index_sync to build the index, or install sentence-transformers."})
        return json.dumps({"results": results, "total": len(results)}, indent=2)

    @server.tool()
    async def semantic_index_sync(root: str = "", max_files: int = 500) -> str:
        """Build semantic search index from code files. Embeds file contents for meaning-based search."""
        from .semantic_search import sync_from_code_index
        return json.dumps(sync_from_code_index(_project_root(root), max_files=max_files), indent=2)

    @server.tool()
    async def semantic_index_status(root: str = "") -> str:
        """Check semantic search index status — model availability, indexed files/chunks."""
        from .semantic_search import index_status
        return json.dumps(index_status(_project_root(root)), indent=2)

    @server.tool()
    async def edit_rollback(root: str = "", edit_id: str = "") -> str:
        """Rollback a specific edit — restore the file to its state before the edit."""
        from .edit_history import EditHistoryStore
        if not edit_id:
            return json.dumps({"success": False, "message": "edit_id is required."})
        project_root = _project_root(root)
        result = EditHistoryStore().rollback(project_root, edit_id)
        return json.dumps(result.to_dict(), indent=2)


    # ── Code Runner tools (bash replacements) ──

    @server.tool()
    async def code_build_project(root: str = "", command: str = "", timeout: int = 120) -> str:
        """Run build command, return success/fail + errors only. Auto-detects build system."""
        from .code_runner import code_build
        result = code_build(_project_root(root), command, timeout=timeout)
        return json.dumps(result.to_dict(), indent=2)

    @server.tool()
    async def code_test_project(root: str = "", command: str = "", timeout: int = 120) -> str:
        """Run test suite, return pass/fail counts + failure details. Auto-detects test framework."""
        from .code_runner import code_test
        result = code_test(_project_root(root), command, timeout=timeout)
        return json.dumps(result.to_dict(), indent=2)

    @server.tool()
    async def code_run_command(root: str = "", command: str = "", timeout: int = 60, max_output: int = 4000) -> str:
        """Run command with capped output. Use instead of raw bash for token efficiency."""
        from .code_runner import code_run
        if not command:
            return json.dumps({"success": False, "error": "command is required"})
        result = code_run(_project_root(root), command, timeout=timeout, max_output=max_output)
        return json.dumps(result.to_dict(), indent=2)

    # ── Git operations tool ──

    @server.tool(eager=True)
    def _is_workflow_action_satisfied(project_root: Path, action: dict) -> bool:
        action_id = action.get("id", "")
        if not action_id:
            return False
        return hub.workflow.is_action_satisfied(project_root, action_id)

    @server.tool()
    async def workflow_action_satisfy(action_id: str, evidence: str, root: str = "") -> str:
        """Mark a workflow action as completed with evidence. Call after doing the required work.
        
        The evidence is logged for audit. Example:
          workflow_action_satisfy("rule-01-01-before_git_commit-advisory", "Updated README.md with v2.2.0b changes")
        """
        project_root = _project_root(root)
        result = hub.workflow.satisfy_action(project_root, action_id, evidence)
        return json.dumps(result, indent=2)

    @server.tool(eager=True)
    async def git_ops(root: str = "", op: str = "status", message: str = "", count: int = 10, branch: str = "") -> str:
        """Basic git operations. op: status, log, diff, add, commit, push, pull, branch, stash.

        Examples:
          git_ops(op="status")
          git_ops(op="log", count=5)
          git_ops(op="diff")
          git_ops(op="commit", message="fix: bug")
          git_ops(op="push")
          git_ops(op="pull")
          git_ops(op="branch")
          git_ops(op="stash")
        """
        from .code_runner import code_run
        project_root = _project_root(root)
        o = op.strip().lower()

        cmd_map = {
            "status": "git status --short",
            "log": f"git log --oneline -{count}",
            "diff": "git diff --stat",
            "diff_staged": "git diff --cached --stat",
            "add": "git add -A",
            "commit": f"git commit -m \"{message}\"" if message else "echo 'message required for commit'",
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
            return json.dumps({"error": f"Unknown op: {op}. Available: {', '.join(sorted(cmd_map.keys()))}"})

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
                unsatisfied = [a for a in pending if not _is_workflow_action_satisfied(project_root, a)]
                if unsatisfied:
                    actions_text = "; ".join(str(a.get("source_segment") or a.get("kind", "action")) for a in unsatisfied)
                    return json.dumps({
                        "op": o,
                        "blocked": True,
                        "reason": f"Workflow rule requires: {actions_text} — complete these before {o}. Then call workflow_action_satisfy(action_id, evidence) to proceed.",
                        "pending_actions": unsatisfied,
                    }, indent=2)

        result = code_run(project_root, cmd, timeout=30)
        return json.dumps({
            "op": o,
            "success": result.success,
            "exit_code": result.exit_code,
            "output": result.stdout_preview or result.stderr_preview,
            "duration": result.duration_seconds,
        }, indent=2)

    # ── Conductor process management ──

    _conductor_process: dict[str, Any] = {}  # shared state for conductor process
    _conductor_output: list[dict[str, Any]] = []  # ring buffer of output lines
    _conductor_output_lock = __import__("threading").Lock()
    _MAX_CONDUCTOR_OUTPUT = 500  # keep last 500 lines

    def _start_output_reader(proc: Any, output_buf: list, lock: Any, max_lines: int) -> None:
        """Background thread that reads conductor stdout and stores in ring buffer."""
        import threading
        import time as _time

        def _reader():
            try:
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    entry = {
                        "text": line.rstrip("\n\r"),
                        "timestamp": _time.time(),
                        "stream": "stdout",
                    }
                    with lock:
                        output_buf.append(entry)
                        if len(output_buf) > max_lines:
                            del output_buf[:len(output_buf) - max_lines]
            except (ValueError, OSError):
                pass  # pipe closed

        def _err_reader():
            try:
                for line in iter(proc.stderr.readline, ""):
                    if not line:
                        break
                    entry = {
                        "text": line.rstrip("\n\r"),
                        "timestamp": _time.time(),
                        "stream": "stderr",
                    }
                    with lock:
                        output_buf.append(entry)
                        if len(output_buf) > max_lines:
                            del output_buf[:len(output_buf) - max_lines]
            except (ValueError, OSError):
                pass

        """Start a persistent long-lived conductor agent for a session.
        
        Backends:
          claude  — interactive Claude Code CLI (stdin/stdout)
          codex   — interactive Codex CLI (stdin/stdout)  
          opencode — OpenCode serve mode (HTTP localhost)
        """
        t2 = threading.Thread(target=_err_reader, daemon=True)
        t1.start()
        t2.start()

    @server.tool()
    async def conductor_start(root: str = "", session_id: str = "", backend: str = "claude", model: str = "") -> str:
        """Start a persistent long-lived conductor agent for a session.
        
        cli_name = {"claude": "claude", "codex": "codex", "opencode": "opencode"}.get(backend)
        if not cli_name:
            return json.dumps({"started": False, "reason": f"Unknown backend: {backend}. Use 'claude', 'codex', or 'opencode'."})
        via conductor_send. It manages lane agents, resolves conflicts,
        and reports progress. Stop with conductor_stop.
        """
        import shutil
        import subprocess

        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)

        # Check for existing conductor
        existing = _conductor_process.get("process")
        if existing and existing.poll() is None:
            return json.dumps({"started": False, "reason": "Conductor already running.", "backend": _conductor_process.get("backend")})

        # Claim session
        from .agent_orchestrator import AgentOrchestrator
        orch = AgentOrchestrator(runtime)
        claim = orch.conductor_claim(project_root, sid, f"conductor-{backend}")
        if not claim.get("claimed"):
            return json.dumps({"started": False, **claim})

        # cli_name already resolved above
        cli_path = shutil.which(cli_name)
        if not cli_path:
            return json.dumps({"started": False, "reason": f"{cli_name} CLI not found."})

        # Build rich context briefing from session state
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
            "2. You analyze the codebase using AIDOCS tools (code_investigate, code_find, code_bundle)",
            "3. You decide: do it yourself (inline) or dispatch to lane agents (parallel)",
            "4. For lane agents: use conductor_lane_control + conductor_guidance to manage them",
            "5. Monitor with conductor_overview — see all lanes, pending questions, activity",
            "6. When done: report what changed, what was tested, what needs attention",
            "7. Wait for the next task — don't exit",
            "",
            "== TOOLS ==",
            "Orchestration: conductor_overview, conductor_lane_control, conductor_guidance, conductor_answer, conductor_ask",
            "Planning: plan_conductor_status, plan_dispatch_next, plan_dispatch_report",
            "Code: code_investigate, code_find, code_trace, code_bundle, code_get_lines",
            "Edit: code_edit_lines, code_str_replace, code_create_file",
            "Session: session_journal_log, task_begin, task_update, task_complete",
            "",
            "== RULES ==",
            "- Always use AIDOCS indexed tools before reading files",
            "- Log significant decisions to session journal",
            "- When dispatching lanes: set clear scope (allowed files), verify results",
            "- When stuck: ask the user via the dashboard (they see conductor_ask messages)",
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
                    files_list = "\n".join(f"  {f['file']} ({f['edits']} edits, agents: {','.join(f['agents']) or 'unknown'})" for f in touched[:20])
                    context_parts.extend(["", "== FILES MODIFIED THIS SESSION ==", files_list])
            except Exception:
                pass
        except Exception:
            pass

        initial_prompt = "\n".join(context_parts)

        try:
            model_flag = model.strip() if model else ""
            if backend == "claude":
                cmd_args = [cli_path, "--output-format", "text"]
                if model_flag:
                    cmd_args.extend(["--model", model_flag])
                child = subprocess.Popen(cmd_args, cwd=str(project_root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            elif backend == "opencode":
                import random
                oc_port = random.randint(10000, 60000)
                child = subprocess.Popen([cli_path, "serve", "--port", str(oc_port)], cwd=str(project_root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                _conductor_process["opencode_port"] = oc_port
            else:  # codex
                cmd_args = [cli_path]
                if model_flag:
                    cmd_args.extend(["-m", model_flag])
                child = subprocess.Popen(cmd_args, cwd=str(project_root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            _conductor_process["process"] = child
            _conductor_process["backend"] = backend
            _conductor_process["session_id"] = sid
            _conductor_process["project_root"] = str(project_root)

            # Start background output reader
            with _conductor_output_lock:
                _conductor_output.clear()
            _start_output_reader(child, _conductor_output, _conductor_output_lock, _MAX_CONDUCTOR_OUTPUT)

            # Send initial context as first message
            try:
                child.stdin.write(initial_prompt + "\n")
                child.stdin.flush()
            except Exception:
                pass

            return json.dumps({"started": True, "backend": backend, "session_id": sid, "pid": child.pid, "mode": "interactive"})
        except Exception as exc:
            return json.dumps({"started": False, "reason": str(exc)})

    @server.tool()
    async def conductor_send(message: str) -> str:
        """Send a message/command to the running conductor agent."""
        proc = _conductor_process.get("process")
        if not proc or proc.poll() is not None:
            _conductor_process.clear()
            return json.dumps({"sent": False, "reason": "No conductor running."})

        backend = _conductor_process.get("backend", "claude")

        # OpenCode: send via `opencode run --attach`
        if backend == "opencode":
            import shutil
            oc_port = _conductor_process.get("opencode_port")
            oc_cli = shutil.which("opencode")
            if not oc_port or not oc_cli:
                return json.dumps({"sent": False, "reason": "OpenCode port/CLI not available"})
            try:
                result = __import__("subprocess").run(
                    [oc_cli, "run", "--attach", f"http://localhost:{oc_port}", message],
                    cwd=_conductor_process.get("project_root", "."),
                    capture_output=True, text=True, timeout=300,
                )
                return json.dumps({"sent": True, "message": message[:200], "output": result.stdout[:500]})
            except Exception as exc:
                return json.dumps({"sent": False, "reason": str(exc)})

        # Claude/Codex: send via stdin
        try:
            proc.stdin.write(message + "\n")
            proc.stdin.flush()
            return json.dumps({"sent": True, "message": message[:200]})
        except (BrokenPipeError, OSError) as exc:
            return json.dumps({"sent": False, "reason": str(exc)})

    @server.tool()
    async def conductor_status() -> str:
        """Check if the conductor agent is running."""
        proc = _conductor_process.get("process")
        if not proc:
            return json.dumps({"running": False})
        if proc.poll() is not None:
            _conductor_process.clear()
            return json.dumps({"running": False, "exit_code": proc.returncode})
        return json.dumps({
            "running": True,
            "backend": _conductor_process.get("backend"),
            "session_id": _conductor_process.get("session_id"),
            "pid": proc.pid,
        })

    @server.tool()
    async def conductor_stop() -> str:
        """Stop the running conductor agent and release session claim."""
        proc = _conductor_process.get("process")
        if not proc:
            return json.dumps({"stopped": False, "reason": "No conductor running."})

        sid = _conductor_process.get("session_id", "")
        backend = _conductor_process.get("backend", "")
        project_root_str = _conductor_process.get("project_root", "")

        # Graceful stop
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        _conductor_process.clear()

        # Release session claim
        if sid and project_root_str:
            try:
                from .agent_orchestrator import AgentOrchestrator
                orch = AgentOrchestrator(runtime)
                orch.conductor_release(Path(project_root_str), sid, f"conductor-{backend}")
            except Exception:
                pass

        return json.dumps({"stopped": True, "session_id": sid})

    # ── Conductor communication tools ──

    @server.tool()
    async def conductor_ask(
        question: str, lane_id: str = "default", wait: bool = False,
        timeout: int = 120, category: str = "question",
        requested_path: str = "",
        root: str = "", session_id: str = "",
    ) -> str:
        """Ask the conductor/operator a question. If wait=True, blocks until answered or timeout.
        
        For scope requests: set category='scope_request' and requested_path='path/to/file'.
        Auto-resolves if no lane conflict exists (no conductor intervention needed).
        """
        from .conductor_comms import agent_ask, auto_resolve_scope_request
        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)

        # Auto-resolve scope requests when possible
        if category == "scope_request" and requested_path:
            result = agent_ask(
                project_root, lane_id, question,
                category=category, session_id=sid, wait=False,
            )
            msg_id = result.get("id", "")
            if msg_id:
                auto = auto_resolve_scope_request(project_root, msg_id, lane_id, requested_path, session_id=sid)
                if auto.get("auto_resolved"):
                    return json.dumps({
                        "id": msg_id, "status": "answered",
                        "response": f"Auto-approved: '{requested_path}' added to your scope.",
                        "auto_resolved": True,
                    }, indent=2)
            # Conflict or error — fall through to normal flow

        result = agent_ask(
            project_root, lane_id, question,
            category=category, session_id=sid, wait=wait, timeout=float(timeout),
        )
        return json.dumps(result, indent=2)

    @server.tool()
    async def conductor_check_response(message_id: str, root: str = "") -> str:
        """Check if a previously submitted question has been answered."""
        from .conductor_comms import check_response
        return json.dumps(check_response(_project_root(root), message_id), indent=2)

    @server.tool()
    async def conductor_answer(message_id: str, response: str, root: str = "") -> str:
        """Answer an agent's pending question (called by conductor or dashboard)."""
        from .conductor_comms import answer_question
        return json.dumps(answer_question(_project_root(root), message_id, response), indent=2)

    @server.tool()
    async def conductor_guidance(
        lane_id: str, message: str, root: str = "", session_id: str = "",
    ) -> str:
        """Send guidance to a lane agent. Agent sees it on next tool call via hook injection."""
        from .conductor_comms import send_guidance
        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)
        return json.dumps(send_guidance(project_root, lane_id, message, session_id=sid), indent=2)

    @server.tool()
    async def conductor_pending_questions(root: str = "", session_id: str = "") -> str:
        """List all pending agent questions awaiting conductor/operator response."""
        from .conductor_comms import get_pending_questions
        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)
        return json.dumps({"questions": get_pending_questions(project_root, sid)}, indent=2)

    @server.tool()
    async def conductor_lane_control(
        lane_id: str, state: str = "active", reason: str = "",
        root: str = "", session_id: str = "",
    ) -> str:
        """Control a lane: set state to 'active', 'paused', or 'canceled'."""
        from .conductor_comms import set_lane_state
        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)
        return json.dumps(set_lane_state(project_root, lane_id, state, reason=reason, session_id=sid), indent=2)

    @server.tool()
    async def conductor_overview(root: str = "", session_id: str = "") -> str:
        """Full conductor situational awareness: all lanes, states, pending questions, recent activity. One call, full picture."""
        from .conductor_comms import get_all_lanes_status
        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)
        return json.dumps(get_all_lanes_status(project_root, sid), indent=2)

    @server.tool()
    async def conductor_auto_resolve_scope(
        message_id: str, lane_id: str, requested_path: str,
        root: str = "", session_id: str = "",
    ) -> str:
        """Auto-resolve a scope expansion request if no lane conflict exists. Approves and expands scope automatically, or flags conflict for manual resolution."""
        from .conductor_comms import auto_resolve_scope_request
        project_root = _project_root(root)
        sid = session_id or _resolve_session_id(hub, project_root)
        return json.dumps(auto_resolve_scope_request(project_root, message_id, lane_id, requested_path, session_id=sid), indent=2)

    @server.tool()
    async def conductor_message_history(
        lane_id: str = "", limit: int = 50, root: str = "",
    ) -> str:
        """Get conductor message history for a lane or all lanes."""
        from .conductor_comms import get_message_history
        return json.dumps({"messages": get_message_history(_project_root(root), lane_id, limit)}, indent=2)

    @server.tool()
    async def conductor_resolve_backend(task_type: str, root: str = "") -> str:
        """Resolve the best backend + model for a task type.
        
        Uses conductor.task_routing config to match task types to agents.
        Task types: refactor, implement, design, test, docs, research, debug, review, deploy.
        
        Example config in aidocs.toml:
          [conductor]
          task_routing = '{"refactor":"opencode/openai/gpt-4o","implement":"claude","test":"codex"}'
        """
        from .conductor_comms import resolve_backend_for_task
        result = resolve_backend_for_task(_project_root(root), task_type)
        return json.dumps(result, indent=2)

    @server.tool()
    async def conductor_output(since: float = 0, limit: int = 100) -> str:
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
        return json.dumps({
            "running": running,
            "lines": lines,
            "total_buffered": len(_conductor_output),
        }, indent=2)


    # ── Execution management tools ──

    @server.tool()
    async def execution_clear_token_usage(root: str = "", session_id: str = "") -> str:
        """Clear token usage data. Scoped to session if provided."""
        project_root = _project_root(root)
        sid = session_id.strip() or None
        count = hub.execution.clear_token_usage(project_root, session_id=sid)
        return json.dumps({"cleared": True, "runs_deleted": count, "session_id": sid})

    @server.tool()
    async def execution_clear_tool_calls(root: str = "", session_id: str = "") -> str:
        """Clear tool call events. Scoped to session if provided."""
        project_root = _project_root(root)
        sid = session_id.strip() or None
        result = hub.execution.clear_tool_calls(project_root, session_id=sid)
        return json.dumps({"cleared": True, **result, "session_id": sid})

    @server.tool()
    async def execution_prune(root: str = "", keep_days: int = 7, max_events: int = 0) -> str:
        """Prune old execution events. Use keep_days to delete by age, max_events to cap total count."""
        project_root = _project_root(root)
        result: dict[str, object] = {}
        if keep_days > 0:
            result["by_age"] = hub.execution.prune_old_events(project_root, keep_days=keep_days)
        if max_events > 0:
            result["by_size"] = hub.execution.prune_to_max_size(project_root, max_events=max_events)
        if not result:
            result = hub.execution.auto_prune(project_root)
        result["current_counts"] = hub.execution.event_count(project_root)
        return json.dumps(result, indent=2)

    @server.tool()
    async def execution_usage_by_identity(root: str = "") -> str:
        """Get token/tool usage broken down by host and agent identity."""
        project_root = _project_root(root)
        return json.dumps({
            "by_host": hub.execution.usage_by_host(project_root),
            "by_agent": hub.execution.usage_by_agent(project_root),
            "counts": hub.execution.event_count(project_root),
        }, indent=2)

    # Patch tool descriptions from TOML — sync, runs before server starts
    _patch_tool_descriptions_sync(server)

    return server


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


def main() -> None:
    import argparse
    import atexit
    import os

    parser = argparse.ArgumentParser(description="Run the AIDOCS MCP server.")
    parser.parse_known_args()

    # Shut down thread pool on exit; handle SIGTERM/SIGHUP for graceful stop (Windows lacks SIGHUP)
    def _cleanup():
        _tool_executor.shutdown(wait=False, cancel_futures=True)

    atexit.register(_cleanup)

    # On Unix, handle SIGHUP/SIGTERM for graceful shutdown
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))

    server = create_server()
    server.run()


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
