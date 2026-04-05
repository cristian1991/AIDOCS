from __future__ import annotations

from pathlib import Path
from .mcp_server_runtime_helpers import resolve_project_root
from typing import Any


def register_plan_task_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_sync: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Connect Plan",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def plan_connect(
        session_id: str,
        run_preflight: bool = True,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Connect to an existing session plan and surface next decisions."""
        return runtime.plan_connect(
            resolve_project_root(root), session_id=session_id, run_preflight=run_preflight
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Create Plan From Spec",
        }
    )
    @timed_sync
    def plan_create_from_spec(
        session_id: str,
        spec_text: str,
        scope: str | None = None,
        constraints: list[str] | None = None,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Create or replace the session plan from a deterministic spec format."""
        return runtime.plan_create_from_spec(
            resolve_project_root(root),
            session_id=session_id,
            spec_text=spec_text,
            scope=scope,
            constraints=constraints,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Validate Plan",
        }
    )
    @timed_sync
    def plan_validate(
        session_id: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Validate that the session plan is executable and has real verification steps."""
        return runtime.plan_validate(resolve_project_root(root), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Conductor Graph",
        }
    )
    @timed_sync
    def plan_conductor_graph(
        session_id: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return the conductor lane graph for a lane-aware session plan."""
        return runtime.plan_conductor_graph(resolve_project_root(root), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Conductor Status",
        }
    )
    @timed_sync
    def plan_conductor_status(
        session_id: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return the conductor graph plus runnable lane status for a lane-aware session plan."""
        return runtime.plan_conductor_status(resolve_project_root(root), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Mode Select",
        }
    )
    @timed_sync
    def execution_mode_select(
        session_id: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return the runtime-owned execution mode selection for a session plan."""
        return runtime.execution_mode_select(resolve_project_root(root), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Dispatch Next Lane",
        }
    )
    @timed_sync
    def plan_dispatch_next(
        session_id: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return the next delegated lane task packet for a session plan."""
        return runtime.plan_dispatch_next(resolve_project_root(root), session_id=session_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Report Dispatch Result",
        }
    )
    @timed_sync
    def plan_dispatch_report(
        session_id: str,
        packet_result: dict[str, Any],
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Ingest one delegated lane result and update conductor state."""
        return runtime.plan_dispatch_report(
            resolve_project_root(root), session_id=session_id, packet_result=packet_result
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Loop Next",
        }
    )
    @timed_sync
    def execution_loop_next(
        session_id: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return the next execution-loop state for a session plan."""
        return runtime.execution_loop_next(resolve_project_root(root), session_id=session_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Report Lane Overlap",
        }
    )
    @timed_sync
    def plan_conductor_report_inflight_overlap(
        session_id: str,
        paused_lane_id: str,
        conflicting_lane_id: str,
        file_path: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Pause a lane when another in-flight lane reports emergent file overlap."""
        return runtime.plan_conductor_report_inflight_overlap(
            resolve_project_root(root),
            session_id=session_id,
            paused_lane_id=paused_lane_id,
            conflicting_lane_id=conflicting_lane_id,
            file_path=file_path,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Resume Lane",
        }
    )
    @timed_sync
    def plan_conductor_resume_lane(
        session_id: str,
        lane_id: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Resume a paused lane after explicit user override or conflict resolution."""
        return runtime.plan_conductor_resume_lane(
            resolve_project_root(root), session_id=session_id, lane_id=lane_id
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Mark Contract Ready",
        }
    )
    @timed_sync
    def plan_conductor_mark_contract_ready(
        session_id: str,
        lane_id: str,
        ready: bool = True,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Mark a contract lane ready so compatible dependent lanes can run."""
        return runtime.plan_conductor_mark_contract_ready(
            resolve_project_root(root),
            session_id=session_id,
            lane_id=lane_id,
            ready=ready,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Record Lane Signal",
        }
    )
    @timed_sync
    def plan_conductor_record_lane_signal(
        session_id: str,
        lane_id: str,
        signal_kind: str,
        target_lane_id: str,
        detail: str = "",
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Record a structured signal from one lane about another lane."""
        return runtime.plan_conductor_record_lane_signal(
            resolve_project_root(root),
            session_id=session_id,
            lane_id=lane_id,
            signal_kind=signal_kind,
            target_lane_id=target_lane_id,
            detail=detail,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Plan Preflight",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def plan_preflight(
        session_id: str,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Analyze a session plan before implementation."""
        return runtime.plan_preflight(resolve_project_root(root), session_id=session_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Begin Task",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def task_begin(
        session_id: str,
        goal: str | None = None,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = True,
        include_tests: bool = False,
        root: str = "",
    ) -> dict[str, Any]:
        """Begin work in a selected session and update session/context state."""
        return runtime.task_begin(
            resolve_project_root(root),
            session_id=session_id,
            goal=goal,
            state=state,
            upcoming=upcoming,
            partial_goals=partial_goals,
            end_goal=end_goal,
            blockers=blockers,
            relevant_files=relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=relevant_snippets,
            session_facts=session_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Task",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def task_update(
        session_id: str,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        root: str = "",
    ) -> dict[str, Any]:
        """Update an active task session and optional context state."""
        return runtime.task_update(
            resolve_project_root(root),
            session_id=session_id,
            state=state,
            upcoming=upcoming,
            partial_goals=partial_goals,
            end_goal=end_goal,
            blockers=blockers,
            relevant_files=relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=relevant_snippets,
            session_facts=session_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Complete Task",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def task_complete(
        session_id: str,
        result_summary: str,
        next_status: str = "done",
        verification_evidence: dict[str, Any] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        root: str = "",
    ) -> dict[str, Any]:
        """Complete task work in a session and update session state."""
        return runtime.task_complete(
            resolve_project_root(root),
            session_id=session_id,
            result_summary=result_summary,
            next_status=next_status,
            verification_evidence=verification_evidence,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Verification Gate",
        }
    )
    @timed_sync
    def verification_gate(
        session_id: str,
        lane_id: str | None = None,
        verification_evidence: dict[str, Any] | None = None,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return runtime-owned verification status for a session or lane."""
        return runtime.verification_gate(
            resolve_project_root(root),
            session_id=session_id,
            lane_id=lane_id,
            verification_evidence=verification_evidence,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Roadmap Feedback",
        }
    )
    def roadmap_feedback_update(
        step_text: str,
        feedback: str,
        root: str = "",
    ) -> dict[str, Any]:
        """Update a pending roadmap step after user feedback."""
        return runtime.update_roadmap_feedback_state(
            resolve_project_root(root),
            step_text=step_text,
            feedback=feedback,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "List Planning Docs",
        },
    )
    def planning_docs_list(root: str) -> dict[str, Any]:
        """List all planning documents (roadmaps, plans, specs) with checkbox status summary."""
        docs = hub.sessions.list_planning_docs(resolve_project_root(root))
        return {"docs": docs, "total": len(docs)}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Mark Planning Step",
        },
    )
    def planning_step_mark(
        path: str,
        line_number: int,
        status: str = "done",
        root: str = "",
    ) -> dict[str, Any]:
        """Toggle a checkbox in a planning doc. Status: done, open, skip, in_progress, blocked."""
        return hub.sessions.mark_planning_step(resolve_project_root(root), path, line_number, status)



    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Normalize Plan Prose",
        }
    )
    def plan_normalize_prose(
        session_id: str,
        root: str = "",
    ) -> dict[str, Any]:
        """Preserve prose-only plan additions and append normalized steps awaiting feedback."""
        return hub.sessions.normalize_plan_feedback_sections(
            resolve_project_root(root), session_id=session_id
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Normalize Session Artifacts",
        }
    )
    def session_artifacts_normalize(
        session_id: str,
        root: str = "",
    ) -> dict[str, Any]:
        """Normalize explicit session artifacts and report changed vs untouched items."""
        return hub.sessions.normalize_session_artifacts(
            resolve_project_root(root), session_id=session_id
        )
