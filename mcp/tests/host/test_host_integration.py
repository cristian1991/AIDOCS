import json
import subprocess
from pathlib import Path

from aidocs_mcp.execution_index_store import ExecutionIndexStore


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


def _write_bootstrapped_project(project_root: Path) -> None:
    (project_root / ".MEMORY" / ".aidocs").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / ".aidocs" / "index.aidocs").write_text(
        "# Router\n", encoding="utf-8"
    )
    (project_root / ".MEMORY" / "INDEX.md").write_text(
        "# Memory Index\n", encoding="utf-8"
    )
    (project_root / ".MEMORY" / "rules").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n", encoding="utf-8"
    )
    (project_root / ".MEMORY" / "rules" / "workflow-actions.md").write_text(
        "# Workflow Actions\n", encoding="utf-8"
    )
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")


def _write_session(project_root: Path, session_id: str, status: str = "active") -> None:
    session_root = project_root / ".MEMORY" / "sessions" / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    (session_root / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n- Session\n\n"
        f"## Status\n- {status}\n\n"
        "## Owner\n- agent\n\n"
        "## Goal\n- Goal\n\n"
        "## Scope\n- Scope\n\n"
        "## Last Updated\n- 2026-03-29 00:00\n",
        encoding="utf-8",
    )


def _write_managed_mode(project_root: Path, session_id: str) -> None:
    (project_root / ".MEMORY" / "config").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "config" / "aidocs-managed.json").write_text(
        json.dumps({"active": True, "session_id": session_id}),
        encoding="utf-8",
    )


def _write_query_gate(project_root: Path, session_id: str, payload: dict) -> None:
    session_root = project_root / ".MEMORY" / "sessions" / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    (session_root / "query-gate.json").write_text(json.dumps(payload), encoding="utf-8")


