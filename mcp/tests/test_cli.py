"""Tests for the aidocs CLI commands."""
import json
from pathlib import Path

from aidocs_mcp import __version__
from aidocs_mcp.cli import cmd_benchmark, cmd_config, cmd_init, cmd_status, cmd_sync, cmd_version


def test_version(capsys: object) -> None:
    """version command prints version string."""
    import io
    import sys
    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    cmd_version([])
    sys.stdout = old_stdout
    assert "aidocs-mcp" in buf.getvalue()
    assert __version__ in buf.getvalue()


def test_version_json() -> None:
    """version --json emits structured version output."""
    import io
    import sys

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    result = cmd_version(["--json"])
    sys.stdout = old_stdout

    payload = json.loads(buf.getvalue())
    assert result == 0
    assert payload["ok"] is True
    assert payload["package"] == "aidocs-mcp"
    assert payload["version"] == __version__


def test_config_json_default_target(monkeypatch) -> None:
    """config --json reports the default config target without opening it."""
    import io
    import sys

    monkeypatch.setenv("AIDOCS_PATH", str(Path(__file__).resolve().parents[2]))

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    result = cmd_config(["--json"])
    sys.stdout = old_stdout

    payload = json.loads(buf.getvalue())
    assert result == 0
    assert payload["ok"] is True
    assert payload["target_kind"] == "config"
    assert payload["target"].endswith("aidocs.toml")


def test_config_json_languages_target(monkeypatch) -> None:
    """config --json reports the language-pack target."""
    import io
    import sys

    monkeypatch.setenv("AIDOCS_PATH", str(Path(__file__).resolve().parents[2]))

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    result = cmd_config(["--json", "--languages"])
    sys.stdout = old_stdout

    payload = json.loads(buf.getvalue())
    assert result == 0
    assert payload["ok"] is True
    assert payload["target_kind"] == "languages"


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


def test_init_json_output(tmp_path: Path) -> None:
    """init --json emits structured bootstrap output."""
    import io
    import sys

    root = tmp_path / "project"
    root.mkdir()

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    result = cmd_init([str(root), "--json"])
    sys.stdout = old_stdout

    payload = json.loads(buf.getvalue())
    assert result == 0
    assert payload["ok"] is True
    assert payload["initialized"] is True
    assert payload["project_name"] == "project"
    assert payload["created_count"] >= 1
    assert "mcp_config" in payload


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


def test_status_json_on_initialized_project(tmp_path: Path) -> None:
    """status --json emits structured output."""
    import io
    import sys

    root = tmp_path / "project"
    root.mkdir()
    cmd_init([str(root)])

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    result = cmd_status([str(root), "--json"])
    sys.stdout = old_stdout

    payload = json.loads(buf.getvalue())
    assert result == 0
    assert payload["ok"] is True
    assert payload["initialized"] is True
    assert payload["project_name"] == "project"
    assert "managed_mode" in payload


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


def test_sync_json_on_initialized_project(tmp_path: Path) -> None:
    """sync --json emits structured output."""
    import io
    import sys

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def hello(): pass\n", encoding="utf-8")

    cmd_init([str(root)])

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    result = cmd_sync([str(root), "--json"])
    sys.stdout = old_stdout

    payload = json.loads(buf.getvalue())
    assert result == 0
    assert payload["ok"] is True
    assert payload["initialized"] is True
    assert payload["code"]["files"] >= 1
    assert "total_seconds" in payload


def test_sync_on_uninitialized_project(tmp_path: Path) -> None:
    """sync returns 1 on uninitialized project."""
    root = tmp_path / "empty"
    root.mkdir()

    result = cmd_sync([str(root)])
    assert result == 1


def test_benchmark_json_on_initialized_project(tmp_path: Path) -> None:
    """benchmark --json emits structured output."""
    import io
    import sys

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def hello(): pass\n", encoding="utf-8")

    cmd_init([str(root)])

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    result = cmd_benchmark([str(root), "--json", "--iterations", "2"])
    sys.stdout = old_stdout

    payload = json.loads(buf.getvalue())
    assert result == 0
    assert payload["ok"] is True
    assert payload["scenario_set"] == "public"
    assert payload["iterations"] == 2
    assert payload["sync"]["code_files"] >= 1
    assert payload["classification"]["prompt_count"] >= 1
    assert payload["classification"]["total_classifications"] == payload["classification"]["prompt_count"] * 2
    assert "per_language" in payload["classification"]
    assert "en" in payload["classification"]["per_language"]
    assert payload["retrieval"]["scenario_count"] >= 1
    assert len(payload["retrieval"]["scenarios"]) == payload["retrieval"]["scenario_count"]
    assert "schema_benchmark" in payload
    assert payload["schema_benchmark"]["scenario_count"] >= 0
    assert "comparative" in payload
    assert payload["comparative"]["scenario_count"] >= 1


def test_benchmark_invalid_scenario_set_returns_error(tmp_path: Path) -> None:
    """benchmark rejects unknown scenario sets cleanly."""
    import io
    import sys

    root = tmp_path / "project"
    root.mkdir()
    cmd_init([str(root)])

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    result = cmd_benchmark([str(root), "--json", "--scenario-set", "private"])
    sys.stdout = old_stdout

    payload = json.loads(buf.getvalue())
    assert result == 1
    assert payload["ok"] is False
    assert payload["reason"] == "invalid_scenario_set"


def test_benchmark_writes_output_file(tmp_path: Path) -> None:
    """benchmark --out writes JSON payload to disk."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def hello(): pass\n", encoding="utf-8")
    out_file = tmp_path / "bench" / "result.json"

    cmd_init([str(root)])

    result = cmd_benchmark([str(root), "--iterations", "1", "--out", str(out_file)])
    assert result == 0
    assert out_file.is_file()

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["scenario_set"] == "public"
    assert "comparative" in payload
