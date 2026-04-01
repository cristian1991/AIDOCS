import asyncio
import json
import os
from pathlib import Path

from aidocs_mcp.mcp_server import create_server
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
    (root.parent / "index.aidocs").write_text("# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n", encoding="utf-8")
    (root.parent / "global-instructions.aidocs").write_text("# Global Instructions\n", encoding="utf-8")
    (root.parent / "coding-standards.aidocs").write_text("# Coding Standards\n", encoding="utf-8")
    (root.parent / "memory-system.aidocs").write_text("# Memory System\n", encoding="utf-8")
    (root.parent / "research-safety.aidocs").write_text("# Research Safety\n", encoding="utf-8")
    (root.parent / "personalities").mkdir(parents=True, exist_ok=True)
    (root.parent / "personalities" / "default.aidocs").write_text("# Default Personality\n", encoding="utf-8")
    memory_template = root / "memory"
    memory_template.mkdir(parents=True, exist_ok=True)
    (memory_template / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (memory_template / "rules").mkdir(parents=True, exist_ok=True)
    (memory_template / "rules" / "workflow-rules.md").write_text("# Workflow Rules\n\n## Workflow Rules\n", encoding="utf-8")
    (memory_template / "rules" / "workflow-actions.md").write_text("# Workflow Actions\n\n## Workflow Actions\n", encoding="utf-8")


def _seed_project(project_root: Path) -> None:
    mem = project_root / ".MEMORY"
    (mem / ".aidocs").mkdir(parents=True, exist_ok=True)
    for name in ["index.aidocs", "global-instructions.aidocs", "coding-standards.aidocs", "memory-system.aidocs"]:
        (mem / ".aidocs" / name).write_text(f"# {name}\n", encoding="utf-8")
    (mem / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")


def test_project_init_copies_full_local_aidocs_bundle(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"

    result = runtime.project_init(project_root, init_git=False, create_remote=False)

    assert result["initialized"] is True
    assert (project_root / ".MEMORY" / ".aidocs" / "index.aidocs").is_file()
    assert (project_root / ".MEMORY" / ".aidocs" / "global-instructions.aidocs").is_file()
    assert (project_root / ".MEMORY" / ".aidocs" / "coding-standards.aidocs").is_file()
    assert (project_root / ".MEMORY" / ".aidocs" / "memory-system.aidocs").is_file()
    assert (project_root / ".MEMORY" / ".aidocs" / "research-safety.aidocs").is_file()
    assert (project_root / ".MEMORY" / ".aidocs" / "personalities" / "default.aidocs").is_file()
    assert not (project_root / ".MEMORY" / ".aidocs" / "templates").exists()



def test_startup_state_requires_session_creation_when_no_sessions(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"

    runtime.project_init(project_root, init_git=False, create_remote=False)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")

    state = runtime.session_start_state(project_root)

    assert state["state"] == "no_session"
    assert state["next_step"] == "create_session"
    assert state["session_id"] is None


def test_startup_state_marks_missing_indexes_as_stale(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"

    runtime.project_init(project_root, init_git=False, create_remote=False)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.managed_mode.set_mode(project_root, session.session_id)

    state = runtime.session_start_state(project_root)

    assert state["state"] == "stale_indexes"


def test_startup_state_rejects_unknown_explicit_session_id(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"

    runtime.project_init(project_root, init_git=False, create_remote=False)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.managed_mode.set_mode(project_root, session.session_id)

    state = runtime.session_start_state(project_root, session_id="2026-03-23-missing")

    assert state["state"] == "session_not_found"
    assert state["next_step"] == "select_session"
    assert state["session_id"] is None
    assert state["requested_session_id"] == "2026-03-23-missing"
    assert state["index_status"] == "unknown"
    assert state["sessions"][0]["session_id"] == session.session_id



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



def test_session_start_includes_selected_skills(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.skills.set_selected_skills(project_root, session.session_id, ["deep-retrieval"])

    result = runtime.session_start(project_root, session_id=session.session_id, include_code_bundle=False, sync_indexes=False)

    assert result["selected_skills"]["selected_skills"] == ["deep-retrieval"]




def test_session_start_state_cache_does_not_become_prompt_state(tmp_path: Path) -> None:
    templates = tmp_path / "templates-session-host-state"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project-session-host-state"

    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(project_root, "session-host-state", "A", "Agent", "Goal A")
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)

    provider_root = tmp_path / "superpowers-external-session-host-state"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    (provider_root / "skills" / "brainstorming").mkdir(parents=True, exist_ok=True)
    (provider_root / "skills" / "brainstorming" / "SKILL.md").write_text(
        "---\n"
        "name: brainstorming\n"
        "description: Imported brainstorming skill.\n"
        "tags: external, provider\n"
        "---\n",
        encoding="utf-8",
    )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )
    runtime.hub.skills.set_selected_skills(project_root, session.session_id, ["superpowers_external/brainstorming"])
    runtime.project_bootstrap_or_resume(project_root, session_id=session.session_id, include_code_bundle=False, include_tests=False)

    startup = runtime.session_start_state(project_root, session_id=session.session_id)
    host_state = runtime.host_state(project_root, session_id=session.session_id, prompt_text="brainstorm the dashboard")

    assert startup["imported_skill_state"]["intent"] == "startup"
    assert host_state["skill_state"]["session_snapshot"]["source"] == "cached_session"
    assert host_state["skill_state"]["session_snapshot"]["mode_metadata"]["selected_skill_modes"] == {
        "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
    }
    assert host_state["skill_state"]["prompt_activation"]["source"] == "live_prompt"
    assert host_state["skill_state"]["prompt_activation"]["mode_metadata"]["active_skill_modes"] == {
        "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
    }
    assert host_state["prompt_state"]["intent"] == "brainstorming"
    assert host_state["prompt_state"]["source"] == "live_prompt"


def test_session_start_includes_active_imported_skills(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    provider_root = tmp_path / "superpowers-external"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    (provider_root / "skills" / "startup-helper").mkdir(parents=True, exist_ok=True)
    (provider_root / "skills" / "startup-helper" / "SKILL.md").write_text(
        "---\n"
        "name: startup-helper\n"
        "description: Imported startup helper.\n"
        "tags: external, provider, session-start\n"
        "---\n",
        encoding="utf-8",
    )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )
    hub.skills.set_selected_skills(project_root, session.session_id, ["superpowers_external/startup-helper"])

    result = runtime.session_start(project_root, session_id=session.session_id, include_code_bundle=False, sync_indexes=False)

    assert result["active_skills"] == ["superpowers_external/startup-helper"]



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
    hub.skills.set_selected_skills(project_root, session.session_id, ["deep-retrieval", "test-driven-validation"])

    result = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=True, journal_last_n=5)

    assert result["session"]["session_id"] == "2026-03-23-a"
    assert result["context"]["sections"]["Relevant Files"][0] == "- `src/app.py`"
    assert result["plan"]["sections"]["End Goal"][0] == "- Goal A"
    assert result["handoff"]["sections"]["What Matters Now"][0] == "- Resume from this point."
    assert result["selected_skills"]["selected_skills"] == ["deep-retrieval", "test-driven-validation"]
    assert result["handoff_freshness"]["status"] in {"fresh", "unknown"}
    assert result["journal"][0]["action_kind"] == "edit"
    assert result["code_bundle"]["primary_files"][0]["path"] == "src/app.py"


def test_session_resume_bundle_includes_overview_payloads(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.update_context(project_root, session.session_id, {"Relevant Files": ["- `src/app.py`"]})
    hub.skills.set_selected_skills(project_root, session.session_id, ["deep-retrieval"])

    result = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=False)

    assert result["project_overview"]["project_name"] == "project"
    assert result["project_overview"]["session_count"] == 1
    assert result["session_overview"]["session_id"] == "2026-03-23-a"
    assert result["session_overview"]["goal"] == "Goal A"
    assert result["session_overview"]["relevant_file_count"] == 1
    assert result["skills_overview"]["selected_skills"] == ["deep-retrieval"]
    assert result["skills_overview"]["active_skills"] == []
    assert result["plan_overview"]["progress"] == "0/0"
    assert result["plan_overview"]["end_goal"] == "Goal A"
    assert result["plan_overview"]["next_step"] is None


def test_session_resume_bundle_preserves_bundled_skill_state(tmp_path: Path) -> None:
    templates = tmp_path / "templates-bundled-resume"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project-bundled-resume"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-bundled", "A", "Agent", "Goal A")
    hub.skills.set_selected_skills(project_root, session.session_id, ["deep-retrieval"])

    result = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=False)

    assert result["selected_skills"]["selected_skills"] == ["deep-retrieval"]
    assert result["imported_skill_state"]["selected_skills"] == ["deep-retrieval"]
    assert result["imported_skill_state"]["active_skills"] == ["deep-retrieval"]
    assert result["imported_skill_state"]["provider_states"] == {"aidocs_bundled_superpowers": "compatible"}
    assert result["skills_overview"]["selected_skills"] == ["deep-retrieval"]
    assert result["skills_overview"]["provider_state"] == "compatible"


def test_session_resume_bundle_preserves_imported_skill_metadata_in_skills_overview(tmp_path: Path) -> None:
    templates = tmp_path / "templates-imported-skills"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project-imported-skills"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")

    provider_root = tmp_path / "superpowers-external-resume"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    (provider_root / "skills" / "brainstorming").mkdir(parents=True, exist_ok=True)
    (provider_root / "skills" / "brainstorming" / "SKILL.md").write_text(
        "---\n"
        "name: brainstorming\n"
        "description: Imported brainstorming skill.\n"
        "tags: external, provider\n"
        "---\n",
        encoding="utf-8",
    )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )
    runtime.hub.skills.set_selected_skills(project_root, session.session_id, ["superpowers_external/brainstorming"])

    result = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=False)

    assert result["selected_skills"]["selected_skills"] == ["superpowers_external/brainstorming"]
    assert result["imported_skill_state"]["selected_skills"] == ["superpowers_external/brainstorming"]
    assert result["imported_skill_state"]["active_skills"] == ["superpowers_external/brainstorming"]
    assert result["imported_skill_state"]["provider_states"] == {"superpowers_external": "compatible"}
    assert result["skills_overview"]["selected_skills"] == ["superpowers_external/brainstorming"]
    assert result["skills_overview"]["provider_state"] == "compatible"
    assert result["skills_overview"]["provider_states"] == {"superpowers_external": "compatible"}


