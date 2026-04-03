from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore
from aidocs_mcp.language_descriptors import (
    descriptor_match_summary,
    descriptor_for_language,
    descriptor_registry_summary,
    descriptor_semantics_summary,
    language_for_custom_descriptor,
    load_language_descriptors,
    validate_language_descriptors,
)


def test_load_language_descriptors_from_toml(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "dart.toml").write_text(
        'name = "dart"\n'
        'extensions = [".dart"]\n'
        'include_globs = ["lib/**/*.dart", "test/**/*.dart"]\n',
        encoding="utf-8",
    )

    data = load_language_descriptors(project_root)

    assert data["extensions"][".dart"] == "dart"
    assert ("lib/**/*.dart", "dart") in data["include_globs"]
    assert data["descriptors"]["dart"].tier == "heuristic"
    assert data["descriptors"]["dart"].source == "project"


def test_language_for_custom_descriptor_by_extension(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "cpp.toml").write_text(
        'name = "cpp"\n'
        'extensions = [".cxx", ".hppx"]\n',
        encoding="utf-8",
    )

    language = language_for_custom_descriptor(project_root, "src/foo.cxx", ".cxx")

    assert language == "cpp"


def test_sync_code_files_honors_custom_descriptor_extension(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "dart.toml").write_text(
        'name = "dart"\n'
        'extensions = [".dartx"]\n',
        encoding="utf-8",
    )
    (project_root / "lib").mkdir(parents=True, exist_ok=True)
    (project_root / "lib" / "app.dartx").write_text("class App {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)

    assert count == 1
    with store.connect(project_root) as conn:
        row = conn.execute("SELECT language, language_tier, language_source FROM code_files WHERE path = 'lib/app.dartx'").fetchone()
    assert row["language"] == "dart"
    assert row["language_source"] == "project"
    assert row["language_tier"] == "heuristic"


def test_project_local_descriptor_overrides_built_in_language_metadata(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "python.toml").write_text(
        'name = "python"\n'
        'extensions = [".py"]\n'
        'tier = "heuristic"\n',
        encoding="utf-8",
    )

    descriptor = descriptor_for_language(project_root, "src/app.py", ".py")

    assert descriptor is not None
    assert descriptor.name == "python"
    assert descriptor.source == "project"
    assert descriptor.tier == "heuristic"


def test_extension_based_descriptor_filename_works_without_name_field(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / ".foo.toml").write_text(
        'extensions = [".foo"]\n'
        'tier = "heuristic"\n',
        encoding="utf-8",
    )

    descriptor = descriptor_for_language(project_root, "src/app.foo", ".foo")

    assert descriptor is not None
    assert descriptor.name == ".foo"


def test_descriptor_registry_summary_includes_metadata(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r", ".R"]\n'
        'tier = "summary"\n',
        encoding="utf-8",
    )

    summary = descriptor_registry_summary(project_root)

    assert summary["count"] >= 1
    assert any(item["name"] == "r" and item["source"] == "project" and item["tier"] == "summary" for item in summary["descriptors"])
    r_item = next(item for item in summary["descriptors"] if item["name"] == "r")
    assert r_item["uses_semantic_tags"] is False


def test_project_local_role_hint_affects_indexed_file_role(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r"]\n'
        'role_hint = "analysis-script"\n',
        encoding="utf-8",
    )
    (project_root / "R").mkdir(parents=True, exist_ok=True)
    (project_root / "R" / "analysis.r").write_text("summary <- function(x) x\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        row = conn.execute("SELECT role FROM code_files WHERE path = 'R/analysis.r'").fetchone()

    assert row["role"] == "analysis-script"


def test_built_in_csharp_descriptor_role_patterns_affect_role_inference(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "PatientService.cs").write_text("public class PatientService {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        row = conn.execute("SELECT role FROM code_files WHERE path = 'Services/PatientService.cs'").fetchone()

    assert row["role"] == "service"


def test_built_in_csharp_semantic_tag_expands_module_hints(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "DTOs").mkdir(parents=True, exist_ok=True)
    (project_root / "DTOs" / "A.cs").write_text("public class A {}\n", encoding="utf-8")
    (project_root / "DTOs" / "B.cs").write_text("public class B {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    modules = store.detect_modules(project_root)

    assert any(module["module_path"] == "DTOs" for module in modules)


def test_built_in_typescript_descriptor_role_patterns_affect_role_inference(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "api").mkdir(parents=True, exist_ok=True)
    (project_root / "api" / "session.ts").write_text("export const route = true\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        row = conn.execute("SELECT role FROM code_files WHERE path = 'api/session.ts'").fetchone()

    assert row["role"] == "route-handler"


def test_built_in_typescript_semantic_tag_expands_barrel_role(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components" / "cms").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "cms" / "index.ts").write_text("export * from './EditPanel'\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        row = conn.execute("SELECT role FROM code_files WHERE path = 'web/components/cms/index.ts'").fetchone()

    assert row["role"] == "barrel-module"


def test_project_local_module_hints_extend_module_detection(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r"]\n'
        'module_hints = ["analyses"]\n',
        encoding="utf-8",
    )
    (project_root / "analyses").mkdir(parents=True, exist_ok=True)
    (project_root / "analyses" / "a.r").write_text("x <- function() x\n", encoding="utf-8")
    (project_root / "analyses" / "b.r").write_text("y <- function() y\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    modules = store.detect_modules(project_root)

    assert any(module["module_path"] == "analyses" for module in modules)


def test_code_status_reports_language_tier_and_source_counts(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r"]\n'
        'tier = "heuristic"\n',
        encoding="utf-8",
    )
    (project_root / "R").mkdir(parents=True, exist_ok=True)
    (project_root / "R" / "analysis.r").write_text("summary <- function(x) x\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["language_tiers"]["heuristic"] >= 1
    assert status["language_sources"]["project"] >= 1


def test_search_code_and_investigate_surface_language_metadata(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r"]\n'
        'tier = "heuristic"\n',
        encoding="utf-8",
    )
    (project_root / "R").mkdir(parents=True, exist_ok=True)
    (project_root / "R" / "analysis.r").write_text("summary <- function(x) x\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    search = store.search_code(project_root, "summary", limit=5)
    investigate = store.investigate(project_root, "summary", limit=5)

    assert search[0]["language_tier"] == "heuristic"
    assert search[0]["language_source"] == "project"
    files = next(item for item in investigate["findings"] if item["area"] == "files")
    assert files["top"][0]["language_tier"] == "heuristic"


def test_search_code_gives_small_preference_to_richer_support_tiers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r"]\n'
        'tier = "heuristic"\n',
        encoding="utf-8",
    )
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "rich.py").write_text("alpha term\n", encoding="utf-8")
    (project_root / "src" / "heuristic.r").write_text("alpha term\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    results = store.search_code(project_root, "alpha", limit=5)

    assert results[0]["path"] == "src/rich.py"
    assert any("tier_weight" in reason for reason in results[0]["why"])


def test_validate_language_descriptors_reports_missing_discovery_keys(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "broken.toml").write_text(
        'name = "broken"\n',
        encoding="utf-8",
    )

    result = validate_language_descriptors(project_root)

    assert result["count"] >= 1
    assert any("missing discovery keys" in issue["issue"] for issue in result["issues"])


def test_validate_language_descriptors_reports_extension_collisions(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "a.toml").write_text(
        'name = "alpha"\n'
        'extensions = [".foo"]\n',
        encoding="utf-8",
    )
    (project_root / "index_languages" / "b.toml").write_text(
        'name = "beta"\n'
        'extensions = [".foo"]\n',
        encoding="utf-8",
    )

    result = validate_language_descriptors(project_root)

    assert any("extension collision" in issue["issue"] for issue in result["issues"])


def test_validate_language_descriptors_reports_unknown_outline_family(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "weird.toml").write_text(
        'name = "weird"\n'
        'extensions = [".weird"]\n'
        'outline_family = "nonexistent_family"\n',
        encoding="utf-8",
    )

    result = validate_language_descriptors(project_root)

    assert any("unknown outline_family" in issue["issue"] for issue in result["issues"])


def test_descriptor_semantics_summary_lists_outline_families() -> None:
    summary = descriptor_semantics_summary()

    assert "outline_families" in summary
    assert "rust_basic" in summary["outline_families"]
    assert "embedded_semantics" in summary
    assert summary["built_in_descriptor_count"] >= summary["built_in_with_extractor_family"]


def test_descriptor_match_summary_shows_project_override_match(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r"]\n'
        'tier = "heuristic"\n',
        encoding="utf-8",
    )

    result = descriptor_match_summary(project_root, "R/helpers.r")

    assert result["matched"] is True
    assert result["language"] == "r"
    assert result["descriptor"]["source"] == "project"


def test_descriptor_match_summary_includes_predicted_role_and_tags(tmp_path: Path) -> None:
    project_root = tmp_path / "project"

    result = descriptor_match_summary(project_root, "web/components/cms/index.tsx")

    assert result["matched"] is True
    assert result["predicted_role"] == "barrel-module"
    assert result["descriptor"]["semantic_tags"] == []
    assert "html_like" in result["descriptor"]["embedded_semantics"]
    assert any(item["glob"] == "**/components/**/index.tsx" for item in result["descriptor"]["role_patterns"])
    assert result["descriptor"]["uses_semantic_tags"] is False
    assert result["descriptor"]["uses_raw_role_patterns"] is True


def test_builtin_descriptor_reports_built_in_source(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    descriptor = descriptor_for_language(project_root, "src/app.py", ".py")

    assert descriptor is not None
    assert descriptor.name == "python"
    assert descriptor.source == "built_in"


def test_toml_outline_patterns_drive_generic_outline_extraction(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r"]\n'
        'tier = "heuristic"\n'
        'outline_patterns = [\n'
        "  { pattern = '^\\s*([A-Za-z_][A-Za-z0-9_.]*)\\s*<-\\s*function\\s*\\(', kind = \"function\" },\n"
        ']\n',
        encoding="utf-8",
    )
    (project_root / "R").mkdir(parents=True, exist_ok=True)
    (project_root / "R" / "helpers.r").write_text(
        "build_patient <- function(id) {\n"
        "  id\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    outline = store.get_outline(project_root, "R/helpers.r")

    assert outline == [{"symbol": "build_patient", "kind": "function", "line_number": 1}]
