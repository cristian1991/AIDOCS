from pathlib import Path

from aidocs_mcp.mcp_server import (
    _apply_trace_depth,
    _grant_indexed_read_gate,
    _grant_known_exact_path_read,
    _require_indexed_read_gate,
    create_server,
)


def test_apply_trace_depth_filters_service_matches_by_source_level() -> None:
    payload = {
        "matches": [
            {"source": "definition", "symbol": "Service"},
            {"source": "reference", "symbol": "Service"},
            {"source": "file_match", "symbol": None},
        ]
    }

    result = _apply_trace_depth(payload, "service", 2)

    assert len(result["matches"]) == 2
    assert {item["source"] for item in result["matches"]} == {"definition", "reference"}


def test_apply_trace_depth_trims_api_to_ui_layers() -> None:
    payload = {
        "api": [{"path": "server/routes/session.ts"}],
        "logic": [{"path": "core/session.ts"}],
        "ui": [{"path": "app/session.tsx"}],
    }

    result = _apply_trace_depth(payload, "api_to_ui", 2)

    assert result["api"]
    assert result["logic"]
    assert result["ui"] == []


def test_indexed_read_gate_blocks_when_no_indexed_query_used(tmp_path: Path) -> None:
    server = create_server()
    hub = server._aidocs_test_hub
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.managed_mode.set_mode(project_root, session_id="s1")

    result = _require_indexed_read_gate(hub, project_root)

    assert result is not None
    assert "Indexed-query prerequisite" in result["error"]


def test_indexed_read_gate_unlocks_after_grant(tmp_path: Path) -> None:
    server = create_server()
    hub = server._aidocs_test_hub
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.managed_mode.set_mode(project_root, session_id="s1")

    _grant_indexed_read_gate(hub, project_root, "code_find")
    result = _require_indexed_read_gate(hub, project_root)

    assert result is None


def test_known_exact_path_grant_stays_narrow(tmp_path: Path) -> None:
    server = create_server()
    hub = server._aidocs_test_hub
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.managed_mode.set_mode(project_root, session_id="s1")

    _grant_known_exact_path_read(hub, project_root, "aidocs_code_create_file", "src/new.txt")

    assert _require_indexed_read_gate(hub, project_root, exact_path="src/new.txt") is None
    blocked = _require_indexed_read_gate(hub, project_root)
    assert blocked is not None
    gate = hub.query_gate.get(project_root, "s1")
    assert gate["allow_read"] is False
    assert gate["last_tool"] == "known_exact_path:aidocs_code_create_file:src/new.txt"
    assert gate["known_exact_paths"] == ["src/new.txt"]


def test_exact_known_relative_code_path_skips_discovery_gate(tmp_path: Path) -> None:
    server = create_server()
    hub = server._aidocs_test_hub
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.managed_mode.set_mode(project_root, session_id="s1")

    assert _require_indexed_read_gate(hub, project_root, exact_path="src/module.py") is None


def test_protected_exact_path_does_not_skip_discovery_gate(tmp_path: Path) -> None:
    server = create_server()
    hub = server._aidocs_test_hub
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.managed_mode.set_mode(project_root, session_id="s1")

    result = _require_indexed_read_gate(hub, project_root, exact_path="aidocs.toml")

    assert result is not None
    assert "Indexed-query prerequisite" in result["error"]


def test_protected_exact_path_cannot_be_granted(tmp_path: Path) -> None:
    server = create_server()
    hub = server._aidocs_test_hub
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.managed_mode.set_mode(project_root, session_id="s1")

    _grant_known_exact_path_read(hub, project_root, "code_create_file", "aidocs.toml")

    result = _require_indexed_read_gate(hub, project_root, exact_path="aidocs.toml")
    gate = hub.query_gate.get(project_root, "s1")

    assert result is not None
    assert "Indexed-query prerequisite" in result["error"]
    assert gate["known_exact_paths"] == []
    assert gate["last_tool"] is None


def test_second_server_grant_in_different_project_does_not_unlock_first_server_gate(tmp_path: Path) -> None:
    project_root = tmp_path / "project-a"
    other_project_root = tmp_path / "project-b"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (other_project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    server1 = create_server()
    hub1 = server1._aidocs_test_hub
    hub1.managed_mode.set_mode(project_root, session_id="s1")
    blocked_before = _require_indexed_read_gate(hub1, project_root)

    server2 = create_server()
    hub2 = server2._aidocs_test_hub
    hub2.managed_mode.set_mode(other_project_root, session_id="s1")

    _grant_indexed_read_gate(hub2, other_project_root, "code_find")
    blocked_after = _require_indexed_read_gate(hub1, project_root)
    gate1 = hub1.query_gate.get(project_root, "s1")
    gate2 = hub2.query_gate.get(other_project_root, "s1")

    assert blocked_before is not None
    assert blocked_after is not None
    assert gate1["allow_read"] is False
    assert gate1["last_tool"] is None
    assert gate2["allow_read"] is True
    assert gate2["last_tool"] == "code_find"


def test_indexed_read_gate_resets_on_task_begin_and_task_complete(tmp_path: Path) -> None:
    from aidocs_mcp.runtime_service import RuntimeService
    from aidocs_mcp.service_hub import AidocsServiceHub

    server = create_server()
    hub = server._aidocs_test_hub
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    hub.managed_mode.set_mode(project_root, session_id="2026-03-23-a")
    _grant_indexed_read_gate(hub, project_root, "code_find")
    assert _require_indexed_read_gate(hub, project_root) is None

    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (templates / "context.md").write_text("# Context\n", encoding="utf-8")
    hub2 = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub2)
    (project_root / ".MEMORY" / ".aidocs").mkdir(parents=True, exist_ok=True)
    for name in ["index.aidocs", "global-instructions.aidocs", "coding-standards.aidocs", "memory-system.aidocs"]:
        (project_root / ".MEMORY" / ".aidocs" / name).write_text(f"# {name}\n", encoding="utf-8")
    (project_root / ".MEMORY" / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    hub2.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    runtime.task_begin(project_root, "2026-03-23-a", goal="Do work", include_code_bundle=False)
    gate = hub2.query_gate.get(project_root, "2026-03-23-a")
    assert gate["allow_read"] is False
    assert gate["last_tool"] == "task_begin"
    hub2.query_gate.set(project_root, "2026-03-23-a", allow_read=True, last_tool="code_find")
    runtime.task_complete(project_root, "2026-03-23-a", result_summary="Done", include_code_bundle=False)
    gate = hub2.query_gate.get(project_root, "2026-03-23-a")
    assert gate["allow_read"] is False
    assert gate["last_tool"] == "task_complete"

