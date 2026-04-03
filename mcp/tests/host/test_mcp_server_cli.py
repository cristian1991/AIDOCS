import sys

import pytest

from aidocs_mcp import mcp_server


def test_mcp_server_help_prints_usage_and_exits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail_create_server() -> None:
        raise AssertionError("create_server should not be called for --help")

    monkeypatch.setattr(mcp_server, "create_server", fail_create_server)
    monkeypatch.setattr(sys, "argv", ["aidocs_mcp.mcp_server", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        mcp_server.main()

    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    assert "usage:" in captured.out.lower()
    assert "show this help message and exit" in captured.out.lower()
