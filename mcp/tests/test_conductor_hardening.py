"""Full-suite verification and lane reopening tests for the plan conductor."""

from pathlib import Path

from aidocs_mcp.plan_conductor import PlanConductor
from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n- active\n\n"
        "## Status\n- active\n\n"
        "## Owner\n- agent\n\n"
        "## Goal\n- goal\n\n"
        "## Scope\n- scope\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- 2026-03-30 00:00\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text("# Context\n", encoding="utf-8")


def _make_runtime(tmp_path: Path, plan_text: str) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub=hub)
    project = tmp_path / "project"
    session_id = "2026-03-30-conductor-hardening"
    runtime.hub.sessions.create_session(
        project, session_id, "Conductor Hardening", "user", "Harden conductor"
    )
    runtime.hub.sessions.plan_file(project, session_id).write_text(
        plan_text, encoding="utf-8"
    )
    return runtime, project, session_id


def test_conductor_reopens_lane_on_fullsuite_failure_attribution(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [x] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/lane_b.py\n"
        "- [ ] Build lane b\n",
    )

    status = runtime.plan_conductor_status(project_root, session_id)
    assert "lane-a" not in status["runnable_lane_ids"]
    assert "lane-b" in status["runnable_lane_ids"]
    assert status.get("reopened_lane_ids", []) == []

    result = runtime.plan_conductor_reopen_lane_on_fullsuite_failure(
        project_root,
        session_id,
        lane_id="lane-a",
        failure_evidence={
            "failed_tests": ["tests/test_lane_a.py::test_something"],
            "failed_files": ["src/lane_a.py"],
            "error": "AssertionError: expected 2, got 1",
        },
    )

    assert "lane-a" in result["reopened_lane_ids"]
    assert "lane-a" in result["runnable_lane_ids"]
    assert "lane-b" in result["runnable_lane_ids"]


def test_conductor_tracks_persistent_lane_ownership_across_reopens(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [x] Build lane a\n",
    )

    for i in range(3):
        runtime.plan_conductor_reopen_lane_on_fullsuite_failure(
            project_root,
            session_id,
            lane_id="lane-a",
            failure_evidence={
                "failed_tests": [f"tests/test_lane_a.py::test_{i}"],
                "failed_files": ["src/lane_a.py"],
                "error": f"AssertionError {i}",
            },
        )

    history = runtime.plan_conductor_lane_ownership_history(project_root, session_id)

    assert "lane-a" in history
    assert len(history["lane-a"]) == 3

    for i, event in enumerate(history["lane-a"]):
        assert event["reopen_count"] == i + 1
        assert event["event"] == "reopened"


def test_conductor_fails_attribution_deterministically_using_runtime_evidence(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [x] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/lane_b.py\n"
        "- [x] Build lane b\n",
    )

    test_output = "src/lane_a.py:42: AssertionError"

    result1 = runtime.plan_conductor_verify_full_suite(
        project_root, session_id, "lane-a", test_output=test_output
    )
    result2 = runtime.plan_conductor_verify_full_suite(
        project_root, session_id, "lane-a", test_output=test_output
    )

    assert result1["attributed_lanes"] == result2["attributed_lanes"]
    assert result1["attributed_lanes"] == ["lane-a"]

    test_output_b = "src/lane_b.py:10: ValueError"
    result3 = runtime.plan_conductor_verify_full_suite(
        project_root, session_id, "lane-b", test_output=test_output_b
    )
    assert result3["attributed_lanes"] == ["lane-b"]
