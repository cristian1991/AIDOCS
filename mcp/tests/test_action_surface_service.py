from pathlib import Path

from aidocs_mcp.action_surface_service import ActionSurfaceService
from aidocs_mcp.capability_index_store import CapabilityIndexStore
from aidocs_mcp.execution_index_store import ExecutionIndexStore
from aidocs_mcp.procedure_capability_link_store import ProcedureCapabilityLinkStore
from aidocs_mcp.procedure_index_store import ProcedureIndexStore
from aidocs_mcp.service_hub import AidocsServiceHub
from aidocs_mcp.workflow_action_service import WorkflowActionService


class _TaskConfig:
    def __init__(self, mode: str = "forbidden") -> None:
        self.mode = mode


class _Tool:
    def __init__(self, name: str, description: str, aliases: list[str] | None = None, family: str | None = None) -> None:
        self.name = name
        self.title = None
        self.description = description
        self.tags = set()
        self.parameters = {"type": "object", "properties": {}}
        self.output_schema = {"type": "object", "properties": {}}
        meta = {}
        if aliases:
            meta["capability_aliases"] = aliases
        if family:
            meta["capability_family"] = family
        self.meta = meta or None
        self.task_config = _TaskConfig()
        self.timeout = None


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
        "## Active Claims\n-\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text(
        "# Context\n\n"
        "## Relevant Files\n-\n\n"
        "## Relevant Commands\n-\n\n"
        "## Relevant Snippets\n-\n\n"
        "## Session Facts\n-\n\n"
        "## Constraints\n-\n",
        encoding="utf-8",
    )


def test_compare_returns_should_can_did_layers(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After each completed task, run `python tools/blink.py`.\n",
        encoding="utf-8",
    )

    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    hub = AidocsServiceHub(templates_root=templates)
    workflow = WorkflowActionService()
    procedures = ProcedureIndexStore()
    capabilities = CapabilityIndexStore()
    links = ProcedureCapabilityLinkStore()
    execution = ExecutionIndexStore()

    compiled = workflow.compile_project_rules(project_root)
    procedures.sync_procedures(project_root, compiled)
    capabilities.sync_capabilities(project_root, [_Tool("local_command", "Run local commands")])
    links.sync_links(
        project_root,
        procedures.find_procedures(project_root, limit=100),
        capabilities.find_capabilities(project_root, limit=100),
    )
    run_id = execution.record_run(
        project_root,
        run_kind="manual_test",
        source_kind="test",
        capability_name="local_command",
        session_id="s1",
        ad_hoc=False,
        procedure_id="rule-01-01-task_complete-local_command",
        status="completed",
    )
    execution.record_event(
        project_root,
        event_kind="tool_invoked",
        source_kind="test",
        capability_name="local_command",
        session_id="s1",
        procedure_id="rule-01-01-task_complete-local_command",
        run_id=run_id,
        status="observed",
        payload={"command": "python tools/blink.py"},
    )

    service = ActionSurfaceService(hub)
    result = service.compare(project_root, query="local_command", session_id="s1", limit=10)

    assert result["coverage"]["has_definition"] is True
    assert result["coverage"]["has_capability"] is True
    assert result["coverage"]["has_execution"] is True
    assert result["coverage"]["resolved_link_count"] == 1
    assert result["coverage"]["unresolved_link_count"] == 0
    assert result["history_summary"]["event_count"] == 1
    assert result["history_summary"]["run_count"] == 1
    assert result["history_summary"]["capabilities_seen"] == ["local_command"]
    assert result["gap_summary"]["missing_definition"] is False
    assert result["gap_summary"]["missing_capability"] is False
    assert result["gap_summary"]["unresolved_actions"] == []
    assert result["assessment"]["state"] == "aligned"
    assert result["assessment"]["recommended_next_steps"] == ["No immediate gap detected; continue monitoring execution history and drift."]
    assert result["should"][0]["definition_kind"] == "workflow_action"
    assert result["can"][0]["name"] == "local_command"
    assert result["did"]["events"][0]["capability_name"] == "local_command"
    assert result["did"]["runs"][0]["capability_name"] == "local_command"


