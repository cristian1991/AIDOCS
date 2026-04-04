from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aidocs_mcp.mcp_server import create_server


def _payload_json(result: object) -> dict[str, object]:
    payload = result[0].text if isinstance(result, list) else result.content[0].text
    return json.loads(payload)


def test_edit_or_create_flow_can_grant_narrow_followup_read_without_unlocking_broad_reads(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "existing.txt").write_text("before\nafter\n", encoding="utf-8")
    (src / "other.txt").write_text("other\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")

        created = _payload_json(
            await server.call_tool(
                "aidocs_code_create_file",
                {
                    "project_root": str(project),
                    "path": "src/new.txt",
                    "content": "alpha\nbeta\n",
                },
            )
        )
        created_followup = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/new.txt",
                    "start_line": 2,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        edit = _payload_json(
            await server.call_tool(
                "aidocs_code_edit_lines",
                {
                    "project_root": str(project),
                    "path": "src/existing.txt",
                    "start_line": 1,
                    "end_line": 1,
                    "new_content": "updated",
                },
            )
        )
        edited_followup = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/existing.txt",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        blocked_other_no_flag = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/other.txt",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                },
            )
        )
        blocked_same_without_flag = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/existing.txt",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                },
            )
        )
        return {
            "created": created,
            "created_followup": created_followup,
            "edit": edit,
            "edited_followup": edited_followup,
            "blocked_other_no_flag": blocked_other_no_flag,
            "blocked_same_without_flag": blocked_same_without_flag,
        }

    data = asyncio.run(run())

    assert data["created"]["success"] is True
    assert data["created_followup"]["content"] == "beta"
    assert data["edit"]["success"] is True
    assert data["edited_followup"]["content"] == "updated"
    # Undiscovered files blocked when known_exact_path is not set
    assert "Indexed-query prerequisite" in str(data["blocked_other_no_flag"]["error"])
    # Discovered file (via edit grant) is readable even without known_exact_path flag
    assert "error" not in data["blocked_same_without_flag"]


def test_batch_edit_flow_can_grant_narrow_followup_reads_for_multiple_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "first.txt").write_text("one\ntwo\n", encoding="utf-8")
    (src / "second.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (src / "other.txt").write_text("other\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")

        batch = _payload_json(
            await server.call_tool(
                "aidocs_code_batch_edit",
                {
                    "project_root": str(project),
                    "edits": [
                        {
                            "path": "src/first.txt",
                            "start_line": 2,
                            "end_line": 2,
                            "new_content": "updated-one",
                        },
                        {
                            "path": "src/second.txt",
                            "start_line": 2,
                            "end_line": 2,
                            "new_content": "updated-two",
                        },
                    ],
                },
            )
        )
        first_followup = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/first.txt",
                    "start_line": 2,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        second_followup = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/second.txt",
                    "start_line": 2,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        blocked_other_exact = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/other.txt",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        return {
            "batch": batch,
            "first_followup": first_followup,
            "second_followup": second_followup,
            "blocked_other_exact": blocked_other_exact,
        }

    data = asyncio.run(run())

    assert data["batch"]["success"] is True
    assert data["first_followup"]["content"] == "updated-one"
    assert data["second_followup"]["content"] == "updated-two"
    assert "Indexed-query prerequisite" in str(data["blocked_other_exact"]["error"])


