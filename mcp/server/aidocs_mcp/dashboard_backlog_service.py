"""Governed backlog CRUD for the dashboard (2026-07-30).

Operator charter: "dashboard needs to be able to CRUD backlog items (RBAC)",
urgent because "i cannot actively see backlog items (dashboard is blind)".

WHY THIS MODULE EXISTS
----------------------
The desktop bridge used to marshal a ``python -c`` payload that imported
``project_backlog_store`` and called ``add`` / ``update`` / ``remove``
directly. That path had no principal, no permission check and no audit row: the
dashboard was not a client of an authority, it WAS the authority. Sealed law
``promoted-457b114130e5`` — no user is exempt from the gates, including the
operator — makes that untenable, so every dashboard backlog act now comes
through this one door.

THE SHAPE OF THE GATE
---------------------
A lawful gate names the actor, records the act, and cannot be switched off by a
value the CALLER controls. So:

* the actor comes from a bearer TOKEN this module authenticates itself — never
  from a caller-supplied identity, context or "trusted" flag;
* the verdict comes from RBAC via ``OperatorAuthService.authorize_admin_command``
  (``ctx is None`` → refused, no env fallback);
* every attempt lands an ``execution_events`` row, refusals included;
* there is no parameter that skips any of it.

A REFUSAL IS NOT AN EMPTY LIST
------------------------------
The refusal payload carries NO ``items`` / ``item`` / ``result`` key. This is
the whole point of the module: the operator's 145-item backlog rendered as a
blank page, and a blank page is indistinguishable from "there is nothing here".
A refusal must say what is missing and name the permission that would fix it.
Newly sealed law both ways — a defect must never be reported in the language of
policy, and a refusal must never be reported as absence.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not touch ``project_backlog_store``'s write paths; it calls them. It
does not change the AGENT surface (``ai_backlog``), which keeps its own task
gate and is not narrowed here. And it does not re-impose a task requirement on
add/update that the API deliberately dropped — only ``remove`` is destructive,
and its gate is the ``backlog.remove`` permission.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .permission_catalog import (
    PERM_BACKLOG_READ,
    PERM_BACKLOG_REMOVE,
    PERM_BACKLOG_WRITE,
)

# Every action a dashboard may take, mapped to the permission that authorizes
# it. An action absent from this table cannot reach the store at all (see
# `unknown_action` below) — a missing entry is a refusal, never a default,
# because a default here would be an ungated write path.
BACKLOG_ACTION_PERMISSIONS: dict[str, str] = {
    "list": PERM_BACKLOG_READ,
    "get": PERM_BACKLOG_READ,
    "add": PERM_BACKLOG_WRITE,
    "update": PERM_BACKLOG_WRITE,
    "remove": PERM_BACKLOG_REMOVE,
}

# Actions that change state. Used only to label the audit row's action_kind —
# the AUTHORITY comes from the permission map above, never from this set.
_MUTATING_ACTIONS = frozenset({"add", "update", "remove"})

_REFUSAL_MESSAGES = {
    "unauthenticated": (
        "not signed in. Reading or changing the backlog from the dashboard "
        "requires an authenticated operator holding {perm}. Sign in via the "
        "Dashboard (or 'aidocs operator-login'); the token is cached for this "
        "machine until it expires. THIS IS A REFUSAL, NOT AN EMPTY BACKLOG — "
        "the items are there, this session is not permitted to read them."
    ),
    "_missing": (
        "your account is authenticated but lacks the {perm} permission at this "
        "project. Ask an admin to grant {perm} or to assign a role that holds "
        "it. THIS IS A REFUSAL, NOT AN EMPTY BACKLOG."
    ),
}


def _audit(
    project_root: Path,
    *,
    action: str,
    permission: str,
    status: str,
    actor: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record the attempt. Best-effort by design: fail open on the REPORT,
    fail closed on the GRANT (``promoted-06ad3c5f61ab``). A broken audit store
    must never be able to block the refusal it was supposed to describe, and it
    must never be able to turn a refusal into an allow either — the verdict is
    already decided before we get here."""
    try:
        from .execution_index_store import ExecutionIndexStore

        payload: dict[str, Any] = {
            "action": action,
            "required_permission": permission,
            "reason": reason,
            "operator_authenticated": bool(actor),
            "source": "dashboard_backlog",
        }
        if extra:
            payload.update(extra)
        ExecutionIndexStore().record_event(
            project_root,
            event_kind="control_plane_mutation",
            source_kind="dashboard_backlog",
            capability_name=f"dashboard-backlog-{action}",
            action_kind="backlog_mutation" if action in _MUTATING_ACTIONS else "backlog_read",
            target_entity=action,
            status=status,
            payload=payload,
            user_id=actor or None,
            permission_name=permission,
            scope_type="project",
            scope_id=str(project_root).replace("\\", "/"),
        )
    except Exception:
        pass


def _refusal(
    project_root: Path,
    *,
    action: str,
    permission: str,
    reason: str,
    actor: str = "",
) -> dict[str, Any]:
    """Build the refusal AND audit it. Deliberately returns no data key: a
    caller cannot render this as an empty result."""
    template = _REFUSAL_MESSAGES.get(reason, _REFUSAL_MESSAGES["_missing"])
    message = f"Backlog {action} refused: " + template.format(perm=permission)
    _audit(
        project_root,
        action=action,
        permission=permission,
        status="refused",
        actor=actor,
        reason=reason,
    )
    return {
        "ok": False,
        "blocked_by": "operator_auth",
        "reason": reason,
        "required_permission": permission,
        "action": action,
        "message": message,
    }


