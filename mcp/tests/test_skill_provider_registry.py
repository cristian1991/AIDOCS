from pathlib import Path

import pytest

from aidocs_mcp.skill_store import SkillStore


def _write_skill(path: Path, *, name: str, description: str, tags: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tags: {tags}\n"
        "---\n",
        encoding="utf-8",
    )


def _write_raw_skill(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_skill_store(tmp_path: Path) -> tuple[SkillStore, Path]:
    store = SkillStore()
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    return store, project_root


def _make_external_provider(tmp_path: Path) -> Path:
    provider_root = tmp_path / "external-provider"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    _write_skill(
        provider_root / "skills" / "brainstorming" / "SKILL.md",
        name="brainstorming",
        description="Imported brainstorming skill.",
        tags="external, provider",
    )
    return provider_root


def _make_skill_store_with_external_provider(tmp_path: Path) -> tuple[SkillStore, Path]:
    store, project_root = _make_skill_store(tmp_path)
    _write_skill(
        project_root / ".MEMORY" / "skills" / "medical-domain.md",
        name="medical-domain",
        description="Project-local skill.",
        tags="domain, medical",
    )
    provider_root = _make_external_provider(tmp_path)
    store.register_external_provider(project_root, provider_name="superpowers_external", path=str(provider_root))
    return store, project_root


def _make_skill_store_with_external_provider_and_session(tmp_path: Path) -> tuple[SkillStore, Path, str]:
    store, project_root = _make_skill_store_with_external_provider(tmp_path)
    session_id = "2026-03-30-provider-session"
    (project_root / ".MEMORY" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
    return store, project_root, session_id


def test_skill_registry_lists_built_in_project_and_external_provider_skills(tmp_path: Path) -> None:
    store, project_root = _make_skill_store_with_external_provider(tmp_path)

    result = store.list_skills(project_root)

    assert any(item["origin"] == "bundled_provider" for item in result)
    assert any(item["name"] == "medical-domain" and item["origin"] == "project_local" for item in result)

    imported = next(item for item in result if item["skill_id"] == "superpowers_external/brainstorming")
    assert imported["name"] == "brainstorming"
    assert imported["provider"] == "superpowers_external"
    assert imported["origin"] == "external_provider"
    assert imported["path"].endswith("skills/brainstorming/SKILL.md") or imported["path"].endswith("skills\\brainstorming\\SKILL.md")


def test_bundled_provider_is_available_without_manual_registration(tmp_path: Path) -> None:
    store, project_root = _make_skill_store(tmp_path)

    result = store.list_skills(project_root)

    assert any(item["provider"] == "aidocs_bundled_superpowers" for item in result)


def test_bundled_provider_does_not_duplicate_built_in_skill_files(tmp_path: Path) -> None:
    store, project_root = _make_skill_store(tmp_path)

    result = store.list_skills(project_root)

    deep_retrieval_records = [item for item in result if Path(str(item["path"])).name == "deep-retrieval.md"]

    assert len(deep_retrieval_records) == 1
    assert deep_retrieval_records[0]["provider"] == "aidocs_bundled_superpowers"


def test_external_provider_requires_local_path(tmp_path: Path) -> None:
    store, project_root = _make_skill_store(tmp_path)

    with pytest.raises(ValueError, match="local path"):
        store.register_external_provider(project_root, provider_name="superpowers_external", path="")


def test_external_provider_requires_valid_provider_id(tmp_path: Path) -> None:
    store, project_root = _make_skill_store(tmp_path)
    provider_root = _make_external_provider(tmp_path)

    with pytest.raises(ValueError, match="provider id"):
        store.register_external_provider(project_root, provider_name="bad/provider", path=str(provider_root))

    assert not store.external_provider_registry_path(project_root).exists()


def test_session_skill_selection_can_reference_imported_skill(tmp_path: Path) -> None:
    store, project_root, session_id = _make_skill_store_with_external_provider_and_session(tmp_path)

    result = store.set_selected_skills(project_root, session_id, ["superpowers_external/brainstorming"])

    assert "superpowers_external/brainstorming" in result["selected_skills"]
    assert store.get_selected_skills(project_root, session_id)["selected_skills"] == ["superpowers_external/brainstorming"]


def test_external_provider_bad_skill_does_not_break_registry_listing(tmp_path: Path) -> None:
    store, project_root = _make_skill_store_with_external_provider(tmp_path)
    provider_root = tmp_path / "external-provider"
    _write_raw_skill(
        provider_root / "skills" / "broken-skill" / "SKILL.md",
        "---\n"
        "name: broken-skill\n"
        "description: Broken metadata skill.\n"
        "tags:\n"
        "  - external\n"
        "  - provider\n"
        "---\n",
    )

    result = store.list_skills(project_root)

    assert any(item["origin"] == "bundled_provider" for item in result)
    assert any(item["name"] == "medical-domain" and item["origin"] == "project_local" for item in result)
    assert any(item.get("skill_id") == "superpowers_external/brainstorming" for item in result)
    assert not any(item.get("skill_id") == "superpowers_external/broken-skill" and item.get("selectable", True) for item in result)

    warning = next(item for item in result if item["origin"] == "external_provider_warning")
    assert warning["provider"] == "superpowers_external"
    assert warning["selectable"] is False
    assert warning["warning"]["kind"] == "malformed_frontmatter"


def test_external_provider_accepts_quoted_scalar_frontmatter_values(tmp_path: Path) -> None:
    store, project_root = _make_skill_store(tmp_path)
    provider_root = tmp_path / "quoted-provider"
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    _write_raw_skill(
        provider_root / "skills" / "brainstorming" / "SKILL.md",
        "---\n"
        'name: "brainstorming"\n'
        'description: "Use when exploring user intent."\n'
        'tags: "external, provider"\n'
        "---\n",
    )
    store.register_external_provider(project_root, provider_name="superpowers_external", path=str(provider_root))

    result = store.list_skills(project_root)

    imported = next(item for item in result if item["skill_id"] == "superpowers_external/brainstorming")
    assert imported["name"] == "brainstorming"
    assert imported["description"] == "Use when exploring user intent."
    assert imported["tags"] == ["external", "provider"]
