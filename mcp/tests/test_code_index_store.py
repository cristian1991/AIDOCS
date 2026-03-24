from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore
from aidocs_mcp.session_store import SessionStore


def test_sync_code_files_indexes_supported_files(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    (project_root / "README.md").write_text("# Project\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)

    assert count == 1
    with store.connect(project_root) as conn:
        rows = conn.execute("SELECT path, language FROM code_files ORDER BY path").fetchall()
    assert [(r["path"], r["language"]) for r in rows] == [("src/app.py", "python")]


def test_sync_code_files_skips_nested_node_modules(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "src" / "app.ts").write_text("export const ok = true\n", encoding="utf-8")
    (project_root / "web" / "node_modules" / ".bin" / "acorn.ps1").write_text("Write-Host 'nope'\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)

    assert count == 1
    with store.connect(project_root) as conn:
        rows = conn.execute("SELECT path FROM code_files ORDER BY path").fetchall()
    assert [r["path"] for r in rows] == ["web/src/app.ts"]


def test_sync_code_files_skips_generated_website_and_temp_plugin_outputs(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "website" / "build").mkdir(parents=True, exist_ok=True)
    (project_root / "website" / ".docusaurus").mkdir(parents=True, exist_ok=True)
    (project_root / ".temp-plugins" / "plugin-sub_123").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "website" / "build" / "bundle.js").write_text("export const nope = true\n", encoding="utf-8")
    (project_root / "website" / ".docusaurus" / "registry.js").write_text("export const nope = true\n", encoding="utf-8")
    (project_root / ".temp-plugins" / "plugin-sub_123" / "generator.js").write_text("export async function generate() {}\n", encoding="utf-8")
    (project_root / "web" / "src" / "app.ts").write_text("export const ok = true\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)

    assert count == 1
    with store.connect(project_root) as conn:
        rows = conn.execute("SELECT path FROM code_files ORDER BY path").fetchall()
    assert [r["path"] for r in rows] == ["web/src/app.ts"]


def test_sync_code_files_skips_obj_backup_and_temp_outputs(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src" / "App" / "obj" / "Debug").mkdir(parents=True, exist_ok=True)
    (project_root / ".BACKUP").mkdir(parents=True, exist_ok=True)
    (project_root / "temp").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "App" / "Service.cs").write_text("public class Service {}\n", encoding="utf-8")
    (project_root / "src" / "App" / "obj" / "Debug" / "Gen.cs").write_text("public class Gen {}\n", encoding="utf-8")
    (project_root / ".BACKUP" / "Old.cs").write_text("public class Old {}\n", encoding="utf-8")
    (project_root / "temp" / "Tmp.cs").write_text("public class Tmp {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)

    assert count == 1
    with store.connect(project_root) as conn:
        rows = conn.execute("SELECT path FROM code_files ORDER BY path").fetchall()
    assert [r["path"] for r in rows] == ["src/App/Service.cs"]


def test_code_status_and_search(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "server.ts").write_text("export function startServer() {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status = store.code_status(project_root)
    results = store.search_code(project_root, "server", limit=5)

    assert status["code_files"] == 1
    assert status["parsed_code_files"] == 1
    assert status["code_outlines"] == 1
    assert status["roles"]["unknown"] == 1
    assert status["role_groups"]["unknown"] == 1
    assert results[0]["path"] == "src/server.ts"
    assert results[0]["role"] == "unknown"


def test_get_outline_for_python_and_typescript(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text(
        "class App:\n"
        "    pass\n\n"
        "def greet():\n"
        "    return 'hi'\n",
        encoding="utf-8",
    )
    (project_root / "src" / "server.ts").write_text(
        "export function startServer() {}\n"
        "const helper = () => {}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    py_outline = store.get_outline(project_root, "src/app.py")
    ts_outline = store.get_outline(project_root, "src/server.ts")

    assert py_outline == [
        {"symbol": "App", "kind": "class", "line_number": 1, "container": None, "is_partial": False},
        {"symbol": "greet", "kind": "function", "line_number": 4, "container": None, "is_partial": False},
    ]
    assert ts_outline == [
        {"symbol": "startServer", "kind": "function", "line_number": 1, "container": None, "is_partial": False},
        {"symbol": "helper", "kind": "function", "line_number": 2, "container": None, "is_partial": False},
    ]


def test_typescript_outline_and_data_structures_include_contract_types(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "contracts.ts").write_text(
        "export interface WorkflowRunInput {\n"
        "  runtime: 'codex' | 'opencode'\n"
        "  model?: string\n"
        "}\n\n"
        "export type WorkflowRunResponse = {\n"
        "  output: string\n"
        "}\n\n"
        "export enum RuntimeMode {\n"
        "  Codex,\n"
        "  OpenCode\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    outline = store.get_outline(project_root, "src/contracts.ts")
    structures = store.find_data_structures(project_root, query="Workflow", limit=20)
    symbol_bundle = store.get_symbol_bundle(project_root, symbol="WorkflowRunInput", path="src/contracts.ts", limit=20)

    outline_kinds = {(item["symbol"], item["kind"], item["container"]) for item in outline}
    structure_kinds = {(item["symbol"], item["kind"], item["container"]) for item in structures}

    assert ("WorkflowRunInput", "interface", None) in outline_kinds
    assert ("runtime", "property", "WorkflowRunInput") in outline_kinds
    assert ("model", "property", "WorkflowRunInput") in outline_kinds
    assert ("WorkflowRunResponse", "type_alias", None) in outline_kinds
    assert ("output", "property", "WorkflowRunResponse") in outline_kinds
    assert ("RuntimeMode", "enum", None) in outline_kinds
    assert ("Codex", "enum_member", "RuntimeMode") in outline_kinds
    assert ("OpenCode", "enum_member", "RuntimeMode") in outline_kinds

    assert ("WorkflowRunInput", "interface", None) in structure_kinds
    assert ("runtime", "property", "WorkflowRunInput") in structure_kinds
    assert symbol_bundle["definitions"]


def test_python_ast_outline_handles_decorators_async_and_methods(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "advanced.py").write_text(
        "from pkg.tools import helper\n\n"
        "@decorator\n"
        "class AppService:\n"
        "    @classmethod\n"
        "    def build(cls):\n"
        "        return cls()\n\n"
        "async def useRunner():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    outline = store.get_outline(project_root, "src/advanced.py")
    deps = store.get_dependencies(project_root, "src/advanced.py")

    assert {item["symbol"] for item in outline} == {"AppService", "build", "useRunner"}
    assert any(item["kind"] == "method" and item["container"] == "AppService" for item in outline)
    assert deps == [{"target": "pkg.tools", "kind": "import"}]


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


def test_find_js_initializers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "wwwroot" / "js").mkdir(parents=True, exist_ok=True)
    (project_root / "wwwroot" / "js" / "site.js").write_text(
        "document.addEventListener('DOMContentLoaded', function() {\n"
        "  initSelect2();\n"
        "});\n"
        "$(document).ready(function() {\n"
        "  initDataTable();\n"
        "});\n"
        "window.addEventListener('load', () => initTheme());\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    initializers = store.find_initializers(project_root, path="wwwroot/js/site.js")

    assert [item["symbol"] for item in initializers] == [
        "document:DOMContentLoaded",
        "jquery:ready",
        "window:load",
    ]


def test_search_symbols_across_languages(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text(
        "class App:\n"
        "    pass\n\n"
        "def greet():\n"
        "    return 'hi'\n",
        encoding="utf-8",
    )
    (project_root / "src" / "site.js").write_text(
        "function initSelect2() {}\n"
        "document.addEventListener('DOMContentLoaded', function() {});\n",
        encoding="utf-8",
    )
    (project_root / "src" / "PatientDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record PatientDto\n"
        "{\n"
        "    public string FullName { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    init_results = store.search_symbols(project_root, "init", limit=20)
    init_symbols = {(item["symbol"], item["kind"]) for item in init_results}
    dom_results = store.search_symbols(project_root, "DOMContentLoaded", limit=20)
    dom_symbols = {(item["symbol"], item["kind"]) for item in dom_results}

    assert ("initSelect2", "function") in init_symbols
    assert ("document:DOMContentLoaded", "initializer") in dom_symbols


def test_search_symbols_uses_concept_variants_for_common_suffixes(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "public record QuoteDto;\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    results = store.search_symbols(project_root, "Quote", limit=10)

    assert any(item["symbol"] == "QuoteDto" for item in results)


def test_find_references_returns_line_level_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text(
        "class QuoteDto:\n"
        "    pass\n\n"
        "def use_quote(dto: QuoteDto):\n"
        "    return dto\n",
        encoding="utf-8",
    )
    (project_root / "src" / "QuotePanel.tsx").write_text(
        "export function QuotePanel() {\n"
        "  const label = 'QuoteDto';\n"
        "  return <div>{label}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    refs = store.find_references(project_root, "QuoteDto", limit=20)

    assert refs["symbol"] == "QuoteDto"
    assert len(refs["matches"]) >= 2
    assert any(match["path"] == "src/app.py" for match in refs["matches"])
    assert any(match["path"] == "src/QuotePanel.tsx" for match in refs["matches"])


def test_find_frontend_symbols_for_components_hooks_and_providers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "EditModeProvider.tsx").write_text(
        "export function EditModeProvider({ children }: { children: React.ReactNode }) {\n"
        "  return <div>{children}</div>;\n"
        "}\n\n"
        "export function useEditMode() {\n"
        "  return { enabled: true };\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "Header.tsx").write_text(
        "export function Header() {\n"
        "  return <header />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "boot.ts").write_text(
        "document.addEventListener('DOMContentLoaded', function() {\n"
        "  initBuilder();\n"
        "});\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    symbols = store.find_frontend_symbols(project_root, query="Mode", limit=20)
    kinds = {(item["symbol"], item["kind"]) for item in symbols}
    all_frontend = store.find_frontend_symbols(project_root, query=None, limit=20)
    all_kinds = {(item["symbol"], item["kind"]) for item in all_frontend}

    assert ("EditModeProvider", "context_provider") in all_kinds
    assert ("useEditMode", "hook") in all_kinds
    assert ("Header", "component") in all_kinds
    assert ("document:DOMContentLoaded", "initializer") in all_kinds
    assert ("EditModeProvider", "context_provider") in kinds
    assert all_frontend[0]["why"]


def test_frontend_ast_extractor_falls_back_cleanly_when_parser_unavailable(tmp_path: Path) -> None:
    store = CodeIndexStore()
    store.frontend_ast._support.available = False
    project_root = tmp_path / "project"
    (project_root / "web").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "Header.tsx").write_text(
        "export function Header() {\n"
        "  return <header />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    outline = store.get_outline(project_root, "web/Header.tsx")

    assert outline[0]["symbol"] == "Header"
    assert outline[0]["kind"] == "component"


def test_code_role_inference_for_frontend_and_backend_files(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "app" / "dashboard").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "app" / "dashboard" / "page.tsx").write_text(
        "export default function DashboardPage() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "EditModeProvider.tsx").write_text(
        "export function EditModeProvider() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService {}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "namespace DentalApp.Controllers;\n\n"
        "public class QuoteController {}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    page = store.get_file_bundle(project_root, "web/app/dashboard/page.tsx")
    provider = store.get_file_bundle(project_root, "web/components/EditModeProvider.tsx")
    service = store.get_file_bundle(project_root, "Services/QuoteService.cs")
    controller = store.get_file_bundle(project_root, "Controllers/QuoteController.cs")

    assert page["role"] == "page"
    assert provider["role"] == "context-provider"
    assert service["role"] == "service"
    assert controller["role"] == "controller"


def test_code_role_inference_expands_support_and_runtime_buckets(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "app" / "api" / "quotes").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "lib").mkdir(parents=True, exist_ok=True)
    (project_root / "server").mkdir(parents=True, exist_ok=True)
    (project_root / "Pages").mkdir(parents=True, exist_ok=True)
    (project_root / "Configurations").mkdir(parents=True, exist_ok=True)
    (project_root / "Workers").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "app" / "api" / "quotes" / "route.ts").write_text("export async function GET() { return Response.json({}); }\n", encoding="utf-8")
    (project_root / "web" / "lib" / "helpers.ts").write_text("export function slugify(x: string) { return x; }\n", encoding="utf-8")
    (project_root / "server" / "plugin-server.js").write_text("export function start() {}\n", encoding="utf-8")
    (project_root / "Pages" / "AccessDenied.cshtml.cs").write_text("public class AccessDeniedModel {}\n", encoding="utf-8")
    (project_root / "Configurations" / "AccountConfiguration.cs").write_text("public class AccountConfiguration {}\n", encoding="utf-8")
    (project_root / "Workers" / "QueueWorker.cs").write_text("public class QueueWorker {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["route-handler"] == 1
    assert status["roles"]["utility-module"] == 1
    assert status["roles"]["server-module"] == 1
    assert status["roles"]["page-model"] == 1
    assert status["roles"]["configuration"] == 1
    assert status["roles"]["worker"] == 1
    assert status["role_groups"]["request-surfaces"] >= 2
    assert status["role_groups"]["support"] >= 2
    assert status["role_groups"]["logic-runtime"] >= 1


def test_code_role_inference_classifies_components_and_plugin_modules(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "random-folder-name" / "demo-plugin" / "templates" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "framework-generators").mkdir(parents=True, exist_ok=True)
    (project_root / "core").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "Hero.tsx").write_text("export function Hero() { return <div />; }\n", encoding="utf-8")
    (project_root / "random-folder-name" / "demo-plugin" / "package.json").write_text("{}\n", encoding="utf-8")
    (project_root / "random-folder-name" / "demo-plugin" / "generator.js").write_text("export async function generate() {}\n", encoding="utf-8")
    (project_root / "random-folder-name" / "demo-plugin" / "index.ts").write_text("export * from './generator'\n", encoding="utf-8")
    (project_root / "framework-generators" / "next-generator.js").write_text("export function create() {}\n", encoding="utf-8")
    (project_root / "core" / "plugin-loader.js").write_text("export function load() {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["component"] == 1
    assert status["roles"]["plugin-generator"] == 1
    assert status["roles"]["plugin-module"] == 1
    assert status["roles"]["framework-generator"] == 1
    assert status["roles"]["core-module"] == 1


def test_code_role_inference_generalizes_hooks_barrels_and_root_scripts(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "any-name" / "feature-pack" / "templates" / "hooks").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "cms").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "any-name" / "feature-pack" / "package.json").write_text("{}\n", encoding="utf-8")
    (project_root / "any-name" / "feature-pack" / "templates" / "hooks" / "useThing.ts").write_text("export function useThing() { return null }\n", encoding="utf-8")
    (project_root / "web" / "components" / "cms" / "index.ts").write_text("export * from './EditPanel'\n", encoding="utf-8")
    (project_root / "bootstrap.js").write_text("#!/usr/bin/env node\nconsole.log('ok')\n", encoding="utf-8")
    (project_root / "next-env.d.ts").write_text("/// <reference types=\"next\" />\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["hook-module"] == 1
    assert status["roles"]["barrel-module"] == 1
    assert status["roles"]["config-module"] == 1


def test_code_role_inference_classifies_plugin_template_modules_generically(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "some-random-folder" / "auth-pack" / "templates").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "some-random-folder" / "auth-pack" / "package.json").write_text("{}\n", encoding="utf-8")
    (project_root / "some-random-folder" / "auth-pack" / "templates" / "prisma-schema.js").write_text("export function getSchema() { return '' }\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["plugin-template-module"] == 1


def test_code_role_inference_classifies_csharp_entities_dtos_and_interfaces(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src" / "Domain" / "Entities").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Application" / "DTOs").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Application" / "Interfaces").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Domain" / "Entities" / "Account.cs").write_text("public class Account {}\n", encoding="utf-8")
    (project_root / "src" / "Application" / "DTOs" / "AccountDto.cs").write_text("public record AccountDto(string Name);\n", encoding="utf-8")
    (project_root / "src" / "Application" / "Interfaces" / "IAccountService.cs").write_text("public interface IAccountService {}\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["data-model"] == 2
    assert status["roles"]["abstraction"] == 1


def test_code_role_inference_classifies_csharp_program_seed_partial_services_and_hubs(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src" / "Infrastructure" / "Data" / "Seeding").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Infrastructure" / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Infrastructure" / "Data").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "Hubs").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "ViewComponents").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Infrastructure" / "Data" / "AppDbContext.cs").write_text("public class AppDbContext {}\n", encoding="utf-8")
    (project_root / "src" / "Infrastructure" / "Data" / "Seeding" / "SeedDataService.Forms.cs").write_text("public partial class SeedDataService {}\n", encoding="utf-8")
    (project_root / "src" / "Infrastructure" / "Services" / "FormPdfService.Html.cs").write_text("public partial class FormPdfService {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "Hubs" / "DebugHub.cs").write_text("public class DebugHub {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "ViewComponents" / "SidebarViewComponent.cs").write_text("public class SidebarViewComponent {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "Program.cs").write_text("public class Program {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "Scripts").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "Scripts" / "SyncTranslations.ps1").write_text("Write-Host 'ok'\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["data-access"] == 1
    assert status["roles"]["script"] >= 2
    assert status["roles"]["service"] == 1
    assert status["roles"]["hub"] == 1
    assert status["roles"]["component"] == 1
    assert status["roles"]["initializer-module"] == 1


def test_code_role_inference_classifies_asset_scripts_tools_and_support_markers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src" / "Web" / "wwwroot" / "js").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Infrastructure").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "Infrastructure").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web").mkdir(parents=True, exist_ok=True)
    (project_root / "tools").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "wwwroot" / "js" / "site.js").write_text("const x = 1\n", encoding="utf-8")
    (project_root / "src" / "Infrastructure" / "DependencyInjection.cs").write_text("public static class DependencyInjection {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "Infrastructure" / "DateTimeModelBinder.cs").write_text("public class DateTimeModelBinder {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "SharedResources.cs").write_text("public class SharedResources {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "NavigationMap.cs").write_text("public class NavigationMap {}\n", encoding="utf-8")
    (project_root / "tools" / "fix-untranslated.py").write_text("print('ok')\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["asset-script"] == 1
    assert status["roles"]["initializer-module"] == 1
    assert status["roles"]["configuration"] == 1
    assert status["roles"]["utility"] == 2
    assert status["roles"]["script"] == 1


def test_search_code_and_symbols_rank_exact_matches_first(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "server.ts").write_text("export function startServer() {}\n", encoding="utf-8")
    (project_root / "src" / "helpers.ts").write_text("export function serverHelper() {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    code_results = store.search_code(project_root, "server", limit=10)
    symbol_results = store.search_symbols(project_root, "startServer", limit=10)

    assert code_results[0]["path"] == "src/server.ts"
    assert code_results[0]["why"]
    assert symbol_results[0]["symbol"] == "startServer"
    assert symbol_results[0]["why"]


def test_path_weighting_prefers_source_over_template_like_paths(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "templates").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "SeoMetadataEditor.tsx").write_text(
        "export function SeoMetadataEditor() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / "templates" / "SeoMetadataEditor.tsx").write_text(
        "export function SeoMetadataEditor() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    results = store.find_frontend_symbols(project_root, query="SeoMetadataEditor", limit=10)

    assert results[0]["path"] == "src/SeoMetadataEditor.tsx"


def test_memory_guided_preferred_root_beats_noisy_match(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY" / "config").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "config" / "indexing.md").write_text(
        "# Indexing\n\n"
        "## Preferred Roots\n"
        "- `web/components/`\n\n"
        "## Avoid Roots\n"
        "- `testplugins/`\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "TESTPLUGINS" / "seo" / "templates" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "SeoMetadataEditor.tsx").write_text(
        "export function SeoMetadataEditor() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / "TESTPLUGINS" / "seo" / "templates" / "components" / "SeoMetadataEditor.tsx").write_text(
        "export function SeoMetadataEditor() { return <div />; }\n",
        encoding="utf-8",
    )

    store.sync_code_files(project_root)
    results = store.find_frontend_symbols(project_root, query="SeoMetadataEditor", limit=10)

    assert results[0]["path"] == "web/components/SeoMetadataEditor.tsx"


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


def test_sync_session_code_indexes_only_relevant_files(tmp_path: Path) -> None:
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
    (project_root / "src" / "keep.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    (project_root / "src" / "skip.py").write_text("def skip():\n    return 2\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    session_store.context_file(project_root, "2026-03-23-test").write_text(
        "# Context\n\n## Relevant Files\n- `src/keep.py`\n", encoding="utf-8"
    )

    count = store.sync_session_code(project_root, "2026-03-23-test")
    assert count == 1
    with store.connect(project_root) as conn:
        rows = conn.execute("SELECT path, parsed FROM code_files ORDER BY path").fetchall()
    assert [(row["path"], row["parsed"]) for row in rows] == [("src/keep.py", 1), ("src/skip.py", 0)]


def test_incremental_sync_preserves_unchanged_file_and_updates_changed_one(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    keep = project_root / "src" / "keep.py"
    change = project_root / "src" / "change.py"
    keep.write_text("def keep():\n    return 1\n", encoding="utf-8")
    change.write_text("def before():\n    return 1\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    before_status = store.code_status(project_root)
    change.write_text("def after():\n    return 2\n", encoding="utf-8")
    store.sync_code_files(project_root)
    after_status = store.code_status(project_root)
    outline = store.get_outline(project_root, "src/change.py")

    assert before_status["code_files"] == 2
    assert before_status["parsed_code_files"] == 2
    assert after_status["code_files"] == 2
    assert after_status["parsed_code_files"] == 2
    assert outline[0]["symbol"] == "after"


def test_manifest_sync_discovers_files_before_deep_parse(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "server.ts").write_text("export function startServer() {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_manifest(project_root)
    status = store.code_status(project_root)

    assert count == 1
    assert status["code_files"] == 1
    assert status["parsed_code_files"] == 0


def test_manifest_sync_infers_roles_from_path_even_before_parse(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers" / "QuoteController.cs").write_text("public class QuoteController {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_manifest(project_root)
    status = store.code_status(project_root)
    results = store.search_code(project_root, "QuoteController", limit=5)

    assert status["roles"]["controller"] == 1
    assert results[0]["role"] == "controller"


def test_code_index_version_invalidation_resets_stale_rows(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status_before = store.code_status(project_root)
    with store.connect(project_root) as conn:
        conn.execute("UPDATE index_meta SET value = 'stale-version' WHERE key = 'code_index_version'")
    store.sync_code_manifest(project_root)
    status_after = store.code_status(project_root)

    assert status_before["parsed_code_files"] == 1
    assert status_after["parsed_code_files"] == 0


def test_lazy_parse_on_symbol_query_uses_manifest_candidates(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "EditPanel.tsx").write_text(
        "export function EditPanel() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_manifest(project_root, include_tests=False)
    before = store.code_status(project_root)
    symbols = store.find_frontend_symbols(project_root, query="Edit", limit=10)
    after = store.code_status(project_root)

    assert before["parsed_code_files"] == 0
    assert symbols[0]["symbol"] == "EditPanel"
    assert after["parsed_code_files"] == 1


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


# ---- New: Razor/Resx/CSS extraction tests ----


def test_razor_cshtml_parsed_with_outline(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Pages").mkdir(parents=True, exist_ok=True)
    (project_root / "Pages" / "Index.cshtml").write_text(
        '@page\n@model IndexModel\n@inject IService Svc\n<partial name="_Header" />\n'
        '@section Scripts {\n<script>\nfunction init() {}\n</script>\n}\n'
        '@Lang.T("Hello")\n<form asp-page-handler="Save"></form>\n',
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    outline = store.get_outline(project_root, "Pages/Index.cshtml")

    kinds = {item["kind"] for item in outline}
    assert "page_route" in kinds
    assert "model_binding" in kinds
    assert "inject" in kinds
    assert "partial_ref" in kinds
    assert "section" in kinds
    assert "js_function" in kinds
    assert "translation_key" in kinds
    assert "form_handler" in kinds


def test_razor_cshtml_role_inference(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Pages").mkdir(parents=True, exist_ok=True)
    (project_root / "Pages" / "Shared").mkdir(parents=True, exist_ok=True)
    (project_root / "Pages" / "Index.cshtml").write_text("@page\n<h1>Home</h1>\n", encoding="utf-8")
    (project_root / "Pages" / "_Header.cshtml").write_text("<header>Logo</header>\n", encoding="utf-8")
    (project_root / "Pages" / "Shared" / "_Layout.cshtml").write_text('<html>\n@RenderBody()\n</html>\n', encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        roles = {r["path"]: r["role"] for r in conn.execute("SELECT path, role FROM code_files")}

    assert roles["Pages/Index.cshtml"] == "page-view"
    assert roles["Pages/_Header.cshtml"] == "partial-view"
    assert roles["Pages/Shared/_Layout.cshtml"] == "layout"


def test_resx_translation_extraction(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Resources").mkdir(parents=True, exist_ok=True)
    (project_root / "Resources" / "Lang.en.resx").write_text(
        '<?xml version="1.0"?>\n<root>\n'
        '  <data name="Hello" xml:space="preserve"><value>Hello World</value></data>\n'
        '  <data name="Save" xml:space="preserve"><value>Save Changes</value></data>\n'
        '</root>\n',
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    outline = store.get_outline(project_root, "Resources/Lang.en.resx")

    symbols = {item["symbol"] for item in outline}
    assert "Hello" in symbols
    assert "Save" in symbols
    assert all(item["kind"] == "translation" for item in outline)


def test_css_class_and_variable_extraction(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "css").mkdir(parents=True, exist_ok=True)
    (project_root / "css" / "app.css").write_text(
        '@theme {\n  --color-primary: #3b82f6;\n  --app-bg: #fff;\n}\n'
        '.modal-overlay { display: flex; }\n'
        '.glassmorphic { backdrop-filter: blur(10px); }\n'
        '@keyframes fadeIn { from { opacity: 0; } }\n',
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    outline = store.get_outline(project_root, "css/app.css")

    kinds = {item["kind"] for item in outline}
    symbols = {item["symbol"] for item in outline}
    assert "css_class" in kinds
    assert "css_variable" in kinds
    assert "keyframes" in kinds
    assert "theme_block" in kinds
    assert "modal-overlay" in symbols
    assert "--color-primary" in symbols
    assert "fadeIn" in symbols


def test_csharp_http_endpoint_extraction(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers" / "ApiController.cs").write_text(
        'using Microsoft.AspNetCore.Mvc;\n'
        'namespace App.Controllers;\n'
        '[Route("api/[controller]")]\n'
        'public class ApiController : ControllerBase\n{\n'
        '    [HttpGet("items")]\n'
        '    public IActionResult GetItems() { return Ok(); }\n'
        '    [HttpPost]\n'
        '    [Authorize(Roles="Admin")]\n'
        '    public IActionResult Create() { return Ok(); }\n'
        '}\n',
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    outline = store.get_outline(project_root, "Controllers/ApiController.cs")

    kinds = {item["kind"] for item in outline}
    assert "http_endpoint" in kinds
    assert "route" in kinds
    assert "authorize" in kinds


def test_search_symbols_by_kind(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Pages").mkdir(parents=True, exist_ok=True)
    (project_root / "Pages" / "Index.cshtml").write_text(
        '@page\n@model IndexModel\n@Lang.T("Welcome")\n@Lang.T("Login")\n',
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    results = store.search_symbols(project_root, query="", kind="translation_key")

    assert len(results) >= 2
    assert all(r["kind"] == "translation_key" for r in results)


def test_incremental_index_version_migration(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("def hello(): pass\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    # First sync
    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        files_before = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
        outlines_before = conn.execute("SELECT COUNT(*) FROM code_outlines").fetchone()[0]

    assert files_before == 1
    assert outlines_before >= 1

    # Simulate version bump
    with store.connect(project_root) as conn:
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('code_index_version', 'old-version')")

    # Re-init triggers version migration
    store.init_db(project_root)
    with store.connect(project_root) as conn:
        # Files should still be there (not deleted), but marked unparsed
        files_after = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
        parsed_after = conn.execute("SELECT COUNT(*) FROM code_files WHERE parsed = 1").fetchone()[0]
        outlines_after = conn.execute("SELECT COUNT(*) FROM code_outlines").fetchone()[0]

    assert files_after == files_before  # files preserved
    assert parsed_after == 0  # all marked unparsed
    assert outlines_after == 0  # outlines cleared for reparse
