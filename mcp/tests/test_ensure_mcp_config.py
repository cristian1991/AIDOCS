import json
from pathlib import Path

from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


def _make_runtime(tmp_path: Path) -> RuntimeService:
    templates_root = tmp_path / "templates"
    templates_root.mkdir(parents=True, exist_ok=True)
    (templates_root / "SESSION.md").write_text("# Session\n\n## Title\n- t\n\n## Status\n- active\n", encoding="utf-8")
    (templates_root / "context.md").write_text("# Context\n", encoding="utf-8")
    hub = AidocsServiceHub(templates_root=templates_root)
    return RuntimeService(hub=hub)


def test_ensure_mcp_config_creates_new_file(tmp_path: Path) -> None:
    """Creates .mcp.json with aidocs entry when no file exists."""
    runtime = _make_runtime(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    result = runtime.ensure_claude_mcp_config(project_root)

    assert result["action"] in ("created", "updated")
    mcp_json = project_root / ".mcp.json"
    assert mcp_json.is_file()

    data = json.loads(mcp_json.read_text(encoding="utf-8"))
    assert "aidocs" in data["mcpServers"]
    entry = data["mcpServers"]["aidocs"]
    assert entry["type"] == "stdio"
    assert "-m" in entry["args"]
    assert "aidocs_mcp.mcp_server" in entry["args"]
    assert "PYTHONPATH" in entry["env"]


def test_ensure_mcp_config_preserves_existing_servers(tmp_path: Path) -> None:
    """Adding aidocs entry doesn't remove existing MCP server entries."""
    runtime = _make_runtime(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    existing = {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["@playwright/mcp@latest"],
            }
        }
    }
    (project_root / ".mcp.json").write_text(json.dumps(existing), encoding="utf-8")

    runtime.ensure_claude_mcp_config(project_root)

    data = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
    assert "playwright" in data["mcpServers"]
    assert "aidocs" in data["mcpServers"]
    assert data["mcpServers"]["playwright"]["command"] == "npx"


def test_ensure_mcp_config_idempotent_no_change(tmp_path: Path) -> None:
    """Calling twice with no changes returns no_change."""
    runtime = _make_runtime(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    result1 = runtime.ensure_claude_mcp_config(project_root)
    result2 = runtime.ensure_claude_mcp_config(project_root)

    assert result1["action"] in ("created", "updated")
    assert result2["action"] == "no_change"


def test_ensure_mcp_config_updates_stale_entry(tmp_path: Path) -> None:
    """Updates aidocs entry if PYTHONPATH is different (e.g., AIDOCS moved)."""
    runtime = _make_runtime(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    stale = {
        "mcpServers": {
            "aidocs": {
                "type": "stdio",
                "command": "python",
                "args": ["-m", "aidocs_mcp.mcp_server"],
                "env": {"PYTHONPATH": "/old/path/that/doesnt/match"},
            }
        }
    }
    (project_root / ".mcp.json").write_text(json.dumps(stale), encoding="utf-8")

    result = runtime.ensure_claude_mcp_config(project_root)

    assert result["action"] in ("created", "updated")
    data = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["aidocs"]["env"]["PYTHONPATH"] != "/old/path/that/doesnt/match"


def test_ensure_mcp_config_handles_corrupt_json(tmp_path: Path) -> None:
    """Handles corrupt .mcp.json gracefully by overwriting."""
    runtime = _make_runtime(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    (project_root / ".mcp.json").write_text("{invalid json", encoding="utf-8")

    result = runtime.ensure_claude_mcp_config(project_root)

    assert result["action"] in ("created", "updated")
    data = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
    assert "aidocs" in data["mcpServers"]
