from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore
from aidocs_mcp.related_project_service import RelatedProjectService
from aidocs_mcp.schema_index_store import SchemaIndexStore


def test_related_project_config_parsing(tmp_path: Path) -> None:
    service = RelatedProjectService()
    project_root = tmp_path / "project"
    config = project_root / ".MEMORY" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "related-projects.md").write_text(
        "# Related Projects\n\n"
        "## DentalApp\n"
        "- Path: `D:/Projects/Active/DentalApp`\n"
        "- Reason: Legacy source for current business logic\n\n"
        "## Musicity\n"
        "- Path: `D:/Projects/Active/Musicity`\n"
        "- Notes: Shared deployment concepts\n",
        encoding="utf-8",
    )

    items = service.list_related_projects(project_root)
    dental = service.get_related_project(project_root, "DentalApp")

    assert len(items) == 2
    assert dental is not None
    assert dental["path"] == "D:/Projects/Active/DentalApp"
    assert dental["reason"] == "Legacy source for current business logic"


def test_related_project_path_resolution(tmp_path: Path) -> None:
    service = RelatedProjectService()
    project_root = tmp_path / "project"
    related_root = tmp_path / "DentalApp"
    related_root.mkdir(parents=True, exist_ok=True)
    config = project_root / ".MEMORY" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "related-projects.md").write_text(
        "# Related Projects\n\n"
        f"## DentalApp\n- Path: `{related_root}`\n",
        encoding="utf-8",
    )

    resolved = service.resolve_related_project_path(project_root, "DentalApp")

    assert resolved == related_root


def test_related_project_compare_concept_shape(tmp_path: Path) -> None:
    root = tmp_path / "current"
    related = tmp_path / "DentalApp"
    for project in [root, related]:
        (project / ".MEMORY" / "config").mkdir(parents=True, exist_ok=True)
        (project / ".MEMORY").mkdir(parents=True, exist_ok=True)
        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "QuoteService.cs").write_text(
            "public class QuoteService { public void LoadQuoteForm() {} }\n",
            encoding="utf-8",
        )
        (project / "Db").mkdir(parents=True, exist_ok=True)
        (project / "Db" / "schema.sql").write_text(
            "CREATE TABLE Quotes ( Id INT );\n",
            encoding="utf-8",
        )
    (root / ".MEMORY" / "config" / "related-projects.md").write_text(
        f"# Related Projects\n\n## DentalApp\n- Path: `{related}`\n",
        encoding="utf-8",
    )

    service = RelatedProjectService()
    related_root = service.resolve_related_project_path(root, "DentalApp")
    code = CodeIndexStore()
    schema = SchemaIndexStore()
    code.sync_code_manifest(root, include_tests=False)
    code.sync_code_manifest(related_root, include_tests=False)
    schema.sync_schema(root)
    schema.sync_schema(related_root)
    result = {
        "concept": "Quote",
        "related_project": {"name": "DentalApp", "path": str(related_root)},
        "current": code.get_subsystem_bundle(root, concept="Quote", limit=10),
        "related": code.get_subsystem_bundle(related_root, concept="Quote", limit=10),
    }

    assert result["concept"] == "Quote"
    assert result["related_project"]["name"] == "DentalApp"
    assert result["current"]["domain_cluster"]
    assert result["related"]["domain_cluster"]
