from pathlib import Path

from aidocs_mcp.procedure_index_store import ProcedureIndexStore
from aidocs_mcp.workflow_action_service import WorkflowActionService


def test_sync_procedures_indexes_workflow_rules_and_compiled_actions(tmp_path: Path) -> None:
    store = ProcedureIndexStore()
    workflow = WorkflowActionService()
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Process\n"
        "- Read the router first.\n"
        "- Keep memory entries concise.\n\n"
        "## Automation Rules\n"
        "- After push, check GitHub workflow status.\n"
        "- After each completed task, run `python tools/blink.py`.\n",
        encoding="utf-8",
    )

    compiled = workflow.compile_project_rules(project_root)
    count = store.sync_procedures(project_root, compiled)
    status = store.procedure_status(project_root)
    automation = store.find_procedures(project_root, query="GitHub workflow", limit=20)
    local_command = store.get_procedure(project_root, "rule-02-01-task_complete-local_command")

    assert count == 6
    assert status["procedure_definitions"] == 6
    assert status["by_kind"]["workflow_rule"] == 4
    assert status["by_kind"]["workflow_action"] == 2
    assert any(item["definition_kind"] == "workflow_rule" for item in automation)
    assert any(item["definition_kind"] == "workflow_action" for item in automation)
    assert local_command is not None
    assert local_command["definition_kind"] == "workflow_action"
    assert local_command["trigger"] == "task_complete"
    assert local_command["action_kind"] == "local_command"
    assert local_command["action_payload"]["command"] == "python tools/blink.py"


def test_sync_procedures_captures_top_level_workflow_bullets_without_sections(tmp_path: Path) -> None:
    store = ProcedureIndexStore()
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "- Stop on unsafe sequences.\n"
        "- Keep memory concise.\n",
        encoding="utf-8",
    )

    count = store.sync_procedures(project_root, compiled_workflow=None)
    status = store.procedure_status(project_root)
    procedures = store.find_procedures(project_root, query="memory concise", limit=10)

    assert count == 2
    assert status["by_kind"]["workflow_rule"] == 2
    assert procedures[0]["section_name"] == "General"
