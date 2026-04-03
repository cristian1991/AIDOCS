import asyncio
import json
from pathlib import Path

import pytest

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
    session_id = "2026-03-30-conductor-interaction"
    runtime.hub.sessions.create_session(
        project,
        session_id,
        "Conductor Interaction",
        "user",
        "Drive conductor interactions",
    )
    runtime.hub.sessions.plan_file(project, session_id).write_text(
        plan_text, encoding="utf-8"
    )
    return runtime, project, session_id


async def _call_tool_json(
    server, tool_name: str, arguments: dict[str, object]
) -> dict[str, object]:
    result = await server.call_tool(tool_name, arguments)
    payload = result[0].text if isinstance(result, list) else result.content[0].text
    return json.loads(payload)


def test_conductor_pauses_lane_when_inflight_file_overlap_is_reported(
    tmp_path: Path,
) -> None:
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

    initial = runtime.plan_conductor_status(project_root, session_id)
    paused = runtime.plan_conductor_report_inflight_overlap(
        project_root,
        session_id,
        paused_lane_id="lane-b",
        conflicting_lane_id="lane-a",
        file_path="src/shared/schema.json",
    )

    assert initial["runnable_lane_ids"] == ["lane-a", "lane-b"]
    assert paused["runnable_lane_ids"] == []
    assert paused["blocked_reasons"]["lane-a"] == [
        "paused:inflight-file-overlap:src/shared/schema.json:lane-b"
    ]
    assert paused["blocked_reasons"]["lane-b"] == [
        "paused:inflight-file-overlap:src/shared/schema.json:lane-a"
    ]
    assert paused["paused_lane_ids"] == ["lane-a", "lane-b"]


def test_conductor_pauses_all_affected_lanes_when_inflight_file_overlap_is_reported(
    tmp_path: Path,
) -> None:
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
        "- [ ] Build lane b\n"
        "- Lane: lane-c\n"
        "- Files: src/lane_c.py\n"
        "- [ ] Build lane c\n",
    )

    paused = runtime.plan_conductor_report_inflight_overlap(
        project_root,
        session_id,
        paused_lane_id="lane-b",
        conflicting_lane_id="lane-a",
        file_path="src/shared/schema.json",
    )

    assert paused["runnable_lane_ids"] == ["lane-c"]
    assert paused["blocked_reasons"]["lane-a"] == [
        "paused:inflight-file-overlap:src/shared/schema.json:lane-b"
    ]
    assert paused["blocked_reasons"]["lane-b"] == [
        "paused:inflight-file-overlap:src/shared/schema.json:lane-a"
    ]
    assert paused["paused_lane_ids"] == ["lane-a", "lane-b"]


def test_user_override_can_resume_paused_lane(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-03-30-conductor-resume",
            "Conductor Resume",
            "user",
            "Resume paused lane",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Shared work\n"
            "- Lane: lane-a\n"
            "- Files: src/lane_a.py\n"
            "- [ ] Build lane a\n"
            "- Lane: lane-b\n"
            "- Files: src/lane_b.py\n"
            "- [ ] Build lane b\n",
            encoding="utf-8",
        )
        paused = await _call_tool_json(
            server,
            "aidocs_plan_conductor_report_inflight_overlap",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
                "paused_lane_id": "lane-b",
                "conflicting_lane_id": "lane-a",
                "file_path": "src/shared/schema.json",
            },
        )
        resumed = await _call_tool_json(
            server,
            "aidocs_plan_conductor_resume_lane",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
                "lane_id": "lane-b",
            },
        )
        return tool_names, paused, resumed

    tool_names, paused, resumed = asyncio.run(run())

    assert "aidocs_plan_conductor_report_inflight_overlap" in tool_names
    assert "aidocs_plan_conductor_resume_lane" in tool_names
    assert paused["blocked_reasons"]["lane-b"] == [
        "paused:inflight-file-overlap:src/shared/schema.json:lane-a"
    ]
    assert resumed["runnable_lane_ids"] == ["lane-b"]
    assert resumed["paused_lane_ids"] == ["lane-a"]


def test_contract_compatible_lanes_can_run_together_when_conductor_marks_contract_ready(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-03-30-conductor-contract-ready",
            "Conductor Contract Ready",
            "user",
            "Allow contract-compatible lanes to proceed",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: API delivery\n"
            "- Lane: api-contract\n"
            "- Files: src/api/contracts.py\n"
            "- [ ] Finalize API contract\n"
            "- Lane: frontend-contract-client\n"
            "- Files: src/frontend/client_contract.ts\n"
            "- depends_on: api-contract\n"
            "- [ ] Implement frontend contract client\n"
            "- Lane: backend-handler\n"
            "- Files: src/backend/handler.py\n"
            "- depends_on: api-contract\n"
            "- [ ] Implement backend handler\n",
            encoding="utf-8",
        )
        initial = await _call_tool_json(
            server,
            "aidocs_plan_conductor_status",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        contract_ready = await _call_tool_json(
            server,
            "aidocs_plan_conductor_mark_contract_ready",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
                "lane_id": "api-contract",
            },
        )
        return tool_names, initial, contract_ready

    tool_names, initial, contract_ready = asyncio.run(run())

    assert "aidocs_plan_conductor_mark_contract_ready" in tool_names
    assert initial["runnable_lane_ids"] == ["api-contract"]
    assert contract_ready["runnable_lane_ids"] == [
        "api-contract",
        "frontend-contract-client",
    ]
    assert contract_ready["waiting_on"] == {"backend-handler": ["api-contract"]}
    assert contract_ready["contract_ready_lane_ids"] == ["api-contract"]


def test_contract_ready_does_not_bypass_non_contract_hard_dependencies(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: foundation-db\n"
        "- Files: src/foundation/db.py\n"
        "- [ ] Build database foundation\n"
        "- Lane: integration\n"
        "- Files: src/integration.py\n"
        "- depends_on: foundation-db\n"
        "- [ ] Build integration\n",
    )

    result = runtime.plan_conductor_mark_contract_ready(
        project_root, session_id, lane_id="foundation-db"
    )

    assert result["runnable_lane_ids"] == ["foundation-db"]
    assert result["waiting_on"] == {"integration": ["foundation-db"]}
    assert result["contract_ready_lane_ids"] == []


def test_contract_ready_ignores_false_positive_contract_names(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Delivery\n"
        "- Lane: api-contractor\n"
        "- Files: src/contracts_notes.md\n"
        "- [ ] Document contractor notes\n"
        "- Lane: frontend-contract-client\n"
        "- Files: src/frontend/client_contract.ts\n"
        "- depends_on: api-contractor\n"
        "- [ ] Implement frontend contract client\n",
    )

    result = runtime.plan_conductor_mark_contract_ready(
        project_root, session_id, lane_id="api-contractor"
    )

    assert result["runnable_lane_ids"] == ["api-contractor"]
    assert result["waiting_on"] == {"frontend-contract-client": ["api-contractor"]}
    assert result["contract_ready_lane_ids"] == []


def test_lane_agent_request_for_undeclared_file_requires_conductor_signal(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project_root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "lane_a.py").write_text("lane-a\n", encoding="utf-8")
    (src / "other.py").write_text("other\n", encoding="utf-8")

    async def run() -> tuple[dict[str, object], dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-03-31-conductor-read-gate",
            "Conductor Read Gate",
            "user",
            "Keep undeclared lane reads blocked",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Shared work\n"
            "- Lane: lane-a\n"
            "- Files: src/lane_a.py\n"
            "- [ ] Build lane a\n",
            encoding="utf-8",
        )
        hub.managed_mode.set_mode(project_root, session_id=session.session_id)

        graph = await _call_tool_json(
            server,
            "aidocs_plan_conductor_graph",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        blocked = await _call_tool_json(
            server,
            "aidocs_code_get_lines",
            {
                "project_root": str(project_root),
                "path": "src/other.py",
                "start_line": 1,
                "count": 1,
                "show_line_numbers": False,
                "known_exact_path": True,
            },
        )
        return graph, blocked

    graph, blocked = asyncio.run(run())

    assert graph["file_owners"] == {"src/lane_a.py": ["lane-a"]}
    assert "src/other.py" not in graph["file_owners"]
    assert "Indexed-query prerequisite" in str(blocked["error"])


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        (
            "plan_conductor_report_inflight_overlap",
            {
                "paused_lane_id": "missing-lane",
                "conflicting_lane_id": "lane-a",
                "file_path": "src/shared/schema.json",
            },
        ),
        (
            "plan_conductor_report_inflight_overlap",
            {
                "paused_lane_id": "lane-a",
                "conflicting_lane_id": "missing-lane",
                "file_path": "src/shared/schema.json",
            },
        ),
        ("plan_conductor_resume_lane", {"lane_id": "missing-lane"}),
        ("plan_conductor_mark_contract_ready", {"lane_id": "missing-lane"}),
    ],
)
def test_conductor_mutation_apis_reject_unknown_lane_ids(
    tmp_path: Path,
    method_name: str,
    kwargs: dict[str, str],
) -> None:
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

    with pytest.raises(ValueError, match="Unknown lane id"):
        getattr(runtime, method_name)(project_root, session_id, **kwargs)


def test_malformed_conductor_state_degrades_to_safe_empty_state(tmp_path: Path) -> None:
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
    runtime._plan_conductor_state_path(project_root, session_id).write_text(
        '{"paused_lanes": ', encoding="utf-8"
    )

    status = runtime.plan_conductor_status(project_root, session_id)
    resumed = runtime.plan_conductor_resume_lane(
        project_root, session_id, lane_id="lane-a"
    )

    assert status["runnable_lane_ids"] == ["lane-a", "lane-b"]
    assert status["paused_lane_ids"] == []
    assert status["contract_ready_lane_ids"] == []
    assert resumed["runnable_lane_ids"] == ["lane-a", "lane-b"]


def test_structurally_malformed_conductor_state_degrades_to_safe_empty_state(
    tmp_path: Path,
) -> None:
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
    runtime._plan_conductor_state_path(project_root, session_id).write_text(
        json.dumps(
            {"paused_lanes": ["lane-a"], "contract_ready_lane_ids": {"lane-b": True}}
        ),
        encoding="utf-8",
    )

    status = runtime.plan_conductor_status(project_root, session_id)

    assert status["runnable_lane_ids"] == ["lane-a", "lane-b"]
    assert status["paused_lane_ids"] == []


def test_lane_can_signal_hidden_dependency_found(tmp_path: Path) -> None:
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

    initial = runtime.plan_conductor_status(project_root, session_id)
    assert initial["runnable_lane_ids"] == ["lane-a", "lane-b"]
    assert initial.get("lane_signals") == {}

    recorded = runtime.plan_conductor_record_lane_signal(
        project_root,
        session_id,
        lane_id="lane-b",
        signal_kind="hidden_dependency_found",
        target_lane_id="lane-a",
        detail="lane-b discovered it needs lane-a's output",
    )

    assert "lane-b" in recorded["blocked_reasons"]
    assert any(
        "hidden_dependency_found" in r for r in recorded["blocked_reasons"]["lane-b"]
    )
    assert recorded["lane_signals"] == {
        "lane-b": [
            {
                "kind": "hidden_dependency_found",
                "target_lane_id": "lane-a",
                "detail": "lane-b discovered it needs lane-a's output",
            }
        ]
    }

    status = runtime.plan_conductor_status(project_root, session_id)
    assert "lane-b" not in status["runnable_lane_ids"]
    assert "lane-a" in status["runnable_lane_ids"]


def test_lane_can_signal_undeclared_file_needed(tmp_path: Path) -> None:
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

    initial = runtime.plan_conductor_status(project_root, session_id)
    assert initial["runnable_lane_ids"] == ["lane-a", "lane-b"]

    recorded = runtime.plan_conductor_record_lane_signal(
        project_root,
        session_id,
        lane_id="lane-b",
        signal_kind="undeclared_file_needed",
        target_lane_id="lane-a",
        detail="lane-b needs src/shared_config.py owned by lane-a",
    )

    assert "lane-b" in recorded["blocked_reasons"]
    assert any(
        "undeclared_file_needed" in r for r in recorded["blocked_reasons"]["lane-b"]
    )

    status = runtime.plan_conductor_status(project_root, session_id)
    assert "lane-b" not in status["runnable_lane_ids"]


def test_conductor_enforces_structured_lane_signals(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> dict[str, object]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-03-31-conductor-signals",
            "Conductor Signals",
            "user",
            "Enforce structured lane signals",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Shared work\n"
            "- Lane: lane-a\n"
            "- Files: src/lane_a.py\n"
            "- [ ] Build lane a\n"
            "- Lane: lane-b\n"
            "- Files: src/lane_b.py\n"
            "- [ ] Build lane b\n"
            "- Lane: lane-c\n"
            "- Files: src/lane_c.py\n"
            "- [ ] Build lane c\n",
            encoding="utf-8",
        )
        hub.managed_mode.set_mode(project_root, session_id=session.session_id)

        status = await _call_tool_json(
            server,
            "aidocs_plan_conductor_status",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        assert status["runnable_lane_ids"] == ["lane-a", "lane-b", "lane-c"]

        await _call_tool_json(
            server,
            "aidocs_plan_conductor_record_lane_signal",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
                "lane_id": "lane-b",
                "signal_kind": "integration_failure_reopened",
                "target_lane_id": "lane-a",
                "detail": "integration tests fail when lane-a changes are merged",
            },
        )

        blocked = await _call_tool_json(
            server,
            "aidocs_plan_conductor_status",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        return blocked

    blocked = asyncio.run(run())

    assert "lane-b" in blocked["blocked_reasons"]
    assert any(
        "integration_failure_reopened" in r
        for r in blocked["blocked_reasons"]["lane-b"]
    )
    assert "lane-b" not in blocked["runnable_lane_ids"]
    assert "lane-a" in blocked["runnable_lane_ids"]
    assert "lane-c" in blocked["runnable_lane_ids"]
    assert (
        blocked["lane_signals"]["lane-b"][0]["kind"] == "integration_failure_reopened"
    )
