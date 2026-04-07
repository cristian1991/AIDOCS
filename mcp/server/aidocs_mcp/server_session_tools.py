from __future__ import annotations

from .mcp_server_runtime_helpers import resolve_project_root
from typing import Any


def register_session_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_sync: Any,
    annotate_skill_result: Any,
    session_summary_to_dict: Any,
    coerce_to_list: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "List Sessions",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def session_list(root: str = "") -> list[dict[str, Any]]:
        """List sessions from project-local /.MEMORY/sessions/."""
        summaries = hub.sessions.list_sessions(resolve_project_root(root))
        return [session_summary_to_dict(item) for item in summaries]

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read Session",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def session_read(session_id: str, root: str = "") -> dict[str, Any]:
        """Read a single session router file and return its parsed sections."""
        session = hub.sessions.read_session(resolve_project_root(root), session_id)
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Select Session",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def session_select(session_id: str, root: str = "") -> dict[str, Any]:
        """Select an existing session and return its summary."""
        session = hub.sessions.select_session(resolve_project_root(root), session_id)
        return session_summary_to_dict(session)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Start Session",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def session_start(
        session_id: str | None = None,
        include_code_bundle: bool = False,
        sync_indexes: bool = True,
        include_tests: bool = False,
        root: str = "",
    ) -> dict[str, Any]:
        """Run the startup/session-selection flow and return ready context."""
        return runtime.session_start(
            resolve_project_root(root),
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            sync_indexes=sync_indexes,
            include_tests=include_tests,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Start State",
        }
    )
    def session_start_state_get(
        session_id: str | None = None, root: str = ""
    ) -> dict[str, Any]:
        """Return lightweight startup readiness and imported skill state for a session."""
        return annotate_skill_result(
            runtime.session_start_state(resolve_project_root(root), session_id=session_id),
            override_store=runtime._skill_overrides,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Bootstrap Project",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def project_bootstrap_or_resume(
        session_id: str | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        timeout: int | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Run the mandatory project setup/index/session bootstrap flow."""
        return runtime.project_bootstrap_or_resume(
            resolve_project_root(root),
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Orchestrate AIDOCS",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def aidocs_orchestrate(
        user_request: str,
        action_kind: str = "understand",
        session_id: str | None = None,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        root: str = "",
    ) -> dict[str, Any]:
        """Run the AIDOCS bootstrap/session/retrieval flow as one high-level entrypoint."""
        return runtime.aidocs_orchestrate(
            resolve_project_root(root),
            user_request=user_request,
            action_kind=action_kind,
            session_id=session_id,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "Get Mode"}
    )
    def aidocs_mode_get(root: str = "") -> dict[str, Any]:
        """Read the current runtime/session-binding AIDOCS-managed mode state."""
        return hub.managed_mode.get_mode(resolve_project_root(root))

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Set Mode",
        }
    )
    def aidocs_mode_set(
        session_id: str, source: str = "/aidocs", root: str = ""
    ) -> dict[str, Any]:
        """Set runtime/session-binding AIDOCS-managed mode for a selected session."""
        return hub.managed_mode.set_mode(
            resolve_project_root(root), session_id=session_id, source=source
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Clear Mode",
        }
    )
    def aidocs_mode_clear(root: str = "") -> dict[str, Any]:
        """Clear the current runtime/session-binding AIDOCS-managed mode state."""
        return hub.managed_mode.clear_mode(resolve_project_root(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Route Prompt",
        }
    )
    def aidocs_route_prompt(
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return the deterministic MCP routing decision for a normal user prompt."""
        return annotate_skill_result(
            runtime.aidocs_route_prompt(
                resolve_project_root(root),
                user_request=user_request,
                action_kind=action_kind,
                explicit_targets=explicit_targets,
            ),
            override_store=runtime._skill_overrides,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Classify Prompt",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def aidocs_classify_prompt(
        user_request: str, explicit_targets: list[str] | None = None
    ) -> dict[str, Any]:
        """Classify a normal prompt into a deterministic AIDOCS action kind."""
        return runtime.classify_prompt_action(
            user_request, explicit_targets=explicit_targets
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Handle Prompt",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def aidocs_handle_prompt(
        user_request: str,
        action_kind: str = "auto",
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        root: str = "",
    ) -> dict[str, Any]:
        """Handle a normal user prompt through the MCP-first routing/orchestration flow."""
        return runtime.aidocs_handle_prompt(
            resolve_project_root(root),
            user_request=user_request,
            action_kind=action_kind,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Create Session",
        }
    )
    def session_create(
        title: str,
        goal: str = "",
        session_id: str = "",
        owner: str = "",
        scope: str = "-",
        status: str = "active",
        predecessor_session_id: str | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Create a new session. Only title is required — ID, owner, date auto-generated."""
        import re as _re
        from datetime import date as _date

        # Auto-generate session_id from date + slugified title
        if not session_id:
            slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
            session_id = f"{_date.today().isoformat()}-{slug}"

        # Auto-detect owner — the agent/host calling this tool
        if not owner:
            try:
                managed = hub.managed_mode.get_mode(resolve_project_root(root))
                # Use host identity from managed mode if available
                owner = str(managed.get("source") or "").strip() or "agent"
            except Exception:
                owner = "agent"

        session = hub.sessions.create_session(
            resolve_project_root(root),
            session_id=session_id,
            title=title,
            owner=owner,
            goal=goal or title,
            scope=scope,
            status=status,
            predecessor_session_id=predecessor_session_id,
        )
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Claim Status",
        }
    )
    def session_claim_status(
        session_id: str, stale_after_minutes: int = 30, root: str = ""
    ) -> dict[str, Any]:
        """List advisory session claims and whether they are stale."""
        claims = hub.sessions.list_claims(
            resolve_project_root(root), session_id, stale_after_minutes=stale_after_minutes
        )
        return {"session_id": session_id, "claims": claims}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Claim Session",
        }
    )
    def session_claim(
        session_id: str,
        agent_id: str,
        run_id: str,
        mode: str = "active",
        root: str = "",
    ) -> dict[str, Any]:
        """Add or refresh an advisory agent claim on a session."""
        session = hub.sessions.claim_session(
            resolve_project_root(root), session_id, agent_id=agent_id, run_id=run_id, mode=mode
        )
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Release Session",
        }
    )
    def session_release(
        session_id: str, agent_id: str, run_id: str | None = None, root: str = ""
    ) -> dict[str, Any]:
        """Release one advisory agent claim from a session."""
        session = hub.sessions.release_claim(
            resolve_project_root(root), session_id, agent_id=agent_id, run_id=run_id
        )
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Prune Stale Claims",
        }
    )
    def session_prune_stale_claims(
        session_id: str, stale_after_minutes: int = 30, root: str = ""
    ) -> dict[str, Any]:
        """Remove stale advisory claims from a session."""
        session = hub.sessions.prune_stale_claims(
            resolve_project_root(root), session_id, stale_after_minutes=stale_after_minutes
        )
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Session Handoff",
        }
    )
    def session_handoff_get(session_id: str, root: str = "") -> dict[str, Any]:
        """Read the structured collaboration handoff for a session."""
        handoff = hub.sessions.read_handoff(resolve_project_root(root), session_id)
        return {
            "session_id": handoff.session_id,
            "path": str(handoff.path),
            "sections": handoff.sections,
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Session Handoff",
        }
    )
    def session_handoff_update(
        session_id: str,
        purpose: list[str] | None = None,
        current_state: list[str] | None = None,
        what_was_done: list[str] | None = None,
        what_failed: list[str] | None = None,
        what_matters_now: list[str] | None = None,
        open_questions: list[str] | None = None,
        risks_and_blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        estimated_effort: list[str] | None = None,
        suggested_next_steps: list[str] | None = None,
        related_sessions: list[str] | None = None,
        related_project_links: list[str] | None = None,
        freshness: list[str] | None = None,
        append: bool = False,
        root: str = "",
    ) -> dict[str, Any]:
        """Update the structured collaboration handoff for a session."""
        purpose = coerce_to_list(purpose)
        current_state = coerce_to_list(current_state)
        what_was_done = coerce_to_list(what_was_done)
        what_failed = coerce_to_list(what_failed)
        what_matters_now = coerce_to_list(what_matters_now)
        open_questions = coerce_to_list(open_questions)
        risks_and_blockers = coerce_to_list(risks_and_blockers)
        relevant_files = coerce_to_list(relevant_files)
        estimated_effort = coerce_to_list(estimated_effort)
        suggested_next_steps = coerce_to_list(suggested_next_steps)
        related_sessions = coerce_to_list(related_sessions)
        related_project_links = coerce_to_list(related_project_links)
        freshness = coerce_to_list(freshness)
        patch: dict[str, list[str]] = {}
        if purpose is not None:
            patch["Purpose"] = runtime._as_bullets(purpose)
        if current_state is not None:
            patch["Current State"] = runtime._as_bullets(current_state)
        if what_was_done is not None:
            patch["What Was Done"] = runtime._as_bullets(what_was_done)
        if what_failed is not None:
            patch["What Failed / Dead Ends"] = runtime._as_bullets(what_failed)
        if what_matters_now is not None:
            patch["What Matters Now"] = runtime._as_bullets(what_matters_now)
        if open_questions is not None:
            patch["Open Questions"] = runtime._as_bullets(open_questions)
        if risks_and_blockers is not None:
            patch["Risks and Blockers"] = runtime._as_bullets(risks_and_blockers)
        if relevant_files is not None:
            patch["Relevant Files"] = runtime._as_file_bullets(relevant_files)
        if estimated_effort is not None:
            patch["Estimated Effort"] = runtime._as_bullets(estimated_effort)
        if suggested_next_steps is not None:
            patch["Suggested Next Steps"] = runtime._as_bullets(suggested_next_steps)
        if related_sessions is not None:
            patch["Related Sessions"] = runtime._as_bullets(related_sessions)
        if related_project_links is not None:
            normalized = []
            for item in related_project_links:
                text = item.strip()
                if not text:
                    continue
                normalized.append(text)
            patch["Related Project Links"] = runtime._as_bullets(normalized)
        if freshness is not None:
            patch["Freshness"] = runtime._as_bullets(freshness)
        handoff = hub.sessions.update_handoff(
            resolve_project_root(root), session_id, patch, append=append
        )
        return {
            "session_id": handoff.session_id,
            "path": str(handoff.path),
            "sections": handoff.sections,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Handoff Steps",
        }
    )
    def session_handoff_steps_get(session_id: str, root: str = "") -> dict[str, Any]:
        """Read structured handoff steps for a session."""
        return {
            "session_id": session_id,
            "steps": hub.sessions.read_handoff_steps(resolve_project_root(root), session_id),
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Normalize Handoff Steps",
        }
    )
    def session_handoff_steps_normalize(
        session_id: str, root: str = ""
    ) -> dict[str, Any]:
        """Normalize legacy/drifted handoff step markers into canonical step states."""
        return hub.sessions.normalize_handoff_steps(resolve_project_root(root), session_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Handoff Step",
        }
    )
    def session_handoff_step_update(
        session_id: str,
        step_id: str | None = None,
        text: str | None = None,
        status: str = "open",
        append: bool = True,
        root: str = "",
    ) -> dict[str, Any]:
        """Create or update one structured handoff step."""
        handoff = hub.sessions.upsert_handoff_step(
            resolve_project_root(root),
            session_id,
            step_id=step_id,
            text=text,
            status=status,
            append=append,
        )
        return {
            "session_id": handoff.session_id,
            "path": str(handoff.path),
            "sections": handoff.sections,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Compliance",
        },
        meta={"anthropic/searchHint": True},
    )
    def session_compliance_get(root: str, session_id: str) -> dict[str, Any]:
        """Return task/logging debt and actionable continuity state for a session."""
        return runtime.session_compliance_summary(resolve_project_root(root), session_id)
