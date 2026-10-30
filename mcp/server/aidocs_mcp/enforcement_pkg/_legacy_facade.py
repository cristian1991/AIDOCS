"""Lane 2 phase 1 — bridge from EnforcementController to legacy
gate_tool.enforce_tool_call.

This module exists ONLY during the migration. As gates move into
the controller's own pipeline (`enforcement_pkg/gates/*`), the
legacy bridge shrinks; once every surface is migrated and the
legacy entry can be deleted, this module goes with it.

Adapters DO NOT import from here directly. They call
`EnforcementController.enforce(request)`; the controller routes
through this module today and through native gates tomorrow.
"""

from __future__ import annotations

from .decision import Decision, GateStep, Verdict
from .surface import Request


def enforce_via_legacy(request: Request) -> Decision:
    """Run the legacy gate cascade and translate EnforceResult to
    Decision.

    The transport-safety boundary fires inside
    `gate_tool.enforce_tool_call` (lane 1.6), so any gate exception
    is already structured before we get the EnforceResult.
    """
    from ..gate_tool import enforce_tool_call

    hub = None
    runtime = None
    try:
        from ..mcp_server import _resolve_script_root, _resolve_templates_root
        from ..runtime_service import RuntimeService
        from ..service_hub import AidocsServiceHub

        hub = AidocsServiceHub(
            templates_root=_resolve_templates_root(),
            script_root=_resolve_script_root(),
        )
        runtime = RuntimeService(hub)
    except Exception:
        hub = None
        runtime = None

    result = enforce_tool_call(
        hub,
        request.project_root,
        request.tool_name,
        request.tool_input,
        fail_closed=True,
        include_freeze=True,
        runtime=runtime,
    )

    if result.refusal is None:
        return Decision(
            request_id=request.request_id,
            verdict=Verdict.ALLOW,
            reason_code=str(result.decision_reason or "allowed"),
            user_message="",
            session_id=str(result.session_id or ""),
            trace=[
                GateStep(
                    gate_name="legacy_facade",
                    verdict="allow",
                    reason="delegated to gate_tool.enforce_tool_call",
                ),
            ],
        )

    refusal = result.refusal
    freeze_id_val: str | None = None
    fz = refusal.get("freeze_state")
    if isinstance(fz, dict):
        rid = fz.get("request_id")
        if isinstance(rid, str) and rid:
            freeze_id_val = rid

    return Decision(
        request_id=request.request_id,
        verdict=Verdict.REFUSE,
        reason_code=str(refusal.get("blocked_by") or "refused"),
        user_message=str(refusal.get("reason") or ""),
        enforcement={k: v for k, v in refusal.items() if k not in {"reason", "blocked_by"}},
        session_id=str(result.session_id or ""),
        freeze_id=freeze_id_val,
        trace=[
            GateStep(
                gate_name="legacy_facade",
                verdict="refuse",
                reason=str(refusal.get("blocked_by") or "refused"),
            ),
        ],
    )
