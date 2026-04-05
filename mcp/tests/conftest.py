"""Shared test fixtures and configuration.

Quick reference for agents:
    pytest tests/ -q                    # full suite, compact output
    pytest tests/ -q -m "not slow"      # skip slow tests
    pytest tests/ -q --lf               # re-run only last failures
    pytest tests/ -q --lf --tb=long     # last failures with full traceback
    pytest tests/ -q -x                 # stop on first failure
    pytest tests/security/ -q           # one domain only
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aidocs_mcp.service_hub import AidocsServiceHub
from aidocs_mcp.runtime_service import RuntimeService


# ── Shared fixtures ──


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Minimal AIDOCS project skeleton."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".MEMORY").mkdir()
    (root / ".MEMORY" / "sessions").mkdir(parents=True)
    return root


@pytest.fixture
def project_with_session(project: Path) -> tuple[Path, str]:
    """Project with a single active session."""
    session_id = "2026-01-01-test"
    session_dir = project / ".MEMORY" / "sessions" / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n- Test session\n\n"
        "## Status\n- active\n\n"
        "## Owner\n- test\n\n"
        "## Goal\n- testing\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- 2026-01-01 00:00\n",
        encoding="utf-8",
    )
    (session_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    return project, session_id


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    """Write canonical session templates for RuntimeService tests."""
    root = tmp_path / "templates"
    root.mkdir()
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
        "# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def hub(templates: Path) -> AidocsServiceHub:
    return AidocsServiceHub(templates_root=templates)


@pytest.fixture
def runtime(hub: AidocsServiceHub) -> RuntimeService:
    return RuntimeService(hub)
