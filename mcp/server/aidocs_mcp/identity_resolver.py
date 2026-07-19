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
  5. Final fallback: 'operator' literal (audit-safe default that
     matches the execution_events column default).

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


def _cache_key(project_root: Path) -> str:
    try:
        return str(project_root.resolve()).replace("\\", "/")
    except Exception:
        return str(project_root).replace("\\", "/")


def current_user(
    project_root: Path,
) -> tuple[str, str, str]:
    """Return (user_id, email, principal_type) for audit stamping.

    Cached per-process + per-project. Safe to call hot-path (hook +
    orchestrator). Never raises — falls back to 'operator' literal.
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

    # 5. Final fallback.
    if not user_id:
        user_id = "operator"
        email = email or "operator@local"

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

    Returns 'super_admin' as a fallback when RBAC data is unavailable
    so dev/solo-flavor audit rows match the column default.
    """
    try:
        from .rbac_store import RBACStore

        store = RBACStore()
        store.init_db(project_root)
        import sqlite3

        with sqlite3.connect(str(store.db_path(project_root))) as conn:
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
    return "super_admin"
