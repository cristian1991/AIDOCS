"""Tests for file_ops — line-based read/edit with safety checks."""
from __future__ import annotations

from pathlib import Path

import pytest

from aidocs_mcp.file_ops import get_lines, edit_lines, batch_edit


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a minimal project with test files."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()  # Project marker for validation
    (root / "hello.txt").write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    (root / "config.toml").write_text('[section]\nkey = "value"\nother = 42\n', encoding="utf-8")
    (root / "code.cs").write_text(
        "namespace App;\n\npublic class Foo\n{\n    public int Bar { get; set; }\n}\n",
        encoding="utf-8",
    )
    sub = root / "src"
    sub.mkdir()
    (sub / "app.js").write_text("const x = 1;\nconst y = 2;\nconst z = 3;\n", encoding="utf-8")
    return root


# ── get_lines ──


class TestGetLines:
    def test_reads_all_lines(self, project: Path) -> None:
        result = get_lines(project, "hello.txt")
        assert result["total_lines"] == 5
        assert result["start_line"] == 1
        assert result["end_line"] == 5
        assert result["content"].count("\n") == 4  # 5 lines = 4 newlines
        assert not result.get("has_more", False)

    def test_reads_specific_range(self, project: Path) -> None:
        result = get_lines(project, "hello.txt", start_line=2, count=2)
        assert result["start_line"] == 2
        assert result["end_line"] == 3
        assert "line2" in result["content"] and "line3" in result["content"]
        assert result["has_more"] is True

    def test_line_numbers_in_content(self, project: Path) -> None:
        result = get_lines(project, "hello.txt", start_line=1, count=2, show_line_numbers=True)
        assert "1  line1" in result["content"]
        assert "2  line2" in result["content"]

    def test_no_line_numbers(self, project: Path) -> None:
        result = get_lines(project, "hello.txt", start_line=1, count=2, show_line_numbers=False)
        assert result["content"] == "line1\nline2"

    def test_clamps_start_to_valid_range(self, project: Path) -> None:
        result = get_lines(project, "hello.txt", start_line=100)
        assert result["start_line"] == 5  # Clamped to last line
        assert result["content"].strip().count("\n") == 0  # single line

    def test_clamps_count_to_max(self, project: Path) -> None:
        result = get_lines(project, "hello.txt", count=9999)
        assert result["truncated"] is True

    def test_subdirectory_file(self, project: Path) -> None:
        result = get_lines(project, "src/app.js")
        assert result["total_lines"] == 3
        assert "const x = 1;" in result["content"]

    def test_backslash_path(self, project: Path) -> None:
        result = get_lines(project, "src\\app.js")
        assert result["total_lines"] == 3

    def test_nonexistent_file_raises(self, project: Path) -> None:
        with pytest.raises(FileNotFoundError):
            get_lines(project, "nonexistent.txt")

    def test_path_traversal_blocked(self, project: Path) -> None:
        with pytest.raises(ValueError, match="traversal"):
            get_lines(project, "../../../etc/passwd")

    def test_absolute_path_blocked(self, project: Path) -> None:
        with pytest.raises(ValueError, match="Absolute"):
            get_lines(project, "C:/Windows/System32/config/SAM")


# ── Security guardrails ──


