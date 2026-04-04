from pathlib import Path

import aidocs_mcp.config as config
from aidocs_mcp.claude_hook import ClaudeHookHandler
from aidocs_mcp.runtime_service import RuntimeService


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


def _seed_project(project_root: Path) -> None:
    mem = project_root / ".MEMORY"
    (mem / ".aidocs").mkdir(parents=True, exist_ok=True)
    for name in [
        "index.aidocs",
        "global-instructions.aidocs",
        "coding-standards.aidocs",
        "memory-system.aidocs",
    ]:
        (mem / ".aidocs" / name).write_text(f"# {name}\n", encoding="utf-8")
    (mem / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")


def _make_handler(tmp_path: Path) -> tuple[ClaudeHookHandler, Path]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    handler = ClaudeHookHandler()
    handler.runtime.hub = handler.runtime.hub.__class__(templates_root=templates)
    handler.runtime = handler.runtime.__class__(handler.runtime.hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    return handler, project_root


def _register_superpowers_provider(
    runtime: RuntimeService, project_root: Path, provider_root: Path
) -> None:
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    skills = {
        "brainstorming": "Imported brainstorming skill.",
        "writing-plans": "Imported planning skill.",
    }
    for skill_name, description in skills.items():
        (provider_root / "skills" / skill_name).mkdir(parents=True, exist_ok=True)
        (provider_root / "skills" / skill_name / "SKILL.md").write_text(
            "---\n"
            f"name: {skill_name}\n"
            f"description: {description}\n"
            "tags: external, provider\n"
            "---\n",
            encoding="utf-8",
        )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )


def test_session_start_guides_initialization_for_plain_project(tmp_path: Path) -> None:
    handler = ClaudeHookHandler()
    project_root = tmp_path / "plain-project"
    project_root.mkdir(parents=True, exist_ok=True)

    result = handler.handle(
        {
            "hook_event_name": "SessionStart",
            "cwd": str(project_root),
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert "/aidocs" in payload["additionalContext"]
    assert "initialize" in payload["additionalContext"].lower()


def test_session_start_guides_session_creation_when_no_sessions(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.project_init(project_root, init_git=False, create_remote=False)

    result = handler.handle(
        {
            "hook_event_name": "SessionStart",
            "cwd": str(project_root),
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert "create a session" in payload["additionalContext"].lower()


def test_session_start_requires_user_choice_when_multiple_sessions_exist(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.project_init(project_root, init_git=False, create_remote=False)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-a", "A", "Agent", "Goal A"
    )
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-b", "B", "Agent", "Goal B"
    )

    result = handler.handle(
        {
            "hook_event_name": "SessionStart",
            "cwd": str(project_root),
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert "ask the user" in payload["additionalContext"].lower()
    assert "which session" in payload["additionalContext"].lower()


def test_session_start_guides_resync_when_indexes_are_stale(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.project_init(project_root, init_git=False, create_remote=False)
    session = handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-c", "C", "Agent", "Goal C"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session.session_id)

    result = handler.handle(
        {
            "hook_event_name": "SessionStart",
            "cwd": str(project_root),
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert "stale" in payload["additionalContext"].lower()
    assert "/aidocs" in payload["additionalContext"]


def test_user_prompt_submit_blocks_when_project_is_unmanaged(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-a", "A", "Agent", "Goal A"
    )

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "fix this bug",
        }
    )

    assert result == {
        "decision": "block",
        "reason": "Run /aidocs first to activate AIDOCS-managed mode for this project.",
    }


def test_user_prompt_submit_adds_context_when_project_is_managed(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text(
        "class App:\n    pass\n", encoding="utf-8"
    )
    (project_root / ".MEMORY" / "rules").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "rules" / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n## Workflow Actions\n- ci_status: check GitHub workflow status\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY" / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n## Workflow Rules\n- After push, ci_status.\n",
        encoding="utf-8",
    )
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-a", "A", "Agent", "Goal A"
    )
    handler.runtime.hub.sessions.context_file(project_root, "2026-03-24-a").write_text(
        "# Context\n\n## Relevant Files\n- `src/app.py`\n", encoding="utf-8"
    )
    handler.runtime.hub.workflow.compile_project_rules(project_root)
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-24-a")

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "investigate the authentication middleware and find where permissions are checked across the codebase",
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "UserPromptSubmit"
    assert "AIDOCS managed" in payload["additionalContext"]
    assert "`2026-03-24-a`" in payload["additionalContext"]
    assert "`understand`" in payload["additionalContext"]


def test_user_prompt_submit_surfaces_imported_skill_state_from_runtime_route(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    session = handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-a", "A", "Agent", "Goal A"
    )
    _register_superpowers_provider(
        handler.runtime, project_root, tmp_path / "superpowers-external"
    )
    handler.runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/brainstorming"]
    )
    handler.runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    handler.runtime.hub.managed_mode.set_mode(
        project_root, session_id=session.session_id
    )

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "brainstorm app ideas for the startup flow and tell me which imported skills AIDOCS wants active before I start changing code",
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "UserPromptSubmit"
    assert "Imported skills" in payload["additionalContext"]
    assert "superpowers_external/brainstorming" in payload["additionalContext"]


def test_user_prompt_submit_uses_live_prompt_activation_from_runtime_host_state(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    session = handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-live", "A", "Agent", "Goal A"
    )
    _register_superpowers_provider(
        handler.runtime, project_root, tmp_path / "superpowers-live"
    )
    handler.runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/brainstorming"]
    )
    handler.runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    handler.runtime.hub.managed_mode.set_mode(
        project_root, session_id=session.session_id
    )

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "write the plan for the startup flow before I change code",
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "UserPromptSubmit"
    assert "Imported skills" not in payload["additionalContext"]
    assert (
        "Runtime-owned workflow capabilities: `planning`."
        in payload["additionalContext"]
    )


def test_user_prompt_submit_surfaces_bundled_helper_skill_guidance(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    session = handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-bundled-guidance", "A", "Agent", "Goal A"
    )
    handler.runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["deep-retrieval"]
    )
    handler.runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    handler.runtime.hub.managed_mode.set_mode(
        project_root, session_id=session.session_id
    )

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "investigate exact method signatures before editing the startup flow",
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert "Active AIDOCS helper skill guidance" in payload["additionalContext"]
    assert "exact signatures" in payload["additionalContext"]


def test_pre_tool_use_blocks_quoted_protected_config_path_in_bash_command(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-quoted-config", "A", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(
        project_root, session_id="2026-03-28-quoted-config"
    )

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Bash",
            "tool_input": {"command": 'cp "aidocs.toml" backup.toml'},
        }
    )

    assert result == {
        "decision": "block",
        "reason": "BLOCKED: Shell command targets protected AIDOCS infrastructure (aidocs.toml).",
    }


def test_pre_tool_use_blocks_quoted_protected_infrastructure_path_in_bash_command(
    monkeypatch, tmp_path: Path
) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-quoted-infra", "A", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(
        project_root, session_id="2026-03-28-quoted-infra"
    )
    monkeypatch.setattr(config, "DEV_MODE", False)

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Bash",
            "tool_input": {"command": 'cp "core/plugins/aidocs.js" /tmp/aidocs.js'},
        }
    )

    assert result == {
        "decision": "block",
        "reason": "BLOCKED: Shell command targets protected AIDOCS infrastructure (core/plugins/aidocs).",
    }


