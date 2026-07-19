"""Canonical seven-domain participant builder for prompt-submit transactions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .prompt_submit_service import CapturedStoreState, PromptSubmitStoreParticipant


_DENY_REQUEST_ID = re.compile(
    r"(?im)^\s*deny:\s*([A-Za-z0-9._:-]+)\s*$"
)


def _participant(
    name: str,
    capture_raw: Callable[[], Mapping[str, Any]],
    restore_raw: Callable[[Mapping[str, Any]], None],
) -> PromptSubmitStoreParticipant:
    def capture() -> CapturedStoreState:
        raw = capture_raw()
        if raw.get("captured") is not True:
            raise RuntimeError(f"{name} facade returned an uncaptured snapshot")
        return CapturedStoreState(
            name=name,
            captured=True,
            existed=bool(raw.get("existed")),
            state=dict(raw),
        )

    def restore(snapshot: CapturedStoreState) -> None:
        if not isinstance(snapshot.state, Mapping):
            raise RuntimeError(f"{name} snapshot payload is malformed")
        restore_raw(snapshot.state)

    return PromptSubmitStoreParticipant(name=name, capture=capture, restore=restore)


def build_prompt_submit_participants(
    runtime: Any,
    *,
    project_root: Path,
    payload: Mapping[str, Any],
    host_kind: str,
    host_session_id: str,
) -> tuple[PromptSubmitStoreParticipant, ...]:
    """Build the fixed seven authority domains in deterministic restore order."""
    from .empire_soul_gate import (
        restore_prompt_submit_state as restore_souls,
        snapshot_prompt_submit_state as snapshot_souls,
    )
    from .escalation_store import EscalationStore
    from .session_freeze_store import SessionFreezeStore
    from .sticky_grants_store import StickyGrantsStore
    from . import task_lifecycle_store

    managed = runtime.hub.managed_mode.get_mode(
        project_root,
        host_session_id=host_session_id,
    )
    session_id = (
        str(managed.get("session_id") or "").strip()
        if isinstance(managed, Mapping) and managed.get("active")
        else ""
    )
    request_ids = tuple(
        dict.fromkeys(_DENY_REQUEST_ID.findall(str(payload.get("prompt") or "")))
    )
    freeze = SessionFreezeStore()
    escalation = EscalationStore()
    sticky = StickyGrantsStore()
    managed_snapshot: dict[str, Any] = {}
    created_session_ids: list[str] = []

    def capture_managed_session() -> Mapping[str, Any]:
        managed_state = runtime.hub.managed_mode.snapshot_prompt_submit_state(
            project_root,
            host_session_id=host_session_id,
        )
        session_state = runtime.hub.sessions.snapshot_prompt_submit_state(project_root)
        raw = {
            "captured": True,
            "existed": bool(
                managed_state.get("existed") or session_state.get("existed")
            ),
            "state": {"managed": managed_state, "sessions": session_state},
        }
        managed_snapshot.clear()
        managed_snapshot.update(raw)
        return raw

    def transaction_created_session_ids() -> tuple[str, ...]:
        if created_session_ids:
            return tuple(created_session_ids)
        state = managed_snapshot.get("state")
        if not isinstance(state, Mapping):
            return ()
        session_state = state.get("sessions")
        if not isinstance(session_state, Mapping):
            return ()
        session_payload = session_state.get("state")
        if not isinstance(session_payload, Mapping):
            return ()
        before = set(session_payload.get("disk_ids") or ()) | set(
            session_payload.get("members") or ()
        )
        current = runtime.hub.managed_mode.get_mode(
            project_root,
            host_session_id=host_session_id,
        )
        bound = (
            str(current.get("session_id") or "").strip()
            if isinstance(current, Mapping) and current.get("active")
            else ""
        )
        if (
            bound
            and bound != session_id
            and bound.startswith("auto-")
            and bound not in before
        ):
            created_session_ids.append(bound)
        return tuple(created_session_ids)

    def restore_query(state: Mapping[str, Any]) -> None:
        for created in transaction_created_session_ids():
            runtime.hub.query_gate.delete_prompt_submit_state(
                project_root,
                created,
            )
        runtime.hub.query_gate.restore_prompt_submit_state(
            project_root,
            session_id,
            state,
        )

    def restore_tasks(state: Mapping[str, Any]) -> None:
        for created in transaction_created_session_ids():
            task_lifecycle_store.delete_prompt_submit_state(
                project_root,
                session_id=created,
            )
        task_lifecycle_store.restore_prompt_submit_state(
            project_root,
            dict(state),
            session_id=session_id,
        )

    def restore_managed_session(snapshot: Mapping[str, Any]) -> None:
        state = snapshot.get("state")
        if not isinstance(state, Mapping):
            raise RuntimeError("managed_session snapshot is malformed")
        exact_created = transaction_created_session_ids()
        errors: list[str] = []
        try:
            runtime.hub.managed_mode.restore_prompt_submit_state(
                project_root,
                state["managed"],
                host_session_id=host_session_id,
            )
        except Exception as exc:
            errors.append(f"managed: {exc}")
        try:
            runtime.hub.sessions.restore_prompt_submit_state(
                project_root,
                state["sessions"],
                created_session_ids=exact_created,
            )
        except Exception as exc:
            errors.append(f"sessions: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    return (
        _participant(
            "query_gate",
            lambda: runtime.hub.query_gate.snapshot_prompt_submit_state(
                project_root,
                session_id,
            ),
            restore_query,
        ),
        _participant(
            "freeze",
            lambda: freeze.snapshot_prompt_submit_state(
                project_root,
                session_id=session_id,
                host_session_id=host_session_id,
                host_kind=host_kind,
            ),
            lambda state: freeze.restore_prompt_submit_state(
                project_root,
                dict(state),
                session_id=session_id,
                host_session_id=host_session_id,
                host_kind=host_kind,
            ),
        ),
        _participant(
            "soul_grants",
            lambda: snapshot_souls(project_root, session_id=session_id),
            lambda state: restore_souls(
                project_root,
                dict(state),
                session_id=session_id,
            ),
        ),
        _participant(
            "escalation",
            lambda: escalation.snapshot_prompt_submit_state(
                project_root,
                session_id=session_id,
                request_ids=request_ids,
            ),
            lambda state: escalation.restore_prompt_submit_state(
                project_root,
                dict(state),
                session_id=session_id,
                request_ids=request_ids,
            ),
        ),
        _participant(
            "sticky_grants",
            lambda: sticky.snapshot_prompt_submit_state(
                project_root,
                session_id=session_id,
            ),
            lambda state: sticky.restore_prompt_submit_state(
                project_root,
                dict(state),
                session_id=session_id,
            ),
        ),
        _participant(
            "managed_session",
            capture_managed_session,
            restore_managed_session,
        ),
        _participant(
            "task_state",
            lambda: task_lifecycle_store.snapshot_prompt_submit_state(
                project_root,
                session_id=session_id,
            ),
            restore_tasks,
        ),
    )
