import json
from pathlib import Path

from aidocs_mcp.config import ConfigResolver, render_interaction_text


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

    effective = resolver.effective_config(
        project_root=project_root, session_id=session_id
    )

    assert effective["agent"]["directive_style"] == "session"
    assert effective["agent"]["inject_rules_on_bootstrap"] is False
    assert (
        resolver.get(
            "agent.directive_style", project_root=project_root, session_id=session_id
        )
        == "session"
    )
    assert (
        resolver.get(
            "agent.inject_rules_on_bootstrap",
            project_root=project_root,
            session_id=session_id,
        )
        is False
    )


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

    assert (
        resolver.get("agent.inject_rules_on_bootstrap", project_root=project_root)
        == "false"
    )


def test_action_hook_defaults_are_loaded_from_action_hooks_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_root = tmp_path / "global"
    _write_config(
        global_root / "action_hooks" / "default.toml",
        """
        [interaction.startup]
        not_initialized = "Run setup from custom action hook defaults."
        """,
    )

    monkeypatch.setenv("AIDOCS_PATH", str(global_root))
    resolver = ConfigResolver()

    assert (
        resolver.render_text("interaction.startup.not_initialized")
        == "Run setup from custom action hook defaults."
    )


def test_project_config_can_override_action_hook_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    _write_config(
        global_root / "action_hooks" / "default.toml",
        """
        [interaction.managed]
        use_mcp_first = "Use MCP from defaults."
        """,
    )
    _write_config(
        project_root / "aidocs.toml",
        """
        [interaction.managed]
        use_mcp_first = "Use project-specific MCP guidance."
        """,
    )

    monkeypatch.setenv("AIDOCS_PATH", str(global_root))

    assert (
        render_interaction_text(
            "interaction.managed.use_mcp_first", project_root=project_root
        )
        == "Use project-specific MCP guidance."
    )


def test_project_config_can_override_presentation_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    _write_config(
        global_root / "aidocs.toml",
        """
        [presentation]
        helper_skill_excerpt_lines = 12
        helper_skill_excerpt_chars = 1200
        workflow_summary_limit = 3
        resume_journal_last_n = 10
        handoff_stale_after_hours = 24
        handoff_recent_hours = 24
        """,
    )
    _write_config(
        project_root / "aidocs.toml",
        """
        [presentation]
        helper_skill_excerpt_lines = 5
        helper_skill_excerpt_chars = 300
        workflow_summary_limit = 2
        resume_journal_last_n = 4
        handoff_stale_after_hours = 8
        handoff_recent_hours = 6
        """,
    )

    monkeypatch.setenv("AIDOCS_PATH", str(global_root))
    resolver = ConfigResolver()

    assert (
        resolver.get(
            "presentation.helper_skill_excerpt_lines", project_root=project_root
        )
        == 5
    )
    assert (
        resolver.get(
            "presentation.helper_skill_excerpt_chars", project_root=project_root
        )
        == 300
    )
    assert (
        resolver.get("presentation.workflow_summary_limit", project_root=project_root)
        == 2
    )
    assert (
        resolver.get("presentation.resume_journal_last_n", project_root=project_root)
        == 4
    )
    assert (
        resolver.get(
            "presentation.handoff_stale_after_hours", project_root=project_root
        )
        == 8
    )
    assert (
        resolver.get("presentation.handoff_recent_hours", project_root=project_root)
        == 6
    )


def test_user_config_is_found_via_user_config_path_constructor(
    tmp_path: Path,
) -> None:
    user_config_dir = tmp_path / "user-config" / "aidocs"
    _write_config(
        user_config_dir / "aidocs.toml",
        """
        [agent]
        directive_style = "user-global"
        """,
    )

    resolver = ConfigResolver(user_config_path=user_config_dir / "aidocs.toml")
    assert resolver.user_config_path() == user_config_dir / "aidocs.toml"
    assert resolver.get("agent.directive_style") == "user-global"


