from pathlib import Path

from aidocs_mcp.config import ConfigResolver


def _write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def test_effective_config_resolves_session_over_project_over_global(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    session_id = "session-a"

    _write_config(
        global_root / "aidocs.toml",
        """
        [agent]
        directive_style = "global"
        inject_rules_on_bootstrap = true
        """,
    )
    _write_config(
        project_root / "aidocs.toml",
        """
        [agent]
        directive_style = "project"
        """,
    )
    _write_config(
        project_root / ".MEMORY" / "sessions" / session_id / "aidocs.toml",
        """
        [agent]
        directive_style = "session"
        inject_rules_on_bootstrap = false
        """,
    )

    monkeypatch.setenv("AIDOCS_PATH", str(global_root))
    resolver = ConfigResolver()

    effective = resolver.effective_config(project_root=project_root, session_id=session_id)

    assert effective["agent"]["directive_style"] == "session"
    assert effective["agent"]["inject_rules_on_bootstrap"] is False
    assert resolver.get("agent.directive_style", project_root=project_root, session_id=session_id) == "session"
    assert resolver.get("agent.inject_rules_on_bootstrap", project_root=project_root, session_id=session_id) is False


def test_effective_config_preserves_string_boolean_inputs_for_runtime_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"

    _write_config(
        global_root / "aidocs.toml",
        """
        [agent]
        inject_rules_on_bootstrap = true
        """,
    )
    _write_config(
        project_root / "aidocs.toml",
        """
        [agent]
        inject_rules_on_bootstrap = "false"
        """,
    )

    monkeypatch.setenv("AIDOCS_PATH", str(global_root))
    resolver = ConfigResolver()

    assert resolver.get("agent.inject_rules_on_bootstrap", project_root=project_root) == "false"