def test_compare_reports_candidate_capabilities_for_unresolved_actions(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After push, check GitHub workflow status.\n",
        encoding="utf-8",
    )

    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    hub = AidocsServiceHub(templates_root=templates)
    workflow = WorkflowActionService()
    procedures = ProcedureIndexStore()
    capabilities = CapabilityIndexStore()
    links = ProcedureCapabilityLinkStore()

    compiled = workflow.compile_project_rules(project_root)
    procedures.sync_procedures(project_root, compiled)
    capabilities.sync_capabilities(project_root, [_Tool("workflow_actions_compile", "Compile workflow actions", family="workflow")])
    links.sync_links(
        project_root,
        procedures.find_procedures(project_root, limit=100),
        capabilities.find_capabilities(project_root, limit=100),
    )

    service = ActionSurfaceService(hub)
    result = service.compare(project_root, query="github_workflow_check", limit=10)

    assert result["coverage"]["has_definition"] is True
    assert result["coverage"]["has_capability"] is False
    assert result["gap_summary"]["unresolved_actions"] == ["github_workflow_check"]
    assert result["gap_summary"]["candidate_capabilities"]["github_workflow_check"] == ["workflow_actions_compile"]
    assert result["assessment"]["state"] == "definition_without_capability"
    assert result["assessment"]["candidate_summary"]["github_workflow_check"] == ["workflow_actions_compile"]
    assert any("callable capability" in item for item in result["assessment"]["recommended_next_steps"])


def test_assess_returns_operator_facing_summary(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After push, check GitHub workflow status.\n",
        encoding="utf-8",
    )

    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    hub = AidocsServiceHub(templates_root=templates)
    workflow = WorkflowActionService()
    procedures = ProcedureIndexStore()
    capabilities = CapabilityIndexStore()
    links = ProcedureCapabilityLinkStore()

    compiled = workflow.compile_project_rules(project_root)
    procedures.sync_procedures(project_root, compiled)
    capabilities.sync_capabilities(project_root, [_Tool("workflow_actions_compile", "Compile workflow actions", family="workflow")])
    links.sync_links(
        project_root,
        procedures.find_procedures(project_root, limit=100),
        capabilities.find_capabilities(project_root, limit=100),
    )

    service = ActionSurfaceService(hub)
    result = service.assess(project_root, query="github_workflow_check", limit=10)

    assert result["state"] == "definition_without_capability"
    assert "procedure exists" in result["headline"].lower()
    assert any("Unresolved actions" in item for item in result["findings"])
    assert result["candidate_summary"]["github_workflow_check"] == ["workflow_actions_compile"]
    assert any("callable capability" in item for item in result["recommended_next_steps"])


def test_status_bundle_groups_ready_and_attention_items(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After each completed task, run `python tools/blink.py`.\n"
        "- After push, check GitHub workflow status.\n",
        encoding="utf-8",
    )

    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    hub = AidocsServiceHub(templates_root=templates)
    workflow = WorkflowActionService()
    procedures = ProcedureIndexStore()
    capabilities = CapabilityIndexStore()
    links = ProcedureCapabilityLinkStore()
    execution = ExecutionIndexStore()

    compiled = workflow.compile_project_rules(project_root)
    procedures.sync_procedures(project_root, compiled)
    capabilities.sync_capabilities(
        project_root,
        [
            _Tool("command_runner", "Run local commands", aliases=["local_command"], family="execution"),
            _Tool("workflow_actions_compile", "Compile workflow actions", family="workflow"),
        ],
    )
    links.sync_links(
        project_root,
        procedures.find_procedures(project_root, limit=100),
        capabilities.find_capabilities(project_root, limit=100),
    )
    run_id = execution.record_run(
        project_root,
        run_kind="manual_test",
        source_kind="test",
        capability_name="command_runner",
        session_id="s1",
        ad_hoc=False,
        procedure_id="rule-01-01-task_complete-local_command",
        status="completed",
    )
    execution.record_event(
        project_root,
        event_kind="tool_invoked",
        source_kind="test",
        capability_name="command_runner",
        session_id="s1",
        procedure_id="rule-01-01-task_complete-local_command",
        run_id=run_id,
        status="observed",
        payload={"command": "python tools/blink.py"},
    )

    service = ActionSurfaceService(hub)
    result = service.status_bundle(
        project_root,
        queries=["local_command", "github_workflow_check", "local_command"],
        session_id="s1",
        limit=10,
    )

    assert result["overall_state"] == "attention_required"
    assert result["counts"]["queries"] == 2
    assert result["counts"]["ready_or_aligned"] == 1
    assert result["counts"]["attention_required"] == 1
    assert result["counts"]["by_state"]["aligned"] == 1
    assert result["counts"]["by_state"]["definition_without_capability"] == 1
    assert result["ready_items"][0]["query"] == "local_command"
    assert result["attention_items"][0]["query"] == "github_workflow_check"


