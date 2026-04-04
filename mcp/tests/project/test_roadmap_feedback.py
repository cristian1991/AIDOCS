import asyncio
from pathlib import Path

from aidocs_mcp.mcp_server import create_server
from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n- agent\n\n"
        "## Goal\n- goal\n\n"
        "## Scope\n- scope\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## Active Claims\n-\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- 2026-03-30 00:00\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text(
        "# Context\n\n"
        "## Relevant Files\n\n"
        "## Relevant Commands\n\n"
        "## Relevant Snippets\n\n"
        "## Session Facts\n\n"
        "## Constraints\n",
        encoding="utf-8",
    )


def _make_runtime(tmp_path: Path) -> tuple[RuntimeService, Path]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    return runtime, project_root


def test_completed_plan_sequence_moves_matching_roadmap_step_to_pending_user_feedback(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-feedback", "Feedback", "user", "Close a roadmap step")
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [~] Ship startup flow\n", encoding="utf-8")

    result = runtime.mark_roadmap_step_pending_feedback(project, "Ship startup flow")

    assert result["status"] == "pending_user_feedback"
    assert "[>] Ship startup flow" in (project / "ROADMAP_2_0_0.md").read_text(encoding="utf-8")


def test_user_feedback_can_move_pending_step_back_to_in_progress(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [>] Ship startup flow\n", encoding="utf-8")

    result = runtime.update_roadmap_feedback_state(project, "Ship startup flow", feedback="needs fixes")

    assert result["status"] == "in_progress"
    assert "[~] Ship startup flow" in (project / "ROADMAP_2_0_0.md").read_text(encoding="utf-8")


def test_prose_only_plan_addition_creates_awaiting_feedback_structure(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-prose", "Prose", "user", "Normalize prose")
    runtime.hub.sessions.update_plan(
        project,
        "2026-03-30-prose",
        {"Steps": ["- The agent should validate the roadmap state and then continue with CLI fixes."]},
    )

    result = runtime.normalize_plan_prose(project, "2026-03-30-prose")

    assert result["status"] == "awaiting_feedback"
    assert result["original_prose"]
    assert any("[>]" in line for line in result["normalized_lines"])


def test_plan_connect_surfaces_prose_normalization_feedback(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-connect", "Connect", "user", "Normalize prose through plan connect")
    runtime.hub.sessions.update_plan(
        project,
        "2026-03-30-connect",
        {"Steps": ["- The agent should validate roadmap state before closing the loop."]},
    )

    result = runtime.plan_connect(project, "2026-03-30-connect", run_preflight=False)

    assert result["plan_feedback"]["status"] == "awaiting_feedback"
    assert result["plan_feedback"]["original_prose"] == [
        "The agent should validate roadmap state before closing the loop."
    ]
    assert any("[>] Validate roadmap state before closing the loop" in line for line in result["plan_feedback"]["normalized_lines"])
    plan_text = (project / ".MEMORY" / "sessions" / "2026-03-30-connect" / "plans" / "PLAN.md").read_text(encoding="utf-8")
    assert "- The agent should validate roadmap state before closing the loop." in plan_text
    assert "- [>] Validate roadmap state before closing the loop" not in plan_text


def test_roadmap_feedback_mutation_uses_only_roadmap_2_file(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    (project / "ROADMAP.md").write_text("# Roadmap\n\n- [~] Ship startup flow\n", encoding="utf-8")

    try:
        runtime.mark_roadmap_step_pending_feedback(project, "Ship startup flow")
    except ValueError as exc:
        assert "No actionable roadmap step matched" in str(exc)
    else:
        raise AssertionError("Expected mutation to ignore fallback roadmap files")

    assert "[~] Ship startup flow" in (project / "ROADMAP.md").read_text(encoding="utf-8")


def test_mcp_tool_updates_roadmap_feedback_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [>] Ship startup flow\n", encoding="utf-8")

    async def run() -> object:
        server = create_server()
        tools = await server.list_tools()
        assert any(item.name == "roadmap_feedback_update" for item in tools)
        return await server.call_tool(
            "roadmap_feedback_update",
            {
                "root": str(project),
                "step_text": "Ship startup flow",
                "feedback": "needs fixes",
            },
        )

    result = asyncio.run(run())
    payload = result[0].text if isinstance(result, list) else str(result)

    assert '"status":"in_progress"' in payload.lower()
    assert "[~] Ship startup flow" in (project / "ROADMAP_2_0_0.md").read_text(encoding="utf-8")


def test_feedback_update_requires_pending_user_feedback_state(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [~] Ship startup flow\n", encoding="utf-8")

    result = runtime.update_roadmap_feedback_state(project, "Ship startup flow", feedback="needs fixes")

    assert result["ok"] is False
    assert result["error_code"] == "roadmap_feedback_state_required"
    assert result["current_status"] == "in_progress"
    assert "[~] Ship startup flow" in (project / "ROADMAP_2_0_0.md").read_text(encoding="utf-8")


def test_feedback_update_reports_ambiguous_duplicate_roadmap_items(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    (project / "ROADMAP_2_0_0.md").write_text(
        "# Roadmap\n\n- [>] Ship startup flow\n- [>] Ship startup flow\n",
        encoding="utf-8",
    )

    result = runtime.update_roadmap_feedback_state(project, "Ship startup flow", feedback="needs fixes")

    assert result["ok"] is False
    assert result["error_code"] == "roadmap_step_ambiguous"
    assert result["match_count"] == 2
    roadmap_text = (project / "ROADMAP_2_0_0.md").read_text(encoding="utf-8")
    assert roadmap_text.count("[>] Ship startup flow") == 2
