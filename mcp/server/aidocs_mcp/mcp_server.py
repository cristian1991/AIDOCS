from __future__ import annotations

import asyncio
import functools
import re
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any, Literal
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
)
from .language_descriptors import (
    descriptor_match_summary,
    descriptor_registry_summary,
    descriptor_semantics_summary,
    validate_language_descriptors,
)
from .git_helpers import run_git_sync as _run_git_sync
from .project_registry_service import ProjectRegistryService
from .runtime_service import RuntimeService
from .server_plan_task_tools import register_plan_task_tools
from .server_session_tools import register_session_tools
from .server_skill_tools import register_skill_tools
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


def _find_git_root(project_root: str) -> Path:
    """Find the actual git root, walking up from project_root if needed.

    Raises RuntimeError if no git repository is found.
    """
    import subprocess

    root = Path(project_root)
    if not root.is_dir():
        raise RuntimeError(f"Directory does not exist: {project_root}")
    try:
        toplevel = _run_git_sync(str(root), "rev-parse", "--show-toplevel")
        if toplevel:
            return Path(toplevel)
    except FileNotFoundError:
        raise RuntimeError("git is not installed or not in PATH")
    except Exception:
        pass
    # Check one level down — common for monorepos (e.g., D:/Projects/OpenCode/opencode/)
    try:
        for child in root.iterdir():
            if child.is_dir() and (child / ".git").exists():
                return child
    except Exception:
        pass
    raise RuntimeError(f"No git repository found at or above: {project_root}")


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

    def _prefixed_public_tool_name(name: str) -> str:
        return name if name.startswith("aidocs_") else f"aidocs_{name}"

    def _taxonomy_tool(*args: Any, **kwargs: Any) -> Any:
        explicit_name = kwargs.pop("name", None)

        def decorator(func: Any) -> Any:
            return raw_server_tool(
                *args,
                name=_prefixed_public_tool_name(explicit_name or func.__name__),
                **kwargs,
            )(func)

        return decorator

    server.tool = _taxonomy_tool

    def _registered_tools() -> list[Any]:
        components = getattr(
            getattr(server, "_local_provider", None), "_components", {}
        )
        return [
            component
            for key, component in components.items()
            if str(key).startswith("tool:")
        ]

    def _timestamp() -> str:
        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _project_root_from_args(arguments: dict[str, Any] | None) -> Path | None:
        if not isinstance(arguments, dict):
            return None
        project_root = arguments.get("project_root")
        if not isinstance(project_root, str) or not project_root.strip():
            return None
        return Path(project_root)

    def _capture_enabled(
        name: str, run_middleware: bool, arguments: dict[str, Any] | None
    ) -> bool:
        if name in {"aidocs_execution_run_record", "aidocs_execution_event_record"}:
            return False
        return _project_root_from_args(arguments) is not None

    def _summarize_tool_result(result: Any) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "result_type": type(result).__name__,
        }
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            summary["structured_keys"] = sorted(str(key) for key in structured.keys())[
                :10
            ]
            result_value = structured.get("result")
            if isinstance(result_value, list):
                summary["result_length"] = len(result_value)
            elif isinstance(result_value, dict):
                summary["result_length"] = len(result_value)
            elif result_value is not None:
                summary["result_scalar_type"] = type(result_value).__name__
        content = getattr(result, "content", None)
        if isinstance(content, list):
            summary["content_items"] = len(content)
            summary["content_types"] = [type(item).__name__ for item in content[:5]]
        return summary

    def _all_capabilities(project_root: Path) -> list[dict[str, Any]]:
        return hub.capabilities.find_capabilities(project_root, query=None, limit=1000)

    def _all_procedures(project_root: Path) -> list[dict[str, Any]]:
        return hub.procedures.find_procedures(project_root, query=None, limit=1000)

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
        if not _capture_enabled(name, run_middleware, arguments):
            return await original_call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )

        project_root = _project_root_from_args(arguments)
        if project_root is None:
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

    def _resolve_related_root(project_root: str, name: str) -> Path:
        resolved = hub.related.resolve_related_project_path(Path(project_root), name)
        if resolved is None:
            raise FileNotFoundError(
                f"Related project '{name}' is not configured or its path does not exist."
            )
        return resolved

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

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read Session Journal",
        }
    )
    def session_journal_read(
        project_root: str,
        session_id: str,
        last_n: int | None = None,
    ) -> list[dict[str, str]]:
        """Read the session journal — a rolling log of significant decisions and outcomes.

        Use this to refresh your memory when resuming a stale session.

        Args:
            last_n: Only return the last N entries. None returns all.
        """
        return hub.sessions.read_journal(Path(project_root), session_id, last_n=last_n)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Log to Session Journal",
        }
    )
    def session_journal_log(
        project_root: str,
        session_id: str,
        action_kind: str,
        intent: str,
        outcome: str,
    ) -> dict[str, Any]:
        """Log a significant decision or outcome to the session journal.

        Only log meaningful work — not greetings, trivial commands, or minor edits.
        The journal auto-evicts oldest entries to archive when full (default: 100 entries).

        Args:
            action_kind: The type of action (edit, trace, investigate, read_error, etc.).
            intent: What the user asked for (1-2 sentences, max 120 chars).
            outcome: What happened (1-2 sentences, max 120 chars).
        """
        return hub.sessions.write_journal_entry(
            Path(project_root),
            session_id,
            action_kind=action_kind,
            intent=intent,
            outcome=outcome,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Runtime Preflight",
        }
    )
    def runtime_preflight(
        project_root: str,
        action_kind: str,
        session_id: str | None = None,
        user_explicit_targets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return host/runtime policy guidance before performing an action."""
        return hub.policy.preflight_action(
            Path(project_root),
            action_kind=action_kind,
            session_id=session_id,
            user_explicit_targets=user_explicit_targets,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Session",
        }
    )
    def session_update(
        project_root: str, session_id: str, patch: dict[str, list[str]]
    ) -> dict[str, Any]:
        """Update structured sections in an existing SESSION.md file."""
        session = hub.sessions.update_session(Path(project_root), session_id, patch)
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def memory_read(project_root: str, targets: list[str]) -> dict[str, str]:
        """Read canonical memory files by target path."""
        return hub.memory.read_memory(Path(project_root), targets)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Memory Index",
        }
    )
    def index_sync(project_root: str) -> dict[str, int]:
        """Rebuild the derived SQLite memory/session index from files."""
        return hub.index.sync_all(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Memory Index Status",
        }
    )
    def index_status(project_root: str) -> dict[str, Any]:
        """Report current derived index status for the project."""
        return hub.index.status(Path(project_root))

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Schema Index",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def schema_index_sync(
        project_root: str, timeout: int | None = None
    ) -> dict[str, int]:
        """Rebuild the derived schema catalog from code and SQL files."""
        return hub.schema.sync_schema(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Search Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def memory_search(
        project_root: str, query: str, limit: int = 10
    ) -> list[dict[str, str]]:
        """Search the derived memory index by path, title, or body text."""
        return hub.index.search_memory(Path(project_root), query=query, limit=limit)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Code Index",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def code_index_sync(
        project_root: str, include_tests: bool = False, timeout: int | None = None
    ) -> dict[str, int]:
        """Rebuild the derived code file manifest and summary index."""
        return {
            "code_files": hub.code.sync_code_files(
                Path(project_root), include_tests=include_tests
            ),
            "modules": hub.code.sync_modules(Path(project_root)),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Modules",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def code_get_modules(
        project_root: str, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """List detected project modules (workspaces, subprojects, informal modules).

        Detects formal workspaces (npm, Cargo, .csproj) and informal monorepo
        boundaries (directories with entry points or well-known module names).

        Args:
            kind: Filter by module kind ('workspace', 'subproject', 'project', 'module'). None returns all.
        """
        return hub.code.get_modules(Path(project_root), kind=kind)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Module Files",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def code_get_module_files(
        project_root: str, module_path: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """List all indexed source files belonging to a specific module.

        Args:
            module_path: The module's relative path (e.g., 'cli', 'server', 'src/Web').
        """
        return hub.code.get_module_files(
            Path(project_root), module_path=module_path, limit=limit
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Index Status",
        }
    )
    def code_index_status(project_root: str) -> dict[str, Any]:
        """Report current derived code index status for the project."""
        return hub.code.code_status(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Search",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_search(
        project_root: str, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search the derived code index by file path and lightweight summary."""
        root = Path(project_root)
        result = hub.code.search_code(root, query=query, limit=limit)
        if result:
            _grant_indexed_read_gate(hub, root, "code_search")
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Text Search",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_text_search(
        project_root: str,
        text: str,
        glob: str | None = None,
        case_sensitive: bool = False,
        limit: int = 50,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Fast literal text search across indexed files. Use instead of grep for 'does this string exist?'. Glob filters by file pattern (e.g. '*.py')."""
        root = Path(project_root)
        matches = hub.code.search_text(
            root,
            text,
            glob=glob,
            case_sensitive=case_sensitive,
            limit=limit,
            include_tests=include_tests,
        )
        if matches:
            _grant_known_exact_path_read(
                hub, root, "code_text_search",
                str(matches[0]["path"]),
            )
        return {"total_matches": len(matches), "results": matches}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Dependencies",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_dependencies(project_root: str, path: str) -> list[dict[str, str]]:
        """Return lightweight dependency edges for one indexed code file."""
        return hub.code.get_dependencies(Path(project_root), path=path)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Symbol Snippet",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_symbol_snippet(
        project_root: str,
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        """Return an exact code snippet for an indexed outline symbol."""
        root = Path(project_root)
        result = hub.code.get_symbol_snippet(
            root,
            path=path,
            symbol=symbol,
            kind=kind,
            line_number=line_number,
        )
        if result:
            _grant_known_exact_path_read(hub, root, "code_get_symbol_snippet", path)
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Method Signature",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_method_signature(
        project_root: str,
        method: str,
        container: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return exact method signatures so agents can call methods correctly without reading whole files."""
        root = Path(project_root)
        result = hub.code.get_method_signature(
            root, method_name=method, container=container, limit=limit
        )
        if result.get("matches"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Method Signature",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_method_signature(
        project_root: str,
        method: str,
        container: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return exact method signatures so agents can call methods correctly without reading whole files."""
        root = Path(project_root)
        result = hub.code.get_method_signature(
            root, method_name=method, container=container, limit=limit
        )
        if result.get("matches"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Method Signatures",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_method_signatures(
        project_root: str,
        methods: list[str],
        container: str | None = None,
        limit_per_method: int = 20,
    ) -> dict[str, Any]:
        """Return exact signatures for multiple methods in one call."""
        root = Path(project_root)
        result = hub.code.get_method_signatures(
            root,
            methods=methods,
            container=container,
            limit_per_method=limit_per_method,
        )
        if result.get("methods"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Enum Values",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_enum_values(
        project_root: str,
        enum_name: str,
        limit: int = 50,
        include_related: bool = False,
    ) -> dict[str, Any]:
        """Return indexed enum definitions with their enum members."""
        root = Path(project_root)
        result = hub.code.get_enum_values(
            root, enum_name=enum_name, limit=limit, include_related=include_related
        )
        if result.get("matches"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Constructor Params",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_constructor_params(
        project_root: str,
        type_name: str,
        limit: int = 20,
        include_related: bool = False,
    ) -> dict[str, Any]:
        """Return constructor or record positional parameter information for a type."""
        root = Path(project_root)
        result = hub.code.get_constructor_params(
            root, type_name=type_name, limit=limit, include_related=include_related
        )
        if result.get("matches"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Constructor Params Batch",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_constructor_params_batch(
        project_root: str,
        types: list[str],
        include_related: bool = False,
        limit_per_type: int = 20,
    ) -> dict[str, Any]:
        """Return constructor or record positional parameter information for multiple types."""
        root = Path(project_root)
        result = hub.code.get_constructor_params_batch(
            root,
            types=types,
            include_related=include_related,
            limit_per_type=limit_per_type,
        )
        if result.get("types"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Service API",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_service_api(
        project_root: str,
        service_name: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return all indexed public method signatures for a service-like class."""
        root = Path(project_root)
        result = hub.code.get_service_api(root, service_name=service_name, limit=limit)
        if result.get("methods"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Entity Properties",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_entity_properties(
        project_root: str,
        entity_name: str,
    ) -> dict[str, Any]:
        """Return a lightweight property list for an entity or DTO."""
        root = Path(project_root)
        result = hub.code.get_entity_properties(root, entity_name=entity_name)
        if result.get("entity_name") and (
            result.get("properties") or result.get("note")
        ):
            pass  # Precision tool - no blanket read grant
        return result

    # ── File operations (line-based read/edit with safety) ──

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Code Lines",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def code_get_lines(
        project_root: str,
        path: str,
        start_line: int = 1,
        count: int = 30,
        show_line_numbers: bool = True,
        known_exact_path: bool = False,
    ) -> dict[str, Any]:
        """Read specific lines from any file after indexed retrieval has established enough context.

        Fast line-based retrieval for any file type — Razor, HTML, TOML, config, etc.
        Use this only after indexed retrieval has narrowed the file and logic context enough for a surgical line pull.

        Args:
            path: Relative path to the file from project root.
            start_line: First line to read (1-indexed).
            count: Number of lines to read (default 30, max 200).
            show_line_numbers: Prefix each line with its number for easy reference.
            known_exact_path: Bypass the indexed-read gate only for an exact relative path.
        """
        root = Path(project_root)
        gate = _require_indexed_read_gate(
            hub,
            root,
            exact_path=path,
            known_exact_path=known_exact_path,
        )
        if gate:
            return gate
        return _file_get_lines(
            root,
            path,
            start_line=start_line,
            count=count,
            show_line_numbers=show_line_numbers,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Create File",
        }
    )
    def code_create_file(
        project_root: str,
        path: str,
        content: str,
        config_edit_mode: Literal["explicit_user_permitted"] | None = None,
    ) -> dict[str, Any]:
        """Create a new file at a relative path with exact content.

        The path must stay inside the project root and still respects the
        existing sensitive-path and self-edit guardrails.
        """
        root = Path(project_root)
        result = _file_create_file(
            root, path, content, config_edit_mode=config_edit_mode
        )
        if result.get("success"):
            _grant_known_exact_path_read(
                hub,
                root,
                "code_create_file",
                str(result.get("canonical_path") or result.get("path") or path),
            )
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Edit Lines",
        }
    )
    def code_edit_lines(
        project_root: str,
        path: str,
        start_line: int,
        end_line: int,
        new_content: str,
        expect: str | None = None,
        dry_run: bool = False,
        mode: str = "auto",
        config_edit_mode: Literal["explicit_user_permitted"] | None = None,
    ) -> dict[str, Any]:
        """Replace a range of lines with new content, with safety verification.

        Line-based editing that works for any file type. Returns old content for verification.
        Use `expect` for safe edits — the edit is rejected if current content doesn't match.
        Use `dry_run=True` to preview changes without writing.

        Set end_line < start_line to INSERT before start_line without removing lines.

        Args:
            path: Relative path to the file from project root.
            start_line: First line to replace (1-indexed, inclusive).
            end_line: Last line to replace (inclusive). Use < start_line for insert mode.
            new_content: Replacement text (can be multi-line).
            expect: If set, current content of the line range must match this or edit is rejected.
            dry_run: Preview changes without writing.
            mode: `auto`, `insert`, or `replace`.
        """
        root = Path(project_root)
        result = _file_edit_lines(
            root,
            path,
            start_line=start_line,
            end_line=end_line,
            new_content=new_content,
            expect=expect,
            dry_run=dry_run,
            mode=mode,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success") and not result.get("dry_run"):
            _grant_known_exact_path_read(
                hub,
                root,
                "code_edit_lines",
                str(result.get("canonical_path") or result.get("path") or path),
            )
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Batch Edit",
        }
    )
    def code_batch_edit(
        project_root: str,
        edits: list[dict[str, Any]],
        dry_run: bool = False,
        atomic: bool = True,
        config_edit_mode: Literal["explicit_user_permitted"] | None = None,
    ) -> dict[str, Any]:
        """Apply multiple line edits atomically across one or more files.

        Each edit: { "path": str, "start_line": int, "end_line": int, "new_content": str, "expect": str?, "mode": str? }

        If atomic=True (default), ALL edits are validated first. If any would fail, NONE are applied.
        Edits within the same file are applied bottom-up to preserve line numbers.
        Max 20 edits per call.

        Args:
            edits: List of edit operations.
            dry_run: Preview all changes without writing.
            atomic: All-or-nothing mode (default True).
        """
        root = Path(project_root)
        result = _file_batch_edit(
            root,
            edits,
            dry_run=dry_run,
            atomic=atomic,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success") and not dry_run:
            for item in result.get("results", []):
                if isinstance(item, dict) and item.get("success"):
                    _grant_known_exact_path_read(
                        hub,
                        root,
                        "code_batch_edit",
                        str(item.get("canonical_path") or item.get("path") or ""),
                    )
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "String Replace",
        }
    )
    def code_str_replace(
        project_root: str,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
        config_edit_mode: Literal["explicit_user_permitted"] | None = None,
    ) -> dict[str, Any]:
        """Quick string-match edit for small changes (old_str under 500 chars, must be unique in file). For large block replacements use code_edit_lines with line numbers."""
        root = Path(project_root)
        result = _file_str_replace(
            root,
            path,
            old_str,
            new_str,
            replace_all=replace_all,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            _grant_known_exact_path_read(
                hub,
                root,
                "code_str_replace",
                str(result.get("path") or path),
            )
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Batch String Replace",
        }
    )
    def code_batch_str_replace(
        project_root: str,
        edits: list[dict[str, Any]],
        atomic: bool = True,
        config_edit_mode: Literal["explicit_user_permitted"] | None = None,
    ) -> dict[str, Any]:
        """Multiple string-match replacements across files, atomic. Each edit: { "path": str, "old_str": str, "new_str": str, "replace_all": bool? }. Max 20 edits."""
        root = Path(project_root)
        result = _file_batch_str_replace(
            root,
            edits,
            atomic=atomic,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            for item in result.get("results", []):
                if isinstance(item, dict) and item.get("success"):
                    _grant_known_exact_path_read(
                        hub,
                        root,
                        "code_batch_str_replace",
                        str(item.get("path") or ""),
                    )
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Config Edit Policy",
        }
    )
    def config_edit_policy_get(
        profile: Literal["release"] = "release",
    ) -> dict[str, Any]:
        """Return the release-profile config edit policy visible to agents."""
        return {
            "profile": profile,
            "available_modes": available_config_edit_modes(profile),
            "security": {
                "self_edit_available": self_edit_available_in_profile(profile),
            },
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Investigate",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_tool
    def code_investigate(
        project_root: str,
        concept: str,
        limit: int = 5,
        depth: str = "standard",
        focus: str = "general",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """START HERE — investigate a concept, feature, or bug area.

        Returns a navigation guide: what was found across symbols, files, schema, CSS, and modules,
        plus which specific tools to call next with reasons why.

        Use this FIRST when you don't know where to start. It replaces guessing with Grep.

        Args:
            concept: The thing to investigate (e.g., "PDF generation", "authorization", "field-input", "Patient").
            depth: `shallow`, `standard`, or `deep`.
            focus: `general`, `workflow`, `service`, `schema`, `ui`, or `backend`.
        """
        root = Path(project_root)
        result = hub.code.investigate(
            root, concept=concept, limit=limit, depth=depth, focus=focus
        )
        if result.get("findings"):
            _grant_indexed_read_gate(hub, root, "code_investigate")
        findings = result.get("findings") or []
        next_tools = result.get("next_tools") or []
        compact = runtime.build_artifact_backed_result(
            root,
            inline_summary=(
                f"Investigation for `{concept}` found {len(findings)} finding(s) and {len(next_tools)} suggested next tool(s)."
            ),
            payload=result,
            artifact_name=f"code-investigate-{concept}",
            structured_summary={
                "concept": concept,
                "finding_count": len(findings),
                "next_tool_count": len(next_tools),
                "depth": depth,
                "focus": focus,
            },
        )
        result.update(compact)
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Unified Tools (v1.1.0) — prefer these over granular tools below
    # ═══════════════════════════════════════════════════════════════════════

    _FIND_MODES = {
        "symbols": "Search symbols by name, kind, or role",
        "references": "Find all usages of a symbol across the codebase",
        "routes": "Find API endpoints, page routes, controllers",
        "hotspots": "Find complex files (high symbol count, deep nesting)",
        "query_hotspots": "Find files with heavy DB query patterns",
        "entrypoints": "Find bootstrap, main, provider-like entry symbols",
        "duplicates": "Find structurally similar code across files",
        "partial_group": "Find all partial class files for a C# type",
        "partial_consumers": "Find pages/views referencing a Razor partial",
        "api_consumers": "Find pages/scripts calling an API endpoint",
        "frontend_symbols": "Find components, hooks, providers by name",
        "data_structures": "Find classes, records, enums with their members",
        "initializers": "Find DOMContentLoaded, document.ready, window.onload",
        "mutations": "Find create/update/delete flows for a concept",
        "validation": "Find validation logic, required fields, validators",
        "async": "Find async boundaries, deferred execution, Task patterns",
        "policy": "Find authorization, RBAC, permission checks",
        "touchpoints": "Find UI↔backend connection points for a concept",
        "mismatches": "Find state/model representation conflicts",
        "clusters": "Find cross-layer grouping for a domain concept",
        "transitions": "Find migration seams, adapters, compatibility layers",
        "factories": "Find Create* helpers, factory-style methods, and setup helpers",
    }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Find",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_tool
    def code_find(
        project_root: str,
        query: str,
        mode: str = "symbols",
        kind: str | None = None,
        role: str | None = None,
        include_tests: bool = False,
        limit: int = 50,
        timeout: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Unified find tool — replaces all code_find_* and code_search_* tools.

        Modes: symbols, references, routes, hotspots, query_hotspots, entrypoints,
        duplicates, partial_group, partial_consumers, api_consumers, frontend_symbols,
        data_structures, initializers, mutations, validation, async, policy,
        touchpoints, mismatches, clusters, transitions, factories.

        Args:
            query: What to find (symbol name, concept, endpoint, class name, etc.).
            mode: Which find mode to use (see above).
            kind: Filter by symbol kind (only for mode=symbols).
            role: Filter by file role (only for mode=symbols).
            include_tests: Include test/fixture files in search. Default False. Auto-enabled for mode=factories.
        """
        root = Path(project_root)
        m = mode.strip().lower()

        # Auto-enable include_tests for modes that commonly need test/fixture content
        if m in ("factories",) and not include_tests:
            include_tests = True

        # If include_tests requested, ensure test files are indexed
        if include_tests:
            hub.code.sync_code_files(root, include_tests=True)

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_indexed_read_gate(hub, root, "code_find")
                return result
            if any(result.get(key) for key in ("matches", "cluster")):
                _grant_indexed_read_gate(hub, root, "code_find")
            return result

        if m == "symbols":
            return _grant(
                hub.code.search_symbols(
                    root, query=query, kind=kind, role=role, limit=limit
                )
            )
        if m == "references":
            return _grant(hub.code.find_references(root, symbol=query, limit=limit))
        if m == "routes":
            return _grant(hub.code.find_routes(root, query=query, limit=limit))
        if m == "hotspots":
            return _grant(hub.code.find_hotspots(root, query=query, limit=limit))
        if m == "query_hotspots":
            return _grant(hub.code.find_query_hotspots(root, query=query, limit=limit))
        if m == "entrypoints":
            return _grant(hub.code.find_entrypoints(root, concept=query, limit=limit))
        if m == "duplicates":
            return _grant(
                hub.code.find_duplicate_structures(
                    root, role_filter=query or None, limit=limit
                )
            )
        if m == "partial_group":
            return _grant(hub.code.find_partial_group(root, symbol=query, limit=limit))
        if m == "partial_consumers":
            return _grant(
                hub.code.find_partial_consumers(root, partial_name=query, limit=limit)
            )
        if m == "api_consumers":
            return _grant(
                hub.code.find_api_consumers(root, endpoint=query, limit=limit)
            )
        if m == "frontend_symbols":
            return _grant(
                hub.code.find_frontend_symbols(root, query=query, limit=limit)
            )
        if m == "data_structures":
            return _grant(hub.code.find_data_structures(root, query=query, limit=limit))
        if m == "initializers":
            return _grant(
                hub.code.find_initializers(
                    root, path=query if query.strip() else None, limit=limit
                )
            )
        if m == "mutations":
            return _grant(
                hub.code.find_mutation_points(root, concept=query, limit=limit)
            )
        if m == "validation":
            return _grant(
                hub.code.find_validation_surfaces(root, concept=query, limit=limit)
            )
        if m == "async":
            return _grant(
                hub.code.find_async_boundaries(root, concept=query or None, limit=limit)
            )
        if m == "policy":
            return _grant(
                hub.code.find_policy_surfaces(root, concept=query, limit=limit)
            )
        if m == "touchpoints":
            return _grant(
                hub.code.find_ui_backend_touchpoints(root, concept=query, limit=limit)
            )
        if m == "mismatches":
            return _grant(
                hub.code.find_state_model_mismatch(root, concept=query, limit=limit)
            )
        if m == "clusters":
            return _grant(
                hub.code.find_domain_clusters(root, concept=query, limit=limit)
            )
        if m == "transitions":
            return _grant(
                hub.code.find_transition_points(root, concept=query, limit=limit)
            )
        if m == "factories":
            return _grant(hub.code.find_factories(root, query=query, limit=limit))
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(create_server._FIND_MODES.keys()),
        }

    _TRACE_MODES = {
        "field_flow": "Trace a field across model→service→UI layers",
        "service": "Find where a service is injected and consumed",
        "model": "Trace a DTO/entity through the full stack",
        "component": "Trace component imports and usage",
        "api_to_ui": "Trace from API endpoint through to UI",
        "css_class": "Find CSS definitions AND HTML/template usages",
        "query_shape": "Trace query patterns + schema relationships",
        "setting": "Trace a configuration setting across layers",
    }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Trace",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_tool
    def code_trace(
        project_root: str,
        query: str,
        mode: str = "field_flow",
        limit: int = 50,
        max_depth: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Unified trace tool — replaces all code_trace_* tools.

        Modes: field_flow, service, model, component, api_to_ui, css_class, query_shape, setting.

        Args:
            query: What to trace (field name, service name, component name, CSS class, etc.).
            mode: Which trace mode to use.
        """
        root = Path(project_root)
        m = mode.strip().lower()

        def _grant(result: dict[str, Any]) -> dict[str, Any]:
            if any(result.get(key) for key in ("matches", "api", "logic", "ui")):
                _grant_indexed_read_gate(hub, root, "code_trace")
            return result

        if m == "field_flow":
            return _grant(
                _apply_trace_depth(
                    hub.code.trace_field_flow(root, field_name=query, limit=limit),
                    m,
                    max_depth,
                )
            )
        if m == "service":
            return _grant(
                _apply_trace_depth(
                    hub.code.trace_service_usage(root, service_name=query, limit=limit),
                    m,
                    max_depth,
                )
            )
        if m == "model":
            return _grant(
                hub.code.trace_model_usage(root, model_name=query, limit=limit)
            )
        if m == "component":
            return _grant(
                _apply_trace_depth(
                    hub.code.trace_component_usage(
                        root, component_name=query, limit=limit
                    ),
                    m,
                    max_depth,
                )
            )
        if m == "api_to_ui":
            return _grant(
                _apply_trace_depth(
                    hub.code.trace_api_to_ui(root, concept=query, limit=limit),
                    m,
                    max_depth,
                )
            )
        if m == "css_class":
            return _grant(
                hub.code.trace_css_class_usage(root, class_name=query, limit=limit)
            )
        if m == "query_shape":
            return _grant(hub.code.trace_query_shape(root, path=query, limit=limit))
        if m == "setting":
            return _grant(
                _apply_trace_depth(
                    hub.code.trace_setting_usage(root, setting_name=query, limit=limit),
                    m,
                    max_depth,
                )
            )
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(create_server._TRACE_MODES.keys()),
        }

    _BUNDLE_MODES = {
        "file": "Full file context: outline + deps + schema hints",
        "service": "Service file + related backend neighbors",
        "component": "Component + imported frontend neighbors",
        "query": "Query hotspot + schema hints + relationship paths",
        "subsystem": "Broad concept analysis across all layers",
        "dependency": "File + resolved dependency chain",
        "partial": "All partial class definitions for a C# type",
        "symbol": "Symbol definition + references + schema matches",
        "style": "CSS selector matches for class names",
        "session": "Session-guided code bundle from context targets",
        "context": "Session-guided ranked context bundle",
        "preset": "Preconfigured bundle (csharp-partial, data-structure, etc.)",
        "tree": "Recursive component import tree",
    }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Bundle",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_tool
    def code_bundle(
        project_root: str,
        target: str,
        mode: str = "file",
        session_id: str | None = None,
        limit: int = 20,
        timeout: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Unified bundle tool — replaces all code_get_*_bundle tools.

        Modes: file, service, component, query, subsystem, dependency, partial,
        symbol, style, session, context, preset, tree.

        Args:
            target: File path, symbol name, concept, CSS class, or preset spec depending on mode.
            mode: Which bundle mode to use.
            session_id: Required for session/context modes.
        """
        root = Path(project_root)
        m = mode.strip().lower()

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_indexed_read_gate(hub, root, "code_bundle")
                return result
            if any(
                result.get(key)
                for key in (
                    "primary_files",
                    "related_files",
                    "files",
                    "symbols",
                    "matches",
                )
            ):
                _grant_indexed_read_gate(hub, root, "code_bundle")
            bundle_type = m
            file_count = (
                len(result.get("files") or []) if isinstance(result, dict) else 0
            )
            symbol_count = (
                len(result.get("symbols") or []) if isinstance(result, dict) else 0
            )
            if not file_count and isinstance(result, dict):
                file_count = len(result.get("primary_files") or []) + len(
                    result.get("related_files") or []
                )
            summary_bits: list[str] = []
            if file_count:
                summary_bits.append(f"files={file_count}")
            if symbol_count:
                summary_bits.append(f"symbols={symbol_count}")
            if result.get("missing"):
                inline_summary = f"Bundle `{bundle_type}` for `{target}` is missing."
            else:
                suffix = f" ({', '.join(summary_bits)})" if summary_bits else ""
                inline_summary = (
                    f"Bundle `{bundle_type}` prepared for `{target}`{suffix}."
                )
            compact = runtime.build_artifact_backed_result(
                root,
                inline_summary=inline_summary,
                payload=result,
                artifact_name=f"code-bundle-{bundle_type}-{target}",
                structured_summary={
                    "mode": bundle_type,
                    "target": target,
                    "missing": bool(result.get("missing")),
                    "file_count": file_count,
                    "symbol_count": symbol_count,
                },
            )
            result.update(compact)
            return result

        if m == "file":
            return _grant(hub.code.get_file_bundle(root, path=target))
        if m == "service":
            return _grant(hub.code.get_service_bundle(root, path=target, limit=limit))
        if m == "component":
            return _grant(hub.code.get_component_bundle(root, path=target, limit=limit))
        if m == "query":
            return _grant(hub.code.get_query_bundle(root, path=target, limit=limit))
        if m == "subsystem":
            return _grant(
                hub.code.get_subsystem_bundle(root, concept=target, limit=limit)
            )
        if m == "dependency":
            return _grant(
                hub.code.get_dependency_bundle(root, path=target, limit=limit)
            )
        if m == "partial":
            return _grant(hub.code.get_partial_bundle(root, symbol=target, limit=limit))
        if m == "symbol":
            return _grant(hub.code.get_symbol_bundle(root, symbol=target, limit=limit))
        if m == "style":
            # Accept comma/space separated class names
            if isinstance(target, str):
                class_names = [
                    s.strip() for s in re.split(r"[,\s]+", target) if s.strip()
                ]
            else:
                class_names = target
            return _grant(
                hub.code.get_style_bundle(root, class_names=class_names, limit=limit)
            )
        if m == "session":
            if not session_id:
                return {"error": "session_id is required for session mode"}
            return _grant(hub.code.get_session_code_bundle(root, session_id=session_id))
        if m == "context":
            if not session_id:
                return {"error": "session_id is required for context mode"}
            return _grant(
                hub.code.get_context_bundle(root, session_id=session_id, limit=limit)
            )
        if m == "preset":
            # target format: "preset_name:value" e.g. "csharp-partial:FormPdfService"
            parts = target.split(":", 1)
            preset = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
            return _grant(
                hub.code.get_preset_bundle(
                    root, preset=preset, value=value, limit=limit
                )
            )
        if m == "tree":
            return _grant(hub.code.get_component_tree(root, path=target, limit=limit))
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(create_server._BUNDLE_MODES.keys()),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Schema Query",
        },
        meta={"anthropic/searchHint": True},
    )
    @timed_tool
    def schema_query(
        project_root: str,
        query: str,
        mode: str = "entities",
        limit: int = 50,
        include_related: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Unified schema tool — replaces all schema_find_*, schema_get_*, schema_trace_* tools.

        Modes: entities, entity, field, trace_flow, trace_path.

        Args:
            query: Entity name, field name, or "source→target" for trace_path mode.
            mode: Which schema operation to run.
        """
        root = Path(project_root)
        m = mode.strip().lower()

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_indexed_read_gate(hub, root, "schema_query")
                return result
            if any(
                result.get(key)
                for key in ("entities", "fields", "matches", "properties")
            ):
                _grant_indexed_read_gate(hub, root, "schema_query")
            return result

        if m == "entities":
            return _grant(
                hub.schema.find_schema_entities(root, query=query or None, limit=limit)
            )
        if m == "entity":
            return _grant(hub.schema.get_schema_entity(root, entity_name=query))
        if m == "batch_entity":
            names = [
                part.strip() for part in re.split(r"[\n,]+", query) if part.strip()
            ]
            return _grant(
                hub.schema.get_schema_entities_batch(root, entity_names=names)
            )
        if m == "field":
            return _grant(
                hub.schema.find_schema_field(root, field_name=query, limit=limit)
            )
        if m == "constructor":
            return _grant(
                hub.schema.get_constructor_params(
                    root, entity_name=query, include_related=include_related
                )
            )
        if m == "properties":
            return _grant(hub.schema.get_entity_properties(root, entity_name=query))
        if m == "trace_flow":
            return _grant(
                hub.schema.trace_entity_flow(root, entity_name=query, limit=limit)
            )
        if m == "trace_path":
            # Accept "Source→Target" or "Source -> Target" or "Source,Target"
            parts = re.split(r"[→\->,]+", query, maxsplit=1)
            if len(parts) < 2:
                return {
                    "error": "trace_path requires 'Source→Target' format",
                    "query": query,
                }
            return _grant(
                hub.schema.trace_relationship_path(
                    root,
                    source_entity=parts[0].strip(),
                    target_entity=parts[1].strip(),
                    limit=limit,
                )
            )
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": [
                "entities",
                "entity",
                "field",
                "trace_flow",
                "trace_path",
            ],
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Legacy Tools (deprecated — use unified tools above)
    # ═══════════════════════════════════════════════════════════════════════

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Capture Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def memory_capture(
        project_root: str,
        kind: str,
        content: str,
        target_hint: str | None = None,
    ) -> dict[str, str]:
        """Capture a durable fact/rule into canonical memory.

        Args:
            kind: Memory category — 'rule', 'feedback', 'domain', 'project', 'user', 'reference', 'system'.
            content: The fact/rule to persist (any language).
            target_hint: Target filename or path. Use this to route to the right file:
        - 'workflow' → rules/workflow-rules.md (git, deploy, task lifecycle, CI rules)
                - 'coding-standards' → rules/coding-standards.md (code style, naming, patterns)
                - 'communication' → rules/communication.md (response style, verbosity, tone)
                - 'design' → rules/design.md (UI, colors, themes, layout preferences)
                - 'security' → rules/security.md (auth, permissions, credentials)
                - 'project-state' → domains/project-state.md (current project status/decisions)
                - 'user-profile' → domains/user-profile.md (user role, expertise, preferences)
                - 'references' → domains/references.md (external system pointers)
                - Or any path like 'domains/accounting.md' for topic-specific files.
                If omitted, AIDOCS uses content-based keyword routing as fallback (English-only, less reliable).
                Prefer providing target_hint for accurate routing.
        """
        result = hub.memory.capture_memory(
            Path(project_root),
            kind=kind,
            content=content,
            target_hint=target_hint,
        )
        return {
            "target_file": str(result.target_file),
            "content": result.content,
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Initialize Project",
        }
    )
    @timed_sync
    def project_init(
        project_root: str,
        init_git: bool = True,
        create_remote: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Initialize AIDOCS structure on a new project — creates .MEMORY/, AGENTS.md/CLAUDE.md, and templates.

        Creates the full AIDOCS directory structure directly (no shell scripts).
        Safe to call on already-initialized projects (idempotent).
        Also ensures the project has a .mcp.json with the aidocs MCP server entry for Claude Code.

        Args:
            init_git: If True (default), initialize a git repo if none exists.
            create_remote: If True, create a private GitHub repo using `gh` CLI. Default: False (opt-in).
        """
        return runtime.project_init(
            Path(project_root), init_git=init_git, create_remote=create_remote
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Ensure MCP Config",
        }
    )
    def project_ensure_mcp_config(project_root: str) -> dict[str, Any]:
        """Ensure the target project has a .mcp.json with the aidocs MCP server entry for Claude Code.

        Idempotent — safe to call repeatedly. Creates or updates .mcp.json as needed.
        Preserves any existing non-aidocs MCP server entries.
        """
        return runtime.ensure_claude_mcp_config(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Check Project",
        }
    )
    def project_check(project_root: str) -> dict[str, Any]:
        """Run strict session-era structural check on a project."""
        return hub.updater.run_check(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Check Project (Legacy)",
        }
    )
    def project_check_legacy(project_root: str) -> dict[str, Any]:
        """Run legacy-compatible structural check on a project."""
        return hub.updater.run_check_legacy(Path(project_root))

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Fix Project",
        }
    )
    def project_fix(project_root: str) -> dict[str, Any]:
        """Run safe deterministic structural fixes on a project."""
        return hub.updater.run_fix(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Inspect Legacy",
        }
    )
    def project_inspect_legacy(project_root: str) -> dict[str, Any]:
        """Inspect whether legacy runtime files/folders are still present."""
        return hub.updater.inspect_legacy_runtime(Path(project_root))

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Project Indexes",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def project_sync_indexes(
        project_root: str, include_tests: bool = False, timeout: int | None = None
    ) -> dict[str, Any]:
        """Refresh all derived indexes for a project in one call."""
        root = Path(project_root)
        capability_count = hub.capabilities.sync_capabilities(root, _registered_tools())
        workflow_sync = hub.workflow.compile_project_rules(root)
        procedure_count = hub.procedures.sync_procedures(
            root, hub.workflow.read_compiled(root)
        )
        link_count = hub.procedure_links.sync_links(
            root, _all_procedures(root), _all_capabilities(root)
        )
        code_processed = hub.code.sync_code_files(root, include_tests=include_tests)
        code_status = hub.code.code_status(root)
        return {
            "memory": hub.index.sync_all(root),
            "capabilities": {"capability_definitions": capability_count},
            "code_manifest": {
                "processed_code_files": code_processed,
                "code_files": code_status.get("code_files"),
                "parsed_code_files": code_status.get("parsed_code_files"),
            },
            "schema": hub.schema.sync_schema(root),
            "workflow": workflow_sync,
            "procedures": {"procedure_definitions": procedure_count},
            "procedure_capability_links": {"links": link_count},
            "execution": hub.execution.execution_status(root),
            "execution_pruning": hub.execution.prune_old_events(root),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Status",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_status(project_root: str) -> dict[str, Any]:
        """Return a consolidated status view for memory, code, and schema indexes."""
        root = Path(project_root)
        return {
            "origins": runtime.project_origins(root),
            "repo_summary": runtime.repo_summary(root),
            "memory": hub.index.status(root),
            "capabilities": hub.capabilities.capability_status(root),
            "code": hub.code.code_status(root),
            "schema": hub.schema.schema_status(root),
            "workflow": hub.workflow.status(root),
            "procedures": hub.procedures.procedure_status(root),
            "procedure_capability_links": hub.procedure_links.link_status(root),
            "execution": hub.execution.execution_status(root),
            "legacy": hub.updater.inspect_legacy_runtime(root),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Origins",
        }
    )
    def project_origins_get(project_root: str) -> dict[str, Any]:
        """Return git remote/origin context, including private/public split hints."""
        root = Path(project_root)
        return runtime.project_origins(root)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptors",
        }
    )
    def index_language_descriptors_get(project_root: str) -> dict[str, Any]:
        """Return the active built-in + project-local language descriptor registry summary."""
        return descriptor_registry_summary(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Validate Language Descriptors",
        }
    )
    def index_language_descriptors_validate(project_root: str) -> dict[str, Any]:
        """Validate built-in and project-local TOML language descriptors."""
        return validate_language_descriptors(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptor Semantics",
        }
    )
    def index_language_descriptor_semantics_get() -> dict[str, Any]:
        """Return the available built-in descriptor semantic families/tags."""
        return descriptor_semantics_summary()

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptor Match",
        }
    )
    def index_language_descriptor_match_get(
        project_root: str, relative_path: str
    ) -> dict[str, Any]:
        """Show which descriptor would classify a given project-relative path."""
        return descriptor_match_summary(Path(project_root), relative_path)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Capability Index Status",
        }
    )
    def capability_index_status(project_root: str) -> dict[str, Any]:
        """Return current MCP capability index status for a project."""
        return hub.capabilities.capability_status(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Capability Definitions",
        },
        meta={"anthropic/searchHint": True},
    )
    def capability_definitions_get(
        project_root: str, query: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Return indexed MCP capability definitions, optionally filtered by query."""
        root = Path(project_root)
        result = hub.capabilities.find_capabilities(root, query=query, limit=limit)
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} capability definition(s).",
            payload=result,
            artifact_name="capability-definitions",
            structured_summary={
                "count": len(result),
                "query": query,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Procedure Index Status",
        }
    )
    def procedure_index_status(project_root: str) -> dict[str, Any]:
        """Return current procedure-definition index status for a project."""
        return hub.procedures.procedure_status(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Procedure Definitions",
        },
        meta={"anthropic/searchHint": True},
    )
    def procedure_definitions_get(
        project_root: str, query: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Return indexed procedure definitions, optionally filtered by query."""
        root = Path(project_root)
        result = hub.procedures.find_procedures(root, query=query, limit=limit)
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} procedure definition(s).",
            payload=result,
            artifact_name="procedure-definitions",
            structured_summary={
                "count": len(result),
                "query": query,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Procedure Capability Link Status",
        }
    )
    def procedure_capability_link_status(project_root: str) -> dict[str, Any]:
        """Return current procedure-to-capability link status for a project."""
        return hub.procedure_links.link_status(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Procedure Capability Links",
        }
    )
    def procedure_capability_links_get(
        project_root: str,
        procedure_id: str | None = None,
        unresolved_only: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return indexed procedure-to-capability links, optionally filtered by procedure or unresolved status."""
        root = Path(project_root)
        result = hub.procedure_links.list_links(
            root,
            procedure_id=procedure_id,
            unresolved_only=unresolved_only,
            limit=limit,
        )
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} procedure-capability link(s).",
            payload=result,
            artifact_name="procedure-capability-links",
            structured_summary={
                "count": len(result),
                "procedure_id": procedure_id,
                "unresolved_only": unresolved_only,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Index Status",
        }
    )
    def execution_index_status(project_root: str) -> dict[str, Any]:
        """Return current execution-evidence index status for a project."""
        return hub.execution.execution_status(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Execution Runs",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_runs_get(
        project_root: str, session_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Return indexed execution runs, optionally filtered by session."""
        root = Path(project_root)
        result = hub.execution.list_runs(root, session_id=session_id, limit=limit)
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} execution run(s).",
            payload=result,
            artifact_name="execution-runs",
            session_id=session_id,
            structured_summary={
                "count": len(result),
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Execution Events",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_events_get(
        project_root: str,
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return indexed execution events, optionally filtered by query/session."""
        root = Path(project_root)
        result = hub.execution.list_events(
            root, query=query, session_id=session_id, limit=limit
        )
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} execution event(s).",
            payload=result,
            artifact_name="execution-events",
            session_id=session_id,
            structured_summary={
                "count": len(result),
                "query": query,
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Record Execution Run",
        }
    )
    def execution_run_record(
        project_root: str,
        run_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        status: str = "started",
        ad_hoc: bool = True,
        target_entity: str | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        """Record or update an execution run for observed work."""
        resolved = hub.execution.record_run(
            Path(project_root),
            run_kind=run_kind,
            source_kind=source_kind,
            session_id=session_id,
            procedure_id=procedure_id,
            capability_name=capability_name,
            status=status,
            ad_hoc=ad_hoc,
            target_entity=target_entity,
            metadata=metadata,
            run_id=run_id,
            completed_at=completed_at,
        )
        return {"run_id": resolved}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Record Execution Event",
        }
    )
    def execution_event_record(
        project_root: str,
        event_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        action_kind: str | None = None,
        target_entity: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Record one execution event for observed work."""
        resolved = hub.execution.record_event(
            Path(project_root),
            event_kind=event_kind,
            source_kind=source_kind,
            session_id=session_id,
            procedure_id=procedure_id,
            capability_name=capability_name,
            action_kind=action_kind,
            target_entity=target_entity,
            status=status,
            payload=payload,
            run_id=run_id,
            event_id=event_id,
            observed_at=observed_at,
        )
        return {"event_id": resolved}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Query Last Execution",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_query_last(
        project_root: str,
        action_kind: str | None = None,
        capability_name: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Query: 'What actually ran last time?' — returns recent execution events matching filters."""
        root = Path(project_root)
        result = hub.execution.query_last_execution(
            root,
            action_kind=action_kind,
            capability_name=capability_name,
            session_id=session_id,
            limit=limit,
        )
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} recent execution event(s).",
            payload=result,
            artifact_name="execution-query-last",
            session_id=session_id,
            structured_summary={
                "count": len(result),
                "action_kind": action_kind,
                "capability_name": capability_name,
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Summary",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_query_summary(
        project_root: str, session_id: str | None = None
    ) -> dict[str, Any]:
        """Query: 'What happened in this session?' — returns aggregate execution summary with ad-hoc vs procedure-linked breakdown."""
        return hub.execution.query_execution_summary(
            Path(project_root), session_id=session_id
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Dashboard Snapshot",
        },
        meta={"anthropic/searchHint": True},
    )
    def dashboard_snapshot(
        project_root: str,
        session_id: str | None = None,
        event_limit: int = 12,
    ) -> dict[str, Any]:
        """Return an operator-friendly dashboard snapshot for sessions, conductor state, config, execution, and usage proxies."""
        root = Path(project_root)
        payload = runtime.dashboard_snapshot(
            root,
            session_id=session_id,
            event_limit=event_limit,
        )
        session_count = len(payload.get("sessions") or [])
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=(
                f"Dashboard snapshot ready for {session_count} session(s). "
                f"Selected session: `{payload.get('selected_session_id') or 'none'}`."
            ),
            payload=payload,
            artifact_name="dashboard-snapshot",
            session_id=session_id,
            structured_summary={
                "session_id": payload.get("selected_session_id"),
                "session_count": session_count,
                "has_selected_session": payload.get("selected_session") is not None,
                "token_usage_available": bool(
                    (payload.get("token_usage") or {}).get("available")
                ),
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Registry List",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_registry_list() -> dict[str, Any]:
        """List MCP-touched AIDOCS projects known to the global registry."""
        return {
            "ok": True,
            "projects": project_registry.list_projects(),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Compliance",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_query_compliance(
        project_root: str, session_id: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Query: 'Did execution follow the intended procedure?' — compares procedure-linked runs vs ad-hoc runs."""
        return hub.execution.query_procedure_compliance(
            Path(project_root), session_id=session_id, limit=limit
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Prune Execution Events",
        }
    )
    def execution_prune(
        project_root: str, max_age_days: int = 30, max_events: int = 10000
    ) -> dict[str, Any]:
        """Prune old execution events by age and count. Runs automatically on project_sync_indexes."""
        return hub.execution.prune_old_events(
            Path(project_root), max_age_days=max_age_days, max_events=max_events
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Compare Action Surface",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_compare(
        project_root: str,
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Compare what should happen, what can happen, and what did happen for a query."""
        return hub.action_surface.compare(
            Path(project_root), query=query, session_id=session_id, limit=limit
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Assess Action Surface",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_assess(
        project_root: str,
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return an operator-facing assessment of the action surface for a query."""
        return hub.action_surface.assess(
            Path(project_root), query=query, session_id=session_id, limit=limit
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Action Surface Status Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_status_bundle(
        project_root: str,
        queries: list[str],
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return an operator-facing multi-query status bundle over action surfaces."""
        return hub.action_surface.status_bundle(
            Path(project_root), queries=queries, session_id=session_id, limit=limit
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Action Surface Session Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_session_bundle(
        project_root: str,
        session_id: str,
        limit: int = 20,
        max_queries: int = 12,
    ) -> dict[str, Any]:
        """Return a session-driven operator-facing action-surface status bundle."""
        return hub.action_surface.session_status_bundle(
            Path(project_root),
            session_id=session_id,
            limit=limit,
            max_queries=max_queries,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Current Session Action Surface",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_current_session_bundle(
        project_root: str,
        limit: int = 20,
        max_queries: int = 12,
    ) -> dict[str, Any]:
        """Return a session-driven operator-facing action-surface bundle for the current managed or sole active session."""
        return hub.action_surface.current_session_bundle(
            Path(project_root), limit=limit, max_queries=max_queries
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Compile Workflow Actions",
        }
    )
    def workflow_actions_compile(project_root: str) -> dict[str, Any]:
        """Compile human-readable workflow rules into the runtime workflow artifact."""
        return hub.workflow.compile_project_rules(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Workflow Actions",
        },
        meta={"anthropic/searchHint": True},
    )
    def workflow_actions_get(project_root: str) -> dict[str, Any] | None:
        """Read the compiled runtime workflow artifact for a project if present."""
        return hub.workflow.read_compiled(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Workflow Triggers",
        },
        meta={"anthropic/searchHint": True},
    )
    def workflow_triggers_for_action(
        project_root: str, action_kind: str
    ) -> dict[str, Any]:
        """Find workflow triggers that would fire after an action_kind completes."""
        triggers = hub.workflow.triggers_for_action_kind(action_kind)
        pending: list[dict[str, Any]] = []
        for trigger in triggers:
            pending.extend(
                hub.workflow.pending_actions_for_trigger(Path(project_root), trigger)
            )
        return {
            "action_kind": action_kind,
            "triggers": triggers,
            "pending_actions": pending,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Status Model",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_status_model_get(project_root: str) -> dict[str, Any] | None:
        """Read the deterministic project status model if present."""
        return hub.project_status.read_model(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Evaluate Project Status",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_status_evaluate(project_root: str) -> dict[str, Any]:
        """Evaluate the deterministic project status model."""
        return hub.project_status.evaluate(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Status Area Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_status_area_bundle(
        project_root: str, area_id: str, limit: int = 20
    ) -> dict[str, Any]:
        """Return status details plus a subsystem bundle for one declared project-status area."""
        return hub.project_status.get_area_bundle(
            Path(project_root), area_id=area_id, limit=limit
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project Code Search",
        },
        meta={"anthropic/searchHint": True},
    )
    def related_project_code_search(
        project_root: str, name: str, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search code in a configured related project using the same generic code index."""
        related_root = _resolve_related_root(project_root, name)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        return hub.code.search_code(related_root, query=query, limit=limit)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project Symbol Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def related_project_symbol_bundle(
        project_root: str,
        name: str,
        symbol: str,
        path: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Build a symbol bundle from a configured related project."""
        related_root = _resolve_related_root(project_root, name)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        return hub.code.get_symbol_bundle(
            related_root, symbol=symbol, path=path, kind=kind, limit=limit
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project Subsystem Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def related_project_subsystem_bundle(
        project_root: str, name: str, concept: str, limit: int = 20
    ) -> dict[str, Any]:
        """Build a broad subsystem bundle from a configured related project."""
        related_root = _resolve_related_root(project_root, name)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        hub.schema.sync_schema(related_root)
        return hub.code.get_subsystem_bundle(related_root, concept=concept, limit=limit)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project Compare Concept",
        },
        meta={"anthropic/searchHint": True},
    )
    def related_project_compare_concept(
        project_root: str, name: str, concept: str, limit: int = 20
    ) -> dict[str, Any]:
        """Compare a concept between the current project and a configured related project."""
        root = Path(project_root)
        related_root = _resolve_related_root(project_root, name)
        hub.code.sync_code_manifest(root, include_tests=False)
        hub.schema.sync_schema(root)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        hub.schema.sync_schema(related_root)
        return {
            "concept": concept,
            "current_project": str(root),
            "related_project": {
                "name": name,
                "path": str(related_root),
            },
            "current": hub.code.get_subsystem_bundle(
                root, concept=concept, limit=limit
            ),
            "related": hub.code.get_subsystem_bundle(
                related_root, concept=concept, limit=limit
            ),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Legacy Read Runtime",
        }
    )
    def legacy_read_runtime(project_root: str) -> dict[str, Any]:
        """Inspect legacy NOW/plans state without mutating the project."""
        return hub.legacy.inspect_legacy(Path(project_root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Legacy Build Session Proposal",
        }
    )
    def legacy_build_session_proposal(
        project_root: str, session_id: str | None = None
    ) -> dict[str, Any]:
        """Build a non-destructive session proposal from legacy NOW/plans state."""
        return hub.legacy.build_session_proposal(
            Path(project_root), session_id=session_id
        )

    # ═══════════════════════════════════════════════════════════════════════
    # Database Query Tool
    # ═══════════════════════════════════════════════════════════════════════

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Database Query",
        }
    )
    def db_query(
        project_root: str,
        sql: str,
        connection_string: str | None = None,
    ) -> dict[str, Any]:
        """Execute a read-only SQL query against the project's PostgreSQL database.

        Safety: Only SELECT statements are allowed. DDL/DML (INSERT, UPDATE, DELETE, DROP, etc.) is blocked.

        Args:
            project_root: Project root path (used to auto-detect connection string from appsettings.json).
            sql: SQL query to execute (SELECT only).
            connection_string: Override connection string (format: 'Host=...;Database=...;Username=...;Password=...').
                             If not provided, reads from appsettings.json or defaults to localhost/dentalapp.
        """
        import subprocess, json as json_mod

        # Safety: block non-SELECT statements
        stripped = sql.strip().lstrip("(").strip()
        first_word = stripped.split()[0].upper() if stripped.split() else ""
        if first_word not in ("SELECT", "WITH", "EXPLAIN"):
            return {
                "error": f"Only SELECT/WITH/EXPLAIN queries allowed, got: {first_word}",
                "rows": [],
            }

        # Resolve connection params
        root = Path(project_root)
        host = "localhost"
        port = "5432"
        database = "dentalapp"
        username = "postgres"
        password = "admin"

        if connection_string:
            # Parse .NET-style connection string
            for part in connection_string.split(";"):
                kv = part.strip().split("=", 1)
                if len(kv) == 2:
                    key, val = kv[0].strip().lower(), kv[1].strip()
                    if key == "host":
                        host = val
                    elif key in ("database", "db"):
                        database = val
                    elif key in ("username", "user id", "user"):
                        username = val
                    elif key == "password":
                        password = val
                    elif key == "port":
                        port = val
        else:
            # Try to read from appsettings.json
            for settings_file in ["appsettings.Development.json", "appsettings.json"]:
                candidates = list(root.rglob(settings_file))
                for candidate in candidates:
                    try:
                        settings = json_mod.loads(
                            candidate.read_text(encoding="utf-8", errors="ignore")
                        )
                        conn_str = (settings.get("ConnectionStrings") or {}).get(
                            "DefaultConnection"
                        )
                        if conn_str:
                            for part in conn_str.split(";"):
                                kv = part.strip().split("=", 1)
                                if len(kv) == 2:
                                    key, val = kv[0].strip().lower(), kv[1].strip()
                                    if key == "host":
                                        host = val
                                    elif key in ("database", "db"):
                                        database = val
                                    elif key in ("username", "user id", "user"):
                                        username = val
                                    elif key == "password":
                                        password = val
                                    elif key == "port":
                                        port = val
                            break
                    except Exception:
                        continue

        env = {**__import__("os").environ, "PGPASSWORD": password}
        try:
            import tempfile as _tf

            _db_out = _db_err = None
            try:
                with _tf.NamedTemporaryFile(
                    mode="w", suffix=".db.out", delete=False
                ) as f:
                    _db_out = f.name
                with _tf.NamedTemporaryFile(
                    mode="w", suffix=".db.err", delete=False
                ) as f:
                    _db_err = f.name
                with open(_db_out, "w") as out_fh, open(_db_err, "w") as err_fh:
                    result = subprocess.run(
                        [
                            "psql",
                            "-h",
                            host,
                            "-p",
                            port,
                            "-U",
                            username,
                            "-d",
                            database,
                            "-t",
                            "-A",
                            "-F",
                            "\t",
                            "-c",
                            sql,
                        ],
                        stdout=out_fh,
                        stderr=err_fh,
                        text=True,
                        timeout=30,
                        env=env,
                    )
                stdout = (
                    Path(_db_out).read_text(encoding="utf-8", errors="ignore").strip()
                )
                stderr = (
                    Path(_db_err).read_text(encoding="utf-8", errors="ignore").strip()
                )
            finally:
                import os as _os

                for p in (_db_out, _db_err):
                    if p:
                        try:
                            _os.unlink(p)
                        except OSError:
                            pass
            if result.returncode != 0:
                return {"error": stderr, "rows": []}

            lines = [line for line in stdout.split("\n") if line.strip()]
            return {"row_count": len(lines), "rows": lines[:200]}
        except FileNotFoundError:
            return {
                "error": "psql not found — install PostgreSQL client tools",
                "rows": [],
            }
        except subprocess.TimeoutExpired:
            return {"error": "Query timed out after 30 seconds", "rows": []}
        except Exception as exc:
            return {"error": str(exc), "rows": []}

    # ═══════════════════════════════════════════════════════════════════════
    # Git Analysis Tools
    # ═══════════════════════════════════════════════════════════════════════

    @server.tool()
    async def git_diag(
        project_root: str,
        upstream: str = "upstream/main",
        local: str = "HEAD",
    ) -> dict[str, Any]:
        """Run a minimal git diagnostic inside the live MCP server process.

        Helps distinguish raw git problems from MCP runtime/process issues.
        """
        import os
        import platform
        import threading
        import time

        start = time.perf_counter()
        root = Path(project_root)
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break

        try:
            merge_base = await _run_git(
                str(root), "merge-base", local, upstream, timeout=_GIT_TIMEOUT
            )
            elapsed = round(time.perf_counter() - start, 3)
            return {
                "ok": True,
                "project_root": project_root,
                "git_root": str(root),
                "local_ref": local,
                "upstream_ref": upstream,
                "merge_base": merge_base[:40],
                "elapsed_seconds": elapsed,
                "runtime": {
                    "pid": os.getpid(),
                    "thread": threading.current_thread().name,
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "git_timeout": _GIT_TIMEOUT,
                    "cwd": os.getcwd(),
                },
            }
        except Exception as exc:
            elapsed = round(time.perf_counter() - start, 3)
            return {
                "ok": False,
                "project_root": project_root,
                "git_root": str(root),
                "local_ref": local,
                "upstream_ref": upstream,
                "elapsed_seconds": elapsed,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "runtime": {
                    "pid": os.getpid(),
                    "thread": threading.current_thread().name,
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "git_timeout": _GIT_TIMEOUT,
                    "cwd": os.getcwd(),
                },
            }

    @server.tool()
    @timed_git_async
    async def git_fork_status(
        project_root: str,
        upstream: str = "upstream/main",
        local: str = "HEAD",
        include_files: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Analyze the state of a fork vs upstream: how far behind, how many local changes, conflict risk.

        START HERE for fork/merge tasks. Returns commit counts and conflict predictions.
        Set include_files=True for full file lists (slower on large repos).
        Auto-detects the git root if project_root isn't one.

        Args:
            upstream: Upstream ref to compare against (e.g., "upstream/main", "upstream/dev").
            local: Local ref (default: HEAD).
            include_files: Include file-level details (slower). Default: False for fast overview.
        """

        import time

        start = time.perf_counter()
        step = "init"
        times: dict[str, float] = {}

        def mark(name: str) -> None:
            times[name] = round(time.perf_counter() - start, 3)

        # Find git root — check project_root itself first, then one level down
        root = Path(project_root)
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break
        mark("root")

        async def git(*args: str, timeout: int = _GIT_TIMEOUT) -> str:
            return await _run_git(str(root), *args, timeout=timeout)

        try:
            # Merge base first
            step = "merge_base"
            merge_base = await git("merge-base", local, upstream, timeout=_GIT_TIMEOUT)
            mark(step)
            if not merge_base:
                return {
                    "error": f"No merge base found between {local} and {upstream}. Run 'git fetch upstream' first.",
                    "debug": {"step": step, "times": times},
                }

            step = "counts"
            counts = await git(
                "rev-list",
                "--left-right",
                "--count",
                f"{local}...{upstream}",
                timeout=_GIT_TIMEOUT,
            )
            mark(step)
            parts = counts.split()
            ahead = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
            behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            divergence = ahead + behind

            result: dict[str, Any] = {
                "git_root": str(root),
                "merge_base": merge_base[:12],
                "behind": behind,
                "ahead": ahead,
                "local_ref": local,
                "upstream_ref": upstream,
                "divergence": divergence,
            }

            # File-level details (optional — can be slow on large repos)
            if include_files:
                step = "local_diff"
                local_changed = [
                    l
                    for l in (
                        await git(
                            "diff",
                            "--name-only",
                            "--no-renames",
                            merge_base,
                            local,
                            timeout=_GIT_TIMEOUT,
                        )
                    ).splitlines()
                    if l.strip()
                ]
                mark(step)
                step = "upstream_diff"
                upstream_changed = [
                    l
                    for l in (
                        await git(
                            "diff",
                            "--name-only",
                            "--no-renames",
                            merge_base,
                            upstream,
                            timeout=_GIT_TIMEOUT,
                        )
                    ).splitlines()
                    if l.strip()
                ]
                mark(step)
                local_set = set(local_changed)
                upstream_set = set(upstream_changed)
                conflict_candidates = sorted(local_set & upstream_set)

                result.update(
                    {
                        "local_stat": f"{len(local_changed)} files changed (exact)",
                        "upstream_stat": f"{len(upstream_changed)} files changed (exact)",
                        "local_changed_files": len(local_changed),
                        "upstream_changed_files": len(upstream_changed),
                        "conflict_candidates": len(conflict_candidates),
                        "conflict_files": conflict_candidates[:50],
                        "local_only_files": sorted(local_set - upstream_set)[:30],
                        "upstream_only_files": sorted(upstream_set - local_set)[:30],
                    }
                )
            elif divergence > _GIT_FAST_DIVERGENCE:
                step = "fast_path"
                result.update(
                    {
                        "local_stat": f"skipped fast-path due to large divergence ({divergence} commits)",
                        "upstream_stat": f"skipped fast-path due to large divergence ({divergence} commits)",
                        "local_changed_files_approx": None,
                        "upstream_changed_files_approx": None,
                        "note": (
                            "Fast path used for a large branch gap. "
                            "Set include_files=True for exact file lists, or narrow the comparison."
                        ),
                    }
                )
                mark(step)
            else:
                step = "local_shortstat"
                local_stat = await git(
                    "diff",
                    "--shortstat",
                    "--no-renames",
                    merge_base,
                    local,
                    timeout=_GIT_TIMEOUT,
                )
                mark(step)
                step = "upstream_shortstat"
                upstream_stat = await git(
                    "diff",
                    "--shortstat",
                    "--no-renames",
                    merge_base,
                    upstream,
                    timeout=_GIT_TIMEOUT,
                )
                mark(step)
                # Estimate file counts from shortstat (fast)
                import re as _re

                local_files = (
                    int(m.group(1))
                    if (m := _re.search(r"(\d+) files? changed", local_stat))
                    else 0
                )
                upstream_files = (
                    int(m.group(1))
                    if (m := _re.search(r"(\d+) files? changed", upstream_stat))
                    else 0
                )
                result.update(
                    {
                        "local_stat": local_stat or "no changes",
                        "upstream_stat": upstream_stat or "no changes",
                        "local_changed_files_approx": local_files,
                        "upstream_changed_files_approx": upstream_files,
                        "note": "Set include_files=True for file lists and conflict prediction (slower)",
                    }
                )

            result["summary"] = (
                f"{behind} commits behind, {ahead} ahead. "
                f"Local: {result.get('local_stat', 'n/a')}. "
                f"Upstream: {result.get('upstream_stat', 'n/a')}."
            )
            mark("done")
            result["debug"] = {"step": step, "times": times}
            return result
        except TimeoutError as exc:
            mark("timeout")
            return {"error": str(exc), "debug": {"step": step, "times": times}}
        except Exception as exc:
            mark("error")
            return {"error": str(exc), "debug": {"step": step, "times": times}}

    @server.tool()
    @timed_git_async
    async def git_upstream_changes(
        project_root: str,
        upstream: str = "upstream/main",
        path_filter: str | None = None,
        limit: int = 50,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Summarize what changed upstream since the fork diverged.

        Groups changes by directory/module and shows commit messages.

        Args:
            upstream: Upstream ref.
            path_filter: Only show changes in this path (e.g., "packages/opencode/src/session/").
        """
        import subprocess

        root = Path(project_root)
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break

        async def git(*args: str, timeout: int = _GIT_TIMEOUT) -> str:
            return await _run_git(str(root), *args, timeout=timeout)

        try:
            merge_base = await git("merge-base", "HEAD", upstream)

            # Get commits with a clear separator format for reliable parsing
            log_args = [
                "log",
                f"--format=COMMIT:%h %s",
                "--name-only",
                f"{merge_base}..{upstream}",
            ]
            if path_filter:
                log_args.extend(["--", path_filter])
            log_args.append(f"-{limit}")
            raw = await git(*log_args)

            commits: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            for line in raw.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("COMMIT:"):
                    if current:
                        commits.append(current)
                    rest = stripped[7:]
                    parts = rest.split(" ", 1)
                    current = {
                        "hash": parts[0],
                        "message": parts[1] if len(parts) > 1 else "",
                        "files": [],
                    }
                elif current:
                    current["files"].append(stripped)
            if current:
                commits.append(current)

            # Group files by top-level directory
            dir_changes: dict[str, int] = {}
            for c in commits:
                for f in c["files"]:
                    top = f.split("/")[0] if "/" in f else "(root)"
                    dir_changes[top] = dir_changes.get(top, 0) + 1

            return {
                "merge_base": merge_base,
                "commit_count": len(commits),
                "commits": commits[:limit],
                "changes_by_directory": dict(
                    sorted(dir_changes.items(), key=lambda x: -x[1])[:20]
                ),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @server.tool()
    @timed_git_async
    async def git_conflict_analysis(
        project_root: str,
        file_path: str,
        upstream: str = "upstream/main",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Deep analysis of a single file that will likely conflict during merge.

        Shows what changed locally vs upstream, with line-level diff context.

        Args:
            file_path: The file to analyze.
            upstream: Upstream ref.
        """
        import subprocess

        root = Path(project_root)
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break

        async def git(*args: str, timeout: int = _GIT_TIMEOUT) -> str:
            return await _run_git(str(root), *args, timeout=timeout)

        try:
            merge_base = await git("merge-base", "HEAD", upstream)

            local_diff = await git("diff", merge_base, "HEAD", "--", file_path)
            upstream_diff = await git("diff", merge_base, upstream, "--", file_path)

            # Count changes
            local_adds = sum(
                1
                for l in local_diff.splitlines()
                if l.startswith("+") and not l.startswith("+++")
            )
            local_dels = sum(
                1
                for l in local_diff.splitlines()
                if l.startswith("-") and not l.startswith("---")
            )
            upstream_adds = sum(
                1
                for l in upstream_diff.splitlines()
                if l.startswith("+") and not l.startswith("+++")
            )
            upstream_dels = sum(
                1
                for l in upstream_diff.splitlines()
                if l.startswith("-") and not l.startswith("---")
            )

            # Upstream commits that touched this file
            upstream_commits = await git(
                "log", "--oneline", f"{merge_base}..{upstream}", "--", file_path
            )

            return {
                "file": file_path,
                "merge_base": merge_base,
                "local_changes": {"additions": local_adds, "deletions": local_dels},
                "upstream_changes": {
                    "additions": upstream_adds,
                    "deletions": upstream_dels,
                },
                "upstream_commits": upstream_commits.splitlines()[:20],
                "local_diff": local_diff[:3000] if local_diff else "(no local changes)",
                "upstream_diff": upstream_diff[:3000]
                if upstream_diff
                else "(no upstream changes)",
                "recommendation": (
                    "KEEP LOCAL"
                    if not upstream_diff
                    else "TAKE UPSTREAM"
                    if not local_diff
                    else "MANUAL MERGE REQUIRED — both sides changed this file"
                ),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @server.tool()
    @timed_git_async
    async def git_merge_plan(
        project_root: str,
        upstream: str = "upstream/main",
        local: str = "HEAD",
        limit: int = 50,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Generate a merge plan: which files to keep, which to take from upstream, which need manual merge.

        Args:
            upstream: Upstream ref to merge from.
        """
        import subprocess
        import time

        start = time.perf_counter()
        step = "init"
        times: dict[str, float] = {}

        def mark(name: str) -> None:
            times[name] = round(time.perf_counter() - start, 3)

        root = Path(project_root)
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break
        mark("root")

        async def git(*args: str, timeout: int = _GIT_TIMEOUT) -> str:
            return await _run_git(str(root), *args, timeout=timeout)

        async def git_lines(*args: str, timeout: int = _GIT_TIMEOUT) -> list[str]:
            return [
                l for l in (await git(*args, timeout=timeout)).splitlines() if l.strip()
            ]

        try:
            step = "merge_base"
            merge_base = await git("merge-base", local, upstream, timeout=_GIT_TIMEOUT)
            mark(step)
            step = "counts"
            counts = await git(
                "rev-list",
                "--left-right",
                "--count",
                f"{local}...{upstream}",
                timeout=_GIT_TIMEOUT,
            )
            mark(step)
            parts = counts.split()
            ahead = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
            behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            divergence = ahead + behind

            if divergence > _GIT_SAMPLE_DIVERGENCE:
                sample = max(limit * 8, 200)
                step = "local_log"
                local_changed = set(
                    await git_lines(
                        "log",
                        "--format=",
                        "--name-only",
                        "--no-renames",
                        f"-{sample}",
                        f"{merge_base}..{local}",
                        timeout=_GIT_TIMEOUT,
                    )
                )
                mark(step)
                step = "upstream_log"
                upstream_changed = set(
                    await git_lines(
                        "log",
                        "--format=",
                        "--name-only",
                        "--no-renames",
                        f"-{sample}",
                        f"{merge_base}..{upstream}",
                        timeout=_GIT_TIMEOUT,
                    )
                )
                mark(step)
                keep_local = sorted(local_changed - upstream_changed)
                take_upstream = sorted(upstream_changed - local_changed)
                manual_merge = sorted(local_changed & upstream_changed)
                return {
                    "merge_base": merge_base,
                    "local_ref": local,
                    "upstream_ref": upstream,
                    "divergence": divergence,
                    "mode": "fast-sampled",
                    "keep_local": keep_local[:limit],
                    "keep_local_count": None,
                    "take_upstream": take_upstream[:limit],
                    "take_upstream_count": None,
                    "manual_merge": manual_merge[:limit],
                    "manual_merge_count": None,
                    "strategy": (
                        f"Large divergence fast path used ({divergence} commits). "
                        f"Lists are sampled from the most recent {sample} commits per side, not exact full-history file sets."
                    ),
                    "debug": {"step": step, "times": times},
                }

            step = "local_diff"
            local_changed = set(
                await git_lines(
                    "diff",
                    "--name-only",
                    "--no-renames",
                    merge_base,
                    local,
                    timeout=_GIT_TIMEOUT,
                )
            )
            mark(step)
            step = "upstream_diff"
            upstream_changed = set(
                await git_lines(
                    "diff",
                    "--name-only",
                    "--no-renames",
                    merge_base,
                    upstream,
                    timeout=_GIT_TIMEOUT,
                )
            )
            mark(step)

            keep_local: list[str] = []  # only we changed
            take_upstream: list[str] = []  # only upstream changed
            manual_merge: list[str] = []  # both changed

            for f in sorted(local_changed | upstream_changed):
                in_local = f in local_changed
                in_upstream = f in upstream_changed
                if in_local and in_upstream:
                    manual_merge.append(f)
                elif in_local:
                    keep_local.append(f)
                else:
                    take_upstream.append(f)

            return {
                "merge_base": merge_base,
                "local_ref": local,
                "upstream_ref": upstream,
                "divergence": divergence,
                "mode": "exact",
                "keep_local": keep_local[:limit],
                "keep_local_count": len(keep_local),
                "take_upstream": take_upstream[:limit],
                "take_upstream_count": len(take_upstream),
                "manual_merge": manual_merge[:limit],
                "manual_merge_count": len(manual_merge),
                "strategy": (
                    f"Safe auto-merge: {len(take_upstream)} files (upstream only). "
                    f"Keep as-is: {len(keep_local)} files (local only). "
                    f"Manual review: {len(manual_merge)} files (both changed)."
                ),
                "debug": {"step": step, "times": times},
            }
        except Exception as exc:
            mark("error")
            return {"error": str(exc), "debug": {"step": step, "times": times}}

    # Patch tool descriptions from TOML — sync, runs before server starts
    _patch_tool_descriptions_sync(server)

    return server


def _patch_tool_descriptions_sync(server: FastMCP) -> None:
    """Override tool docstrings with terse agent-facing descriptions from TOML."""
    from .config import _ACTION_HOOK_DEFAULTS, _get_dotted

    descriptions = _get_dotted(_ACTION_HOOK_DEFAULTS, "tool_descriptions")
    if not isinstance(descriptions, dict):
        return
    prefix = "aidocs_"

    provider = getattr(server, "_local_provider", None)
    if provider is None:
        return
    components = getattr(provider, "_components", None)
    if not isinstance(components, dict):
        return

    for short_name, desc in descriptions.items():
        if not isinstance(desc, str):
            continue
        full_name = f"{prefix}{short_name}"
        # FastMCP stores tools with key format "tool:{name}@"
        key = f"tool:{full_name}@"
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
