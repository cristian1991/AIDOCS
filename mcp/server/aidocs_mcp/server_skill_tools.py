from __future__ import annotations

from pathlib import Path
from .mcp_server_runtime_helpers import resolve_project_root
from typing import Any


def register_skill_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    annotate_skill_result: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Skill Registry",
        }
    )
    def skill_registry_get(root: str = "") -> dict[str, Any]:
        """Return the available built-in + project-local skills."""
        return {"skills": hub.skills.list_skills(resolve_project_root(root))}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Skills",
        }
    )
    def session_skills_get(session_id: str, root: str = "") -> dict[str, Any]:
        """Return the selected skills for a session."""
        return hub.skills.get_selected_skills(resolve_project_root(root), session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Skill Trigger State",
        }
    )
    def skill_trigger_state_get(
        session_id: str,
        intent: str,
        workflow_state: str | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return the AIDOCS-native active skill trigger state for a session."""
        return annotate_skill_result(
            runtime.skill_trigger_state(
                resolve_project_root(root), session_id, intent, workflow_state
            ),
            override_store=runtime._skill_overrides,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Skill Override Registry",
        }
    )
    def skill_override_registry_get(root: str = "") -> dict[str, Any]:
        """Return the configured skill override rules for inspection/debugging."""
        _ = root
        return {
            "rules": [item.to_dict() for item in runtime._skill_overrides.list_rules()]
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Skill Provider Status",
        }
    )
    def skill_provider_status_get(
        provider_id: str, root: str = ""
    ) -> dict[str, Any]:
        """Return compatibility status and user choices for one external skill provider."""
        return runtime.skill_provider_status(resolve_project_root(root), provider_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Set Skill Provider Override",
        }
    )
    def skill_provider_override_set(
        provider_id: str, choice: str | None, root: str = ""
    ) -> dict[str, Any]:
        """Persist a user override choice for one external skill provider."""
        return runtime.set_skill_provider_override(
            resolve_project_root(root), provider_id, choice
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Set Session Skills",
        }
    )
    def session_skills_set(
        session_id: str, selected_skills: list[str], root: str = ""
    ) -> dict[str, Any]:
        """Set the selected skills for a session."""
        return runtime.set_session_skills(
            resolve_project_root(root), session_id, selected_skills
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Resume Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def session_resume_bundle(
        session_id: str,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        journal_last_n: int = 10,
        root: str = "",
    ) -> dict[str, Any]:
        """Return a collaboration-oriented resume bundle for a session."""
        return runtime.session_resume_bundle(
            resolve_project_root(root),
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
            journal_last_n=journal_last_n,
        )
