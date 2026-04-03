from pathlib import Path

from aidocs_mcp.legacy_migration_service import LegacyMigrationService


def test_inspect_legacy_reads_now_and_plan_inventory(tmp_path: Path) -> None:
    service = LegacyMigrationService()
    project_root = tmp_path / "project"
    memory_root = project_root / ".MEMORY"
    (memory_root / "plans").mkdir(parents=True, exist_ok=True)
    (memory_root / "NOW.md").write_text(
        "# NOW\n\n"
        "## Goal\n- Stabilize routing\n\n"
        "## Active\n- Fix updater\n\n"
        "## Upcoming\n- Migrate sessions\n- Recheck project\n\n"
        "## Blockers\n- None\n",
        encoding="utf-8",
    )
    (memory_root / "plans" / "audit.md").write_text("# Plan\n", encoding="utf-8")

    result = service.inspect_legacy(project_root)

    assert result["has_now"] is True
    assert result["has_root_plans"] is True
    assert result["plan_files"] == ["plans/audit.md"]
    assert result["now_summary"]["goal"] == "Stabilize routing"
    assert result["now_summary"]["active"] == "Fix updater"


def test_build_session_proposal_from_legacy_state(tmp_path: Path) -> None:
    service = LegacyMigrationService()
    project_root = tmp_path / "project"
    memory_root = project_root / ".MEMORY"
    (memory_root / "plans").mkdir(parents=True, exist_ok=True)
    (memory_root / "NOW.md").write_text(
        "# NOW\n\n"
        "## Goal\n- Session migration\n\n"
        "## Upcoming\n- Build sessions\n- Verify routing\n",
        encoding="utf-8",
    )
    (memory_root / "plans" / "migration.md").write_text("# Plan\n", encoding="utf-8")

    proposal = service.build_session_proposal(project_root, session_id="legacy-migration")

    assert proposal["legacy_present"] is True
    assert proposal["proposed_session_id"] == "legacy-migration"
    assert proposal["title"] == "Session migration"
    assert proposal["upcoming"] == ["Build sessions", "Verify routing"]
    assert proposal["plan_files"] == ["plans/migration.md"]
    assert proposal["decision_required"] is True
    assert proposal["next_step"] == "issue_stop_and_ask_for_migration_path"
    assert len(proposal["options"]) == 2
