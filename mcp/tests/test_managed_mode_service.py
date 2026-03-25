from pathlib import Path

from aidocs_mcp.managed_mode_service import ManagedModeService


def test_managed_mode_set_get_clear(tmp_path: Path) -> None:
    service = ManagedModeService()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY" / "config").mkdir(parents=True, exist_ok=True)

    active = service.set_mode(project_root, session_id="2026-03-23-a", source="/aidocs")
    current = service.get_mode(project_root)
    cleared = service.clear_mode(project_root)

    assert active["active"] is True
    assert active["session_id"] == "2026-03-23-a"
    assert current["active"] is True
    assert current["session_id"] == "2026-03-23-a"
    assert cleared["active"] is False
