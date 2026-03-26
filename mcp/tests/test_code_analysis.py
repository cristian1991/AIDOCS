from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore
from aidocs_mcp.session_store import SessionStore
from aidocs_mcp.schema_index_store import SchemaIndexStore


def test_csharp_partial_group_detection(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "App" / "MainWindow.xaml.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void Initialize() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "App" / "MainWindow.Actions.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void RunAction() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status = store.code_status(project_root)
    outline = store.get_outline(project_root, "App/MainWindow.xaml.cs")
    partials = store.find_partial_group(project_root, "MainWindow")

    assert status["partial_symbols"] == 2
    assert outline[0] == {
        "symbol": "MainWindow",
        "kind": "class",
        "line_number": 3,
        "container": "DentalApp.UI",
        "is_partial": True,
    }
    assert len(partials) == 2
    assert {item["path"] for item in partials} == {"App/MainWindow.xaml.cs", "App/MainWindow.Actions.cs"}

def test_find_csharp_data_structures_and_members(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "PatientDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record PatientDto\n"
        "{\n"
        "    public string FullName { get; set; } = string.Empty;\n"
        "    public int Age;\n"
        "}\n\n"
        "public enum PatientStatus\n"
        "{\n"
        "    Active,\n"
        "    Archived\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    structures = store.find_data_structures(project_root, query="Patient", limit=20)

    kinds = {(item["symbol"], item["kind"]) for item in structures}
    assert ("PatientDto", "record") in kinds
    assert ("FullName", "property") in kinds
    assert ("Age", "field") in kinds
    assert ("PatientStatus", "enum") in kinds
    assert ("Active", "enum_member") in kinds
    assert ("Archived", "enum_member") in kinds

def test_get_symbol_snippet_and_partial_bundle(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "App" / "MainWindow.xaml.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void Initialize()\n"
        "    {\n"
        "        var ready = true;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "App" / "MainWindow.Actions.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void RunAction()\n"
        "    {\n"
        "        var executed = true;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    snippet = store.get_symbol_snippet(project_root, "App/MainWindow.xaml.cs", "MainWindow", kind="class")
    bundle = store.get_partial_bundle(project_root, "MainWindow")

    assert "public partial class MainWindow" in snippet["snippet"]
    assert len(bundle) == 2
    assert any("RunAction" in item["snippet"] for item in bundle)

def test_get_symbol_bundle_combines_definition_references_and_schema(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void Handle(QuoteDto dto) { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)

    bundle = store.get_symbol_bundle(project_root, "QuoteDto")

    assert bundle["symbol"] == "QuoteDto"
    assert bundle["definitions"]
    assert bundle["references"]
    assert any(item["entity_name"] == "QuoteDto" for item in bundle["schema_entities"])

def test_get_subsystem_bundle_combines_generic_analyzers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void QuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)

    bundle = store.get_subsystem_bundle(project_root, concept="Quote", limit=10)

    assert bundle["concept"] == "Quote"
    assert bundle["domain_cluster"]
    assert bundle["touchpoints"]
    assert bundle["data_structures"]

def test_dependency_edges_across_languages(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "mod.py").write_text(
        "import os\nfrom pkg.tools import helper\n",
        encoding="utf-8",
    )
    (project_root / "src" / "site.js").write_text(
        "import thing from './thing.js';\nconst lib = require('jquery');\n",
        encoding="utf-8",
    )
    (project_root / "src" / "App.cs").write_text(
        "using DentalApp.Models;\nnamespace DentalApp;\npublic class App {}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    py_deps = store.get_dependencies(project_root, "src/mod.py")
    js_deps = store.get_dependencies(project_root, "src/site.js")
    cs_deps = store.get_dependencies(project_root, "src/App.cs")
    dependents = store.find_dependents(project_root, "DentalApp.Models")

    assert {tuple(item.values()) for item in py_deps} == {("os", "import"), ("pkg.tools", "import")}
    assert {tuple(item.values()) for item in js_deps} == {("./thing.js", "import"), ("jquery", "require")}
    assert cs_deps == [{"target": "DentalApp.Models", "kind": "using"}]
    assert dependents == [{"path": "src/App.cs", "kind": "using"}]

def test_dependency_bundle_resolves_local_targets(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "dep.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (project_root / "src" / "main.ts").write_text(
        "import { x } from './dep';\nexport function run() { return x; }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    bundle = store.get_dependency_bundle(project_root, "src/main.ts")

    assert bundle["root"]["path"] == "src/main.ts"
    assert bundle["dependencies"][0]["target"] == "./dep"
    assert bundle["dependencies"][0]["resolved_paths"] == ["src/dep.ts"]
    assert bundle["dependencies"][0]["resolved_files"][0]["path"] == "src/dep.ts"

def test_trace_field_flow_ranks_cross_layer_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void ApplyPreferredDoctorId(QuoteDto dto) { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void SavePreferredDoctorId() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  const PreferredDoctorId = 'x';\n"
        "  return <input value={PreferredDoctorId} />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    flow = store.trace_field_flow(project_root, "PreferredDoctorId", limit=20)

    layers = [item["layer"] for item in flow["matches"]]
    assert flow["field"] == "PreferredDoctorId"
    assert flow["confidence"] in {"high", "medium", "low"}
    assert flow["why"]
    assert "data" in layers
    assert "logic" in layers
    assert "api" in layers
    assert "ui" in layers

def test_trace_field_flow_includes_schema_hits_when_present(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    result = store.trace_field_flow(project_root, "PreferredDoctorId", limit=20)

    assert any(item.get("source") == "schema" for item in result["matches"])

def test_trace_setting_usage_ranks_setting_related_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Config").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Config" / "QuoteSettings.cs").write_text(
        "public record QuoteSettings\n"
        "{\n"
        "    public bool EnableQuoteBuilder { get; set; }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteConfigService.cs").write_text(
        "public class QuoteConfigService\n"
        "{\n"
        "    public bool IsQuoteBuilderEnabled() => true;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void UpdateQuoteSettings() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteSettingsPanel.tsx").write_text(
        "export function QuoteSettingsPanel() {\n"
        "  const enableQuoteBuilder = true;\n"
        "  return <input checked={enableQuoteBuilder} />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_setting_usage(project_root, "QuoteBuilder", limit=20)
    layers = {item["layer"] for item in result["matches"]}

    assert result["setting"] == "QuoteBuilder"
    assert "data" in layers or "logic" in layers
    assert "api" in layers or "ui" in layers

def test_trace_service_usage_collects_definition_and_usage_sites(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    private readonly QuoteService _quoteService;\n"
        "    public QuoteController(QuoteService quoteService) { _quoteService = quoteService; }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  const label = 'QuoteService';\n"
        "  return <div>{label}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_service_usage(project_root, "QuoteService", limit=20)
    sources = {item["source"] for item in result["matches"]}
    paths = {item["path"] for item in result["matches"]}

    assert result["service"] == "QuoteService"
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert "definition" in sources
    assert "reference" in sources or "file_match" in sources
    assert "Services/QuoteService.cs" in paths

def test_trace_model_usage_combines_definitions_references_and_schema(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void Handle(QuoteDto dto) { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_model_usage(project_root, "QuoteDto", limit=20)

    assert result["model"] == "QuoteDto"
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert result["definitions"]
    assert result["references"]
    assert result["schema"]

def test_trace_component_usage_collects_definitions_references_and_neighbors(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "EditPanel.tsx").write_text(
        "import { Toolbar } from './Toolbar';\n"
        "export function EditPanel() {\n"
        "  return <Toolbar />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "Toolbar.tsx").write_text(
        "export function Toolbar() {\n"
        "  const child = 'EditPanel';\n"
        "  return <div>{child}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_component_usage(project_root, "EditPanel", limit=20)

    assert result["component"] == "EditPanel"
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert result["definitions"]
    assert result["references"]
    assert any(node["path"] == "web/components/Toolbar.tsx" for node in result["neighbors"])

def test_find_mutation_points_detects_update_save_style_flows(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void SaveQuote() { }\n"
        "    public void UpdateQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void SaveQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  function saveQuote() {}\n"
        "  return <button onClick={saveQuote}>Save</button>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_mutation_points(project_root, concept="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Services/QuoteService.cs" in paths
    assert "Controllers/QuoteController.cs" in paths or "Components/QuoteForm.tsx" in paths

def test_find_validation_surfaces_detects_validator_and_required_logic(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Validators").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Validators" / "QuoteValidator.cs").write_text(
        "public class QuoteValidator\n"
        "{\n"
        "    public bool ValidateQuote() => true;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void RequireQuoteName() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  const validationError = 'Quote required';\n"
        "  return <div>{validationError}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_validation_surfaces(project_root, concept="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Validators/QuoteValidator.cs" in paths or "Services/QuoteService.cs" in paths

def test_find_async_boundaries_detects_async_and_deferred_patterns(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Workers").mkdir(parents=True, exist_ok=True)
    (project_root / "web").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public async Task SaveQuoteAsync() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Workers" / "QueueWorker.cs").write_text(
        "public class QueueWorker\n"
        "{\n"
        "    public void ScheduleQuoteJob() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "quote.js").write_text(
        "function saveQuoteLater() {\n"
        "  setTimeout(() => console.log('later'), 1000);\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_async_boundaries(project_root, concept="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Services/QuoteService.cs" in paths or "Workers/QueueWorker.cs" in paths or "web/quote.js" in paths

def test_find_hotspots_scores_complex_service_file_higher(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Utils").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "BuilderService.cs").write_text(
        "public class BuilderService\n"
        "{\n"
        "    public void StartBuilder() { }\n"
        "    public void ValidateBuilder() { }\n"
        "    public void SaveBuilder() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Utils" / "small.py").write_text(
        "def ok():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_hotspots(project_root, query="Builder", limit=10)

    assert result["matches"]
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert result["matches"][0]["path"] == "Services/BuilderService.cs"
    assert result["matches"][0]["why"]

def test_find_query_hotspots_detects_include_join_heavy_file(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Utils").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QueryService.cs").write_text(
        "public class QueryService\n"
        "{\n"
        "    public void Load()\n"
        "    {\n"
        "        var q = ctx.Documents\n"
        "            .Include(x => x.Patient)\n"
        "            .ThenInclude(x => x.Clinic)\n"
        "            .Where(x => x.IsActive)\n"
        "            .Select(x => new { x.Id })\n"
        "            .ToListAsync();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Utils" / "small.cs").write_text(
        "public class Small { public void Ok() {} }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_query_hotspots(project_root, query=None, limit=10)

    assert result["matches"]
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert result["matches"][0]["path"] == "Services/QueryService.cs"
    assert any(reason.startswith("includes:") for reason in result["matches"][0]["why"])

def test_find_state_model_mismatch_detects_competing_representations(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "DocumentState.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public enum DocumentState\n"
        "{\n"
        "    Draft,\n"
        "    Signed\n"
        "}\n\n"
        "public record DocumentDto\n"
        "{\n"
        "    public bool IsSigned { get; set; }\n"
        "    public string Status { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "DocumentService.cs").write_text(
        "public class DocumentService\n"
        "{\n"
        "    public void SyncDocumentState() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "DocumentStatusBadge.tsx").write_text(
        "export function DocumentStatusBadge() {\n"
        "  const status = 'Draft';\n"
        "  return <span>{status}</span>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_state_model_mismatch(project_root, concept="DocumentState", limit=20)

    mismatch_types = {item["mismatch_type"] for item in result["matches"]}
    assert result["concept"] == "DocumentState"
    assert "enum_state_model" in mismatch_types
    assert "boolean_flag_model" in mismatch_types or "named_state_field" in mismatch_types

def test_find_ui_backend_touchpoints_collects_cross_layer_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void QuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  return <input name=\"PreferredDoctorId\" />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_ui_backend_touchpoints(project_root, concept="QuoteForm", limit=20)
    layers = {item["layer"] for item in result["matches"]}

    assert result["concept"] == "QuoteForm"
    assert "logic" in layers or "data" in layers
    assert "api" in layers
    assert "ui" in layers

def test_find_policy_surfaces_collects_guard_and_permission_layers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Policies").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Policies" / "QuotePolicy.cs").write_text(
        "public class QuotePolicy\n"
        "{\n"
        "    public bool CanEditQuote() => true;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuotePermissionService.cs").write_text(
        "public class QuotePermissionService\n"
        "{\n"
        "    public bool HasQuoteEditPermission() => true;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void EditQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteToolbar.tsx").write_text(
        "export function QuoteToolbar() {\n"
        "  const canEditQuote = true;\n"
        "  return <button disabled={!canEditQuote}>Edit</button>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_policy_surfaces(project_root, concept="Quote", limit=20)
    layers = {item["layer"] for item in result["matches"]}
    paths = {item["path"] for item in result["matches"]}

    assert result["concept"] == "Quote"
    assert "logic" in layers or "code" in layers
    assert "api" in layers
    assert "ui" in layers
    assert any("Policy" in path or "Permission" in path for path in paths)

def test_find_domain_clusters_combines_code_and_schema_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_domain_clusters(project_root, concept="Quote", limit=20)
    sources = {item["source"] for item in result["cluster"]}
    layers = {item["layer"] for item in result["cluster"]}

    assert result["concept"] == "Quote"
    assert "symbol" in sources or "file" in sources
    assert "data" in layers
    assert "logic" in layers or "ui" in layers

def test_find_transition_points_detects_adapter_and_legacy_patterns(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Compatibility").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "LegacyQuoteAdapter.cs").write_text(
        "public class LegacyQuoteAdapter\n"
        "{\n"
        "    public void MigrateQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Compatibility" / "QuoteCompatibilityService.cs").write_text(
        "public class QuoteCompatibilityService\n"
        "{\n"
        "    public void ApplyFallback() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_transition_points(project_root, concept="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Services/LegacyQuoteAdapter.cs" in paths or "Compatibility/QuoteCompatibilityService.cs" in paths

def test_find_entrypoints_detects_bootstrap_and_provider_like_symbols(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "App" / "bootstrap.ts").write_text(
        "export function bootstrapApp() {}\n"
        "export function registerPlugins() {}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "CmsProvider.tsx").write_text(
        "export function CmsProvider() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_entrypoints(project_root, concept="bootstrap", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "App/bootstrap.ts" in paths

def test_find_routes_detects_api_and_controller_surfaces(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "app" / "api" / "quotes").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void GetQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "app" / "api" / "quotes" / "route.ts").write_text(
        "export async function GET() {\n"
        "  return new Response('ok');\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_routes(project_root, query="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Controllers/QuoteController.cs" in paths or "web/app/api/quotes/route.ts" in paths

def test_trace_api_to_ui_collects_api_logic_and_ui_groups(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "app" / "api" / "quotes").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void GetQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "app" / "api" / "quotes" / "route.ts").write_text(
        "export async function GET() {\n"
        "  return new Response('ok');\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_api_to_ui(project_root, concept="Quote", limit=20)

    assert result["api"]
    assert result["logic"] or result["ui"]
    assert result["ui"]

def test_get_file_bundle_and_session_bundle(tmp_path: Path) -> None:
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
    (templates / "context.md").write_text(
        "# Context\n\n"
        "## Relevant Files\n"
        "- `src/app.py`\n",
        encoding="utf-8",
    )

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text(
        "class App:\n"
        "    pass\n\n"
        "def greet():\n"
        "    return 'hi'\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    session_store.context_file(project_root, "2026-03-23-test").write_text(
        "# Context\n\n"
        "## Relevant Files\n"
        "- `src/app.py`\n",
        encoding="utf-8",
    )

    store.sync_code_files(project_root)
    file_bundle = store.get_file_bundle(project_root, "src/app.py")
    session_bundle = store.get_session_code_bundle(project_root, "2026-03-23-test")

    assert file_bundle["path"] == "src/app.py"
    assert file_bundle["outline"][0]["symbol"] == "App"
    assert session_bundle["session_id"] == "2026-03-23-test"
    assert session_bundle["files"][0]["path"] == "src/app.py"

def test_get_component_bundle_includes_imported_frontend_neighbors(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "EditPanel.tsx").write_text(
        "import { CmsProvider } from './CmsProvider';\n"
        "import { useEditMode } from './useEditMode';\n"
        "export function EditPanel() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "CmsProvider.tsx").write_text(
        "export function CmsProvider() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "useEditMode.ts").write_text(
        "export function useEditMode() {\n"
        "  return { enabled: true };\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    bundle = store.get_component_bundle(project_root, "web/components/EditPanel.tsx")

    assert bundle["root"]["path"] == "web/components/EditPanel.tsx"
    assert any(item["symbol"] == "EditPanel" for item in bundle["frontend_symbols"])
    imported_paths = [item["file"]["path"] for item in bundle["imported_frontend_files"]]
    assert "web/components/CmsProvider.tsx" in imported_paths
    assert "web/components/useEditMode.ts" in imported_paths

def test_get_service_bundle_includes_related_backend_neighbors(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n"
        "using DentalApp.Controllers;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "namespace DentalApp.Controllers;\n\n"
        "public class QuoteController {}\n",
        encoding="utf-8",
    )
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "public record QuoteDto;\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    bundle = store.get_service_bundle(project_root, "Services/QuoteService.cs")

    assert bundle["root"]["path"] == "Services/QuoteService.cs"
    assert bundle["service_symbols"]
    related_paths = [item["file"]["path"] for item in bundle["local_related_files"]]
    assert "Controllers/QuoteController.cs" in related_paths

def test_get_query_bundle_includes_hotspot_and_schema_hints(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteQueryService.cs").write_text(
        "public class QuoteQueryService\n"
        "{\n"
        "    public void Load()\n"
        "    {\n"
        "        var q = ctx.Quotes\n"
        "            .Where(x => x.PreferredDoctorId != null)\n"
        "            .Select(x => x.PreferredDoctorId)\n"
        "            .ToListAsync();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    bundle = store.get_query_bundle(project_root, "Services/QuoteQueryService.cs")

    assert bundle["root"]["path"] == "Services/QuoteQueryService.cs"
    assert bundle["hotspot"] is not None
    assert bundle["schema_entities"]
    assert bundle["schema_fields"]

def test_trace_query_shape_includes_relationship_paths_when_schema_connects_entities(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteQueryService.cs").write_text(
        "public class QuoteQueryService\n"
        "{\n"
        "    public void Load()\n"
        "    {\n"
        "        var q = ctx.QuoteItems\n"
        "            .Where(x => x.ServiceId != null)\n"
        "            .Select(x => x.ServiceId)\n"
        "            .ToListAsync();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE QuoteItems (\n"
        "    Id INT,\n"
        "    ServiceId INT\n"
        ");\n"
        "CREATE TABLE Services (\n"
        "    Id INT\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    result = store.trace_query_shape(project_root, "Services/QuoteQueryService.cs")

    assert result["root"]["path"] == "Services/QuoteQueryService.cs"
    assert result["hotspot"] is not None
    assert result["schema_entities"]
    assert result["schema_fields"]

def test_get_component_tree_recursively_follows_local_frontend_imports(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "EditPanel.tsx").write_text(
        "import { CmsProvider } from './CmsProvider';\n"
        "import { Toolbar } from './Toolbar';\n"
        "export function EditPanel() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "CmsProvider.tsx").write_text(
        "export function CmsProvider() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "Toolbar.tsx").write_text(
        "import { IconButton } from './IconButton';\n"
        "export function Toolbar() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "IconButton.tsx").write_text(
        "export function IconButton() {\n"
        "  return <button />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    tree = store.get_component_tree(project_root, "web/components/EditPanel.tsx", depth=2, limit=20)

    node_paths = {node["path"] for node in tree["nodes"]}
    edge_pairs = {(edge["from"], edge["to"]) for edge in tree["edges"]}

    assert "web/components/EditPanel.tsx" in node_paths
    assert "web/components/CmsProvider.tsx" in node_paths
    assert "web/components/Toolbar.tsx" in node_paths
    assert "web/components/IconButton.tsx" in node_paths
    assert ("web/components/EditPanel.tsx", "web/components/CmsProvider.tsx") in edge_pairs
    assert ("web/components/Toolbar.tsx", "web/components/IconButton.tsx") in edge_pairs

def test_get_context_bundle_ranks_primary_and_dependency_items(tmp_path: Path) -> None:
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

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.ts").write_text(
        "import { x } from './dep';\nexport function run() { return x; }\n",
        encoding="utf-8",
    )
    (project_root / "src" / "dep.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    session_store.context_file(project_root, "2026-03-23-test").write_text(
        "# Context\n\n"
        "## Relevant Files\n"
        "- `src/main.ts`\n\n"
        "## Relevant Snippets\n"
        "```text\n"
        "run() depends on ./dep\n"
        "```\n",
        encoding="utf-8",
    )

    store.sync_code_files(project_root)
    bundle = store.get_context_bundle(project_root, "2026-03-23-test")

    assert bundle["primary_files"][0]["path"] == "src/main.ts"
    assert bundle["dependency_files"][0]["path"] == "src/dep.ts"
    assert bundle["ordered_items"][0]["kind"] == "primary_file"

def test_context_bundle_uses_plan_derived_code_targets(tmp_path: Path) -> None:
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

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.ts").write_text(
        "import { x } from './dep';\nexport function run() { return x; }\n",
        encoding="utf-8",
    )
    (project_root / "src" / "dep.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session = session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    plans_dir = session.path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "flow.md").write_text("Check `src/main.ts` and `src/dep.ts`.\n", encoding="utf-8")

    store.sync_code_files(project_root)
    bundle = store.get_context_bundle(project_root, "2026-03-23-test")

    assert [item["path"] for item in bundle["primary_files"]] == ["src/main.ts", "src/dep.ts"]

def test_context_bundle_skips_existing_but_unindexed_test_files(tmp_path: Path) -> None:
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

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "tests").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.ts").write_text("export function run() {}\n", encoding="utf-8")
    (project_root / "tests" / "main.test.ts").write_text("export function shouldSkip() {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session = session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    plans_dir = session.path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "flow.md").write_text("Check `src/main.ts` and `tests/main.test.ts`.\n", encoding="utf-8")

    store.sync_code_manifest(project_root, include_tests=False)
    store.sync_session_code(project_root, "2026-03-23-test", include_tests=False)
    bundle = store.get_context_bundle(project_root, "2026-03-23-test")

    assert [item["path"] for item in bundle["primary_files"]] == ["src/main.ts"]

def test_get_preset_bundle(tmp_path: Path) -> None:
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

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "App" / "MainWindow.xaml.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void Initialize() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "App" / "MainWindow.Actions.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void RunAction() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    bundle = store.get_preset_bundle(project_root, preset="csharp-partial", value="MainWindow")

    assert bundle["preset"] == "csharp-partial"
    assert bundle["value"] == "MainWindow"
    assert len(bundle["bundle"]) == 2
