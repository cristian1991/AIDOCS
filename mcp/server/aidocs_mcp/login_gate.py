"""CONNECTED login gate (#404 wiring phase, 2026-07-16).

Login is REQUIRED to use AIDOCS. This module is the ONE pure seam the
host hooks consult: :func:`login_required_block` answers "is this
caller logged in?" and, when not, returns a structured block carrying
an actionable login message. It fails CLOSED on authentication-resolution errors: uncertainty cannot
broaden authority, and the returned reason remains generic/actionable.

Approvable-block invariant (operator bug 2026-07-17): a block for a
known host_session_id also STAGES an idempotent PENDING host-operator
binding, so the dashboard approval queue (`aidocs
dashboard-binding-list` / the dashboard "Bind to me" surface) has
something to approve — previously the gate blocked while the queue
stayed empty, so a dashboard login could never unbrick the session.
Staging is best-effort and NEVER authenticates: the block is still
returned until a real authenticated operator approves the binding.

Authentication truth comes from ``project_authority._authenticated_uid``
(fail-closed): a valid ``AIDOCS_OPERATOR_TOKEN`` bearer token, or an
APPROVED host-operator binding for the calling host session. Nothing
else counts — env identities, audit attribution, and flavor rows are
NOT authority (the local bootstrap auto-mint was excised).

Unprovisioned-project seam: absence of a local identity row is never
authority. The gate still blocks and stages an approvable host binding;
the dashboard/Codenexus login path is the on-ramp.

External-dependency seam (flagged): the browser/OAuth authority login
(codenexus loopback, see apps/aidocs-dashboard/src-tauri/src/main.rs::
webmcp_oauth_capture) is referenced in the operator message but the
CLI OAuth capture is a TODO — the PASSWORD path (``aidocs
operator-login``) is the proven on-ramp.
"""

from __future__ import annotations

from pathlib import Path

LOGIN_BLOCKED_BY = "login_required"

# Operator ruling 2026-07-25: a refusal is STATE + ACTION + ESCAPE, never a
# manual. The old block spent 12 lines on token-cache paths, env overrides and
# OAuth URLs before the operator could see what to DO. Everything cut lives in
# `aidocs login --help`; the pairing lines (staged separately) append below.
_LOGIN_HELP = (
    "Blocked: not logged in (no operator token or approved host binding).\n"
    "→ Dashboard: approve this session — 'Bind to me'\n"
    "→ CLI:       aidocs operator-login --email <you> --password <pw>\n"
    "Details: aidocs login --help"
)


def login_message() -> str:
    """The actionable operator-facing login instructions."""
    return _LOGIN_HELP



# A pairing line the operator cannot act on is worse than no line: it sends
# them to a row that dies while they read it. #557 S1 — a pending with only
# seconds of TTL left is treated as DEAD and replaced with a fresh one rather
# than printed.
_PAIRING_MIN_REMAINING_SECONDS = 60


def _approve_line(binding_id: str) -> str:
    return (
        "\nPairing pending — approve in dashboard, or: "
        f"aidocs dashboard-binding-approve --binding-id {binding_id}"
    )


def _live_pending_id(store, root: Path, host_session_id: str) -> str:
    """binding_id of this session's pending row that is still worth
    PRINTING, or ''. ``list_pending`` already drops rows past their
    expiry; this additionally rejects a row about to die (#557 S1).
    """
    import time

    from .host_operator_binding_store import _canon_sid, _parse_iso

    want = _canon_sid(host_session_id)
    now = time.time()
    for b in store.list_pending(root):
        if _canon_sid(b.host_session_id) != want:
            continue
        expires = _parse_iso(b.expires_at)
        if expires is not None and (expires - now) < _PAIRING_MIN_REMAINING_SECONDS:
            continue  # dead on arrival — mint a fresh pairing instead
        return str(b.binding_id or "")
    return ""


