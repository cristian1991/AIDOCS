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

    Reads the caller's host session id at this seam (r0b2 blocker 1). The
    caller resolves ONLY its own per-conductor binding (strict: no
    project-singleton fallback), so caller B can never land on caller A's
    session and spend A's grant; a caller with no binding resolves nothing and
    the act is refused. An identity-less caller resolves nothing either: the
    old singleton fallback is gone (identity recut 2026-09-29 -- every caller
    has one host-supplied identity, and identity fails closed).
    """
    try:
        from .managed_mode_service import resolve_managed_session
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        host = str(current_calling_host_session_id() or "").strip()
    except Exception:
        return ""  # cannot establish who is calling => resolve nothing
    if not host:
        return ""
    try:
        return str(
            resolve_managed_session(
                hub.managed_mode, project_root, host_session_id=host, strict=True
            )
            or ""
        )
    except Exception:
        return ""


def human_intent_refusal(
    tool: str, mode: str, target: str, *, noun: str = "session"
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "_error": "human_intent_required",
        "action": f"{tool} {mode}",
        "_detail": (
            f"{tool} {mode} of {noun} {target!r} was not performed: no "
            f"matching current operator intent exists for this exact act and "
            f"target (the operator's prompt in the current turn holds no "
            f"request to {mode} {noun} {target!r})."
        ),
    }
    out["session_id" if noun == "session" else "target"] = target
    return out


def require_local_human_intent(
    hub: Any,
    project_root: Path,
    *,
    tool: str,
    mode: str,
    target: str,
    noun: str = "session",
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
    return human_intent_refusal(tool, mode, target, noun=noun)


def require_session_choice(hub: Any, project_root: Path, session_id: object) -> tuple[str, dict[str, Any] | None]:
    """#1166 (operator 2026-10-19; r0b1 77914355-7d9): the ONE sink-side check
    for every model-callable act that BINDS this conversation to a session --
    ai_session(connect), the session_start compat alias, aidocs_mode_set,
    aidocs_orchestrate. Which session the agent works in is the operator's
    word ("it may not. maybe operator wants a new session, in 2 steps:
    connect p1, create s12"), so there is no direct-MCP way round it.

    Returns ``(session_id, refusal)``. Free: the session the conversation is
    in, and the one it was last in on this project (a lapsed bind, /clear --
    "currently unbound" is not "new", r0b1 578622ff-064). Any other session
    spends a current-prompt "connect session <id>" grant scoped to THIS
    project. Nothing named and nothing to resume -> ``session_id_required``.
    A gate principal never comes here: gate consent is its own confirmed
    handle. The auto-connects that are not the model's call (the SessionStart
    hook's bind, /aidocs) never come through these tools."""
    from .binding_intent_store import last_session_connect, require_move_intent
    from .mcp_server_runtime_helpers import current_calling_host_session_id

    host = str(current_calling_host_session_id() or "").strip()
    current = resolve_calling_session(hub, project_root)
    resume = current or last_session_connect(host, project_root)
    wanted = str(session_id or "").strip() or resume
    if not wanted:
        return "", {
            "ok": False,
            "connected": False,
            "_error": "session_id_required",
            "_detail": (
                "Name the session to connect: which session the agent works in "
                "is the operator's choice (for example: connect session <id>, or "
                "create session <id>)."
            ),
        }
    if wanted == resume:
        return wanted, None
    refusal = require_move_intent(host, "ai_session", wanted, project_root=project_root)
    if refusal is not None:
        return wanted, {**refusal, "ok": False, "current_session_id": current}
    return wanted, None


def record_session_choice(project_root: Path, session_id: object) -> None:
    """After a bind LANDED: remember it as this conversation's last session on
    this project (what require_session_choice resumes for free). Never raises."""
    try:
        from .binding_intent_store import record_session_connect
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        record_session_connect(current_calling_host_session_id(), project_root, str(session_id or ""))
    except Exception:  # noqa: BLE001 -- no record: the next connect needs the word
        pass
