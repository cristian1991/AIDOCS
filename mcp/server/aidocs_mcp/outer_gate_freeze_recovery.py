"""WebMCP freeze recovery — the org-admin ``clear_freeze`` control-plane op.

War 0 (2026-07-13): the witnessed deadlock — a session freeze on the WEB gate
could not be cleared by the authenticated org admin because the local MCP
``admin_clear_freeze`` tool requires a host-session operator identity
(``no_admin_identity``) and the prod CLI requires an operator token
(``operator_auth``). This op is the gate-side adapter: it authorizes from the
VERIFIED WebMCP principal and clears through the ONE canonical
``ClearFreezeService.clear_with_audit`` primitive — the SAME service the CLI
and the local MCP tool use. It is NOT a second clearing implementation.

Authority layers (all fail-closed, in order):
  1. org OWNER/ADMIN principal — enforced by ``handle_project_tool`` BEFORE
     this runs (mirrors approve/deny escalation).
  2. ``rbac.admin_clear_freeze`` capability on the project's RBAC store for
     the authenticated user_id (refusal audited).
  3. the relational ladder (``freeze_clear_ladder_block``): no self-clear
     unless org-admin; an admin's freeze only by an org-admin.
  4. a CONSUMABLE confirmation (canonical_invocation.ConfirmStore): single
     use, TTL, bound to (user, org, project, session, record, reason) — a
     replayed / cross-record / mutated-reason token fails with a named reason.

Repeating a successful request is harmless: an unknown/already-cleared
freeze_id returns ``{ok: True, status: "no_active_record"}`` ("no active
record remains"), never an error and never a second mutation.

Kept as a FOCUSED module (not inlined in the dispatcher) so the authority
sink-caller audit classifies a single reviewable freeze-clearing writer.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

_TOOL_NAME = "clear_freeze"


def _audit(root: Path, *, event_kind: str, status: str, user_id: str, payload: dict) -> None:
    """Best-effort control-plane audit row (refusals + successes both land)."""
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind=event_kind,
            source_kind="webmcp_clear_freeze",
            session_id=str(payload.get("session_id") or "") or None,
            capability_name="clear_freeze",
            action_kind="clear",
            target_entity=str(payload.get("freeze_id") or ""),
            status=status,
            payload=payload,
            user_id=user_id or None,
        )
    except Exception:  # noqa: BLE001 — audit is best-effort on REFUSALS; the
        # canonical service still enforces ledger-first ordering on the clear.
        pass


def _intent_hash(*, org_id: str, freeze: Any) -> str:
    """Bind the confirmation to the exact RECORD as witnessed at proposal:
    org + id + fingerprint + frozen_at. A different record (or the same id
    re-frozen later) yields a different intent → confirm_mismatch."""
    h = hashlib.sha256()
    for part in (
        org_id,
        str(getattr(freeze, "request_id", "")),
        str(getattr(freeze, "fingerprint_phrase", "")),
        str(getattr(freeze, "frozen_at", "")),
        str(getattr(freeze, "session_id", "")),
    ):
        h.update(b"\x1f")
        h.update(part.encode("utf-8"))
    return h.hexdigest()


def clear_freeze(
    root,
    *,
    freeze_id: str,
    reason: str,
    approver_user_id: str,
    approver_label: str,
    org_id: str = "",
    confirm_token: str = "",
) -> dict:
    """Resolve + authorize + (two-phase) clear ONE freeze record by its exact
    freeze_id. See module docstring for the authority ladder."""
    from .canonical_invocation import ConfirmStore, normalize_args
    from .clear_freeze_service import ClearFreezeService, freeze_clear_ladder_block
    from .identity_store import IdentityStore
    from .permission_catalog import PERM_ADMIN_CLEAR_FREEZE
    from .rbac_store import RBACStore
    from .session_freeze_store import SessionFreezeStore

    root_p = Path(root)
    freeze_id = str(freeze_id or "").strip()
    reason = str(reason or "").strip()
    if not freeze_id or not reason:
        return {
            "_error": "bad_args",
            "_detail": "clear_freeze requires the exact freeze_id AND a non-empty reason",
        }

    base_payload = {
        "freeze_id": freeze_id,
        "reason": reason,
        "org_id": org_id,
        "approver_label": approver_label,
    }

    # 2) capability gate — fail-closed, refusal audited.
    try:
        # #516: asked at PROJECT scope (the #500/#512 read pattern). The scope
        # chain still honours the operator's GLOBAL break-glass grant, while a
        # tenant's project-scoped org-admin grant counts only in ITS project.
        from .project_authority import project_scope_key

        has_perm = RBACStore().has_permission(
            root_p,
            approver_user_id,
            PERM_ADMIN_CLEAR_FREEZE,
            scope_type="project",
            scope_id=project_scope_key(root_p),
        )
    except Exception:  # noqa: BLE001 — an RBAC lookup error never grants
        has_perm = False
    if not approver_user_id or not has_perm:
        _audit(
            root_p,
            event_kind="webmcp_clear_freeze_refused",
            status="refused",
            user_id=approver_user_id,
            payload={**base_payload, "blocked_by": "missing_permission"},
        )
        return {
            "_error": "missing_permission",
            "_detail": (
                f"the authenticated operator lacks {PERM_ADMIN_CLEAR_FREEZE} on this "
                "project's RBAC store; grant it before clearing freezes over the web"
            ),
        }

    # 3) resolve the record SERVER-SIDE in the bound project's identity DB.
    #    A record belonging to another org/project lives in another project's
    #    DB and is simply unaddressable from here (tenant scoping by
    #    construction). Unknown id ⇒ harmless-repeat contract.
    sfs = SessionFreezeStore()
    target = sfs.get_active_freeze_by_id(root_p, freeze_id)
    if target is None:
        _audit(
            root_p,
            event_kind="webmcp_clear_freeze_noop",
            status="ok",
            user_id=approver_user_id,
            payload={**base_payload, "result": "no_active_record"},
        )
        return {
            "ok": True,
            "cleared": False,
            "status": "no_active_record",
            "freeze_id": freeze_id,
            "message": "no active record remains — nothing to clear (harmless repeat)",
        }

    # 4) relational ladder floor (fail-closed tiers).
    floor = freeze_clear_ladder_block(
        root_p,
        approver_user_id=approver_user_id,
        target_user_id=str(getattr(target, "user_id", "") or ""),
    )
    if floor is not None:
        _audit(
            root_p,
            event_kind="webmcp_clear_freeze_refused",
            status="refused",
            user_id=approver_user_id,
            payload={
                **base_payload,
                "session_id": target.session_id,
                "blocked_by": floor.get("blocked_by", "tier_floor"),
            },
        )
        return floor

    # 5) CONSUMABLE confirmation — canonical ConfirmStore, bound to
    #    (user, org, project, session, record, reason).
    from .outer_gate_executor import exec_project_id

    store = ConfirmStore(IdentityStore().db_path(root_p))
    norm = normalize_args({"freeze_id": freeze_id, "reason": reason})
    intent = _intent_hash(org_id=org_id, freeze=target)
    bind = {
        "operator": approver_user_id,
        "project_id": exec_project_id(root_p),
        "session_id": str(target.session_id or ""),
        "tool": _TOOL_NAME,
        "normalized_args": norm,
        "intent": intent,
    }
    now = time.time()
    if not confirm_token:
        token = store.issue(now=now, **bind)
        return {
            "_error": "confirm_required",
            "_detail": (
                "clear_freeze lifts a security lockdown; ask the user, then "
                "re-invoke with confirm_token"
            ),
            "action": _TOOL_NAME,
            "freeze_id": freeze_id,
            "confirm_token": token,
            "summary": (
                f"About to CLEAR freeze {freeze_id!r} on session "
                f"{target.session_id!r} (kind {target.kind!r}) — reason: "
                f"{reason!r}. This lifts the lockdown WITHOUT minting a grant. "
                "The user must confirm."
            ),
        }
    refusal = store.consume(str(confirm_token), now=now, **bind)
    if refusal is not None:
        _audit(
            root_p,
            event_kind="webmcp_clear_freeze_refused",
            status="refused",
            user_id=approver_user_id,
            payload={
                **base_payload,
                "session_id": target.session_id,
                "blocked_by": refusal.blocked_by,
            },
        )
        return {"_error": refusal.blocked_by, "_detail": refusal.reason}

    # 6) clear through the ONE audited primitive (same service as the CLI +
    #    local MCP tool — ledger-first ordering, escalation declined, lock
    #    lifted, cleared event written).
    result = ClearFreezeService().clear_with_audit(
        root_p,
        target_freeze=target,
        reason=reason,
        approver_label=approver_label,
        approver_user_id=approver_user_id,
        source_kind="webmcp_clear_freeze",
        cleared_event_kind="freeze_admin_cleared",
        permission_name=PERM_ADMIN_CLEAR_FREEZE,
    )
    return {
        "ok": result.cleared and result.status == "cleared",
        "freeze_id": result.request_id,
        "session_id": result.session_id,
        "cleared": result.cleared,
        "status": result.status,
        "escalation_status": result.escalation_status,
        "minted_grant": False,
        "message": result.message,
    }
