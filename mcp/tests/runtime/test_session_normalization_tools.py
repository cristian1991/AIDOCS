import asyncio
import json
from pathlib import Path

from aidocs_mcp.mcp_server import create_server


def _payload_json(result: object) -> dict[str, object]:
    payload = result[0].text if isinstance(result, list) else result.content[0].text
    return json.loads(payload)


def test_normalize_handoff_steps_converts_legacy_open_done_mix_deterministically(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> dict[str, object]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project,
            "2026-03-30-normalize-handoff",
            "Normalize Handoff",
            "user",
            "Normalize legacy handoff steps",
        )
        handoff_path = hub.sessions.handoff_file(project, session.session_id)
        handoff_path.write_text(
            handoff_path.read_text(encoding="utf-8").replace(
                "## Steps\n-\n",
                "## Steps\n"
                "- [done] s1: Legacy completed step\n"
                "- [open] s2: Legacy open step\n",
            ),
            encoding="utf-8",
        )

        return _payload_json(
            await server.call_tool(
                "session_handoff_steps_normalize",
                {
                    "root": str(project),
                    "session_id": session.session_id,
                },
            )
        )

    result = asyncio.run(run())

    assert result["status"] == "normalized"
    assert result["changed"] == [
        {
            "from": "- [done] s1: Legacy completed step",
            "to": "- [x] s1: Legacy completed step",
        },
        {
            "from": "- [open] s2: Legacy open step",
            "to": "- [ ] s2: Legacy open step",
        },
    ]
    assert result["untouched"] == []
    handoff_text = (
        project
        / ".MEMORY"
        / "sessions"
        / "2026-03-30-normalize-handoff"
        / "2026-03-30-normalize-handoff.handoff.md"
    ).read_text(encoding="utf-8")
    assert "- [x] s1: Legacy completed step" in handoff_text
    assert "- [ ] s2: Legacy open step" in handoff_text


def test_normalize_plan_feedback_sections_preserves_user_prose(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[dict[str, object], str]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project,
            "2026-03-30-normalize-plan",
            "Normalize Plan",
            "user",
            "Normalize plan feedback prose",
        )
        hub.sessions.update_plan(
            project,
            session.session_id,
            {
                "Steps": [
                    "- The agent should validate the roadmap state and then continue with CLI fixes.",
                    "- [ ] Keep the existing structured step",
                ]
            },
        )
        result = _payload_json(
            await server.call_tool(
                "plan_normalize_prose",
                {
                    "root": str(project),
                    "session_id": session.session_id,
                },
            )
        )
        plan_text = hub.sessions.plan_file(project, session.session_id).read_text(encoding="utf-8")
        return result, plan_text

    result, plan_text = asyncio.run(run())

    assert result["status"] == "awaiting_feedback"
    assert result["changed"] == [
        {
            "from": "- The agent should validate the roadmap state and then continue with CLI fixes.",
            "to": "- [>] Validate the roadmap state and then continue with CLI fixes",
        }
    ]
    assert result["untouched"] == ["- [ ] Keep the existing structured step"]
    assert "- The agent should validate the roadmap state and then continue with CLI fixes." in plan_text
    assert "- [>] Validate the roadmap state and then continue with CLI fixes" in plan_text
