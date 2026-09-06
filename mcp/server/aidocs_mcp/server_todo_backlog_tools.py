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
from .mcp_server_runtime_helpers import (
    require_active_task_strict,
    resolve_project_root,
)

# #601: the ai_backlog modes that genuinely REFUSE without an active task.
# WRITES only. Reads (list/get) stay task-free — an agent with no task must
# still be able to READ the backlog, which is why the tool sits in
# _TASK_GATE_EXEMPT in the first place; that set is keyed on tool NAME and
# cannot say "my writes are gated", so the writes gate themselves here.
# THIS SET IS THE SINGLE SOURCE OF THE CLAIM. The tool docstring states it in
# prose, and test_backlog_task_gate_601 cross-checks the prose against this
# set in both directions — the old docstring promised an enforcement that did
# not exist and nothing could tell.
#
# OPERATOR RULING (2026-07-29): RECORDING IS NOT GATED. `add` and `update` were
# in this set and are deliberately OUT of it now.
#
# Two concrete failures drove it. First, REACHABILITY: every gate refusal in this
# system ends with a footer naming `ai_backlog(mode='add', tags=['false-positive'])`
# as the remedy. Gating `add` made the documented remedy unreachable for exactly
# the caller who had something worth filing — a named remedy that cannot be
# reached is law 311bf3e6 broken by the system pointing at itself. (#601 exempted
# ai_issues as an escape hatch, but the footers still name ai_backlog, so the
# hatch existed and nobody was told.) Second, THE SHARED SLOT: task_complete
# clears the session's single slot for every actor (#599), so a sibling agent
# finishing its work silently revoked the conductor's ability to file. The
# observed workaround was opening a throwaway task for one call — ceremony that
# produced no attribution at all.
#
# #82's GOAL SURVIVES, its ENFORCEMENT does not: attribution is a FACT RECORDED,
# not a PRECONDITION. source_task_id is still stamped whenever a task exists, and
# its absence is recorded honestly rather than converted into a refusal. That is
# the same rule the rest of this codebase now follows — an unknown gets written
# down, never turned into a gate.
#
# STRUCTURAL changes stay gated. remove/merge/unmerge alter OTHER people's
# records rather than adding to the pile, and remove already demands a reason.
_BACKLOG_TASK_GATED_MODES: frozenset[str] = frozenset({"remove", "merge", "unmerge"})
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


# ── #593: per-field applied/rejected receipts for ai_backlog(mode='update')
#
# #399 made update non-destructive: only passed fields change. Its failure
# mode is that a field which was PASSED BUT NOT UNDERSTOOD is indistinguishable
# from a field that was NOT PASSED — so a partial application returned ok:true
# and the caller had no way to tell. The cure is to stop deriving the receipt
# from the arguments and derive it from the ROW, read back after the write.
#
# Whole-call rejection is not available at this layer: the store owns the
# transaction and has already committed by the time the wrapper can observe
# the outcome, so "reject everything" would mean claiming a rollback that did
# not happen — the same lie in the other direction. Malformed VALUES (an
# unknown `kind`) are already refused atomically by the store before any write.
# What is left is the silent-drop class, and for that the honest receipt is
# per-field, with ok:false whenever the applied set is smaller than requested.

# Row keys these tool params write to.
_BACKLOG_FIELD_TO_ROW_KEY: dict[str, str] = {
    "status": "status",
    "content": "content",
    "priority": "priority",
    "kind": "kind",
    "difficulty": "difficulty",
    "tags": "tags",
}


