"""ai_todo + ai_backlog MCP tool registration.

ai_todo — task-owned execution support. Lighter shape, smaller taxonomy.
ai_backlog — project-owned durable future work. Fuller metadata,
              strict status lifecycle, priority buckets.

Shared architecture:
- dispatcher pattern matching ai_slop(mode=...)
- remove is tombstone (status='removed'), never physical DELETE
- remove requires reason (>=8 chars after trim, see reason_validator)
- audit via execution_events (action_kind='todo_*' / 'backlog_*')
- deterministic: no NLP, no heuristic classification
- mutations on ai_todo limited to current-task-owned rows

See .MEMORY/system/invariants.md for the filing-vs-action rule that
shapes these tools (explicit args, deterministic queries, no fuzzy).
"""

from __future__ import annotations

import time
from typing import Any

from . import project_backlog_store, task_todos_store
from .dual_audience import (
    fail_edit as _fail_edit,
)
from .dual_audience import (
    fmt_tags as _fmt_tags,
)
from .dual_audience import (
    ok_edit as _ok_edit,
)
from .mcp_server_runtime_helpers import require_active_task, resolve_project_root
from .reason_validator import validate_reason

# KISS list defaults (#59): list is an INVENTORY surface, not a
# READING surface. Default shape: id/title/status/priority/tags/
# updated_at(date-only). Body is fetched per-item via mode="get"
# with offset/limit paging — never via list.
_BACKLOG_LIST_DEFAULT_LIMIT = 20
_TAGS_DEFAULT_CAP = 3
_CONTENT_PREVIEW_CHARS = 120

# KISS get defaults (#59): single-item read with body paging. Bodies
# are mini-specs (often 5-15k chars); paging keeps any single call
# bounded. Default page: 6000 chars. Hard cap: 8000. No "give me the
# whole thing" escape — if it doesn't fit in 8000, page it.
_BACKLOG_GET_BODY_LIMIT_DEFAULT = 6000
_BACKLOG_GET_BODY_LIMIT_MAX = 8000


def _shorten_iso_date(value: str) -> str:
    """ISO timestamp → date-only (YYYY-MM-DD). Pass through on parse fail."""
    if not value:
        return value
    return value[:10] if len(value) >= 10 and value[4] == "-" else value


def _cap_tags(tags: list[str], cap: int = _TAGS_DEFAULT_CAP) -> list[str]:
    """Cap tag list at `cap`, append '+N' marker for the remainder.

    Keeps default rows lean while preserving the most useful filter
    signals (first 3 tags). Full tag list is one include_tags=True
    away when callers need it.
    """
    if not tags or len(tags) <= cap:
        return list(tags or [])
    head = list(tags[:cap])
    head.append(f"+{len(tags) - cap}")
    return head


def _shape_default_list_item(
    item: dict[str, Any],
    *,
    include_tags: bool,
    include_preview: bool,
) -> dict[str, Any]:
    """KISS list inventory shape — id/title/status/priority/updated_at,
    plus optional tags/preview. Body is fetched via mode="get" only.
    """
    out: dict[str, Any] = {
        "id": item.get("id"),
        "title": item.get("title") or "",
        "status": item.get("status"),
        "priority": item.get("priority"),
        "updated_at": _shorten_iso_date(str(item.get("updated_at") or "")),
    }
    if include_tags:
        out["tags"] = _cap_tags(item.get("tags") or [])
    if include_preview:
        body = str(item.get("content") or "")
        if len(body) > _CONTENT_PREVIEW_CHARS:
            out["content_preview"] = body[:_CONTENT_PREVIEW_CHARS] + "..."
        else:
            out["content_preview"] = body
    return out


