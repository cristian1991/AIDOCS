from pathlib import Path

from aidocs_mcp.procedure_capability_link_store import ProcedureCapabilityLinkStore


def test_sync_links_resolves_workflow_rule_backtick_capability_mentions(tmp_path: Path) -> None:
    store = ProcedureCapabilityLinkStore()
    project_root = tmp_path / "project"

    procedures = [
        {
            "procedure_id": "workflow-rule:Git analysis:1",
            "definition_kind": "workflow_rule",
            "body_text": "Start with `git_fork_status` before merge work.",
        },
        {
            "procedure_id": "workflow-rule:Git analysis:2",
            "definition_kind": "workflow_rule",
            "body_text": "Use `git_diag` if MCP git behavior looks wrong.",
        },
    ]
    capabilities = [
        {"name": "git_fork_status", "aliases": []},
        {"name": "git_diag", "aliases": []},
    ]

    count = store.sync_links(project_root, procedures, capabilities)
    rows = store.list_links(project_root, limit=10)

    assert count == 2
    assert len(rows) == 2
    assert rows[0]["capability_name"] == "git_fork_status"
    assert rows[0]["link_kind"] == "procedure_mentions_capability"
    assert rows[0]["match_basis"] == "workflow_rule_backtick_match"
    assert rows[1]["capability_name"] == "git_diag"


def test_sync_links_resolves_workflow_rule_alias_mentions(tmp_path: Path) -> None:
    store = ProcedureCapabilityLinkStore()
    project_root = tmp_path / "project"

    procedures = [
        {
            "procedure_id": "workflow-rule:Memory:1",
            "definition_kind": "workflow_rule",
            "body_text": "Use `remember` to persist a durable workflow note.",
        }
    ]
    capabilities = [
        {"name": "memory_capture", "aliases": ["remember", "write_memory"]},
    ]

    count = store.sync_links(project_root, procedures, capabilities)
    rows = store.list_links(project_root, limit=10)

    assert count == 1
    assert len(rows) == 1
    assert rows[0]["capability_name"] == "memory_capture"
    assert rows[0]["match_basis"] == "workflow_rule_backtick_match"


def test_sync_links_skips_internal_workflow_actions_without_capability_matches(tmp_path: Path) -> None:
    store = ProcedureCapabilityLinkStore()
    project_root = tmp_path / "project"

    procedures = [
        {
            "procedure_id": "rule-01-01-after_git_push-github_workflow_check",
            "definition_kind": "workflow_action",
            "action_kind": "github_workflow_check",
        }
    ]
    capabilities = [{"name": "git_diag", "aliases": []}]

    count = store.sync_links(project_root, procedures, capabilities)
    rows = store.list_links(project_root, limit=10)

    assert count == 0
    assert rows == []
