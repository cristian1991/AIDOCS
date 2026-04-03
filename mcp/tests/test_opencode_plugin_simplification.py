import json
import subprocess
from pathlib import Path

from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


REPO_ROOT = Path(__file__).resolve().parents[2]
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


def _make_runtime_project(
    tmp_path: Path, *, selected_skills: list[str]
) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "2026-03-30-plugin-simplification", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, selected_skills
    )
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    return runtime, project_root, session.session_id


def _run_plugin_with_runtime_host_state(tmp_path: Path, *, prompt: str) -> dict:
    _, project_root, _session_id = _make_runtime_project(
        tmp_path,
        selected_skills=["superpowers_external/brainstorming"],
    )
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, {json.dumps(prompt)});
  const context = plugin._internal.buildPromptContext(state, {json.dumps(prompt)}, "", null, promptHostState);
  console.log(JSON.stringify({{
    source: promptHostState && promptHostState.source,
    prompt_state_source: promptHostState && promptHostState.payload && promptHostState.payload.prompt_state && promptHostState.payload.prompt_state.source,
    active_skills: promptHostState && promptHostState.payload && promptHostState.payload.prompt_state && promptHostState.payload.prompt_state.active_skills,
    prompt_context: context,
  }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    return _run_node_json(script)


def _run_plugin_with_route_failure_and_stale_snapshot(tmp_path: Path) -> dict:
    _, project_root, _session_id = _make_runtime_project(
        tmp_path,
        selected_skills=["superpowers_external/brainstorming"],
    )
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const previousPython = process.env.AIDOCS_PYTHON;
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const first = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "brainstorm homepage");
  process.env.AIDOCS_PYTHON = "__definitely_missing_python__";
  const second = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "explain the database flow");
  if (previousPython === undefined) {{
    delete process.env.AIDOCS_PYTHON;
  }} else {{
    process.env.AIDOCS_PYTHON = previousPython;
  }}
  const promptContext = plugin._internal.buildPromptContext(state, "explain the database flow", "", null, second);
  console.log(JSON.stringify({{
    first_source: first && first.source,
    second,
    prompt_context: promptContext,
  }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    return _run_node_json(script)


def _run_plugin_with_provider_content_override(tmp_path: Path) -> dict:
    _, project_root, _session_id = _make_runtime_project(
        tmp_path,
        selected_skills=["superpowers_external/brainstorming"],
    )
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const promptHostState = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "brainstorm product positioning");
  console.log(JSON.stringify({{
    source: promptHostState && promptHostState.source,
    mode_metadata_source: promptHostState && promptHostState.payload ? "runtime" : null,
    active_skill_modes: promptHostState && promptHostState.payload && promptHostState.payload.prompt_state && promptHostState.payload.prompt_state.override_modes,
  }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    return _run_node_json(script)


def _run_plugin_message_flow(tmp_path: Path, *, prompt: str) -> dict:
    _, project_root, _session_id = _make_runtime_project(
        tmp_path,
        selected_skills=["superpowers_external/brainstorming"],
    )
    script = f"""
const pluginModule = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const plugin = await pluginModule.AIDOCSPlugin({{ worktree: {json.dumps(str(project_root))} }});
  const messageOutput = {{ parts: [{{ type: "text", text: {json.dumps(prompt)} }}] }};
  await plugin["chat.message"]({{ sessionID: "session-1" }}, messageOutput);
  const systemOutput = {{ system: [] }};
  await plugin["experimental.chat.system.transform"]({{ sessionID: "session-1" }}, systemOutput);
  console.log(JSON.stringify({{ system: systemOutput.system }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    return _run_node_json(script)


def _run_plugin_message_flow_with_spawn_count(tmp_path: Path, *, prompt: str) -> dict:
    _, project_root, _session_id = _make_runtime_project(
        tmp_path,
        selected_skills=["superpowers_external/brainstorming"],
    )
    script = f"""
const childProcess = require("node:child_process");
const originalSpawnSync = childProcess.spawnSync;
let pythonCalls = 0;
childProcess.spawnSync = function(...args) {{
  pythonCalls += 1;
  return originalSpawnSync.apply(this, args);
}};
const pluginModule = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const plugin = await pluginModule.AIDOCSPlugin({{ worktree: {json.dumps(str(project_root))} }});
  const messageOutput = {{ parts: [{{ type: "text", text: {json.dumps(prompt)} }}] }};
  await plugin["chat.message"]({{ sessionID: "session-1" }}, messageOutput);
  const systemOutput = {{ system: [] }};
  await plugin["experimental.chat.system.transform"]({{ sessionID: "session-1" }}, systemOutput);
  console.log(JSON.stringify({{ python_calls: pythonCalls, system: systemOutput.system }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }}).finally(() => {{
  childProcess.spawnSync = originalSpawnSync;
}});
"""
    return _run_node_json(script)


def _run_plugin_continuation_transform(tmp_path: Path) -> dict:
    _, project_root, session_id = _make_runtime_project(
        tmp_path,
        selected_skills=["superpowers_external/brainstorming"],
    )
    plan_root = project_root / ".MEMORY" / "sessions" / session_id / "plans"
    plan_root.mkdir(parents=True, exist_ok=True)
    (plan_root / "PLAN.md").write_text(
        "# Plan\n\n- [ ] Implement the continuation branch\n",
        encoding="utf-8",
    )
    script = f"""
const pluginModule = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const plugin = await pluginModule.AIDOCSPlugin({{ worktree: {json.dumps(str(project_root))} }});
  await plugin["chat.message"]({{ sessionID: "session-1" }}, {{ parts: [{{ type: "text", text: "investigate the plugin path" }}] }});
  const output = {{ messages: [{{ parts: [{{ type: "text", text: "continue" }}] }}] }};
  await plugin["experimental.chat.messages.transform"]({{ sessionID: "session-1" }}, output);
  console.log(JSON.stringify(output));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    return _run_node_json(script)


def test_opencode_plugin_consumes_runtime_host_state_payload(tmp_path: Path) -> None:
    state = _run_plugin_with_runtime_host_state(tmp_path, prompt="brainstorm homepage")

    assert state["source"] == "runtime_host_state"
    assert state["prompt_state_source"] == "live_prompt"
    assert state["active_skills"] == ["superpowers_external/brainstorming"]


def test_opencode_plugin_does_not_reconstruct_prompt_state_from_startup_snapshot(
    tmp_path: Path,
) -> None:
    state = _run_plugin_with_route_failure_and_stale_snapshot(tmp_path)

    assert state["first_source"] == "runtime_host_state"
    assert state["second"] is None
    assert "brainstorming" not in state["prompt_context"].lower()


def test_opencode_plugin_thins_mode_reconstruction_logic(tmp_path: Path) -> None:
    state = _run_plugin_with_provider_content_override(tmp_path)

    assert state["source"] == "runtime_host_state"
    assert state["mode_metadata_source"] == "runtime"
    assert state["active_skill_modes"] == {
        "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
    }


def test_opencode_plugin_message_flow_does_not_inject_stale_startup_skill_context(
    tmp_path: Path,
) -> None:
    result = _run_plugin_message_flow(tmp_path, prompt="explain the database flow")

    assert len(result["system"]) == 1
    assert "Imported skills:" not in result["system"][0]
    assert "superpowers_external/brainstorming" not in result["system"][0]


def test_opencode_plugin_message_flow_uses_single_runtime_host_state_call(
    tmp_path: Path,
) -> None:
    result = _run_plugin_message_flow_with_spawn_count(
        tmp_path, prompt="brainstorm homepage"
    )

    assert result["python_calls"] == 1


def test_opencode_plugin_continuation_transform_uses_current_runtime_state(
    tmp_path: Path,
) -> None:
    result = _run_plugin_continuation_transform(tmp_path)

    rendered = result["messages"][-1]["parts"][-1]["text"]
    assert "Session plan has 1 incomplete step" in rendered
    assert "Implement the continuation branch" in rendered
