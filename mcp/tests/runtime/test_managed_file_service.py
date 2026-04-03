from pathlib import Path

from aidocs_mcp.constants import MANAGED_MARKER
from aidocs_mcp.managed_file_service import ManagedFileService


def test_rewrite_managed_section_creates_file(tmp_path: Path) -> None:
    svc = ManagedFileService()
    path = tmp_path / "file.md"

    svc.rewrite_managed_section(path, "# Title\n\nManaged")

    text = path.read_text(encoding="utf-8")
    assert "# Title" in text
    assert MANAGED_MARKER in text


def test_rewrite_managed_section_preserves_user_content(tmp_path: Path) -> None:
    svc = ManagedFileService()
    path = tmp_path / "file.md"
    path.write_text(
        "old managed\n\n"
        + MANAGED_MARKER
        + "\n\n"
        + "user line\n",
        encoding="utf-8",
    )

    svc.rewrite_managed_section(path, "new managed")

    text = path.read_text(encoding="utf-8")
    assert text.startswith("new managed")
    assert "user line" in text


def test_read_user_section_returns_only_post_marker_content(tmp_path: Path) -> None:
    svc = ManagedFileService()
    path = tmp_path / "file.md"
    path.write_text("managed\n\n" + MANAGED_MARKER + "\n\nuser section\n", encoding="utf-8")

    assert svc.read_user_section(path) == "user section\n"
