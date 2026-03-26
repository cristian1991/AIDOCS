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


# ── select_session tests ─────────────────────────────────────────────


def test_select_session_returns_summary_fields(tmp_path: Path) -> None:
    """select_session returns a SessionSummary with correct fields from SESSION.md."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-25-test", "Test Session", "user", "Fix bugs")
    summary = store.select_session(project_root, "2026-03-25-test")

    assert summary.session_id == "2026-03-25-test"
    assert summary.title == "Test Session"
    assert summary.owner == "user"
    assert summary.goal == "Fix bugs"
    assert summary.status == "active"
    assert summary.path is not None


# ── Session claim lifecycle tests ────────────────────────────────────


def test_claim_session_adds_agent_claim(tmp_path: Path) -> None:
    """claim_session adds a claim entry to SESSION.md."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-25-claims", "Claim Test", "user", "Test claims")
    store.claim_session(project_root, "2026-03-25-claims", agent_id="agent-1", run_id="run-001")

    claims = store.list_claims(project_root, "2026-03-25-claims")
    assert len(claims) == 1
    assert claims[0]["agent_id"] == "agent-1"
    assert claims[0]["run_id"] == "run-001"
    assert claims[0]["mode"] == "active"
    assert claims[0]["stale"] is False


def test_claim_session_replaces_existing_claim_same_agent_run(tmp_path: Path) -> None:
    """Re-claiming with the same agent+run updates the timestamp, doesn't duplicate."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-25-reclaim", "Reclaim", "user", "Test")
    store.claim_session(project_root, "2026-03-25-reclaim", agent_id="agent-1", run_id="run-001")
    store.claim_session(project_root, "2026-03-25-reclaim", agent_id="agent-1", run_id="run-001", mode="idle")

    claims = store.list_claims(project_root, "2026-03-25-reclaim")
    assert len(claims) == 1
    assert claims[0]["mode"] == "idle"


def test_claim_session_multiple_agents(tmp_path: Path) -> None:
    """Multiple agents can claim the same session concurrently."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-25-multi", "Multi", "user", "Test")
    store.claim_session(project_root, "2026-03-25-multi", agent_id="agent-1", run_id="run-001")
    store.claim_session(project_root, "2026-03-25-multi", agent_id="agent-2", run_id="run-002")

    claims = store.list_claims(project_root, "2026-03-25-multi")
    agent_ids = {c["agent_id"] for c in claims}
    assert agent_ids == {"agent-1", "agent-2"}


def test_release_claim_removes_specific_agent(tmp_path: Path) -> None:
    """release_claim removes only the specified agent's claim."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-25-release", "Release", "user", "Test")
    store.claim_session(project_root, "2026-03-25-release", agent_id="agent-1", run_id="run-001")
    store.claim_session(project_root, "2026-03-25-release", agent_id="agent-2", run_id="run-002")

    store.release_claim(project_root, "2026-03-25-release", agent_id="agent-1")

    claims = store.list_claims(project_root, "2026-03-25-release")
    assert len(claims) == 1
    assert claims[0]["agent_id"] == "agent-2"


def test_release_claim_by_run_id(tmp_path: Path) -> None:
    """release_claim with run_id only removes that specific run."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-25-runid", "RunID", "user", "Test")
    store.claim_session(project_root, "2026-03-25-runid", agent_id="agent-1", run_id="run-001")
    store.claim_session(project_root, "2026-03-25-runid", agent_id="agent-1", run_id="run-002")

    store.release_claim(project_root, "2026-03-25-runid", agent_id="agent-1", run_id="run-001")

    claims = store.list_claims(project_root, "2026-03-25-runid")
    assert len(claims) == 1
    assert claims[0]["run_id"] == "run-002"


def test_prune_stale_claims_removes_old_entries(tmp_path: Path) -> None:
    """prune_stale_claims removes claims older than the threshold."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-25-prune", "Prune", "user", "Test")
    store.claim_session(project_root, "2026-03-25-prune", agent_id="agent-1", run_id="run-001")

    # Manually backdate the claim to make it stale
    session_file = store.session_file(project_root, "2026-03-25-prune")
    text = session_file.read_text(encoding="utf-8")
    # Replace the timestamp in the claim line with one from an hour ago
    import re
    from datetime import datetime, timedelta
    old_time = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    # Claim format: `agent-1 | run-001 | active | 2026-03-26 01:55`
    text = re.sub(r"(\| active \| )\d{4}-\d{2}-\d{2} \d{2}:\d{2}", rf"\g<1>{old_time}", text)
    session_file.write_text(text, encoding="utf-8")

    # Prune with 30-minute threshold — the claim should be stale
    store.prune_stale_claims(project_root, "2026-03-25-prune", stale_after_minutes=30)

    claims = store.list_claims(project_root, "2026-03-25-prune")
    assert len(claims) == 0


def test_list_claims_empty_session(tmp_path: Path) -> None:
    """list_claims on a fresh session with no claims returns empty list."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-25-empty", "Empty", "user", "Test")

    claims = store.list_claims(project_root, "2026-03-25-empty")
    assert claims == []


