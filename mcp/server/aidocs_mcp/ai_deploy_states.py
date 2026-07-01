"""ai_deploy stepwise state machine + the one-time dashboard sign-link.

ai_deploy is multi-step. The agent TRIGGERS a deploy, but the SECURE half — the TOTP second factor
and the static password that unlocks the signing key — happens on the operator's OWN dashboard,
never inside the agent's (ChatGPT/Claude) harness. So a triggered deploy advances through EXPLICIT
states, and the agent is handed a ONE-TIME link to the dashboard to complete the 2-factor sign.

This module is the pure state model + link/token logic (no I/O, no persistence): the queue, the
daemon, the tools, and the dashboard all wire onto it.

Happy path:
    queued -> awaiting_2fa --(operator: TOTP + password on the dashboard -> key signs)--> signing
           -> testing (DEV) -> promoting (DEV->LIVE) -> ok
Any step may fail -> failed; a POST-promote failure -> rolled_back. ok / failed / rolled_back are
terminal.
"""

from __future__ import annotations

import secrets
from urllib.parse import quote

# ── States ──────────────────────────────────────────────────────────
QUEUED = "queued"
AWAITING_2FA = "awaiting_2fa"  # the agent has the link; waiting for the operator's dashboard sign
SIGNING = "signing"            # 2FA + password accepted; the release is being signed
TESTING = "testing"            # signed; running the gate's DEV-custody test suite
PROMOTING = "promoting"        # tests green; atomically promoting DEV -> LIVE
OK = "ok"
FAILED = "failed"
ROLLED_BACK = "rolled_back"    # a post-promote failure rolled LIVE back to the previous release

ORDERED = (QUEUED, AWAITING_2FA, SIGNING, TESTING, PROMOTING, OK, FAILED, ROLLED_BACK)
_TERMINAL = frozenset({OK, FAILED, ROLLED_BACK})

# Legal forward transitions. Anything not listed is refused (no step-skipping, no resurrecting a
# terminal state). FAILED is reachable from every non-terminal step; ROLLED_BACK only after promote.
_TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({AWAITING_2FA, FAILED}),
    AWAITING_2FA: frozenset({SIGNING, FAILED}),
    SIGNING: frozenset({TESTING, FAILED}),
    TESTING: frozenset({PROMOTING, FAILED}),
    PROMOTING: frozenset({OK, ROLLED_BACK, FAILED}),
    OK: frozenset(),
    FAILED: frozenset(),
    ROLLED_BACK: frozenset(),
}


class StateError(Exception):
    """Named, fail-closed refusal: an unknown state or an illegal transition."""


def is_terminal(state: str) -> bool:
    """True for ok / failed / rolled_back — states that never advance again."""
    return state in _TERMINAL


def can_advance(current: str, target: str) -> bool:
    """True iff `current -> target` is a legal forward transition."""
    return target in _TRANSITIONS.get(current, frozenset())


def advance(current: str, target: str) -> str:
    """Return `target` iff `current -> target` is legal; else raise StateError. The single
    fail-closed chokepoint every caller (daemon/dashboard) goes through to move a deploy forward —
    so an illegal jump (e.g. queued -> promoting, or reviving a terminal state) is impossible."""
    if current not in _TRANSITIONS:
        raise StateError(f"unknown deploy state {current!r}")
    if target not in _TRANSITIONS[current]:
        raise StateError(f"illegal deploy transition {current!r} -> {target!r}")
    return target


# ── One-time dashboard sign-link ────────────────────────────────────
# The agent is handed a link to the operator dashboard to complete the 2-factor sign. The link
# carries a ONE-TIME token (bound to the deploy_id) so only the holder of that exact link can drive
# the sign flow — a different agent/session cannot hijack a pending deploy. The token is a secret;
# persist only a HASH of it (caller's job) and compare in constant time.

_SIGN_PATH = "/deploy/sign"


def new_sign_token() -> str:
    """A fresh, high-entropy one-time token for a deploy's dashboard sign-link."""
    return secrets.token_urlsafe(32)


def token_matches(presented: str, expected: str) -> bool:
    """Constant-time equality for a presented sign-token vs the expected one. Fail-closed on empty."""
    if not presented or not expected:
        return False
    return secrets.compare_digest(str(presented), str(expected))


def sign_link(dashboard_base_url: str, *, deploy_id: str, token: str) -> str:
    """Build the one-time operator dashboard sign-link for a deploy. `dashboard_base_url` is the
    operator's superadmin dashboard origin (e.g. https://codenexus.cloud). Raises StateError on a
    missing deploy_id/token so a link is never emitted without its one-time secret."""
    if not deploy_id or not token:
        raise StateError("refuse: sign-link needs both deploy_id and a one-time token")
    base = (dashboard_base_url or "").rstrip("/")
    return f"{base}{_SIGN_PATH}?deploy_id={quote(deploy_id)}&token={quote(token)}"