def dashboard_backlog(
    project_root: Path | str,
    action: str,
    *,
    token: str | None = None,
    backlog_id: int | None = None,
    content: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    reason: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run one backlog action on behalf of an authenticated dashboard operator.

    Returns either ``{"ok": True, "action": ..., <data>}`` or a refusal
    (``{"ok": False, "blocked_by": "operator_auth", "reason", ...}``) that
    carries NO data key. There is no third outcome and no silent no-op.

    ``token`` is the operator's bearer credential. Note what is NOT a
    parameter: no context, no identity, no role, no bypass. The caller supplies
    a credential and this function decides — which is the difference between a
    gate and a suggestion.
    """
    root = Path(project_root)
    action = str(action or "").strip()

    permission = BACKLOG_ACTION_PERMISSIONS.get(action)
    if permission is None:
        # Unknown actions refuse rather than fall through to something
        # permissive. Not audited as a policy refusal because no policy decided
        # it — a bad action name is a caller bug, and the only lawful cause of
        # failure is tampering, so this is triaged as a bug, not a verdict.
        return {
            "ok": False,
            "blocked_by": "input",
            "reason": "unknown_action",
            "action": action,
            "message": (
                f"Unknown backlog action {action!r}. Known actions: "
                f"{', '.join(sorted(BACKLOG_ACTION_PERMISSIONS))}."
            ),
        }

    from .operator_auth_service import OperatorAuthService

    auth = OperatorAuthService()
    ctx = auth.authenticate(token, root, source="dashboard") if token else None
    allowed, why = auth.authorize_admin_command(
        ctx,
        root,
        permission=permission,
        scope_type="project",
        scope_id=str(root).replace("\\", "/"),
    )
    actor = str(getattr(ctx, "user_id", "") or "") if ctx is not None else ""
    if not allowed:
        return _refusal(root, action=action, permission=permission, reason=why, actor=actor)

    from . import project_backlog_store

    try:
        if action == "list":
            kwargs: dict[str, Any] = {}
            if status:
                kwargs["status"] = status
            if priority:
                kwargs["priority"] = priority
            if limit:
                kwargs["limit"] = int(limit)
            out: dict[str, Any] = {
                "ok": True,
                "action": action,
                "items": project_backlog_store.list_backlog(root, **kwargs),
            }
        elif action == "get":
            if backlog_id is None:
                return _bad_input(action, "backlog_id is required")
            out = {
                "ok": True,
                "action": action,
                "item": project_backlog_store.get_by_id(root, backlog_id=int(backlog_id)),
            }
        elif action == "add":
            if not (content or "").strip():
                return _bad_input(action, "content is required")
            result = project_backlog_store.add(
                root,
                content=str(content),
                **_opt(priority=priority, kind=kind, tags=tags),
            )
            out = _store_result(action, result)
        elif action == "update":
            if backlog_id is None:
                return _bad_input(action, "backlog_id is required")
            result = project_backlog_store.update(
                root,
                backlog_id=int(backlog_id),
                **_opt(
                    status=status,
                    content=content,
                    priority=priority,
                    kind=kind,
                    tags=tags,
                    reason=reason,
                ),
            )
            out = _store_result(action, result)
        else:  # remove — the only destructive action, admin tier only.
            if backlog_id is None:
                return _bad_input(action, "backlog_id is required")
            if not (reason or "").strip():
                return _bad_input(action, "reason is required to remove an item")
            result = project_backlog_store.remove(
                root, backlog_id=int(backlog_id), reason=str(reason)
            )
            out = _store_result(action, result)
    except Exception as exc:  # store-level failure — a BUG, not a policy verdict.
        _audit(
            root,
            action=action,
            permission=permission,
            status="failed",
            actor=actor,
            reason="store_error",
        )
        return {
            "ok": False,
            "blocked_by": "store",
            "reason": "store_error",
            "action": action,
            "message": f"Backlog {action} failed in the store: {exc}",
        }

    _audit(
        root,
        action=action,
        permission=permission,
        status="applied" if out.get("ok") else "failed",
        actor=actor,
        reason="ok" if out.get("ok") else "store_rejected",
        extra={"backlog_id": backlog_id} if backlog_id is not None else None,
    )
    return out


def _opt(**kw: Any) -> dict[str, Any]:
    """Drop unset fields so the store's non-destructive update contract (#399)
    is preserved — passing None would be indistinguishable from "clear it"."""
    return {k: v for k, v in kw.items() if v is not None}


def _store_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    """Carry the store's own verdict through instead of asserting success.

    The store returns ``{"ok": False, "error": ...}`` for a rejected priority or
    an unknown kind. Reporting that as ``ok`` would reproduce the
    ``{"ok": true, "applied": []}`` defect in the API layer — a write that
    silently did nothing while claiming it worked."""
    if not result.get("ok", True):
        return {
            "ok": False,
            "blocked_by": "store",
            "reason": "store_rejected",
            "action": action,
            "message": str(result.get("error") or f"backlog {action} rejected"),
        }
    return {"ok": True, "action": action, "result": result}


def _bad_input(action: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "blocked_by": "input",
        "reason": "missing_args",
        "action": action,
        "message": f"Backlog {action}: {message}.",
    }
