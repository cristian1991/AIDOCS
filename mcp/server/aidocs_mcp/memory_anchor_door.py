"""ai_memory's anchor modes (#1187, operator = dashboard): ONE door, BOTH surfaces.

anchor_list / anchor_preview read; anchor_apply changes anchors through
anchor_service and needs, on either surface:

  * memory.manage_anchors for the AUTHENTICATED operator of the bound project
    (project_authority.require_admin: the local operator or the gate's web
    principal, never audit identity), and
  * the operator's consent for exactly this op, tier and target:
      - on the gate: the registry's two-phase confirmation (gate_confirm +
        confirm_modes=("anchor_apply",) on ai_memory). The gate mints a
        single-use handle bound to the tool and these arguments and consumes it
        BEFORE this body runs; the body reads the execution scope, never an
        argument (the transport strips confirm_token);
      - locally: the user-intent grant the operator's own UserPromptSubmit text
        minted ({tool: ai_memory, mode: anchor_<op>[:<tier>], target}),
        consumed here, single-use.

Consent is spent only after the preview still matches the caller's
fingerprint and the permission holds, so neither a stale preview nor a missing
permission costs the operator their instruction. anchor_service re-checks the
fingerprint inside its write transaction. The project is the bound one: the
gate scopes every call to the selected project, so no op reaches another
project or tenant.
"""

from __future__ import annotations

from typing import Any

TOOL = "ai_memory"


def grant_mode(op: str, to: str) -> str:
    """The user-intent grant mode an anchor_apply of (op, to) consumes."""
    op = str(op or "").strip().lower()
    to = str(to or "").strip().lower()
    return f"anchor_{op}:{to}" if to else f"anchor_{op}"


def register_memory_anchor_impl(*, server: Any, hub: Any, runtime: Any) -> None:
    """Register the internal ``memory_anchor`` impl ai_memory delegates to."""
    del server, runtime  # uniform registration signature

    from . import anchor_service
    from . import tool_interface as _ti
    from .local_human_intent import require_local_human_intent
    from .mcp_server_runtime_helpers import (
        current_gate_confirmation,
        current_gate_principal,
        resolve_project_root,
    )
    from .permission_catalog import PERM_MEMORY_MANAGE_ANCHORS
    from .project_authority import require_admin

    def memory_anchor(
        action: str,
        target: str = "",
        op: str = "",
        to: str = "",
        fingerprint: str = "",
    ) -> dict:
        project_root = resolve_project_root()
        act = str(action or "").strip().lower()
        if act == "list":
            return anchor_service.list_anchors(project_root, target)
        if act == "preview":
            return anchor_service.preview(project_root, op, target, to=to)
        if act != "apply":
            return {
                "ok": False,
                "_error": "unknown_mode",
                "_detail": "anchor_list | anchor_preview | anchor_apply",
            }

        plan = anchor_service.preview(project_root, op, target, to=to)
        if not plan.get("ok"):
            return plan
        if plan["fingerprint"] != str(fingerprint or ""):
            return {
                "ok": False,
                "_error": "state_changed",
                "_detail": "the anchors are not in the previewed state; preview again",
                "fingerprint": plan["fingerprint"],
            }
        on_gate = current_gate_principal() is not None
        actor = {"source": TOOL, "surface": "gate" if on_gate else "local"}
        decision = require_admin(
            project_root, permission=PERM_MEMORY_MANAGE_ANCHORS, operation="anchor_apply"
        )
        # r0b2 adef865a-203: the audit actor is the AUTHENTICATED principal the
        # permission was checked for (require_admin's one resolution), never a
        # caller-supplied or audit identity. An allow that names nobody cannot
        # be attributed, so it changes nothing.
        user_id = str(decision.get("user_id") or "").strip() if decision.ok else ""
        if decision.ok and not user_id:
            return anchor_service.apply(
                project_root,
                op,
                target,
                expected_fingerprint=fingerprint,
                authorize=lambda: (False, "the authorized principal could not be identified"),
                actor=actor,
                to=to,
            )
        actor.update(user_id=user_id, auth_path=str(decision.get("auth_path") or ""))
        if not decision.ok:
            return anchor_service.apply(
                project_root,
                op,
                target,
                expected_fingerprint=fingerprint,
                authorize=lambda: (False, str(decision.reason)),
                actor=actor,
                to=to,
            )
        if on_gate:
            confirmed = current_gate_confirmation()
            if not (confirmed and confirmed[0] == TOOL):
                # The registry confirms anchor_apply before the body runs; a gate
                # call that reaches here unconfirmed is refused, never applied.
                return {
                    "ok": False,
                    "_error": "confirm_required",
                    "_detail": "anchor_apply on the gate is confirmed by the gate's two-phase handle",
                }
        else:
            refusal = require_local_human_intent(
                hub,
                project_root,
                tool=TOOL,
                mode=grant_mode(op, to),
                target=target,
                noun="anchor target",
            )
            if refusal is not None:
                return refusal
        return anchor_service.apply(
            project_root,
            op,
            target,
            expected_fingerprint=fingerprint,
            authorize=lambda: (True, ""),
            actor=actor,
            to=to,
        )

    _ti.register_impl("memory_anchor", memory_anchor)
