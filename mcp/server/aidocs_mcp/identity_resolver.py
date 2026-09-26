"""Identity resolver (2026-04-21).

Resolves "who is the current acting principal?" for audit stamping.
Single source of truth consulted by every record_event call site so
operator vs agent vs subagent attribution stays consistent.

Resolution order:
  1. AIDOCS_EXPERT_LANE_ID env set → principal_type='subagent', user_id
     = the spawning operator (inherited from the parent process env).
  2. AIDOCS_OPERATOR_ID env set → that's the acting user (Profile B
     dashboard launches pass this).
  3. AIDOCS_OPERATOR_EMAIL env set → lookup in identity_store.
  4. Fall back to single bootstrapped local user via
     identity_store.list_users()[0] (Profile A/solo flavor).
  5. NO FALLBACK (#936). Unresolved stays UNATTRIBUTED_USER ("").
     This step used to mint the literal 'operator' and call it an
     "audit-safe default"; it was the opposite of audit-safe, because
     'operator' is a REAL user_id and a reader could not tell it from a
     resolved one. The execution_events column default it matched has
     moved to '' for the same reason.

Caches the resolution per-process (~lifetime of one MCP server run).
UserPromptSubmit doesn't clear it — a fresh CLI session triggers a
new process anyway.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC
from pathlib import Path

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[str, str, str]] = {}  # project_key → (user_id, email, principal_type)

# The honest answer when a principal's role cannot be resolved (#576 D1).
# Deliberately NOT a role name: it must never appear in a role set, an
# auditor grade, or any allow-list. Consumers that want a different
# default when resolution fails must compare against this explicitly.
UNKNOWN_ROLE = "unknown"


def _cache_key(project_root: Path) -> str:
    try:
        return str(project_root.resolve()).replace("\\", "/")
    except Exception:
        return str(project_root).replace("\\", "/")


# #936. The user_id recorded for an actor that could NOT be resolved.
#
# Empty, not a word, and deliberately not a plausible one. UNKNOWN_ROLE can be
# 'unknown' because no role is spelled that way, so the sentinel is
# unambiguous. Every candidate word for a USER is a user_id somebody could
# hold — 'operator' most of all, since bootstrap_local_superadmin mints exactly
# that. Only the empty string cannot collide with a real actor.
#
# This is the honest-empty convention (#672) the other eight stores already use
# for an unattributed user (rbac_store, session_freeze_store,
# sticky_grants_store, host_operator_binding_store); the audit ledger was the
# outlier.
UNATTRIBUTED_USER = ""


def current_user(
    project_root: Path,
) -> tuple[str, str, str]:
    """Return (user_id, email, principal_type) for audit stamping.

    Cached per-process + per-project. Safe to call hot-path (hook +
    orchestrator). Never raises.

    An identity that does not resolve comes back as UNATTRIBUTED_USER ("") —
    #936. There is NO fallback to a person: this function feeds the audit
    ledger, and a ledger that invents an actor is not evidence.
    """
    key = _cache_key(project_root)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    # Resolution attempts. Any failure falls through to the next.
    user_id = ""
    email = ""
    principal_type = "human"

    # 1. Subagent / lane worker env.
    lane_id = os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()
    if lane_id:
        principal_type = "subagent"
        user_id = os.environ.get("AIDOCS_OPERATOR_ID", "").strip() or ""
        email = os.environ.get("AIDOCS_OPERATOR_EMAIL", "").strip() or ""

    # 2. Explicit operator id from env (dashboard launch or CI).
    if not user_id:
        user_id = os.environ.get("AIDOCS_OPERATOR_ID", "").strip() or ""

    # 3. Operator email from env → identity_store lookup.
    if not user_id:
        email = os.environ.get("AIDOCS_OPERATOR_EMAIL", "").strip() or email
        if email:
            try:
                from .identity_store import IdentityStore

                u = IdentityStore().get_user_by_email(project_root, email)
                if u is not None:
                    user_id = u.user_id
                    email = u.email
            except Exception:
                pass

    # 4. Bootstrapped local super_admin (Profile A/solo).
    if not user_id:
        try:
            from .identity_store import IdentityStore

            users = IdentityStore().list_users(project_root)
            # First non-disabled user wins. In solo flavor there's
            # exactly one (the bootstrapped super_admin).
            for u in users:
                if not u.disabled:
                    user_id = u.user_id
                    email = u.email
                    break
        except Exception:
            pass

    # 5. NO FALLBACK (#936). An unresolved identity stays UNATTRIBUTED_USER
    #    ("", the initial value) and an unresolved address stays "".
    #
    #    What used to be here — user_id = "operator"; email = email or
    #    "operator@local" — manufactured a PERSON. #631 removed the same lie
    #    from the role column and left this one standing, so a ledger row could
    #    read user_id='operator', effective_role='unknown': self-contradictory,
    #    with the lying half naming a human. The email was the worse of the
    #    two, being shaped like a real address and so even harder to doubt.
    #
    #    SAFE BY CONSTRUCTION, and established before changing rather than
    #    assumed: this value is attribution-only and NEVER authority
    #    (scratch/planning/AUTHORITY_MATRIX.md; operator_auth_service "It is
    #    NEVER consulted"; issue_filing_service "attribution-only by
    #    doctrine"). No gate reads it, so nothing here can widen one.
    #    principal_type IS security-consumed — enforcement.hard_protected_
    #    authority requires human — and is deliberately untouched; see
    #    test_the_principal_type_channel_is_untouched, which fences it.

    with _LOCK:
        _CACHE[key] = (user_id, email, principal_type)
    return user_id, email, principal_type


def current_user_id(project_root: Path) -> str:
    """Convenience shim — just the id."""
    return current_user(project_root)[0]


def current_principal_type(project_root: Path) -> str:
    """Convenience shim — just the principal_type (human/agent/subagent)."""
    return current_user(project_root)[2]


def invalidate_cache(project_root: Path | None = None) -> None:
    """Drop cached identity. Called on session_connect + when
    env changes. Optional project_root → invalidate just that one.
    """
    with _LOCK:
        if project_root is None:
            _CACHE.clear()
        else:
            _CACHE.pop(_cache_key(project_root), None)


def current_effective_role(
    project_root: Path,
    user_id: str,
) -> str:
    """Best-effort resolution of the user's highest-authority role at
    global scope. Used for execution_events.effective_role stamping.

    Returns ``UNKNOWN_ROLE`` when RBAC data is unavailable or no
    assignment matches (#576 D1).

    This used to return the literal 'super_admin' so audit rows "matched
    the column default". That made the field a LIE: service_hub stamps it
    into the rbac_denied audit event and into the operator-facing
    refusal, so a principal holding NO ROLE was reported as super_admin —
    and every consumer that ever believed the field (dashboards, audit
    queries, incident reconstruction) inherited it. Empire law 183074ae:
    an empty attribution acting as a wildcard is worse than a missing
    capability. Attribution is not authorization, but an unknown
    attribution must SAY unknown.

    Callers that legitimately want a different default when the role
    cannot be resolved must treat ``UNKNOWN_ROLE`` as unresolved
    explicitly (see operator_auth_service) — never re-introduce a
    permissive default here.
    """
    try:
        from .rbac_store import RBACStore

        store = RBACStore()
        store.init_db(project_root)
        import sqlite3

        # #755/#756: the ONE canonical connect. This was
        # `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION
        # context manager, which commits and NEVER closes the handle --
        # with no pragmas. Role attribution is a pure SELECT, so
        # read_only=True; sqlite3.Row is kept below unchanged.
        from ._sqlite_connect import connect as _canonical_connect

        with _canonical_connect(
            str(store.db_path(project_root)), read_only=True
        ) as conn:
            conn.row_factory = sqlite3.Row
            # Pick the role with the LOWEST rank (strongest) that the
            # user holds at global scope + not expired.
            from datetime import datetime

            now = datetime.fromtimestamp(
                __import__("time").time(),
                tz=UTC,
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = conn.execute(
                "SELECT r.name FROM rbac_user_roles ur "
                "JOIN rbac_roles r ON r.role_id = ur.role_id "
                "WHERE ur.user_id = ? "
                "AND ur.scope_type = 'global' "
                "AND (ur.expires_at IS NULL OR ur.expires_at > ?) "
                "ORDER BY r.rank ASC LIMIT 1",
                (user_id, now),
            ).fetchone()
            if row is not None:
                return str(row["name"])
    except Exception:
        pass
    return UNKNOWN_ROLE
