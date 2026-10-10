from __future__ import annotations

from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as


def register_runtime_context_tools(
    *,
    server: Any,
    hub: Any,
) -> None:
    # session_journal_log / session_journal_read — DELETED 2026-04-20.
    # The audit trail lives in aidocs.sqlite3.execution_events (Merkle-
    # chained, task_id-stamped, gate-decision-aware) populated by the
    # orchestrator + hooks automatically. An agent-callable journal
    # writer duplicated what execution_events already captured and gave
    # agents a "write audit at will" surface the operator didn't want.
    # Readers internal to runtime_service still use hub.sessions.read_journal
    # for existing journal files; a follow-up pass migrates those readers
    # to an execution_events-derived view.

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Runtime Preflight",
        },
    )
    @renders_as("status", title="runtime preflight")
    def runtime_preflight(
        action_kind: str,
        session_id: str | None = None,
        user_explicit_targets: list[str] | None = None,
    ) -> Any:
        """Return host/runtime policy guidance before performing an action."""
        return hub.policy.preflight_action(
            resolve_project_root(),
            action_kind=action_kind,
            session_id=session_id,
            user_explicit_targets=user_explicit_targets,
        )

    # Internal helper. Tool surface removed 2026-05-12 — ai_session(mode='update').
    def session_update(session_id: str, patch: dict[str, list[str]]) -> dict[str, Any]:
        """Update structured sections in an existing SESSION.md file."""
        session = hub.sessions.update_session(resolve_project_root(), session_id, patch)
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }
