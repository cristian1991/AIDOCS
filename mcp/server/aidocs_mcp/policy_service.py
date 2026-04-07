from __future__ import annotations

from pathlib import Path


class PolicyService:
    """Host/runtime integration policy over the AIDOCS lifecycle."""

    def __init__(self, hub) -> None:
        self.hub = hub

    def preflight_action(
        self,
        project_root: Path,
        action_kind: str,
        session_id: str | None = None,
        user_explicit_targets: list[str] | None = None,
    ) -> dict[str, object]:
        explicit_targets = [item for item in (user_explicit_targets or []) if str(item).strip()]
        sessions = self.hub.sessions.list_sessions(project_root)
        active_sessions = [item for item in sessions if item.status == "active"]

        inspection_allowed = action_kind in {"inspect", "read_file", "read_error"} and len(explicit_targets) > 0
        grounding_required = action_kind in {
            "edit",
            "write_memory",
            "project_update",
            "legacy_update",
            "archive",
            "delete_session",
            "task_begin",
            "task_update",
            "task_complete",
            "code_bundle",
        }

        if inspection_allowed and not grounding_required:
            return {
                "allowed": True,
                "requires_session": False,
                "reason": "explicit_user_target_can_be_inspected_first",
                "next_step": "inspect_target_then_ground_before_broader_execution",
                "active_sessions": [self._summary(item) for item in active_sessions],
            }

        if session_id is not None:
            return {
                "allowed": True,
                "requires_session": True,
                "reason": "session_selected",
                "selected_session": session_id,
                "active_sessions": [self._summary(item) for item in active_sessions],
            }

        if not grounding_required:
            return {
                "allowed": True,
                "requires_session": False,
                "reason": "no_session_required_for_this_action",
                "active_sessions": [self._summary(item) for item in active_sessions],
            }

        if len(active_sessions) == 1:
            return {
                "allowed": True,
                "requires_session": True,
                "reason": "single_active_session_available",
                "selected_session": active_sessions[0].session_id,
                "active_sessions": [self._summary(item) for item in active_sessions],
            }

        reason = "no_active_session" if len(active_sessions) == 0 else "multiple_active_sessions"
        return {
            "allowed": False,
            "requires_session": True,
            "reason": reason,
            "active_sessions": [self._summary(item) for item in active_sessions],
            "next_step": "run session_start or issue STOP for session selection",
        }

    def _summary(self, session) -> dict[str, object]:
        return {
            "session_id": session.session_id,
            "title": session.title,
            "status": session.status,
            "owner": session.owner,
            "goal": session.goal,
            "last_updated": session.last_updated,
        }
