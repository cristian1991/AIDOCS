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
    session_id = "2026-04-02-execution-mode"
    runtime.hub.sessions.create_session(
        project, session_id, "Execution Mode", "user", "Select execution mode"
    )
    runtime.hub.sessions.plan_file(project, session_id).write_text(
        plan_text, encoding="utf-8"
    )
    return runtime, project, session_id


def _register_superpowers_provider(
    runtime: RuntimeService, project_root: Path, tmp_path: Path
) -> None:
    provider_root = tmp_path / "superpowers-5-1-0"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        json.dumps({"provider_id": "superpowers_external", "version": "5.1.0"}) + "\n",
        encoding="utf-8",
    )
    skill_root = provider_root / "skills" / "subagent-driven-development"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: subagent-driven-development\n"
        "description: Imported execution workflow skill.\n"
        "tags: external, provider, parallel\n"
        "---\n",
        encoding="utf-8",
    )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )


def test_execution_mode_select_returns_inline_without_lanes(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(tmp_path, "# Plan\n")

    result = runtime.execution_mode_select(project_root, session_id)

    assert result["mode"] == "inline"
    assert result["has_lanes"] is False
    assert result["lane_count"] == 0


def test_execution_mode_select_returns_parallel_for_independent_runnable_lanes(
    tmp_path: Path,
) -> None:
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

    result = runtime.execution_mode_select(project_root, session_id)

    assert result["mode"] == "delegated_parallel"
    assert result["runnable_lane_ids"] == ["lane-a", "lane-b"]
    assert result["overlap_risk"] == "none"


def test_execution_mode_select_returns_serial_when_only_one_lane_is_runnable(
    tmp_path: Path,
) -> None:
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
        "- depends_on: lane-a\n"
        "- [ ] Build lane b\n",
    )

    result = runtime.execution_mode_select(project_root, session_id)

    assert result["mode"] == "delegated_serial"
    assert result["runnable_lane_ids"] == ["lane-a"]
    assert result["dependency_pressure"] in {"low", "medium"}


def test_execution_mode_select_blocks_parallel_when_overlap_exists(
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

    result = runtime.execution_mode_select(project_root, session_id)

    assert result["mode"] == "delegated_serial"
    assert result["overlap_risk"] == "high"
    assert result["runnable_lane_ids"] == []


def test_provider_workflow_skill_does_not_change_execution_mode(tmp_path: Path) -> None:
    plan_text = (
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [ ] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/lane_b.py\n"
        "- [ ] Build lane b\n"
    )
    runtime, project_root, session_id = _make_runtime(tmp_path, plan_text)
    base = runtime.execution_mode_select(project_root, session_id)

    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root,
        session_id,
        ["superpowers_external/subagent-driven-development"],
    )
    after = runtime.execution_mode_select(project_root, session_id)

    assert base["mode"] == "delegated_parallel"
    assert after["mode"] == base["mode"]
    assert after["runnable_lane_ids"] == base["runnable_lane_ids"]


def test_mcp_execution_mode_select_tool_returns_runtime_decision(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-04-02-execution-mode-tool",
            "Execution Mode Tool",
            "user",
            "Read execution mode",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Foundation\n"
            "- Lane: lane-a\n"
            "- Files: src/lane_a.py\n"
            "- [ ] Build lane a\n"
            "- Lane: lane-b\n"
            "- Files: src/lane_b.py\n"
            "- [ ] Build lane b\n",
            encoding="utf-8",
        )
        result = await server.call_tool(
            "aidocs_execution_mode_select",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(payload)

    tool_names, payload = asyncio.run(run())

    assert "aidocs_execution_mode_select" in tool_names
    assert payload["mode"] == "delegated_parallel"
    assert payload["session_id"] == "2026-04-02-execution-mode-tool"
