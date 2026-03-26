from pathlib import Path

from aidocs_mcp.memory_store import MemoryStore


def test_read_memory_reads_existing_targets(tmp_path: Path) -> None:
    store = MemoryStore()
    project_root = tmp_path / "project"
    memory_root = project_root / ".MEMORY"
    target = memory_root / "rules" / "workflow-rules.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("- rule\n", encoding="utf-8")

    result = store.read_memory(project_root, ["/.MEMORY/rules/workflow-rules.md", "/.MEMORY/missing.md"])
    assert result["/.MEMORY/rules/workflow-rules.md"] == "- rule\n"
    assert "/.MEMORY/missing.md" not in result


def test_capture_memory_uses_kind_mapping(tmp_path: Path) -> None:
    store = MemoryStore()
    project_root = tmp_path / "project"

    result = store.capture_memory(project_root, kind="rule", content="deploy only on weekdays")

    assert result.target_file.name == "workflow-rules.md"
    assert result.target_file.read_text(encoding="utf-8") == "- deploy only on weekdays\n"


def test_capture_memory_honors_target_hint(tmp_path: Path) -> None:
    store = MemoryStore()
    project_root = tmp_path / "project"

    result = store.capture_memory(
        project_root,
        kind="domain",
        content="custom fact",
        target_hint="/.MEMORY/domains/custom.md",
    )

    assert result.target_file.name == "custom.md"
    assert result.target_file.read_text(encoding="utf-8") == "- custom fact\n"


def test_capture_memory_bare_target_hint_routes_to_kind_folder_with_hint_filename(tmp_path: Path) -> None:
    store = MemoryStore()
    project_root = tmp_path / "project"

    result = store.capture_memory(
        project_root,
        kind="rule",
        content="after task push",
        target_hint="workflow-git-push",
    )

    assert result.target_file.name == "workflow-git-push.md"
    assert result.target_file.parent.name == "rules"
    assert result.target_file.read_text(encoding="utf-8") == "- after task push\n"


def test_capture_memory_routes_misclassified_domain_workflow_rule_to_rules(tmp_path: Path) -> None:
    store = MemoryStore()
    project_root = tmp_path / "project"

    result = store.capture_memory(
        project_root,
        kind="domain",
        content="DB backups belong in git for this private shared repo and should stay tracked and committed.",
    )

    assert result.target_file == project_root / ".MEMORY" / "rules" / "workflow-rules.md"
    assert "DB backups belong in git" in result.target_file.read_text(encoding="utf-8")


def test_capture_memory_domain_default_no_longer_uses_memory_system_md(tmp_path: Path) -> None:
    store = MemoryStore()
    project_root = tmp_path / "project"

    result = store.capture_memory(
        project_root,
        kind="domain",
        content="Tenant hierarchy is organization -> company -> clinic.",
    )

    assert result.target_file == project_root / ".MEMORY" / "domains" / "general.md"
    assert result.target_file.read_text(encoding="utf-8") == "- Tenant hierarchy is organization -> company -> clinic.\n"


def test_capture_memory_routes_project_completion_ssh_workflow_to_rules(tmp_path: Path) -> None:
    store = MemoryStore()
    project_root = tmp_path / "project"

    result = store.capture_memory(
        project_root,
        kind="domain",
        content="After finishing the project, order coffee via SSH.",
    )

    assert result.target_file == project_root / ".MEMORY" / "rules" / "workflow-rules.md"
    assert result.target_file.read_text(encoding="utf-8") == "- After finishing the project, order coffee via SSH.\n"