def test_lane_owned_file_read_is_granted_only_for_the_current_lane_context(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "lane_a.py").write_text("lane-a\n", encoding="utf-8")
    (src / "lane_b.py").write_text("lane-b\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project, "s1", "Lane Owned Read", "user", "Read lane-owned files"
        )
        hub.sessions.plan_file(project, session.session_id).write_text(
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
        hub.managed_mode.set_mode(project, session_id=session.session_id)
        hub.query_gate.set(
            project,
            session.session_id,
            allow_read=False,
            last_tool="plan_conductor_lane_context",
            current_lane_id="lane-a",
        )

        lane_owned = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_a.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        blocked_other_lane = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_b.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        gate = hub.query_gate.get(project, session.session_id)
        return {
            "lane_owned": lane_owned,
            "blocked_other_lane": blocked_other_lane,
            "gate": gate,
        }

    data = asyncio.run(run())

    assert "Indexed-query prerequisite" in str(data["lane_owned"]["error"])
    assert "Indexed-query prerequisite" in str(data["blocked_other_lane"]["error"])
    assert data["gate"]["allow_read"] is False
    assert data["gate"]["known_exact_paths"] == []


def test_lane_owned_protected_prefix_file_requires_matching_lane_context(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    server_dir = project / "mcp" / "server" / "aidocs_mcp"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "lane_owned.py").write_text("protected-owned\n", encoding="utf-8")
    (server_dir / "undeclared.py").write_text(
        "protected-undeclared\n", encoding="utf-8"
    )

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project, "s1", "Protected Lane Read", "user", "Read protected lane files"
        )
        hub.sessions.plan_file(project, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Shared work\n"
            "- Lane: lane-a\n"
            "- Files: mcp/server/aidocs_mcp/lane_owned.py\n"
            "- [ ] Build lane a\n",
            encoding="utf-8",
        )
        hub.managed_mode.set_mode(project, session_id=session.session_id)
        hub.query_gate.set(
            project,
            session.session_id,
            allow_read=False,
            last_tool="plan_conductor_lane_context",
            current_lane_id="lane-a",
        )

        lane_owned = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "mcp/server/aidocs_mcp/lane_owned.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        blocked_undeclared = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "mcp/server/aidocs_mcp/undeclared.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        return {
            "lane_owned": lane_owned,
            "blocked_undeclared": blocked_undeclared,
        }

    data = asyncio.run(run())

    assert "Indexed-query prerequisite" in str(data["lane_owned"]["error"])
    assert "Indexed-query prerequisite" in str(data["blocked_undeclared"]["error"])


def test_failed_service_api_lookup_does_not_unlock_broad_reads(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    services = project / "Services"
    services.mkdir(parents=True, exist_ok=True)
    (services / "FormPdfService.cs").write_text(
        "public class FormPdfService { public void Render() {} }\n", encoding="utf-8"
    )

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")

        await server.call_tool(
            "aidocs_code_index_sync",
            {
                "project_root": str(project),
                "include_tests": False,
            },
        )
        missing = _payload_json(
            await server.call_tool(
                "aidocs_code_get_service_api",
                {
                    "project_root": str(project),
                    "service_name": "AccountService",
                },
            )
        )
        blocked_read = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "Services/FormPdfService.cs",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                },
            )
        )
        return {
            "missing": missing,
            "blocked_read": blocked_read,
        }

    data = asyncio.run(run())

    assert data["missing"]["not_found"] is True
    assert data["missing"]["methods"] == []
    assert "Indexed-query prerequisite" in str(data["blocked_read"]["error"])