def test_pre_tool_use_blocks_powershell_copy_item_to_protected_config(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-ps-copy-config", "A", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(
        project_root, session_id="2026-03-28-ps-copy-config"
    )

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Bash",
            "tool_input": {"command": 'Copy-Item "aidocs.toml" "backup.toml"'},
        }
    )

    assert result == {
        "decision": "block",
        "reason": "BLOCKED: Shell command targets protected AIDOCS infrastructure (aidocs.toml).",
    }


def test_pre_tool_use_blocks_powershell_remove_item_for_backslash_infrastructure_path(
    monkeypatch, tmp_path: Path
) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-ps-remove-infra", "A", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(
        project_root, session_id="2026-03-28-ps-remove-infra"
    )
    monkeypatch.setattr(config, "DEV_MODE", False)

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Bash",
            "tool_input": {"command": 'Remove-Item "aidocs_mcp\\claude_hook.py"'},
        }
    )

    assert result == {
        "decision": "block",
        "reason": "BLOCKED: Shell command targets protected AIDOCS infrastructure (aidocs_mcp).",
    }


def test_pre_tool_use_blocks_powershell_out_file_for_backslash_infrastructure_path(
    monkeypatch, tmp_path: Path
) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-ps-outfile-infra", "A", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(
        project_root, session_id="2026-03-28-ps-outfile-infra"
    )
    monkeypatch.setattr(config, "DEV_MODE", False)

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Bash",
            "tool_input": {
                "command": '"content" | Out-File "core\\plugins\\aidocs.js"'
            },
        }
    )

    assert result == {
        "decision": "block",
        "reason": "BLOCKED: Shell command targets protected AIDOCS infrastructure (core/plugins/aidocs).",
    }


def test_session_start_surfaces_imported_skill_state_for_selected_session(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    session = handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-a", "A", "Agent", "Goal A"
    )
    _register_superpowers_provider(
        handler.runtime, project_root, tmp_path / "superpowers-startup"
    )
    handler.runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/brainstorming"]
    )
    handler.runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )

    result = handler.handle(
        {
            "hook_event_name": "SessionStart",
            "cwd": str(project_root),
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert "Imported skills" in payload["additionalContext"]
    assert "superpowers_external/brainstorming" in payload["additionalContext"]


def test_pre_tool_use_blocks_read_in_managed_mode(tmp_path: Path) -> None:
    """Raw Read is hard-blocked at Level 1 when managed mode is active."""
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-a", "A", "Agent", "Goal A"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-24-a")

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        }
    )

    assert result is not None
    assert result["decision"] == "block"
    assert "code_get_lines" in result["reason"]