def _project_list_items(
    project_root: Any,
    items: list[dict[str, Any]],
    columns: list[str] | None,
) -> list[dict[str, Any]]:
    """Apply RBAC-aware column projection. Caller role resolved from
    the session's current principal (falls back to 'super_admin' in
    solo flavor per identity_resolver contract).

    Used by ai_todo's mode='list' path. ai_backlog moved to KISS
    include_* flags via _shape_default_list_item; this helper stays
    for the as-yet-unrefactored todo surface.
    """
    try:
        from .identity_resolver import (
            current_effective_role,
            current_user_id,
        )
        from .list_projection import project_items

        user_id = current_user_id(project_root)
        role = current_effective_role(project_root, user_id)
        return project_items(items, columns=columns, caller_role=role)
    except Exception:
        # Projection/RBAC failure must not break the read. Fall back to
        # returning items as-is — no worse than pre-projection behavior.
        return list(items)


def register_todo_backlog_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
) -> None:

    def _current_task_and_session(project_root) -> tuple[str, str]:
        """Read current (task_id, session_id) from session_query_gate."""
        try:
            from .managed_mode_service import ManagedModeService  # noqa

            managed = hub.managed_mode.get_mode(project_root)
            session_id = str(managed.get("session_id") or "")
        except Exception:
            session_id = ""
        task_id = ""
        try:
            task_id = str(
                hub.query_gate.get_current_task_id(
                    project_root,
                    session_id,
                )
                or "",
            )
        except Exception:
            pass
        return task_id, session_id

    def _audit_todo(
        project_root,
        *,
        action: str,
        todo_id: int,
        session_id: str,
        task_id: str,
        reason: str | None = None,
    ) -> None:
        try:
            hub.execution.record_event(
                project_root,
                event_kind="todo_mutation",
                source_kind="ai_todo",
                session_id=session_id,
                capability_name="ai_todo",
                action_kind=f"todo_{action}",
                target_entity=str(todo_id),
                status="ok",
                payload={"task_id": task_id, "reason": reason or ""},
            )
        except Exception:
            pass

    def _audit_backlog(
        project_root,
        *,
        action: str,
        backlog_id: int,
        session_id: str,
        reason: str | None = None,
    ) -> None:
        try:
            hub.execution.record_event(
                project_root,
                event_kind="backlog_mutation",
                source_kind="ai_backlog",
                session_id=session_id,
                capability_name="ai_backlog",
                action_kind=f"backlog_{action}",
                target_entity=str(backlog_id),
                status="ok",
                payload={"reason": reason or ""},
            )
        except Exception:
            pass

    @server.tool(
        annotations={"destructiveHint": False, "openWorldHint": False, "title": "Task Todo"},
    )
    def ai_todo(
        mode: str,
        content: str = "",
        id: int = 0,
        status: str = "",
        tags: list[str] | None = None,
        scope: str = "task",
        include_done: bool = False,
        include_removed: bool = False,
        reason: str = "",
        columns: list[str] | None = None,
    ) -> Any:
        """Task-owned todo items. Modes: add | list | update | remove.

        Agent output terse (✓ id=N); operator output formatted.
        See rules/standards.md dual-audience rule.

        add: requires active task. Creates todo owned by current task.
        list: scope='task' (default, current-task unresolved) or
              'session' (unresolved todos from ANY task in current
              session). include_done/include_removed widen visibility.
        update: mutations restricted to current-task-owned rows.
        remove: tombstone (status='removed'). Reason required,
                >=8 chars after trim.
        """
        t0 = time.perf_counter()
        project_root = resolve_project_root()
        task_id, session_id = _current_task_and_session(project_root)

        if mode == "add":
            gate = require_active_task(hub, project_root, "ai_todo")
            if gate is not None:
                return gate
            if not task_id:
                return _fail_edit(
                    error="no active task (call task_begin first)",
                    tool_name="ai_todo",
                    started_at=t0,
                )
            if not (content or "").strip():
                return _fail_edit(
                    error="content required (non-empty)",
                    tool_name="ai_todo",
                    started_at=t0,
                )
            r = task_todos_store.add(
                project_root,
                task_id=task_id,
                session_id=session_id,
                content=content.strip(),
                tags=tags,
            )
            _audit_todo(
                project_root,
                action="add",
                todo_id=r["id"],
                session_id=session_id,
                task_id=task_id,
            )
            tag_suffix = _fmt_tags(r.get("tags") or [])
            return _ok_edit(
                ack=f"✓ id={r['id']}",
                pretty_lines=[f'📝 todo #{r["id"]} added: "{r["content"]}"{tag_suffix}'],
                structured={"id": r["id"]},
                tool_name="ai_todo",
                started_at=t0,
            )

        if mode == "list":
            if scope == "session":
                if not session_id:
                    return _fail_edit(
                        error="no active session for scope='session'",
                        tool_name="ai_todo",
                        started_at=t0,
                    )
                items = task_todos_store.list_for_session_unresolved(
                    project_root,
                    session_id=session_id,
                    include_done=include_done,
                    include_removed=include_removed,
                )
            else:
                if not task_id:
                    return _fail_edit(
                        error="no active task for scope='task'. Use scope='session' or open a task.",
                        tool_name="ai_todo",
                        started_at=t0,
                    )
                items = task_todos_store.list_for_task(
                    project_root,
                    task_id=task_id,
                    include_done=include_done,
                    include_removed=include_removed,
                )
            projected = _project_list_items(
                project_root,
                items,
                columns,
            )
            return {"ok": True, "count": len(projected), "items": projected}

        if mode == "update":
            gate = require_active_task(hub, project_root, "ai_todo")
            if gate is not None:
                return gate
            if not id:
                return _fail_edit(
                    error="id required for update",
                    tool_name="ai_todo",
                    started_at=t0,
                )
            if status and status not in {"open", "in_progress", "done", "skipped", "blocked"}:
                return _fail_edit(
                    error=f"status {status!r} invalid",
                    tool_name="ai_todo",
                    started_at=t0,
                )
            r = task_todos_store.update(
                project_root,
                todo_id=int(id),
                task_id=task_id,
                status=status or None,
                content=content or None,
                tags=tags,
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "update failed"),
                    tool_name="ai_todo",
                    started_at=t0,
                )
            _audit_todo(
                project_root,
                action="update",
                todo_id=int(id),
                session_id=session_id,
                task_id=task_id,
            )
            changes = []
            if status:
                changes.append(f"status={status}")
            if content:
                changes.append(f'content="{content[:50]}{"..." if len(content) > 50 else ""}"')
            if tags is not None:
                changes.append(f"tags={tags}")
            return _ok_edit(
                ack=f"✓ id={id}",
                pretty_lines=[
                    f"📝 todo #{id} updated: {', '.join(changes) if changes else '(no changes)'}",
                ],
                structured={"id": int(id)},
                tool_name="ai_todo",
                started_at=t0,
            )

        if mode == "remove":
            gate = require_active_task(hub, project_root, "ai_todo")
            if gate is not None:
                return gate
            if not id:
                return _fail_edit(
                    error="id required for remove",
                    tool_name="ai_todo",
                    started_at=t0,
                )
            reason_check = validate_reason(reason)
            if reason_check is not None:
                return _fail_edit(
                    error=reason_check["error"],
                    tool_name="ai_todo",
                    started_at=t0,
                    extra_structured={"code": reason_check.get("code")},
                )
            r = task_todos_store.remove(
                project_root,
                todo_id=int(id),
                task_id=task_id,
                reason=reason.strip(),
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "remove failed"),
                    tool_name="ai_todo",
                    started_at=t0,
                )
            _audit_todo(
                project_root,
                action="remove",
                todo_id=int(id),
                session_id=session_id,
                task_id=task_id,
                reason=reason.strip(),
            )
            if r.get("already_removed"):
                return _ok_edit(
                    ack=f"✓ id={id}",
                    pretty_lines=[f"🗑 todo #{id} already removed (no-op)"],
                    structured={"id": int(id), "already_removed": True},
                    tool_name="ai_todo",
                    started_at=t0,
                )
            return _ok_edit(
                ack=f"✓ id={id}",
                pretty_lines=[f'🗑 todo #{id} removed: "{reason.strip()}"'],
                structured={"id": int(id)},
                tool_name="ai_todo",
                started_at=t0,
            )

        return _fail_edit(
            error=f"unknown mode {mode!r}. Use: add|list|update|remove",
            tool_name="ai_todo",
            started_at=t0,
        )

    @server.tool(
        annotations={"destructiveHint": False, "openWorldHint": False, "title": "Project Backlog"},
    )
    def ai_backlog(
        mode: str,
        content: str = "",
        id: int = 0,
        status: str = "",
        priority: str = "",
        tags: list[str] | None = None,
        tag_filter: str = "",
        include_removed: bool = False,
        limit: int = 0,
        reason: str = "",
        include_tags: bool = True,
        include_preview: bool = False,
        body_offset: int = 0,
        body_limit: int = 0,
    ) -> Any:
        """Project-owned durable backlog. Modes: add | list | get | update | remove.

        Agent output terse (✓ id=N); operator output formatted.
        See rules/standards.md dual-audience rule.

        Status semantics:
          open/in_progress/blocked/done — active work lifecycle
          rejected — considered and intentionally declined as work
          removed — admin tombstone, cleanup without product judgment

        add: requires active task (#82, 2026-05-10) — every backlog
             write ties to an in-flight work item for audit-trail
             traceability. Defaults priority='medium' if omitted.
        list: INVENTORY surface (KISS, #59). Filter by status/priority/
              tag. Default hides removed; include_removed=True shows
              tombstoned rows. Default shape: id/title/status/priority/
              tags(cap-3+N)/updated_at(date-only). Default limit 20.
              Toggles:
                include_tags=False — drop tags entirely (fully lean)
                include_preview=True — add 120-char content_preview
              List NEVER returns full body. Use mode="get" for body.
        get: READING surface for one item. Required: id. Returns full
             metadata + paged body. body_offset (default 0) + body_limit
             (default 6000, hard cap 8000). Response includes
             body_returned_chars / body_total_chars / body_truncated so
             callers can page. Bodies over 8000 chars require multiple
             get calls — by design.
        update: non-destructive metadata edit. content edits re-derive
                title automatically.
        remove: tombstone. Reason required, >=8 chars after trim.
        """
        t0 = time.perf_counter()
        project_root = resolve_project_root()
        task_id_for_source, session_id = _current_task_and_session(project_root)

        if mode == "add":
            gate = require_active_task(hub, project_root, "ai_backlog")
            if gate is not None:
                return gate
            if not (content or "").strip():
                return _fail_edit(
                    error="content required (non-empty)",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            r = project_backlog_store.add(
                project_root,
                content=content.strip(),
                priority=(priority or "medium"),
                tags=tags,
                created_in_session_id=session_id or None,
                source_task_id=task_id_for_source or None,
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "add failed"),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            _audit_backlog(project_root, action="add", backlog_id=r["id"], session_id=session_id)
            tag_suffix = _fmt_tags(r.get("tags") or [])
            preview = r["content"][:60] + ("..." if len(r["content"]) > 60 else "")
            return _ok_edit(
                ack=f"✓ id={r['id']}",
                pretty_lines=[
                    f'📋 backlog #{r["id"]} added: "{preview}" '
                    f"[priority={r['priority']}]{tag_suffix}",
                ],
                structured={"id": r["id"]},
                tool_name="ai_backlog",
                started_at=t0,
            )

        if mode == "list":
            # KISS inventory surface (#59): slim shape only — id/title/
            # status/priority/tags(cap-3)/updated_at(date-only). No body
            # ever. Use mode="get" to read a specific item.
            effective_limit = limit if limit > 0 else _BACKLOG_LIST_DEFAULT_LIMIT
            items = project_backlog_store.list_backlog(
                project_root,
                status=status or None,
                priority=priority or None,
                tag_filter=tag_filter or None,
                include_removed=include_removed,
                limit=effective_limit,
            )
            shaped = [
                _shape_default_list_item(
                    it,
                    include_tags=include_tags,
                    include_preview=include_preview,
                )
                for it in items
            ]
            return {
                "ok": True,
                "count": len(shaped),
                "items": shaped,
                "limit_applied": effective_limit,
                "shape": "preview" if include_preview else "slim",
            }

        if mode == "get":
            # KISS reading surface (#59): single item, paged body.
            if not id:
                return _fail_edit(
                    error="id required for get",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            offset = max(0, int(body_offset))
            requested = body_limit if body_limit > 0 else _BACKLOG_GET_BODY_LIMIT_DEFAULT
            page_size = max(1, min(int(requested), _BACKLOG_GET_BODY_LIMIT_MAX))
            row = project_backlog_store.get_by_id(project_root, backlog_id=int(id))
            if row is None:
                return _fail_edit(
                    error=f"backlog id={id} not found",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            body = str(row.get("content") or "")
            total = len(body)
            slice_end = min(total, offset + page_size)
            body_slice = body[offset:slice_end] if offset < total else ""
            return {
                "ok": True,
                "id": row.get("id"),
                "title": row.get("title") or "",
                "status": row.get("status"),
                "priority": row.get("priority"),
                "tags": row.get("tags") or [],
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "body": body_slice,
                "body_offset": offset,
                "body_returned_chars": len(body_slice),
                "body_total_chars": total,
                "body_truncated": slice_end < total,
            }

        if mode == "update":
            gate = require_active_task(hub, project_root, "ai_backlog")
            if gate is not None:
                return gate
            if not id:
                return _fail_edit(
                    error="id required for update",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            r = project_backlog_store.update(
                project_root,
                backlog_id=int(id),
                status=status or None,
                content=content or None,
                priority=priority or None,
                tags=tags,
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "update failed"),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            _audit_backlog(project_root, action="update", backlog_id=int(id), session_id=session_id)
            changes = []
            if status:
                changes.append(f"status={status}")
            if priority:
                changes.append(f"priority={priority}")
            if content:
                changes.append(f'content="{content[:40]}{"..." if len(content) > 40 else ""}"')
            if tags is not None:
                changes.append(f"tags={tags}")
            return _ok_edit(
                ack=f"✓ id={id}",
                pretty_lines=[
                    f"📋 backlog #{id} updated: {', '.join(changes) if changes else '(no changes)'}",
                ],
                structured={"id": int(id)},
                tool_name="ai_backlog",
                started_at=t0,
            )

        if mode == "remove":
            gate = require_active_task(hub, project_root, "ai_backlog")
            if gate is not None:
                return gate
            if not id:
                return _fail_edit(
                    error="id required for remove",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            reason_check = validate_reason(reason)
            if reason_check is not None:
                return _fail_edit(
                    error=reason_check["error"],
                    tool_name="ai_backlog",
                    started_at=t0,
                    extra_structured={"code": reason_check.get("code")},
                )
            r = project_backlog_store.remove(
                project_root,
                backlog_id=int(id),
                reason=reason.strip(),
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "remove failed"),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            _audit_backlog(
                project_root,
                action="remove",
                backlog_id=int(id),
                session_id=session_id,
                reason=reason.strip(),
            )
            if r.get("already_removed"):
                return _ok_edit(
                    ack=f"✓ id={id}",
                    pretty_lines=[f"🗑 backlog #{id} already removed (no-op)"],
                    structured={"id": int(id), "already_removed": True},
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            return _ok_edit(
                ack=f"✓ id={id}",
                pretty_lines=[f'🗑 backlog #{id} removed: "{reason.strip()}"'],
                structured={"id": int(id)},
                tool_name="ai_backlog",
                started_at=t0,
            )

        return _fail_edit(
            error=f"unknown mode {mode!r}. Use: add|list|get|update|remove",
            tool_name="ai_backlog",
            started_at=t0,
        )