def _approved_binding_id(store, root: Path, host_session_id: str) -> str:
    """binding_id of this session's live APPROVED row, or ''."""
    from .host_operator_binding_store import _canon_sid

    want = _canon_sid(host_session_id)
    match = ""
    for b in store.list_bindings(root, statuses=("approved",)):
        if _canon_sid(b.host_session_id) == want:
            match = str(b.binding_id or "")  # ordered by created_at: latest wins
    return match


def _stage_pending_binding(
    root: Path,
    host_session_id: str,
    host_kind: str = "host_hook",
) -> str:
    """Ensure a PENDING host-operator binding exists for this host
    session so the dashboard has something to approve. Idempotent:
    a live pending for the same (canonical) session id is reused, not
    superseded, so repeated blocked prompts keep ONE approvable row.

    Two #557 residues are enforced here:

    S2 — a session that ALREADY holds an approved binding is not sent
    round the pairing loop again. Approval is not what is failing (the
    row exists and outlives the pending TTL); minting another pairing
    only adds a second approved row for the same session, which is the
    treadmill the operator hit — approve, refused, approve a NEW code,
    refused. The refusal now NAMES the binding and points at the two
    routes that can actually help: ``dashboard-binding-revoke`` (to pair
    a different operator — the very next blocked prompt then stages a
    fresh pairing) and the machine login in the base message. Nothing is
    taken away: one command restores the ordinary 'Bind to me' flow.

    S1 — a pairing line is printed only for a row with real life left,
    and a row superseded by a concurrent stage is never printed.

    Returns extra operator-facing message lines ('' when nothing could
    be staged). Best-effort + fail-quiet: staging must never crash the
    gate, and it NEVER authenticates anyone.
    """
    if not host_session_id:
        return ""
    try:
        from .host_operator_binding_store import HostOperatorBindingStore

        store = HostOperatorBindingStore()

        approved_uid = store.resolve_operator(root, host_session_id)
        if approved_uid:
            bid = _approved_binding_id(store, root, host_session_id)
            named = f" {bid}" if bid else ""
            revoke = (
                f"\n→ Re-pair:   aidocs dashboard-binding-revoke --binding-id {bid}"
                if bid
                else ""
            )
            return (
                f"\nThis host session ALREADY has an approved binding{named}"
                f" → {approved_uid}; approving another pairing will not change"
                " that. The failure is below, not in the approval." + revoke
            )

        live_id = _live_pending_id(store, root, host_session_id)
        if live_id:
            return _approve_line(live_id)
        binding_id, code = store.create_pending(
            root,
            # #906: the WEB path stages under its own kind. Defaulted so every
            # existing hook caller is byte-identical; a row minted for a web
            # connector that claimed `host_hook` would be indistinguishable in
            # the dashboard from a local window, and the operator approving it
            # could not tell WHICH client they were binding to themselves.
            host_kind=host_kind or "host_hook",
            host_session_id=host_session_id,
        )
        minted = store.get(root, binding_id)
        if minted is not None and minted.status != "pending":
            # Superseded by a concurrent stage between INSERT and read: this
            # code is already dead. Point at the row that is actually alive.
            live_id = _live_pending_id(store, root, host_session_id)
            return _approve_line(live_id) if live_id else ""
        return (
            f"\nPairing code {code} — approve in dashboard, or: "
            f"aidocs dashboard-binding-approve --binding-id {binding_id}"
        )
    except Exception:
        return ""  # staging is best-effort; the block below still stands