def test_project_bootstrap_or_resume_includes_default_plan_overview(tmp_path: Path) -> None:
    templates = tmp_path / "templates-bootstrap-plan"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project-bootstrap-plan"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")

    result = runtime.project_bootstrap_or_resume(project_root, session_id=session.session_id, include_code_bundle=False)

    assert result["plan_overview"]["session_id"] == session.session_id
    assert result["plan_overview"]["progress"] == "0/0"
    assert result["plan_overview"]["end_goal"] == "Goal A"


def test_plan_connect_fallback_includes_safe_plan_overview(tmp_path: Path) -> None:
    templates = tmp_path / "templates-plan-fallback"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project-plan-fallback"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.plan_file(project_root, session.session_id).unlink()
    hub.sessions.upsert_handoff_step(project_root, session.session_id, text="Follow up on the blocker", status="open")

    result = runtime.plan_connect(project_root, session.session_id, run_preflight=False)

    assert result["plan_source"] == "session_open_work"
    assert result["plan_overview"]["session_id"] == session.session_id
    assert result["plan_overview"]["progress"] == "0/0"
    assert result["plan_overview"]["next_step"] is None


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


def test_session_resume_bundle_includes_compliance_debt_when_work_is_unlogged(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.execution.record_event(project_root, event_kind="tool_call_completed", source_kind="mcp_tool_call", session_id=session.session_id, action_kind="edit", observed_at="2026-03-27 10:00:00")

    result = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=False)

    assert result["compliance"]["logging_debt"] is True
    assert "work occurred after the latest journal entry" in result["compliance"]["warnings"]


