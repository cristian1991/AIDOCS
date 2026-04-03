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


def test_sync_code_files_skips_generic_build_outputs(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "mcp" / "build" / "lib").mkdir(parents=True, exist_ok=True)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "mcp" / "build" / "lib" / "generated.py").write_text("def nope(): pass\n", encoding="utf-8")
    (project_root / "src" / "real.py").write_text("def ok(): pass\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)

    assert count == 1
    with store.connect(project_root) as conn:
        rows = conn.execute("SELECT path FROM code_files ORDER BY path").fetchall()
    assert [r["path"] for r in rows] == ["src/real.py"]

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
    assert results[0]["path"] == "src/server.ts"
    assert results[0]["role"] != "unknown"  # server.ts gets a real role now

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


# ── os.walk pruning tests ────────────────────────────────────────────


def test_walk_prunes_node_modules_never_entered(tmp_path: Path) -> None:
    """node_modules with many files is pruned at directory level, not file level."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.ts").write_text("export const ok = true\n", encoding="utf-8")
    nm = project_root / "node_modules" / "pkg" / "lib"
    nm.mkdir(parents=True, exist_ok=True)
    for i in range(60):
        (nm / f"file_{i}.js").write_text(f"module.exports = {i}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    import time
    t0 = time.time()
    count = store.sync_code_files(project_root)
    elapsed = time.time() - t0

    assert count == 1
    assert elapsed < 2.0


def test_walk_prunes_git_and_hidden_dirs(tmp_path: Path) -> None:
    """.git, .venv, __pycache__ directories are never entered."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    for d in [".git/objects", ".venv/lib/python3/site-packages", "__pycache__"]:
        (project_root / d).mkdir(parents=True, exist_ok=True)
        (project_root / d / "hidden.py").write_text("x = 1\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)
    assert count == 1
    with store.connect(project_root) as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM code_files").fetchall()]
    assert paths == ["src/main.py"]


def test_walk_prunes_target_and_migrations(tmp_path: Path) -> None:
    """Rust target/ and migrations/ directories are skipped."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (project_root / "target" / "debug").mkdir(parents=True, exist_ok=True)
    (project_root / "target" / "debug" / "build.rs").write_text("fn build() {}\n", encoding="utf-8")
    (project_root / "migrations" / "2024-01-01").mkdir(parents=True, exist_ok=True)
    (project_root / "migrations" / "2024-01-01" / "up.sql").write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)
    assert count == 1
    with store.connect(project_root) as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM code_files").fetchall()]
    assert paths == ["src/main.rs"]


# ── Skip pattern tests ───────────────────────────────────────────────


def test_skip_sql_dumps_and_executed_scripts(tmp_path: Path) -> None:
    """SQLScripts/Executed/ and similar archived SQL dirs are skipped."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "schema.sql").write_text("CREATE TABLE users (id INT);\n", encoding="utf-8")
    (project_root / "SQLScripts" / "Executed").mkdir(parents=True, exist_ok=True)
    (project_root / "SQLScripts" / "Executed" / "old.sql").write_text("ALTER TABLE x ADD y INT;\n", encoding="utf-8")
    (project_root / "db" / "archived").mkdir(parents=True, exist_ok=True)
    (project_root / "db" / "archived" / "dump.sql").write_text("INSERT INTO x VALUES (1);\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM code_files ORDER BY path").fetchall()]
    assert paths == ["src/schema.sql"]


def test_skip_fixtures_directory(tmp_path: Path) -> None:
    """Test fixtures directories are skipped."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.ts").write_text("export const ok = true\n", encoding="utf-8")
    (project_root / "scripts" / "fixtures").mkdir(parents=True, exist_ok=True)
    (project_root / "scripts" / "fixtures" / "data.json").write_text('{"test": true}', encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM code_files").fetchall()]
    assert paths == ["src/app.ts"]


def test_skip_lock_files(tmp_path: Path) -> None:
    """Lock files (package-lock.json, bun.lock, etc.) are skipped."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.js").write_text("module.exports = {}\n", encoding="utf-8")
    for lock in ["package-lock.json", "bun.lock", "yarn.lock", "Cargo.lock", "composer.lock"]:
        (project_root / lock).write_text("{}", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM code_files").fetchall()]
    assert paths == ["src/app.js"]


def test_skip_large_json_files(tmp_path: Path) -> None:
    """JSON files over 100KB are skipped, small ones are indexed."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "config.json").write_text('{"key": "value"}', encoding="utf-8")
    (project_root / "src" / "huge.json").write_text('{"data": "' + "x" * 110_000 + '"}', encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM code_files ORDER BY path").fetchall()]
    assert "src/config.json" in paths
    assert "src/huge.json" not in paths


def test_skip_minified_files(tmp_path: Path) -> None:
    """.min.js and .min.css files are skipped."""
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.js").write_text("export const ok = true\n", encoding="utf-8")
    (project_root / "src" / "app.min.js").write_text("var a=1;", encoding="utf-8")
    (project_root / "src" / "style.min.css").write_text("body{}", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    count = store.sync_code_files(project_root)
    with store.connect(project_root) as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM code_files").fetchall()]
    assert paths == ["src/app.js"]