def _backlog_requested_fields(
    *,
    status: str,
    content: str | None,
    priority: str,
    kind: str,
    difficulty: int,
    tags: list[str] | None,
) -> dict[str, Any]:
    """The fields this update call actually ASKED to change.

    Mirrors exactly what the update branch forwards to the store, so a field
    is listed here if and only if the store was told to write it.
    """
    requested: dict[str, Any] = {}
    if status:
        requested["status"] = status
    if content is not None:
        requested["content"] = content
    if priority:
        requested["priority"] = priority
    if kind:
        requested["kind"] = kind
    # 0 is the tool-layer "not passed" sentinel — it is NOT a legal rung
    # (the ladder starts at 1), so it can never be confused with a rating.
    if difficulty:
        requested["difficulty"] = difficulty
    if tags is not None:
        requested["tags"] = tags
    return requested


def _backlog_field_equal(field: str, requested: Any, stored: Any) -> bool:
    """Did `stored` end up matching what was `requested` for this field?"""
    if field == "tags":
        req = {str(t).strip() for t in (requested or []) if str(t).strip()}
        got = {str(t).strip() for t in (stored or []) if str(t).strip()}
        return req == got
    return str(requested or "").strip() == str(stored or "").strip()


def _backlog_applied_fields(
    requested: dict[str, Any],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """Split the requested fields into (applied, not_applied) by READING BACK.

    A field counts as applied when the stored value now matches the request,
    OR when the stored value changed at all — the store legitimately normalizes
    some values (the legacy priority alias 'medium' lands as 'normal'), and a
    normalized write is an understood write, not a dropped one. A field is
    reported as NOT applied only when it is both unchanged and still different
    from what the caller asked for: the actual silent-drop signature.

    If the row cannot be re-read, nothing is claimed as dropped — an unverified
    write must not be reported as a failed one.
    """
    if not isinstance(after, dict):
        return list(requested), []
    applied: list[str] = []
    not_applied: list[str] = []
    for field, want in requested.items():
        row_key = _BACKLOG_FIELD_TO_ROW_KEY[field]
        now = after.get(row_key)
        if _backlog_field_equal(field, want, now):
            applied.append(field)
            continue
        prior = before.get(row_key) if isinstance(before, dict) else None
        if not _backlog_field_equal(field, prior, now):
            # Value moved — the store understood the write and normalized it.
            applied.append(field)
            continue
        not_applied.append(field)
    return applied, not_applied


def _backlog_preview(value: Any) -> str:
    """Short, receipt-safe rendering of a requested field value."""
    if isinstance(value, list):
        return str(value)
    text = str(value if value is not None else "")
    return f'"{text[:40]}{"..." if len(text) > 40 else ""}"' if len(text) > 20 else text


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
        # #573: kind rides the slim shape — severity x kind is the triage grid,
        # and a grid with one axis missing from the inventory view is unusable.
        "kind": item.get("kind") or "",
        # Decomposition depth 1..5; None = UNRATED. It rides the slim shape for
        # the same reason kind does — the triage question is "what is big AND
        # urgent", and an axis missing from the inventory view cannot answer it.
        "difficulty": item.get("difficulty"),
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
    the session's current principal. An unresolvable principal resolves
    to identity_resolver.UNKNOWN_ROLE, which is NOT an auditor grade, so
    audit chrome is stripped (#576 D1 — it used to resolve to
    'super_admin' and hand the full record to an unattributed caller).

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
    # #599: the CALLER's own actor slot first, the shared session slot only
    # as the fallback. This is the FILING half of the stolen-slot defect:
    # `ai_task(mode='add')` refuses outright without a task_id, and every
    # backlog add stamps source_task_id from here — so an agent whose
    # session slot had been drained by a concurrent actor could not record
    # what it had just found, and what it did record was attributed to
    # another actor's task. Same read order as the universal gate
    # (require_active_task), so filing and gating can never disagree.
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
    try:
        from .task_actor_identity import resolve_caller_task_id

        task_id = resolve_caller_task_id(project_root, session_id, task_id)
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
            # #601: `ai_task` is exempt from the universal gate BY NECESSITY —
            # task_begin cannot require a task. That exemption made this call
            # (which named "ai_task") inert, so a todo write that intended to
            # be gated never was. A todo is task-OWNED; the strict form gates
            # this branch without re-gating the lifecycle modes.
            require_active_task_strict(hub, project_root, "ai_task")
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
            require_active_task_strict(hub, project_root, "ai_task")  # #601
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
        title: str = "",
        id: int = 0,
        status: str = "",
        priority: str = "",
        kind: str = "",
        kind_filter: str = "",
        difficulty: int = 0,
        difficulty_filter: str = "",
        difficulty_min: int = 0,
        difficulty_max: int = 0,
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
        append: bool = False,
        format: str = "",
    ) -> Any:
        """Project-owned durable backlog. Modes: add | list | get | update | remove | merge | unmerge.

        Status: open/in_progress/blocked/done; rejected = declined as work;
        removed = tombstone. Priority ladder (#101): critical > urgent >
        high > normal (default) > low > idea ('medium' = legacy normal).

        Kind (#573) says WHAT KIND OF MONSTER an item is — orthogonal to
        priority, which says how much it matters. known-fix | wire-up |
        design | investigate | research; default UNSET (''), never guessed,
        and MEANT to change as understanding improves. severity x kind is
        the triage grid; high-severity x known-fix is the actionable
        quadrant. An unknown kind is REJECTED, not coerced.

        Difficulty is the THIRD axis and answers what neither of the other
        two can: HOW MANY AGENTS IT TAKES TO KILL. An INTEGER 1..5 —
        1 one edit, no decomposition; 2 one agent, one pass; 3 one agent
        over several passes or a captain with a couple of workers; 4 MUST
        be decomposed, a captain with a real worker fleet; 5 needs
        sub-conductors, spans items and sessions. It is DECOMPOSITION
        DEPTH, not wall-clock time: an item is hard when it must be SPLIT,
        not when it is long. It is also a MEASUREMENT, not a permission —
        any future "how deep may this tree go" rule is a function OF this
        number, never the number itself, so the rule can change without
        re-rating every item. NO such rule is wired today. Default UNSET
        (null), never guessed; an out-of-range or non-integer value is
        REJECTED, never clamped or parsed. So the grid is
        priority x kind x difficulty: how much it matters, what kind of
        monster it is, and how deep a tree it needs.

        remove, merge and unmerge REQUIRE an active task (#82) and are
        audited. add, update, list and get never do — RECORDING IS NOT
        GATED (operator ruling 2026-07-29), so an observation can always
        be written down and the backlog stays readable without one.
        Attribution still happens: source_task_id is stamped when a task
        is open and left EMPTY when none is, never fabricated.
        Why the split: gating `add` made the remedy every gate refusal
        names — ai_backlog(mode='add') — unreachable for the one caller
        with something worth filing, and the shared task slot (#599) let
        a sibling agent's task_complete silently revoke the ability to
        record. Structural change to other people's rows stays gated.
        Refused on remove/merge with no task? Call task_begin, or use
        ai_issues, which is deliberately task-free.
        add: status is not a filing field (#818) — a new item always lands
             'open'; passing status='open' is accepted (matches reality),
             any other value is REFUSED before the row is written. Use
             mode='update' to change status after filing, with reason=.
        list: slim INVENTORY only (id/title/status/priority/kind/tags cap-3/
              updated_at date). Filters status/priority/kind_filter/
              tag_filter/tags; kind_filter='unset' selects the UNRATED.
              difficulty_filter mirrors it ('1'..'5' or 'unset'), and
              difficulty_min/difficulty_max give the RANGE the number
              exists for: difficulty_min=4 is "what needs decomposing".
              A range never sweeps in UNRATED rows.
              include_removed/include_merged widen; include_preview=True
              adds a 120-char preview; include_tags=False drops tags;
              format='table'|'csv' (#20) opt-in rendering. Default limit
              20; truncation is announced. NEVER returns bodies — use get.
        get: one item + paged body — body_offset/body_limit (default 6000,
             hard cap 8000); body_total_chars/body_truncated guide paging.
        update: only PASSED fields change (#399) — but content is NOT
                protected (#800). content=<text> REPLACES the whole body
                and RE-DERIVES the title from its first line; there is no
                history and no undo. To keep the existing reasoning and
                add to it, pass append=True — it appends and leaves the
                title alone. allow_clear=True is only for content=''.
                A status change LEAVES merged_into intact (#595): marking
                a folded child 'done' keeps it in its umbrella. ONE
                exception, the legacy #450 reactivation — status='open'
                on a row whose status is literally 'merged' clears
                merged_into and returns unmerged=True. Un-folding at any
                other status is mode='unmerge'.
        remove: tombstone; reason required (>=8 chars).
        merge (#450/#580): ids=[...] (>=2, ANY status) fold into one
               umbrella (umbrella_id optional; lowest id survives).
               Merge is a RELATION, not a state transition: absorbed rows
               get merged_into and KEEP their status ('done' stays done);
               only an 'open' item takes the legacy status='merged'.
               Mergeable forever — re-merging RE-POINTS to a new umbrella,
               an umbrella may itself be folded into a bigger one, and the
               only refusal is a REMOVED umbrella. The default list view
               hides on merged_into, not on status.
        unmerge (#580): id=N clears merged_into and LEAVES STATUS ALONE
               (update(status='open') would clobber a folded 'done' item
               back to open). Never-merged id = honest no-op.

        Dual audience: terse agent ack (✓ id=N) + formatted operator lines.
        add's trailing 'similar' = open items sharing >=2 tags with the
        new item (advisory candidate-overlap list; never auto-merges).
        """
        t0 = time.perf_counter()
        project_root = resolve_project_root()
        # #601: ONE enforcement point for every mutating mode, before dispatch.
        # It replaces four per-branch calls that named this tool — which is
        # EXEMPT from the universal gate — and so did nothing at all while
        # reading as enforcement. Refusal raises; reads never reach this.
        if mode in _BACKLOG_TASK_GATED_MODES:
            require_active_task_strict(hub, project_root, "ai_backlog")
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
                kind=(kind or None),  # #573: '' means UNSET, not a guess
                # 0 is the tool-layer "not passed" sentinel; the ladder starts
                # at 1, so it can never be mistaken for a rating. The store
                # rejects any other illegal value rather than clamping it.
                difficulty=(difficulty or None),
                tags=tags,
                created_in_session_id=session_id or None,
                source_task_id=task_id_for_source or None,
                # #818 clause 2: '' means "not passed" (the same sentinel
                # convention as kind/difficulty above) — forwarded, never
                # silently dropped. The store refuses anything other than
                # the implicit 'open' before any row is written.
                status=(status or None),
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
                (f'📋 backlog #{r["id"]} added: "{preview}" '
                f"[{_urgency_icon(r['priority'])} priority={r['priority']}]{tag_suffix}"),
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
            # #573: validate kind_filter BEFORE the query so a typo returns an
            # actionable refusal. A bad filter must never come back as an empty
            # list — an empty result reads exactly like "no such items", which
            # is how a typo'd filter gets mistaken for a clean backlog.
            if kind_filter and kind_filter != project_backlog_store.KIND_FILTER_UNSET:
                kind_err = project_backlog_store.validate_kind(kind_filter)
                if kind_err is not None:
                    return _fail_edit(
                        error=(
                            f"{kind_err} (kind_filter also accepts "
                            f"'{project_backlog_store.KIND_FILTER_UNSET}' for unrated items)"
                        ),
                        tool_name="ai_backlog",
                        started_at=t0,
                    )
            # Same treatment for difficulty, for the same reason. The MCP
            # parameter is a string, so a rung arrives as '4' — parsing THAT is
            # a transport concern, not a coercion of a rating: only exact
            # digits are accepted, and anything else is refused here rather
            # than silently matching nothing.
            difficulty_arg: Any = None
            if difficulty_filter:
                if difficulty_filter == project_backlog_store.DIFFICULTY_FILTER_UNSET:
                    difficulty_arg = project_backlog_store.DIFFICULTY_FILTER_UNSET
                elif difficulty_filter.strip().isdigit():
                    difficulty_arg = int(difficulty_filter.strip())
                else:
                    difficulty_arg = difficulty_filter  # store refuses it, with the scale
                if difficulty_arg != project_backlog_store.DIFFICULTY_FILTER_UNSET:
                    diff_err = project_backlog_store.validate_difficulty(difficulty_arg)
                    if diff_err is not None:
                        return _fail_edit(
                            error=(
                                f"{diff_err} (difficulty_filter also accepts "
                                f"'{project_backlog_store.DIFFICULTY_FILTER_UNSET}' for "
                                f"unrated items; use difficulty_min/difficulty_max for "
                                f"a range)"
                            ),
                            tool_name="ai_backlog",
                            started_at=t0,
                        )
            for _bound, _label in ((difficulty_min, "difficulty_min"),
                                   (difficulty_max, "difficulty_max")):
                if not _bound:
                    continue
                bound_err = project_backlog_store.validate_difficulty(_bound)
                if bound_err is not None:
                    return _fail_edit(
                        error=f"{_label}: {bound_err}",
                        tool_name="ai_backlog",
                        started_at=t0,
                    )
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
                kind_filter=kind_filter or None,
                difficulty_filter=difficulty_arg,
                difficulty_min=difficulty_min or None,
                difficulty_max=difficulty_max or None,
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
            # #836: mark items a LATER commit claims to have fixed. Measured
            # 2026-08-19: 7 of 27 open critical+urgent items described defects
            # that no longer existed - a 26% false-open rate - because the
            # commit knows the item number and the item never learns the commit.
            # #744 read as a live DEPLOY BLOCKER for 17 days after its own named
            # alternative landed. A stale critical consumes exactly the
            # attention the real ones need.
            #
            # FLAGS, NEVER CLOSES, and the wording stays hedged: three of those
            # seven were only PARTLY satisfied, so an auto-close would have
            # destroyed real remaining work. This is a prompt to verify.
            #
            # Only for OPEN listings - a done item having a commit is not news -
            # and fail-quiet, so a staleness hint can never break the listing.
            if str(status or "").strip().lower() in ("", "open"):
                try:
                    from . import backlog_staleness

                    backlog_staleness.annotate(project_root, shaped)
                except Exception:  # noqa: BLE001 - a hint must not break the list
                    pass
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
            # REFUSED CREATES RIDE ALONGSIDE, NEVER INSIDE (operator ruling
            # 2026-08-30). They are intents the server never accepted, so they
            # are not backlog items: counting them or letting a status filter
            # match them would make this surface disagree with the server about
            # how many items exist — the exact divergence the cutover removes.
            #
            # Computed AFTER `count`/`items`/filters are already fixed, and
            # attached only when non-empty, so an ordinary listing is
            # byte-identical to before. A rejected UPDATE is deliberately NOT
            # here: its item IS authoritative, so it stays in `items` showing the
            # server's value.
            # STEP 4, increment 1 (operator ruling 2026-08-30): authority flips
            # by SEMANTICS. Two facts ride alongside the listing — neither
            # mutates anything, and neither is folded into `items`/`count`.
            #
            #   backlog_authority   WHICH store answered. Under server authority
            #                       a reader must not mistake this surface for a
            #                       local-only one, and vice versa.
            #   legacy_local_unmigrated
            #                       PRE-BINDING rows the server has never seen.
            #                       Ruling 3: visible as explicit migration DEBT,
            #                       "not normal authoritative backlog and not
            #                       silently hidden" — the two failure directions
            #                       it sits between. Emptying this list is the
            #                       precondition for ruling 6 (demoting
            #                       project_backlog to a projection), so it is a
            #                       WORK QUEUE, not a warning.
            #
            # Attached only when non-empty / under authority, so an ordinary
            # local listing is byte-identical to before.
            try:
                from . import backlog_hub_client as _hub

                if _hub.server_read_authority(project_root):
                    out["backlog_authority"] = "server"
                    _debt = _hub.legacy_local_unmigrated(project_root)
                    # Attached whenever the state is NOT provably clean — which
                    # is either real debt OR an unanswerable question (backlog
                    # 985). Silence here means exactly one thing: known, and
                    # nothing left to migrate. An ordinary listing stays
                    # byte-identical to before, because that is the only case
                    # that attaches nothing.
                    if not _debt.satisfies_ruling_6():
                        out["legacy_local_unmigrated"] = _debt.as_payload()
                        if _debt.known:
                            out["legacy_local_unmigrated_note"] = (
                                f"{len(_debt.items)} local row(s) PREDATE this project's "
                                "server binding and were never offered to it. They are "
                                "NOT in `items` and NOT in `count` — the server is "
                                "authoritative here. They are preserved, not deleted: "
                                "migrate or classify them, and only then can the local "
                                "table be demoted to a projection."
                            )
                        else:
                            out["legacy_local_unmigrated_note"] = (
                                "MIGRATION DEBT COULD NOT BE DETERMINED "
                                f"({_debt.reason}). This is NOT a statement that there "
                                "is none: the question was unanswerable, so the local "
                                "table must NOT be demoted or cleaned on this evidence."
                            )
            except Exception:  # noqa: BLE001 — an authority note never breaks a list
                pass

            try:
                from . import backlog_outbox_service as _outbox

                _unaccepted = _outbox.unaccepted_local_writes(project_root)
                if _unaccepted:
                    out["local_unaccepted"] = _unaccepted
                    out["local_unaccepted_note"] = (
                        f"{len(_unaccepted)} local write(s) the server REFUSED. "
                        "They are NOT in `items` and NOT in `count` — they were "
                        "never accepted into the backlog. Each carries its intent "
                        "and the server's reason; they are preserved, not lost."
                    )
            except Exception:  # noqa: BLE001 — a conflict view never breaks a list
                pass
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
                "kind": row.get("kind") or "",  # #573; '' = unrated
                # Decomposition depth 1..5; null = unrated, never guessed.
                "difficulty": row.get("difficulty"),
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
            if not id:
                return _fail_edit(
                    error="id required for update",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            # #399 non-destructive contract: omitted content ('') means
            # UNTOUCHED. An explicit clear requires content='' AND
            # allow_clear=True — only then does '' reach the store.
            #
            # #800: append=True also lets '' through, so the store can answer
            # with the append-specific refusal ("append=True with empty content
            # does nothing"). Swallowing it here would make a mistyped append a
            # SILENT no-op, which is the same class of quiet failure this item
            # is about.
            content_arg = content if content else ("" if (allow_clear or append) else None)
            # #593: the receipt used to be built from the ARGUMENTS — it said
            # "content=..." because content was PASSED, never because content
            # LANDED. Under #399 (only passed fields change) a field that was
            # passed but not understood is indistinguishable from a field that
            # was not passed, so a partial application read as a complete one.
            # Snapshot before, re-read after, and report per field.
            requested = _backlog_requested_fields(
                status=status,
                content=content_arg,
                priority=priority,
                kind=kind,
                difficulty=difficulty,
                tags=tags,
            )
            before = project_backlog_store.get_by_id(project_root, backlog_id=int(id))
            r = project_backlog_store.update(
                project_root,
                backlog_id=int(id),
                status=status or None,
                content=content_arg,
                # #839: '' = untouched. A caller clearing a headline must say
                # so with real text; there is no "blank means wipe it" path.
                title=title or None,
                priority=priority or None,
                kind=kind or None,  # #573: '' = untouched, not "clear the kind"
                # 0 = untouched, not "clear the rating" (the ladder starts at 1).
                difficulty=difficulty or None,
                tags=tags,
                reason=reason or None,
                allow_clear=allow_clear,
                append=append,
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "update failed"),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            _audit_backlog(hub, project_root, action="update", backlog_id=int(id), session_id=session_id)
            after = project_backlog_store.get_by_id(project_root, backlog_id=int(id))
            applied, not_applied = _backlog_applied_fields(requested, before, after)
            changes = [f"{field}={_backlog_preview(requested[field])}" for field in applied]
            if not_applied:
                # A partial application must NOT read as success (#593).
                dropped = ", ".join(
                    f"{field}={_backlog_preview(requested[field])}" for field in not_applied
                )
                landed = ", ".join(changes) if changes else "(nothing)"
                return _fail_edit(
                    error=(
                        f"backlog #{id} update applied only PART of the call: "
                        f"landed [{landed}]; NOT applied [{dropped}]. The row was "
                        f"read back after the write — the un-applied fields are "
                        f"unchanged on disk. Re-issue those fields; do not assume "
                        f"the write landed."
                    ),
                    tool_name="ai_backlog",
                    started_at=t0,
                    extra_structured={
                        "id": int(id),
                        "applied": applied,
                        "not_applied": not_applied,
                        "partial_write": True,
                    },
                )
            # #710: `reason` is not one of the row FIELDS, so it can never
            # appear in `applied` — which is exactly why a caller could not
            # tell a persisted rationale from a discarded one. It is now
            # durable (event log) and the receipt says so under its own key,
            # rather than staying silent about a field the caller passed.
            _reason_recorded = str(r.get("reason") or "").strip()
            _structured: dict[str, Any] = {
                "id": int(id),
                "applied": applied,
                "not_applied": [],
            }
            if _reason_recorded:
                _structured["reason_recorded"] = _reason_recorded
            return _ok_edit(
                ack=f"✓ id={id}",
                pretty_lines=[
                    f"📋 backlog #{id} updated: {', '.join(changes) if changes else '(no changes)'}",
                ],
                structured=_structured,
                tool_name="ai_backlog",
                started_at=t0,
            )

        if mode == "remove":
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
                    (f"🔗 backlog #{r['umbrella_id']} absorbed "
                    f"{len(r['merged_ids'])} item(s): {r['merged_ids']}"
                    f"{_fmt_tags(r.get('tags') or [])}"),
                ],
                structured={
                    "id": int(r["umbrella_id"]),
                    "merged_ids": r["merged_ids"],
                    "tags": r.get("tags") or [],
                },
                tool_name="ai_backlog",
                started_at=t0,
            )

        if mode == "cutover_status":
            # READ-ONLY. "Am I ready to hand authority to the server, and if not
            # what is stopping me" is a question an operator has BEFORE the flip
            # exists, and answering it is what makes the precondition
            # actionable rather than a wall they hit later.
            from . import backlog_outbox_service as _outbox

            r = _outbox.cutover_readiness(project_root)
            lines = (
                ["✅ ready for the authority flip"]
                if r.get("ready")
                else ["⛔ NOT ready for the authority flip:"]
                + [f"   • {b}" for b in r.get("blockers", [])]
            )
            # #1002 gap 3: the sync state, in one line, so the reader sees WHY
            # nothing is moving without unpacking the structured block.
            sync = r.get("sync") if isinstance(r.get("sync"), dict) else {}
            if sync:
                drain = sync.get("drain") if isinstance(sync.get("drain"), dict) else {}
                vps = sync.get("vps_hub") if isinstance(sync.get("vps_hub"), dict) else {}
                last = sync.get("last_cycle") if isinstance(sync.get("last_cycle"), dict) else {}
                bits = [
                    f"sitter={'running' if sync.get('sitter_running') else 'not running'}",
                    f"pending={sync.get('pending')}",
                ]
                if last:
                    bits.append(f"last_cycle={'ok' if last.get('ok') else 'errors:' + ','.join(last.get('errors') or []) or 'errors'}")
                    if last.get("deferred_by"):
                        bits.append(f"deferred_by={last['deferred_by']}")
                if vps:
                    bits.append(f"vps_hub={vps.get('skipped') or vps.get('error') or ('ok' if vps.get('enabled') else 'disabled')}")
                if drain:
                    bits.append(
                        "drain="
                        + (
                            (drain.get("reason") or "ok")
                            + (f"[{drain.get('credential')}]" if drain.get("credential") else "")
                            + (f" via {drain.get('route')}" if drain.get("route") else "")
                            if drain.get("attempted")
                            else "never attempted"
                        )
                    )
                lines.append("   sync: " + " · ".join(bits))
            return _ok_edit(
                ack="✓ cutover status",
                pretty_lines=lines,
                structured=r,
                tool_name="ai_backlog",
                started_at=t0,
            )

        if mode == "classify_conflict":
            # THE CUTOVER GATE'S INPUT (operator ruling 2026-08-30: "Existing
            # unresolved conflicts may block flip until classified"). Without a
            # door an operator can actually reach, "classified" is a state
            # nothing can ever enter and the flip could never be attempted.
            if not id:
                return _fail_edit(
                    error=(
                        "id required — pass the queue_id from a "
                        "`local_unaccepted` entry or from the conflicts list"
                    ),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            from . import backlog_write_queue as _q

            r = _q.classify(
                project_root,
                queue_id=int(id),
                classification=str(reason or ""),
                by=str(session_id or ""),
            )
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "classify failed"),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            _audit_backlog(
                hub,
                project_root,
                action="classify_conflict",
                backlog_id=int(id),
                session_id=session_id,
                reason=str(r.get("classification") or ""),
            )
            return _ok_edit(
                ack=f"✓ queue_id={id}",
                pretty_lines=[
                    f"⚖️  conflict queue_id={id} classified "
                    f"'{r.get('classification')}'"
                    + (
                        " — returned to pending; the next drain retries it"
                        if r.get("classification") == "requeue"
                        else " — the record is kept, not deleted"
                    )
                ],
                structured=r,
                tool_name="ai_backlog",
                started_at=t0,
            )

        if mode == "unmerge":
            if not id:
                return _fail_edit(
                    error="id required for unmerge",
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            r = project_backlog_store.unmerge(project_root, backlog_id=int(id))
            if not r.get("ok"):
                return _fail_edit(
                    error=str(r.get("error") or "unmerge failed"),
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            _audit_backlog(
                hub,
                project_root,
                action="unmerge",
                backlog_id=int(id),
                session_id=session_id,
                reason=f"was_merged_into={r.get('was_merged_into')}",
            )
            if r.get("not_merged"):
                return _ok_edit(
                    ack=f"✓ id={id}",
                    pretty_lines=[f"🔗 backlog #{id} was not merged (no-op)"],
                    structured={"id": int(id), "not_merged": True},
                    tool_name="ai_backlog",
                    started_at=t0,
                )
            return _ok_edit(
                ack=f"✓ id={id}",
                pretty_lines=[
                    (f"🔗 backlog #{id} unmerged from #{r.get('was_merged_into')} "
                     f"(status={r.get('status')}, unchanged)"),
                ],
                structured={
                    "id": int(id),
                    "was_merged_into": r.get("was_merged_into"),
                    "status": r.get("status"),
                },
                tool_name="ai_backlog",
                started_at=t0,
            )

        return _fail_edit(
            error=f"unknown mode {mode!r}. Use: add|list|get|update|remove|merge|unmerge",
            tool_name="ai_backlog",
            started_at=t0,
        )
