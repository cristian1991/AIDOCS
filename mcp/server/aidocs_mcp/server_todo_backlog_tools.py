"""Todo + ai_backlog tool implementations.

todo handler — task-owned execution support. Lighter shape, smaller
              taxonomy. #83 HARD merge (Emperor ruling 2026-07-18, no
              alias): the standalone ai_todo tool is GONE from every
              advertised surface; the impl lives here as
              build_todo_handler() and is invoked by ai_task's
              scope-driven dispatch (server_plan_task_tools.ai_task,
              modes add|list|remove + update with scope='task'|'session').
ai_backlog — project-owned durable future work. Fuller metadata,
              strict status lifecycle, priority buckets.

Shared architecture:
- dispatcher pattern matching ai_slop(mode=...)
- remove is tombstone (status='removed'), never physical DELETE
- remove requires reason (>=8 chars after trim, see reason_validator)
- audit via execution_events (action_kind='todo_*' / 'backlog_*')
- deterministic: no NLP, no heuristic classification
- todo mutations limited to current-task-owned rows

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


# #20 (SSOT-05 #386): opt-in token-cheaper list renderings. Default stays
# structured JSON (zero regression); format='table'|'csv' replaces `items`
# with a rendered string field. get/add/update stay structured — tables
# don't fit nested single records.
_FORMAT_CELL_TRUNCATE = 60


def _format_cell(value: Any) -> str:
    """Markdown-table cell: arrays comma-join, dicts JSON-string fallback,
    newlines/pipes escaped, >60 chars truncated with '…'."""
    import json as _json

    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    elif isinstance(value, dict):
        value = _json.dumps(value, ensure_ascii=False)
    text = str(value if value is not None else "")
    text = text.replace("\n", " ").replace("|", "\\|")
    if len(text) > _FORMAT_CELL_TRUNCATE:
        text = text[: _FORMAT_CELL_TRUNCATE - 1] + "…"
    return text


def _rows_as_table(rows: list[dict[str, Any]]) -> str:
    """Markdown table; header from the first row's keys (post-projection).
    Empty input → empty string (count=0 already says it all)."""
    if not rows:
        return ""
    keys = list(rows[0].keys())
    lines = [
        "| " + " | ".join(keys) + " |",
        "|" + "|".join("---" for _ in keys) + "|",
    ]
    lines.extend("| " + " | ".join(_format_cell(r.get(k)) for k in keys) + " |" for r in rows)
    return "\n".join(lines)


def _rows_as_csv(rows: list[dict[str, Any]]) -> str:
    """RFC 4180 CSV (csv module handles quoting); arrays/dicts JSON-serialized;
    no truncation (machine consumption)."""
    import csv as _csv
    import io as _io
    import json as _json

    if not rows:
        return ""
    keys = list(rows[0].keys())
    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\r\n")
    writer.writerow(keys)
    for r in rows:
        writer.writerow(
            [
                _json.dumps(v, ensure_ascii=False)
                if isinstance(v, (list, dict))
                else ("" if v is None else v)
                for v in (r.get(k) for k in keys)
            ],
        )
    return buf.getvalue()


def _apply_list_format(out: dict[str, Any], rows: list[dict[str, Any]], fmt: str) -> dict[str, Any]:
    """Swap `items` for a rendered field when format='table'|'csv'. Unknown /
    empty / 'structured' → unchanged (fail-open to today's behavior)."""
    if fmt == "table":
        out.pop("items", None)
        out["items_table"] = _rows_as_table(rows)
        out["format"] = "table"
    elif fmt == "csv":
        out.pop("items", None)
        out["items_csv"] = _rows_as_csv(rows)
        out["format"] = "csv"
    return out


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

    Used by the todo handler's mode='list' path (ai_task todo modes).
    ai_backlog moved to KISS include_* flags via
    _shape_default_list_item; this helper stays for the
    as-yet-unrefactored todo surface.
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


# #101 operator-facing urgency markers (todo urgency / backlog priority).
_URGENCY_ICONS = {
    "critical": "🔴",
    "urgent": "🟠",
    "high": "🟡",
    "normal": "⚪",
    "low": "·",
    "idea": "…",
}


def _urgency_icon(level: str) -> str:
    return _URGENCY_ICONS.get(level, "⚪")


def _current_task_and_session(hub: Any, project_root) -> tuple[str, str]:
    """Read current (task_id, session_id) from session_query_gate."""
    try:
        # Identity doctrine (2026-07-16): resolve with the calling
        # host identity so attribution keys off the host binding,
        # not the project singleton.
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
        )

        managed = hub.managed_mode.get_mode(
            project_root,
            host_session_id=current_calling_host_session_id(),
        )
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
    hub: Any,
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
            source_kind="ai_task",
            session_id=session_id,
            capability_name="ai_task",
            action_kind=f"todo_{action}",
            target_entity=str(todo_id),
            status="ok",
            payload={"task_id": task_id, "reason": reason or ""},
        )
    except Exception:
        pass


