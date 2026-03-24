from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any
from uuid import uuid4

from .runtime_service import RuntimeService
from .service_hub import AidocsServiceHub


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


def create_server() -> Any:
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError(
            "FastMCP is not installed. Install the MCP package dependencies before running the server."
        ) from exc

    hub = AidocsServiceHub(templates_root=_resolve_templates_root(), script_root=_resolve_script_root())
    runtime = RuntimeService(hub)
    server = FastMCP("AIDOCS MCP")

    def _registered_tools() -> list[Any]:
        components = getattr(getattr(server, "_local_provider", None), "_components", {})
        return [component for key, component in components.items() if str(key).startswith("tool:")]

    def _timestamp() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _project_root_from_args(arguments: dict[str, Any] | None) -> Path | None:
        if not isinstance(arguments, dict):
            return None
        project_root = arguments.get("project_root")
        if not isinstance(project_root, str) or not project_root.strip():
            return None
        return Path(project_root)

    def _capture_enabled(name: str, run_middleware: bool, arguments: dict[str, Any] | None) -> bool:
        if run_middleware:
            return False
        if name in {"execution_run_record", "execution_event_record"}:
            return False
        return _project_root_from_args(arguments) is not None

    def _summarize_tool_result(result: Any) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "result_type": type(result).__name__,
        }
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            summary["structured_keys"] = sorted(str(key) for key in structured.keys())[:10]
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
            return await original_call_tool(name, arguments, version=version, run_middleware=run_middleware, task_meta=task_meta)

        project_root = _project_root_from_args(arguments)
        if project_root is None:
            return await original_call_tool(name, arguments, version=version, run_middleware=run_middleware, task_meta=task_meta)

        managed = hub.managed_mode.get_mode(project_root)
        session_id = str(managed.get("session_id") or "").strip() or None
        run_id = f"mcp-{uuid4()}"
        payload_summary = {
            "tool_name": name,
            "argument_keys": sorted(arguments.keys()) if isinstance(arguments, dict) else [],
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
            result = await original_call_tool(name, arguments, version=version, run_middleware=run_middleware, task_meta=task_meta)
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
            raise FileNotFoundError(f"Related project '{name}' is not configured or its path does not exist.")
        return resolved

    @server.tool()
    def session_list(project_root: str) -> list[dict[str, Any]]:
        """List sessions from project-local /.MEMORY/sessions/."""
        summaries = hub.sessions.list_sessions(Path(project_root))
        return [_session_summary_to_dict(item) for item in summaries]

    @server.tool()
    def session_read(project_root: str, session_id: str) -> dict[str, Any]:
        """Read a single session router file and return its parsed sections."""
        session = hub.sessions.read_session(Path(project_root), session_id)
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool()
    def session_select(project_root: str, session_id: str) -> dict[str, Any]:
        """Select an existing session and return its summary."""
        session = hub.sessions.select_session(Path(project_root), session_id)
        return _session_summary_to_dict(session)

    @server.tool()
    def session_start(
        project_root: str,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        sync_indexes: bool = True,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Run the startup/session-selection flow and return ready context."""
        return runtime.session_start(
            Path(project_root),
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            sync_indexes=sync_indexes,
            include_tests=include_tests,
        )

    @server.tool()
    def project_bootstrap_or_resume(
        project_root: str,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Run the mandatory project setup/index/session bootstrap flow."""
        return runtime.project_bootstrap_or_resume(
            Path(project_root),
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool()
    def aidocs_orchestrate(
        project_root: str,
        user_request: str,
        action_kind: str = "understand",
        session_id: str | None = None,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Run the AIDOCS bootstrap/session/retrieval flow as one high-level entrypoint."""
        return runtime.aidocs_orchestrate(
            Path(project_root),
            user_request=user_request,
            action_kind=action_kind,
            session_id=session_id,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool()
    def aidocs_mode_get(project_root: str) -> dict[str, Any]:
        """Read the current AIDOCS-managed mode state for this project."""
        return hub.managed_mode.get_mode(Path(project_root))

    @server.tool()
    def aidocs_mode_set(project_root: str, session_id: str, source: str = "/aidocs") -> dict[str, Any]:
        """Set AIDOCS-managed mode and bind it to a selected session."""
        return hub.managed_mode.set_mode(Path(project_root), session_id=session_id, source=source)

    @server.tool()
    def aidocs_mode_clear(project_root: str) -> dict[str, Any]:
        """Clear the current AIDOCS-managed mode state for this project."""
        return hub.managed_mode.clear_mode(Path(project_root))

    @server.tool()
    def aidocs_route_prompt(
        project_root: str,
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the deterministic MCP routing decision for a normal user prompt."""
        return runtime.aidocs_route_prompt(
            Path(project_root),
            user_request=user_request,
            action_kind=action_kind,
            explicit_targets=explicit_targets,
        )

    @server.tool()
    def aidocs_classify_prompt(user_request: str, explicit_targets: list[str] | None = None) -> dict[str, Any]:
        """Classify a normal prompt into a deterministic AIDOCS action kind."""
        return runtime.classify_prompt_action(user_request, explicit_targets=explicit_targets)

    @server.tool()
    def aidocs_handle_prompt(
        project_root: str,
        user_request: str,
        action_kind: str = "auto",
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Handle a normal user prompt through the MCP-first routing/orchestration flow."""
        return runtime.aidocs_handle_prompt(
            Path(project_root),
            user_request=user_request,
            action_kind=action_kind,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool()
    def session_create(
        project_root: str,
        session_id: str,
        title: str,
        owner: str,
        goal: str,
        scope: str = "-",
        status: str = "active",
    ) -> dict[str, Any]:
        """Create a new session folder from canonical templates."""
        session = hub.sessions.create_session(
            Path(project_root),
            session_id=session_id,
            title=title,
            owner=owner,
            goal=goal,
            scope=scope,
            status=status,
        )
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool()
    def session_claim_status(project_root: str, session_id: str, stale_after_minutes: int = 30) -> dict[str, Any]:
        """List advisory session claims and whether they are stale."""
        claims = hub.sessions.list_claims(Path(project_root), session_id, stale_after_minutes=stale_after_minutes)
        return {"session_id": session_id, "claims": claims}

    @server.tool()
    def session_claim(project_root: str, session_id: str, agent_id: str, run_id: str, mode: str = "active") -> dict[str, Any]:
        """Add or refresh an advisory agent claim on a session."""
        session = hub.sessions.claim_session(Path(project_root), session_id, agent_id=agent_id, run_id=run_id, mode=mode)
        return {"session_id": session.session_id, "path": str(session.path), "sections": session.sections}

    @server.tool()
    def session_release(project_root: str, session_id: str, agent_id: str, run_id: str | None = None) -> dict[str, Any]:
        """Release one advisory agent claim from a session."""
        session = hub.sessions.release_claim(Path(project_root), session_id, agent_id=agent_id, run_id=run_id)
        return {"session_id": session.session_id, "path": str(session.path), "sections": session.sections}

    @server.tool()
    def session_prune_stale_claims(project_root: str, session_id: str, stale_after_minutes: int = 30) -> dict[str, Any]:
        """Remove stale advisory claims from a session."""
        session = hub.sessions.prune_stale_claims(Path(project_root), session_id, stale_after_minutes=stale_after_minutes)
        return {"session_id": session.session_id, "path": str(session.path), "sections": session.sections}

    @server.tool()
    def task_begin(
        project_root: str,
        session_id: str,
        goal: str | None = None,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = True,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Begin work in a selected session and update session/context state."""
        return runtime.task_begin(
            Path(project_root),
            session_id=session_id,
            goal=goal,
            state=state,
            upcoming=upcoming,
            blockers=blockers,
            relevant_files=relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=relevant_snippets,
            session_facts=session_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool()
    def task_update(
        project_root: str,
        session_id: str,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Update an active task session and optional context state."""
        return runtime.task_update(
            Path(project_root),
            session_id=session_id,
            state=state,
            upcoming=upcoming,
            blockers=blockers,
            relevant_files=relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=relevant_snippets,
            session_facts=session_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool()
    def task_complete(
        project_root: str,
        session_id: str,
        result_summary: str,
        next_status: str = "done",
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Complete task work in a session and update session state."""
        return runtime.task_complete(
            Path(project_root),
            session_id=session_id,
            result_summary=result_summary,
            next_status=next_status,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool()
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

    @server.tool()
    def session_update(project_root: str, session_id: str, patch: dict[str, list[str]]) -> dict[str, Any]:
        """Update structured sections in an existing SESSION.md file."""
        session = hub.sessions.update_session(Path(project_root), session_id, patch)
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool()
    def memory_read(project_root: str, targets: list[str]) -> dict[str, str]:
        """Read canonical memory files by target path."""
        return hub.memory.read_memory(Path(project_root), targets)

    @server.tool()
    def index_sync(project_root: str) -> dict[str, int]:
        """Rebuild the derived SQLite memory/session index from files."""
        return hub.index.sync_all(Path(project_root))

    @server.tool()
    def index_status(project_root: str) -> dict[str, Any]:
        """Report current derived index status for the project."""
        return hub.index.status(Path(project_root))

    @server.tool()
    def schema_index_sync(project_root: str) -> dict[str, int]:
        """Rebuild the derived schema catalog from code and SQL files."""
        return hub.schema.sync_schema(Path(project_root))

    @server.tool()
    def schema_index_status(project_root: str) -> dict[str, Any]:
        """Report current derived schema index status for the project."""
        return hub.schema.schema_status(Path(project_root))

    @server.tool()
    def schema_find_entities(project_root: str, query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Find indexed schema entities such as tables, DTOs, models, and enums."""
        return hub.schema.find_schema_entities(Path(project_root), query=query, limit=limit)

    @server.tool()
    def schema_get_entity(project_root: str, entity_name: str) -> dict[str, Any]:
        """Return one indexed schema/catalog entity with its fields/members."""
        return hub.schema.get_schema_entity(Path(project_root), entity_name=entity_name)

    @server.tool()
    def schema_find_field(project_root: str, field_name: str, limit: int = 50) -> list[dict[str, Any]]:
        """Find indexed schema fields/columns/properties by name."""
        return hub.schema.find_schema_field(Path(project_root), field_name=field_name, limit=limit)

    @server.tool()
    def schema_trace_entity_flow(project_root: str, entity_name: str, limit: int = 50) -> dict[str, Any]:
        """Trace one schema/catalog entity across schema and indexed code references."""
        return hub.schema.trace_entity_flow(Path(project_root), entity_name=entity_name, limit=limit)

    @server.tool()
    def schema_trace_relationship_path(
        project_root: str, source_entity: str, target_entity: str, limit: int = 20
    ) -> dict[str, Any]:
        """Trace possible relationship paths between two schema entities."""
        return hub.schema.trace_relationship_path(
            Path(project_root), source_entity=source_entity, target_entity=target_entity, limit=limit
        )

    @server.tool()
    def memory_search(project_root: str, query: str, limit: int = 10) -> list[dict[str, str]]:
        """Search the derived memory index by path, title, or body text."""
        return hub.index.search_memory(Path(project_root), query=query, limit=limit)

    @server.tool()
    def code_index_sync(project_root: str, include_tests: bool = False) -> dict[str, int]:
        """Rebuild the derived code file manifest and summary index."""
        return {"code_files": hub.code.sync_code_files(Path(project_root), include_tests=include_tests)}

    @server.tool()
    def code_index_status(project_root: str) -> dict[str, Any]:
        """Report current derived code index status for the project."""
        return hub.code.code_status(Path(project_root))

    @server.tool()
    def code_search(project_root: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search the derived code index by file path and lightweight summary."""
        return hub.code.search_code(Path(project_root), query=query, limit=limit)

    @server.tool()
    def code_get_dependencies(project_root: str, path: str) -> list[dict[str, str]]:
        """Return lightweight dependency edges for one indexed code file."""
        return hub.code.get_dependencies(Path(project_root), path=path)

    @server.tool()
    def code_find_dependents(project_root: str, target: str, limit: int = 50) -> list[dict[str, str]]:
        """Return files that depend on a given import/using target."""
        return hub.code.find_dependents(Path(project_root), target=target, limit=limit)

    @server.tool()
    def code_get_dependency_bundle(
        project_root: str, path: str, include_dependents: bool = False, limit: int = 20
    ) -> dict[str, Any]:
        """Return a dependency-aware bundle for one indexed code file."""
        return hub.code.get_dependency_bundle(
            Path(project_root), path=path, include_dependents=include_dependents, limit=limit
        )

    @server.tool()
    def code_search_symbols(
        project_root: str,
        query: str = "",
        kind: str | None = None,
        role: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search indexed outline symbols across the codebase.

        Args:
            query: Symbol name to search for (can be empty if kind is provided).
            kind: Filter by symbol kind (e.g., 'partial_ref', 'js_function', 'form_handler',
                  'translation_key', 'api_call', 'inject', 'section', 'model_binding',
                  'page_route', 'layout_ref', 'component_ref', 'translation',
                  'class', 'method', 'property', 'enum', 'enum_member', 'namespace').
            role: Filter by file role (e.g., 'page-view', 'partial-view', 'service', 'page-model').
        """
        return hub.code.search_symbols(Path(project_root), query=query, kind=kind, role=role, limit=limit)

    @server.tool()
    def code_find_references(project_root: str, symbol: str, limit: int = 100) -> dict[str, Any]:
        """Find exact line-level references to a symbol across indexed code files."""
        return hub.code.find_references(Path(project_root), symbol=symbol, limit=limit)

    @server.tool()
    def code_trace_field_flow(project_root: str, field_name: str, limit: int = 50) -> dict[str, Any]:
        """Trace likely cross-layer field/setting usage across the indexed codebase."""
        return hub.code.trace_field_flow(Path(project_root), field_name=field_name, limit=limit)

    @server.tool()
    def code_trace_setting_usage(project_root: str, setting_name: str, limit: int = 50) -> dict[str, Any]:
        """Trace likely cross-layer usage of a config/setting concept."""
        return hub.code.trace_setting_usage(Path(project_root), setting_name=setting_name, limit=limit)

    @server.tool()
    def code_trace_service_usage(project_root: str, service_name: str, limit: int = 50) -> dict[str, Any]:
        """Trace likely definition and usage points for a service-like concept."""
        return hub.code.trace_service_usage(Path(project_root), service_name=service_name, limit=limit)

    @server.tool()
    def code_trace_model_usage(project_root: str, model_name: str, limit: int = 50) -> dict[str, Any]:
        """Trace likely definition and usage points for a DTO/model/entity-like concept."""
        return hub.code.trace_model_usage(Path(project_root), model_name=model_name, limit=limit)

    @server.tool()
    def code_find_mutation_points(project_root: str, concept: str, limit: int = 50) -> dict[str, Any]:
        """Find likely create/update/save/toggle/mutation points for a concept."""
        return hub.code.find_mutation_points(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_validation_surfaces(project_root: str, concept: str, limit: int = 50) -> dict[str, Any]:
        """Find likely validation logic, validators, required rules, and validation-related surfaces for a concept."""
        return hub.code.find_validation_surfaces(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_async_boundaries(project_root: str, concept: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Find likely async, background, deferred, or queued execution boundaries."""
        return hub.code.find_async_boundaries(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_hotspots(project_root: str, query: str | None = None, limit: int = 30) -> dict[str, Any]:
        """Find likely complexity hotspots using generic code-index signals."""
        return hub.code.find_hotspots(Path(project_root), query=query, limit=limit)

    @server.tool()
    def code_find_query_hotspots(project_root: str, query: str | None = None, limit: int = 30) -> dict[str, Any]:
        """Find likely query-complexity hotspots using generic query signals."""
        return hub.code.find_query_hotspots(Path(project_root), query=query, limit=limit)

    @server.tool()
    def code_trace_component_usage(project_root: str, component_name: str, limit: int = 50) -> dict[str, Any]:
        """Trace likely definition, references, and local frontend neighbors for a component-like symbol."""
        return hub.code.trace_component_usage(Path(project_root), component_name=component_name, limit=limit)

    @server.tool()
    def code_find_state_model_mismatch(project_root: str, concept: str, limit: int = 50) -> dict[str, Any]:
        """Find likely mixed or competing state-model representations for a concept."""
        return hub.code.find_state_model_mismatch(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_ui_backend_touchpoints(project_root: str, concept: str, limit: int = 50) -> dict[str, Any]:
        """Find likely UI/backend touchpoints for a concept across indexed code."""
        return hub.code.find_ui_backend_touchpoints(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_policy_surfaces(project_root: str, concept: str, limit: int = 50) -> dict[str, Any]:
        """Find likely policy/RBAC/guard enforcement surfaces for a concept."""
        return hub.code.find_policy_surfaces(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_domain_clusters(project_root: str, concept: str, limit: int = 50) -> dict[str, Any]:
        """Find a broader cross-layer domain cluster for a concept."""
        return hub.code.find_domain_clusters(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_duplicate_structures(
        project_root: str,
        role_filter: str | None = None,
        kind_filter: str | None = None,
        min_shared: int = 3,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Find files with overlapping outline symbols — candidates for extraction into shared partials/components.

        Groups files by shared symbol fingerprints (same symbol+kind in multiple files).
        Use role_filter to narrow to specific file types (e.g., 'page-view', 'partial-view').
        Use kind_filter to focus on specific symbol kinds (e.g., 'translation_key', 'js_function', 'partial_ref', 'api_call', 'form_handler').
        """
        return hub.code.find_duplicate_structures(
            Path(project_root),
            role_filter=role_filter,
            kind_filter=kind_filter,
            min_shared=min_shared,
            limit=limit,
        )

    @server.tool()
    def code_find_transition_points(project_root: str, concept: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Find likely migration seams, adapters, compatibility layers, and transition hotspots."""
        return hub.code.find_transition_points(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_entrypoints(project_root: str, concept: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Find likely startup, bootstrap, registration, or provider entrypoints."""
        return hub.code.find_entrypoints(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_find_routes(project_root: str, query: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Find likely route, endpoint, controller, and page entry surfaces."""
        return hub.code.find_routes(Path(project_root), query=query, limit=limit)

    @server.tool()
    def code_trace_api_to_ui(project_root: str, concept: str, limit: int = 50) -> dict[str, Any]:
        """Trace likely API-to-UI connection points for a concept."""
        return hub.code.trace_api_to_ui(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_get_outline(project_root: str, path: str) -> list[dict[str, Any]]:
        """Return a lightweight outline for a specific indexed code file."""
        return hub.code.get_outline(Path(project_root), path=path)

    @server.tool()
    def code_find_partial_group(project_root: str, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return all indexed partial definitions for a C# symbol."""
        return hub.code.find_partial_group(Path(project_root), symbol=symbol, limit=limit)

    @server.tool()
    def code_find_data_structures(project_root: str, query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return indexed DTO/model/enum/data-structure style symbols and members."""
        return hub.code.find_data_structures(Path(project_root), query=query, limit=limit)

    @server.tool()
    def code_find_frontend_symbols(
        project_root: str,
        query: str | None = None,
        kinds: tuple[str, ...] = ("component", "context_provider", "hook", "function", "initializer"),
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return indexed frontend-oriented symbols like components and hooks."""
        return hub.code.find_frontend_symbols(Path(project_root), query=query, kinds=kinds, limit=limit)

    @server.tool()
    def code_find_initializers(project_root: str, path: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return indexed JS/TS global initializer hooks and startup listeners."""
        return hub.code.find_initializers(Path(project_root), path=path, limit=limit)

    @server.tool()
    def code_get_symbol_snippet(
        project_root: str,
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        """Return an exact code snippet for an indexed outline symbol."""
        return hub.code.get_symbol_snippet(
            Path(project_root),
            path=path,
            symbol=symbol,
            kind=kind,
            line_number=line_number,
        )

    @server.tool()
    def code_get_symbol_bundle(
        project_root: str,
        symbol: str,
        path: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return a full symbol bundle: definitions, references, dependencies, partials, and schema hints."""
        return hub.code.get_symbol_bundle(
            Path(project_root),
            symbol=symbol,
            path=path,
            kind=kind,
            limit=limit,
        )

    @server.tool()
    def code_get_subsystem_bundle(project_root: str, concept: str, limit: int = 20) -> dict[str, Any]:
        """Return a broad subsystem bundle for a concept using multiple generic analyzers."""
        return hub.code.get_subsystem_bundle(Path(project_root), concept=concept, limit=limit)

    @server.tool()
    def code_get_partial_bundle(project_root: str, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return snippet bundles for all indexed partial definitions of a C# symbol."""
        return hub.code.get_partial_bundle(Path(project_root), symbol=symbol, limit=limit)

    @server.tool()
    def code_get_file_bundle(project_root: str, path: str) -> dict[str, Any]:
        """Return a targeted bundle for one indexed code file."""
        return hub.code.get_file_bundle(Path(project_root), path=path)

    @server.tool()
    def code_get_component_bundle(project_root: str, path: str, limit: int = 20) -> dict[str, Any]:
        """Return a frontend-oriented bundle for a component file and its imported neighbors."""
        return hub.code.get_component_bundle(Path(project_root), path=path, limit=limit)

    @server.tool()
    def code_get_service_bundle(project_root: str, path: str, limit: int = 20) -> dict[str, Any]:
        """Return a backend-oriented bundle for a service-like file and its related local neighbors."""
        return hub.code.get_service_bundle(Path(project_root), path=path, limit=limit)

    @server.tool()
    def code_get_query_bundle(project_root: str, path: str, limit: int = 20) -> dict[str, Any]:
        """Return a query-oriented bundle for a query-heavy file, including schema hints and dependencies."""
        return hub.code.get_query_bundle(Path(project_root), path=path, limit=limit)

    @server.tool()
    def code_trace_query_shape(project_root: str, path: str, limit: int = 20) -> dict[str, Any]:
        """Trace the likely shape of a query-heavy file across entities, fields, and relationships."""
        return hub.code.trace_query_shape(Path(project_root), path=path, limit=limit)

    @server.tool()
    def code_get_component_tree(project_root: str, path: str, depth: int = 2, limit: int = 50) -> dict[str, Any]:
        """Return a recursive local frontend import tree for a component/page/provider file."""
        return hub.code.get_component_tree(Path(project_root), path=path, depth=depth, limit=limit)

    @server.tool()
    def code_get_style_bundle(project_root: str, class_names: list[str], limit: int = 100) -> dict[str, Any]:
        """Return CSS selector matches for a set of class names."""
        return hub.code.get_style_bundle(Path(project_root), class_names=class_names, limit=limit)

    @server.tool()
    def code_get_session_bundle(project_root: str, session_id: str) -> dict[str, Any]:
        """Return a targeted code bundle guided by the selected session context."""
        return hub.code.get_session_code_bundle(Path(project_root), session_id=session_id)

    @server.tool()
    def code_get_context_bundle(
        project_root: str,
        session_id: str,
        include_dependencies: bool = True,
        include_styles: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return a ranked code bundle guided by session context."""
        return hub.code.get_context_bundle(
            Path(project_root),
            session_id=session_id,
            include_dependencies=include_dependencies,
            include_styles=include_styles,
            limit=limit,
        )

    @server.tool()
    def code_get_preset_bundle(project_root: str, preset: str, value: str, limit: int = 50) -> dict[str, Any]:
        """Return a higher-level bundle preset for common retrieval cases."""
        return hub.code.get_preset_bundle(Path(project_root), preset=preset, value=value, limit=limit)

    @server.tool()
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
                - 'workflow' → rules/workflow.md (git, deploy, task lifecycle, CI rules)
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

    @server.tool()
    def project_init(project_root: str) -> dict[str, Any]:
        """Initialize AIDOCS structure on a new project — creates .MEMORY/, AGENTS.md/CLAUDE.md, and templates.

        This is the MCP equivalent of running the install script's fix mode on a project.
        Safe to call on already-initialized projects (idempotent).
        After initialization, call project_bootstrap_or_resume to activate managed mode.
        """
        root = Path(project_root)
        fix_result = hub.updater.run_fix(root)
        if not fix_result.get("ok"):
            return {
                "initialized": False,
                "reason": fix_result.get("stderr") or "Fix script failed",
                "fix_result": fix_result,
            }
        return {
            "initialized": True,
            "fix_result": fix_result,
            "next_step": "Call project_bootstrap_or_resume to activate managed mode and select a session.",
        }

    @server.tool()
    def project_check(project_root: str) -> dict[str, Any]:
        """Run strict session-era structural check on a project."""
        return hub.updater.run_check(Path(project_root))

    @server.tool()
    def project_check_legacy(project_root: str) -> dict[str, Any]:
        """Run legacy-compatible structural check on a project."""
        return hub.updater.run_check_legacy(Path(project_root))

    @server.tool()
    def project_fix(project_root: str) -> dict[str, Any]:
        """Run safe deterministic structural fixes on a project."""
        return hub.updater.run_fix(Path(project_root))

    @server.tool()
    def project_inspect_legacy(project_root: str) -> dict[str, Any]:
        """Inspect whether legacy runtime files/folders are still present."""
        return hub.updater.inspect_legacy_runtime(Path(project_root))

    @server.tool()
    def project_sync_indexes(project_root: str, include_tests: bool = False) -> dict[str, Any]:
        """Refresh all derived indexes for a project in one call."""
        root = Path(project_root)
        capability_count = hub.capabilities.sync_capabilities(root, _registered_tools())
        workflow_sync = hub.workflow.compile_project_rules(root)
        procedure_count = hub.procedures.sync_procedures(root, hub.workflow.read_compiled(root))
        link_count = hub.procedure_links.sync_links(root, _all_procedures(root), _all_capabilities(root))
        return {
            "memory": hub.index.sync_all(root),
            "capabilities": {"capability_definitions": capability_count},
            "code_manifest": {"code_files": hub.code.sync_code_files(root, include_tests=include_tests)},
            "schema": hub.schema.sync_schema(root),
            "workflow": workflow_sync,
            "procedures": {"procedure_definitions": procedure_count},
            "procedure_capability_links": {"links": link_count},
            "execution": hub.execution.execution_status(root),
            "execution_pruning": hub.execution.prune_old_events(root),
        }

    @server.tool()
    def project_status(project_root: str) -> dict[str, Any]:
        """Return a consolidated status view for memory, code, and schema indexes."""
        root = Path(project_root)
        return {
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

    @server.tool()
    def capability_index_status(project_root: str) -> dict[str, Any]:
        """Return current MCP capability index status for a project."""
        return hub.capabilities.capability_status(Path(project_root))

    @server.tool()
    def capability_definitions_get(project_root: str, query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return indexed MCP capability definitions, optionally filtered by query."""
        return hub.capabilities.find_capabilities(Path(project_root), query=query, limit=limit)

    @server.tool()
    def procedure_index_status(project_root: str) -> dict[str, Any]:
        """Return current procedure-definition index status for a project."""
        return hub.procedures.procedure_status(Path(project_root))

    @server.tool()
    def procedure_definitions_get(project_root: str, query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return indexed procedure definitions, optionally filtered by query."""
        return hub.procedures.find_procedures(Path(project_root), query=query, limit=limit)

    @server.tool()
    def procedure_capability_link_status(project_root: str) -> dict[str, Any]:
        """Return current procedure-to-capability link status for a project."""
        return hub.procedure_links.link_status(Path(project_root))

    @server.tool()
    def procedure_capability_links_get(
        project_root: str,
        procedure_id: str | None = None,
        unresolved_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return indexed procedure-to-capability links, optionally filtered by procedure or unresolved status."""
        return hub.procedure_links.list_links(Path(project_root), procedure_id=procedure_id, unresolved_only=unresolved_only, limit=limit)

    @server.tool()
    def execution_index_status(project_root: str) -> dict[str, Any]:
        """Return current execution-evidence index status for a project."""
        return hub.execution.execution_status(Path(project_root))

    @server.tool()
    def execution_runs_get(project_root: str, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return indexed execution runs, optionally filtered by session."""
        return hub.execution.list_runs(Path(project_root), session_id=session_id, limit=limit)

    @server.tool()
    def execution_events_get(
        project_root: str,
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return indexed execution events, optionally filtered by query/session."""
        return hub.execution.list_events(Path(project_root), query=query, session_id=session_id, limit=limit)

    @server.tool()
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

    @server.tool()
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

    @server.tool()
    def execution_query_last(
        project_root: str,
        action_kind: str | None = None,
        capability_name: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Query: 'What actually ran last time?' — returns recent execution events matching filters."""
        return hub.execution.query_last_execution(
            Path(project_root), action_kind=action_kind, capability_name=capability_name, session_id=session_id, limit=limit,
        )

    @server.tool()
    def execution_query_summary(project_root: str, session_id: str | None = None) -> dict[str, Any]:
        """Query: 'What happened in this session?' — returns aggregate execution summary with ad-hoc vs procedure-linked breakdown."""
        return hub.execution.query_execution_summary(Path(project_root), session_id=session_id)

    @server.tool()
    def execution_query_compliance(project_root: str, session_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Query: 'Did execution follow the intended procedure?' — compares procedure-linked runs vs ad-hoc runs."""
        return hub.execution.query_procedure_compliance(Path(project_root), session_id=session_id, limit=limit)

    @server.tool()
    def execution_prune(project_root: str, max_age_days: int = 30, max_events: int = 10000) -> dict[str, Any]:
        """Prune old execution events by age and count. Runs automatically on project_sync_indexes."""
        return hub.execution.prune_old_events(Path(project_root), max_age_days=max_age_days, max_events=max_events)

    @server.tool()
    def action_surface_compare(
        project_root: str,
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Compare what should happen, what can happen, and what did happen for a query."""
        return hub.action_surface.compare(Path(project_root), query=query, session_id=session_id, limit=limit)

    @server.tool()
    def action_surface_assess(
        project_root: str,
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return an operator-facing assessment of the action surface for a query."""
        return hub.action_surface.assess(Path(project_root), query=query, session_id=session_id, limit=limit)

    @server.tool()
    def action_surface_status_bundle(
        project_root: str,
        queries: list[str],
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return an operator-facing multi-query status bundle over action surfaces."""
        return hub.action_surface.status_bundle(Path(project_root), queries=queries, session_id=session_id, limit=limit)

    @server.tool()
    def action_surface_session_bundle(
        project_root: str,
        session_id: str,
        limit: int = 20,
        max_queries: int = 12,
    ) -> dict[str, Any]:
        """Return a session-driven operator-facing action-surface status bundle."""
        return hub.action_surface.session_status_bundle(Path(project_root), session_id=session_id, limit=limit, max_queries=max_queries)

    @server.tool()
    def action_surface_current_session_bundle(
        project_root: str,
        limit: int = 20,
        max_queries: int = 12,
    ) -> dict[str, Any]:
        """Return a session-driven operator-facing action-surface bundle for the current managed or sole active session."""
        return hub.action_surface.current_session_bundle(Path(project_root), limit=limit, max_queries=max_queries)

    @server.tool()
    def workflow_actions_compile(project_root: str) -> dict[str, Any]:
        """Compile human-readable workflow rules into structured workflow actions."""
        return hub.workflow.compile_project_rules(Path(project_root))

    @server.tool()
    def workflow_actions_get(project_root: str) -> dict[str, Any] | None:
        """Read the compiled workflow action config for a project if present."""
        return hub.workflow.read_compiled(Path(project_root))

    @server.tool()
    def workflow_triggers_for_action(project_root: str, action_kind: str) -> dict[str, Any]:
        """Find workflow triggers that would fire after an action_kind completes."""
        triggers = hub.workflow.triggers_for_action_kind(action_kind)
        pending: list[dict[str, Any]] = []
        for trigger in triggers:
            pending.extend(hub.workflow.pending_actions_for_trigger(Path(project_root), trigger))
        return {
            "action_kind": action_kind,
            "triggers": triggers,
            "pending_actions": pending,
        }

    @server.tool()
    def project_status_model_get(project_root: str) -> dict[str, Any] | None:
        """Read the deterministic project status model if present."""
        return hub.project_status.read_model(Path(project_root))

    @server.tool()
    def project_status_evaluate(project_root: str) -> dict[str, Any]:
        """Evaluate the deterministic project status model."""
        return hub.project_status.evaluate(Path(project_root))

    @server.tool()
    def project_status_area_bundle(project_root: str, area_id: str, limit: int = 20) -> dict[str, Any]:
        """Return status details plus a subsystem bundle for one declared project-status area."""
        return hub.project_status.get_area_bundle(Path(project_root), area_id=area_id, limit=limit)

    @server.tool()
    def related_project_code_search(project_root: str, name: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search code in a configured related project using the same generic code index."""
        related_root = _resolve_related_root(project_root, name)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        return hub.code.search_code(related_root, query=query, limit=limit)

    @server.tool()
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
        return hub.code.get_symbol_bundle(related_root, symbol=symbol, path=path, kind=kind, limit=limit)

    @server.tool()
    def related_project_subsystem_bundle(project_root: str, name: str, concept: str, limit: int = 20) -> dict[str, Any]:
        """Build a broad subsystem bundle from a configured related project."""
        related_root = _resolve_related_root(project_root, name)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        hub.schema.sync_schema(related_root)
        return hub.code.get_subsystem_bundle(related_root, concept=concept, limit=limit)

    @server.tool()
    def related_project_compare_concept(project_root: str, name: str, concept: str, limit: int = 20) -> dict[str, Any]:
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
            "current": hub.code.get_subsystem_bundle(root, concept=concept, limit=limit),
            "related": hub.code.get_subsystem_bundle(related_root, concept=concept, limit=limit),
        }

    @server.tool()
    def legacy_read_runtime(project_root: str) -> dict[str, Any]:
        """Inspect legacy NOW/plans state without mutating the project."""
        return hub.legacy.inspect_legacy(Path(project_root))

    @server.tool()
    def legacy_build_session_proposal(project_root: str, session_id: str | None = None) -> dict[str, Any]:
        """Build a non-destructive session proposal from legacy NOW/plans state."""
        return hub.legacy.build_session_proposal(Path(project_root), session_id=session_id)

    return server


def main() -> None:
    server = create_server()
    server.run()


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
