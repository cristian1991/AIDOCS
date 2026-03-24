from __future__ import annotations

from pathlib import Path

from .action_surface_service import ActionSurfaceService
from .capability_index_store import CapabilityIndexStore
from .code_index_store import CodeIndexStore
from .execution_index_store import ExecutionIndexStore
from .index_store import IndexStore
from .policy_service import PolicyService
from .procedure_index_store import ProcedureIndexStore
from .procedure_capability_link_store import ProcedureCapabilityLinkStore
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
        self.capabilities = CapabilityIndexStore()
        self.code = CodeIndexStore(session_store=self.sessions)
        self.execution = ExecutionIndexStore()
        self.procedures = ProcedureIndexStore()
        self.procedure_links = ProcedureCapabilityLinkStore()
        self.schema = SchemaIndexStore()
        self.updater = UpdaterService(script_root=script_root or templates_root.parents[2] / "scripts")
        self.legacy = LegacyMigrationService()
        self.related = RelatedProjectService()
        self.policy = PolicyService(self)
        self.project_status = ProjectStatusService(self)
        self.action_surface = ActionSurfaceService(self)
        self.workflow = WorkflowActionService()
