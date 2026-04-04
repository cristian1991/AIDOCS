from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aidocs_mcp.mcp_server import create_server


def _payload_json(result: object) -> dict[str, object]:
    payload = result[0].text if isinstance(result, list) else result.content[0].text
    return json.loads(payload)


def test_discovery_read_still_requires_indexed_query(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")

    async def run() -> object:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")
        return await server.call_tool(
            "aidocs_code_get_lines",
            {
                "project_root": str(project),
                "path": "src/module.py",
                "start_line": 1,
                "count": 1,
                "show_line_numbers": False,
            },
        )

    data = _payload_json(asyncio.run(run()))

    assert "Indexed-query prerequisite" in str(data["error"])


def test_known_exact_path_bypasses_read_gate(
    tmp_path: Path,
) -> None:
    """known_exact_path=true now bypasses the indexed-read gate."""
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")

    async def run() -> object:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")
        return await server.call_tool(
            "aidocs_code_get_lines",
            {
                "project_root": str(project),
                "path": "src/module.py",
                "start_line": 1,
                "count": 1,
                "show_line_numbers": False,
                "known_exact_path": True,
            },
        )

    data = _payload_json(asyncio.run(run()))

    assert "error" not in data
    assert data["content"] == "value = 1"


def test_protected_path_stays_blocked_even_with_known_exact_path(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project / "aidocs.toml").write_text(
        '[agent]\ndirective_style = "normal"\n', encoding="utf-8"
    )

    async def run() -> object:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")
        return await server.call_tool(
            "aidocs_code_get_lines",
            {
                "project_root": str(project),
                "path": "aidocs.toml",
                "start_line": 1,
                "count": 1,
                "show_line_numbers": False,
                "known_exact_path": True,
            },
        )

    data = _payload_json(asyncio.run(run()))

    assert "Indexed-query prerequisite" in str(data["error"])


def test_protected_path_followup_read_stays_blocked_after_allowed_edit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project / "aidocs.toml").write_text(
        '[agent]\ndirective_style = "short"\n',
        encoding="utf-8",
    )

    async def run() -> tuple[dict[str, object], dict[str, object]]:
        server = create_server()
        hub = server._aidocs_test_hub
        hub.managed_mode.set_mode(project, session_id="s1")

        edited = _payload_json(
            await server.call_tool(
                "aidocs_code_edit_lines",
                {
                    "project_root": str(project),
                    "path": "aidocs.toml",
                    "start_line": 2,
                    "end_line": 2,
                    "new_content": 'directive_style = "detailed"',
                    "config_edit_mode": "explicit_user_permitted",
                },
            )
        )
        lines = _payload_json(
            await server.call_tool(
                "aidocs_code_get_lines",
                {
                    "project_root": str(project),
                    "path": "aidocs.toml",
                    "start_line": 1,
                    "count": 1,
                    "show_line_numbers": False,
                    "known_exact_path": True,
                },
            )
        )
        return edited, lines

    edited, lines = asyncio.run(run())

    assert edited["success"] is True
    assert "Indexed-query prerequisite" in str(lines["error"])
