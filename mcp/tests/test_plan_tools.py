"""Tests for plan_preflight and plan_connect tools."""
from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore
from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub
from aidocs_mcp.session_store import SessionStore


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


def _write_plan(project: Path, session_id: str, steps: list[tuple[bool, str]]) -> None:
    """Overwrite the default PLAN.md created by create_session with specific steps."""
    plan_path = project / ".MEMORY" / "sessions" / session_id / "plans" / "PLAN.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Plan\n", "\n## Purpose\n- Implement the session goal\n", "\n## Steps\n"]
    for done, text in steps:
        mark = "x" if done else " "
        lines.append(f"- [{mark}] {text}\n")
    lines.append("\n## End Goal\n- Complete all steps\n")
    plan_path.write_text("".join(lines), encoding="utf-8")


# ── plan_preflight tests ─────────────────────────────────────────────


def test_preflight_returns_step_analysis(tmp_path: Path) -> None:
    """preflight analyzes each incomplete step and classifies it."""
    runtime, project = _make_runtime(tmp_path)

    # Create project files so investigate finds something
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "auth.py").write_text("class AuthService:\n    def login(self): pass\n", encoding="utf-8")
    CodeIndexStore().sync_code_files(project)

    runtime.hub.sessions.create_session(project, "2026-03-28-test", "Test", "user", "Test preflight")
    _write_plan(project, "2026-03-28-test", [
        (True, "Set up project structure"),
        (False, "Add authentication service"),
        (False, "Create new dashboard UI"),
    ])

    result = runtime.plan_preflight(project, "2026-03-28-test")

    assert result["total_steps"] == 2
    assert len(result["steps"]) == 2
    assert result["steps"][0]["step"] == "Add authentication service"
    assert result["steps"][0]["status"] in ("extend", "integrate", "create")
    assert "summary" in result
    assert result["summary"]["decisions_needed"] >= 0


def test_preflight_all_complete_returns_message(tmp_path: Path) -> None:
    """preflight on a fully complete plan returns a completion message."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-28-done", "Done", "user", "All done")
    _write_plan(project, "2026-03-28-done", [
        (True, "Step one"),
        (True, "Step two"),
    ])

    result = runtime.plan_preflight(project, "2026-03-28-done")

    assert result["steps"] == []
    assert "complete" in result["message"].lower()


def test_preflight_default_plan_has_no_steps(tmp_path: Path) -> None:
    """preflight on default auto-created plan (no checkbox steps) returns empty."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-28-noplan", "No Plan", "user", "No plan")

    result = runtime.plan_preflight(project, "2026-03-28-noplan")

    assert result["steps"] == []
    assert "complete" in result.get("message", "").lower()


def test_preflight_classifies_greenfield_as_create(tmp_path: Path) -> None:
    """Steps with no matching code are classified as 'create' with decisions."""
    runtime, project = _make_runtime(tmp_path)
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    CodeIndexStore().sync_code_files(project)

    runtime.hub.sessions.create_session(project, "2026-03-28-green", "Green", "user", "Greenfield")
    _write_plan(project, "2026-03-28-green", [
        (False, "Implement quantum flux capacitor service"),
    ])

    result = runtime.plan_preflight(project, "2026-03-28-green")

    assert result["steps"][0]["status"] == "create"
    assert result["summary"]["create"] == 1
    assert len(result.get("decisions", [])) >= 1


# ── plan_connect tests ───────────────────────────────────────────────


def test_connect_shows_progress_and_next_steps(tmp_path: Path) -> None:
    """connect returns progress, completed/incomplete counts, and next steps."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-28-conn", "Connect", "user", "Test connect")
    _write_plan(project, "2026-03-28-conn", [
        (True, "Set up project"),
        (True, "Add models"),
        (False, "Add controllers"),
        (False, "Add views"),
        (False, "Add tests"),
    ])

    result = runtime.plan_connect(project, "2026-03-28-conn", run_preflight=False)

    assert result["connected"] is True
    assert result["progress"] == "2/5"
    assert result["completed_count"] == 2
    assert result["incomplete_count"] == 3
    assert result["next_steps"][0] == "Add controllers"
    assert "instruction" in result


def test_connect_with_preflight_includes_decisions(tmp_path: Path) -> None:
    """connect with run_preflight=True includes step analysis and decisions."""
    runtime, project = _make_runtime(tmp_path)
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    CodeIndexStore().sync_code_files(project)

    runtime.hub.sessions.create_session(project, "2026-03-28-pf", "PF", "user", "Test preflight")
    _write_plan(project, "2026-03-28-pf", [
        (True, "Done step"),
        (False, "Build something new that does not exist anywhere"),
    ])

    result = runtime.plan_connect(project, "2026-03-28-pf", run_preflight=True)

    assert result["connected"] is True
    assert "step_analysis" in result
    assert "recommended_order" in result


def test_connect_fully_complete_plan(tmp_path: Path) -> None:
    """connect on a fully complete plan reports completion."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-28-full", "Full", "user", "Done")
    _write_plan(project, "2026-03-28-full", [
        (True, "Step A"),
        (True, "Step B"),
    ])

    result = runtime.plan_connect(project, "2026-03-28-full", run_preflight=False)

    assert result["connected"] is True
    assert result["progress"] == "2/2"
    assert result["incomplete_count"] == 0
    assert "complete" in result["instruction"].lower()


def test_connect_default_plan_shows_complete(tmp_path: Path) -> None:
    """connect on default auto-created plan (no checkbox steps) reports complete."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-28-no", "No", "user", "No plan")

    result = runtime.plan_connect(project, "2026-03-28-no", run_preflight=False)

    assert result["connected"] is True
    assert result["incomplete_count"] == 0
    assert "complete" in result["instruction"].lower()


def test_connect_includes_purpose_and_end_goal(tmp_path: Path) -> None:
    """connect extracts purpose and end goal from plan sections."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-28-goal", "Goal", "user", "Ship it")
    _write_plan(project, "2026-03-28-goal", [
        (False, "Last step"),
    ])

    result = runtime.plan_connect(project, "2026-03-28-goal", run_preflight=False)

    assert result["connected"] is True
    assert "purpose" in result
    assert "end_goal" in result