def _audit_backlog(
    hub: Any,
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


def build_todo_handler(*, hub: Any) -> Any:
    """Build the todo impl callable dispatched by ai_task's todo modes.

    #83 HARD merge (no-alias law): this is NOT registered as a standalone
    MCP tool anywhere. server_plan_task_tools.ai_task routes
    mode='add'|'list'|'remove' (and mode='update' with
    scope='task'|'session') here; the callable keeps the exact pre-merge
    ai_todo semantics and payload shapes (parity-pinned by
    tests/security/test_ai_task_todo_merge.py).
    """

    def todo_handler(
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
        urgency: str = "",
        format: str = "",
    ) -> Any:
        """Task-owned todo items (ai_task todo modes: add|list|update|remove).

        Agent output terse (✓ id=N); operator output formatted.
        See rules/standards.md dual-audience rule.

        add: requires active task. Creates todo owned by current task.
             Optional urgency (#101): critical > urgent > high > normal
             (default) > low. Lists sort urgent-first.
        list: scope='task' (default, current-task unresolved) or
              'session' (unresolved todos from ANY task in current
              session). include_done/include_removed widen visibility.
              tags=[...] keeps only todos whose tag set intersects the
              requested tags (any-of); empty/absent = no tag filtering.
        update: mutations allowed for current-task rows AND rows owned by
                prior tasks of the SAME session (task supersession must
                not orphan a session's todos). Cross-session refused.
        remove: tombstone (status='removed'). Reason required,
                >=8 chars after trim.
        list format='table'|'csv' (#20): opt-in token-cheap rendering
        (items → items_table / items_csv). Default: structured JSON.
        """
        t0 = time.perf_counter()
        project_root = resolve_project_root()
        task_id, session_id = _current_task_and_session(hub, project_root)

        if mode == "add":
            if not task_id:
                return _fail_edit(
                    error="no active task — start or select a task first (todos are task-owned)",
                    tool_name="ai_task",
                    started_at=t0,
                )
            if not (content or "").strip():
                return _fail_edit(
                    error="content required (non-empty)",
                    tool_name="ai_task",
                    started_at=t0,
                )
            r = task_todos_store.add(
                project_root,
                task_id=task_id,
                session_id=session_id,
                content=content.strip(),
                tags=tags,
                urgency=(urgency or "normal"),
            )
            if not r.get("ok", True):
                return _fail_edit(
                    error=str(r.get("error") or "add failed"),
                    tool_name="ai_task",
                    started_at=t0,
                )
            _audit_todo(
                hub,
                project_root,
                action="add",
                todo_id=r["id"],
                session_id=session_id,
                task_id=task_id,
            )
            tag_suffix = _fmt_tags(r.get("tags") or [])
            icon = _urgency_icon(r.get("urgency") or "normal")
            return _ok_edit(
                ack=f"✓ id={r['id']}",
                pretty_lines=[f'📝 {icon} todo #{r["id"]} added: "{r["content"]}"{tag_suffix}'],
                structured={"id": r["id"]},
                tool_name="ai_task",
                started_at=t0,
            )

        if mode == "list":
            if scope == "session":
                if not session_id:
                    return _fail_edit(
                        error="no active session for scope='session'",
                        tool_name="ai_task",
                        started_at=t0,
                    )
                items = task_todos_store.list_for_session_unresolved(
                    project_root,
                    session_id=session_id,
                    include_done=include_done,
                    include_removed=include_removed,
                    tags=tags,
                )
            else:
                if not task_id:
                    return _fail_edit(
                        error="no active task for scope='task'. Use scope='session' or open a task.",
                        tool_name="ai_task",
                        started_at=t0,
                    )
                items = task_todos_store.list_for_task(
                    project_root,
                    task_id=task_id,
                    include_done=include_done,
                    include_removed=include_removed,
                    tags=tags,
                )
            projected = _project_list_items(
                project_root,
                items,
                columns,
            )
            out = {"ok": True, "count": len(projected), "items": projected}
            return _apply_list_format(out, projected, format)

        if mode == "update":
            gate = require_active_task(hub, project_root, "ai_task")
            if gate is not None:
                return gate
            if not id:
                return _fail_edit(
                    error="id required for update",
                    tool_name="ai_task",
                    started_at=t0,
                )
            if status and status not in {"open", "in_progress", "done", "skipped", "blocked"}:
                return _fail_edit(
                    error=f"status {status!r} invalid",
                    tool_name="ai_task",
                    started_at=t0,
                )
            r = task_todos_store.update(
                project_root,
                todo_id=int(id),
                task_id=task_id,
                session_id=session_id or None,
                status=status or None,
                content=content or None,
                tags=tags,
                urgency=urgency or None,
                reason=reason or None,
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "update failed"),
                    tool_name="ai_task",
                    started_at=t0,
                )
            _audit_todo(
                hub,
                project_root,
                action="update",
                todo_id=int(id),
                session_id=session_id,
                task_id=task_id,
            )
            changes = []
            if status:
                changes.append(f"status={status}")
            if urgency:
                changes.append(f"urgency={_urgency_icon(urgency)} {urgency}")
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
                tool_name="ai_task",
                started_at=t0,
            )

        if mode == "remove":
            gate = require_active_task(hub, project_root, "ai_task")
            if gate is not None:
                return gate
            if not id:
                return _fail_edit(
                    error="id required for remove",
                    tool_name="ai_task",
                    started_at=t0,
                )
            reason_check = validate_reason(reason)
            if reason_check is not None:
                return _fail_edit(
                    error=reason_check["error"],
                    tool_name="ai_task",
                    started_at=t0,
                    extra_structured={"code": reason_check.get("code")},
                )
            r = task_todos_store.remove(
                project_root,
                todo_id=int(id),
                task_id=task_id,
                session_id=session_id or None,
                reason=reason.strip(),
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "remove failed"),
                    tool_name="ai_task",
                    started_at=t0,
                )
            _audit_todo(
                hub,
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
                    tool_name="ai_task",
                    started_at=t0,
                )
            return _ok_edit(
                ack=f"✓ id={id}",
                pretty_lines=[f'🗑 todo #{id} removed: "{reason.strip()}"'],
                structured={"id": int(id)},
                tool_name="ai_task",
                started_at=t0,
            )

        return _fail_edit(
            error=f"unknown mode {mode!r}. Use: add|list|update|remove",
            tool_name="ai_task",
            started_at=t0,
        )

    return todo_handler


