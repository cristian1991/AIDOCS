from pathlib import Path
import json

from aidocs_mcp.code_index_store import CodeIndexStore


# ── Module detection tests ───────────────────────────────────────────



def test_detect_modules_informal_with_entry_points(tmp_path: Path) -> None:
    """Top-level dirs with entry points are detected as informal modules."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    # cli/ with index.js entry point
    (project_root / "cli").mkdir()
    (project_root / "cli" / "index.js").write_text("module.exports = {}\n", encoding="utf-8")

    # server/ with server.ts entry point
    (project_root / "server").mkdir()
    (project_root / "server" / "server.ts").write_text("export default {}\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    names = {m["name"] for m in modules}
    assert "cli" in names
    assert "server" in names
    for m in modules:
        assert m["kind"] == "module"

def test_detect_modules_informal_with_manifest(tmp_path: Path) -> None:
    """Top-level dirs with package.json are detected as subprojects."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "web").mkdir()
    (project_root / "web" / "package.json").write_text('{"name": "web"}', encoding="utf-8")
    (project_root / "web" / "index.ts").write_text("export const x = 1\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    assert len(modules) == 1
    assert modules[0]["name"] == "web"
    assert modules[0]["kind"] == "subproject"
    assert modules[0]["stack"] == "javascript"

def test_detect_modules_npm_workspaces(tmp_path: Path) -> None:
    """Formal npm workspaces are detected."""
    import json
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": ["packages/*"]}), encoding="utf-8"
    )
    (project_root / "packages" / "core").mkdir(parents=True)
    (project_root / "packages" / "core" / "package.json").write_text(
        json.dumps({"name": "@org/core"}), encoding="utf-8"
    )
    (project_root / "packages" / "core" / "index.ts").write_text("export const x = 1\n", encoding="utf-8")
    (project_root / "packages" / "ui").mkdir(parents=True)
    (project_root / "packages" / "ui" / "package.json").write_text(
        json.dumps({"name": "@org/ui"}), encoding="utf-8"
    )
    (project_root / "packages" / "ui" / "index.tsx").write_text("export default function App() {}\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    names = {m["name"] for m in modules}
    assert "core" in names
    assert "ui" in names
    for m in modules:
        if m["name"] in ("core", "ui"):
            assert m["kind"] == "workspace"

def test_detect_modules_dotnet_csproj(tmp_path: Path) -> None:
    """.csproj files trigger project detection."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "src" / "Web").mkdir(parents=True)
    (project_root / "src" / "Web" / "Web.csproj").write_text("<Project></Project>", encoding="utf-8")
    (project_root / "src" / "Web" / "Program.cs").write_text("class Program {}", encoding="utf-8")

    (project_root / "src" / "Domain").mkdir(parents=True)
    (project_root / "src" / "Domain" / "Domain.csproj").write_text("<Project></Project>", encoding="utf-8")
    (project_root / "src" / "Domain" / "Entity.cs").write_text("class Entity {}", encoding="utf-8")

    modules = store.detect_modules(project_root)
    names = {m["name"] for m in modules}
    assert "Web" in names
    assert "Domain" in names
    for m in modules:
        if m["name"] in ("Web", "Domain"):
            assert m["kind"] == "project"
            assert m["stack"] == "csharp"

def test_detect_modules_hint_dirs_with_source(tmp_path: Path) -> None:
    """Well-known directory names with source files are detected even without entry points."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "plugins").mkdir()
    (project_root / "plugins" / "auth.js").write_text("export function auth() {}\n", encoding="utf-8")
    (project_root / "plugins" / "cache.js").write_text("export function cache() {}\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    assert len(modules) == 1
    assert modules[0]["name"] == "plugins"
    assert modules[0]["kind"] == "module"

def test_detect_modules_skips_node_modules_and_hidden(tmp_path: Path) -> None:
    """node_modules, dist, and hidden dirs are never detected as modules."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "node_modules" / "pkg").mkdir(parents=True)
    (project_root / "node_modules" / "pkg" / "index.js").write_text("module.exports = {}\n", encoding="utf-8")
    (project_root / "dist").mkdir()
    (project_root / "dist" / "bundle.js").write_text("var x=1\n", encoding="utf-8")
    (project_root / ".hidden").mkdir()
    (project_root / ".hidden" / "secret.js").write_text("export const s = 1\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    assert len(modules) == 0

def test_detect_modules_no_duplicates(tmp_path: Path) -> None:
    """A dir detected as a workspace is not also detected as an informal module."""
    import json
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": ["web"]}), encoding="utf-8"
    )
    (project_root / "web").mkdir()
    (project_root / "web" / "package.json").write_text(json.dumps({"name": "web"}), encoding="utf-8")
    (project_root / "web" / "index.ts").write_text("export const x = 1\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    web_modules = [m for m in modules if m["name"] == "web"]
    assert len(web_modules) == 1
    assert web_modules[0]["kind"] == "workspace"

def test_sync_modules_persists_to_db(tmp_path: Path) -> None:
    """sync_modules writes to code_modules table and tags code_files."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "api").mkdir()
    (project_root / "api" / "index.ts").write_text("export const handler = {}\n", encoding="utf-8")
    (project_root / "api" / "routes.ts").write_text("export const routes = []\n", encoding="utf-8")

    store.sync_code_files(project_root)
    count = store.sync_modules(project_root)

    assert count >= 1
    with store.connect(project_root) as conn:
        mod_rows = conn.execute("SELECT * FROM code_modules WHERE name = 'api'").fetchall()
        assert len(mod_rows) == 1
        assert mod_rows[0]["kind"] == "module"

        # code_files should be tagged with their module
        tagged = conn.execute("SELECT path, module FROM code_files WHERE module = 'api' ORDER BY path").fetchall()
        assert len(tagged) == 2

def test_detect_modules_mixed_monorepo(tmp_path: Path) -> None:
    """Simulates ADB-like informal monorepo with multiple module types."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    # Formal: web/ has its own package.json
    (project_root / "web").mkdir()
    (project_root / "web" / "package.json").write_text('{"name": "web"}', encoding="utf-8")
    (project_root / "web" / "index.ts").write_text("export default {}\n", encoding="utf-8")

    # Informal with entry: cli/
    (project_root / "cli").mkdir()
    (project_root / "cli" / "index.js").write_text("module.exports = {}\n", encoding="utf-8")

    # Informal hint: plugins/ (no entry point, but has source)
    (project_root / "plugins").mkdir()
    (project_root / "plugins" / "auth.js").write_text("export function auth() {}\n", encoding="utf-8")

    # .NET project
    (project_root / "src" / "Api").mkdir(parents=True)
    (project_root / "src" / "Api" / "Api.csproj").write_text("<Project></Project>", encoding="utf-8")
    (project_root / "src" / "Api" / "Program.cs").write_text("class Program {}", encoding="utf-8")

    # Should skip: node_modules
    (project_root / "node_modules" / "pkg").mkdir(parents=True)
    (project_root / "node_modules" / "pkg" / "index.js").write_text("nope\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    names = {m["name"] for m in modules}
    assert "web" in names
    assert "cli" in names
    assert "plugins" in names
    assert "Api" in names
    assert "pkg" not in names
    assert "node_modules" not in names

    kinds = {m["name"]: m["kind"] for m in modules}
    assert kinds["web"] == "subproject"
    assert kinds["cli"] == "module"
    assert kinds["Api"] == "project"


# ── pnpm / Cargo workspace tests ────────────────────────────────────


def test_detect_modules_pnpm_workspaces(tmp_path: Path) -> None:
    """pnpm-workspace.yaml workspace packages are detected."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "pnpm-workspace.yaml").write_text(
        "packages:\n  - packages/*\n", encoding="utf-8"
    )
    (project_root / "package.json").write_text(json.dumps({"name": "root"}), encoding="utf-8")

    for pkg in ["core", "ui"]:
        d = project_root / "packages" / pkg
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({"name": f"@org/{pkg}"}), encoding="utf-8")
        (d / "index.ts").write_text(f"export const {pkg} = true\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    ws_names = {m["name"] for m in modules if m["kind"] == "workspace"}
    assert "core" in ws_names
    assert "ui" in ws_names


def test_detect_modules_cargo_workspaces(tmp_path: Path) -> None:
    """Cargo workspace members are detected."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "Cargo.toml").write_text(
        "[workspace]\nmembers = [\n    \"crates/*\"\n]\n", encoding="utf-8"
    )

    for crate in ["models", "api"]:
        d = project_root / "crates" / crate
        (d / "src").mkdir(parents=True)
        (d / "Cargo.toml").write_text(f'[package]\nname = "{crate}"\n', encoding="utf-8")
        (d / "src" / "lib.rs").write_text(f"pub mod {crate};\n", encoding="utf-8")

    modules = store.detect_modules(project_root)
    ws_names = {m["name"] for m in modules if m["kind"] == "workspace"}
    assert "models" in ws_names
    assert "api" in ws_names


def test_sync_modules_tags_code_files_with_module(tmp_path: Path) -> None:
    """After sync_modules, code_files rows have correct module column."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "api").mkdir()
    (project_root / "api" / "index.ts").write_text("export const handler = {}\n", encoding="utf-8")
    (project_root / "api" / "routes.ts").write_text("export const routes = []\n", encoding="utf-8")
    (project_root / "lib").mkdir()
    (project_root / "lib" / "utils.ts").write_text("export function helper() {}\n", encoding="utf-8")

    store.sync_code_files(project_root)
    store.sync_modules(project_root)

    with store.connect(project_root) as conn:
        api_files = conn.execute(
            "SELECT path, module FROM code_files WHERE module = 'api' ORDER BY path"
        ).fetchall()
        lib_files = conn.execute(
            "SELECT path, module FROM code_files WHERE module = 'lib' ORDER BY path"
        ).fetchall()

    assert len(api_files) == 2
    assert all(r["module"] == "api" for r in api_files)
    assert len(lib_files) == 1
    assert lib_files[0]["module"] == "lib"


def test_get_module_files_returns_correct_files(tmp_path: Path) -> None:
    """get_module_files returns only files belonging to the specified module."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    (project_root / "api").mkdir()
    (project_root / "api" / "index.ts").write_text("export const handler = {}\n", encoding="utf-8")
    (project_root / "api" / "routes.ts").write_text("export const routes = []\n", encoding="utf-8")
    (project_root / "lib").mkdir()
    (project_root / "lib" / "utils.ts").write_text("export function helper() {}\n", encoding="utf-8")

    store.sync_code_files(project_root)
    store.sync_modules(project_root)

    api_files = store.get_module_files(project_root, "api")
    lib_files = store.get_module_files(project_root, "lib")

    assert len(api_files) == 2
    assert all("api/" in f["path"] for f in api_files)
    assert len(lib_files) == 1
    assert lib_files[0]["path"] == "lib/utils.ts"
