from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore
from aidocs_mcp.index_store import IndexStore
from aidocs_mcp.schema_index_store import SchemaIndexStore
from aidocs_mcp.session_store import SessionStore
from aidocs_mcp.updater_service import UpdaterService


def test_inspect_legacy_runtime_flags(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    service = UpdaterService(script_root=scripts)
    project_root = tmp_path / "project"
    memory_root = project_root / ".MEMORY"
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "NOW.md").write_text("legacy now\n", encoding="utf-8")
    (memory_root / "plans").mkdir()

    result = service.inspect_legacy_runtime(project_root)

    assert result["has_now"] is True
    assert result["has_root_plans"] is True
    assert result["legacy_present"] is True


def test_inspect_legacy_runtime_clean_project(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    service = UpdaterService(script_root=scripts)
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    result = service.inspect_legacy_runtime(project_root)

    assert result["legacy_present"] is False


def test_combined_index_status_shape(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text("# Session\n", encoding="utf-8")
    (templates / "context.md").write_text("# Context\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Db" / "schema.sql").write_text("CREATE TABLE Quotes ( Id INT );\n", encoding="utf-8")

    memory = IndexStore(session_store=SessionStore(templates_root=templates))
    code = CodeIndexStore()
    schema = SchemaIndexStore()

    memory.sync_all(project_root)
    code.sync_code_manifest(project_root)
    schema.sync_schema(project_root)

    memory_status = memory.status(project_root)
    code_status = code.code_status(project_root)
    schema_status = schema.schema_status(project_root)

    assert "memory_files" in memory_status
    assert "code_files" in code_status
    assert "schema_entities" in schema_status
