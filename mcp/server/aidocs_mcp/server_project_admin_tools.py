from __future__ import annotations

from pathlib import Path
from .mcp_server_runtime_helpers import resolve_project_root
from typing import Any


def register_project_admin_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_sync: Any,
    resolve_related_root: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Procedure Index Status",
        }
    )
    def procedure_index_status(root: str = "") -> dict[str, Any]:
        """Return current procedure-definition index status for a project."""
        return hub.procedures.procedure_status(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Procedure Definitions",
        },
        meta={"anthropic/searchHint": True},
    )
    def procedure_definitions_get(
        query: str | None = None, limit: int = 50, root: str = ""
    ) -> dict[str, Any]:
        """Return indexed procedure definitions, optionally filtered by query."""
        root = resolve_project_root(root)
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
    def procedure_capability_link_status(root: str = "") -> dict[str, Any]:
        """Return current procedure-to-capability link status for a project."""
        return hub.procedure_links.link_status(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Procedure Capability Links",
        }
    )
    def procedure_capability_links_get(
        procedure_id: str | None = None,
        unresolved_only: bool = False,
        limit: int = 50,
        root: str = "",
    ) -> dict[str, Any]:
        """Return indexed procedure-to-capability links, optionally filtered by procedure or unresolved status."""
        root = resolve_project_root(root)
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
    def execution_index_status(root: str = "") -> dict[str, Any]:
        """Return current execution-evidence index status for a project."""
        return hub.execution.execution_status(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Execution Runs",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_runs_get(
        session_id: str | None = None, limit: int = 50, root: str = ""
    ) -> dict[str, Any]:
        """Return indexed execution runs, optionally filtered by session."""
        root = resolve_project_root(root)
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
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        root: str = "",
    ) -> dict[str, Any]:
        """Return indexed execution events, optionally filtered by query/session."""
        root = resolve_project_root(root)
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
        root: str = "",
    ) -> dict[str, Any]:
        """Record or update an execution run for observed work."""
        resolved = hub.execution.record_run(
            resolve_project_root(root),
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
        root: str = "",
    ) -> dict[str, Any]:
        """Record one execution event for observed work."""
        resolved = hub.execution.record_event(
            resolve_project_root(root),
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
        action_kind: str | None = None,
        capability_name: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
        root: str = "",
    ) -> dict[str, Any]:
        """Query: 'What actually ran last time?' — returns recent execution events matching filters."""
        root = resolve_project_root(root)
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
        session_id: str | None = None, root: str = ""
    ) -> dict[str, Any]:
        """Query: 'What happened in this session?' — returns aggregate execution summary with ad-hoc vs procedure-linked breakdown."""
        return hub.execution.query_execution_summary(resolve_project_root(root), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Dashboard Snapshot",
        },
        meta={"anthropic/searchHint": True},
    )
    def dashboard_snapshot(
        session_id: str | None = None,
        event_limit: int = 12,
        root: str = "",
    ) -> dict[str, Any]:
        """Return an operator-friendly dashboard snapshot for sessions, conductor state, config, execution, and usage proxies."""
        root = resolve_project_root(root)
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
        session_id: str | None = None, limit: int = 20, root: str = ""
    ) -> dict[str, Any]:
        """Query: 'Did execution follow the intended procedure?' — compares procedure-linked runs vs ad-hoc runs."""
        return hub.execution.query_procedure_compliance(
            resolve_project_root(root), session_id=session_id, limit=limit
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Prune Execution Events",
        }
    )
    def execution_prune(
        max_age_days: int = 30, max_events: int = 10000, root: str = ""
    ) -> dict[str, Any]:
        """Prune old execution events by age and count. Runs automatically on project_sync_indexes."""
        return hub.execution.prune_old_events(
            resolve_project_root(root), max_age_days=max_age_days, max_events=max_events
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
        query: str,
        session_id: str | None = None,
        limit: int = 20,
        root: str = "",
    ) -> dict[str, Any]:
        """Compare what should happen, what can happen, and what did happen for a query."""
        return hub.action_surface.compare(
            resolve_project_root(root), query=query, session_id=session_id, limit=limit
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
        query: str,
        session_id: str | None = None,
        limit: int = 20,
        root: str = "",
    ) -> dict[str, Any]:
        """Return an operator-facing assessment of the action surface for a query."""
        return hub.action_surface.assess(
            resolve_project_root(root), query=query, session_id=session_id, limit=limit
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
        queries: list[str],
        session_id: str | None = None,
        limit: int = 20,
        root: str = "",
    ) -> dict[str, Any]:
        """Return an operator-facing multi-query status bundle over action surfaces."""
        return hub.action_surface.status_bundle(
            resolve_project_root(root), queries=queries, session_id=session_id, limit=limit
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
        session_id: str,
        limit: int = 20,
        max_queries: int = 12,
        root: str = "",
    ) -> dict[str, Any]:
        """Return a session-driven operator-facing action-surface status bundle."""
        return hub.action_surface.session_status_bundle(
            resolve_project_root(root),
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
        limit: int = 20,
        max_queries: int = 12,
        root: str = "",
    ) -> dict[str, Any]:
        """Return a session-driven operator-facing action-surface bundle for the current managed or sole active session."""
        return hub.action_surface.current_session_bundle(
            resolve_project_root(root), limit=limit, max_queries=max_queries
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Compile Workflow Actions",
        }
    )
    def workflow_actions_compile(root: str = "") -> dict[str, Any]:
        """Compile human-readable workflow rules into the runtime workflow artifact."""
        return hub.workflow.compile_project_rules(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Workflow Actions",
        },
        meta={"anthropic/searchHint": True},
    )
    def workflow_actions_get(root: str = "") -> dict[str, Any] | None:
        """Read the compiled runtime workflow artifact for a project if present."""
        return hub.workflow.read_compiled(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Workflow Triggers",
        },
        meta={"anthropic/searchHint": True},
    )
    def workflow_triggers_for_action(action_kind: str, root: str = "") -> dict[str, Any]:
        """Find workflow triggers that would fire after an action_kind completes."""
        triggers = hub.workflow.triggers_for_action_kind(action_kind)
        pending: list[dict[str, Any]] = []
        for trigger in triggers:
            pending.extend(
                hub.workflow.pending_actions_for_trigger(resolve_project_root(root), trigger)
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
    def project_status_model_get(root: str = "") -> dict[str, Any] | None:
        """Read the deterministic project status model if present."""
        return hub.project_status.read_model(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Evaluate Project Status",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_status_evaluate(root: str = "") -> dict[str, Any]:
        """Evaluate the deterministic project status model."""
        return hub.project_status.evaluate(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Status Area Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_status_area_bundle(
        area_id: str, limit: int = 20, root: str = ""
    ) -> dict[str, Any]:
        """Return status details plus a subsystem bundle for one declared project-status area."""
        return hub.project_status.get_area_bundle(
            resolve_project_root(root), area_id=area_id, limit=limit
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
        name: str, query: str, limit: int = 10, root: str = ""
    ) -> list[dict[str, Any]]:
        """Search code in a configured related project using the same generic code index."""
        related_root = resolve_related_root(root, name)
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
        name: str,
        symbol: str,
        path: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        root: str = "",
    ) -> dict[str, Any]:
        """Build a symbol bundle from a configured related project."""
        related_root = resolve_related_root(root, name)
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
        name: str, concept: str, limit: int = 20, root: str = ""
    ) -> dict[str, Any]:
        """Build a broad subsystem bundle from a configured related project."""
        related_root = resolve_related_root(root, name)
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
        name: str, concept: str, limit: int = 20, root: str = ""
    ) -> dict[str, Any]:
        """Compare a concept between the current project and a configured related project."""
        root = resolve_project_root(root)
        related_root = resolve_related_root(root, name)
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

