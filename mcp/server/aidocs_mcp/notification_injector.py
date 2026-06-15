"""Universal tool-call notification injector — emperor 2026-05-07.

The Emperor's directive: notifications must surface on EVERY tool call,
not just `@renders_as`-wrapped ones. Tools returning plain dicts
(session_connect, conductor_*, lane_*, etc.) silently bypassed the
existing tool_display.py drain because it only fires inside
`@renders_as` and `text_result`.

Single chokepoint: monkey-patch `server.tool` AFTER FastMCP init so
every subsequent `@server.tool(...)` registration is wrapped. The
wrapper:
  1. Calls the original tool function.
  2. Peeks pending run_notifications + lane_completion_reviews for
     this conductor's session.
  3. Augments the return value WITHOUT changing its shape semantics:
       - ToolResult: append a TextContent block.
       - dict: add a `_notifications` key with the formatted block.
       - str: prepend the formatted block.
       - list/scalar/None: wrap with a notifications field.

Notifications PERSIST until satisfied (run_notifications.dismiss_run on
output read; lane_completion_reviews status flips on conductor verdict).
That's the 'until satisfied' lifecycle.

Workers fenced via AIDOCS_EXPERT_LANE_ID env — they don't surface
their parent conductor's notifications.

Best-effort: any failure in injection passes the original return
through unchanged. Tool semantics never break for a notification glitch.
"""

from __future__ import annotations

import functools
import inspect
import os
from pathlib import Path
from typing import Any


def _is_worker_caller() -> bool:
    """Lane workers must NOT see conductor notifications.
    Their lane plan / handoff path delivers run results separately.
    """
    return bool(os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip())


def _resolve_drain_session_id() -> tuple[Path | None, str]:
    """Return (project_root, aidocs_session_id) for filtered drains.
    Empty session_id when managed mode isn't active or identity is
    unresolvable. Best-effort; never raises.
    """
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
            resolve_project_root,
        )

        project_root = resolve_project_root()
    except Exception:
        return None, ""
    try:
        from . import managed_mode_service as _mm

        host_sid = current_calling_host_session_id()
        managed = _mm.ManagedModeService().get_mode(
            project_root,
            host_session_id=host_sid,
        )
        if managed.get("active"):
            return project_root, str(managed.get("session_id") or "").strip()
    except Exception:
        pass
    return project_root, ""


def _collect_notification_blocks(
    project_root: Path,
    session_id: str,
) -> list[str]:
    """Peek pending notifications + lane reviews; return formatted
    text blocks. Empty list when nothing pending.
    """
    blocks: list[str] = []
    if not session_id:
        return blocks
    # Run-done notifications. Phoenix 2026-05-10: surface_for_session
    # bumps each record's surfaced_count and auto-dismisses it when
    # the count hits `notifications.max_displays`. Default 3 — three
    # surfaces, then the record drops itself from the queue even if
    # the agent never read the output. max_displays=0 → classic
    # 'until satisfied' behavior (notification persists forever
    # until ai_run_output dismisses it).
    try:
        from . import run_notifications as _rn

        try:
            from .config import get_setting

            _max_displays = int(
                get_setting(
                    "notifications.max_displays",
                    project_root=project_root,
                    default=3,
                )
                or 0,
            )
        except Exception:
            _max_displays = 3
        pending_runs = _rn.surface_for_session(
            project_root,
            session_id=session_id,
            max_displays=_max_displays,
        )
        if pending_runs:
            blocks.append(_rn.format_block(pending_runs))
    except Exception:
        pass
    # Lane completion reviews. OR-match on session_id + host_session_id
    # so a conductor that swapped sessions mid-run still sees their
    # pending reviews. Orphan rows missing one field but matching the
    # other still reach their owner.
    try:
        from . import lane_completion_review_store as _lcr

        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            host_sid = current_calling_host_session_id()
        except Exception:
            host_sid = ""
        pending_reviews = _lcr.pending_for_session(
            project_root,
            session_id=session_id,
            host_session_id=host_sid,
        )
        if pending_reviews:
            blocks.append(_lcr.format_pending_block(pending_reviews))
    except Exception:
        pass
    # Messages — role-addressed comms available to all agents.
    try:
        from . import conductor_comms as _cc

        role = _cc.msg_resolve_caller_role(project_root)
        pending_chat = _cc.msg_inbox(
            project_root,
            role=role,
            unread_only=True,
            mark_read=True,
        )
        if pending_chat:
            blocks.append(_cc.msg_format_block(pending_chat))
    except Exception:
        pass
    return blocks


def _augment_return(raw: Any, blocks: list[str]) -> Any:
    """Inject notification blocks into `raw` without breaking its
    shape contract. Returns the augmented value.
    """
    if not blocks:
        return raw
    block_text = "\n\n".join(blocks)

    # ToolResult: append a TextContent block.
    try:
        from fastmcp.tools.tool import ToolResult
        from mcp.types import TextContent

        if isinstance(raw, ToolResult):
            try:
                new_content = list(raw.content) + [
                    TextContent(type="text", text=block_text),
                ]
                return ToolResult(content=new_content)
            except Exception:
                return raw
    except Exception:
        pass

    # dict: add `_notifications` field.
    if isinstance(raw, dict):
        if "_notifications" in raw:
            return raw
        out = dict(raw)
        out["_notifications"] = block_text
        return out

    # str: prepend block.
    if isinstance(raw, str):
        return f"{block_text}\n\n{raw}"

    # list: wrap with notifications metadata.
    if isinstance(raw, list):
        return {"items": raw, "_notifications": block_text}

    # None / scalars: wrap.
    if raw is None:
        return {"_notifications": block_text}
    return {"value": raw, "_notifications": block_text}


def _inject_into_return(raw: Any) -> Any:
    """Best-effort injection. Worker callers / unresolvable session /
    empty queues all pass-through cleanly.
    """
    try:
        if _is_worker_caller():
            return raw
        project_root, session_id = _resolve_drain_session_id()
        if project_root is None or not session_id:
            return raw
        blocks = _collect_notification_blocks(project_root, session_id)
        if not blocks:
            return raw
        return _augment_return(raw, blocks)
    except Exception:
        return raw


def install_universal_notification_injection(server: Any) -> None:
    """Monkey-patch `server.tool` so EVERY subsequent `@server.tool(...)`
    registration wraps the registered function with the notification
    injector. Idempotent — guarded by an attribute marker.

    Must be called BEFORE any @server.tool() decoration. mcp_server.py
    invokes this immediately after `server = FastMCP(...)` construction.
    """
    if getattr(server, "_aidocs_universal_drain_installed", False):
        return

    original_tool = server.tool

    def patched_tool(*args, **kwargs):
        inner_decorator = original_tool(*args, **kwargs)

        def wrap_with_drain(fn):
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapped(*a, **kw):
                    raw = await fn(*a, **kw)
                    return _inject_into_return(raw)

                return inner_decorator(async_wrapped)

            @functools.wraps(fn)
            def sync_wrapped(*a, **kw):
                raw = fn(*a, **kw)
                return _inject_into_return(raw)

            return inner_decorator(sync_wrapped)

        return wrap_with_drain

    try:
        server.tool = patched_tool  # type: ignore[assignment]
        server._aidocs_universal_drain_installed = True
    except Exception:
        # If FastMCP refuses re-binding `tool`, fall back to the
        # existing per-tool drain in tool_display.py — universal
        # coverage is best-effort.
        pass
