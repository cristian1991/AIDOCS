import sqlite3
from pathlib import Path

from aidocs_mcp.index_store import IndexStore
from aidocs_mcp.session_store import SessionStore


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text(
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
    (root / "context.md").write_text("# Context\n", encoding="utf-8")


def test_init_db_creates_tables(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = IndexStore(SessionStore(templates_root=templates))
    project_root = tmp_path / "project"

    store.init_db(project_root)

    with store.connect(project_root) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"sessions", "memory_files", "memory_links"}.issubset(tables)


def test_sync_sessions_indexes_session_summaries(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    sessions = SessionStore(templates_root=templates)
    store = IndexStore(sessions)
    project_root = tmp_path / "project"

    sessions.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    count = store.sync_sessions(project_root)

    assert count == 1
    with store.connect(project_root) as conn:
        row = conn.execute("SELECT session_id, title, status FROM sessions").fetchone()
    assert row["session_id"] == "2026-03-23-test"
    assert row["title"] == "Test"
    assert row["status"] == "active"


def test_sync_memory_files_indexes_links_and_kinds(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    sessions = SessionStore(templates_root=templates)
    store = IndexStore(sessions)
    project_root = tmp_path / "project"
    memory_root = project_root / ".MEMORY"

    (memory_root / ".aidocs").mkdir(parents=True, exist_ok=True)
    (memory_root / ".aidocs" / "index.aidocs").write_text(
        "# Index\n\n- [INDEX](../INDEX.md)\n", encoding="utf-8"
    )
    (memory_root / "INDEX.md").write_text(
        "# Memory Index\n\n- [rule](rules/workflow-rules.md)\n", encoding="utf-8"
    )
    (memory_root / "rules").mkdir(parents=True, exist_ok=True)
    (memory_root / "rules" / "workflow-rules.md").write_text("- rule\n", encoding="utf-8")

    count = store.sync_memory_files(project_root)

    assert count == 3
    with store.connect(project_root) as conn:
        files = conn.execute("SELECT path, kind, title FROM memory_files ORDER BY path").fetchall()
        links = conn.execute("SELECT source_path, target_path FROM memory_links ORDER BY source_path, target_path").fetchall()
    assert (".aidocs/index.aidocs", "aidocs") in [(r["path"], r["kind"]) for r in files]
    assert ("rules/workflow-rules.md", "rule") in [(r["path"], r["kind"]) for r in files]
    assert any(r["title"] == "Memory Index" for r in files)
    assert (".aidocs/index.aidocs", "INDEX.md") in [(r["source_path"], r["target_path"]) for r in links]


def test_sync_memory_files_dedupes_repeated_links_and_ignores_local_anchors(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    sessions = SessionStore(templates_root=templates)
    store = IndexStore(sessions)
    project_root = tmp_path / "project"
    memory_root = project_root / ".MEMORY"

    (memory_root / ".aidocs").mkdir(parents=True, exist_ok=True)
    (memory_root / ".aidocs" / "index.aidocs").write_text(
        "# Index\n\n"
        "- [INDEX](../INDEX.md)\n"
        "- [INDEX again](../INDEX.md#details)\n"
        "- [Local](#local-anchor)\n",
        encoding="utf-8",
    )
    (memory_root / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")

    count = store.sync_memory_files(project_root)

    assert count == 2
    with store.connect(project_root) as conn:
        links = conn.execute("SELECT source_path, target_path FROM memory_links ORDER BY source_path, target_path").fetchall()
    assert [(row["source_path"], row["target_path"]) for row in links] == [(".aidocs/index.aidocs", "INDEX.md")]


def test_status_and_search_memory(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    sessions = SessionStore(templates_root=templates)
    store = IndexStore(sessions)
    project_root = tmp_path / "project"
    memory_root = project_root / ".MEMORY"

    sessions.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    (memory_root / "domains").mkdir(parents=True, exist_ok=True)
    (memory_root / "domains" / "memory-system.md").write_text(
        "# Memory System\n\n- Session-based memory capabilities\n", encoding="utf-8"
    )

    sync_result = store.sync_all(project_root)
    status = store.status(project_root)
    search = store.search_memory(project_root, "session-based", limit=5)

    assert sync_result["sessions"] == 1
    assert status["sessions"] == 1
    assert status["memory_files"] >= 2
    assert any(item["path"] == "domains/memory-system.md" for item in search)