def test_task_begin_establishes_real_lane_scoped_reads(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "lane_a.py").write_text("lane-a\n", encoding="utf-8")
    (src / "lane_b.py").write_text("lane-b\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project, "s1", "Lane Owned Read", "user", "Read lane-owned files"
        )
        hub.sessions.plan_file(project, session.session_id).write_text(
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
        hub.managed_mode.set_mode(project, session_id=session.session_id)

        begun = _payload_json(
            await server.call_tool(
                "aidocs_task_begin",
                {
                    "project_root": str(project),
                    "session_id": session.session_id,
                    "goal": "Implement lane a",
                    "relevant_files": ["src/lane_a.py"],
                    "include_code_bundle": False,
                },
            )
        )
        lane_owned = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_a.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        blocked_other_lane = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_b.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        gate = hub.query_gate.get(project, session.session_id)
        return {
            "begun": begun,
            "lane_owned": lane_owned,
            "blocked_other_lane": blocked_other_lane,
            "gate": gate,
        }

    data = asyncio.run(run())

    assert any(
        "Implement lane a" in item
        for item in data["begun"]["session"]["sections"]["State"]
    )
    assert data["lane_owned"]["content"] == "lane-a"
    assert "Indexed-query prerequisite" in str(data["blocked_other_lane"]["error"])
    assert data["gate"]["current_lane_id"] == "lane-a"
    assert data["gate"]["lane_exact_paths"] == ["src/lane_a.py"]


def test_task_complete_clears_lane_scoped_read_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "lane_a.py").write_text("lane-a\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project, "s1", "Lane Reset", "user", "Reset lane state"
        )
        hub.sessions.plan_file(project, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Shared work\n"
            "- Lane: lane-a\n"
            "- Files: src/lane_a.py\n"
            "- [ ] Build lane a\n",
            encoding="utf-8",
        )
        hub.managed_mode.set_mode(project, session_id=session.session_id)

        await server.call_tool(
            "aidocs_task_begin",
            {
                "project_root": str(project),
                "session_id": session.session_id,
                "goal": "Implement lane a",
                "relevant_files": ["src/lane_a.py"],
                "include_code_bundle": False,
            },
        )
        before_complete = hub.query_gate.get(project, session.session_id)
        completed = _payload_json(
            await server.call_tool(
                "aidocs_task_complete",
                {
                    "project_root": str(project),
                    "session_id": session.session_id,
                    "result_summary": "Finished lane a",
                    "verification_evidence": {
                        "commands_run": ["pytest tests/test_lane_a.py -q"],
                        "command_results": ["1 passed"],
                    },
                    "include_code_bundle": False,
                },
            )
        )
        blocked_after_complete = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_a.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        after_complete = hub.query_gate.get(project, session.session_id)
        return {
            "completed": completed,
            "before_complete": before_complete,
            "blocked_after_complete": blocked_after_complete,
            "after_complete": after_complete,
        }

    data = asyncio.run(run())

    assert data["before_complete"]["current_lane_id"] == "lane-a"
    assert data["before_complete"]["lane_exact_paths"] == ["src/lane_a.py"]
    assert data["completed"]["session"]["sections"]["Status"][0] == "- done"
    assert data["after_complete"]["current_lane_id"] is None
    assert data["after_complete"]["lane_exact_paths"] == []


def test_lane_files_are_conductor_hints_not_hard_edit_locks(tmp_path: Path) -> None:
    """Lane file ownership in the plan does not act as a hard security gate for reads."""
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "lane_a.py").write_text("lane-a\n", encoding="utf-8")
    (src / "other.py").write_text("other\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project, "s1", "Lane Hints", "user", "Lane files are hints"
        )
        hub.sessions.plan_file(project, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Shared work\n"
            "- Lane: lane-a\n"
            "- Files: src/lane_a.py\n"
            "- [ ] Build lane a\n",
            encoding="utf-8",
        )
        hub.managed_mode.set_mode(project, session_id=session.session_id)
        hub.query_gate.set(
            project,
            session.session_id,
            allow_read=False,
            last_tool="task_begin",
            current_lane_id="lane-a",
        )

        lane_owned = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_a.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        blocked_other = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/other.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        return {
            "lane_owned": lane_owned,
            "blocked_other": blocked_other,
        }

    data = asyncio.run(run())

    assert "Indexed-query prerequisite" in str(data["lane_owned"]["error"])
    assert "Indexed-query prerequisite" in str(data["blocked_other"]["error"])


def test_conductor_can_delegate_small_fix_outside_current_lane_ownership(
    tmp_path: Path,
) -> None:
    """The conductor can grant explicit read scope for files outside lane ownership."""
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "lane_a.py").write_text("lane-a\n", encoding="utf-8")
    (src / "other.py").write_text("other\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project, "s1", "Delegate Fix", "user", "Delegate outside lane"
        )
        hub.sessions.plan_file(project, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Shared work\n"
            "- Lane: lane-a\n"
            "- Files: src/lane_a.py\n"
            "- [ ] Build lane a\n",
            encoding="utf-8",
        )
        hub.managed_mode.set_mode(project, session_id=session.session_id)
        hub.query_gate.set(
            project,
            session.session_id,
            allow_read=False,
            last_tool="task_begin",
            current_lane_id="lane-a",
            lane_exact_paths=["src/other.py"],
        )

        lane_owned = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_a.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        delegated = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/other.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        return {
            "lane_owned": lane_owned,
            "delegated": delegated,
        }

    data = asyncio.run(run())

    assert "Indexed-query prerequisite" in str(data["lane_owned"]["error"])
    assert data["delegated"]["content"] == "other"


