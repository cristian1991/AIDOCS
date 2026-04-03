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


def test_plan_connect_uses_session_plan_when_present(tmp_path: Path) -> None:
    """An existing session plan takes precedence over roadmap fallback."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-plan", "Plan", "user", "Use session plan")
    _write_plan(project, "2026-03-30-plan", [
        (False, "Implement session step"),
    ])
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [ ] Ship roadmap fallback\n", encoding="utf-8")

    result = runtime.plan_connect(project, "2026-03-30-plan", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "session_plan"
    assert result["next_steps"] == ["Implement session step"]


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


def test_connect_preserves_nonterminal_plan_states(tmp_path: Path) -> None:
    """connect and preflight keep in-progress/feedback/blocked plan items visible."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-rich", "Rich", "user", "Track richer states")
    plan_path = project / ".MEMORY" / "sessions" / "2026-03-30-rich" / "plans" / "PLAN.md"
    plan_path.write_text(
        "# Plan\n"
        "\n## Purpose\n- Track richer states\n"
        "\n## Steps\n"
        "- [x] Completed step\n"
        "- [~] In progress step\n"
        "- [>] Awaiting user feedback\n"
        "- [!] Blocked step\n"
        "\n## End Goal\n- Finish the tracked work\n",
        encoding="utf-8",
    )

    connected = runtime.plan_connect(project, "2026-03-30-rich", run_preflight=False)
    preflight = runtime.plan_preflight(project, "2026-03-30-rich")

    assert connected["progress"] == "1/4"
    assert connected["completed_count"] == 1
    assert connected["incomplete_count"] == 3
    assert connected["next_steps"] == [
        "In progress step",
        "Awaiting user feedback",
        "Blocked step",
    ]
    assert preflight["total_steps"] == 3
    assert [step["step"] for step in preflight["steps"]] == [
        "In progress step",
        "Awaiting user feedback",
        "Blocked step",
    ]


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


def test_plan_connect_with_lane_aware_steps_keeps_checkbox_progress(tmp_path: Path) -> None:
    """connect ignores lane metadata lines and only tracks actual checkbox work."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-lane-connect", "Lane Connect", "user", "Track lane plan")
    plan_path = project / ".MEMORY" / "sessions" / "2026-03-30-lane-connect" / "plans" / "PLAN.md"
    plan_path.write_text(
        "# Plan\n"
        "\n## Purpose\n- Track lane-aware work\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: homepage-hero\n"
        "- Files: src/components/home/Hero.tsx, src/cms/hero-block.ts\n"
        "- [ ] Build hero component\n"
        "- Lane: homepage-shell\n"
        "- Files: src/pages/index.tsx\n"
        "- depends_on: homepage-hero\n"
        "- [ ] Integrate homepage shell\n"
        "\n## End Goal\n- Ship homepage\n",
        encoding="utf-8",
    )

    result = runtime.plan_connect(project, "2026-03-30-lane-connect", run_preflight=False)

    assert result["connected"] is True
    assert result["progress"] == "0/2"
    assert result["next_steps"] == ["Build hero component", "Integrate homepage shell"]


def test_plan_feedback_ignores_lane_metadata_lines(tmp_path: Path) -> None:
    """lane metadata stays structured and does not become awaiting-feedback prose."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-lane-feedback", "Lane Feedback", "user", "Normalize safely")
    runtime.hub.sessions.update_plan(
        project,
        "2026-03-30-lane-feedback",
        {
            "Steps": [
                "- Phase: Homepage foundation",
                "- Lane: shared-shell",
                "- Files: src/components/home/Shell.tsx",
                "- Lane: homepage-hero",
                "- Files: src/components/home/Hero.tsx",
                "- depends_on: shared-shell",
                "- The agent should confirm copy alignment.",
            ]
        },
    )

    result = runtime.plan_connect(project, "2026-03-30-lane-feedback", run_preflight=False)

    assert result["plan_feedback"]["status"] == "awaiting_feedback"
    assert result["plan_feedback"]["original_prose"] == ["The agent should confirm copy alignment."]
    assert result["plan_feedback"]["changed"] == [
        {
            "from": "- The agent should confirm copy alignment.",
            "to": "- [>] Confirm copy alignment",
        }
    ]



def test_plan_connect_includes_lane_graph_summary_for_lane_aware_plan(tmp_path: Path) -> None:
    """lane-aware plans expose a lane summary without changing checkbox progress."""
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-lane-summary", "Lane Summary", "user", "Track conductor state")
    plan_path = project / ".MEMORY" / "sessions" / "2026-03-30-lane-summary" / "plans" / "PLAN.md"
    plan_path.write_text(
        "# Plan\n"
        "\n## Purpose\n- Track lane-aware work\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: homepage-hero\n"
        "- Files: src/components/home/Hero.tsx\n"
        "- [ ] Build hero component\n"
        "- Lane: homepage-shell\n"
        "- Files: src/pages/index.tsx\n"
        "- depends_on: homepage-hero\n"
        "- [ ] Integrate homepage shell\n"
        "\n## End Goal\n- Ship homepage\n",
        encoding="utf-8",
    )

    result = runtime.plan_connect(project, "2026-03-30-lane-summary", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "session_plan"
    assert result["progress"] == "0/2"
    assert result["next_steps"] == ["Build hero component", "Integrate homepage shell"]
    assert result["lane_summary"]["graph"]["phase_order"] == ["homepage-foundation"]
    assert result["lane_summary"]["runnable"]["runnable_lane_ids"] == ["homepage-hero"]
    assert result["lane_summary"]["runnable"]["waiting_on"] == {"homepage-shell": ["homepage-hero"]}
    assert "phase_order" not in result["lane_summary"]["runnable"]
    assert result["lane_summary"]["graph"] == result["conductor"]["graph"]
    assert result["lane_summary"]["runnable"] == result["conductor"]["runnable"]


def test_handoff_step_update_accepts_completed_status(tmp_path: Path) -> None:
    """handoff step updates accept completed and normalize legacy done safely."""
    runtime, project = _make_runtime(tmp_path)
    session = runtime.hub.sessions.create_session(
        project,
        "2026-03-30-handoff-step",
        "Handoff Step",
        "user",
        "Track handoff completion",
    )

    runtime.hub.sessions.upsert_handoff_step(
        project,
        session.session_id,
        text="Close the handoff loop",
        status="completed",
    )
    runtime.hub.sessions.upsert_handoff_step(
        project,
        session.session_id,
        step_id="s1",
        status="done",
    )

    steps = runtime.hub.sessions.read_handoff_steps(project, session.session_id)
    handoff_text = runtime.hub.sessions.handoff_file(project, session.session_id).read_text(encoding="utf-8")

    assert len(steps) == 1
    assert steps[0]["status"] == "completed"
    assert steps[0]["text"] == "Close the handoff loop"
    assert "- [x] s1 @" in handoff_text
