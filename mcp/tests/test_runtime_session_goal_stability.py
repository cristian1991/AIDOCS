from pathlib import Path

from aidocs_mcp.runtime_service import RuntimeService
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
    (root.parent / "index.aidocs").write_text(
        "# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n", encoding="utf-8"
    )
    (root.parent / "global-instructions.aidocs").write_text(
        "# Global Instructions\n", encoding="utf-8"
    )
    (root.parent / "coding-standards.aidocs").write_text(
        "# Coding Standards\n", encoding="utf-8"
    )
    (root.parent / "memory-system.aidocs").write_text(
        "# Memory System\n", encoding="utf-8"
    )
    (root.parent / "research-safety.aidocs").write_text(
        "# Research Safety\n", encoding="utf-8"
    )
    (root.parent / "personalities").mkdir(parents=True, exist_ok=True)
    (root.parent / "personalities" / "default.aidocs").write_text(
        "# Default Personality\n", encoding="utf-8"
    )
    memory_template = root / "memory"
    memory_template.mkdir(parents=True, exist_ok=True)
    (memory_template / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (memory_template / "rules").mkdir(parents=True, exist_ok=True)
    (memory_template / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n## Workflow Rules\n", encoding="utf-8"
    )
    (memory_template / "rules" / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n## Workflow Actions\n", encoding="utf-8"
    )


def _seed_project(project_root: Path) -> None:
    mem = project_root / ".MEMORY"
    (mem / ".aidocs").mkdir(parents=True, exist_ok=True)
    for name in [
        "index.aidocs",
        "global-instructions.aidocs",
        "coding-standards.aidocs",
        "memory-system.aidocs",
    ]:
        (mem / ".aidocs" / name).write_text(f"# {name}\n", encoding="utf-8")
    (mem / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")


def _make_runtime_with_session(
    tmp_path: Path, goal: str = "Ship host hooks"
) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session_id = "2026-03-30-goal-stability"
    hub.sessions.create_session(
        project_root, session_id, "Runtime stability", "Agent", goal
    )
    hub.sessions.update_plan(
        project_root,
        session_id,
        {
            "Purpose": [f"- {goal}"],
            "End Goal": [f"- {goal}"],
        },
    )
    hub.sessions.update_handoff(project_root, session_id, {"Purpose": [f"- {goal}"]})
    return runtime, project_root, session_id


def test_task_begin_does_not_overwrite_session_goal_when_working_subtask_starts(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_session(
        tmp_path, goal="Ship host hooks"
    )

    started = runtime.task_begin(
        project_root,
        session_id,
        goal="Fix one failing test",
        state=["Investigating runtime goal stability"],
        upcoming=["Run targeted pytest"],
    )

    assert started["session"]["sections"]["Goal"][0] == "- Ship host hooks"
    assert any(
        "Fix one failing test" in line
        for line in started["session"]["sections"]["State"]
    )
    assert any(
        "Fix one failing test" in line
        for line in started["plan"]["sections"]["Current State"]
    )


def test_task_update_preserves_original_session_purpose(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_session(
        tmp_path, goal="Ship host hooks"
    )

    runtime.task_begin(project_root, session_id, goal="Fix one failing test")
    updated = runtime.task_update(
        project_root, session_id, state=["Investigating query gate"]
    )

    assert updated["plan"]["sections"]["Purpose"][0] == "- Ship host hooks"
    assert (
        updated["plan"]["sections"]["Current State"][0] == "- Investigating query gate"
    )


def test_task_complete_updates_execution_state_without_rewriting_high_level_goal(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_session(
        tmp_path, goal="Ship host hooks"
    )

    runtime.task_begin(
        project_root,
        session_id,
        goal="Fix one failing test",
        state=["Investigating query gate"],
    )
    completed = runtime.task_complete(
        project_root,
        session_id,
        result_summary="Fixed query gate",
        verification_evidence={
            "commands_run": ["pytest tests/test_query_gate.py -q"],
            "command_results": ["1 passed"],
        },
    )

    assert completed["session"]["sections"]["Goal"][0] == "- Ship host hooks"
    assert completed["plan"]["sections"]["Purpose"][0] == "- Ship host hooks"
    assert any(
        "Fixed query gate" in line for line in completed["session"]["sections"]["State"]
    )
    assert any(
        "Completion result: Fixed query gate" in line
        for line in completed["plan"]["sections"]["Validation"]
    )
