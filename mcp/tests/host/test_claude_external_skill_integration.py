import asyncio
import json
from pathlib import Path

from aidocs_mcp.mcp_server import create_server
from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text("# Context\n", encoding="utf-8")
    (root.parent / "index.aidocs").write_text(
        "# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n", encoding="utf-8"
    )
    (root.parent / "global-instructions.aidocs").write_text(
        "# Global Instructions\n", encoding="utf-8"
    )
    (root.parent / "coding-standards.aidocs").write_text(
        "# Coding Standards\n", encoding="utf-8"
    )
    (root.parent / "memory-system.aidocs").write_text(
        "# Memory System\n", encoding="utf-8"
    )
    (root.parent / "research-safety.aidocs").write_text(
        "# Research Safety\n", encoding="utf-8"
    )
    (root.parent / "personalities").mkdir(parents=True, exist_ok=True)
    (root.parent / "personalities" / "default.aidocs").write_text(
        "# Default Personality\n", encoding="utf-8"
    )
    memory_template = root / "memory"
    memory_template.mkdir(parents=True, exist_ok=True)
    (memory_template / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (memory_template / "rules").mkdir(parents=True, exist_ok=True)
    (memory_template / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n## Workflow Rules\n", encoding="utf-8"
    )
    (memory_template / "rules" / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n## Workflow Actions\n", encoding="utf-8"
    )


def _seed_project(project_root: Path) -> None:
    mem = project_root / ".MEMORY"
    (mem / ".aidocs").mkdir(parents=True, exist_ok=True)
    for name in [
        "index.aidocs",
        "global-instructions.aidocs",
        "coding-standards.aidocs",
        "memory-system.aidocs",
    ]:
        (mem / ".aidocs" / name).write_text(f"# {name}\n", encoding="utf-8")
    (mem / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")


def _register_superpowers_provider(
    runtime: RuntimeService, project_root: Path, provider_root: Path
) -> None:
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        json.dumps({"provider_id": "superpowers_external", "version": "5.1.0"}) + "\n",
        encoding="utf-8",
    )
    skills = {
        "brainstorming": "Imported brainstorming skill.",
        "writing-plans": "Imported planning skill.",
    }
    for skill_name, description in skills.items():
        (provider_root / "skills" / skill_name).mkdir(parents=True, exist_ok=True)
        (provider_root / "skills" / skill_name / "SKILL.md").write_text(
            "---\n"
            f"name: {skill_name}\n"
            f"description: {description}\n"
            "tags: external, provider\n"
            "---\n",
            encoding="utf-8",
        )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )


def _make_runtime_with_selected_superpowers(
    tmp_path: Path,
) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "session-a", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "superpowers-external"
    )
    runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/brainstorming"]
    )
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    return runtime, project_root, session.session_id


def _make_runtime_with_missing_provider(
    tmp_path: Path,
) -> tuple[RuntimeService, Path, str]:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )
    provider_root = tmp_path / "superpowers-external"
    for file in sorted(provider_root.rglob("*"), reverse=True):
        if file.is_file():
            file.unlink()
        elif file.is_dir():
            file.rmdir()
    provider_root.rmdir()
    return runtime, project_root, session_id


def _make_runtime_with_selected_bundled_skill(
    tmp_path: Path, name: str, skill_id: str
) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / f"templates-{name}"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / f"project-{name}"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, f"session-{name}", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    runtime.hub.skills.set_selected_skills(project_root, session.session_id, [skill_id])
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    return runtime, project_root, session.session_id


def test_claude_uses_bundled_skills_without_external_plugin(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_bundled_skill(
        tmp_path,
        "bundled-claude",
        "deep-retrieval",
    )

    startup = runtime.session_start_state(project_root, session_id=session_id)
    host_state = runtime.host_state(project_root, session_id=session_id)

    assert startup["active_skills"] == ["deep-retrieval"]
    assert startup["imported_skill_state"]["active_skills"] == ["deep-retrieval"]
    assert host_state["skill_state"]["session_snapshot"]["selected_skills"] == [
        "deep-retrieval"
    ]
    assert host_state["skill_state"]["session_snapshot"]["active_skills"] == [
        "deep-retrieval"
    ]
    assert "aidocs_bundled_superpowers/deep-retrieval" not in json.dumps(host_state)


def test_host_views_show_canonical_bundled_skill_names(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        runtime = RuntimeService(hub)
        project_root = tmp_path / "project-host-view-bundled"
        runtime.project_init(project_root, init_git=False, create_remote=False)
        session = hub.sessions.create_session(
            project_root, "session-host-view-bundled", "A", "Agent", "Goal A"
        )
        hub.managed_mode.set_mode(project_root, session.session_id)
        hub.skills.set_selected_skills(
            project_root,
            session.session_id,
            ["deep-retrieval", "test-driven-validation"],
        )
        runtime.project_bootstrap_or_resume(
            project_root,
            session_id=session.session_id,
            include_code_bundle=False,
            include_tests=False,
        )
        host_state = runtime.host_state(project_root, session_id=session.session_id)
        tool_result = await server.call_tool(
            "session_start_state_get",
            {
                "root": str(project_root),
                "session_id": session.session_id,
            },
        )
        payload = (
            tool_result[0].text
            if isinstance(tool_result, list)
            else tool_result.content[0].text
        )
        return host_state, json.loads(payload)

    host_state, session_start = asyncio.run(run())

    assert session_start["active_skills"] == [
        "deep-retrieval",
        "test-driven-validation",
    ]
    assert session_start["imported_skill_state"]["selected_skills"] == [
        "deep-retrieval",
        "test-driven-validation",
    ]
    assert host_state["skill_state"]["session_snapshot"]["selected_skills"] == [
        "deep-retrieval",
        "test-driven-validation",
    ]
    assert host_state["skill_state"]["session_snapshot"]["active_skills"] == [
        "deep-retrieval",
        "test-driven-validation",
    ]
    assert "aidocs_bundled_superpowers/deep-retrieval" not in json.dumps(host_state)
    assert "aidocs_bundled_superpowers/test-driven-validation" not in json.dumps(
        host_state
    )
    assert "aidocs_bundled_superpowers/deep-retrieval" not in json.dumps(session_start)
    assert "aidocs_bundled_superpowers/test-driven-validation" not in json.dumps(
        session_start
    )


def test_claude_startup_context_can_include_imported_skill_state(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )

    result = runtime.session_start_state(project_root, session_id=session_id)

    assert "superpowers_external/brainstorming" in result.get("active_skills", [])


def test_claude_startup_context_uses_override_resolved_active_skills(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates-override"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-override"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "session-override", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "superpowers-external-override"
    )
    runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/writing-plans"]
    )
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )

    result = runtime.session_start_state(project_root, session_id=session.session_id)

    assert result["active_skills"] == []
    assert result["imported_skill_state"]["active_skills"] == []
    assert result["runtime_owned_capabilities"][0]["capability_id"] == "planning"


