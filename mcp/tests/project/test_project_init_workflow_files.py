from pathlib import Path
import asyncio

from aidocs_mcp.mcp_server import create_server


def test_project_init_creates_split_workflow_files(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    async def run() -> object:
        server = create_server()
        tools = await server.list_tools()
        assert any(item.name == "project_init" for item in tools)
        return await server.call_tool("project_init", {"root": str(project_root), "init_git": False, "create_remote": False})

    result = asyncio.run(run())
    payload = result[0].text if isinstance(result, list) else str(result)

    assert '"initialized":true' in payload.lower()
    assert (project_root / ".MEMORY" / "rules" / "workflow-rules.md").is_file()
    assert (project_root / ".MEMORY" / "rules" / "workflow-actions.md").is_file()
