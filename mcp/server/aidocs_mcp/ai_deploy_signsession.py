"""ai_deploy dashboard sign-session — the pure 2-factor verification the gate endpoint runs.

When the operator opens the ONE-TIME dashboard link and submits (TOTP code + static password), the
gate must verify, FAIL-CLOSED, before any signing:
  1. the deploy is in AWAITING_2FA,
  2. the presented one-time link credential matches the deploy's stored hash (constant-time), and is
     NOT already consumed (replay protection), and
  3. the TOTP code is valid for the operator's enrolled seed.
The static password is verified implicitly downstream (decrypt of the signing blob fails closed on a
wrong password). This module is the pure decision — no I/O, no persistence: the gate endpoint
persists the consumed flag and advances the state machine via ai_deploy_states.advance().
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import ai_deploy_states as states
from . import ai_deploy_totp as totp

# Stable, audited refusal codes (surfaced to the operator dashboard; never leak the link cred/seed).
REFUSE_WRONG_STATE = "deploy_not_awaiting_2fa"
REFUSE_LINK_CONSUMED = "sign_link_already_used"
REFUSE_LINK_MISMATCH = "bad_sign_link"
REFUSE_BAD_TOTP = "bad_totp_code"


def link_cred_sha256(raw_cred: str) -> str:
    """The hex sha256 of a one-time sign-link credential — what the deploy entry persists (never raw)."""
    return hashlib.sha256((raw_cred or "").encode("utf-8")).hexdigest()


def verify_sign_request(
    *,
    state: str,
    presented_cred: str,
    stored_cred_sha256: str,
    already_consumed: bool,
    totp_seed: str,
    totp_code: str,
    now: float,
) -> dict[str, Any]:
    """Fail-closed 2-factor verification of a dashboard sign submit. Returns ``{"ok": True}`` ONLY
    when the deploy is awaiting_2fa, the one-time link credential matches + is unconsumed, and the
    TOTP code is valid; otherwise ``{"ok": False, "refusal": <stable code>}``. Pure — the caller
    persists the consumed flag (one-time) and advances the state on ok."""
    if state != states.AWAITING_2FA:
        return {"ok": False, "refusal": REFUSE_WRONG_STATE}
    if already_consumed:
        return {"ok": False, "refusal": REFUSE_LINK_CONSUMED}
    if not states.token_matches(link_cred_sha256(presented_cred), str(stored_cred_sha256 or "")):
        return {"ok": False, "refusal": REFUSE_LINK_MISMATCH}
    if not totp.verify(totp_seed, totp_code, now=now):
        return {"ok": False, "refusal": REFUSE_BAD_TOTP}
    return {"ok": True, "refusal": ""}


REFUSE_UNKNOWN_DEPLOY = "unknown_deploy_id"


def process_sign_submit(
    queue_dir,
    deploy_id: str,
    *,
    presented_cred: str,
    totp_seed: str,
    totp_code: str,
    now: float,
) -> dict[str, Any]:
    """Gate-endpoint orchestration for one dashboard sign submit. Reads the deploy entry, runs the
    fail-closed 2-factor `verify_sign_request`, and ON SUCCESS atomically CONSUMES the one-time link
    (sign_link_consumed=True) and advances the state awaiting_2fa -> signing (via the legal-transition
    chokepoint). Returns the verify result; on success also the new `state`. A replayed submit then
    sees sign_link_consumed=True and is refused. The signing-key unlock (blob fetch + materialized_key
    + sign) happens AFTER this returns ok — this function only gates + advances, it never touches the
    key."""
    from . import ai_deploy_queue as queue

    payload = queue.read_payload(queue_dir, deploy_id)
    if payload is None:
        return {"ok": False, "refusal": REFUSE_UNKNOWN_DEPLOY}
    result = verify_sign_request(
        state=str(payload.get("state") or ""),
        presented_cred=presented_cred,
        stored_cred_sha256=str(payload.get("sign_token_sha256") or ""),
        already_consumed=bool(payload.get("sign_link_consumed", False)),
        totp_seed=totp_seed,
        totp_code=totp_code,
        now=now,
    )
    if not result["ok"]:
        return result
    new_state = states.advance(states.AWAITING_2FA, states.SIGNING)  # raises only on a logic bug
    queue.update_request_fields(queue_dir, deploy_id, sign_link_consumed=True, state=new_state)
    return {"ok": True, "refusal": "", "state": new_state}