def test_session_start_report_surfaces_compliance_warnings(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.execution.record_event(project_root, event_kind="tool_call_completed", source_kind="mcp_tool_call", session_id=session.session_id, action_kind="edit", observed_at="2026-03-27 10:00:00")

    result = runtime.session_start(project_root, session_id=session.session_id, include_code_bundle=False, sync_indexes=False)

    assert any("Compliance: work occurred after the latest journal entry." == bullet for bullet in result["report"]["bullets"])


def test_execution_event_write_does_not_change_code_freshness_state(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    app = project_root / "src" / "app.py"
    app.write_text("def app():\n    return 1\n", encoding="utf-8")

    hub.index.sync_all(project_root)
    hub.code.sync_code_files(project_root)
    status, _ = runtime._index_freshness_status(project_root)
    assert status == "ready"

    app.write_text("def app():\n    return 2\n", encoding="utf-8")
    original_stat = app.stat()
    os.utime(app, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    after_write_status, after_write_details = runtime._index_freshness_status(project_root)
    assert after_write_status == "stale"
    assert "code:content_drift" in after_write_details["reasons"]

    hub.execution.record_event(
        project_root,
        event_kind="tool_call_completed",
        source_kind="mcp_tool_call",
        action_kind="edit",
    )
    after_status, after_details = runtime._index_freshness_status(project_root)

    assert after_status == "stale"
    assert "code:content_drift" in after_details["reasons"]


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


def test_repo_summary_includes_language_tier_and_source_counts(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    (project_root / "index_languages").mkdir(parents=True, exist_ok=True)
    (project_root / "index_languages" / "r.toml").write_text(
        'name = "r"\n'
        'extensions = [".r"]\n'
        'tier = "heuristic"\n',
        encoding="utf-8",
    )
    (project_root / "R").mkdir(parents=True, exist_ok=True)
    (project_root / "R" / "analysis.r").write_text("summary <- function(x) x\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    hub.code.sync_code_files(project_root)
    summary = runtime.repo_summary(project_root)

    assert summary["language_tiers"]["heuristic"] >= 1
    assert summary["language_sources"]["project"] >= 1


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


def test_failed_handoff_steps_reappear_in_open_work(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-failed", "A", "Agent", "Goal A")
    hub.sessions.upsert_handoff_step(project_root, session.session_id, text="Retry the failed migration", status="failed")

    bundle = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=False)
    open_work = runtime._collect_session_open_work(project_root, session.session_id)

    assert bundle["actionable_handoff_steps"][0]["status"] == "failed"
    assert open_work == [{"source": "handoff_step", "status": "failed", "text": "Retry the failed migration"}]



def test_completed_handoff_steps_do_not_reappear_as_open_work(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "2026-03-23-complete", "A", "Agent", "Goal A")
    hub.sessions.upsert_handoff_step(project_root, session.session_id, text="Finish the handoff", status="completed")

    bundle = runtime.session_resume_bundle(project_root, session.session_id, include_code_bundle=False)
    open_work = runtime._collect_session_open_work(project_root, session.session_id)

    assert bundle["handoff_steps"][0]["status"] == "completed"
    assert bundle["actionable_handoff_steps"] == []
    assert open_work == []


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


def test_aidocs_orchestrate_includes_active_imported_skills(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    provider_root = tmp_path / "superpowers-external"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    (provider_root / "skills" / "brainstorming").mkdir(parents=True, exist_ok=True)
    (provider_root / "skills" / "brainstorming" / "SKILL.md").write_text(
        "---\n"
        "name: brainstorming\n"
        "description: Imported brainstorming skill.\n"
        "tags: external, provider\n"
        "---\n",
        encoding="utf-8",
    )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )
    hub.skills.set_selected_skills(project_root, session.session_id, ["superpowers_external/brainstorming"])

    result = runtime.aidocs_orchestrate(project_root, user_request="brainstorm app ideas")

    assert result["active_skills"] == ["superpowers_external/brainstorming"]
    assert result["skill_trigger_state"]["active_skills"] == ["superpowers_external/brainstorming"]


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


def test_aidocs_handle_prompt_surfaces_active_imported_skills(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    provider_root = tmp_path / "superpowers-external"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    (provider_root / "skills" / "creative-helper").mkdir(parents=True, exist_ok=True)
    (provider_root / "skills" / "creative-helper" / "SKILL.md").write_text(
        "---\n"
        "name: creative-helper\n"
        "description: Imported creative helper.\n"
        "tags: external, provider, brainstorming\n"
        "---\n",
        encoding="utf-8",
    )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )
    hub.managed_mode.set_mode(project_root, session_id=session.session_id)

    result = runtime.aidocs_handle_prompt(project_root, user_request="brainstorm app ideas", action_kind="understand")

    assert result["orchestration"]["active_skills"] == ["superpowers_external/creative-helper"]


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
    assert started["session"]["sections"]["Goal"][0] == "- Goal A"
    assert started["plan"]["sections"]["Purpose"][0] == "- Implement the session goal: Goal A"
    assert any("Investigate app entry" in item for item in started["session"]["sections"]["State"])
    assert any("Investigate app entry" in item for item in started["plan"]["sections"]["Current State"])
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
    assert completed["session"]["sections"]["Goal"][0] == "- Goal A"
    assert completed["session"]["sections"]["Status"][0] == "- done"
    assert any("Confirmed the entry path" in item for item in completed["session"]["sections"]["State"])
    assert completed["plan"]["sections"]["Purpose"][0] == "- Implement the session goal: Goal A"
    assert any("Completion result:" in item for item in completed["plan"]["sections"]["Validation"])
    assert completed["handoff"]["sections"]["What Was Done"][0] == "- Confirmed the entry path and no change was needed."


def test_task_begin_and_complete_update_lane_scoped_query_gate_state(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "lane_a.py").write_text("lane-a\n", encoding="utf-8")
    (project_root / "src" / "lane_b.py").write_text("lane-b\n", encoding="utf-8")
    session = hub.sessions.create_session(project_root, "2026-03-23-a", "A", "Agent", "Goal A")
    hub.sessions.plan_file(project_root, session.session_id).write_text(
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared work\n"
        "- Lane: lane-a\n"
        "- Files: src/lane_a.py\n"
        "- [ ] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/lane_b.py\n"
        "- [ ] Build lane b\n",
        encoding="utf-8",
    )

    started = runtime.task_begin(
        project_root,
        session_id=session.session_id,
        goal="Implement lane a",
        relevant_files=["src/lane_a.py"],
        include_code_bundle=False,
    )
    started_gate = hub.query_gate.get(project_root, session.session_id)
    runtime.task_complete(project_root, session.session_id, result_summary="Finished lane a", include_code_bundle=False)
    completed_gate = hub.query_gate.get(project_root, session.session_id)

    assert any("Implement lane a" in item for item in started["session"]["sections"]["State"])
    assert started_gate["current_lane_id"] == "lane-a"
    assert started_gate["lane_exact_paths"] == ["src/lane_a.py"]
    assert completed_gate["current_lane_id"] is None
    assert completed_gate["lane_exact_paths"] == []



def test_plan_conductor_status_returns_lane_graph_and_runnable_state(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(
        project_root,
        "2026-03-30-conductor-status",
        "Conductor Status",
        "user",
        "Inspect conductor state",
    )
    hub.sessions.plan_file(project_root, session.session_id).write_text(
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: homepage-hero\n"
        "- Files: src/components/home/Hero.tsx\n"
        "- [ ] Build hero component\n"
        "- Lane: homepage-shell\n"
        "- Files: src/pages/index.tsx\n"
        "- depends_on: homepage-hero\n"
        "- [ ] Integrate homepage shell\n",
        encoding="utf-8",
    )

    result = runtime.plan_conductor_status(project_root, session.session_id)

    assert result["phase_order"] == ["homepage-foundation"]
    assert [lane["lane_id"] for lane in result["lanes"]] == ["homepage-hero", "homepage-shell"]
    assert result["runnable_lane_ids"] == ["homepage-hero"]
    assert result["waiting_on"] == {"homepage-shell": ["homepage-hero"]}


def test_mcp_tool_returns_conductor_graph(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> dict[str, object]:
        server = create_server()
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-03-30-conductor-graph",
            "Conductor Graph",
            "user",
            "Inspect conductor graph",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Homepage foundation\n"
            "- Lane: homepage-hero\n"
            "- Files: src/components/home/Hero.tsx\n"
            "- [ ] Build hero component\n"
            "- Lane: homepage-shell\n"
            "- Files: src/pages/index.tsx\n"
            "- depends_on: homepage-hero\n"
            "- [ ] Integrate homepage shell\n",
            encoding="utf-8",
        )
        result = await server.call_tool(
            "aidocs_plan_conductor_graph",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return json.loads(payload)

    result = asyncio.run(run())

    assert result["phase_order"] == ["homepage-foundation"]
    assert [lane["lane_id"] for lane in result["lanes"]] == ["homepage-hero", "homepage-shell"]
    assert result["dependencies"] == {"homepage-shell": ["homepage-hero"]}


def test_mcp_tool_returns_conductor_status(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        session = hub.sessions.create_session(
            project_root,
            "2026-03-30-conductor-status-tool",
            "Conductor Status Tool",
            "user",
            "Inspect conductor status",
        )
        hub.sessions.plan_file(project_root, session.session_id).write_text(
            "# Plan\n"
            "\n## Steps\n"
            "- Phase: Homepage foundation\n"
            "- Lane: homepage-hero\n"
            "- Files: src/components/home/Hero.tsx\n"
            "- [ ] Build hero component\n"
            "- Lane: homepage-shell\n"
            "- Files: src/pages/index.tsx\n"
            "- depends_on: homepage-hero\n"
            "- [ ] Integrate homepage shell\n",
            encoding="utf-8",
        )
        result = await server.call_tool(
            "aidocs_plan_conductor_status",
            {
                "project_root": str(project_root),
                "session_id": session.session_id,
            },
        )
        payload = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(payload)

    tool_names, result = asyncio.run(run())

    assert "aidocs_plan_conductor_status" in tool_names
    assert result["phase_order"] == ["homepage-foundation"]
    assert [lane["lane_id"] for lane in result["lanes"]] == ["homepage-hero", "homepage-shell"]
    assert result["runnable_lane_ids"] == ["homepage-hero"]
    assert result["waiting_on"] == {"homepage-shell": ["homepage-hero"]}


def test_mcp_skill_trigger_tool_returns_active_skills(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        runtime = RuntimeService(hub)
        _seed_project(project_root)

        provider_root = tmp_path / "superpowers-external"
        provider_root.mkdir(parents=True, exist_ok=True)
        (provider_root / "provider.json").write_text(
            '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
            encoding="utf-8",
        )
        (provider_root / "skills" / "brainstorming").mkdir(parents=True, exist_ok=True)
        (provider_root / "skills" / "brainstorming" / "SKILL.md").write_text(
            "---\n"
            "name: brainstorming\n"
            "description: Imported brainstorming skill.\n"
            "tags: external, provider\n"
            "---\n",
            encoding="utf-8",
        )
        runtime.hub.skills.register_external_provider(
            project_root,
            provider_name="superpowers_external",
            path=str(provider_root),
        )
        runtime.hub.skills.set_selected_skills(project_root, "session-a", ["superpowers_external/brainstorming"])

        result = await server.call_tool(
            "aidocs_skill_trigger_state_get",
            {
                "project_root": str(project_root),
                "session_id": "session-a",
                "intent": "brainstorming",
            },
        )

        text = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(text)

    tool_names, payload = asyncio.run(run())

    assert "aidocs_skill_trigger_state_get" in tool_names
    assert payload["active_skills"] == ["superpowers_external/brainstorming"]
    assert payload["triggered"][0]["override_mode"] == "provider_content_aidocs_runtime"


def test_mcp_skill_trigger_tool_surfaces_override_modes(tmp_path: Path) -> None:
    project_root = tmp_path / "project-override"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        runtime = RuntimeService(hub)
        _seed_project(project_root)

        provider_root = tmp_path / "superpowers-external-override"
        provider_root.mkdir(parents=True, exist_ok=True)
        (provider_root / "provider.json").write_text(
            '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
            encoding="utf-8",
        )
        (provider_root / "skills" / "writing-plans").mkdir(parents=True, exist_ok=True)
        (provider_root / "skills" / "writing-plans" / "SKILL.md").write_text(
            "---\n"
            "name: writing-plans\n"
            "description: Imported planning skill.\n"
            "tags: external, provider\n"
            "---\n",
            encoding="utf-8",
        )
        runtime.hub.skills.register_external_provider(
            project_root,
            provider_name="superpowers_external",
            path=str(provider_root),
        )
        runtime.hub.skills.set_selected_skills(project_root, "session-a", ["superpowers_external/writing-plans"])

        result = await server.call_tool(
            "aidocs_skill_trigger_state_get",
            {
                "project_root": str(project_root),
                "session_id": "session-a",
                "intent": "planning",
            },
        )

        text = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(text)

    tool_names, payload = asyncio.run(run())

    assert "aidocs_skill_trigger_state_get" in tool_names
    assert payload["override_modes"] == {"writing-plans": "aidocs_native_override"}
    assert payload["imported_skill_state"]["mode_metadata"]["active_skill_modes"] == {
        "writing-plans": "aidocs_native_override"
    }


def test_self_edit_mode_is_not_available_in_release_profile() -> None:
    async def run() -> tuple[list[str], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        result = await server.call_tool("aidocs_config_edit_policy_get", {})
        text = result[0].text if isinstance(result, list) else result.content[0].text
        return tool_names, json.loads(text)

    tool_names, payload = asyncio.run(run())

    assert "aidocs_config_edit_policy_get" in tool_names
    assert payload["profile"] == "release"
    assert payload["available_modes"] == ["explicit_user_permitted"]
    assert payload["security"]["self_edit_available"] is False


def test_runtime_can_expose_effective_config_view(tmp_path: Path, monkeypatch) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project-config"
    session_id = "2026-03-31-config"

    global_root = tmp_path / "global-config"
    (global_root / "aidocs.toml").parent.mkdir(parents=True, exist_ok=True)
    (global_root / "aidocs.toml").write_text(
        "[agent]\n"
        'directive_style = "global"\n'
        "inject_rules_on_bootstrap = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIDOCS_PATH", str(global_root))

    runtime.project_init(project_root, init_git=False, create_remote=False)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    (project_root / "aidocs.toml").write_text(
        "[agent]\n"
        'directive_style = "project"\n',
        encoding="utf-8",
    )
    session = hub.sessions.create_session(project_root, session_id, "Config", "Agent", "Check merged config")
    (project_root / ".MEMORY" / "sessions" / session.session_id / "aidocs.toml").write_text(
        "[agent]\n"
        'directive_style = "session"\n'
        'inject_rules_on_bootstrap = "false"\n',
        encoding="utf-8",
    )

    effective = runtime.effective_config(project_root, session_id=session.session_id)
    result = runtime.project_bootstrap_or_resume(project_root, session_id=session.session_id, include_code_bundle=False)

    assert effective["agent"]["directive_style"] == "session"
    assert effective["agent"]["inject_rules_on_bootstrap"] == "false"
    assert "rules" not in result


def test_classify_prompt_action_uses_scoped_language_config(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)

    project_en = tmp_path / "project-en"
    runtime.project_init(project_en, init_git=False, create_remote=False)
    (project_en / "aidocs.toml").write_text(
        "[languages]\n"
        'enabled = "en"\n',
        encoding="utf-8",
    )

    project_es = tmp_path / "project-es"
    runtime.project_init(project_es, init_git=False, create_remote=False)
    (project_es / "aidocs.toml").write_text(
        "[languages]\n"
        'enabled = "es"\n',
        encoding="utf-8",
    )

    assert runtime.classify_prompt_action("arregla esto", project_root=project_en)["action_kind"] == "understand"
    assert runtime.classify_prompt_action("arregla esto", project_root=project_es)["action_kind"] == "edit"


