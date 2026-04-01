from pathlib import Path

from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub
from aidocs_mcp.skill_store import SkillStore


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


def _write_project_skill(project_root: Path, name: str, description: str = "Project local skill.") -> None:
    skills_dir = project_root / ".MEMORY" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / f"{name}.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "tags: local\n"
        "---\n",
        encoding="utf-8",
    )


def _make_runtime_with_bundled_session(tmp_path: Path) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session = hub.sessions.create_session(project_root, "session-bundled", "A", "Agent", "Goal A")
    return runtime, project_root, session.session_id


def test_bundled_provider_is_primary_in_registry_listing(tmp_path: Path) -> None:
    store = SkillStore()
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    result = store.list_skills(project_root)

    assert result[0]["provider"] == "aidocs_bundled_superpowers"


def test_bundled_provider_selected_skill_participates_in_runtime_state(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_bundled_session(tmp_path)

    runtime.hub.skills.set_selected_skills(project_root, session_id, ["deep-retrieval"])

    result = runtime.skill_trigger_state(project_root, session_id, intent="deep retrieval")

    assert result["imported_skill_state"]["active_skills"] == ["deep-retrieval"]
    assert result["imported_skill_state"]["provider_states"] == {"aidocs_bundled_superpowers": "compatible"}
    assert result["skills_overview"]["active_skills"] == ["deep-retrieval"]


def test_bundled_skill_names_are_clean_in_default_runtime_state(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_bundled_session(tmp_path)

    runtime.hub.skills.set_selected_skills(project_root, session_id, ["deep-retrieval"])

    result = runtime.host_state(project_root, session_id=session_id, prompt_text="use deep retrieval")

    assert result["skill_state"]["session_snapshot"]["selected_skills"] == ["deep-retrieval"]
    assert result["skill_state"]["session_snapshot"]["active_skills"] == ["deep-retrieval"]
    assert result["inspection_state"]["provider_states"] == {"aidocs_bundled_superpowers": "compatible"}


def test_legacy_bundled_id_does_not_rebind_to_project_local_skill(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_bundled_session(tmp_path)
    _write_project_skill(project_root, "deep-retrieval", description="Project-local collision skill.")

    runtime.hub.skills.set_selected_skills(project_root, session_id, ["aidocs_bundled_superpowers/deep-retrieval"])

    result = runtime.skill_trigger_state(project_root, session_id, intent="deep retrieval")

    assert result["selected_skills"] == ["deep-retrieval"]
    assert result["imported_skill_state"]["active_skills"] == ["deep-retrieval"]
    assert result["triggered"][0]["provider"] == "aidocs_bundled_superpowers"
    assert result["triggered"][0]["skill_id"] == "deep-retrieval"
