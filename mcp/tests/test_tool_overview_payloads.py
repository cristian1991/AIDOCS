import asyncio
import json
from pathlib import Path

from aidocs_mcp.mcp_server import create_server
from aidocs_mcp.runtime_service import RuntimeService


def _seed_project(project_root: Path) -> None:
    mem = project_root / ".MEMORY"
    (mem / ".aidocs").mkdir(parents=True, exist_ok=True)
    for name in ["index.aidocs", "global-instructions.aidocs", "coding-standards.aidocs", "memory-system.aidocs"]:
        (mem / ".aidocs" / name).write_text(f"# {name}\n", encoding="utf-8")
    (mem / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")


def test_project_bootstrap_tool_includes_overview_payloads(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> dict[str, object]:
        server = create_server()
        hub = server._aidocs_test_hub
        runtime = RuntimeService(hub)
        _seed_project(project_root)
        (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
        session = hub.sessions.create_session(project_root, "2026-03-30-a", "A", "Agent", "Goal A")
        hub.skills.set_selected_skills(project_root, session.session_id, ["deep-retrieval"])

        result = await server.call_tool(
            "project_bootstrap_or_resume",
            {
                "root": str(project_root),
                "session_id": session.session_id,
                "include_code_bundle": False,
                "include_tests": False,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return json.loads(payload)

    payload = asyncio.run(run())

    assert payload["project_overview"]["selected_session_id"] == "2026-03-30-a"
    assert payload["session_overview"]["session_id"] == "2026-03-30-a"
    assert payload["session_overview"]["status"] == "active"
    assert payload["skills_overview"]["selected_skill_count"] == 1
    assert payload["plan_overview"]["session_id"] == "2026-03-30-a"
    assert payload["plan_overview"]["progress"] == "0/0"


def test_skill_trigger_tool_includes_skills_overview(tmp_path: Path) -> None:
    project_root = tmp_path / "project-skills"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> dict[str, object]:
        server = create_server()
        hub = server._aidocs_test_hub
        runtime = RuntimeService(hub)
        _seed_project(project_root)

        provider_root = tmp_path / "superpowers-external"
        provider_root.mkdir(parents=True, exist_ok=True)
        (provider_root / "provider.json").write_text(
            '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
            encoding="utf-8",
        )
        (provider_root / "skills" / "brainstorming").mkdir(parents=True, exist_ok=True)
        (provider_root / "skills" / "brainstorming" / "SKILL.md").write_text(
            "---\n"
            "name: brainstorming\n"
            "description: Imported brainstorming skill.\n"
            "tags: external, provider\n"
            "---\n",
            encoding="utf-8",
        )
        runtime.hub.skills.register_external_provider(
            project_root,
            provider_name="superpowers_external",
            path=str(provider_root),
        )
        runtime.hub.skills.set_selected_skills(project_root, "session-a", ["superpowers_external/brainstorming"])

        result = await server.call_tool(
            "skill_trigger_state_get",
            {
                "root": str(project_root),
                "session_id": "session-a",
                "intent": "brainstorming",
            },
        )

        text = result[0].text if isinstance(result, list) else result.content[0].text
        return json.loads(text)

    payload = asyncio.run(run())

    assert payload["skills_overview"]["selected_skills"] == ["superpowers_external/brainstorming"]
    assert payload["skills_overview"]["active_skills"] == ["superpowers_external/brainstorming"]
    assert payload["skills_overview"]["override_modes"] == {"superpowers_external/brainstorming": "provider_content_aidocs_runtime"}


def test_skill_overview_preserves_full_skill_ids_in_override_modes(tmp_path: Path) -> None:
    server = create_server()
    runtime = RuntimeService(server._aidocs_test_hub)

    overview = runtime._build_skills_overview(
        session_id="session-a",
        selected_skills={"selected_skills": ["provider_one/brainstorming", "provider_two/brainstorming"]},
        active_skills=["provider_one/brainstorming", "provider_two/brainstorming"],
        imported_skill_state=None,
        skill_trigger_state={
            "triggered": [
                {"skill_id": "provider_one/brainstorming", "override_mode": "provider_content_aidocs_runtime"},
                {"skill_id": "provider_two/brainstorming", "override_mode": "provider_native"},
            ]
        },
    )

    assert overview["override_modes"] == {
        "provider_one/brainstorming": "provider_content_aidocs_runtime",
        "provider_two/brainstorming": "provider_native",
    }


def test_plan_connect_tool_includes_plan_overview_defaults(tmp_path: Path) -> None:
    project_root = tmp_path / "project-plan"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> dict[str, object]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-03-30-plan-overview",
            "Plan Overview",
            "user",
            "Goal A",
        )

        result = await server.call_tool(
            "plan_connect",
            {
                "root": str(project_root),
                "session_id": session.session_id,
                "run_preflight": False,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return json.loads(payload)

    payload = asyncio.run(run())

    assert payload["plan_overview"]["session_id"] == "2026-03-30-plan-overview"
    assert payload["plan_overview"]["progress"] == "0/0"
    assert payload["plan_overview"]["end_goal"] == "Goal A"
    assert payload["plan_overview"]["next_step"] is None


def test_plan_connect_tool_fallback_includes_safe_plan_overview(tmp_path: Path) -> None:
    project_root = tmp_path / "project-plan-fallback"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> dict[str, object]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-03-30-plan-fallback",
            "Plan Fallback",
            "user",
            "Goal B",
        )
        hub.sessions.plan_file(project_root, session.session_id).unlink()
        hub.sessions.upsert_handoff_step(project_root, session.session_id, text="Follow up on the blocker", status="open")

        result = await server.call_tool(
            "plan_connect",
            {
                "root": str(project_root),
                "session_id": session.session_id,
                "run_preflight": False,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return json.loads(payload)

    payload = asyncio.run(run())

    assert payload["plan_source"] == "session_open_work"
    assert payload["plan_overview"]["session_id"] == "2026-03-30-plan-fallback"
    assert payload["plan_overview"]["progress"] == "0/0"
    assert payload["plan_overview"]["next_step"] is None
