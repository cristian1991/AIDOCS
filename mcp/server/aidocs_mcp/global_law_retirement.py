"""Laddered global-law retirement service (#601).

The public memory surface routes here through the existing internal
``memory_promote`` home. Request and decision are separate actions: an admin
may stage a request, while a different super-admin must approve or deny it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def handle_global_law_retirement(
    project_root: Path,
    hub: Any,
    *,
    mode: str,
    law_id: str = "",
    request_id: str = "",
    reason: str = "",
    approve: bool = False,
) -> dict[str, Any]:
    """Request or decide a soft retirement with a two-party role ladder."""
    op = (mode or "").strip().lower()
    why = (reason or "").strip()
    from .project_authority import _authenticated_uid

    actor_uid = _authenticated_uid(project_root)
    if not actor_uid:
        return {
            "ok": False,
            "blocked_by": "operator_auth",
            "reason": "authenticated_operator_required",
        }
    if not why:
        return {
            "ok": False,
            "reason": "reason_required",
            "message": "global-law retirement requires an audited reason",
        }

    from .escalation_store import EscalationStore, STATUS_PENDING
    from .global_law_store import (
        read_global_law,
        retire_global_law,
        upsert_global_law,
    )
    from .permission_catalog import (
        PERM_ADMIN_MANAGE_CONFIG,
        PERM_MEMORY_RETIRE_GLOBAL_LAW,
    )

    store = EscalationStore()
    if op == "request":
        lid = (law_id or "").strip()
        law = read_global_law(lid)
        if law is None:
            return {
                "ok": False,
                "reason": "active_global_law_not_found",
                "law_id": lid,
            }
        gate = hub.require_permission(
            project_root,
            PERM_ADMIN_MANAGE_CONFIG,
            scope_type="project",
            scope_id=str(project_root).replace("\\", "/"),
            tool_name="memory_promote",
            extra_payload={"operation": "retire_request", "law_id": lid},
        )
        if not gate["ok"]:
            return gate
        req = store.create_request(
            project_root,
            requester_user_id=actor_uid,
            requester_label=actor_uid,
            gate_permission=PERM_MEMORY_RETIRE_GLOBAL_LAW,
            gate_phrase=f"retire global law {lid}",
            sticky=False,
            extra={
                "operation": "global_law_retire",
                "law_id": lid,
                "law_checksum": str(law.get("checksum") or ""),
                "request_reason": why[:500],
            },
        )
        try:
            hub.execution.record_event(
                project_root,
                event_kind="global_law_retire_requested",
                source_kind="memory_promote",
                capability_name="memory_promote",
                action_kind="memory",
                target_entity=lid,
                status="pending",
                payload={
                    "request_id": req.request_id,
                    "requester_user_id": actor_uid,
                    "reason": why[:500],
                    "checksum": law.get("checksum", ""),
                },
            )
        except Exception:
            pass
        return {
            "ok": True,
            "status": "pending",
            "request_id": req.request_id,
            "law_id": lid,
            "message": (
                "retirement requested; the law remains active until a "
                "different super_admin decides the request"
            ),
        }

    if op != "decide":
        return {
            "ok": False,
            "reason": "unknown_mode",
            "available_modes": ["request", "decide"],
        }

    rid = (request_id or "").strip()
    if not rid:
        return {"ok": False, "reason": "request_id_required"}
    gate = hub.require_permission(
        project_root,
        PERM_MEMORY_RETIRE_GLOBAL_LAW,
        scope_type="project",
        scope_id=str(project_root).replace("\\", "/"),
        tool_name="memory_promote",
        extra_payload={"operation": "retire_decide", "request_id": rid},
    )
    if not gate["ok"]:
        return gate
    from .clear_freeze_service import _holds_super_admin

    if not _holds_super_admin(project_root, actor_uid):
        return {
            "ok": False,
            "blocked_by": "tier_floor",
            "reason": "super_admin_required",
        }
    req = store.get(project_root, rid)
    if req is None:
        return {"ok": False, "reason": "retirement_request_not_found"}
    if req.status != STATUS_PENDING:
        return {
            "ok": False,
            "reason": "retirement_request_not_pending",
            "status": req.status,
        }
    extra = dict(req.extra or {})
    if (
        req.gate_permission != PERM_MEMORY_RETIRE_GLOBAL_LAW
        or extra.get("operation") != "global_law_retire"
    ):
        return {"ok": False, "reason": "wrong_request_type"}
    if str(req.requester_user_id or "") == actor_uid:
        return {
            "ok": False,
            "blocked_by": "self_approval_forbidden",
            "reason": "a different super_admin must decide this request",
        }
    lid = str(extra.get("law_id") or "").strip()

    if not approve:
        decided = store.decide(
            project_root,
            rid,
            approve=False,
            approver_user_id=actor_uid,
            approver_label=actor_uid,
            reason=why,
        )
        return {
            "ok": decided is not None,
            "status": "denied" if decided is not None else "decision_failed",
            "request_id": rid,
            "law_id": lid,
        }

    law = read_global_law(lid)
    if law is None:
        return {
            "ok": False,
            "reason": "active_global_law_not_found",
            "law_id": lid,
        }
    expected_checksum = str(extra.get("law_checksum") or "")
    if expected_checksum and str(law.get("checksum") or "") != expected_checksum:
        return {
            "ok": False,
            "reason": "global_law_changed_since_request",
            "law_id": lid,
        }
    if not retire_global_law(lid):
        return {"ok": False, "reason": "global_law_retire_failed", "law_id": lid}
    decided = store.decide(
        project_root,
        rid,
        approve=True,
        approver_user_id=actor_uid,
        approver_label=actor_uid,
        reason=why,
    )
    if decided is None:
        rollback_ok = True
        try:
            upsert_global_law(
                law_id=lid,
                kind=str(law.get("kind") or ""),
                content=str(law.get("content") or ""),
                keywords=str(law.get("keywords") or ""),
                sovereign_owner=law.get("sovereign_owner"),
                source=str(law.get("source") or "manual"),
            )
        except Exception:
            rollback_ok = False
        return {
            "ok": False,
            "reason": "retirement_decision_receipt_failed",
            "rollback_ok": rollback_ok,
            "request_id": rid,
            "law_id": lid,
        }
    try:
        hub.execution.record_event(
            project_root,
            event_kind="global_law_retired",
            source_kind="memory_promote",
            capability_name="memory_promote",
            action_kind="memory",
            target_entity=lid,
            status="retired",
            payload={
                "request_id": rid,
                "requester_user_id": req.requester_user_id,
                "approver_user_id": actor_uid,
                "reason": why[:500],
                "checksum": law.get("checksum", ""),
            },
        )
    except Exception:
        pass
    return {
        "ok": True,
        "status": "retired",
        "request_id": rid,
        "law_id": lid,
    }
