import asyncio
import json
from pathlib import Path

from aidocs_mcp.mcp_server import create_server
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
        "## Last Updated\n- 2026-04-02 00:00\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text("# Context\n", encoding="utf-8")


def _make_runtime(tmp_path: Path, plan_text: str) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub=hub)
    project = tmp_path / "project"
    session_id = "2026-04-02-execution-loop"
    runtime.hub.sessions.create_session(
        project, session_id, "Execution Loop", "user", "Run delegated loop"
    )
    runtime.hub.sessions.plan_file(project, session_id).write_text(
        plan_text, encoding="utf-8"
    )
    return runtime, project, session_id


def test_execution_loop_next_returns_next_runnable_lane(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [ ] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/lane_b.py\n"
        "- [ ] Build lane b\n",
    )

    result = runtime.execution_loop_next(project_root, session_id)

    assert result["state"] == "delegated"
    assert result["dispatch"]["packet"]["lane_id"] == "lane-a"


def test_dispatch_report_reopens_on_hidden_dependency(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [ ] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/lane_b.py\n"
        "- [ ] Build lane b\n",
    )

    result = runtime.plan_dispatch_report(
        project_root,
        session_id,
        {
            "lane_id": "lane-b",
            "hidden_dependencies": [
                {
                    "target_lane_id": "lane-a",
                    "detail": "lane-b discovered it needs lane-a output first",
                }
            ],
        },
    )

    assert result["result"] == "signaled_hidden_dependency"
    assert "lane-b" in result["status"]["blocked_reasons"]


def test_dispatch_report_blocks_on_overlap_signal(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [ ] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/lane_b.py\n"
        "- [ ] Build lane b\n",
    )

    result = runtime.plan_dispatch_report(
        project_root,
        session_id,
        {
            "lane_id": "lane-b",
            "overlap_found": {
                "conflicting_lane_id": "lane-a",
                "file_path": "src/shared/schema.json",
            },
        },
    )

    assert result["result"] == "paused_overlap"
    assert result["status"]["paused_lane_ids"] == ["lane-a", "lane-b"]


def test_dispatch_report_reopens_on_full_suite_failure(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [x] Build lane a\n",
    )

    result = runtime.plan_dispatch_report(
        project_root,
        session_id,
        {
            "lane_id": "lane-a",
            "verification_results": {
                "full_suite_failed": True,
                "failure_evidence": {
                    "failed_files": ["src/lane_a.py"],
                    "failed_tests": ["tests/test_lane_a.py::test_regression"],
                    "error": "AssertionError",
                },
            },
        },
    )

    assert result["result"] == "reopened_full_suite_failure"
    assert result["attributed_lanes"] == ["lane-a"]
    assert "lane-a" in result["status"]["reopened_lane_ids"]


def test_dispatch_report_blocks_claimed_done_without_fresh_evidence(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [ ] Build lane a\n",
    )

    result = runtime.plan_dispatch_report(
        project_root,
        session_id,
        {
            "lane_id": "lane-a",
            "claimed_done": True,
        },
    )

    assert result["result"] == "blocked_missing_evidence"
    assert result["verification"]["verified"] is False


def test_execution_loop_stops_only_when_blocked_or_complete(tmp_path: Path) -> None:
    blocked_runtime, blocked_project, blocked_session = _make_runtime(
        tmp_path / "blocked",
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/shared.py\n"
        "- [ ] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/shared.py\n"
        "- [ ] Build lane b\n",
    )
    blocked = blocked_runtime.execution_loop_next(blocked_project, blocked_session)

    complete_runtime, complete_project, complete_session = _make_runtime(
        tmp_path / "complete",
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [x] Build lane a\n",
    )
    complete = complete_runtime.execution_loop_next(complete_project, complete_session)

    assert blocked["state"] == "blocked"
    assert complete["state"] == "complete"


def test_mcp_execution_loop_tools_round_trip(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-04-02-execution-loop-tool",
            "Execution Loop Tool",
            "user",
            "Drive execution loop tools",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Foundation\n"
            "- Lane: lane-a\n"
            "- Files: src/lane_a.py\n"
            "- [ ] Build lane a\n",
            encoding="utf-8",
        )
        next_result = await server.call_tool(
            "aidocs_execution_loop_next",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        next_payload = json.loads(
            next_result[0].text
            if isinstance(next_result, list)
            else next_result.content[0].text
        )
        report_result = await server.call_tool(
            "aidocs_plan_dispatch_report",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
                "packet_result": {
                    "lane_id": "lane-a",
                    "claimed_done": True,
                },
            },
        )
        report_payload = json.loads(
            report_result[0].text
            if isinstance(report_result, list)
            else report_result.content[0].text
        )
        return tool_names, next_payload, report_payload

    tool_names, next_payload, report_payload = asyncio.run(run())

    assert "aidocs_execution_loop_next" in tool_names
    assert "aidocs_plan_dispatch_report" in tool_names
    assert next_payload["state"] == "delegated"
    assert report_payload["result"] == "blocked_missing_evidence"
