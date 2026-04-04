import asyncio
import json
from pathlib import Path

import pytest

import aidocs_mcp.skill_store as skill_store_module
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


def _make_runtime(tmp_path: Path) -> tuple[RuntimeService, Path, Path]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    project_root = tmp_path / "project"
    _seed_project(project_root)
    return runtime, project_root, templates


def _register_superpowers_provider(runtime: RuntimeService, project_root: Path, tmp_path: Path, *, version: str) -> None:
    provider_root = tmp_path / "superpowers-external"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        f'{{"provider_id": "superpowers_external", "version": "{version}"}}\n',
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


def _make_runtime_with_incompatible_superpowers(tmp_path: Path) -> tuple[RuntimeService, Path, Path]:
    runtime, project_root, templates = _make_runtime(tmp_path)
    _register_superpowers_provider(runtime, project_root, tmp_path, version="1.2.3")
    return runtime, project_root, templates


def _register_provider_with_skill(
    runtime: RuntimeService,
    project_root: Path,
    tmp_path: Path,
    *,
    provider_id: str,
    version: str,
    skill_name: str,
) -> None:
    provider_root = tmp_path / provider_id
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        json.dumps({"provider_id": provider_id, "version": version}) + "\n",
        encoding="utf-8",
    )
    (provider_root / "skills" / skill_name).mkdir(parents=True, exist_ok=True)
    (provider_root / "skills" / skill_name / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_name}\n"
        f"description: Imported {skill_name} skill.\n"
        "tags: external, provider\n"
        "---\n",
        encoding="utf-8",
    )
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name=provider_id,
        path=str(provider_root),
    )


def test_incompatible_external_provider_reports_versions_and_choices(tmp_path: Path) -> None:
    runtime, project_root, _templates = _make_runtime_with_incompatible_superpowers(tmp_path)

    result = runtime.skill_provider_status(project_root, "superpowers_external")

    assert result["provider_id"] == "superpowers_external"
    assert result["provider_state"] == "detected_incompatible"
    assert result["aidocs_version"]
    assert result["provider_version"] == "1.2.3"
    assert result["compatible_versions"] == [">=5.0.0", "<6.0.0"]
    assert result["compatible_version_range"] == ">=5.0.0,<6.0.0"
    assert result["choices"] == ["disable", "keep_enabled_anyway"]
    assert result["user_choice"] is None


def test_user_can_override_incompatible_provider(tmp_path: Path) -> None:
    runtime, project_root, templates = _make_runtime_with_incompatible_superpowers(tmp_path)

    result = runtime.set_skill_provider_override(project_root, "superpowers_external", "keep_enabled_anyway")

    assert result["provider_id"] == "superpowers_external"
    assert result["provider_state"] == "incompatible_but_user_override"
    assert result["override"] == "keep_enabled_anyway"

    reloaded_runtime = RuntimeService(AidocsServiceHub(templates_root=templates))
    status = reloaded_runtime.skill_provider_status(project_root, "superpowers_external")

    assert status["provider_state"] == "incompatible_but_user_override"
    assert status["user_choice"] == "keep_enabled_anyway"


def test_incompatible_provider_skills_are_listed_but_not_selectable_until_override(tmp_path: Path) -> None:
    runtime, project_root, _templates = _make_runtime_with_incompatible_superpowers(tmp_path)

    listed_skill = next(
        item for item in runtime.hub.skills.list_skills(project_root) if item.get("skill_id") == "superpowers_external/brainstorming"
    )

    assert listed_skill["provider_state"] == "detected_incompatible"
    assert listed_skill["selectable"] is False

    with pytest.raises(ValueError, match="not selectable"):
        runtime.hub.skills.set_selected_skills(project_root, "session-a", ["superpowers_external/brainstorming"])

    runtime.set_skill_provider_override(project_root, "superpowers_external", "keep_enabled_anyway")

    listed_skill = next(
        item for item in runtime.hub.skills.list_skills(project_root) if item.get("skill_id") == "superpowers_external/brainstorming"
    )
    assert listed_skill["provider_state"] == "incompatible_but_user_override"
    assert listed_skill["selectable"] is True
    assert runtime.hub.skills.set_selected_skills(project_root, "session-a", ["superpowers_external/brainstorming"])["selected_skills"] == [
        "superpowers_external/brainstorming"
    ]


def test_runtime_can_clear_override_and_restore_incompatible_state(tmp_path: Path) -> None:
    runtime, project_root, _templates = _make_runtime_with_incompatible_superpowers(tmp_path)

    runtime.set_skill_provider_override(project_root, "superpowers_external", "keep_enabled_anyway")
    result = runtime.set_skill_provider_override(project_root, "superpowers_external", None)

    assert result["provider_state"] == "detected_incompatible"
    assert result["override"] is None
    assert runtime.skill_provider_status(project_root, "superpowers_external")["user_choice"] is None


@pytest.mark.parametrize(
    ("version", "expected_state"),
    [
        ("5.0", "compatible"),
        ("5.0.0", "compatible"),
        ("5.0.0-beta.1", "detected_incompatible"),
        ("5.1.2+build.7", "compatible"),
        ("6.0.0", "detected_incompatible"),
    ],
)
def test_provider_status_uses_semver_compatible_gating(tmp_path: Path, version: str, expected_state: str) -> None:
    runtime, project_root, _templates = _make_runtime(tmp_path)
    _register_superpowers_provider(runtime, project_root, tmp_path, version=version)

    result = runtime.skill_provider_status(project_root, "superpowers_external")

    assert result["provider_version"] == version
    assert result["provider_state"] == expected_state


