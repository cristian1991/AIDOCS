import json
from pathlib import Path

from aidocs_mcp.mcp_server import _annotate_skill_result
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


def _make_runtime(tmp_path: Path) -> tuple[RuntimeService, Path]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    _seed_project(project_root)
    return runtime, project_root


def _register_superpowers_provider(
    runtime: RuntimeService,
    project_root: Path,
    tmp_path: Path,
    *,
    version: str,
    skills: dict[str, tuple[str, str]] | None = None,
) -> None:
    provider_root = tmp_path / f"superpowers-{version.replace('.', '-')}"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        json.dumps({"provider_id": "superpowers_external", "version": version}) + "\n",
        encoding="utf-8",
    )
    skill_definitions = skills or {
        "brainstorming": ("Imported brainstorming skill.", "external, provider"),
        "systematic-debugging": ("Imported debugging skill.", "external, provider"),
        "verification-before-completion": (
            "Imported verification skill.",
            "external, provider",
        ),
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


def _register_external_provider(
    runtime: RuntimeService,
    project_root: Path,
    tmp_path: Path,
    *,
    provider_name: str,
    version: str,
    skills: dict[str, tuple[str, str]],
) -> None:
    provider_root = tmp_path / f"{provider_name}-{version.replace('.', '-')}"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        json.dumps({"provider_id": provider_name, "version": version}) + "\n",
        encoding="utf-8",
    )
    for skill_name, (description, tags) in skills.items():
        skill_root = provider_root / "skills" / skill_name
        skill_root.mkdir(parents=True, exist_ok=True)
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: {description}\ntags: {tags}\n---\n",
            encoding="utf-8",
        )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name=provider_name,
        path=str(provider_root),
    )


def _make_runtime_with_selected_superpowers(
    tmp_path: Path,
) -> tuple[RuntimeService, Path, str]:
    runtime, project_root = _make_runtime(tmp_path)
    session_id = "session-a"
    _register_superpowers_provider(runtime, project_root, tmp_path, version="5.1.0")
    runtime.hub.skills.set_selected_skills(
        project_root,
        session_id,
        [
            "superpowers_external/brainstorming",
            "superpowers_external/systematic-debugging",
            "superpowers_external/verification-before-completion",
        ],
    )
    return runtime, project_root, session_id


def _make_runtime_with_incompatible_superpowers(
    tmp_path: Path,
) -> tuple[RuntimeService, Path, str]:
    runtime, project_root = _make_runtime(tmp_path)
    session_id = "session-a"
    _register_superpowers_provider(runtime, project_root, tmp_path, version="1.2.3")
    return runtime, project_root, session_id


