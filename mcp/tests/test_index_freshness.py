import os
from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_code_status_reports_missing_stale_and_ready_states(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    app = project_root / "src" / "app.py"
    app.parent.mkdir(parents=True, exist_ok=True)
    app.write_text("def app():\n    return 1\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    missing = store.code_status(project_root)

    assert missing["code_files"] == 0
    assert missing["parsed_code_files"] == 0
    assert missing["freshness"]["state"] == "missing"
    assert "missing_index_state" in missing["freshness"]["reasons"]
    assert missing["freshness"]["drifted_paths"] == ["src/app.py"]

    store.sync_code_files(project_root)
    ready = store.code_status(project_root)

    assert ready["code_files"] == 1
    assert ready["parsed_code_files"] == 1
    assert ready["freshness"]["state"] == "ready"
    assert ready["freshness"]["reasons"] == []
    assert ready["freshness"]["drifted_paths"] == []
    assert ready["freshness"]["indexed_paths"] == 1
    assert ready["freshness"]["tracked_paths"] == 1

    app.write_text("def app():\n    return 2\n", encoding="utf-8")
    _bump_mtime(app)
    stale = store.code_status(project_root)

    assert stale["code_files"] == 1
    assert stale["parsed_code_files"] == 1
    assert stale["freshness"]["state"] == "stale"
    assert "content_drift" in stale["freshness"]["reasons"]
    assert stale["freshness"]["drifted_paths"] == ["src/app.py"]


def test_handled_file_edit_replacement_sync_keeps_index_fresh(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    src = project_root / "src"
    keep = src / "keep.py"
    change = src / "change.py"
    src.mkdir(parents=True, exist_ok=True)
    keep.write_text("def keep():\n    return 1\n", encoding="utf-8")
    change.write_text("def before():\n    return 1\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    change.write_text("def after():\n    return 2\n", encoding="utf-8")
    _bump_mtime(change)

    store.sync_code_files(project_root, paths=["src/change.py"])
    status = store.code_status(project_root)
    outline = store.get_outline(project_root, "src/change.py")

    assert status["code_files"] == 2
    assert status["parsed_code_files"] == 2
    assert status["freshness"]["state"] == "ready"
    assert status["freshness"]["reasons"] == []
    assert status["freshness"]["drifted_paths"] == []
    assert outline[0]["symbol"] == "after"


def test_code_status_detects_same_size_edit_even_when_mtime_is_restored(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    app = project_root / "src" / "app.py"
    app.parent.mkdir(parents=True, exist_ok=True)
    app.write_text("def app():\n    return 1\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    original_stat = app.stat()
    app.write_text("def app():\n    return 2\n", encoding="utf-8")
    os.utime(app, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    status = store.code_status(project_root)

    assert status["freshness"]["state"] == "stale"
    assert "content_drift" in status["freshness"]["reasons"]
    assert status["freshness"]["drifted_paths"] == ["src/app.py"]


def test_code_status_stays_ready_for_test_inclusive_index(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "tests").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (project_root / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root, include_tests=True)
    status = store.code_status(project_root)

    assert status["code_files"] == 2
    assert status["parsed_code_files"] == 2
    assert status["freshness"]["state"] == "ready"
    assert status["freshness"]["reasons"] == []
    assert status["freshness"]["extra_paths"] == []
    assert status["freshness"]["tracked_paths"] == 2
