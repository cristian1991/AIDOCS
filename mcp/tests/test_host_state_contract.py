import json
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
    (root.parent / "index.aidocs").write_text(
        "# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n", encoding="utf-8"
    )
    (root.parent / "global-instructions.aidocs").write_text(
        "# Global Instructions\n", encoding="utf-8"
    )
    (root.parent / "coding-standards.aidocs").write_text(
        "# Coding Standards\n", encoding="utf-8"
    )
    (root.parent / "memory-system.aidocs").write_text(
        "# Memory System\n", encoding="utf-8"
    )
    (root.parent / "research-safety.aidocs").write_text(
        "# Research Safety\n", encoding="utf-8"
    )
    (root.parent / "personalities").mkdir(parents=True, exist_ok=True)
    (root.parent / "personalities" / "default.aidocs").write_text(
        "# Default Personality\n", encoding="utf-8"
    )
    memory_template = root / "memory"
    memory_template.mkdir(parents=True, exist_ok=True)
    (memory_template / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (memory_template / "rules").mkdir(parents=True, exist_ok=True)
    (memory_template / "rules" / "workflow-rules.md").write_text(
        "# Workflow Rules\n\n## Workflow Rules\n", encoding="utf-8"
    )
    (memory_template / "rules" / "workflow-actions.md").write_text(
        "# Workflow Actions\n\n## Workflow Actions\n", encoding="utf-8"
    )


def _register_superpowers_provider(
    runtime: RuntimeService, project_root: Path, provider_root: Path
) -> None:
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        json.dumps({"provider_id": "superpowers_external", "version": "5.1.0"}) + "\n",
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


def _make_runtime_with_selected_superpowers(
    tmp_path: Path,
) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    session = runtime.hub.sessions.create_session(
        project_root, "session-a", "A", "Agent", "Goal A"
    )
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    _register_superpowers_provider(
        runtime, project_root, tmp_path / "superpowers-external"
    )
    runtime.hub.skills.set_selected_skills(
        project_root, session.session_id, ["superpowers_external/brainstorming"]
    )
    runtime.project_bootstrap_or_resume(
        project_root,
        session_id=session.session_id,
        include_code_bundle=False,
        include_tests=False,
    )
    return runtime, project_root, session.session_id


def test_runtime_host_state_contract_contains_session_skill_prompt_and_inspection_sections(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )

    result = runtime.host_state(
        project_root, session_id=session_id, prompt_text="brainstorm the issue"
    )

    assert set(result.keys()) >= {
        "session_state",
        "skill_state",
        "prompt_state",
        "inspection_state",
        "host_actions",
    }
    assert result["session_state"]["session_id"] == session_id
    assert set(result["skill_state"].keys()) == {
        "session_snapshot",
        "prompt_activation",
    }
    assert result["skill_state"]["session_snapshot"]["source"] == "cached_session"
    assert result["skill_state"]["prompt_activation"]["source"] == "live_prompt"
    assert result["prompt_state"]["intent"] == "brainstorming"
    assert result["prompt_state"]["source"] == "live_prompt"
    assert result["prompt_state"]["override_modes"] == {
        "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
    }


def test_prompt_state_is_live_and_not_sourced_from_cached_snapshot(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )

    first = runtime.host_state(
        project_root, session_id=session_id, prompt_text="brainstorm a feature"
    )
    snapshot_path = runtime._host_skill_state_path(project_root, session_id)
    snapshot_path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "intent": "brainstorming",
                "workflow_state": "session_start",
                "selected_skills": ["superpowers_external/brainstorming"],
                "active_skills": ["superpowers_external/brainstorming"],
                "provider_states": {"superpowers_external": "compatible"},
                "provider_state": "compatible",
                "triggered": [{"skill_id": "superpowers_external/brainstorming"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    second = runtime.host_state(
        project_root,
        session_id=session_id,
        prompt_text="summarize the current repository status",
    )

    assert first["prompt_state"] != second["prompt_state"]
    assert second["prompt_state"]["source"] == "live_prompt"
    assert second["prompt_state"]["intent"] != "brainstorming"
    assert second["prompt_state"]["activation_succeeded"] is False
    assert second["prompt_state"]["override_modes"] == {}


def test_session_state_can_be_cached_without_affecting_prompt_state(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )

    startup = runtime.session_start_state(project_root, session_id=session_id)
    snapshot_path = Path(startup["imported_skill_state"]["path"])
    snapshot_path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "intent": "startup",
                "workflow_state": "session_start",
                "selected_skills": ["superpowers_external/brainstorming"],
                "active_skills": ["superpowers_external/brainstorming"],
                "provider_states": {"superpowers_external": "compatible"},
                "provider_state": "compatible",
                "triggered": [{"skill_id": "superpowers_external/brainstorming"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = runtime.host_state(
        project_root, session_id=session_id, prompt_text="write the plan"
    )

    assert result["session_state"]["session_id"] == session_id
    assert result["prompt_state"]["intent"] == "planning"
    assert result["prompt_state"]["source"] == "live_prompt"
    assert result["skill_state"]["session_snapshot"]["selected_skills"] == [
        "superpowers_external/brainstorming"
    ]
    assert result["skill_state"]["session_snapshot"]["mode_metadata"][
        "selected_skill_modes"
    ] == {"superpowers_external/brainstorming": "provider_content_aidocs_runtime"}
    assert result["skill_state"]["prompt_activation"]["source"] == "live_prompt"
    assert result["skill_state"]["prompt_activation"]["active_skills"] == []
    assert result["skill_state"]["prompt_activation"]["mode_metadata"] is None
    assert result["prompt_state"]["override_modes"] == {}
    assert (
        result["prompt_state"]["runtime_owned_capabilities"][0]["capability_id"]
        == "planning"
    )


def test_prompt_state_no_match_leaves_activation_succeeded_false(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )

    result = runtime.host_state(
        project_root,
        session_id=session_id,
        prompt_text="summarize the current repository status",
    )

    assert result["prompt_state"]["intent"] == "understand"
    assert result["prompt_state"]["triggered_skills"] == []
    assert result["prompt_state"]["override_modes"] == {}
    assert result["prompt_state"]["activation_succeeded"] is False
