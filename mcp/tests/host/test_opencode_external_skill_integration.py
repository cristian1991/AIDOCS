import json
import subprocess
from pathlib import Path

from aidocs_mcp.mcp_server import create_server
from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_PATH = REPO_ROOT / "core" / "plugins" / "aidocs.js"


def _run_node_json(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


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
    runtime: RuntimeService, project_root: Path, tmp_path: Path
) -> None:
    provider_root = tmp_path / "superpowers-external"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
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


def _run_plugin_message_transform(project_root: Path, prompt: str) -> dict:
    script = f"""
const pluginModule = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const plugin = await pluginModule.AIDOCSPlugin({{ worktree: {json.dumps(str(project_root))} }});
  await plugin["chat.message"]({{ sessionID: "session-1" }}, {{ parts: [{{ type: "text", text: {json.dumps(prompt)} }}] }});
  const output = {{ messages: [{{ parts: [{{ type: "text", text: {json.dumps(prompt)} }}] }}] }};
  await plugin["experimental.chat.messages.transform"]({{ sessionID: "session-1" }}, output);
  console.log(JSON.stringify(output));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    return _run_node_json(script)


def _make_managed_project(tmp_path: Path, name: str) -> Path:
    templates = tmp_path / f"templates-{name}"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / f"project-{name}"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, f"2026-03-30-{name}", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    return project_root


def _make_runtime_with_selected_bundled_skill(
    tmp_path: Path, name: str, skill_id: str
) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / f"templates-{name}"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / f"project-{name}"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, f"2026-03-31-{name}", "A", "Agent", "Goal A"
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


def test_opencode_uses_bundled_skills_without_external_plugin(tmp_path: Path) -> None:
    _runtime, project_root, _session_id = _make_runtime_with_selected_bundled_skill(
        tmp_path,
        "bundled-opencode",
        "deep-retrieval",
    )

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const context = plugin._internal.buildPromptContext(state, "inspect project state", "", null);
  console.log(JSON.stringify({{ state, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["state"]["importedSkillState"]["source"] == "cached_session"
    assert result["state"]["importedSkillState"]["selected_skills"] == [
        "deep-retrieval"
    ]
    assert result["state"]["importedSkillState"]["active_skills"] == ["deep-retrieval"]
    assert (
        result["state"]["importedSkillState"]["helper_skill_guidance"][0]["name"]
        == "deep-retrieval"
    )
    assert "Imported skills" in result["context"]
    assert "`deep-retrieval`" in result["context"]
    assert "Active AIDOCS helper skill guidance" in result["context"]
    assert "exact signatures" in result["context"]
    assert "aidocs_bundled_superpowers/deep-retrieval" not in result["context"]


def test_opencode_prompt_context_surfaces_bundled_helper_skill_guidance(
    tmp_path: Path,
) -> None:
    _runtime, project_root, _session_id = _make_runtime_with_selected_bundled_skill(
        tmp_path,
        "bundled-opencode-prompt",
        "deep-retrieval",
    )

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "investigate exact method signatures before editing");
  const context = plugin._internal.buildPromptContext(state, "investigate exact method signatures before editing", "", null, promptHostState);
  console.log(JSON.stringify({{ state, promptHostState, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["promptHostState"]["payload"]["prompt_state"]["active_skills"] == [
        "deep-retrieval"
    ]
    assert "Active AIDOCS helper skill guidance" in result["context"]
    assert "exact signatures" in result["context"]


def test_opencode_plugin_surfaces_imported_skill_state_without_superpowers_plugin(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-a", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/brainstorming"]
    )
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    runtime.aidocs_route_prompt(
        project_root, user_request="brainstorm app ideas", action_kind="understand"
    )

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "brainstorm app ideas");
  const context = plugin._internal.buildPromptContext(state, "brainstorm app ideas", "", null, promptHostState);
  console.log(JSON.stringify({{ state, promptHostState, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["state"]["importedSkillState"]["source"] == "cached_session"
    assert result["state"]["importedSkillState"]["session_id"] == session.session_id
    assert result["state"]["importedSkillState"]["active_skills"] == [
        "superpowers_external/brainstorming"
    ]
    assert result["promptHostState"]["source"] == "runtime_host_state"
    assert "Imported skills" in result["context"]
    assert "superpowers_external/brainstorming" in result["context"]


def test_opencode_plugin_surfaces_override_resolved_host_skill_state(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates-override"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-override"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-override", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "override-provider"
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

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  console.log(JSON.stringify(state));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["importedSkillState"]["active_skills"] == []
    assert (
        result["importedSkillState"]["runtime_owned_capabilities"][0]["capability_id"]
        == "planning"
    )


def test_opencode_runtime_state_can_include_override_mode_metadata(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates-override-metadata"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-override-metadata"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-override-metadata", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "override-metadata-provider"
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

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "write the plan");
  const context = plugin._internal.buildPromptContext(state, "write the plan", "", null, promptHostState);
  console.log(JSON.stringify({{ state, promptHostState, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["state"]["importedSkillState"]["mode_metadata"][
        "selected_skill_modes"
    ] == {"superpowers_external/writing-plans": "aidocs_runtime_owned"}
    assert result["promptHostState"]["payload"]["prompt_state"]["override_modes"] == {}
    assert (
        result["promptHostState"]["payload"]["prompt_state"][
            "runtime_owned_capabilities"
        ][0]["capability_id"]
        == "planning"
    )
    assert "Runtime-owned workflow capabilities: `planning`." in result["context"]


def test_opencode_runtime_state_surfaces_provider_content_override_mode_metadata(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates-provider-content-metadata"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-provider-content-metadata"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-provider-content-metadata", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "provider-content-metadata-provider"
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

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "brainstorm app ideas");
  const context = plugin._internal.buildPromptContext(state, "brainstorm app ideas", "", null, promptHostState);
  console.log(JSON.stringify({{ state, promptHostState, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["state"]["importedSkillState"]["mode_metadata"][
        "active_skill_modes"
    ] == {"superpowers_external/brainstorming": "provider_content_aidocs_runtime"}
    assert result["promptHostState"]["payload"]["prompt_state"]["override_modes"] == {
        "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
    }
    assert "provider_content_aidocs_runtime" in result["context"]


def test_opencode_plugin_mode_metadata_source_is_runtime_host_state(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates-provider-content-runtime"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-provider-content-runtime"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-provider-content-runtime", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "provider-content-runtime-provider"
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

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "brainstorm app ideas");
  const context = plugin._internal.buildPromptContext(state, "brainstorm app ideas", "", null, promptHostState);
  console.log(JSON.stringify({{
    source: promptHostState && promptHostState.source,
    mode_metadata_source: promptHostState && promptHostState.payload ? "runtime" : null,
    prompt_state: promptHostState && promptHostState.payload && promptHostState.payload.prompt_state,
    context,
  }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["source"] == "runtime_host_state"
    assert result["mode_metadata_source"] == "runtime"
    assert result["prompt_state"]["override_modes"] == {
        "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
    }
    assert "provider_content_aidocs_runtime" in result["context"]


def test_opencode_plugin_ignores_other_session_trigger_snapshot(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session_a = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-a", "A", "Agent", "Goal A"
    )
    session_b = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-b", "B", "Agent", "Goal B"
    )
    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root, session_a.session_id, ["superpowers_external/brainstorming"]
    )
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session_a.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    runtime.aidocs_route_prompt(
        project_root, user_request="brainstorm app ideas", action_kind="understand"
    )
    runtime.hub.managed_mode.set_mode(project_root, session_id=session_b.session_id)

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const context = plugin._internal.buildPromptContext(state, "brainstorm app ideas", "", null);
  console.log(JSON.stringify({{ state, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["state"]["sessionID"] == session_b.session_id
    assert result["state"]["importedSkillState"]["session_id"] == session_b.session_id
    assert result["state"]["importedSkillState"]["active_skills"] == []
    assert "superpowers_external/brainstorming" not in result["context"]


def test_opencode_plugin_reflects_session_skill_changes_after_mcp_update(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    project_root = tmp_path / "project-mcp"

    async def run() -> dict:
        server = create_server()
        hub = server._aidocs_test_hub
        runtime = RuntimeService(hub)
        runtime.project_init(project_root, init_git=False, create_remote=False)
        session = hub.sessions.create_session(
            project_root, "2026-03-30-a", "A", "Agent", "Goal A"
        )
        hub.managed_mode.set_mode(project_root, session_id=session.session_id)
        _register_superpowers_provider(runtime, project_root, tmp_path / "mcp-provider")
        await server.call_tool(
            "aidocs_session_skills_set",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
                "selected_skills": ["superpowers_external/brainstorming"],
            },
        )
        runtime.aidocs_route_prompt(
            project_root, user_request="brainstorm app ideas", action_kind="understand"
        )
        script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  console.log(JSON.stringify(state));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
        return _run_node_json(script)

    result = __import__("asyncio").run(run())

    assert result["importedSkillState"]["active_skills"] == [
        "superpowers_external/brainstorming"
    ]


def test_opencode_message_directive_uses_overview_tools_for_common_path(
    tmp_path: Path,
) -> None:
    project_root = _make_managed_project(tmp_path, "overview-directive")

    result = _run_plugin_message_transform(project_root, "explain the plugin path")

    parts = result["messages"][-1]["parts"]
    assert len(parts) == 1
    assert parts[0]["text"] == "explain the plugin path"


def test_opencode_message_directive_uses_overview_tools_for_investigate_common_path(
    tmp_path: Path,
) -> None:
    project_root = _make_managed_project(tmp_path, "investigate-directive")

    result = _run_plugin_message_transform(project_root, "investigate the plugin path")

    parts = result["messages"][-1]["parts"]
    assert len(parts) == 1
    assert parts[0]["text"] == "investigate the plugin path"


def test_opencode_message_directive_uses_overview_tools_for_inspect_common_path(
    tmp_path: Path,
) -> None:
    project_root = _make_managed_project(tmp_path, "inspect-directive")

    result = _run_plugin_message_transform(project_root, "inspect the plugin path")

    parts = result["messages"][-1]["parts"]
    assert len(parts) == 1
    assert parts[0]["text"] == "inspect the plugin path"


def test_opencode_prompt_runtime_state_activates_brainstorming_for_matching_prompt(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-current-prompt"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-a", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/brainstorming"]
    )
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptSkillState = await plugin._internal.resolvePromptImportedSkillState({json.dumps(str(project_root))}, state, "brainstorm app ideas for the dashboard");
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "brainstorm app ideas for the dashboard");
  const context = plugin._internal.buildPromptContext(state, "brainstorm app ideas for the dashboard", "", null, promptHostState);
  console.log(JSON.stringify({{ promptSkillState, promptHostState, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["promptSkillState"]["source"] == "live_prompt"
    assert result["promptSkillState"]["active_skills"] == [
        "superpowers_external/brainstorming"
    ]
    assert result["promptHostState"]["source"] == "runtime_host_state"
    assert "Imported skills" in result["context"]


def test_opencode_prompt_runtime_state_does_not_keep_stale_brainstorming_for_unrelated_prompt(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-stale-prompt"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-a", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/brainstorming"]
    )
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const first = await plugin._internal.resolvePromptImportedSkillState({json.dumps(str(project_root))}, state, "brainstorm app ideas for the dashboard");
  const second = await plugin._internal.resolvePromptImportedSkillState({json.dumps(str(project_root))}, state, "explain the database migration flow");
  const context = plugin._internal.buildPromptContext(state, "explain the database migration flow", "", null, second);
  console.log(JSON.stringify({{ first, second, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["first"]["active_skills"] == ["superpowers_external/brainstorming"]
    assert result["second"]["source"] == "live_prompt"
    assert result["second"]["active_skills"] == []
    assert "superpowers_external/brainstorming" not in result["context"]


def test_opencode_prompt_runtime_state_does_not_fallback_to_stale_snapshot_when_route_fails(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates-route-failure"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-route-failure"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-route-failure", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "route-failure-provider"
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

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const previousPython = process.env.AIDOCS_PYTHON;
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const first = await plugin._internal.resolvePromptImportedSkillState({json.dumps(str(project_root))}, state, "brainstorm app ideas for the dashboard");
  process.env.AIDOCS_PYTHON = "__definitely_missing_python__";
  const second = await plugin._internal.resolvePromptImportedSkillState({json.dumps(str(project_root))}, state, "explain the database migration flow");
  if (previousPython === undefined) {{
    delete process.env.AIDOCS_PYTHON;
  }} else {{
    process.env.AIDOCS_PYTHON = previousPython;
  }}
  const context = plugin._internal.buildPromptContext(state, "explain the database migration flow", "", null, second);
  console.log(JSON.stringify({{ first, second, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["first"]["active_skills"] == ["superpowers_external/brainstorming"]
    assert result["second"] is None
    assert "superpowers_external/brainstorming" not in result["context"]


def test_opencode_prompt_context_explicit_null_prompt_state_suppresses_persisted_snapshot(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates-explicit-null"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-explicit-null"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-explicit-null", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "explicit-null-provider"
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

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "brainstorm app ideas");
  const startupContext = plugin._internal.buildPromptContext(state, "brainstorm app ideas", "", null, promptHostState);
  const suppressedContext = plugin._internal.buildPromptContext(state, "explain the database migration flow", "", null, null);
  console.log(JSON.stringify({{ startupContext, suppressedContext }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert "superpowers_external/brainstorming" in result["startupContext"]
    assert "superpowers_external/brainstorming" not in result["suppressedContext"]
