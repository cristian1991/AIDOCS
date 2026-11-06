"""Shape the ai_session(connect) receipt so an agent reads the verdict first.

MEASURED 2026-10-25 (ubermega, window 1613a45c): the receipt led with
``connected: true`` / ``message: "Managed mode activated"``, then 28 plan rows
(24 ``no_steps``) and a long identity warning, and put the one fact that
decided whether the agent could message anyone -- ``xaacp_attach: {status:
confirm_required}`` -- LAST. The agent took it for success and its next XAACP
send was refused gate-side (``managed_mode_inactive``).

``connected`` stays exactly what it was (the LOCAL bind). This only adds a
one-line ``verdict`` + ``messaging`` state (+ ``next_action`` when the agent
owes one) at the FRONT, and folds step-less plans into a count. No field is
removed; a failed connect is returned untouched.
"""

from __future__ import annotations

MESSAGING_READY = "ready"
MESSAGING_CONFIRM_REQUIRED = "confirm_required"
MESSAGING_NOT_ATTACHED = "not_attached"
MESSAGING_NOT_ATTEMPTED = "not_attempted"

# The headline this module derives. A raw receipt key of the same name is
# dropped, never merged: an upstream ``verdict`` must not outrank this one.
_HEADLINE_KEYS = ("verdict", "messaging", "approval_pending", "next_action")


def _attach_state(attach: object) -> tuple[str, str, str]:
    """``(messaging, actor_id, reason)`` from the xaacp_attach_on_connect result."""
    if not isinstance(attach, dict):
        return MESSAGING_NOT_ATTEMPTED, "", ""
    actor_id = str(attach.get("actor_id") or "")
    # Local registry: {"registered": bool, ...}; gate: the xaacp_attach reply.
    # An explicit ``ok: False`` beside a success marker is a contradiction and
    # never reads as ready (r0b2 2cf48ec6-efd).
    succeeded = attach.get("registered") is True or str(attach.get("status") or "") == "attached"
    if succeeded and attach.get("ok") is not False:
        return MESSAGING_READY, actor_id, ""
    if succeeded:
        return MESSAGING_NOT_ATTACHED, actor_id, "attach reported success and failure (ok: false)"
    if str(attach.get("status") or "") == "confirm_required" and attach.get("confirm_token"):
        return MESSAGING_CONFIRM_REQUIRED, actor_id, "gate bind needs operator confirmation"
    reason = str(attach.get("reason") or attach.get("error") or attach.get("status") or "unknown")
    return MESSAGING_NOT_ATTACHED, actor_id, reason


def shape_connect_receipt(receipt: dict) -> dict:
    if not isinstance(receipt, dict) or not receipt.get("connected"):
        return receipt
    sid = str(receipt.get("session_id") or "")
    attach = receipt.get("xaacp_attach")
    messaging, actor_id, reason = _attach_state(attach)

    head: dict = {}
    if messaging == MESSAGING_READY:
        actor = f" as XAACP actor {actor_id}" if actor_id else ""
        head["verdict"] = f"CONNECTED: session {sid} bound{actor}; messaging ready."
    elif messaging == MESSAGING_NOT_ATTEMPTED:
        head["verdict"] = (
            f"CONNECTED (local only): session {sid} bound; no XAACP attach was attempted "
            "for this caller."
        )
    else:
        head["verdict"] = (
            f"PARTIAL: session {sid} bound locally, but this caller is NOT an XAACP actor "
            f"({reason}). ai_msg XAACP calls will be refused until that is fixed."
        )
    head["messaging"] = messaging
    if messaging == MESSAGING_CONFIRM_REQUIRED:
        # No confirm value and no runnable call here: a turnkey self-confirm at
        # the top of the receipt is exactly what an agent would act on. The
        # value stays only in the raw xaacp_attach (r0b2 2cf48ec6-efd).
        head["approval_pending"] = True
        head["next_action"] = (
            "Operator confirmation is required before this connect is retried. Ask "
            "the operator explicitly; do not submit the confirmation value without "
            "their approval. A confirmation value is not operator approval."
        )

    rest = {k: v for k, v in receipt.items() if k not in _HEADLINE_KEYS}
    plans = rest.get("plans")
    if isinstance(plans, list):
        kept = [p for p in plans if not (isinstance(p, dict) and p.get("status") == "no_steps")]
        if len(kept) != len(plans):
            rest["plans"] = kept
            rest["plans_without_steps"] = len(plans) - len(kept)
    return {**head, **rest}
