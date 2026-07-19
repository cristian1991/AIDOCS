"""CONNECTED login gate (#404 wiring phase, 2026-07-16).

Login is REQUIRED to use AIDOCS. This module is the ONE pure seam the
host hooks consult: :func:`login_required_block` answers "is this
caller logged in?" and, when not, returns a structured block carrying
an actionable login message. It fails OPEN on internal error — the
gate must never wedge a live host session on a resolver bug.

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

Setup-phase seam: a project whose identity store has NO user account
cannot satisfy a login demand — there is nothing to log in AS, and the
first-operator on-ramp is the provisioning flow, not this gate. The
gate therefore arms only once at least one operator account exists.

External-dependency seam (flagged): the browser/OAuth authority login
(codenexus loopback, see apps/aidocs-dashboard/src-tauri/src/main.rs::
webmcp_oauth_capture) is referenced in the operator message but the
CLI OAuth capture is a TODO — the PASSWORD path (``aidocs
operator-login``) is the proven on-ramp.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

LOGIN_BLOCKED_BY = "login_required"

_LOGIN_HELP = (
    "AIDOCS requires login. No valid operator token or approved "
    "host-session binding was presented.\n"
    "To log in:\n"
    "  1. Password login (mints a bearer token, cached machine-side):\n"
    "       aidocs operator-login --email <you@example.com> --password <pw>\n"
    "     The token is cached at ~/.aidocs/operator_token.json and later\n"
    "     aidocs commands pick it up automatically until it expires\n"
    "     (env AIDOCS_OPERATOR_TOKEN / --operator-token still override).\n"
    "  2. Or approve this host session from the AIDOCS dashboard "
    "(host-operator binding / OAuth authority login at /oauth/authorize).\n"
    "After logging in, retry."
)


def login_message() -> str:
    """The actionable operator-facing login instructions."""
    return _LOGIN_HELP


def _identity_provisioned(project_root: Path) -> bool:
    """True when at least one operator account exists (login is possible).

    Read-only: never creates the identity DB. Fail-open (False) on any
    IO/schema error — an unreadable store must not brick the host.
    """
    try:
        db = (
            Path(project_root)
            / ".MEMORY"
            / ".index"
            / "aidocs_identity.sqlite3"
        )
        if not db.is_file():
            return False
        with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM identity_users WHERE disabled=0"
                ).fetchone()
            except sqlite3.OperationalError:
                return False  # table absent → nothing to log in as
        return bool(row and int(row[0]) > 0)
    except Exception:
        return False


def _stage_pending_binding(root: Path, host_session_id: str) -> str:
    """Ensure a PENDING host-operator binding exists for this host
    session so the dashboard has something to approve. Idempotent:
    a live pending for the same (canonical) session id is reused, not
    superseded, so repeated blocked prompts keep ONE approvable row.

    Returns extra operator-facing message lines ('' when nothing could
    be staged). Best-effort + fail-quiet: staging must never crash the
    gate, and it NEVER authenticates anyone.
    """
    if not host_session_id:
        return ""
    try:
        from .host_operator_binding_store import (
            HostOperatorBindingStore,
            _canon_sid,
        )

        store = HostOperatorBindingStore()
        for b in store.list_pending(root):
            if _canon_sid(b.host_session_id) == _canon_sid(host_session_id):
                return (
                    "\nA pairing request for this session is already pending "
                    f"(binding {b.binding_id}). Approve it while logged in: "
                    "dashboard 'Bind to me', or\n"
                    f"  aidocs dashboard-binding-approve --binding-id {b.binding_id}"
                )
        binding_id, code = store.create_pending(
            root,
            host_kind="host_hook",
            host_session_id=host_session_id,
        )
        return (
            "\nA pairing request was created for this session — "
            f"pairing code {code} (binding {binding_id}).\n"
            "Approve it while logged in: dashboard 'Bind to me', or\n"
            f"  aidocs dashboard-binding-approve --binding-id {binding_id}"
        )
    except Exception:
        return ""  # staging is best-effort; the block below still stands


def login_required_block(
    project_root: Path | str,
    host_session_id: str = "",
) -> dict[str, str] | None:
    """Return None when the caller is logged in (or the gate cannot
    arm), else ``{"blocked_by": "login_required", "reason": <message>}``.

    Decision seam shared by PreToolUse and UserPromptSubmit. Fail-open
    on internal error: never wedge the host. The block path stages an
    approvable pending binding (see :func:`_stage_pending_binding`) —
    the ONLY mutation this module performs, and it grants nothing.
    """
    try:
        root = Path(project_root)
        sid = str(host_session_id or "").strip()
        from . import project_authority as _pa

        if _pa._authenticated_uid(root, sid):
            return None  # logged in — non-brick path
        if not _identity_provisioned(root):
            return None  # setup phase — no account to log in as yet
        extra = _stage_pending_binding(root, sid)
        return {
            "blocked_by": LOGIN_BLOCKED_BY,
            "reason": login_message() + extra,
        }
    except Exception:
        return None  # fail-open: an internal error must not wedge the host
