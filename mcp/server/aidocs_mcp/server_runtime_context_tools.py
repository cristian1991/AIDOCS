from __future__ import annotations

from pathlib import Path
from .mcp_server_runtime_helpers import resolve_project_root
from typing import Any

def register_runtime_context_tools(
    *,
    server: Any,
    hub: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read Session Journal",
        }
    )
    def session_journal_read(
        session_id: str,
        last_n: int | None = None,
        root: str = "",
    ) -> list[dict[str, str]]:
        """Read the session journal — a rolling log of significant decisions and outcomes.

        Use this to refresh your memory when resuming a stale session.

        Args:
            last_n: Only return the last N entries. None returns all.
        """
        return hub.sessions.read_journal(resolve_project_root(root), session_id, last_n=last_n)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Log to Session Journal",
        }
    )
    def session_journal_log(
        session_id: str,
        action_kind: str,
        intent: str,
        outcome: str,
        root: str = "",
    ) -> dict[str, Any]:
        """Log a significant decision or outcome to the session journal.

        Only log meaningful work — not greetings, trivial commands, or minor edits.
        The journal auto-evicts oldest entries to archive when full (default: 100 entries).

        Args:
            action_kind: The type of action (edit, trace, investigate, read_error, etc.).
            intent: What the user asked for (1-2 sentences, max 120 chars).
            outcome: What happened (1-2 sentences, max 120 chars).
        """
        return hub.sessions.write_journal_entry(
            resolve_project_root(root),
            session_id,
            action_kind=action_kind,
            intent=intent,
            outcome=outcome,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Runtime Preflight",
        }
    )
    def runtime_preflight(
        action_kind: str,
        session_id: str | None = None,
        user_explicit_targets: list[str] | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Return host/runtime policy guidance before performing an action."""
        return hub.policy.preflight_action(
            resolve_project_root(root),
            action_kind=action_kind,
            session_id=session_id,
            user_explicit_targets=user_explicit_targets,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Session",
        }
    )
    def session_update(
        session_id: str, patch: dict[str, list[str]], root: str = ""
    ) -> dict[str, Any]:
        """Update structured sections in an existing SESSION.md file."""
        session = hub.sessions.update_session(resolve_project_root(root), session_id, patch)
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }
