import asyncio
import json
from pathlib import Path

from aidocs_mcp.mcp_server import create_server
from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


def _make_runtime(tmp_path: Path) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text(
        "# Session\n\n## Title\n- t\n\n## Status\n- active\n\n## Owner\n- agent\n\n"
        "## Goal\n- Ship feature\n\n## Scope\n- app\n\n## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## Active Claims\n-\n\n## State\n-\n\n## Upcoming\n-\n\n## Blockers\n-\n\n"
        "## Last Updated\n- 2026-04-02 00:00\n",
        encoding="utf-8",
    )
    (templates / "context.md").write_text("# Context\n", encoding="utf-8")
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub=hub)
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session = runtime.hub.sessions.create_session(
        project, "2026-04-02-plan-create", "Plan", "user", "Ship feature"
    )
    return runtime, project, session.session_id


def test_plan_create_from_spec_writes_normalized_session_plan(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime(tmp_path)

    result = runtime.plan_create_from_spec(
        project,
        session_id,
        spec_text="""
Purpose: Ship the feature safely
Goal: Ship feature
- Add API endpoint
- Add UI integration
Validation:
- pytest tests/test_feature.py -q
- npm run lint
""",
        scope="feature slice",
    )

    assert result["sections"]["Purpose"][0] == "- Ship the feature safely"
    assert result["sections"]["Scope"][0] == "- feature slice"
    step_lines = [line for line in result["sections"]["Steps"] if line.strip()]
    assert step_lines == ["- [ ] Add API endpoint", "- [ ] Add UI integration"]
    assert result["has_lanes"] is False


def test_plan_validate_rejects_vague_steps(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime(tmp_path)
    runtime.hub.sessions.update_plan(
        project,
        session_id,
        {
            "Purpose": ["- Ship feature"],
            "Steps": ["- [ ] Do the thing"],
            "Validation": ["- pytest tests/test_feature.py -q"],
            "End Goal": ["- Ship feature"],
        },
    )

    result = runtime.plan_validate(project, session_id)

    assert result["valid"] is False
    assert any("Vague step" in item for item in result["errors"])


def test_plan_validate_requires_verification_commands(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime(tmp_path)
    runtime.hub.sessions.update_plan(
        project,
        session_id,
        {
            "Purpose": ["- Ship feature"],
            "Steps": ["- [ ] Add API endpoint"],
            "Validation": [
                "- Define concrete verification steps before considering the work complete."
            ],
            "End Goal": ["- Ship feature"],
        },
    )

    result = runtime.plan_validate(project, session_id)

    assert result["valid"] is False
    assert any("verification commands" in item for item in result["errors"])


def test_plan_create_from_spec_generates_lane_hints_when_parallelizable(
    tmp_path: Path,
) -> None:
    runtime, project, session_id = _make_runtime(tmp_path)

    result = runtime.plan_create_from_spec(
        project,
        session_id,
        spec_text="""
Purpose: Ship feature
Task: Build API contract | Files: src/api/contracts.py
Task: Build frontend client | Files: src/frontend/client.ts | depends_on: build-api-contract
Validation:
- pytest tests/test_feature.py -q
""",
    )

    assert result["has_lanes"] is True
    assert result["lane_count"] == 2
    assert result["sections"]["Steps"][0] == "- Phase: Planned Work"


def test_mcp_plan_create_and_validate_tools_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project,
            "2026-04-02-plan-tools",
            "Plan Tools",
            "user",
            "Ship feature",
        )
        created = await server.call_tool(
            "aidocs_plan_create_from_spec",
            {
                "project_root": str(project),
                "session_id": session.session_id,
                "spec_text": "Purpose: Ship feature\n- Add API endpoint\nValidation:\n- pytest tests/test_feature.py -q",
            },
        )
        validated = await server.call_tool(
            "aidocs_plan_validate",
            {
                "project_root": str(project),
                "session_id": session.session_id,
            },
        )
        created_payload = json.loads(
            created[0].text if isinstance(created, list) else created.content[0].text
        )
        validated_payload = json.loads(
            validated[0].text
            if isinstance(validated, list)
            else validated.content[0].text
        )
        return tool_names, created_payload, validated_payload

    tool_names, created_payload, validated_payload = asyncio.run(run())

    assert "aidocs_plan_create_from_spec" in tool_names
    assert "aidocs_plan_validate" in tool_names
    step_lines = [line for line in created_payload["sections"]["Steps"] if line.strip()]
    assert step_lines == ["- [ ] Add API endpoint"]
    assert validated_payload["valid"] is True
