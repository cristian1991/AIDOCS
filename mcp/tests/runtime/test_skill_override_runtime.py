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


def _make_runtime(tmp_path: Path) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    _seed_project(project_root)
    session_id = "session-a"
    return runtime, project_root, session_id


def _register_superpowers_provider(
    runtime: RuntimeService, project_root: Path, tmp_path: Path
) -> None:
    provider_root = tmp_path / "superpowers-5-1-0"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        json.dumps({"provider_id": "superpowers_external", "version": "5.1.0"}) + "\n",
        encoding="utf-8",
    )
    skill_definitions = {
        "brainstorming": ("Imported brainstorming skill.", "external, provider"),
        "systematic-debugging": ("Imported debugging skill.", "external, provider"),
        "writing-plans": ("Imported planning skill.", "external, provider"),
    }
    for skill_name, (description, tags) in skill_definitions.items():
        skill_root = provider_root / "skills" / skill_name
        skill_root.mkdir(parents=True, exist_ok=True)
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: {description}\ntags: {tags}\n---\n",
            encoding="utf-8",
        )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )


def test_runtime_uses_runtime_owned_capability_for_writing_plans(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(tmp_path)
    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root, session_id, ["superpowers_external/writing-plans"]
    )

    result = runtime.skill_trigger_state(project_root, session_id, intent="planning")

    assert result["active_skills"] == []
    assert result["runtime_owned_capabilities"][0]["capability_id"] == "planning"
    assert result["triggered"][0]["provider"] == "superpowers_external"
    assert result["triggered"][0]["runtime_provider"] == "aidocs_runtime"
    assert result["triggered"][0]["override_mode"] == "aidocs_runtime_owned"


def test_runtime_uses_external_brainstorming_content_under_aidocs_runtime_control(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime(tmp_path)
    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root, session_id, ["superpowers_external/brainstorming"]
    )

    result = runtime.skill_trigger_state(
        project_root, session_id, intent="brainstorming"
    )

    assert result["active_skills"] == ["superpowers_external/brainstorming"]
    assert result["triggered"][0]["provider"] == "superpowers_external"
    assert result["triggered"][0]["override_mode"] == "provider_content_aidocs_runtime"
    assert result["triggered"][0]["runtime_provider"] == "aidocs"


def test_runtime_leaves_systematic_debugging_provider_native(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime(tmp_path)
    _register_superpowers_provider(runtime, project_root, tmp_path)
    runtime.hub.skills.set_selected_skills(
        project_root, session_id, ["superpowers_external/systematic-debugging"]
    )

    result = runtime.skill_trigger_state(project_root, session_id, intent="debugging")

    assert result["active_skills"] == ["superpowers_external/systematic-debugging"]
    assert result["triggered"][0]["provider"] == "superpowers_external"
    assert result["triggered"][0]["override_mode"] == "provider_native"
    assert result["triggered"][0]["runtime_provider"] == "superpowers_external"
