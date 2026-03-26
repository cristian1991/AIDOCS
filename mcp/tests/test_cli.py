"""Tests for the aidocs CLI commands."""
from pathlib import Path

from aidocs_mcp.cli import cmd_init, cmd_status, cmd_sync, cmd_version


def test_version(capsys: object) -> None:
    """version command prints version string."""
    import io
    import sys
    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    cmd_version([])
    sys.stdout = old_stdout
    assert "aidocs-mcp" in buf.getvalue()


def test_init_creates_structure(tmp_path: Path) -> None:
    """init creates .MEMORY/, AGENTS.md, CLAUDE.md."""
    root = tmp_path / "project"
    root.mkdir()

    cmd_init([str(root)])

    assert (root / ".MEMORY").is_dir()
    assert (root / ".MEMORY" / "INDEX.md").is_file()
    assert (root / ".MEMORY" / ".aidocs" / "index.aidocs").is_file()
    assert (root / "AGENTS.md").is_file()
    assert (root / "CLAUDE.md").is_file()


def test_init_idempotent(tmp_path: Path) -> None:
    """Running init twice doesn't duplicate files."""
    root = tmp_path / "project"
    root.mkdir()

    cmd_init([str(root)])
    # Read a file
    content1 = (root / ".MEMORY" / "INDEX.md").read_text(encoding="utf-8")

    cmd_init([str(root)])
    content2 = (root / ".MEMORY" / "INDEX.md").read_text(encoding="utf-8")

    assert content1 == content2


def test_status_on_initialized_project(tmp_path: Path) -> None:
    """status returns 0 on an initialized project."""
    root = tmp_path / "project"
    root.mkdir()
    cmd_init([str(root)])

    result = cmd_status([str(root)])
    assert result == 0


def test_status_on_uninitialized_project(tmp_path: Path) -> None:
    """status returns 1 on a project without .MEMORY/."""
    root = tmp_path / "empty"
    root.mkdir()

    result = cmd_status([str(root)])
    assert result == 1


def test_sync_on_initialized_project(tmp_path: Path) -> None:
    """sync runs without error on initialized project with source files."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def hello(): pass\n", encoding="utf-8")

    cmd_init([str(root)])
    result = cmd_sync([str(root)])
    assert result == 0

    # Verify index was populated
    from aidocs_mcp.code_index_store import CodeIndexStore
    store = CodeIndexStore()
    with store.connect(root) as conn:
        count = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
    assert count >= 1


def test_sync_on_uninitialized_project(tmp_path: Path) -> None:
    """sync returns 1 on uninitialized project."""
    root = tmp_path / "empty"
    root.mkdir()

    result = cmd_sync([str(root)])
    assert result == 1
