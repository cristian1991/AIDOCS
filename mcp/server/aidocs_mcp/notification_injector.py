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

Workers fenced via the identity seam (task_actor_identity.
resolve_task_actor: env stamp / principal / #217 registry) — they
don't surface their parent conductor's notifications.

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

    #360/#356: worker-ness comes from the ONE identity seam
    (``task_actor_identity.resolve_task_actor`` — spawn-path env stamp,
    resolved principal type, per-process latch, or the #217 registry
    chain), not the env stamp alone. A subagent whose process lost the
    env stamp (e.g. a host-spawned subagent sharing the conductor's MCP
    process) previously drained the CONDUCTOR's session rail — the #356
    leak. Fail-open to the env check so a seam hiccup never changes the
    conductor path.
    """
    if os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
        return True
    try:
        from .mcp_server_runtime_helpers import resolve_project_root
        from .task_actor_identity import resolve_task_actor

        return bool(resolve_task_actor(resolve_project_root())[2])
    except Exception:
        return False


def _resolve_drain_identity() -> tuple[Path | None, str, str, str]:
    """Return project, work session, actor context, and lane for this call."""
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_agent_context_id,
            current_calling_host_session_id,
            resolve_project_root,
        )

        project_root = resolve_project_root()
        actor_id = current_calling_agent_context_id(project_root)
    except Exception:
        return None, "", "", ""
    lane_id = os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()
    try:
        from . import managed_mode_service as _mm

        managed = _mm.ManagedModeService().get_mode(
            project_root,
            host_session_id=current_calling_host_session_id(),
        )
        if managed.get("active"):
            return (
                project_root,
                str(managed.get("session_id") or "").strip(),
                actor_id,
                lane_id,
            )
    except Exception:
        pass
    return project_root, "", actor_id, lane_id



def _collect_notification_blocks(
    project_root: Path,
    session_id: str,
    agent_context_id: str,
    lane_id: str,
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
            agent_context_id=agent_context_id,
            lane_id=lane_id,
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
        # #215: only a bound seat has an inbox. An unmapped caller resolves to a
        # non-seat role — skip the read (msg_inbox rejects it anyway) so a lane
        # worker never drains a seat inbox and we avoid a per-turn exception.
        if role in _cc.MSG_ROLES:
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
    # Deploy phase-2 SAFE-TO-EDIT (#326): while a crown deploy is PAST its local
    # gates, surface "live tree free to edit; do NOT move HEAD" on every tool
    # call until the deploy clears its marker OR the per-epoch surface cap
    # (3, operator 2026-07-16) is reached — told, then it stops nagging. The
    # gate still owns the marker's lifetime; the cap only bounds repetition.
    try:
        from .deploy_edit_window import safe_to_edit_notice

        _ste = safe_to_edit_notice(project_root, session_id=session_id)
        if _ste:
            blocks.append(_ste)
    except Exception:
        pass
    # Deploy FAIL SUMMARY (2026-07-13): #271 made the exit codes valuable (class +
    # step + reason + artifact link) but printed them ONLY to the deploy's stdout —
    # the harness surfaces just "failed with exit code 11", so the operator had to
    # look up a number to learn what broke. Surface the HUMAN-READABLE summary on
    # this same rail. Project-scoped; cleared when the next deploy starts; fail-quiet.
    try:
        from .deploy_edit_window import deploy_failure_notice

        _fail = deploy_failure_notice(project_root, session_id=session_id)
        if _fail:
            blocks.append(_fail)
    except Exception:
        pass
    # HOOK BOOTSTRAP (#364): the hook's own self-repair rides the hooks it
    # repairs — remove the `hooks` key from ~/.claude/settings.json and no
    # hook ever fires again, so self-repair is unreachable and the host-level
    # gate stays OFF forever. THIS rail is the one surface that survives hook
    # death (the agent still calls MCP tools), so every governed call for a
    # commissioned project verifies the host hook wiring and HEALS it via the
    # canonical installer (`aidocs setup` code path, Article XXII) — or, when
    # healing is impossible, refuses LOUDLY with the exact repair command.
    # Healthy path is a memoized single os.stat. Fail-quiet on crash — the
    # gate_health hook-silence probe below remains the independent backstop.
    try:
        from .hook_bootstrap import ensure_host_hooks

        _hb = ensure_host_hooks(project_root)
        if _hb:
            # #474 notify-on-change: full banner ONCE per session, again only
            # when the hook state CHANGES (healed / degraded further). The
            # ledger fails open — a broken ledger repeats the banner (the old
            # behavior) rather than ever losing a real alert.
            from .session_response_ledger import dedupe_state_notice

            _hb_notice = dedupe_state_notice(
                project_root,
                session_id,
                "hook_bootstrap",
                _hb["notice"],
            )
            if _hb_notice:
                blocks.append(_hb_notice)
        else:
            # Healthy — record the transition so a later re-degradation with
            # the IDENTICAL banner text still re-emits (heal → re-break).
            from .session_response_ledger import mark_state

            mark_state(project_root, session_id, "hook_bootstrap", "healthy")
    except Exception:
        pass
    # GATE LIVENESS: the security gate can die SILENTLY (hook declines itself
    # on package drift; hooks stop firing; the NLP security surface returns
    # None forever) and today both the operator and the agent would see a
    # perfectly normal, green, UNGOVERNED session. Two jobs here, on the same
    # proven rail deploy_failure_notice rides (Article XXII — one pattern, not
    # a second one):
    #   1. record_mcp_activity() — every governed tool call feeds the "active
    #      traffic" clock, so the hook-silence probe can distinguish "active
    #      session, no hooks arriving" (the ALARM) from "idle" (normal, and it
    #      must never cry wolf).
    #   2. gate_health_notice() — a DEGRADED or UNKNOWN gate announces itself
    #      on the agent's NEXT tool call, so no agent can work ungoverned in
    #      ignorance. An OK gate is SILENT here (green is displayed on the
    #      dashboard, not shouted on the rail). UNKNOWN is a WARNING, never a
    #      pass — a probe that could not check must never read as green.
    # Fail-quiet like every other layer: a broken health signal degrades to no
    # block, never to a fabricated all-clear.
    try:
        from . import gate_health as _gh

        _gh.record_mcp_activity()
        _ghn = _gh.gate_health_notice(project_root)
        if _ghn:
            # #474 notify-on-change (same contract as hook_bootstrap above):
            # DEGRADED/UNKNOWN announces once per session per distinct state.
            from .session_response_ledger import dedupe_state_notice

            _ghn_once = dedupe_state_notice(
                project_root,
                session_id,
                "gate_health",
                _ghn,
            )
            if _ghn_once:
                blocks.append(_ghn_once)
        else:
            from .session_response_ledger import mark_state

            mark_state(project_root, session_id, "gate_health", "healthy")
    except Exception:
        pass
    # Durable-content capture hint (#9): when conversation/prompt content was
    # classified as DECLARATIVE durable knowledge (rule/decision/preference/
    # invariant), surface a terse 💾 "record as durable?" hint. Once per
    # content (hash ledger) and max 2 surfaces, then auto-drop — mirrors
    # run_notifications max_displays semantics. Fail-quiet like every layer.
    try:
        from .aidocs_nlp.durable_hint_store import (
            format_hint_block,
            surface_pending,
        )

        _hints = surface_pending(project_root, session_id=session_id)
        if _hints:
            blocks.append(format_hint_block(_hints))
    except Exception:
        pass
    # Freeze-strike notices (operator directive 2026-07-15): a security strike
    # — including a self-cancel strike minted when the AGENT clears its own
    # freeze — surfaces here on the rail, 3 times then auto-drops. Replaces the
    # old per-prompt UPS strike-note (hook_pipeline): told, then it stops
    # nagging. Fail-quiet like every layer.
    try:
        from . import freeze_strike_notice_store as _fsn

        _strikes = _fsn.surface_pending(
            project_root, session_id=session_id, agent_context_id=agent_context_id
        )
        if _strikes:
            blocks.append(_fsn.format_strike_block(_strikes))
    except Exception:
        pass
    # Open-backlog surfacing (#419 War DD): compact counts + top items with
    # the tell-the-user instruction, epoch-deduped via the session ledger.
    # Shares the 'backlog_surface' key with the UPS additionalContext path,
    # so whichever surface fires first wins and the other stays quiet —
    # this rail is the fallback for hookless contexts. Fail-quiet: empty
    # backlog or dead store → nothing (no-padding law). Workers are already
    # fenced upstream in _inject_into_return AND inside the surfacer.
    try:
        from .backlog_surfacer import context_backlog_block

        _bl = context_backlog_block(project_root, session_id)
        if _bl:
            blocks.append(_bl)
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
                # PRESERVE structured_content + meta (2026-07-13). Rebuilding
                # with content only silently DESTROYED the structured payload
                # every time a notification fired — dual-audience tools
                # (task_begin/complete, edit tools) contractually return
                # {ok: ...} in structured_content, and any client reading it
                # got None instead. On the VPS proof run the gate-health
                # DEGRADED rail block fired mid-suite and two query-gate
                # tests crashed with KeyError: 'ok' — that KeyError was this
                # contract break announcing itself, not a test bug.
                return ToolResult(
                    content=new_content,
                    structured_content=raw.structured_content,
                    meta=raw.meta,
                )
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
    """Best-effort identity-scoped notification injection."""
    try:
        if _is_worker_caller():
            return raw
        project_root, session_id, actor_id, lane_id = _resolve_drain_identity()
        if project_root is None or not session_id:
            return raw
        blocks = _collect_notification_blocks(
            project_root,
            session_id,
            actor_id,
            lane_id,
        )
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
