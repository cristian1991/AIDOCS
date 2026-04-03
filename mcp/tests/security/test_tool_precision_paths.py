import asyncio
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
    (root.parent / "index.aidocs").write_text("# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n", encoding="utf-8")
    (root.parent / "global-instructions.aidocs").write_text("# Global Instructions\n", encoding="utf-8")
    (root.parent / "coding-standards.aidocs").write_text("# Coding Standards\n", encoding="utf-8")
    (root.parent / "memory-system.aidocs").write_text("# Memory System\n", encoding="utf-8")
    (root.parent / "research-safety.aidocs").write_text("# Research Safety\n", encoding="utf-8")
    (root.parent / "personalities").mkdir(parents=True, exist_ok=True)
    (root.parent / "personalities" / "default.aidocs").write_text("# Default Personality\n", encoding="utf-8")
    memory_template = root / "memory"
    memory_template.mkdir(parents=True, exist_ok=True)
    (memory_template / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (memory_template / "rules").mkdir(parents=True, exist_ok=True)
    (memory_template / "rules" / "workflow-rules.md").write_text("# Workflow Rules\n\n## Workflow Rules\n", encoding="utf-8")
    (memory_template / "rules" / "workflow-actions.md").write_text("# Workflow Actions\n\n## Workflow Actions\n", encoding="utf-8")


def _run_plugin_message_transform(tmp_path: Path, prompt: str) -> dict:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(project_root, "2026-03-30-precision-paths", "A", "Agent", "Goal A")
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    runtime.project_bootstrap_or_resume(project_root, session_id=session.session_id, include_code_bundle=False, include_tests=False)

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


def test_precision_tools_still_exist_for_exact_queries() -> None:
    async def run() -> set[str]:
        server = create_server()
        return {tool.name for tool in await server.list_tools()}

    tool_names = asyncio.run(run())

    assert {
        "aidocs_code_find",
        "aidocs_code_trace",
        "aidocs_code_get_lines",
        "aidocs_code_edit_lines",
        "aidocs_session_resume_bundle",
        "aidocs_action_surface_current_session_bundle",
    } <= tool_names


def test_opencode_edit_directive_keeps_precision_read_path(tmp_path: Path) -> None:
    result = _run_plugin_message_transform(tmp_path, "fix the plugin path")

    rendered = result["messages"][-1]["parts"][-1]["text"]
    assert 'action="edit"' in rendered
    assert "`code_get_lines`" not in rendered
    assert "aidocs_code_get_lines" in rendered or "code_read" in rendered
    assert "aidocs_code_edit_lines" in rendered
    assert "session_resume_bundle" not in rendered


def test_runtime_host_state_failure_does_not_misclassify_initialized_project(tmp_path: Path) -> None:
    templates = tmp_path / "templates-failure-fallback"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-failure-fallback"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(project_root, "2026-03-30-failure-fallback", "A", "Agent", "Goal A")
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    runtime.project_bootstrap_or_resume(project_root, session_id=session.session_id, include_code_bundle=False, include_tests=False)

    script = f"""
const childProcess = require("node:child_process");
const originalSpawnSync = childProcess.spawnSync;
childProcess.spawnSync = function(...args) {{
  return {{ status: 1, stdout: "", stderr: "runtime failed" }};
}};
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  console.log(JSON.stringify(state));
}})().catch((err) => {{ console.error(err); process.exit(1); }}).finally(() => {{
  childProcess.spawnSync = originalSpawnSync;
}});
"""

    result = _run_node_json(script)

    assert result["initialized"] is True
    assert result["startupState"] != "not_initialized"
    assert result["managed"] is True
    assert result["sessionID"] == session.session_id


def test_runtime_host_state_failure_is_cooled_down_without_stale_prompt_fallback(tmp_path: Path) -> None:
    templates = tmp_path / "templates-failure-cooldown"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-failure-cooldown"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(project_root, "2026-03-30-failure-cooldown", "A", "Agent", "Goal A")
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    runtime.project_bootstrap_or_resume(project_root, session_id=session.session_id, include_code_bundle=False, include_tests=False)

    script = f"""
const childProcess = require("node:child_process");
const originalSpawnSync = childProcess.spawnSync;
let pythonCalls = 0;
childProcess.spawnSync = function(...args) {{
  pythonCalls += 1;
  return {{ status: 1, stdout: "", stderr: "runtime failed" }};
}};
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const first = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "brainstorm app ideas");
  const second = await plugin._internal.resolvePromptHostState({json.dumps(str(project_root))}, state, "explain the database flow");
  const context = plugin._internal.buildPromptContext(state, "explain the database flow", "", null, second);
  console.log(JSON.stringify({{ pythonCalls, state, first, second, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }}).finally(() => {{
  childProcess.spawnSync = originalSpawnSync;
}});
"""

    result = _run_node_json(script)

    assert result["pythonCalls"] == 1
    assert result["first"] is None
    assert result["second"] is None
    assert "brainstorming" not in result["context"].lower()
