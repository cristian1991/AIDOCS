from pathlib import Path

from aidocs_mcp.claude_hook import ClaudeHookHandler


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
    for name in ["index.aidocs", "global-instructions.aidocs", "coding-standards.aidocs", "memory-system.aidocs"]:
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


def test_user_prompt_submit_blocks_when_project_is_unmanaged(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    handler.runtime.hub.sessions.create_session(project_root, "2026-03-24-a", "A", "Agent", "Goal A")

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


def test_user_prompt_submit_adds_context_when_project_is_managed(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    (project_root / ".MEMORY" / "rules").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "rules" / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n## Workflow Actions\n- ci_status: check GitHub workflow status\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY" / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n## Workflow Rules\n- After push, ci_status.\n",
        encoding="utf-8",
    )
    handler.runtime.hub.sessions.create_session(project_root, "2026-03-24-a", "A", "Agent", "Goal A")
    handler.runtime.hub.sessions.context_file(project_root, "2026-03-24-a").write_text(
        "# Context\n\n## Relevant Files\n- `src/app.py`\n", encoding="utf-8"
    )
    handler.runtime.hub.workflow.compile_project_rules(project_root)
    handler.runtime.hub.managed_mode.set_mode(project_root, session_id="2026-03-24-a")

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": "understand the app",
        }
    )

    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "UserPromptSubmit"
    assert "AIDOCS-managed mode is active" in payload["additionalContext"]
    assert "`2026-03-24-a`" in payload["additionalContext"]
    assert "`understand`" in payload["additionalContext"]


def test_pre_tool_use_adds_context_when_project_is_managed(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    (project_root / ".MEMORY" / "rules").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "rules" / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n## Workflow Actions\n- blink: run `python tools/blink.py`\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY" / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n## Workflow Rules\n- After each completed task, blink.\n",
        encoding="utf-8",
    )
    handler.runtime.hub.sessions.create_session(project_root, "2026-03-24-a", "A", "Agent", "Goal A")
    handler.runtime.hub.workflow.compile_project_rules(project_root)
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
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "PreToolUse"
    assert "task_begin" in payload["additionalContext"]
    assert "local_command" in payload["additionalContext"]


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
