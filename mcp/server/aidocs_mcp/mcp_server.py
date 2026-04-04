from __future__ import annotations

import asyncio
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


def _grant_indexed_read_gate(
    hub: AidocsServiceHub, project_root: Path, tool_name: str
) -> None:
    """Legacy shim — blanket allow_read grants are removed.

    Existing _grant closures in tool functions call this. Now a no-op because
    AccessGate uses per-file discovery only. Tool-specific _grant closures that
    need per-file grants should call _grant_known_exact_path_read instead.
    """


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
    """After a successful edit: grant read access + reindex so indexed tools see the change."""
    _grant_known_exact_path_read(hub, project_root, tool_name, path)
    canonical = path.replace("\\", "/").strip()
    try:
        hub.code.sync_code_files(project_root, paths=[canonical])
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
        "path": str(summary.path),
        "title": summary.title,
        "status": summary.status,
        "owner": summary.owner,
        "goal": summary.goal,
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

    raw_server_tool = server.tool

    def _taxonomy_tool(*args: Any, **kwargs: Any) -> Any:
        explicit_name = kwargs.pop("name", None)

        def decorator(func: Any) -> Any:
            tool_name = explicit_name or func.__name__
            if tool_name.startswith("aidocs_"):
                tool_name = tool_name.removeprefix("aidocs_")
            return raw_server_tool(*args, name=tool_name, **kwargs)(func)

        return decorator

    server.tool = _taxonomy_tool

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
        payload_summary = {
            "tool_name": name,
            "argument_keys": sorted(arguments.keys())
            if isinstance(arguments, dict)
            else [],
        }
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
            raise

        result_summary = _summarize_tool_result(result)
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
        grant_indexed_read_gate=_grant_indexed_read_gate,
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
