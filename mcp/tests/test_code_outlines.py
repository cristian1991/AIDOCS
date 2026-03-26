from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore


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
        {"symbol": "App", "kind": "class", "line_number": 1},
        {"symbol": "greet", "kind": "function", "line_number": 4},
    ]
    assert ts_outline == [
        {"symbol": "startServer", "kind": "function", "line_number": 1},
        {"symbol": "helper", "kind": "function", "line_number": 2},
    ]

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


# ── Language mapping tests ───────────────────────────────────────────


def test_language_mapping_covers_new_extensions(tmp_path: Path) -> None:
    """All new language extensions map to the correct language string."""
    store = CodeIndexStore()
    from pathlib import PurePosixPath as P
    mapping = {
        "main.rs": "rust", "main.go": "go", "App.java": "java",
        "App.kt": "kotlin", "build.gradle.kts": "kotlin",
        "app.rb": "ruby", "index.php": "php",
        "app.ex": "elixir", "test.exs": "elixir",
        "query.sql": "sql", "page.html": "html", "page.htm": "html",
        "style.scss": "scss", "style.sass": "sass", "style.less": "less",
        "App.vue": "vue", "App.svelte": "svelte",
        "schema.prisma": "prisma", "config.toml": "toml",
        "config.yaml": "yaml", "config.yml": "yaml",
        "data.json": "json",
    }
    for filename, expected_lang in mapping.items():
        from pathlib import Path as P2
        result = store._language_for(P2(filename))
        assert result == expected_lang, f"{filename} -> expected {expected_lang}, got {result}"


# ── New language outline extraction tests ────────────────────────────


def test_rust_outline_extracts_structs_enums_traits_fns(tmp_path: Path) -> None:
    """Rust outline extraction handles struct, enum, trait, fn, impl."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "lib.rs").write_text(
        "pub struct Vehicle {\n    pub id: u32,\n}\n\n"
        "pub enum Direction {\n    North,\n    South,\n}\n\n"
        "pub trait Movable {\n    fn move_to(&self);\n}\n\n"
        "pub async fn start_engine(v: &Vehicle) {}\n\n"
        "impl Movable for Vehicle {\n    fn move_to(&self) {}\n}\n\n"
        "mod utils;\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        outlines = conn.execute(
            "SELECT symbol, kind FROM code_outlines WHERE path = 'src/lib.rs' ORDER BY line_number"
        ).fetchall()

    symbols_by_kind = {}
    for r in outlines:
        symbols_by_kind.setdefault(r["kind"], set()).add(r["symbol"])
    assert "Vehicle" in symbols_by_kind.get("struct", set())
    assert "Direction" in symbols_by_kind.get("enum", set())
    assert "Movable" in symbols_by_kind.get("trait", set())
    assert "start_engine" in symbols_by_kind.get("function", set())
    assert "utils" in symbols_by_kind.get("module", set())
    assert "impl" in symbols_by_kind or any(r["kind"] == "impl" for r in outlines)


def test_go_outline_extracts_structs_interfaces_funcs(tmp_path: Path) -> None:
    """Go outline extraction handles type struct, interface, func."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.go").write_text(
        "package main\n\n"
        "type Server struct {\n    Port int\n}\n\n"
        "type Handler interface {\n    ServeHTTP()\n}\n\n"
        "func NewServer(port int) *Server {\n    return &Server{Port: port}\n}\n\n"
        "func (s *Server) Start() {}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        outlines = conn.execute(
            "SELECT symbol, kind FROM code_outlines WHERE path = 'src/main.go' ORDER BY line_number"
        ).fetchall()

    kinds = {r["symbol"]: r["kind"] for r in outlines}
    assert kinds["Server"] == "struct"
    assert kinds["Handler"] == "interface"
    assert kinds["NewServer"] == "function"
    assert kinds["Start"] == "function"


def test_java_outline_extracts_classes_interfaces_methods(tmp_path: Path) -> None:
    """Java outline extraction handles class, interface, enum, method."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "App.java").write_text(
        "public class UserService {\n"
        "    public User findById(long id) { return null; }\n"
        "    private void validate(User u) {}\n"
        "}\n\n"
        "public interface Repository {\n"
        "    User save(User u);\n"
        "}\n\n"
        "public enum Status {\n    ACTIVE, INACTIVE\n}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        outlines = conn.execute(
            "SELECT symbol, kind FROM code_outlines WHERE path = 'src/App.java' ORDER BY line_number"
        ).fetchall()

    kinds = {r["symbol"]: r["kind"] for r in outlines}
    assert kinds["UserService"] == "class"
    assert kinds["findById"] == "method"
    assert kinds["Repository"] == "interface"
    assert kinds["Status"] == "enum"


def test_sql_outline_extracts_tables_views_functions(tmp_path: Path) -> None:
    """SQL outline extraction handles CREATE TABLE, VIEW, FUNCTION."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "db").mkdir(parents=True, exist_ok=True)
    (project_root / "db" / "schema.sql").write_text(
        'CREATE TABLE "Users" (\n    "Id" INT PRIMARY KEY,\n    "Name" TEXT\n);\n\n'
        "CREATE OR REPLACE VIEW ActiveUsers AS\n    SELECT * FROM Users WHERE active = true;\n\n"
        "CREATE FUNCTION get_user_count() RETURNS INT AS $$\n    SELECT COUNT(*) FROM Users;\n$$ LANGUAGE sql;\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        outlines = conn.execute(
            "SELECT symbol, kind FROM code_outlines WHERE path = 'db/schema.sql' ORDER BY line_number"
        ).fetchall()

    kinds = {r["symbol"]: r["kind"] for r in outlines}
    assert kinds["Users"] == "table"
    assert kinds["ActiveUsers"] == "view"
    assert kinds["get_user_count"] == "function"


# ── CamelCase / multi-word symbol search tests ──────────────────────


def test_search_symbols_camelcase_to_words(tmp_path: Path) -> None:
    """Searching CamelCase name generates word variants that find it."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "service.cs").write_text(
        "public class FormPdfService {\n"
        "    public void GeneratePdfAsync() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    results = store.search_symbols(project_root, "FormPdfService", limit=5)
    symbols = [r["symbol"] for r in results]
    assert "FormPdfService" in symbols


def test_search_symbols_words_to_camelcase(tmp_path: Path) -> None:
    """Space-separated words generate CamelCase join that finds the symbol."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "service.cs").write_text(
        "public class FormPdfService {\n"
        "    public void GeneratePdfAsync() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    results = store.search_symbols(project_root, "form pdf service", limit=5)
    symbols = [r["symbol"] for r in results]
    assert "FormPdfService" in symbols


def test_search_symbols_prefix_match_ranked_first(tmp_path: Path) -> None:
    """Prefix matches rank higher than substring matches."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "pdf.cs").write_text(
        "public interface IFormPdfService {}\n"
        "public class GeneratePdfAsync {}\n"
        "public class GeneratePdfWithLayoutAsync {}\n"
        "public class UnrelatedPdfThing {}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    results = store.search_symbols(project_root, "GeneratePdf", limit=5)
    # GeneratePdf* should rank before IFormPdfService/UnrelatedPdfThing
    assert results[0]["symbol"].startswith("GeneratePdf")
