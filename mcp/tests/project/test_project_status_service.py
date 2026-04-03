from pathlib import Path

from aidocs_mcp.project_status_service import ProjectStatusService
from aidocs_mcp.service_hub import AidocsServiceHub


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text("# Session\n", encoding="utf-8")
    (root / "context.md").write_text("# Context\n", encoding="utf-8")


def test_project_status_evaluate_scores_declared_areas(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    service = ProjectStatusService(hub)
    project_root = tmp_path / "project"
    (project_root / ".MEMORY" / "config").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text("public record QuoteDto;\n", encoding="utf-8")

    (project_root / ".MEMORY" / "config" / "project-status.json").write_text(
        '{"areas":[{"id":"quotes","title":"Quotes","weight":10,"checks":[{"type":"symbol","target":"QuoteDto"},{"type":"file_exists","target":"Models/QuoteDto.cs"}]}]}',
        encoding="utf-8",
    )

    hub.code.sync_code_files(project_root)
    result = service.evaluate(project_root)

    assert result["exists"] is True
    assert result["score"] == 100
    assert result["areas"][0]["state"] == "ready"


def test_project_status_missing_model_returns_empty_state(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    service = ProjectStatusService(hub)
    project_root = tmp_path / "project"

    result = service.evaluate(project_root)

    assert result["exists"] is False
    assert result["score"] == 0