class TestSecurityGuardrails:
    def test_system_directory_blocked_for_read(self, tmp_path: Path) -> None:
        """Cannot use system directories as project root."""
        with pytest.raises(ValueError, match="system directory"):
            get_lines(Path("C:/Windows"), "win.ini")

    def test_system_directory_blocked_for_edit(self) -> None:
        result = edit_lines(Path("C:/Windows"), "win.ini", 1, 1, "x")
        assert result["success"] is False
        assert "system directory" in result["error"]

    def test_system_directory_blocked_for_batch(self) -> None:
        result = batch_edit(Path("C:/Windows"), [{"path": "win.ini", "start_line": 1, "end_line": 1, "new_content": "x"}])
        assert result["success"] is False
        assert "system directory" in result["error"]

    def test_self_edit_blocked(self) -> None:
        """MCP server cannot edit its own source."""
        from aidocs_mcp.file_ops import _SELF_DIR
        mcp_root = Path(_SELF_DIR).parent.parent.parent  # Go up to project root
        if mcp_root.is_dir() and (mcp_root / ".git").exists():
            result = edit_lines(mcp_root, "mcp/server/aidocs_mcp/file_ops.py", 1, 1, "# hacked")
            assert result["success"] is False
            assert "self-edit" in result["error"].lower() or "AIDOCS" in result["error"]

    def test_nonexistent_project_root_blocked(self) -> None:
        result = edit_lines(Path("/nonexistent/fake/project"), "foo.txt", 1, 1, "x")
        assert result["success"] is False

    def test_no_project_markers_blocked(self, tmp_path: Path) -> None:
        """Directory without any project markers is rejected."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = edit_lines(empty_dir, "foo.txt", 1, 1, "x")
        assert result["success"] is False
        assert "project" in result["error"].lower()

    def test_valid_project_with_git_passes(self, project: Path) -> None:
        """Directory with .git marker passes validation."""
        result = get_lines(project, "hello.txt")
        assert result["total_lines"] == 5

    def test_sensitive_file_blocked_in_batch(self, project: Path) -> None:
        (project / ".git").mkdir(exist_ok=True)
        (project / ".env").write_text("SECRET=123\n", encoding="utf-8")
        result = batch_edit(project, [
            {"path": ".env", "start_line": 1, "end_line": 1, "new_content": "HACKED=true"},
        ])
        # Atomic mode should reject because sensitive check fails during apply
        assert result["success"] is False or result["failed"] > 0


# ── edit_lines ──


class TestEditLines:
    def test_replace_single_line(self, project: Path) -> None:
        result = edit_lines(project, "hello.txt", start_line=2, end_line=2, new_content="REPLACED")
        assert result["success"] is True
        assert result["old_content"] == "line2"
        assert result["lines_removed"] == 1
        assert result["lines_added"] == 1
        # Verify file
        lines = (project / "hello.txt").read_text(encoding="utf-8").splitlines()
        assert lines[1] == "REPLACED"

    def test_replace_range(self, project: Path) -> None:
        result = edit_lines(project, "hello.txt", start_line=2, end_line=4, new_content="A\nB")
        assert result["success"] is True
        assert result["lines_removed"] == 3
        assert result["lines_added"] == 2
        lines = (project / "hello.txt").read_text(encoding="utf-8").splitlines()
        assert lines == ["line1", "A", "B", "line5"]

    def test_insert_mode(self, project: Path) -> None:
        # end_line < start_line = insert before start_line
        result = edit_lines(project, "hello.txt", start_line=3, end_line=2, new_content="INSERTED")
        assert result["success"] is True
        assert result["old_content"] == ""
        assert result["lines_removed"] == 0
        assert result["lines_added"] == 1
        lines = (project / "hello.txt").read_text(encoding="utf-8").splitlines()
        assert lines[2] == "INSERTED"
        assert lines[3] == "line3"

    def test_expect_match_succeeds(self, project: Path) -> None:
        result = edit_lines(
            project, "hello.txt", start_line=1, end_line=1,
            new_content="NEW", expect="line1",
        )
        assert result["success"] is True

    def test_expect_mismatch_rejects(self, project: Path) -> None:
        result = edit_lines(
            project, "hello.txt", start_line=1, end_line=1,
            new_content="NEW", expect="wrong content",
        )
        assert result["success"] is False
        assert "mismatch" in result["error"].lower()
        # File should be unchanged
        lines = (project / "hello.txt").read_text(encoding="utf-8").splitlines()
        assert lines[0] == "line1"

    def test_dry_run_no_write(self, project: Path) -> None:
        result = edit_lines(
            project, "hello.txt", start_line=1, end_line=1,
            new_content="CHANGED", dry_run=True,
        )
        assert result["success"] is True
        assert result["dry_run"] is True
        # File should be unchanged
        lines = (project / "hello.txt").read_text(encoding="utf-8").splitlines()
        assert lines[0] == "line1"

    def test_multiline_replacement(self, project: Path) -> None:
        result = edit_lines(
            project, "hello.txt", start_line=1, end_line=1,
            new_content="first\nsecond\nthird",
        )
        assert result["success"] is True
        assert result["lines_added"] == 3
        lines = (project / "hello.txt").read_text(encoding="utf-8").splitlines()
        assert lines[0] == "first"
        assert lines[1] == "second"
        assert lines[2] == "third"
        assert lines[3] == "line2"

    def test_delete_lines(self, project: Path) -> None:
        result = edit_lines(
            project, "hello.txt", start_line=2, end_line=4,
            new_content="",
        )
        assert result["success"] is True
        assert result["lines_removed"] == 3
        assert result["lines_added"] == 0
        lines = (project / "hello.txt").read_text(encoding="utf-8").splitlines()
        assert lines == ["line1", "line5"]

    def test_sensitive_file_blocked(self, project: Path) -> None:
        (project / ".env").write_text("SECRET=123\n", encoding="utf-8")
        result = edit_lines(project, ".env", start_line=1, end_line=1, new_content="x")
        assert result["success"] is False
        assert "sensitive" in result["error"].lower()

    def test_invalid_start_line(self, project: Path) -> None:
        result = edit_lines(project, "hello.txt", start_line=0, end_line=1, new_content="x")
        assert result["success"] is False

    def test_start_beyond_file(self, project: Path) -> None:
        result = edit_lines(project, "hello.txt", start_line=100, end_line=100, new_content="x")
        assert result["success"] is False


# ── batch_edit ──


class TestBatchEdit:
    def test_multiple_files(self, project: Path) -> None:
        result = batch_edit(project, [
            {"path": "hello.txt", "start_line": 1, "end_line": 1, "new_content": "FIRST"},
            {"path": "src/app.js", "start_line": 2, "end_line": 2, "new_content": "const y = 99;"},
        ])
        assert result["success"] is True
        assert result["applied"] == 2
        assert result["failed"] == 0
        # Verify both files
        assert (project / "hello.txt").read_text(encoding="utf-8").splitlines()[0] == "FIRST"
        assert (project / "src" / "app.js").read_text(encoding="utf-8").splitlines()[1] == "const y = 99;"

    def test_same_file_multiple_edits(self, project: Path) -> None:
        # Edit line 5, then line 2 — should work because bottom-up processing
        result = batch_edit(project, [
            {"path": "hello.txt", "start_line": 5, "end_line": 5, "new_content": "LAST"},
            {"path": "hello.txt", "start_line": 2, "end_line": 2, "new_content": "SECOND"},
        ])
        assert result["success"] is True
        lines = (project / "hello.txt").read_text(encoding="utf-8").splitlines()
        assert lines[1] == "SECOND"
        assert lines[4] == "LAST"

    def test_atomic_rejects_all_on_failure(self, project: Path) -> None:
        result = batch_edit(project, [
            {"path": "hello.txt", "start_line": 1, "end_line": 1, "new_content": "OK"},
            {"path": "hello.txt", "start_line": 1, "end_line": 1, "new_content": "OK", "expect": "WRONG"},
        ], atomic=True)
        assert result["success"] is False
        assert result["applied"] == 0
        # File unchanged
        assert (project / "hello.txt").read_text(encoding="utf-8").splitlines()[0] == "line1"

    def test_dry_run_no_writes(self, project: Path) -> None:
        result = batch_edit(project, [
            {"path": "hello.txt", "start_line": 1, "end_line": 1, "new_content": "CHANGED"},
        ], dry_run=True)
        assert result["success"] is True
        assert result["applied"] == 1
        # File unchanged
        assert (project / "hello.txt").read_text(encoding="utf-8").splitlines()[0] == "line1"

    def test_empty_batch(self, project: Path) -> None:
        result = batch_edit(project, [])
        assert result["success"] is True
        assert result["total_edits"] == 0

    def test_too_many_edits_rejected(self, project: Path) -> None:
        edits = [{"path": "hello.txt", "start_line": 1, "end_line": 1, "new_content": "x"}] * 25
        result = batch_edit(project, edits)
        assert result["success"] is False
        assert "too many" in result["error"].lower()

    def test_expect_verification(self, project: Path) -> None:
        result = batch_edit(project, [
            {"path": "hello.txt", "start_line": 1, "end_line": 1, "new_content": "NEW", "expect": "line1"},
        ])
        assert result["success"] is True
        assert (project / "hello.txt").read_text(encoding="utf-8").splitlines()[0] == "NEW"
