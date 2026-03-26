from pathlib import Path

from aidocs_mcp.procedure_index_store import ProcedureIndexStore
from aidocs_mcp.workflow_action_service import WorkflowActionService


def test_sync_procedures_indexes_workflow_action_defs_and_flow_steps(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_dir = project_root / ".MEMORY" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n"
        "## Workflow Actions\n"
        "- ci_status: check github workflow status\n"
        "- repo_status: check git status\n",
        encoding="utf-8",
    )
    (rules_dir / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
        "- After push, ci_status then repo_status.\n",
        encoding="utf-8",
    )

    compiled = WorkflowActionService().compile_project_rules(project_root)
    store = ProcedureIndexStore()
    count = store.sync_procedures(project_root, compiled)
    rows = store.find_procedures(project_root, limit=20)

    kinds = {row["definition_kind"] for row in rows}

    assert count == len(rows)
    assert "workflow_action_definition" in kinds
    assert "workflow_flow" in kinds
    assert "workflow_flow_step" in kinds
    assert any(row["title"] == "ci_status" for row in rows if row["definition_kind"] == "workflow_action_definition")
    assert any(row["title"] == "repo_status" for row in rows if row["definition_kind"] == "workflow_flow_step")


def test_sync_procedures_reads_split_workflow_files(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_dir = project_root / ".MEMORY" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
        "- After push, ci_status.\n",
        encoding="utf-8",
    )
    (rules_dir / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n"
        "## Workflow Actions\n"
        "- ci_status: check github workflow status\n",
        encoding="utf-8",
    )

    compiled = WorkflowActionService().compile_project_rules(project_root)
    store = ProcedureIndexStore()
    rows = store.find_procedures(project_root, limit=20)
    if not rows:
        store.sync_procedures(project_root, compiled)
        rows = store.find_procedures(project_root, limit=20)

    assert any(row["source_path"] == ".MEMORY/rules/workflow-rules.md" for row in rows if row["definition_kind"] == "workflow_rule")