def test_opencode_state_reports_not_initialized(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const result = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  console.log(JSON.stringify(result));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["startupState"] == "not_initialized"


def test_opencode_state_reports_no_session_after_bootstrap(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_bootstrapped_project(project_root)
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const result = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  console.log(JSON.stringify(result));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["startupState"] == "no_session"


def test_opencode_prompt_context_requires_session_choice_when_multiple_sessions(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_bootstrapped_project(project_root)
    _write_session(project_root, "2026-03-29-a", status="active")
    _write_session(project_root, "2026-03-29-b", status="active")
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const context = plugin._internal.buildPromptContext(state, "fix the startup flow", "", null);
  console.log(JSON.stringify({{ state, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["state"]["startupState"] == "multiple_sessions"
    assert "which session" in result["context"].lower()


def test_opencode_state_reports_stale_indexes_for_ready_session(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_bootstrapped_project(project_root)
    _write_session(project_root, "2026-03-29-a", status="active")
    (project_root / ".MEMORY" / "config").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "config" / "aidocs-managed.json").write_text(
        json.dumps({"active": True, "session_id": "2026-03-29-a"}),
        encoding="utf-8",
    )
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  const context = plugin._internal.buildPromptContext(state, "investigate the startup flow", "", null);
  console.log(JSON.stringify({{ state, context }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["state"]["startupState"] == "stale_indexes"
    assert (
        "run `/aidocs` first" in result["context"].lower()
        or "run `/aidocs`" in result["context"].lower()
    )


def test_opencode_before_hook_blocks_read_without_query_gate_grant(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_bootstrapped_project(project_root)
    _write_session(project_root, "2026-04-01-a", status="active")
    _write_managed_mode(project_root, "2026-04-01-a")
    _write_query_gate(
        project_root,
        "2026-04-01-a",
        {
            "allow_read": False,
            "known_exact_paths": [],
            "lane_exact_paths": [],
        },
    )
    absolute_secret = (project_root / "src" / "secret.py").as_posix()
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const hooks = await plugin.AIDOCSPlugin({{ directory: {json.dumps(str(project_root))} }});
  const results = [];
  for (const filePath of ["src/secret.py", {json.dumps(absolute_secret)}]) {{
    try {{
      await hooks["tool.execute.before"](
        {{ tool: "read", sessionID: "s1", args: {{ filePath }} }},
        {{ args: {{ filePath }} }}
      );
      results.push({{ filePath, blocked: false }});
    }} catch (err) {{
      results.push({{ filePath, blocked: true, message: String(err.message || err) }});
    }}
  }}
  console.log(JSON.stringify(results));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    results = _run_node_json(script)

    assert results[0]["blocked"] is True
    assert results[1]["blocked"] is True
    assert "indexed-read gate" in results[0]["message"]
    assert "indexed-read gate" in results[1]["message"]
    assert "src/secret.py" in results[0]["message"]
    assert "src/secret.py" in results[1]["message"]


def test_opencode_before_hook_allows_read_with_query_gate_grant(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_bootstrapped_project(project_root)
    _write_session(project_root, "2026-04-01-b", status="active")
    _write_managed_mode(project_root, "2026-04-01-b")
    _write_query_gate(
        project_root,
        "2026-04-01-b",
        {
            "allow_read": False,
            "known_exact_paths": ["src/app.py"],
            "lane_exact_paths": [],
        },
    )
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const hooks = await plugin.AIDOCSPlugin({{ directory: {json.dumps(str(project_root))} }});
  try {{
    await hooks["tool.execute.before"](
      {{ tool: "read", sessionID: "s1", args: {{ filePath: "src/app.py" }} }},
      {{ args: {{ filePath: "src/app.py" }} }}
    );
    console.log(JSON.stringify({{ blocked: false }}));
  }} catch (err) {{
    console.log(JSON.stringify({{ blocked: true, message: String(err.message || err) }}));
  }}
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["blocked"] is False


def test_opencode_after_hook_records_native_tool_use(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_bootstrapped_project(project_root)
    _write_session(project_root, "2026-04-01-c", status="active")
    _write_managed_mode(project_root, "2026-04-01-c")
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const hooks = await plugin.AIDOCSPlugin({{ directory: {json.dumps(str(project_root))} }});
  await hooks["tool.execute.after"](
    {{ tool: "read", sessionID: "s1", args: {{ filePath: "src/app.py" }} }},
    {{ ok: true, content: "hello" }}
  );
  console.log(JSON.stringify({{ done: true }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)
    events = ExecutionIndexStore().list_events(
        project_root, session_id="2026-04-01-c", limit=20
    )

    assert result["done"] is True
    native_events = [
        event for event in events if event["event_kind"] == "native_tool_use"
    ]
    assert native_events
    event = native_events[0]
    assert event["source_kind"] == "opencode_plugin"
    assert event["capability_name"] == "read"
    assert event["action_kind"] == "native_tool"
    assert event["status"] == "success"


def test_opencode_prompt_context_mentions_imported_skills_from_runtime_state() -> None:
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
const context = plugin._internal.buildPromptContext({{
  initialized: true,
  bootstrapped: true,
  managed: true,
  sessionID: "2026-03-30-a",
  startupState: "ready",
  workflowActions: [],
  importedSkillState: {{
    active_skills: ["superpowers_external/brainstorming"],
  }},
}}, "brainstorm app ideas", "", null);
console.log(JSON.stringify({{ context }}));
"""

    result = _run_node_json(script)

    assert "Imported skills" in result["context"]
    assert "superpowers_external/brainstorming" in result["context"]


def test_opencode_prompt_context_prefers_current_bound_session_flow() -> None:
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
const context = plugin._internal.buildPromptContext({{
  initialized: true,
  bootstrapped: true,
  managed: true,
  sessionID: "2026-03-30-a",
  startupState: "ready",
  workflowActions: [],
}}, "continue with option 1", "", null);
console.log(JSON.stringify({{ context }}));
"""

    result = _run_node_json(script)

    assert "Bound session: `2026-03-30-a`." in result["context"]
    assert "Stay in the bound AIDOCS session" in result["context"]


def test_opencode_prompt_context_includes_task_update_followthrough_nudge() -> None:
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
const context = plugin._internal.buildPromptContext({{
  initialized: true,
  bootstrapped: true,
  managed: true,
  sessionID: "2026-04-01-a",
  startupState: "ready",
  workflowActions: [],
  hostState: {{
    lifecycle_state: {{
      needs_task_update: true,
      needs_task_complete: false,
    }},
  }},
}}, "keep investigating the startup flow", "", null);
console.log(JSON.stringify({{ context }}));
"""

    result = _run_node_json(script)

    assert "Lifecycle follow-through" in result["context"]
    assert "task_update" in result["context"]


def test_cursor_packaging_declares_session_start_hook() -> None:
    plugin_manifest = REPO_ROOT / "core" / ".cursor-plugin" / "plugin.json"

    assert plugin_manifest.is_file()

    plugin = json.loads(plugin_manifest.read_text(encoding="utf-8"))
    hooks_manifest = plugin_manifest.parent / plugin["hooks"]

    assert hooks_manifest.is_file()

    hooks = json.loads(hooks_manifest.read_text(encoding="utf-8"))
    startup_script = plugin_manifest.parent / "hooks" / "session-start"

    assert plugin.get("hooks")
    assert "sessionStart" in hooks.get("hooks", {})
    assert startup_script.is_file()


def test_codex_packaging_declares_startup_and_prompt_hooks() -> None:
    hooks_manifest = REPO_ROOT / "core" / ".codex" / "hooks.json"

    assert hooks_manifest.is_file()

    hooks = json.loads(hooks_manifest.read_text(encoding="utf-8"))
    assert "SessionStart" in hooks.get("hooks", {})
    assert "UserPromptSubmit" in hooks.get("hooks", {})


def test_claude_local_hooks_manifest_declares_session_start() -> None:
    hooks_manifest = REPO_ROOT / "core" / "hooks" / "hooks.json"

    assert hooks_manifest.is_file()

    hooks = json.loads(hooks_manifest.read_text(encoding="utf-8"))
    assert "SessionStart" in hooks.get("hooks", {})


def test_windows_hook_wrapper_exists() -> None:
    wrapper = REPO_ROOT / "core" / "hooks" / "run-hook.cmd"
    startup = REPO_ROOT / "core" / "hooks" / "session-start"

    assert wrapper.is_file()
    assert startup.is_file()


def test_claude_installers_register_session_start() -> None:
    shell_installer = (
        REPO_ROOT / "core" / "scripts" / "install-agent-routing.sh"
    ).read_text(encoding="utf-8")
    powershell_installer = (
        REPO_ROOT / "core" / "scripts" / "install-agent-routing.ps1"
    ).read_text(encoding="utf-8")

    assert "SessionStart" in shell_installer
    assert "SessionStart" in powershell_installer


def test_host_integration_docs_cover_codex_limits() -> None:
    doc = (REPO_ROOT / "mcp" / "HOST_INTEGRATION.md").read_text(encoding="utf-8")

    assert "Codex" in doc
    assert "experimental" in doc.lower()
    assert "Windows" in doc