def test_session_status_bundle_derives_queries_from_session_context(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After each completed task, run `python tools/blink.py`.\n",
        encoding="utf-8",
    )

    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    workflow = WorkflowActionService()
    procedures = ProcedureIndexStore()
    capabilities = CapabilityIndexStore()
    links = ProcedureCapabilityLinkStore()
    execution = ExecutionIndexStore()

    hub.sessions.create_session(project_root, "2026-03-24-a", "A", "Agent", "Goal mentions `memory_capture`.")
    hub.sessions.update_session(
        project_root,
        "2026-03-24-a",
        {
            "State": ["- Using `local_command` in this session."],
            "Upcoming": ["- Persist lessons with `write_memory` later."],
        },
    )
    hub.sessions.update_context(
        project_root,
        "2026-03-24-a",
        {
            "Relevant Commands": ["- `action_surface_compare`", "- python -m pytest"],
            "Session Facts": ["- Need to use `task_complete` after finishing."],
            "Constraints": ["- Keep `memory_capture` MCP-first."],
        },
    )

    compiled = workflow.compile_project_rules(project_root)
    procedures.sync_procedures(project_root, compiled)
    capabilities.sync_capabilities(
        project_root,
        [
            _Tool("command_runner", "Run local commands", aliases=["local_command"], family="execution"),
            _Tool("memory_capture", "Capture memory", family="memory"),
            _Tool("task_complete", "Complete tasks", family="task_lifecycle"),
            _Tool("action_surface_compare", "Compare action surfaces", family="analysis"),
        ],
    )
    links.sync_links(
        project_root,
        procedures.find_procedures(project_root, limit=100),
        capabilities.find_capabilities(project_root, limit=100),
    )
    execution.record_run(
        project_root,
        run_kind="manual_test",
        source_kind="test",
        capability_name="command_runner",
        session_id="2026-03-24-a",
        ad_hoc=True,
        status="completed",
    )

    service = ActionSurfaceService(hub)
    result = service.session_status_bundle(project_root, session_id="2026-03-24-a", limit=10, max_queries=10)

    assert result["query_source"] == "session_context"
    assert "local_command" in result["derived_queries"]
    assert "memory_capture" in result["derived_queries"]
    assert "task_complete" in result["derived_queries"]
    assert "action_surface_compare" in result["derived_queries"]
    assert result["counts"]["queries"] >= 4


def test_current_session_bundle_uses_managed_mode_session(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After each completed task, run `python tools/blink.py`.\n",
        encoding="utf-8",
    )

    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    workflow = WorkflowActionService()
    procedures = ProcedureIndexStore()
    capabilities = CapabilityIndexStore()
    links = ProcedureCapabilityLinkStore()

    hub.sessions.create_session(project_root, "2026-03-24-a", "A", "Agent", "Goal mentions `memory_capture`.")
    hub.sessions.update_context(
        project_root,
        "2026-03-24-a",
        {"Relevant Commands": ["- `memory_capture`", "- `task_complete`"]},
    )
    hub.managed_mode.set_mode(project_root, session_id="2026-03-24-a")

    compiled = workflow.compile_project_rules(project_root)
    procedures.sync_procedures(project_root, compiled)
    capabilities.sync_capabilities(
        project_root,
        [
            _Tool("memory_capture", "Capture memory", family="memory"),
            _Tool("task_complete", "Complete tasks", family="task_lifecycle"),
        ],
    )
    links.sync_links(
        project_root,
        procedures.find_procedures(project_root, limit=100),
        capabilities.find_capabilities(project_root, limit=100),
    )

    service = ActionSurfaceService(hub)
    result = service.current_session_bundle(project_root, limit=10, max_queries=10)

    assert result["ready"] is True
    assert result["resolution"] == "managed_mode"
    assert result["managed_mode_active"] is True
    assert result["session_id"] == "2026-03-24-a"
    assert "memory_capture" in result["derived_queries"]


def test_current_session_bundle_requires_selection_when_ambiguous(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    hub.sessions.create_session(project_root, "2026-03-24-a", "A", "Agent", "Goal A")
    hub.sessions.create_session(project_root, "2026-03-24-b", "B", "Agent", "Goal B")

    service = ActionSurfaceService(hub)
    result = service.current_session_bundle(project_root, limit=10, max_queries=10)

    assert result["ready"] is False
    assert result["reason"] == "session_selection_required"
    assert result["resolution"] == "none"
    assert len(result["active_sessions"]) == 2
