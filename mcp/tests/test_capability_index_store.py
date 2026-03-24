from pathlib import Path

from aidocs_mcp.capability_index_store import CapabilityIndexStore
from aidocs_mcp.mcp_server import create_server


class _TaskConfig:
    def __init__(self, mode: str = "forbidden") -> None:
        self.mode = mode


class _Tool:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.title = None
        self.description = description
        self.tags = set()
        self.parameters = {"type": "object", "properties": {}}
        self.output_schema = {"type": "object", "properties": {}}
        self.meta = {"capability_aliases": ["alias_one", "alias_two"], "capability_family": "demo_family"}
        self.task_config = _TaskConfig()
        self.timeout = None


def test_sync_capabilities_indexes_registered_mcp_tools(tmp_path: Path) -> None:
    store = CapabilityIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    server = create_server()
    tools = [component for key, component in server._local_provider._components.items() if str(key).startswith("tool:")]

    count = store.sync_capabilities(project_root, tools)
    status = store.capability_status(project_root)
    session_tools = store.find_capabilities(project_root, query="session", limit=20)
    session_list = store.get_capability(project_root, "session_list")

    assert count >= 50
    assert status["capability_definitions"] == count
    assert status["by_kind"]["mcp_tool"] == count
    assert status["by_source"]["mcp_registry"] == count
    assert any(item["name"] == "session_list" for item in session_tools)
    assert session_list is not None
    assert session_list["name"] == "session_list"
    assert session_list["capability_kind"] == "mcp_tool"
    assert session_list["source_kind"] == "mcp_registry"
    assert session_list["task_mode"] == "forbidden"
    assert "project_root" in session_list["parameters"]["properties"]


def test_sync_capabilities_preserves_aliases_and_family(tmp_path: Path) -> None:
    store = CapabilityIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_capabilities(project_root, [_Tool("command_runner", "Run commands")])
    capability = store.get_capability(project_root, "command_runner")
    alias_results = store.find_capabilities(project_root, query="alias_one", limit=10)

    assert count == 1
    assert capability is not None
    assert capability["capability_family"] == "demo_family"
    assert capability["aliases"] == ["alias_one", "alias_two"]
    assert alias_results[0]["name"] == "command_runner"
