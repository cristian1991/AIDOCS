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
    session_id = "2026-04-02-subagent-packets"
    runtime.hub.sessions.create_session(
        project, session_id, "Subagent Packets", "user", "Build task packets"
    )
    runtime.hub.sessions.plan_file(project, session_id).write_text(
        plan_text, encoding="utf-8"
    )
    return runtime, project, session_id


def test_subagent_packet_is_scope_limited(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py, src/shared/util.py\n"
        "- [ ] Build lane a\n",
    )
    runtime.hub.sessions.context_file(project_root, session_id).write_text(
        "# Context\n\n## Constraints\n- Preserve existing API behavior\n",
        encoding="utf-8",
    )

    result = runtime.plan_dispatch_next(project_root, session_id)

    packet = result["packet"]
    assert result["dispatch_state"] == "delegated"
    assert packet["allowed_files"] == ["src/lane_a.py", "src/shared/util.py"]
    assert packet["required_reads"] == packet["allowed_files"]
    assert (
        "Stay within allowed_files unless the conductor explicitly expands scope."
        in packet["constraints"]
    )


def test_subagent_packet_includes_verification_commands(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [ ] Build lane a\n",
    )
    runtime.hub.sessions.context_file(project_root, session_id).write_text(
        "# Context\n\n"
        "## Relevant Commands\n"
        "- `pytest tests/test_lane_a.py -q`\n"
        "- `npm run lint`\n",
        encoding="utf-8",
    )

    result = runtime.plan_dispatch_next(project_root, session_id)

    packet = result["packet"]
    assert packet["verification_commands"] == [
        "`pytest tests/test_lane_a.py -q`",
        "`npm run lint`",
    ]


def test_subagent_packet_forbids_replanning_and_scope_expansion(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [ ] Build lane a\n",
    )

    result = runtime.plan_dispatch_next(project_root, session_id)

    packet = result["packet"]
    assert "Do not change files outside allowed_files." in packet["must_not"]
    assert "Do not broaden scope or re-plan the workflow." in packet["must_not"]
    assert "claimed_done" in packet["output_schema"]["required"]


def test_plan_dispatch_next_returns_blocked_when_no_runnable_lane_exists(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
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

    result = runtime.plan_dispatch_next(project_root, session_id)

    assert result["dispatch_state"] == "blocked"
    assert result["packet"] is None
    assert result["mode"] == "delegated_serial"


def test_mcp_plan_dispatch_next_tool_returns_packet(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-04-02-dispatch-tool",
            "Dispatch Tool",
            "user",
            "Return next lane packet",
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
        result = await server.call_tool(
            "aidocs_plan_dispatch_next",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(payload)

    tool_names, payload = asyncio.run(run())

    assert "aidocs_plan_dispatch_next" in tool_names
    assert payload["dispatch_state"] == "delegated"
    assert payload["packet"]["lane_id"] == "lane-a"
