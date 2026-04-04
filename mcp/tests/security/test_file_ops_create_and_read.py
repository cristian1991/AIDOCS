from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aidocs_mcp.access_gate import _is_safe_grantable_path
from aidocs_mcp.mcp_server import create_server


def _payload_json(result: object) -> dict[str, object]:
    payload = result[0].text if isinstance(result, list) else result.content[0].text
    return json.loads(payload)


def test_known_exact_path_allows_followup_read_after_native_file_create(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project / "src").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[object, object]:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")

        created = await server.call_tool(
            "aidocs_code_create_file",
            {
                "project_root": str(project),
                "path": "src/created.txt",
                "content": "one\ntwo\nthree\n",
            },
        )
        lines = await server.call_tool(
            "aidocs_code_get_lines",
            {
                "project_root": str(project),
                "path": "src/created.txt",
                "start_line": 2,
                "count": 1,
                "show_line_numbers": False,
                "known_exact_path": True,
            },
        )
        return created, lines

    created, lines = asyncio.run(run())
    created_data = _payload_json(created)
    data = _payload_json(lines)

    assert created_data["success"] is True
    assert data["path"] == "src/created.txt"
    assert data["start_line"] == 2
    assert data["end_line"] == 2
    assert data["content"] == "two"


def test_known_exact_path_requires_project_relative_path_even_after_grant(tmp_path: Path) -> None:
    """Absolute paths are rejected by file_ops even with known_exact_path."""
    import pytest as _pytest
    from fastmcp.exceptions import ToolError

    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    created_path = project / "src" / "created.txt"
    created_path.parent.mkdir(parents=True, exist_ok=True)

    async def run() -> None:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")

        await server.call_tool(
            "aidocs_code_create_file",
            {
                "project_root": str(project),
                "path": "src/created.txt",
                "content": "one\ntwo\n",
            },
        )
        with _pytest.raises(ToolError, match="Absolute paths are not allowed"):
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": str(created_path),
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )

    asyncio.run(run())
    assert _is_safe_grantable_path(str(created_path)) is False


def test_known_exact_path_rejects_drive_qualified_absolute_path() -> None:
    assert _is_safe_grantable_path("C:/temp/file.txt") is False