def _patch_bundled_superpowers(
    monkeypatch,
    tmp_path: Path,
    *,
    skills: dict[str, tuple[str, str]],
) -> None:
    bundled_root = tmp_path / "bundled-superpowers"
    bundled_root.mkdir(parents=True, exist_ok=True)
    for skill_name, (description, tags) in skills.items():
        (bundled_root / f"{skill_name}.md").write_text(
            f"---\nname: {skill_name}\ndescription: {description}\ntags: {tags}\n---\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(SkillStore, "_built_in_dir", lambda self: bundled_root)


def test_runtime_selects_external_brainstorming_for_creative_task(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )

    result = runtime.skill_trigger_state(
        project_root, session_id, intent="brainstorming"
    )

    assert "superpowers_external/brainstorming" in result["active_skills"]


def test_runtime_selects_external_debugging_for_bug_work(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )

    result = runtime.skill_trigger_state(project_root, session_id, intent="debugging")

    assert "superpowers_external/systematic-debugging" in result["active_skills"]


def test_incompatible_provider_is_not_auto_activated_without_override(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_incompatible_superpowers(
        tmp_path
    )

    result = runtime.skill_trigger_state(project_root, session_id, intent="planning")

    assert result["active_skills"] == []


def test_runtime_logs_why_external_skill_was_triggered(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )

    result = runtime.skill_trigger_state(
        project_root, session_id, intent="verification"
    )

    assert result["triggered"][0]["provider"] == "superpowers_external"
    assert result["triggered"][0]["override_mode"] == "runtime_owned"
    assert result["triggered"][0]["why"]


def test_selected_skills_rank_ahead_of_compatible_unselected_auto_triggered_skills(
    tmp_path: Path,
) -> None:
    runtime, project_root = _make_runtime(tmp_path)
    session_id = "session-a"
    _register_superpowers_provider(
        runtime,
        project_root,
        tmp_path,
        version="5.1.0",
        skills={
            "brainstorming": ("Imported brainstorming skill.", "external, provider"),
            "creative-helper": (
                "Imported creative helper.",
                "external, provider, brainstorming",
            ),
        },
    )
    runtime.hub.skills.set_selected_skills(
        project_root, session_id, ["superpowers_external/brainstorming"]
    )

    result = runtime.skill_trigger_state(
        project_root, session_id, intent="brainstorming"
    )

    assert result["active_skills"] == ["superpowers_external/brainstorming"]


def test_compatible_unselected_imported_skill_can_auto_trigger_from_intent(
    tmp_path: Path,
) -> None:
    runtime, project_root = _make_runtime(tmp_path)
    session_id = "session-a"
    _register_superpowers_provider(
        runtime,
        project_root,
        tmp_path,
        version="5.1.0",
        skills={
            "creative-helper": (
                "Imported creative helper.",
                "external, provider, brainstorming",
            ),
        },
    )

    result = runtime.skill_trigger_state(
        project_root, session_id, intent="brainstorming"
    )

    assert result["active_skills"] == ["superpowers_external/creative-helper"]


def test_provider_qualified_legacy_id_migrates_to_canonical_name(
    tmp_path: Path,
) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(
        tmp_path
    )
    runtime.hub.skills.set_selected_skills(
        project_root, session_id, ["aidocs_bundled_superpowers/deep-retrieval"]
    )

    result = runtime.skill_trigger_state(
        project_root, session_id, intent="deep retrieval"
    )
    stored = runtime.hub.skills.get_selected_skills(project_root, session_id)

    assert result["selected_skills"] == ["deep-retrieval"]
    assert result["imported_skill_state"]["selected_skills"] == ["deep-retrieval"]
    assert result["imported_skill_state"]["active_skills"] == ["deep-retrieval"]
    assert result["triggered"][0]["skill_id"] == "deep-retrieval"
    assert result["triggered"][0]["provider"] == "aidocs_bundled_superpowers"
    assert stored["selected_skills"] == ["deep-retrieval"]


def test_bundled_writing_plans_becomes_runtime_owned_capability(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_bundled_superpowers(
        monkeypatch,
        tmp_path,
        skills={
            "writing-plans": ("Bundled planning skill.", "external, provider"),
        },
    )
    runtime, project_root = _make_runtime(tmp_path)
    session_id = "session-a"
    runtime.hub.skills.set_selected_skills(project_root, session_id, ["writing-plans"])

    result = runtime.skill_trigger_state(project_root, session_id, intent="planning")
    annotated = _annotate_skill_result(result, override_store=runtime._skill_overrides)

    assert result["active_skills"] == []
    assert result["triggered"][0]["skill_id"] == "writing-plans"
    assert result["triggered"][0]["provider"] == "aidocs_bundled_superpowers"
    assert result["triggered"][0]["runtime_provider"] == "aidocs_runtime"
    assert result["triggered"][0]["override_mode"] == "runtime_owned"
    assert result["runtime_owned_capabilities"][0]["capability_id"] == "planning"
    assert result["imported_skill_state"]["mode_metadata"]["selected_skill_modes"] == {
        "writing-plans": "runtime_owned"
    }
    assert annotated["override_modes"] == {}


def test_bundled_brainstorming_still_uses_provider_content_runtime_control(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_bundled_superpowers(
        monkeypatch,
        tmp_path,
        skills={
            "brainstorming": ("Bundled brainstorming skill.", "external, provider"),
        },
    )
    runtime, project_root = _make_runtime(tmp_path)
    session_id = "session-a"
    runtime.hub.skills.set_selected_skills(project_root, session_id, ["brainstorming"])

    result = runtime.skill_trigger_state(
        project_root, session_id, intent="brainstorming"
    )
    annotated = _annotate_skill_result(result, override_store=runtime._skill_overrides)

    assert result["active_skills"] == ["brainstorming"]
    assert result["triggered"][0]["skill_id"] == "brainstorming"
    assert result["triggered"][0]["provider"] == "aidocs_bundled_superpowers"
    assert result["triggered"][0]["runtime_provider"] == "aidocs"
    assert result["triggered"][0]["override_mode"] == "provider_content_aidocs_runtime"
    assert result["imported_skill_state"]["mode_metadata"]["active_skill_modes"] == {
        "brainstorming": "provider_content_aidocs_runtime"
    }
    assert annotated["override_modes"] == {
        "brainstorming": "provider_content_aidocs_runtime"
    }


def test_bundled_systematic_debugging_remains_provider_native(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_bundled_superpowers(
        monkeypatch,
        tmp_path,
        skills={
            "systematic-debugging": ("Bundled debugging skill.", "external, provider"),
        },
    )
    runtime, project_root = _make_runtime(tmp_path)
    session_id = "session-a"
    runtime.hub.skills.set_selected_skills(
        project_root, session_id, ["systematic-debugging"]
    )

    result = runtime.skill_trigger_state(project_root, session_id, intent="debugging")
    annotated = _annotate_skill_result(result, override_store=runtime._skill_overrides)

    assert result["active_skills"] == ["systematic-debugging"]
    assert result["triggered"][0]["skill_id"] == "systematic-debugging"
    assert result["triggered"][0]["provider"] == "aidocs_bundled_superpowers"
    assert result["triggered"][0]["runtime_provider"] == "aidocs_bundled_superpowers"
    assert result["triggered"][0]["override_mode"] == "provider_native"
    assert result["imported_skill_state"]["mode_metadata"]["active_skill_modes"] == {
        "systematic-debugging": "provider_native"
    }
    assert annotated["override_modes"] == {"systematic-debugging": "provider_native"}


def test_bundled_skill_mode_metadata_prefers_canonical_selected_skill_over_provider_leaf_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_bundled_superpowers(
        monkeypatch,
        tmp_path,
        skills={
            "brainstorming": ("Bundled brainstorming skill.", "external, provider"),
        },
    )
    runtime, project_root = _make_runtime(tmp_path)
    session_id = "session-a"
    _register_external_provider(
        runtime,
        project_root,
        tmp_path,
        provider_name="other_external",
        version="5.1.0",
        skills={
            "brainstorming": (
                "Other provider brainstorming skill.",
                "external, provider",
            ),
        },
    )
    runtime.hub.skills.set_selected_skills(
        project_root,
        session_id,
        ["other_external/brainstorming", "brainstorming"],
    )

    result = runtime.skill_trigger_state(
        project_root, session_id, intent="brainstorming"
    )
    annotated = _annotate_skill_result(result, override_store=runtime._skill_overrides)

    bundled_decision = next(
        item for item in result["triggered"] if item["skill_id"] == "brainstorming"
    )
    bundled_mode_decision = next(
        item
        for item in result["imported_skill_state"]["mode_metadata"]["decisions"]
        if item["skill_id"] == "brainstorming"
    )

    assert bundled_decision["provider"] == "aidocs_bundled_superpowers"
    assert bundled_mode_decision["selected_skill_id"] == "brainstorming"
    assert result["imported_skill_state"]["mode_metadata"]["selected_skill_modes"] == {
        "brainstorming": "provider_content_aidocs_runtime",
        "other_external/brainstorming": "provider_native",
    }
    assert annotated["imported_skill_state"]["mode_metadata"][
        "selected_skill_modes"
    ] == {
        "brainstorming": "provider_content_aidocs_runtime",
        "other_external/brainstorming": "provider_native",
    }
