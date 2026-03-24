from pathlib import Path

from aidocs_mcp.policy_service import PolicyService
from aidocs_mcp.service_hub import AidocsServiceHub


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


def test_preflight_allows_direct_inspection_for_user_target(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    policy = PolicyService(hub)
    project_root = tmp_path / "project"

    result = policy.preflight_action(
        project_root,
        action_kind="inspect",
        user_explicit_targets=["src/file.cs"],
    )

    assert result["allowed"] is True
    assert result["requires_session"] is False
    assert result["reason"] == "explicit_user_target_can_be_inspected_first"


def test_preflight_requires_session_when_multiple_active_exist(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    policy = PolicyService(hub)
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.create_session(project_root, "2026-03-23-b", "B", "Agent", "Goal B")

    result = policy.preflight_action(project_root, action_kind="edit")

    assert result["allowed"] is False
    assert result["requires_session"] is True
    assert result["reason"] == "multiple_active_sessions"


def test_preflight_auto_allows_single_active_session_for_edit(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    policy = PolicyService(hub)
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.create_session(project_root, "2026-03-23-b", "B", "Agent", "Goal B", status="paused")

    result = policy.preflight_action(project_root, action_kind="edit")

    assert result["allowed"] is True
    assert result["requires_session"] is True
    assert result["selected_session"] == "2026-03-23-a"
