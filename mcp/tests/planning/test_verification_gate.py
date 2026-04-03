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


def _make_runtime(tmp_path: Path) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub=hub)
    project = tmp_path / "project"
    session_id = "2026-04-02-verification-gate"
    runtime.hub.sessions.create_session(
        project, session_id, "Verification Gate", "user", "Verify completion"
    )
    runtime.hub.sessions.context_file(project, session_id).write_text(
        "# Context\n\n## Relevant Commands\n- pytest tests/test_target.py -q\n",
        encoding="utf-8",
    )
    runtime.hub.sessions.plan_file(project, session_id).write_text(
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [x] Build lane a\n",
        encoding="utf-8",
    )
    return runtime, project, session_id


def test_verification_gate_blocks_completion_without_fresh_evidence(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(tmp_path)

    result = runtime.verification_gate(project_root, session_id, lane_id="lane-a")

    assert result["verified"] is False
    assert result["status"] == "blocked_missing_evidence"


def test_verification_gate_accepts_completion_with_required_checks(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(tmp_path)

    result = runtime.verification_gate(
        project_root,
        session_id,
        lane_id="lane-a",
        verification_evidence={
            "commands_run": ["pytest tests/test_target.py -q"],
            "command_results": ["1 passed"],
        },
    )

    assert result["verified"] is True
    assert result["status"] == "verified"


def test_verification_gate_reopens_lane_on_failed_broader_suite(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(tmp_path)

    result = runtime.verification_gate(
        project_root,
        session_id,
        lane_id="lane-a",
        verification_evidence={
            "commands_run": ["pytest tests/test_target.py -q"],
            "command_results": ["1 passed, 1 failed"],
            "full_suite_failed": True,
            "failure_evidence": {
                "failed_files": ["src/lane_a.py"],
                "failed_tests": ["tests/test_lane_a.py::test_regression"],
                "error": "AssertionError",
            },
        },
    )

    assert result["verified"] is False
    assert result["status"] == "reopened_full_suite_failure"
    assert result["attributed_lanes"] == ["lane-a"]


def test_mcp_verification_gate_tool_returns_runtime_result(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-04-02-verification-gate-tool",
            "Verification Gate Tool",
            "user",
            "Read verification gate",
        )
        hub.sessions.context_file(project_root, session.session_id).write_text(
            "# Context\n\n## Relevant Commands\n- pytest tests/test_target.py -q\n",
            encoding="utf-8",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n\n## Steps\n- Phase: Shared work\n- Lane: lane-a\n- Files: src/lane_a.py\n- [x] Build lane a\n",
            encoding="utf-8",
        )
        result = await server.call_tool(
            "aidocs_verification_gate",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
                "lane_id": "lane-a",
                "verification_evidence": {
                    "commands_run": ["pytest tests/test_target.py -q"],
                    "command_results": ["1 passed"],
                },
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(payload)

    tool_names, payload = asyncio.run(run())

    assert "aidocs_verification_gate" in tool_names
    assert payload["verified"] is True
