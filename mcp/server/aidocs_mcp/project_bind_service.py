"""ai_project bind service — gate + bind + escalation.

The local mirror of the outer-gate ``project_select`` (see
outer_gate_transport.py): bind the CALLING host session to an
AIDOCS-enabled project so ``resolve_project_root()`` re-roots every later
``ai_*`` call to that tree (host-session keyed → per-session, cross-user
isolated; idle-TTL'd by SessionProjectBindStore).

Authority is NOT invented here — it routes through the existing
``project_authority.require_cross_project`` gate:
  * same project (bind to your current tree) → always allowed.
  * different tree → requires the target COMMISSIONED + an APPROVED
    relation (security.approved_external_roots) + permission (solo/dev
    local-admin passthrough; corpo RBAC on PERM_ADMIN_MANAGE_SESSIONS).
On a deny, we file a pending admin-approval row via
``escalation_hook.request_escalation`` (block + escalation, per the Empire
spec) and return a structured "blocked" envelope — never a silent bind.

Imports are module-level so tests can monkeypatch the two seams
(require_cross_project / request_escalation) without lazy-import gymnastics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .escalation_hook import request_escalation
from .permission_catalog import PERM_ADMIN_MANAGE_SESSIONS
from .project_authority import _authenticated_uid, require_cross_project
from .session_project_bind_store import DEFAULT_BIND_TTL_MINUTES, SessionProjectBindStore

_BIND_PERMISSION = PERM_ADMIN_MANAGE_SESSIONS


def _configured_ttl(project_root: Path) -> int:
    """Idle-TTL minutes from config (session.project_bind_ttl_minutes,
    dashboard-tunable), falling back to the default. Read once at bind
    time and captured into the bind row — never on the hot resolve path."""
    try:
        from .config import get_setting

        val = get_setting(
            "session.project_bind_ttl_minutes",
            project_root=project_root,
            default=DEFAULT_BIND_TTL_MINUTES,
        )
        return int(val) if val is not None else DEFAULT_BIND_TTL_MINUTES
    except Exception:
        return DEFAULT_BIND_TTL_MINUTES


def bind_project(
    *,
    host_session_id: str,
    conductor_root: Path | str,
    target_root: Path | str,
) -> dict[str, Any]:
    """Bind ``host_session_id`` → ``target_root`` after the cross-project
    authority gate passes. On deny, file an escalation and return a
    blocked envelope (never bind).
    """
    sid = (host_session_id or "").strip()
    if not sid:
        return {
            "bound": False,
            "blocked_by": "no_host_session",
            "reason": (
                "ai_project bind requires a host session id (the calling "
                "conductor) — bindings are keyed per host session"
            ),
        }
    conductor = Path(conductor_root)
    target = Path(target_root)

    decision = require_cross_project(
        conductor,
        target,
        permission=_BIND_PERMISSION,
        operation="project_bind",
        host_session_id=sid,
    )
    if not decision.get("ok"):
        req_id = ""
        try:
            req = request_escalation(
                conductor,
                gate_permission=_BIND_PERMISSION,
                gate_phrase=f"project_bind {target}",
                requester_label=sid,
                extra={"target_root": str(target), "operation": "project_bind"},
            )
            req_id = getattr(req, "request_id", "") or ""
        except Exception:
            req_id = ""
        return {
            "bound": False,
            "blocked_by": decision.get("blocked_by"),
            "reason": decision.get("reason"),
            "target_root": str(target),
            "escalation_request_id": req_id,
            "message": (
                "project bind refused. An escalation has been filed; an admin "
                "can approve it from the dashboard, or add the target to "
                "security.approved_external_roots."
            ),
        }

    uid = ""
    try:
        uid = _authenticated_uid(conductor, sid)
    except Exception:
        uid = ""
    ttl = _configured_ttl(conductor)
    SessionProjectBindStore().bind(sid, target, bound_by_uid=uid, ttl_minutes=ttl)
    return {
        "bound": True,
        "project_root": str(target),
        "host_session_id": sid,
        "ttl_minutes": ttl,
        "flavor": decision.get("flavor"),
    }


def status_project(*, host_session_id: str) -> dict[str, Any]:
    """Report the calling host session's current project bind (if live)."""
    sid = (host_session_id or "").strip()
    if not sid:
        return {"bound": False, "host_session_id": "", "bound_project_root": None}
    bound = SessionProjectBindStore().resolve(sid)
    return {
        "bound": bound is not None,
        "host_session_id": sid,
        "bound_project_root": bound,
        "ttl_minutes": DEFAULT_BIND_TTL_MINUTES,
    }


def unbind_project(*, host_session_id: str) -> dict[str, Any]:
    """Drop the calling host session's project bind (revert to cwd-discovery)."""
    sid = (host_session_id or "").strip()
    if not sid:
        return {"unbound": False, "host_session_id": ""}
    removed = SessionProjectBindStore().unbind(sid)
    return {"unbound": removed, "host_session_id": sid}
