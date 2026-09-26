"""Local human-intent consent for control-plane acts (bleed B, 2026-09-24).

On the local, hook-capable surface the approval for an irreversible
control-plane act (ai_session create / delete today) comes from USER INTENT:
a grant the operator's own UserPromptSubmit text minted into
``session_query_gate.user_intent_actions``. Never from a confirm_token -- a
fixed phrase printed in a refusal is something the model can echo, so it
confirms nothing.

This module is the ONE local consumer seam:

  * ``resolve_calling_session`` -- the managed session whose query-gate row
    holds this caller's per-turn grants (the same session the UPS hook wrote
    to for this host);
  * ``require_local_human_intent`` -- atomically consume the matching grant
    (store-owned, single-use) or return the ``human_intent_required``
    refusal.

The gate-principal path never calls this: gate consent is its own minted,
single-use handle, confirmed at the gate boundary.

Refusal text is FACTUAL: it names the act and the exact target and states
where consent comes from. It carries no confirm_token and no instruction
addressed to the model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_calling_session(hub: Any, project_root: Path) -> str:
    """The managed session bound to THIS caller, or ''.

    Reads the caller's host session id at this seam (r0b2 blocker 1). A
    NAMED host resolves ONLY its own per-conductor binding (strict: no
    project-singleton fallback), so caller B can never land on caller A's
    session and spend A's grant; a named host with no binding resolves
    nothing and the act is refused. An identity-less local host keeps the
    documented singleton fallback.
    """
    try:
        from .managed_mode_service import resolve_managed_session
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        host = str(current_calling_host_session_id() or "").strip()
    except Exception:
        return ""  # cannot establish who is calling => resolve nothing
    try:
        if host:
            return str(
                resolve_managed_session(
                    hub.managed_mode, project_root, host_session_id=host, strict=True
                )
                or ""
            )
        return str(resolve_managed_session(hub.managed_mode, project_root) or "")
    except Exception:
        return ""


def human_intent_refusal(tool: str, mode: str, target: str) -> dict[str, Any]:
    return {
        "ok": False,
        "_error": "human_intent_required",
        "action": f"{tool} {mode}",
        "session_id": target,
        "_detail": (
            f"{tool} {mode} of session {target!r} was not performed: no "
            f"matching current operator intent exists for this exact act and "
            f"target (the operator's prompt in the current turn holds no "
            f"request to {mode} session {target!r})."
        ),
    }


def require_local_human_intent(
    hub: Any,
    project_root: Path,
    *,
    tool: str,
    mode: str,
    target: str,
) -> dict[str, Any] | None:
    """Consume the operator's matching current-turn grant.

    Returns None when exactly one matching grant was consumed (proceed), or
    the ``human_intent_required`` refusal otherwise. Fail-closed: any store
    error is a refusal, never a pass.
    """
    sid = resolve_calling_session(hub, project_root)
    consumed = None
    if sid:
        try:
            consumed = hub.query_gate.consume_user_intent_action(
                project_root,
                sid,
                tool=tool,
                mode=mode,
                target=target,
            )
        except Exception:
            consumed = None
    if consumed:
        return None
    return human_intent_refusal(tool, mode, target)
