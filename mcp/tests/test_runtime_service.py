from pathlib import Path

from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


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


def test_session_start_requires_selection_when_multiple_active(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.create_session(project_root, "2026-03-23-b", "B", "Agent", "Goal B")

    result = runtime.session_start(project_root, include_code_bundle=False, sync_indexes=False)

    assert result["requires_session_selection"] is True
    assert result["reason"] == "no_unique_active_session"
    assert len(result["sessions"]) == 2


def test_session_start_auto_selects_single_active_session(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.create_session(project_root, "2026-03-23-b", "B", "Agent", "Goal B", status="paused")

    result = runtime.session_start(project_root, include_code_bundle=False, sync_indexes=False)

    assert result["requires_session_selection"] is False
    assert result["selected_session"]["session_id"] == "2026-03-23-a"


def test_session_start_with_explicit_session_returns_context_bundle(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.context_file(project_root, session.session_id).write_text(
        "# Context\n\n## Relevant Files\n- `src/app.py`\n", encoding="utf-8"
    )

    result = runtime.session_start(project_root, session_id="2026-03-23-a", include_code_bundle=True, sync_indexes=True)

    assert result["requires_session_selection"] is False
    assert result["selected_session"]["session_id"] == "2026-03-23-a"
    assert result["code_bundle"]["primary_files"][0]["path"] == "src/app.py"


def test_session_start_returns_handoff_context(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.update_handoff(
        project_root,
        session.session_id,
        {"What Matters Now": ["- Verify that successor agents see this handoff summary at session start."]},
    )

    result = runtime.session_start(project_root, session_id="2026-03-23-a", include_code_bundle=False, sync_indexes=False)

    assert result["handoff"]["sections"]["What Matters Now"][0] == "- Verify that successor agents see this handoff summary at session start."
    assert any("successor agents" in bullet for bullet in result["report"]["bullets"])


def test_session_resume_bundle_combines_session_context_plan_handoff_and_journal(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.update_context(project_root, session.session_id, {"Relevant Files": ["- `src/app.py`"]})
    hub.sessions.update_handoff(project_root, session.session_id, {"What Matters Now": ["- Resume from this point."]})
    hub.sessions.write_journal_entry(project_root, session.session_id, action_kind="edit", intent="Did a thing", outcome="It worked")

    result = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=True, journal_last_n=5)

    assert result["session"]["session_id"] == "2026-03-23-a"
    assert result["context"]["sections"]["Relevant Files"][0] == "- `src/app.py`"
    assert result["plan"]["sections"]["End Goal"][0] == "- Goal A"
    assert result["handoff"]["sections"]["What Matters Now"][0] == "- Resume from this point."
    assert result["handoff_freshness"]["status"] in {"fresh", "unknown"}
    assert result["journal"][0]["action_kind"] == "edit"
    assert result["code_bundle"]["primary_files"][0]["path"] == "src/app.py"


def test_session_resume_bundle_marks_stale_handoff(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.update_handoff(
        project_root,
        session.session_id,
        {"Freshness": ["- Updated 2025-01-01 00:00 manually."]},
    )

    result = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=False)

    assert result["handoff_freshness"]["status"] == "stale"


def test_project_bootstrap_repairs_partial_structure(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    (project_root / ".MEMORY" / ".index").mkdir(parents=True, exist_ok=True)
    (project_root / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")

    result = runtime.project_bootstrap_or_resume(project_root, include_code_bundle=False)

    assert (project_root / ".MEMORY" / "INDEX.md").is_file()
    assert (project_root / ".MEMORY" / ".aidocs" / "index.aidocs").is_file()
    assert result["repaired"] is not None
    assert any("Repaired canonical AIDOCS structure" in bullet for bullet in result["report"]["bullets"])


def test_session_resume_bundle_includes_structured_handoff_steps(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.upsert_handoff_step(project_root, session.session_id, text="Re-test the patient flow", status="reset")

    result = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=False)

    assert result["handoff_steps"][0]["status"] == "reset"
    assert result["handoff_steps"][0]["text"] == "Re-test the patient flow"
    assert result["actionable_handoff_steps"][0]["status"] == "reset"
    assert result["recently_changed_handoff_steps"][0]["status"] == "reset"


def test_session_start_reports_actionable_handoff_steps(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.upsert_handoff_step(project_root, session.session_id, text="Re-open payment flow validation", status="reset")

    result = runtime.session_start(project_root, session_id=session.session_id, include_code_bundle=False, sync_indexes=False)

    assert any("Actionable handoff steps: 1." == bullet for bullet in result["report"]["bullets"])


def test_project_bootstrap_or_resume_requires_setup_when_uninitialized(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"

    result = runtime.project_bootstrap_or_resume(project_root)

    assert result["stage"] == "setup_required"
    assert result["ready"] is False
    assert result["next_step"] == "project_init"


def test_project_bootstrap_or_resume_syncs_and_selects_session(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
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
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.context_file(project_root, "2026-03-23-a").write_text(
        "# Context\n\n## Relevant Files\n- `src/app.py`\n", encoding="utf-8"
    )

    result = runtime.project_bootstrap_or_resume(project_root, include_code_bundle=True)
    status = hub.code.code_status(project_root)

    assert result["initialized"] is True
    assert result["indexes_synced"] is True
    assert result["ready"] is True
    assert result["session"]["selected_session"]["session_id"] == "2026-03-23-a"
    assert result["sync"]["workflow"]["action_count"] == 1
    assert result["sync"]["workflow"]["actions"][0]["kind"] == "github_workflow_check"


def test_collect_pending_workflow_prefers_ordered_rule_steps(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / ".MEMORY" / "rules").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "rules" / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n"
        "## Workflow Actions\n"
        "- ci_status: check github workflow status\n"
        "- repo_status: check git status\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY" / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n"
        "## Workflow Rules\n"
        "- After push, ci_status then repo_status.\n",
        encoding="utf-8",
    )

    hub.workflow.compile_project_rules(project_root)

    summary = runtime._collect_pending_workflow("git_push", project_root)

    assert "after_git_push" in summary
    assert "ci_status then repo_status" in summary


def test_aidocs_orchestrate_uses_session_bundle_by_default(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.context_file(project_root, "2026-03-23-a").write_text(
        "# Context\n\n## Relevant Files\n- `src/app.py`\n", encoding="utf-8"
    )

    result = runtime.aidocs_orchestrate(project_root, user_request="understand app")

    assert result["selected_session_id"] == "2026-03-23-a"
    assert result["managed_mode"]["active"] is True
    assert result["managed_mode"]["session_id"] == "2026-03-23-a"
    assert result["retrieval"]["mode"] == "session_bundle_deferred"
    assert result["retrieval"]["session_target_count"] == 1
    assert result["retrieval"]["memory_structure"]["router_files"] == ["/.MEMORY/.aidocs/index.aidocs", "/.MEMORY/INDEX.md"]
    assert result["retrieval"]["memory_structure"]["sections"][0]["name"] == "sessions"
    assert result["retrieval"]["memory_structure"]["sections"][0]["active_count"] == 1


def test_aidocs_orchestrate_includes_session_bundle_when_requested(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.context_file(project_root, "2026-03-23-a").write_text(
        "# Context\n\n## Relevant Files\n- `src/app.py`\n", encoding="utf-8"
    )

    result = runtime.aidocs_orchestrate(project_root, user_request="understand app", include_code_bundle=True)

    assert result["retrieval"]["mode"] == "session_bundle"
    assert result["retrieval"]["bundle"]["primary_files"][0]["path"] == "src/app.py"


def test_aidocs_orchestrate_summarizes_large_session_targets_by_default(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    relevant_lines = "\n".join(f"- `src/services/service_{index}.cs`" for index in range(12))
    hub.sessions.context_file(project_root, "2026-03-23-a").write_text(
        f"# Context\n\n## Relevant Files\n{relevant_lines}\n",
        encoding="utf-8",
    )

    result = runtime.aidocs_orchestrate(project_root, user_request="understand app")

    assert result["retrieval"]["mode"] == "session_bundle_deferred"
    assert result["retrieval"]["session_target_count"] == 12
    sections = {item["name"]: item for item in result["retrieval"]["memory_structure"]["sections"]}
    assert sections["sessions"]["active_count"] == 1


def test_aidocs_orchestrate_reports_memory_structure_sections(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    (project_root / ".MEMORY" / "rules").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "rules" / "a.md").write_text("- rule\n", encoding="utf-8")
    (project_root / ".MEMORY" / "domains").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "domains" / "billing.md").write_text("# billing\n", encoding="utf-8")
    (project_root / ".MEMORY" / "policy").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "policy" / "workflow.md").write_text("# workflow\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")

    result = runtime.aidocs_orchestrate(project_root, user_request="understand app")

    sections = {item["name"]: item for item in result["retrieval"]["memory_structure"]["sections"]}
    assert sections["rules"]["file_count"] >= 3
    assert "a.md" in sections["rules"]["samples"]
    assert sections["domains"]["file_count"] == 1
    assert sections["policy"]["file_count"] == 1
    assert sections["policy"]["legacy"] is True


def test_aidocs_orchestrate_uses_explicit_targets_when_given(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.code.sync_code_files(project_root)

    result = runtime.aidocs_orchestrate(
        project_root,
        user_request="inspect file",
        action_kind="inspect",
        explicit_targets=["src/app.py"],
    )

    assert result["retrieval"]["mode"] == "explicit_targets_deferred"
    assert result["retrieval"]["targets"] == ["src/app.py"]


def test_aidocs_orchestrate_includes_explicit_target_bundles_when_requested(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.code.sync_code_files(project_root)

    result = runtime.aidocs_orchestrate(
        project_root,
        user_request="inspect file",
        action_kind="inspect",
        explicit_targets=["src/app.py"],
        include_code_bundle=True,
    )

    assert result["retrieval"]["mode"] == "explicit_targets"
    assert result["retrieval"]["bundles"][0]["path"] == "src/app.py"


def test_aidocs_route_prompt_requires_aidocs_when_not_managed(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"

    result = runtime.aidocs_route_prompt(project_root, user_request="fix bug", action_kind="edit")

    assert result["managed_mode"] is False
    assert result["recommended_mcp_flow"] == ["/aidocs"]


def test_aidocs_route_prompt_uses_managed_mode_and_preflight(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.managed_mode.set_mode(project_root, session_id="2026-03-23-a")

    result = runtime.aidocs_route_prompt(project_root, user_request="fix bug", action_kind="edit")

    assert result["managed_mode"] is True
    assert result["requires_session"] is True
    assert result["requires_task_lifecycle"] is True
    assert "aidocs_orchestrate" in result["recommended_mcp_flow"]


def test_classify_prompt_action_uses_deterministic_keyword_rules(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)

    assert runtime.classify_prompt_action("fix the bug")["action_kind"] == "edit"
    assert runtime.classify_prompt_action("why does this field not show in ui?")["action_kind"] == "trace"
    assert runtime.classify_prompt_action("archive finished work")["action_kind"] == "archive"
    assert runtime.classify_prompt_action("read this file", explicit_targets=["src/app.py"])["action_kind"] == "inspect"


def test_aidocs_handle_prompt_requires_entry_when_unmanaged(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"

    result = runtime.aidocs_handle_prompt(project_root, user_request="fix bug", action_kind="edit")

    assert result["handled"] is False
    assert result["mode"] == "requires_aidocs_entry"
    assert result["next_step"] == "/aidocs"


def test_aidocs_handle_prompt_orchestrates_when_managed(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.context_file(project_root, "2026-03-23-a").write_text(
        "# Context\n\n## Relevant Files\n- `src/app.py`\n", encoding="utf-8"
    )
    hub.managed_mode.set_mode(project_root, session_id="2026-03-23-a")

    result = runtime.aidocs_handle_prompt(project_root, user_request="understand app", action_kind="understand")

    assert result["handled"] is True
    assert result["mode"] == "mcp_orchestrated"
    assert result["classification"]["action_kind"] == "understand"
    assert result["orchestration"]["selected_session_id"] == "2026-03-23-a"


def test_project_bootstrap_or_resume_returns_migration_required_for_legacy_project(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    (project_root / ".MEMORY" / "NOW.md").write_text(
        "# NOW\n\n## Goal\n- Migrate this project\n\n## Active\n- Legacy work\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY" / "plans").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY" / "plans" / "migration.md").write_text("# Plan\n", encoding="utf-8")

    result = runtime.project_bootstrap_or_resume(project_root)

    assert result["stage"] == "migration_required"
    assert result["ready"] is False
    assert result["legacy"]["legacy_present"] is True
    assert result["proposal"]["decision_required"] is True


def test_task_begin_update_and_complete(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")

    started = runtime.task_begin(
        project_root,
        session_id="2026-03-23-a",
        goal="Investigate app entry",
        state=["Inspected the main app structure"],
        upcoming=["Read app.py"],
        partial_goals=["Map the app entry path", "Confirm whether edits are needed"],
        end_goal="Understand the entry path well enough to decide whether any code change is needed",
        relevant_files=["src/app.py"],
        relevant_snippets=['class="custom-shell text-red-500"'],
        constraints=["Keep changes minimal"],
        include_code_bundle=True,
    )
    assert started["session"]["sections"]["Goal"][0] == "- Investigate app entry"
    assert started["plan"]["sections"]["Partial Goals"][0] == "- Map the app entry path"
    assert started["plan"]["sections"]["End Goal"][0] == "- Understand the entry path well enough to decide whether any code change is needed"
    assert started["context"]["sections"]["Relevant Files"][0] == "- `src/app.py`"
    assert started["code_bundle"]["primary_files"][0]["path"] == "src/app.py"

    updated = runtime.task_update(
        project_root,
        session_id="2026-03-23-a",
        partial_goals=["Map the app entry path", "Record the final conclusion"],
        blockers=["Waiting for design decision"],
    )
    assert updated["session"]["sections"]["Blockers"][0] == "- Waiting for design decision"
    assert updated["plan"]["sections"]["Partial Goals"][1] == "- Record the final conclusion"

    completed = runtime.task_complete(
        project_root,
        session_id="2026-03-23-a",
        result_summary="Confirmed the entry path and no change was needed.",
    )
    assert completed["session"]["sections"]["Status"][0] == "- done"
    assert any("Confirmed the entry path" in item for item in completed["session"]["sections"]["State"])
    assert any("Completion result:" in item for item in completed["plan"]["sections"]["Validation"])
    assert completed["handoff"]["sections"]["What Was Done"][0] == "- Confirmed the entry path and no change was needed."