def stage_pairing_for(
    project_root: Path | str,
    host_session_id: str,
    host_kind: str = "",
) -> str:
    """Public seam: ensure this host session has something to APPROVE (#906).

    WHY THIS IS EXPORTED. The staging machinery was reachable only through
    ``login_required_block``, whose sole production callers are claude_hook and
    hook_pipeline -- BOTH THE LOCAL HOOK PATH. So a web caller hitting an
    authority wall got a refusal naming a permission and NOTHING TO APPROVE:
    the dashboard had no pending row, and no amount of granting
    admin.manage_sessions could help, because the caller had no authenticated
    identity for a grant to attach to. Measured 2026-08-25 with an org OWNER,
    valid entitlement, explicit org selection -- still `operator_auth`.

    NEVER AUTHENTICATES ANYONE, and cannot: it only mints/reuses a PENDING row
    that a human must still approve. Idempotent and fail-quiet, inheriting both
    from ``_stage_pending_binding`` -- a session that already holds an approved
    binding is told so rather than sent round the pairing loop again (#557 S2).
    """
    sid = str(host_session_id or "").strip()
    if not sid:
        return ""
    try:
        return _stage_pending_binding(Path(project_root), sid, host_kind=host_kind)
    except Exception:  # noqa: BLE001 -- a hint must never break a refusal
        return ""


def login_required_block(
    project_root: Path | str,
    host_session_id: str = "",
) -> dict[str, str] | None:
    """Return ``None`` only for an authenticated principal.

    Every unauthenticated or indeterminate caller receives the same actionable
    login refusal. Authentication resolver failures never authorize; pending
    host binding staging remains best-effort and never grants authority.
    """
    try:
        root = Path(project_root)
    except Exception:
        return {
            "blocked_by": LOGIN_BLOCKED_BY,
            "reason": login_message(),
        }
    sid = str(host_session_id or "").strip()

    uid = ""
    diag: list[dict[str, object]] = []
    try:
        from . import project_authority as _pa

        uid, diag = _pa._authenticated_uid_diag(root, sid)
    except Exception as exc:  # noqa: BLE001 — a resolver fault never authorizes
        diag = [{"path": "resolver", "outcome": "error", "detail": repr(exc)[:200]}]
    if uid:
        return None

    extra = _stage_pending_binding(root, sid)
    return {
        "blocked_by": LOGIN_BLOCKED_BY,
        "reason": login_message() + extra + _diag_lines(diag),
    }


def _diag_lines(diag: list[dict[str, object]]) -> str:
    """Render WHY each auth path declined, for the operator-facing refusal.

    #557: the refusal always claimed "no valid operator token or approved
    host-session binding was presented", even when the truth was that the
    process could not READ the credential store. An operator cannot fix what the
    message misdescribes, so the message now distinguishes a clean logout from a
    broken store — and, when the machine path is involved, NAMES the identity DB
    it opened and whether that file exists. A HOME/db-path divergence is then one
    look instead of an afternoon.

    TWO triggers, and the second is the one that matters. Surfacing only
    'error' outcomes was WRONG: a diverged HOME does not raise — the identity
    store simply is not there, so the machine path reports a perfectly calm
    `absent` ("no live machine-login token"). Verified by pointing HOME at an
    empty dir and reproducing #557's shape: every outcome came back `absent`
    while `exists=False` on a temp-dir store was the only tell. A diagnostic that
    stays silent in the case it was built for is decoration, so a MISSING
    identity store is surfaced too.

    Otherwise quiet: on a genuine logout with a store that exists, the existing
    help text is already correct and three lines of "absent" would be noise.
    Never includes token material — only path names, exception kinds, and a
    local db path.
    """
    errors = [d for d in diag if str(d.get("outcome")) == "error"]
    missing_store = [
        d for d in diag if "identity_db" in d and not d.get("exists")
    ]
    notable = errors + [d for d in missing_store if d not in errors]
    if not notable:
        return ""
    lines = [
        "",
        "  DIAGNOSTIC — this may be an INFRASTRUCTURE fault, not a missing login:",
    ]
    for d in notable:
        lines.append(f"    - {d.get('path')}: {d.get('outcome')} — {d.get('detail')}")
        if "identity_db" in d:
            lines.append(
                f"      identity store: {d.get('identity_db')} (exists={d.get('exists')})"
            )
    lines.append(
        "  If the store path above is not the one your `aidocs login` wrote to, "
        "this process resolved a different HOME."
    )
    return "\n".join(lines)