# ── Session journal tests ────────────────────────────────────────────


def test_journal_write_and_read(tmp_path: Path) -> None:
    """Writing a journal entry and reading it back works."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-26-journal", "Journal Test", "user", "Test journal")
    result = store.write_journal_entry(
        project_root, "2026-03-26-journal",
        action_kind="edit",
        intent="Fix the CSS bug on the form page",
        outcome="Fixed field-input class in forms.css",
    )

    assert result["logged"] is True
    entries = store.read_journal(project_root, "2026-03-26-journal")
    assert len(entries) == 1
    assert entries[0]["action_kind"] == "edit"
    assert "CSS bug" in entries[0]["intent"]
    assert "field-input" in entries[0]["outcome"]


def test_journal_skips_trivial_actions(tmp_path: Path) -> None:
    """Trivial actions like task_begin are not journaled."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-26-trivial", "Trivial", "user", "Test")
    result = store.write_journal_entry(
        project_root, "2026-03-26-trivial",
        action_kind="task_begin",
        intent="Starting work on the feature",
        outcome="began",
    )

    assert result["logged"] is False
    assert "trivial" in result["reason"]
    entries = store.read_journal(project_root, "2026-03-26-trivial")
    assert len(entries) == 0


def test_journal_skips_short_intents(tmp_path: Path) -> None:
    """Very short intents (greetings, etc.) are not journaled."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-26-short", "Short", "user", "Test")
    result = store.write_journal_entry(
        project_root, "2026-03-26-short",
        action_kind="edit",
        intent="hello",
        outcome="greeted",
    )

    assert result["logged"] is False
    assert "too short" in result["reason"]


def test_journal_evicts_oldest_when_full(tmp_path: Path) -> None:
    """When journal reaches max entries, oldest are archived."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-26-evict", "Evict", "user", "Test")

    # Write 25 entries with max_entries=20
    for i in range(25):
        store.write_journal_entry(
            project_root, "2026-03-26-evict",
            action_kind="edit",
            intent=f"Task number {i}: fix something in the codebase",
            outcome=f"Fixed issue {i}",
            max_entries=20,
        )

    entries = store.read_journal(project_root, "2026-03-26-evict")
    assert len(entries) <= 20

    # Archive should exist with evicted entries
    archive = store.journal_archive_path(project_root, "2026-03-26-evict")
    assert archive.is_file()
    archive_text = archive.read_text(encoding="utf-8")
    assert "Task number 0" in archive_text  # oldest entry was evicted


def test_journal_read_last_n(tmp_path: Path) -> None:
    """read_journal with last_n returns only the most recent entries."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-26-lastn", "LastN", "user", "Test")
    for i in range(10):
        store.write_journal_entry(
            project_root, "2026-03-26-lastn",
            action_kind="edit",
            intent=f"Task number {i}: do something meaningful in the project",
            outcome=f"Done {i}",
        )

    last_3 = store.read_journal(project_root, "2026-03-26-lastn", last_n=3)
    assert len(last_3) == 3
    assert "Task number 9" in last_3[-1]["intent"]
    assert "Task number 7" in last_3[0]["intent"]


def test_journal_empty_session(tmp_path: Path) -> None:
    """read_journal on a session with no journal returns empty list."""
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project_root = tmp_path / "project"

    store.create_session(project_root, "2026-03-26-nolog", "NoLog", "user", "Test")
    entries = store.read_journal(project_root, "2026-03-26-nolog")
    assert entries == []


def test_journal_task_complete_auto_logs(tmp_path: Path) -> None:
    """task_complete in runtime auto-logs a journal entry."""
    templates = tmp_path / "templates"
    _write_templates(templates)

    from aidocs_mcp.service_hub import AidocsServiceHub
    from aidocs_mcp.runtime_service import RuntimeService

    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub=hub)
    project_root = tmp_path / "project"

    hub.sessions.create_session(project_root, "2026-03-26-auto", "Auto", "user", "Test auto journal")
    runtime.task_complete(
        project_root, "2026-03-26-auto",
        result_summary="Fixed the authorization middleware bug",
    )

    entries = hub.sessions.read_journal(project_root, "2026-03-26-auto")
    assert len(entries) == 1
    assert entries[0]["action_kind"] == "task_complete"
    assert "authorization" in entries[0]["intent"]
