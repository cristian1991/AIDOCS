from pathlib import Path
import asyncio

from aidocs_mcp.claude_hook import ClaudeHookHandler
from aidocs_mcp.execution_index_store import ExecutionIndexStore
from aidocs_mcp.mcp_server import create_server


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


def _seed_project(project_root: Path) -> None:
    mem = project_root / ".MEMORY"
    (mem / ".aidocs").mkdir(parents=True, exist_ok=True)
    for name in ["index.aidocs", "global-instructions.aidocs", "coding-standards.aidocs", "memory-system.aidocs"]:
        (mem / ".aidocs" / name).write_text(f"# {name}\n", encoding="utf-8")
    (mem / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")


def test_record_run_and_event_supports_ad_hoc_execution(tmp_path: Path) -> None:
    store = ExecutionIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    run_id = store.record_run(
        project_root,
        run_kind="manual_debug",
        source_kind="test",
        session_id="s1",
        status="started",
        ad_hoc=True,
        metadata={"note": "ad hoc"},
    )
    event_id = store.record_event(
        project_root,
        event_kind="tool_invoked",
        source_kind="test",
        session_id="s1",
        capability_name="Read",
        action_kind="inspect",
        run_id=run_id,
        status="observed",
        payload={"path": "src/app.py"},
    )

    status = store.execution_status(project_root)
    runs = store.list_runs(project_root, session_id="s1", limit=10)
    events = store.list_events(project_root, query="Read", session_id="s1", limit=10)

    assert run_id.startswith("run-")
    assert event_id.startswith("event-")
    assert status["execution_runs"] == 1
    assert status["execution_events"] == 1
    assert status["run_kinds"]["manual_debug"] == 1
    assert status["event_kinds"]["tool_invoked"] == 1
    assert runs[0]["ad_hoc"] is True
    assert events[0]["capability_name"] == "Read"
    assert events[0]["payload"]["path"] == "src/app.py"


def test_claude_hook_records_execution_events(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    handler = ClaudeHookHandler()
    handler.runtime.hub = handler.runtime.hub.__class__(templates_root=templates)
    handler.runtime = handler.runtime.__class__(handler.runtime.hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    handler.runtime.hub.sessions.create_session(project_root, "2026-03-24-a", "A", "Agent", "Goal A")
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-24-a")

    handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        }
    )

    events = handler.runtime.hub.execution.list_events(project_root, session_id="2026-03-24-a", limit=10)

    assert events
    assert events[0]["event_kind"] == "pretooluse"
    assert events[0]["source_kind"] == "claude_hook"
    assert events[0]["capability_name"] == "Read"
    assert events[0]["status"] == "observed"


def test_server_call_tool_records_mcp_invocation_execution(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _seed_project(project_root)
    server = create_server()

    result = asyncio.run(server.call_tool("session_list", {"project_root": str(project_root)}))
    store = ExecutionIndexStore()
    status = store.execution_status(project_root)
    runs = store.list_runs(project_root, limit=10)
    events = store.list_events(project_root, query="session_list", limit=10)

    assert result is not None
    assert status["execution_runs"] == 1
    assert status["execution_events"] == 2
    assert status["run_kinds"]["mcp_tool_invocation"] == 1
    assert status["event_kinds"]["tool_call_started"] == 1
    assert status["event_kinds"]["tool_call_completed"] == 1
    assert runs[0]["capability_name"] == "session_list"
    assert runs[0]["status"] == "completed"
    assert runs[0]["metadata"]["result_summary"]["result_type"] == "ToolResult"
    assert runs[0]["metadata"]["result_summary"]["structured_keys"] == ["result"]
    assert all(item["capability_name"] == "session_list" for item in events)
    completed = next(item for item in events if item["event_kind"] == "tool_call_completed")
    assert completed["payload"]["result_summary"]["result_type"] == "ToolResult"


def test_query_last_execution_filters_by_action_kind(tmp_path: Path) -> None:
    store = ExecutionIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.record_event(project_root, event_kind="prompt_classified", source_kind="hook", action_kind="edit", status="classified")
    store.record_event(project_root, event_kind="prompt_classified", source_kind="hook", action_kind="trace", status="classified")
    store.record_event(project_root, event_kind="prompt_classified", source_kind="hook", action_kind="edit", status="classified")

    results = store.query_last_execution(project_root, action_kind="edit")
    assert len(results) == 2
    assert all(r["action_kind"] == "edit" for r in results)


def test_query_execution_summary(tmp_path: Path) -> None:
    store = ExecutionIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.record_event(project_root, event_kind="prompt_classified", source_kind="hook", action_kind="edit", session_id="s1")
    store.record_event(project_root, event_kind="tool_invoked", source_kind="mcp", action_kind="edit", session_id="s1")
    store.record_event(project_root, event_kind="prompt_classified", source_kind="hook", action_kind="trace", session_id="s1")

    summary = store.query_execution_summary(project_root, session_id="s1")
    assert summary["total_events"] == 3
    assert summary["by_action_kind"]["edit"] == 2
    assert summary["by_action_kind"]["trace"] == 1
    assert summary["ad_hoc_events"] == 3  # all events have no procedure_id
    assert summary["procedure_linked_events"] == 0


def test_query_procedure_compliance(tmp_path: Path) -> None:
    store = ExecutionIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    # Procedure-linked run
    store.record_run(project_root, run_kind="task", source_kind="mcp", session_id="s1", procedure_id="proc-1", ad_hoc=False)
    # Ad-hoc runs
    store.record_run(project_root, run_kind="task", source_kind="mcp", session_id="s1", ad_hoc=True)
    store.record_run(project_root, run_kind="task", source_kind="mcp", session_id="s1", ad_hoc=True)

    compliance = store.query_procedure_compliance(project_root, session_id="s1")
    assert len(compliance["procedure_linked_runs"]) == 1
    assert len(compliance["ad_hoc_runs"]) == 2
    assert compliance["compliance_ratio"] == "1/3"


def test_prune_old_events(tmp_path: Path) -> None:
    store = ExecutionIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    # Create events with old timestamps
    store.record_event(project_root, event_kind="old", source_kind="test", observed_at="2020-01-01T00:00:00Z")
    store.record_event(project_root, event_kind="old", source_kind="test", observed_at="2020-01-02T00:00:00Z")
    store.record_event(project_root, event_kind="recent", source_kind="test")  # current timestamp

    result = store.prune_old_events(project_root, max_age_days=30)
    assert result["pruned_events_by_age"] == 2

    status = store.execution_status(project_root)
    assert status["execution_events"] == 1
