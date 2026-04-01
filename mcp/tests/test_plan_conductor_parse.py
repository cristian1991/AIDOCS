from pathlib import Path

import pytest

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
        "## Last Updated\n- 2026-03-30 00:00\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text("# Context\n", encoding="utf-8")


def _make_store(tmp_path: Path) -> tuple[SessionStore, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    store = SessionStore(templates_root=templates)
    project = tmp_path / "project"
    session_id = "2026-03-30-lane-plan"
    store.create_session(project, session_id, "Lane Plan", "user", "Parse lane plan")
    return store, project, session_id


def _write_plan(store: SessionStore, project: Path, session_id: str, text: str) -> None:
    store.plan_file(project, session_id).write_text(text, encoding="utf-8")


def test_read_plan_parses_phase_lane_files_and_optional_dependencies(tmp_path: Path) -> None:
    store, project, session_id = _make_store(tmp_path)
    _write_plan(
        store,
        project,
        session_id,
        "# Plan\n"
        "\n## Purpose\n- Parse lane-aware plans\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: homepage-hero\n"
        "- Files: src/components/home/Hero.tsx, src/cms/hero-block.ts\n"
        "- [ ] Build hero component\n"
        "- [ ] Wire CMS hero block\n"
        "- Lane: homepage-shell\n"
        "- Files: src/pages/index.tsx\n"
        "- depends_on: homepage-hero\n"
        "- [ ] Integrate homepage shell\n"
        "\n## End Goal\n- Ship homepage\n",
    )

    plan = store.read_plan(project, session_id)

    assert [phase.name for phase in plan.phases] == ["Homepage foundation"]
    assert plan.phases[0].phase_id == "homepage-foundation"
    assert [lane.name for lane in plan.lanes] == ["homepage-hero", "homepage-shell"]
    assert plan.lanes[0].phase_id == "homepage-foundation"
    assert plan.lanes[0].files == ["src/components/home/Hero.tsx", "src/cms/hero-block.ts"]
    assert plan.lanes[0].depends_on == []
    assert [step.text for step in plan.lanes[0].steps] == ["Build hero component", "Wire CMS hero block"]
    assert plan.lanes[1].depends_on == ["homepage-hero"]


def test_lane_without_files_is_rejected(tmp_path: Path) -> None:
    store, project, session_id = _make_store(tmp_path)
    _write_plan(
        store,
        project,
        session_id,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: homepage-hero\n"
        "- [ ] Build hero component\n",
    )

    with pytest.raises(ValueError, match="Files are required"):
        store.read_plan(project, session_id)


def test_duplicate_phase_ids_are_rejected(tmp_path: Path) -> None:
    store, project, session_id = _make_store(tmp_path)
    _write_plan(
        store,
        project,
        session_id,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Home Page\n"
        "- Lane: homepage-hero\n"
        "- Files: src/home/Hero.tsx\n"
        "- [ ] Build hero\n"
        "- Phase: home-page\n"
        "- Lane: homepage-shell\n"
        "- Files: src/home/Page.tsx\n"
        "- [ ] Build shell\n",
    )

    with pytest.raises(ValueError, match="Duplicate phase id"):
        store.read_plan(project, session_id)


def test_duplicate_lane_ids_are_rejected(tmp_path: Path) -> None:
    store, project, session_id = _make_store(tmp_path)
    _write_plan(
        store,
        project,
        session_id,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: Home Hero\n"
        "- Files: src/home/Hero.tsx\n"
        "- [ ] Build hero\n"
        "- Lane: home-hero\n"
        "- Files: src/home/HeroBlock.ts\n"
        "- [ ] Build hero block\n",
    )

    with pytest.raises(ValueError, match="Duplicate lane id"):
        store.read_plan(project, session_id)


def test_unknown_depends_on_lane_is_rejected(tmp_path: Path) -> None:
    store, project, session_id = _make_store(tmp_path)
    _write_plan(
        store,
        project,
        session_id,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: homepage-hero\n"
        "- Files: src/home/Hero.tsx\n"
        "- depends_on: homepage-shell\n"
        "- [ ] Build hero\n",
    )

    with pytest.raises(ValueError, match="Unknown depends_on lane id"):
        store.read_plan(project, session_id)


def test_self_referential_depends_on_is_rejected(tmp_path: Path) -> None:
    store, project, session_id = _make_store(tmp_path)
    _write_plan(
        store,
        project,
        session_id,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: homepage-hero\n"
        "- Files: src/home/Hero.tsx\n"
        "- depends_on: homepage-hero\n"
        "- [ ] Build hero\n",
    )

    with pytest.raises(ValueError, match="cannot depend on itself"):
        store.read_plan(project, session_id)


def test_legacy_checkbox_plan_still_reads_without_lanes(tmp_path: Path) -> None:
    store, project, session_id = _make_store(tmp_path)
    _write_plan(
        store,
        project,
        session_id,
        "# Plan\n"
        "\n## Steps\n"
        "- [ ] Keep legacy checkbox plans working\n",
    )

    plan = store.read_plan(project, session_id)

    assert plan.lanes == []
    assert plan.phases == []
    assert plan.sections["Steps"] == ["- [ ] Keep legacy checkbox plans working"]