def test_user_config_discovery_respects_xdg_config_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    xdg_dir = tmp_path / "xdg-config" / "aidocs"
    _write_config(
        xdg_dir / "aidocs.toml",
        """
        [agent]
        directive_style = "xdg-user"
        """,
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    import os
    if os.name == "nt":
        # On Windows, _find_user_config_file checks APPDATA not XDG;
        # test via explicit user_config_path instead
        monkeypatch.setenv("APPDATA", str(tmp_path / "xdg-config"))
        appdata_dir = tmp_path / "xdg-config" / "aidocs"
        _write_config(
            appdata_dir / "aidocs.toml",
            """
            [agent]
            directive_style = "appdata-user"
            """,
        )
        from aidocs_mcp.config import _find_user_config_file
        result = _find_user_config_file()
        assert result is not None
        assert "aidocs.toml" in str(result)
    else:
        from aidocs_mcp.config import _find_user_config_file
        result = _find_user_config_file()
        assert result is not None
        assert result == xdg_dir / "aidocs.toml"


def test_four_layer_merge_order_distribution_user_project_session(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    user_root = tmp_path / "user-config" / "aidocs"
    project_root = tmp_path / "project"
    session_id = "sess-1"

    _write_config(
        dist_root / "aidocs.toml",
        """
        [agent]
        directive_style = "distribution"
        inject_rules_on_bootstrap = true

        [journal]
        max_entries = 50
        """,
    )
    _write_config(
        user_root / "aidocs.toml",
        """
        [agent]
        directive_style = "user"

        [journal]
        max_entries = 75
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
        """,
    )

    resolver = ConfigResolver(
        global_config_path=dist_root / "aidocs.toml",
        user_config_path=user_root / "aidocs.toml",
    )

    effective = resolver.effective_config(
        project_root=project_root, session_id=session_id
    )

    # Session wins for directive_style (most specific layer)
    assert effective["agent"]["directive_style"] == "session"
    # User layer set max_entries=75, no project/session override → user wins
    assert effective["journal"]["max_entries"] == 75
    # Distribution set inject_rules_on_bootstrap, no override above → distribution value preserved
    assert effective["agent"]["inject_rules_on_bootstrap"] is True


def test_distribution_config_path_is_separate_from_user_config_path(
    tmp_path: Path,
) -> None:
    dist_path = tmp_path / "dist" / "aidocs.toml"
    user_path = tmp_path / "user" / "aidocs.toml"
    _write_config(dist_path, "[agent]\ndirective_style = 'dist'")
    _write_config(user_path, "[agent]\ndirective_style = 'user'")

    resolver = ConfigResolver(
        global_config_path=dist_path,
        user_config_path=user_path,
    )

    assert resolver.distribution_config_path() == dist_path
    assert resolver.user_config_path() == user_path
    # global_config_path backward compat prefers user over distribution
    assert resolver.global_config_path() == user_path


def test_config_layer_paths_returns_all_layers(tmp_path: Path) -> None:
    dist_path = tmp_path / "dist" / "aidocs.toml"
    user_path = tmp_path / "user" / "aidocs.toml"
    project_root = tmp_path / "project"
    _write_config(dist_path, "[agent]\ndirective_style = 'dist'")
    _write_config(user_path, "[agent]\ndirective_style = 'user'")
    _write_config(project_root / "aidocs.toml", "[agent]\ndirective_style = 'proj'")

    resolver = ConfigResolver(
        global_config_path=dist_path,
        user_config_path=user_path,
    )
    layers = resolver.config_layer_paths(
        project_root=project_root, session_id="test-sess"
    )

    assert layers["distribution"] == dist_path
    assert layers["user"] == user_path
    assert layers["project"] == project_root / "aidocs.toml"
    assert layers["session"] == (
        project_root / ".MEMORY" / "sessions" / "test-sess" / "aidocs.toml"
    )


def test_write_resolved_config_produces_valid_json(tmp_path: Path) -> None:
    dist_path = tmp_path / "dist" / "aidocs.toml"
    project_root = tmp_path / "project"
    _write_config(dist_path, "[agent]\ndirective_style = 'dist'")
    _write_config(
        project_root / "aidocs.toml", "[agent]\ndirective_style = 'project-val'"
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    resolver = ConfigResolver(global_config_path=dist_path)
    out_path = resolver.write_resolved_config(
        project_root=project_root, session_id="s1"
    )

    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    assert "resolved" in payload
    assert "layers" in payload
    assert "active_layers" in payload
    assert payload["resolved"]["agent"]["directive_style"] == "project-val"
    assert "distribution" in payload["active_layers"]
    assert "project" in payload["active_layers"]


def test_write_resolved_config_includes_session_layer_when_present(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    session_id = "sess-rc"
    _write_config(
        project_root / "aidocs.toml", "[tools]\ntool_call_timeout = 15"
    )
    _write_config(
        project_root / ".MEMORY" / "sessions" / session_id / "aidocs.toml",
        "[tools]\ntool_call_timeout = 5",
    )

    resolver = ConfigResolver()
    out_path = resolver.write_resolved_config(
        project_root=project_root, session_id=session_id
    )

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["resolved"]["tools"]["tool_call_timeout"] == 5
    assert "session" in payload["active_layers"]
