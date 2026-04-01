from pathlib import Path

from aidocs_mcp.skill_store import SkillStore


def test_list_skills_includes_built_in_skill_descriptors(tmp_path: Path) -> None:
    store = SkillStore()
    project_root = tmp_path / "project"

    skills = store.list_skills(project_root)

    names = {item["name"] for item in skills}
    assert "deep-retrieval" in names
    assert "test-driven-validation" in names


def test_project_local_skills_are_merged(tmp_path: Path) -> None:
    store = SkillStore()
    project_root = tmp_path / "project"
    local_dir = project_root / ".MEMORY" / "skills"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "medical-domain.md").write_text(
        "---\n"
        "name: medical-domain\n"
        "description: Prefer medical/domain-specific reasoning.\n"
        "tags: domain, medical\n"
        "---\n",
        encoding="utf-8",
    )

    skills = store.list_skills(project_root)

    names = {item["name"] for item in skills}
    assert "medical-domain" in names
