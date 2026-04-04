"""Tests for string-match edit tools: str_replace and batch_str_replace."""

from __future__ import annotations

from pathlib import Path

import pytest
from aidocs_mcp.file_ops import str_replace, batch_str_replace


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    (p / ".MEMORY").mkdir(parents=True)
    src = p / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "def hello():\n    return 'world'\n\ndef goodbye():\n    return 'moon'\n",
        encoding="utf-8",
    )
    (src / "utils.py").write_text(
        "MAX_RETRIES = 3\nTIMEOUT = 30\n",
        encoding="utf-8",
    )
    return p


# ── str_replace: basic ──


class TestStrReplace:
    def test_simple_replacement(self, project: Path) -> None:
        result = str_replace(project, "src/app.py", "return 'world'", "return 'earth'")
        assert result["success"] is True
        assert result["changed"] == 1
        content = (project / "src" / "app.py").read_text(encoding="utf-8")
        assert "return 'earth'" in content

    def test_multiline_replacement(self, project: Path) -> None:
        result = str_replace(
            project,
            "src/app.py",
            "def hello():\n    return 'world'",
            "def hello(name):\n    return f'hello {name}'",
        )
        assert result["success"] is True
        content = (project / "src" / "app.py").read_text(encoding="utf-8")
        assert "def hello(name):" in content

    def test_no_match_fails(self, project: Path) -> None:
        result = str_replace(project, "src/app.py", "nonexistent string", "replacement")
        assert result["success"] is False
        assert "no match" in result["error"].lower()

    def test_ambiguous_match_fails(self, project: Path) -> None:
        result = str_replace(project, "src/app.py", "return", "yield")
        assert result["success"] is False
        assert "2" in result["error"] or "multiple" in result["error"].lower()

    def test_replace_all_mode(self, project: Path) -> None:
        result = str_replace(
            project, "src/app.py", "return", "yield", replace_all=True
        )
        assert result["success"] is True
        assert result["replacements"] == 2
        content = (project / "src" / "app.py").read_text(encoding="utf-8")
        assert "return" not in content
        assert content.count("yield") == 2

    def test_old_str_size_limit(self, project: Path) -> None:
        huge = "x" * 600
        result = str_replace(project, "src/app.py", huge, "small")
        assert result["success"] is False
        assert "code_edit_lines" in result["error"]

    def test_sensitive_file_blocked(self, project: Path) -> None:
        (project / ".env").write_text("SECRET=123\n", encoding="utf-8")
        result = str_replace(project, ".env", "SECRET=123", "SECRET=456")
        assert result["success"] is False
        assert "sensitive" in result["error"].lower()

    def test_response_is_slim(self, project: Path) -> None:
        result = str_replace(project, "src/app.py", "return 'world'", "return 'earth'")
        assert "old_content" not in result
        assert "new_content" not in result


# ── batch_str_replace ──


class TestBatchStrReplace:
    def test_multiple_edits_same_file(self, project: Path) -> None:
        result = batch_str_replace(
            project,
            [
                {"path": "src/app.py", "old_str": "return 'world'", "new_str": "return 'earth'"},
                {"path": "src/app.py", "old_str": "return 'moon'", "new_str": "return 'sun'"},
            ],
        )
        assert result["success"] is True
        assert result["applied"] == 2
        content = (project / "src" / "app.py").read_text(encoding="utf-8")
        assert "earth" in content
        assert "sun" in content

    def test_multiple_edits_cross_file(self, project: Path) -> None:
        result = batch_str_replace(
            project,
            [
                {"path": "src/app.py", "old_str": "return 'world'", "new_str": "return 'earth'"},
                {"path": "src/utils.py", "old_str": "TIMEOUT = 30", "new_str": "TIMEOUT = 60"},
            ],
        )
        assert result["success"] is True
        assert result["applied"] == 2

    def test_atomic_rollback_on_failure(self, project: Path) -> None:
        result = batch_str_replace(
            project,
            [
                {"path": "src/app.py", "old_str": "return 'world'", "new_str": "return 'earth'"},
                {"path": "src/app.py", "old_str": "nonexistent", "new_str": "fail"},
            ],
        )
        assert result["success"] is False
        # Atomic: first edit should NOT have been applied
        content = (project / "src" / "app.py").read_text(encoding="utf-8")
        assert "return 'world'" in content

    def test_max_batch_size(self, project: Path) -> None:
        edits = [
            {"path": "src/app.py", "old_str": f"x{i}", "new_str": f"y{i}"}
            for i in range(25)
        ]
        result = batch_str_replace(project, edits)
        assert result["success"] is False
        assert "maximum" in result["error"].lower()