def register_todo_backlog_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
) -> None:
    """Register ai_backlog. #83 hard merge: the todo surface is no longer
    a standalone MCP tool — ai_task's todo modes call build_todo_handler().
    """

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
        include_merged: bool = False,
        limit: int = 0,
        reason: str = "",
        include_tags: bool = True,
        include_preview: bool = False,
        body_offset: int = 0,
        body_limit: int = 0,
        ids: list[int] | None = None,
        umbrella_id: int = 0,
        allow_clear: bool = False,
        format: str = "",
    ) -> Any:
        """Project-owned durable backlog. Modes: add | list | get | update | remove | merge.

        Status: open/in_progress/blocked/done; rejected = declined as work;
        removed = tombstone. Priority ladder (#101): critical > urgent >
        high > normal (default) > low > idea ('medium' = legacy normal).

        add: requires an active task (#82); audited.
        list: slim INVENTORY only (id/title/status/priority/tags cap-3/
              updated_at date). Filters status/priority/tag_filter/tags.
              include_removed/include_merged widen; include_preview=True
              adds a 120-char preview; include_tags=False drops tags;
              format='table'|'csv' (#20) opt-in rendering. Default limit
              20; truncation is announced. NEVER returns bodies — use get.
        get: one item + paged body — body_offset/body_limit (default 6000,
             hard cap 8000); body_total_chars/body_truncated guide paging.
        update: non-destructive (#399) — only passed fields change;
                clearing the body needs content='' + allow_clear=True.
                Reactivating a merged item clears merged_into.
        remove: tombstone; reason required (>=8 chars).
        merge (#450): ids=[...] (>=2 open) fold into one umbrella
               (umbrella_id optional; lowest id survives); absorbed rows →
               status='merged' + merged_into; reversible via
               update(status='open').

        Dual audience: terse agent ack (✓ id=N) + formatted operator lines.
        add's trailing 'similar' = open items sharing >=2 tags with the
        new item (advisory candidate-overlap list; never auto-merges).
        """
        t0 = time.perf_counter()
        project_root = resolve_project_root()
        task_id_for_source, session_id = _current_task_and_session(hub, project_root)

        if mode == "add":
            if not (content or "").strip():
                return _fail_edit(
                    error="content required (non-empty)",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            r = project_backlog_store.add(
                project_root,
                content=content.strip(),
                priority=(priority or "normal"),
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
            _audit_backlog(hub, project_root, action="add", backlog_id=r["id"], session_id=session_id)
            tag_suffix = _fmt_tags(r.get("tags") or [])
            preview = r["content"][:60] + ("..." if len(r["content"]) > 60 else "")
            pretty = [
                f'📋 backlog #{r["id"]} added: "{preview}" '
                f"[{_urgency_icon(r['priority'])} priority={r['priority']}]{tag_suffix}",
            ]
            structured: dict[str, Any] = {"id": r["id"]}
            # #450 suggestion half: candidate overlaps by shared tags (>=2
            # common) among OPEN items. Advisory only — never auto-merges;
            # one terse line per candidate (#429 output-slimming spirit).
            try:
                similar = project_backlog_store.similar_open_items(
                    project_root,
                    tags=r.get("tags") or [],
                    exclude_id=r["id"],
                )
            except Exception:
                similar = []
            if similar:
                structured["similar"] = similar
                pretty.extend(
                    f"≈ similar open item: #{s['id']} {s['title']}" for s in similar
                )
            return _ok_edit(
                ack=f"✓ id={r['id']}",
                pretty_lines=pretty,
                structured=structured,
                tool_name="ai_backlog",
                started_at=t0,
            )

        if mode == "list":
            # KISS inventory surface (#59): slim shape only — id/title/
            # status/priority/tags(cap-3)/updated_at(date-only). No body
            # ever. Use mode="get" to read a specific item.
            effective_limit = limit if limit > 0 else _BACKLOG_LIST_DEFAULT_LIMIT
            # Fetch ONE MORE than asked, so truncation is DETECTABLE. Without
            # this, `count == limit` means either "you saw everything" or "you
            # were silently cut off" — indistinguishable, so a full page reads as
            # a complete answer. That silence misled an adversarial auditor
            # (2026-07-13) who was dispatched to hunt exactly this sin: it saw
            # 100 of 223 done items and reasoned about a truncated world.
            # Note the +1 (not `count == limit`): with exactly N rows and limit
            # N, the caller DID see everything, and crying wolf on every full
            # page would train the reader to ignore the flag.
            probe = project_backlog_store.list_backlog(
                project_root,
                status=status or None,
                priority=priority or None,
                tag_filter=tag_filter or None,
                tags=tags,
                include_removed=include_removed,
                include_merged=include_merged,
                limit=effective_limit + 1,
            )
            truncated = len(probe) > effective_limit
            items = probe[:effective_limit]
            shaped = [
                _shape_default_list_item(
                    it,
                    include_tags=include_tags,
                    include_preview=include_preview,
                )
                for it in items
            ]
            out = {
                "ok": True,
                "count": len(shaped),
                "items": shaped,
                "limit_applied": effective_limit,
                "truncated": truncated,
                "shape": "preview" if include_preview else "slim",
            }
            if truncated:
                out["warning"] = (
                    f"TRUNCATED — MORE than {effective_limit} items match this query; "
                    f"you are NOT seeing all of them. Re-run with a higher limit (or "
                    f"narrow status/priority/tags) BEFORE drawing any conclusion from "
                    f"this list. A truncated list looks exactly like a complete one."
                )
            return _apply_list_format(out, shaped, format)

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
                "merged_into": row.get("merged_into"),
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
            # #399 non-destructive contract: omitted content ('') means
            # UNTOUCHED. An explicit clear requires content='' AND
            # allow_clear=True — only then does '' reach the store.
            content_arg = content if content else ("" if allow_clear else None)
            r = project_backlog_store.update(
                project_root,
                backlog_id=int(id),
                status=status or None,
                content=content_arg,
                priority=priority or None,
                tags=tags,
                reason=reason or None,
                allow_clear=allow_clear,
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "update failed"),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            _audit_backlog(hub, project_root, action="update", backlog_id=int(id), session_id=session_id)
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
                hub,
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

        if mode == "merge":
            gate = require_active_task(hub, project_root, "ai_backlog")
            if gate is not None:
                return gate
            id_list = [int(v) for v in (ids or [])]
            if len(id_list) < (1 if umbrella_id else 2):
                return _fail_edit(
                    error="merge requires ids=[...] with >= 2 backlog ids "
                    "(or >= 1 plus an explicit umbrella_id)",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            r = project_backlog_store.merge(
                project_root,
                ids=id_list,
                umbrella_id=int(umbrella_id) if umbrella_id else None,
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "merge failed"),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            _audit_backlog(
                hub,
                project_root,
                action="merge",
                backlog_id=int(r["umbrella_id"]),
                session_id=session_id,
                reason=f"absorbed={r['merged_ids']}",
            )
            return _ok_edit(
                ack=f"✓ id={r['umbrella_id']}",
                pretty_lines=[
                    f"🔗 backlog #{r['umbrella_id']} absorbed "
                    f"{len(r['merged_ids'])} item(s): {r['merged_ids']}"
                    f"{_fmt_tags(r.get('tags') or [])}",
                ],
                structured={
                    "id": int(r["umbrella_id"]),
                    "merged_ids": r["merged_ids"],
                    "tags": r.get("tags") or [],
                },
                tool_name="ai_backlog",
                started_at=t0,
            )

        return _fail_edit(
            error=f"unknown mode {mode!r}. Use: add|list|get|update|remove|merge",
            tool_name="ai_backlog",
            started_at=t0,
        )