def test_session_skills_set_returns_structured_incompatible_provider_payload(tmp_path: Path) -> None:
    runtime, project_root, _templates = _make_runtime_with_incompatible_superpowers(tmp_path)

    result = runtime.hub.skills.try_set_selected_skills(project_root, "session-a", ["superpowers_external/brainstorming"])

    assert result["ok"] is False
    assert result["error"] == "incompatible_provider"
    assert result["blocked_skill_ids"] == ["superpowers_external/brainstorming"]
    assert result["provider"]["aidocs_version"]
    assert result["provider"]["provider_version"] == "1.2.3"
    assert result["provider"]["compatible_versions"] == [">=5.0.0", "<6.0.0"]
    assert result["provider"]["choices"] == ["disable", "keep_enabled_anyway"]


def test_external_provider_skill_ids_remain_provider_qualified(tmp_path: Path) -> None:
    runtime, project_root, _templates = _make_runtime(tmp_path)
    _register_superpowers_provider(runtime, project_root, tmp_path, version="5.1.0")

    listed_skill = next(
        item for item in runtime.hub.skills.list_skills(project_root) if item.get("provider") == "superpowers_external"
    )

    assert listed_skill["skill_id"] == "superpowers_external/brainstorming"


def test_set_selected_skills_rejects_unknown_skill_ids(tmp_path: Path) -> None:
    runtime, project_root, _templates = _make_runtime(tmp_path)

    with pytest.raises(ValueError, match="Unknown skill"):
        runtime.hub.skills.set_selected_skills(project_root, "session-a", ["missing-skill"])


def test_try_set_selected_skills_reports_unknown_skill_ids(tmp_path: Path) -> None:
    runtime, project_root, _templates = _make_runtime(tmp_path)

    result = runtime.hub.skills.try_set_selected_skills(project_root, "session-a", ["missing-skill"])

    assert result["ok"] is False
    assert result["error"] == "unknown_skill"
    assert result["unknown_skill_ids"] == ["missing-skill"]


def test_try_set_selected_skills_reports_all_blocked_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        skill_store_module._PROVIDER_COMPATIBILITY,
        "second_external",
        {
            "compatible_versions": [">=9.0.0", "<10.0.0"],
            "choices": ["disable", "keep_enabled_anyway"],
        },
    )
    runtime, project_root, _templates = _make_runtime_with_incompatible_superpowers(tmp_path)
    _register_provider_with_skill(
        runtime,
        project_root,
        tmp_path,
        provider_id="second_external",
        version="1.0.0",
        skill_name="debugging",
    )

    result = runtime.hub.skills.try_set_selected_skills(
        project_root,
        "session-a",
        ["superpowers_external/brainstorming", "second_external/debugging"],
    )

    assert result["ok"] is False
    assert result["error"] == "incompatible_provider"
    assert result["blocked_skill_ids"] == ["superpowers_external/brainstorming", "second_external/debugging"]
    assert [item["provider_id"] for item in result["providers"]] == ["second_external", "superpowers_external"]


def test_get_selected_skills_drops_stale_ids_and_reports_them(tmp_path: Path) -> None:
    runtime, project_root, _templates = _make_runtime(tmp_path)
    session_id = "session-a"
    path = runtime.hub.skills.session_skill_state_path(project_root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "selected_skills": [
                    "aidocs_bundled_superpowers/deep-retrieval",
                    "missing-skill",
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = runtime.hub.skills.get_selected_skills(project_root, session_id)
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert result["selected_skills"] == ["deep-retrieval"]
    assert result["invalid_selected_skills"] == ["missing-skill"]
    assert persisted == {"selected_skills": ["deep-retrieval"]}


def test_mcp_tools_surface_provider_status_override_and_structured_selection_block(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[list[str], dict[str, object], dict[str, object], dict[str, object]]:
        server = create_server()
        tool_names = [tool.name for tool in await server.list_tools()]
        hub = server._aidocs_test_hub
        _seed_project(project_root)
        runtime = RuntimeService(hub)
        _register_superpowers_provider(runtime, project_root, tmp_path, version="1.2.3")

        status_result = await server.call_tool(
            "skill_provider_status_get",
            {"root": str(project_root), "provider_id": "superpowers_external"},
        )
        blocked_result = await server.call_tool(
            "session_skills_set",
            {
                "root": str(project_root),
                "session_id": "session-a",
                "selected_skills": ["superpowers_external/brainstorming"],
            },
        )
        override_result = await server.call_tool(
            "skill_provider_override_set",
            {
                "root": str(project_root),
                "provider_id": "superpowers_external",
                "choice": "keep_enabled_anyway",
            },
        )

        def _payload(result: object) -> dict[str, object]:
            text = result[0].text if isinstance(result, list) else result.content[0].text
            return json.loads(text)

        return tool_names, _payload(status_result), _payload(blocked_result), _payload(override_result)

    tool_names, status_payload, blocked_payload, override_payload = asyncio.run(run())

    assert "skill_provider_status_get" in tool_names
    assert "skill_provider_override_set" in tool_names
    assert status_payload["provider_state"] == "detected_incompatible"
    assert blocked_payload["error"] == "incompatible_provider"
    assert blocked_payload["provider"]["provider_version"] == "1.2.3"
    assert blocked_payload["provider"]["choices"] == ["disable", "keep_enabled_anyway"]
    assert override_payload["provider_state"] == "incompatible_but_user_override"
