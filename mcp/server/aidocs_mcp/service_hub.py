from __future__ import annotations

from pathlib import Path

from .code_index_store import CodeIndexStore
from .index_store import IndexStore
from .policy_service import PolicyService
from .legacy_migration_service import LegacyMigrationService
from .managed_file_service import ManagedFileService
from .managed_mode_service import ManagedModeService
from .memory_store import MemoryStore
from .project_status_service import ProjectStatusService
from .related_project_service import RelatedProjectService
from .session_store import SessionStore
from .schema_index_store import SchemaIndexStore
from .updater_service import UpdaterService
from .workflow_action_service import WorkflowActionService


class AidocsServiceHub:
    """Small composition root for the first MCP-backed AIDOCS services."""

    def __init__(self, templates_root: Path, script_root: Path | None = None) -> None:
        self.managed_files = ManagedFileService()
        self.managed_mode = ManagedModeService()
        self.sessions = SessionStore(templates_root=templates_root)
        self.memory = MemoryStore()
        self.index = IndexStore(session_store=self.sessions)
        self.code = CodeIndexStore(session_store=self.sessions)
        self.schema = SchemaIndexStore()
        self.updater = UpdaterService(script_root=script_root or templates_root.parents[2] / "scripts")
        self.legacy = LegacyMigrationService()
        self.related = RelatedProjectService()
        self.policy = PolicyService(self)
        self.project_status = ProjectStatusService(self)
        self.workflow = WorkflowActionService()
