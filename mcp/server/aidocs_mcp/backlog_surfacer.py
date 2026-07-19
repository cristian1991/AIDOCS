"""Open-backlog surfacing block — War DD #419.

The operator asked "how many items left open?" — the system should have
been volunteering that answer. This module computes a compact, bounded
summary of the project's open backlog (counts by priority + top
critical/urgent titles) carrying an explicit INSTRUCTION to the agent:
tell the user about these backlogged task(s). It rides three surfaces:

  1. UPS additionalContext — ``prompt_context_service
     .build_enforced_context`` (the prompt path, hook or hookless).
  2. The Stop hook — ``claude_hook._handle_stop``: a turn must not seal
     silently while open work is unmentioned. The reminder blocks the
     clean stop ONCE per (epoch, backlog-state), then yields.
  3. The universal notification rail — ``notification_injector``:
     fallback for hookless contexts. Shares the ``backlog_surface``
     ledger key with surface 1 so whichever fires first wins and the
     other stays quiet.

Dedupe is EPOCH-BASED via the session_response_ledger (War AZ #474):
the ledger state is ``epoch|counts|top-ids``, so the block emits once
per epoch per session and re-emits when the backlog CHANGES (item
added/closed — the notify-on-change pattern) or the epoch rotates
(compaction).

Laws honored:
  * no-padding — empty backlog → no block, ever;
  * fail-quiet — backlog store dead → no block; the context BANNER
    fails open on a broken ledger (AZ contract: repeat, never lose),
    but the STOP-block fails quiet (a block that cannot be deduped
    could loop the stop forever);
  * worker fencing (doctrine §III) — lane workers get curated
    directives, never conductor-facing backlog nags;
  * hard bound — the block never exceeds ``MAX_BLOCK_CHARS`` (600) and
    the instruction line always survives the trim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "MAX_BLOCK_CHARS",
    "build_backlog_summary",
    "context_backlog_block",
    "stop_backlog_reminder",
]

_KEY_CONTEXT = "backlog_surface"
_KEY_STOP = "backlog_surface_stop"
MAX_BLOCK_CHARS = 600
_TOP_N = 3
_TITLE_CHARS = 64
# Active work statuses only — done/rejected/removed/merged never nag.
_ACTIVE_STATUSES = ("in_progress", "open", "blocked")
_PRIORITY_ORDER = ("critical", "urgent", "high", "normal", "low", "idea")

_INSTRUCTION = (
    "INSTRUCTION: tell the user about these backlogged task(s) now — "
    "report the open counts and name the top items. "
    "Full list: ai_backlog(mode='list')."
)


def _is_worker_context() -> bool:
    """Lane workers never see conductor-facing backlog nags (§III)."""
    try:
        from .notification_injector import _is_worker_caller

        return _is_worker_caller()
    except Exception:
        return False


def _resolve_epoch(
    project_root: Path,
    *,
    host_kind: str = "",
    host_session_id: str = "",
) -> str:
    """Current agent_memory_epoch, "" when unresolvable (best-effort)."""
    try:
        from .helper_skill_injector import _resolve_epoch as _re

        return _re(
            project_root,
            host_kind=host_kind or None,
            host_session_id=host_session_id or None,
        )
    except Exception:
        return ""


def build_backlog_summary(project_root: Path) -> tuple[str, str] | None:
    """(block, state-signature) for the open backlog.

    None when the backlog is empty (no-padding law) or the store is
    unavailable (fail-quiet). The signature feeds the ledger dedupe:
    it changes whenever counts or the top items change.
    """
    try:
        from . import project_backlog_store as _pbs

        rows: list[dict[str, Any]] = []
        for status in _ACTIVE_STATUSES:
            rows.extend(_pbs.list_backlog(project_root, status=status, limit=500))
    except Exception:
        return None
    if not rows:
        return None

    counts: dict[str, int] = {}
    for r in rows:
        p = str(r.get("priority") or "normal")
        counts[p] = counts.get(p, 0) + 1
    total = len(rows)
    count_parts = [f"{counts[p]} {p}" for p in _PRIORITY_ORDER if counts.get(p)]
    other = total - sum(counts.get(p, 0) for p in _PRIORITY_ORDER)
    if other:
        count_parts.append(f"{other} other")
    header = f"📋 OPEN BACKLOG — {total} item(s): {', '.join(count_parts)}."

    rank = {p: i for i, p in enumerate(_PRIORITY_ORDER)}
    top = sorted(
        (r for r in rows if str(r.get("priority")) in ("critical", "urgent")),
        key=lambda r: (rank.get(str(r.get("priority")), 99), int(r.get("id") or 0)),
    )[:_TOP_N]

    lines = [header]
    budget = MAX_BLOCK_CHARS - len(header) - len(_INSTRUCTION) - 2
    for r in top:
        title = str(r.get("title") or "").strip()
        if len(title) > _TITLE_CHARS:
            title = title[: _TITLE_CHARS - 1] + "…"
        line = f"  #{r.get('id')} [{r.get('priority')}] {title}"
        if budget - (len(line) + 1) < 0:
            break
        lines.append(line)
        budget -= len(line) + 1
    lines.append(_INSTRUCTION)
    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:  # defense in depth; instruction is last
        block = block[:MAX_BLOCK_CHARS]

    sig = "|".join(
        [
            f"total={total}",
            ",".join(f"{p}:{counts[p]}" for p in sorted(counts)),
            "top=" + ",".join(str(r.get("id")) for r in top),
        ]
    )
    return block, sig


def _should_emit_open(project_root: Path, session_id: str, key: str, state: str) -> bool:
    """Banner dedupe — AZ fail-OPEN contract: no session identity or a
    broken ledger repeats the banner rather than ever losing it."""
    if not session_id:
        return True
    try:
        from .session_response_ledger import _LEDGER

        return _LEDGER.should_emit(project_root, session_id, key, state)
    except Exception:
        return True


def _should_emit_strict(project_root: Path, session_id: str, key: str, state: str) -> bool:
    """Stop-block dedupe — fail-QUIET: only block when the dedupe write
    provably succeeded, else a stop that can never dedupe loops forever."""
    if not session_id:
        return False
    try:
        from .session_response_ledger import _LEDGER

        return _LEDGER.should_emit(project_root, session_id, key, state)
    except Exception:
        return False


def context_backlog_block(
    project_root: Path,
    session_id: str,
    *,
    host_kind: str = "",
    host_session_id: str = "",
) -> str | None:
    """The additionalContext / notification-rail surface (key
    ``backlog_surface``). None when there is nothing to say, the caller
    is a lane worker, or this (epoch, backlog-state) was already told.
    """
    try:
        if _is_worker_context():
            return None
        built = build_backlog_summary(project_root)
        if built is None:
            return None
        block, sig = built
        epoch = _resolve_epoch(
            project_root, host_kind=host_kind, host_session_id=host_session_id
        )
        if not _should_emit_open(project_root, session_id, _KEY_CONTEXT, f"{epoch}|{sig}"):
            return None
        return block
    except Exception:
        return None


def stop_backlog_reminder(
    project_root: Path,
    *,
    event_name: str,
    host_session_id: str,
    host_kind: str = "claude_code",
) -> dict[str, str] | None:
    """The Stop-hook surface (key ``backlog_surface_stop``): a
    ``{"decision": "block", "reason": ...}`` envelope ordering the agent
    to tell the user what's still queued — once per (epoch, state), so
    the immediately following stop attempt seals. SubagentStop (lane
    workers) never blocks; neither does a caller without a session id.
    """
    try:
        if event_name != "Stop":
            return None
        if _is_worker_context():
            return None
        built = build_backlog_summary(project_root)
        if built is None:
            return None
        block, sig = built
        epoch = _resolve_epoch(
            project_root, host_kind=host_kind, host_session_id=host_session_id
        )
        if not _should_emit_strict(
            project_root, host_session_id, _KEY_STOP, f"{epoch}|{sig}"
        ):
            return None
        reason = (
            "🛑 OPEN BACKLOG UNREPORTED — do not end the turn silently while "
            "work is queued.\n"
            f"{block}\n"
            "Tell the user what is still open (counts + top items), then stop."
        )
        return {"decision": "block", "reason": reason}
    except Exception:
        return None