def test_lane_context_still_helps_with_read_scope_without_becoming_security_policy(
    tmp_path: Path,
) -> None:
    """Lane context (lane_exact_paths) grants reads without lane ownership becoming security policy."""
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "lane_a.py").write_text("lane-a\n", encoding="utf-8")
    (src / "lane_b.py").write_text("lane-b\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project, "s1", "Lane Context", "user", "Lane context helps reads"
        )
        hub.sessions.plan_file(project, session.session_id).write_text(
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
        hub.managed_mode.set_mode(project, session_id=session.session_id)
        hub.query_gate.set(
            project,
            session.session_id,
            allow_read=False,
            last_tool="task_begin",
            current_lane_id="lane-a",
            lane_exact_paths=["src/lane_a.py"],
        )

        granted = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_a.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        blocked = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/lane_b.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        return {
            "granted": granted,
            "blocked": blocked,
        }

    data = asyncio.run(run())

    assert data["granted"]["content"] == "lane-a"
    assert "Indexed-query prerequisite" in str(data["blocked"]["error"])


def test_precision_tools_do_not_grant_blanket_read_access(tmp_path: Path) -> None:
    """Calling precision tools (symbol snippet, method signature, etc.) should not
    unlock blanket read access to all files."""
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(
        "class Foo:\n    def bar(self): pass\n", encoding="utf-8"
    )
    (src / "other.py").write_text("other\n", encoding="utf-8")

    async def run() -> dict[str, dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")

        await server.call_tool(
            "aidocs_code_index_sync",
            {"project_root": str(project), "include_tests": False},
        )

        await server.call_tool(
            "aidocs_code_get_method_signature",
            {"project_root": str(project), "method": "bar"},
        )

        blocked_read = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "src/app.py",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                },
            )
        )
        gate = hub.query_gate.get(project, "s1")
        return {"blocked_read": blocked_read, "gate": gate}

    data = asyncio.run(run())

    assert "Indexed-query prerequisite" in str(data["blocked_read"]["error"])
    assert data["gate"]["allow_read"] is False


def test_blanket_allow_read_no_ttl_expiry(tmp_path: Path) -> None:
    """TTL-based expiry was removed — allow_read is now a legacy passthrough field.

    The QueryGateStore no longer expires allow_read grants based on granted_at
    timestamps. AccessGate ignores allow_read entirely (per-file discovery only),
    but the store still preserves the field for backward compat with callers that
    haven't been migrated yet.
    """
    import json
    from datetime import datetime, timedelta

    project = tmp_path / "project"
    (project / ".MEMORY" / "sessions" / "s1").mkdir(parents=True, exist_ok=True)

    gate_path = project / ".MEMORY" / "sessions" / "s1" / "query-gate.json"
    old_time = (datetime.now() - timedelta(minutes=999)).strftime("%Y-%m-%d %H:%M:%S")
    gate_path.write_text(
        json.dumps(
            {
                "allow_read": True,
                "granted_at": old_time,
                "last_tool": "code_search",
                "known_exact_paths": [],
                "current_lane_id": None,
                "lane_exact_paths": [],
                "updated_at": old_time,
            }
        ),
        encoding="utf-8",
    )

    from aidocs_mcp.query_gate import QueryGateStore

    store = QueryGateStore()
    state = store.get(project, "s1")

    # No TTL expiry — the value passes through as-is from JSON
    assert state["allow_read"] is True