def test_pre_tool_use_blocks_all_raw_file_tools_in_managed_mode(
    tmp_path: Path,
) -> None:
    """Read, Grep, Glob, Edit, Write are all hard-blocked at Level 1."""
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-b", "B", "Agent", "Goal B"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-24-b")

    for tool in ["Read", "Grep", "Glob", "Edit", "Write"]:
        result = handler.handle(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(project_root),
                "tool_name": tool,
                "tool_input": {},
            }
        )
        assert result is not None, f"Expected block for {tool}"
        assert result["decision"] == "block", f"Expected block for {tool}"


def test_pre_tool_use_allows_bash_in_managed_mode(
    tmp_path: Path,
) -> None:
    """Bash is NOT blocked at Level 1 — gating deferred."""
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-24-c", "C", "Agent", "Goal C"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-24-c")

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )

    # Bash should not be blocked (may be None or advisory nudge)
    assert result is None or "decision" not in result


def test_non_aidocs_project_returns_no_hook_output(tmp_path: Path) -> None:
    handler = ClaudeHookHandler()
    project_root = tmp_path / "plain-project"
    project_root.mkdir(parents=True, exist_ok=True)

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "fix this bug",
        }
    )

    assert result is None


# ── Conversational skip tests ────────────────────────────────────────


def test_short_conversational_prompt_skips_directives(tmp_path: Path) -> None:
    """Short prompts without action keywords return None (no directive injection)."""
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-a", "A", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-28-a")

    for prompt in ["ok", "yes", "thanks", "nice!", "all of them :D", "sure", "👍"]:
        result = handler.handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "cwd": str(project_root),
                "prompt": prompt,
            }
        )
        assert result is None, (
            f"Expected None for conversational prompt '{prompt}', got {result}"
        )


def test_short_prompt_with_action_keyword_gets_directives(tmp_path: Path) -> None:
    """Short prompts containing action keywords still get directives."""
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-b", "B", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-28-b")

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "fix the login bug",
        }
    )
    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "code_get_lines" in context
    assert "code_edit_lines" in context
    assert "code_get_outline" not in context


def test_user_prompt_submit_includes_task_complete_followthrough_nudge(
    tmp_path: Path,
) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-04-01-a", "A", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-04-01-a")
    handler.runtime.hub.execution.record_event(
        project_root,
        event_kind="tool_call_completed",
        source_kind="mcp_call",
        session_id="2026-04-01-a",
        capability_name="task_begin",
        action_kind="mcp_tool_call",
        status="completed",
    )
    handler.runtime.hub.execution.record_event(
        project_root,
        event_kind="native_tool_use",
        source_kind="opencode_plugin",
        session_id="2026-04-01-a",
        capability_name="edit",
        action_kind="native_tool",
        status="success",
    )

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "fix the login bug",
        }
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "Lifecycle follow-through" in context
    assert "task_complete" in context


# ── Comment enforcement tests ────────────────────────────────────────


def test_edit_tool_blocked_in_managed_mode(tmp_path: Path) -> None:
    """Raw Edit is hard-blocked at Level 1 — use code_edit_lines instead."""
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-c", "C", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-28-c")

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "src/app.py",
                "old_string": "x",
                "new_string": "y",
            },
        }
    )

    assert result is not None
    assert result["decision"] == "block"
    assert "code_edit_lines" in result["reason"]


def test_non_edit_tool_no_comment_reminder(tmp_path: Path) -> None:
    """PreToolUse on non-edit tools (like Bash) does not inject comment reminder."""
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-d", "D", "Agent", "Goal"
    )
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-28-d")

    result = handler.handle(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project_root),
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )

    # Bash has no MCP alternative and no comment enforcement — should return None
    assert result is None


# ── Rules injection tests ────────────────────────────────────────────


def test_bootstrap_includes_rules_when_configured(tmp_path: Path) -> None:
    """project_bootstrap_or_resume includes rules content when inject_rules_on_bootstrap=True."""
    handler, project_root = _make_handler(tmp_path)

    # Create rules files
    rules_dir = project_root / ".MEMORY" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "workflow.md").write_text(
        "# Workflow\n\n- Use task_begin before edits.\n", encoding="utf-8"
    )
    (rules_dir / "standards.md").write_text(
        "# Standards\n\n- Comments must explain WHY.\n", encoding="utf-8"
    )

    handler.runtime.hub.sessions.create_session(
        project_root, "2026-03-28-e", "E", "Agent", "Goal"
    )

    result = handler.runtime.project_bootstrap_or_resume(
        project_root, session_id="2026-03-28-e"
    )

    assert "rules" in result
    assert "workflow" in result["rules"]
    assert "task_begin" in result["rules"]["workflow"]
    assert "standards" in result["rules"]
    assert "WHY" in result["rules"]["standards"]