def test_claude_runtime_state_can_include_override_mode_metadata(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        runtime = RuntimeService(hub)
        project_root = tmp_path / "project-claude-override-metadata"
        runtime.project_init(project_root, init_git=False, create_remote=False)
        session = hub.sessions.create_session(
            project_root, "session-override-metadata", "A", "Agent", "Goal A"
        )
        hub.managed_mode.set_mode(project_root, session.session_id)
        _register_superpowers_provider(
            runtime, project_root, tmp_path / "superpowers-external-override-metadata"
        )
        hub.skills.set_selected_skills(
            project_root, session.session_id, ["superpowers_external/writing-plans"]
        )
        runtime.project_bootstrap_or_resume(
            project_root,
            session_id=session.session_id,
            include_code_bundle=False,
            include_tests=False,
        )
        result = await server.call_tool(
            "session_start_state_get",
            {
                "root": str(project_root),
                "session_id": session.session_id,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(payload)

    tool_names, result = asyncio.run(run())

    assert "session_start_state_get" in tool_names
    assert result["imported_skill_state"]["mode_metadata"]["selected_skill_modes"] == {
        "superpowers_external/writing-plans": "runtime_owned"
    }


def test_missing_external_provider_degrades_gracefully(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_missing_provider(tmp_path)

    result = runtime.skill_trigger_state(project_root, session_id, intent="planning")

    assert result["active_skills"] == []
    assert result["provider_state"] == "missing"
    assert result["imported_skill_state"]["provider_state"] == "missing"


def test_claude_hook_and_opencode_share_same_prompt_level_skill_decision_source(
    tmp_path: Path,
) -> None:
    from aidocs_mcp.claude_hook import ClaudeHookHandler

    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )
    handler = ClaudeHookHandler()
    handler.runtime.hub = runtime.hub
    handler.runtime = runtime

    prompt = "write the plan for the startup flow before I change code"
    host_state = runtime.host_state(
        project_root, session_id=session_id, prompt_text=prompt
    )

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": prompt,
        }
    )

    assert host_state["prompt_state"]["intent"] == "planning"
    assert host_state["prompt_state"]["active_skills"] == []
    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert (
        "Action: `edit`." in context
        or "Action: `understand`." in context
        or "Action: `task_begin`." in context
        or "Action: `planning`." in context
    )
    assert "superpowers_external/brainstorming" not in context
    assert "Runtime-owned workflow capabilities: `planning`." in context


def test_mcp_session_start_state_tool_can_include_imported_skill_state(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        runtime = RuntimeService(hub)
        project_root = tmp_path / "project-mcp"
        runtime.project_init(project_root, init_git=False, create_remote=False)
        session = hub.sessions.create_session(
            project_root, "session-tool", "A", "Agent", "Goal A"
        )
        hub.managed_mode.set_mode(project_root, session.session_id)
        _register_superpowers_provider(
            runtime, project_root, tmp_path / "superpowers-external-tool"
        )
        hub.skills.set_selected_skills(
            project_root, session.session_id, ["superpowers_external/brainstorming"]
        )
        runtime.project_bootstrap_or_resume(
            project_root,
            session_id=session.session_id,
            include_code_bundle=False,
            include_tests=False,
        )
        result = await server.call_tool(
            "session_start_state_get",
            {
                "root": str(project_root),
                "session_id": session.session_id,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(payload)

    tool_names, result = asyncio.run(run())

    assert "session_start_state_get" in tool_names
    assert "superpowers_external/brainstorming" in result.get("active_skills", [])
    assert result["imported_skill_state"]["source"] == "skill_trigger_state"
