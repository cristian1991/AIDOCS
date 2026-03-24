from pathlib import Path

from aidocs_mcp.workflow_action_service import WorkflowActionService


def test_compile_project_rules_writes_empty_config_when_source_missing(tmp_path: Path) -> None:
    service = WorkflowActionService()
    project_root = tmp_path / "project"

    result = service.compile_project_rules(project_root)

    assert result["source_exists"] is False
    assert result["section_found"] is False
    assert result["action_count"] == 0
    assert (project_root / ".MEMORY" / "config" / "workflow-actions.json").is_file()


def test_compile_project_rules_compiles_supported_automation_rules(tmp_path: Path) -> None:
    service = WorkflowActionService()
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After each completed task, commit and push.\n"
        "- After push, check GitHub workflow status.\n"
        "- After deploy success, ssh `prod` `systemctl status app`.\n"
        "- After each completed task, run `python tools/blink.py`.\n",
        encoding="utf-8",
    )

    result = service.compile_project_rules(project_root)
    compiled = service.read_compiled(project_root)

    assert result["action_count"] == 4
    assert result["unsupported_count"] == 0
    assert compiled is not None
    assert [item["kind"] for item in compiled["actions"]] == [
        "git_commit_and_push",
        "github_workflow_check",
        "ssh_command",
        "local_command",
    ]
    assert compiled["actions"][2]["host"] == "prod"
    assert compiled["actions"][2]["remote_command"] == "systemctl status app"
    assert compiled["actions"][3]["command"] == "python tools/blink.py"


def test_compile_project_rules_ignores_prose_outside_automation_section(tmp_path: Path) -> None:
    service = WorkflowActionService()
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "- Session entry order: read the router first.\n\n"
        "## Automation Rules\n"
        "- After push, check git status.\n",
        encoding="utf-8",
    )

    result = service.compile_project_rules(project_root)

    assert result["action_count"] == 1
    assert result["actions"][0]["kind"] == "git_status_check"


def test_compile_project_rules_reports_unsupported_rules(tmp_path: Path) -> None:
    service = WorkflowActionService()
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After push, order a coffee.\n",
        encoding="utf-8",
    )

    result = service.compile_project_rules(project_root)

    assert result["action_count"] == 0
    assert result["unsupported_count"] == 1
    assert "unsupported action" in result["unsupported_rules"][0]["reason"]
