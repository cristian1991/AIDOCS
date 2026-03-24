from pathlib import Path

from aidocs_mcp.capability_index_store import CapabilityIndexStore
from aidocs_mcp.procedure_capability_link_store import ProcedureCapabilityLinkStore
from aidocs_mcp.procedure_index_store import ProcedureIndexStore
from aidocs_mcp.workflow_action_service import WorkflowActionService


class _TaskConfig:
    def __init__(self, mode: str = "forbidden") -> None:
        self.mode = mode


class _Tool:
    def __init__(self, name: str, description: str, aliases: list[str] | None = None) -> None:
        self.name = name
        self.title = None
        self.description = description
        self.tags = set()
        self.parameters = {"type": "object", "properties": {}}
        self.output_schema = {"type": "object", "properties": {}}
        self.meta = {"capability_aliases": aliases or []}
        self.task_config = _TaskConfig()
        self.timeout = None


def test_sync_links_resolves_direct_action_kind_matches_and_reports_gaps(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After push, check GitHub workflow status.\n"
        "- After each completed task, run `python tools/blink.py`.\n",
        encoding="utf-8",
    )

    workflow = WorkflowActionService()
    procedures = ProcedureIndexStore()
    capabilities = CapabilityIndexStore()
    links = ProcedureCapabilityLinkStore()

    compiled = workflow.compile_project_rules(project_root)
    procedures.sync_procedures(project_root, compiled)
    capabilities.sync_capabilities(project_root, [_Tool("local_command", "Run local commands")])

    count = links.sync_links(
        project_root,
        procedures.find_procedures(project_root, limit=100),
        capabilities.find_capabilities(project_root, limit=100),
    )
    status = links.link_status(project_root)
    resolved = links.list_links(project_root, procedure_id="rule-02-01-task_complete-local_command", limit=10)
    unresolved = links.list_links(project_root, unresolved_only=True, limit=10)

    assert count == 2
    assert status["procedure_capability_links"] == 2
    assert status["by_resolution"]["resolved"] == 1
    assert status["by_resolution"]["unresolved"] == 1
    assert resolved[0]["capability_name"] == "local_command"
    assert resolved[0]["match_basis"] == "direct_action_kind_match"
    assert any(item["action_kind"] == "github_workflow_check" for item in unresolved)


def test_sync_links_resolves_capability_alias_matches(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rules_path = project_root / ".MEMORY" / "rules" / "workflow.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Workflow\n\n"
        "## Automation Rules\n"
        "- After each completed task, run `python tools/blink.py`.\n",
        encoding="utf-8",
    )

    workflow = WorkflowActionService()
    procedures = ProcedureIndexStore()
    capabilities = CapabilityIndexStore()
    links = ProcedureCapabilityLinkStore()

    compiled = workflow.compile_project_rules(project_root)
    procedures.sync_procedures(project_root, compiled)
    capabilities.sync_capabilities(project_root, [_Tool("command_runner", "Run local commands", aliases=["local_command"])])

    links.sync_links(
        project_root,
        procedures.find_procedures(project_root, limit=100),
        capabilities.find_capabilities(project_root, limit=100),
    )
    resolved = links.list_links(project_root, procedure_id="rule-01-01-task_complete-local_command", limit=10)

    assert resolved[0]["capability_name"] == "command_runner"
    assert resolved[0]["match_basis"] == "capability_alias_match"
