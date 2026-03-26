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
    rules_dir = project_root / ".MEMORY" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
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
    rules_dir = project_root / ".MEMORY" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "workflow-rules.md").write_text(
        "# Workflow\n\n"
        "## Workflow Notes\n"
        "- Session entry order: read the router first.\n\n"
        "## Workflow Rules\n"
        "- After push, check git status.\n",
        encoding="utf-8",
    )

    result = service.compile_project_rules(project_root)

    assert result["action_count"] == 1
    assert result["actions"][0]["kind"] == "git_status_check"


def test_compile_project_rules_reports_unsupported_rules(tmp_path: Path) -> None:
    service = WorkflowActionService()
    project_root = tmp_path / "project"
    rules_dir = project_root / ".MEMORY" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
        "- After push, order a coffee.\n",
        encoding="utf-8",
    )

    result = service.compile_project_rules(project_root)

    assert result["action_count"] == 0
    assert result["unsupported_count"] == 1
    assert "unsupported action" in result["unsupported_rules"][0]["reason"]


def test_compile_project_rules_matches_multilingual_workflow_tokens(tmp_path: Path) -> None:
    service = WorkflowActionService()
    project_root = tmp_path / "project"
    rules_dir = project_root / ".MEMORY" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
        "- After push, verificar github workflow status.\n",
        encoding="utf-8",
    )

    result = service.compile_project_rules(project_root)

    assert result["action_count"] == 1
    assert result["unsupported_count"] == 0
    assert result["actions"][0]["kind"] == "github_workflow_check"


def test_compile_project_rules_supports_separate_rule_and_action_sections(tmp_path: Path) -> None:
    service = WorkflowActionService()
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

    result = service.compile_project_rules(project_root)
    compiled = service.read_compiled(project_root)

    assert result["action_definition_count"] == 2
    assert result["rule_count"] == 1
    assert result["action_count"] == 2
    assert compiled is not None
    assert [item["name"] for item in compiled["action_definitions"]] == ["ci_status", "repo_status"]
    assert [item["kind"] for item in compiled["actions"]] == ["github_workflow_check", "git_status_check"]
    assert [item["action_ref"] for item in compiled["actions"]] == ["ci_status", "repo_status"]
    assert [item["action_ref"] for item in compiled["rules"][0]["steps"]] == ["ci_status", "repo_status"]


def test_compile_project_rules_reads_split_workflow_files_first(tmp_path: Path) -> None:
    service = WorkflowActionService()
    project_root = tmp_path / "project"
    rules_dir = project_root / ".MEMORY" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n"
        "## Workflow Actions\n"
        "- ci_status: check github workflow status\n",
        encoding="utf-8",
    )
    (rules_dir / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
        "- After push, ci_status.\n",
        encoding="utf-8",
    )
    result = service.compile_project_rules(project_root)

    assert result["action_definition_count"] == 1
    assert result["rule_count"] == 1
    assert result["action_count"] == 1
    assert result["actions"][0]["action_ref"] == "ci_status"


# ── triggers_for_action_kind tests ───────────────────────────────────


def test_triggers_for_action_kind_maps_known_kinds(tmp_path: Path) -> None:
    """Known action kinds map to the correct workflow triggers."""
    service = WorkflowActionService()

    assert service.triggers_for_action_kind("task_complete") == ["task_complete"]
    assert service.triggers_for_action_kind("task_begin") == ["task_begin"]
    assert service.triggers_for_action_kind("edit") == ["task_complete"]
    assert service.triggers_for_action_kind("write_memory") == ["memory_write"]
    assert service.triggers_for_action_kind("archive") == ["archive"]
    assert service.triggers_for_action_kind("project_update") == ["project_update"]


def test_triggers_for_action_kind_returns_empty_for_unknown(tmp_path: Path) -> None:
    """Unknown action kinds return no triggers."""
    service = WorkflowActionService()

    assert service.triggers_for_action_kind("understand") == []
    assert service.triggers_for_action_kind("trace") == []
    assert service.triggers_for_action_kind("nonexistent") == []


# ── pending_actions_for_trigger tests ────────────────────────────────


def test_pending_actions_for_trigger_returns_matching_actions(tmp_path: Path) -> None:
    """pending_actions_for_trigger returns only actions with matching trigger."""
    service = WorkflowActionService()
    project_root = tmp_path / "project"

    (project_root / ".MEMORY" / "rules").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
        "- After each completed task, run `python tools/check.py`.\n"
        "- After each push, run `python tools/deploy.py`.\n",
        encoding="utf-8",
    )

    service.compile_project_rules(project_root)

    task_actions = service.pending_actions_for_trigger(project_root, "task_complete")
    push_actions = service.pending_actions_for_trigger(project_root, "after_git_push")
    nonexistent = service.pending_actions_for_trigger(project_root, "nonexistent_trigger")

    assert len(task_actions) == 1
    assert task_actions[0]["trigger"] == "task_complete"
    assert len(push_actions) == 1
    assert push_actions[0]["trigger"] == "after_git_push"
    assert len(nonexistent) == 0


def test_pending_actions_returns_empty_when_no_compiled_rules(tmp_path: Path) -> None:
    """pending_actions_for_trigger returns empty when no rules have been compiled."""
    service = WorkflowActionService()
    project_root = tmp_path / "project"

    result = service.pending_actions_for_trigger(project_root, "task_complete")
    assert result == []


# ── status tests ─────────────────────────────────────────────────────


def test_status_reports_compiled_state(tmp_path: Path) -> None:
    """status() returns compiled rule stats after compile_project_rules."""
    service = WorkflowActionService()
    project_root = tmp_path / "project"

    (project_root / ".MEMORY" / "rules").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
        "- After each completed task, run `python tools/check.py`.\n",
        encoding="utf-8",
    )
    service.compile_project_rules(project_root)

    status = service.status(project_root)
    assert status["exists"] is True
    assert status["source_exists"] is True
    assert status["action_count"] == 1
    assert status["section_found"] is True


def test_status_reports_missing_state(tmp_path: Path) -> None:
    """status() returns exists=False when nothing has been compiled."""
    service = WorkflowActionService()
    project_root = tmp_path / "project"

    status = service.status(project_root)
    assert status["exists"] is False
    assert status["action_count"] == 0
