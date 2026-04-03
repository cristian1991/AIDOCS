"""Tests for resilient session plan resolution fallbacks."""
from pathlib import Path

from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


def _make_runtime(tmp_path: Path) -> tuple[RuntimeService, Path]:
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text(
        "# Session\n\n## Title\n- t\n\n## Status\n- active\n\n## Owner\n- agent\n\n"
        "## Goal\n- g\n\n## Scope\n- s\n\n## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## Active Claims\n-\n\n## State\n-\n\n## Upcoming\n-\n\n## Blockers\n-\n\n"
        "## Last Updated\n- 2026-03-28 00:00\n",
        encoding="utf-8",
    )
    (templates / "context.md").write_text("# Context\n", encoding="utf-8")
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub=hub)
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    return runtime, project


def _remove_session_plan(project: Path, session_id: str) -> None:
    (project / ".MEMORY" / "sessions" / session_id / "plans" / "PLAN.md").unlink()


def test_plan_connect_without_session_plan_summarizes_roadmap_and_asks_user(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-roadmap", "Roadmap", "user", "Use roadmap")
    _remove_session_plan(project, "2026-03-30-roadmap")
    (project / "ROADMAP_2_0_0.md").write_text(
        "# Roadmap\n\n- [ ] Ship roadmap fallback\n- [x] Ignore completed step\n",
        encoding="utf-8",
    )
    runtime.hub.sessions.upsert_handoff_step(
        project,
        "2026-03-30-roadmap",
        text="Investigate roadmap parser",
        status="open",
    )
    runtime.hub.sessions.update_session(
        project,
        "2026-03-30-roadmap",
        {"Blockers": ["- Waiting on API contract"]},
    )
    runtime.hub.sessions.update_handoff(
        project,
        "2026-03-30-roadmap",
        {"What Matters Now": ["- Pending feedback from user on CLI wording"]},
    )

    result = runtime.plan_connect(project, "2026-03-30-roadmap", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "roadmap_summary"
    assert result["next_action"] == "ask_user_what_to_work_on"
    assert "ask the user" in result["instruction"].lower()
    assert result["roadmap_steps"][0]["text"] == "Ship roadmap fallback"
    open_work = {item["text"] for item in result["open_work"]}
    assert "Investigate roadmap parser" in open_work
    assert "Waiting on API contract" in open_work
    assert "Pending feedback from user on CLI wording" in open_work


def test_plan_connect_without_plan_does_not_recreate_missing_handoff(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-read-only", "Read Only", "user", "Inspect state")
    _remove_session_plan(project, "2026-03-30-read-only")
    handoff_path = project / ".MEMORY" / "sessions" / "2026-03-30-read-only" / "2026-03-30-read-only.handoff.md"
    handoff_path.unlink()
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [ ] Read existing roadmap only\n", encoding="utf-8")

    result = runtime.plan_connect(project, "2026-03-30-read-only", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "roadmap_summary"
    assert handoff_path.exists() is False


def test_plan_connect_without_session_plan_uses_open_work_when_no_roadmap(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-open-work", "Open Work", "user", "Use open work")
    _remove_session_plan(project, "2026-03-30-open-work")
    runtime.hub.sessions.upsert_handoff_step(
        project,
        "2026-03-30-open-work",
        text="Review unresolved handoff item",
        status="open",
    )
    runtime.hub.sessions.update_session(
        project,
        "2026-03-30-open-work",
        {"Blockers": ["- Waiting for user confirmation"]},
    )
    runtime.hub.sessions.update_handoff(
        project,
        "2026-03-30-open-work",
        {"Open Questions": ["- Need approval on schema choice"]},
    )

    result = runtime.plan_connect(project, "2026-03-30-open-work", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "session_open_work"
    assert result["roadmap_steps"] == []
    assert result["next_action"] == "ask_user_what_to_work_on"
    assert "ask the user" in result["instruction"].lower()
    open_work = {item["text"] for item in result["open_work"]}
    assert "Review unresolved handoff item" in open_work
    assert "Waiting for user confirmation" in open_work
    assert "Need approval on schema choice" in open_work
    pending_feedback = {
        item["text"]
        for item in result["open_work"]
        if item["status"] == "pending_user_feedback"
    }
    assert "Waiting for user confirmation" in pending_feedback
    assert "Need approval on schema choice" in pending_feedback


def test_plan_connect_without_plan_or_roadmap_asks_user_for_next_steps(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-empty", "Empty", "user", "Need direction")
    _remove_session_plan(project, "2026-03-30-empty")

    result = runtime.plan_connect(project, "2026-03-30-empty", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "none"
    assert result["next_action"] == "create_plan_or_roadmap"
    assert "ask the user" in result["instruction"].lower()
