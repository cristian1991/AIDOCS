from pathlib import Path

from aidocs_mcp.session_store import SessionStore


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n- active\n\n"
        "## Status\n- active\n\n"
        "## Owner\n- agent\n\n"
        "## Goal\n- goal\n\n"
        "## Scope\n- scope\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- 2026-03-24 00:00\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text("# Context\n", encoding="utf-8")


def test_session_code_targets_ignores_multiline_backtick_blocks(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-24-a", "A", "Agent", "Goal")
    plan_path = project_root / ".MEMORY" / "sessions" / "2026-03-24-a" / "plans" / "test.md"
    plan_path.write_text(
        "# Plan\n\n"
        "Use `src/app.py`.\n\n"
        "```text\n"
        "src/one.cs\n"
        "src/two.cs\n"
        "```\n",
        encoding="utf-8",
    )

    targets = store.session_code_targets(project_root, "2026-03-24-a")

    assert targets == ["src/app.py", "src/one.cs", "src/two.cs"]
