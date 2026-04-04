import asyncio
from pathlib import Path

from aidocs_mcp.mcp_server import create_server


def test_public_tool_surface_has_no_aidocs_prefix() -> None:
    """Tools no longer have the aidocs_ prefix — hosts add their own branding."""
    async def run() -> list[str]:
        server = create_server()
        return sorted(tool.name for tool in await server.list_tools())

    tool_names = asyncio.run(run())

    assert [name for name in tool_names if name.startswith("aidocs_")] == []


def test_precision_and_debug_tools_remain_distinct_in_taxonomy() -> None:
    async def run() -> set[str]:
        server = create_server()
        return {tool.name for tool in await server.list_tools()}

    tool_names = asyncio.run(run())

    assert {
        "code_get_lines",
        "code_edit_lines",
        "db_query",
        "execution_query_summary",
        "git_fork_status",
        "schema_query",
    } <= tool_names


def test_execution_recorder_tools_do_not_self_instrument_under_aidocs_taxonomy(tmp_path: Path) -> None:
    project_root = tmp_path / "project"

    async def run() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        server = create_server()
        hub = server._aidocs_test_hub

        await server.call_tool(
            "execution_run_record",
            {
                "root": str(project_root),
                "run_kind": "manual_check",
                "source_kind": "test",
                "capability_name": "taxonomy-test",
                "status": "completed",
            },
            run_middleware=False,
        )

        return hub.execution.list_runs(project_root, limit=10), hub.execution.list_events(project_root, limit=10)

    runs, events = asyncio.run(run())

    assert len(runs) == 1
    assert runs[0]["run_kind"] == "manual_check"
    assert runs[0]["capability_name"] == "taxonomy-test"
    assert events == []
