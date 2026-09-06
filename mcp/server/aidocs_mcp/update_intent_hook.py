"""Hook glue for the update-intent durability gate (#219/#221 PR-1, ADVISE only).

Two entry points, both thin and host-agnostic:

  * ``process_user_prompt`` — UPS side. Advances the turn counter (expiring
    stale rows LOUDLY), runs the pure detector on the OPERATOR prompt only,
    persists a pending row + ``update_intent_detected`` event on detection,
    and returns the advise blocks (new detection + repeat reminders for
    still-pending rows). Advise text only — PR-1 never blocks anything.

  * ``observe_tool_result`` — PostToolUse side. When a SUCCESSFUL durable
    write lands (ai_backlog add|update, ai_task todo add|update (#83 —
    the former ai_todo), ai_plan_create/expand, memory_capture),
    satisfies the session's pending rows and emits
    ``update_intent_satisfied``. Failed / refused / confirm-required calls
    never satisfy (Empire acceptance).

Config: ``nlp.update_gate`` ∈ {off, advise}; default advise; ``block`` is
PR-2 and is treated as advise here (no PreToolUse authority in PR-1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import pending_durable_writes_store as _pdw
from .update_intent_detector import detect_update_intent

# Durable-write satisfiers (Empire §9.2: ai_task(begin) is NOT one — lifecycle
# registration is not content durability). #83 hard merge: todos ride ai_task,
# so ai_task is durable ONLY on its todo routes — mode='add' (todo-only mode)
# and mode='update' WITH scope='task'|'session' (the scope-driven todo patch);
# lifecycle begin/update/complete/status stay non-satisfiers, exactly as before
# the merge (ai_task was never in this table).
_MODE_GATED_DURABLE = {"ai_backlog": {"add", "update"}}
_ALWAYS_DURABLE = {"memory_capture", "ai_plan_create", "ai_plan_expand"}
_TODO_SCOPES = {"task", "session"}


def _is_task_todo_durable(tool_input: dict | None) -> bool:
    """True when an ai_task call is a todo add/update (#83 route law —
    mirrors server_plan_task_tools.route_task_mode restricted to writes)."""
    mode = str((tool_input or {}).get("mode") or "").strip().lower()
    if mode == "add":
        return True
    scope = str((tool_input or {}).get("scope") or "").strip().lower()
    return mode == "update" and scope in _TODO_SCOPES

_FAILURE_MARKERS = (
    '"ok": false',
    "'ok': false",
    "_error",
    "✗ failed",
    "confirm_required",
    "refused",
    "blocked_by",
)


def _gate_mode(project_root: Path, session_id: str | None) -> str:
    try:
        from .config_resolver import LayeredConfigResolver

        rv = LayeredConfigResolver().resolve(
            "nlp.update_gate", project_root, session_id=session_id or None
        )
        val = str(rv.value or "").strip().lower()
        if val in ("off", "advise", "block"):
            return val
    except Exception:
        pass
    return "advise"


def _emit(project_root: Path, session_id: str, event_kind: str, payload: dict) -> None:
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind=event_kind,
            source_kind="update_intent",
            session_id=session_id,
            capability_name="update_intent_gate",
            action_kind="durability",
            target_entity=str(payload.get("snippet") or "")[:200],
            status=payload.get("status", "recorded"),
            payload=payload,
        )
    except Exception:
        pass  # advisory telemetry must never break the hook


def _advise_text(row: dict, *, new: bool) -> str:
    tool_hint = {
        "backlog": "ai_backlog(mode='add', ...)",
        "todo": "ai_task(mode='add', ...)",
        "plan": "ai_plan_create / ai_plan_expand",
        "memory": "memory_capture(kind=..., content=...)",
    }.get(str(row.get("suggested_target") or "backlog"), "ai_backlog(mode='add', ...)")
    if row.get("ambiguous"):
        return (
            f"📌 Possible operator update ({row['snippet']!r}). Was this a "
            f"plan/spec/priority change? If YES, record it durably via "
            f"{tool_hint} before other work; if NO, say so briefly and it "
            f"stands down."
        )
    lead = "📌 Operator UPDATE detected" if new else "📌 REMINDER — operator update still unrecorded"
    return (
        f"{lead} ({row['snippet']!r}; verbs: {', '.join(row.get('verbs') or []) or '-'}; "
        f"objects: {', '.join(row.get('objects') or []) or '-'}). Record it durably "
        f"BEFORE other work: {tool_hint} — then reference the id in your reply. "
        f"This reminder repeats until a durable write lands; unrecorded updates "
        f"expire LOUDLY at the end of the next turn."
    )


def _search_memory(project_root: Path, query: str, limit: int = 8) -> list[Any]:
    """Default existence-check search (clause 2) — the memory index. Any
    store/index error degrades to [] so assess_fit -> 'novel': a broken index
    must NEVER suppress the nudge (guardrail: no permissive fallback)."""
    try:
        from .index_store import IndexStore
        from .session_store import SessionStore

        return IndexStore(SessionStore(project_root)).search_memory(
            project_root, query=query, limit=limit
        )
    except Exception:
        return []


def _existing_durable_match(
    project_root: Path, snippet: str, *, search: Any = None
) -> str | None:
    """Clause 2 (#256): is this operator update ALREADY captured durably?
    assess_fit=duplicate -> a short ref to the existing entry; else None.
    Degrades to None on any error (the nudge still fires — no permissive skip)."""
    _search = search or (lambda q, limit=8: _search_memory(project_root, q, limit))
    try:
        from .memory_fit import assess_fit

        fit = assess_fit(snippet or "", _search)
        if fit.get("verdict") == "duplicate":
            match = (fit.get("matches") or [{}])[0]
            return str(match.get("path") or match.get("title") or "existing entry")
    except Exception:
        return None
    return None


_AUTOMATED_PROMPT_MARKERS: tuple[str, ...] = (
    "[SYSTEM NOTIFICATION - NOT USER INPUT]",
    "<task-notification>",
    "<system-reminder>",
    "Stop hook feedback",
    "This is an automated",
)


def _is_automated_prompt(prompt: str | None) -> bool:
    """#325: True when the prompt is a HARNESS-injected automated notification
    (completed background task, system-reminder, Stop-hook feedback), NOT
    operator input. The UPS capture-nudge must never mine an operator-update
    from these — a task's `<output-file>…tasks/…` tag was being read as
    'verbs: file; objects: tasks'. Exact harness-string match."""
    if not prompt:
        return False
    return any(m in prompt for m in _AUTOMATED_PROMPT_MARKERS)


def process_user_prompt(
    project_root: Path,
    session_id: str,
    prompt: str,
    *,
    nlp=None,
    search=None,
) -> list[str]:
    """UPS entry. Returns advise blocks ([] when mode=off or nothing to say)."""
    if not session_id:
        return []
    mode = _gate_mode(project_root, session_id)
    if mode == "off":
        return []
    turn = _pdw.begin_turn(project_root, session_id)
    for expired in turn["expired"]:
        _emit(
            project_root,
            session_id,
            "durable_write_expired",
            {
                "status": "expired",
                "row_id": expired["id"],
                "snippet": expired["snippet"],
                "suggested_target": expired["suggested_target"],
            },
        )

    blocks: list[str] = []
    for stale in turn["expired"]:
        blocks.append(
            f"⚠️ DURABILITY EXPIRED — the operator update ({stale['snippet']!r}) was "
            f"never recorded and has now expired unrecorded (audited as "
            f"durable_write_expired). If it still matters, record it NOW."
        )

    # #325: automated notifications (task-notification / system-reminder /
    # Stop-hook feedback) are NOT operator input — skip NEW detection. Turn-
    # advance + expiry (above) and repeat reminders (below) still run.
    proposal = None if _is_automated_prompt(prompt) else detect_update_intent(prompt or "", nlp=nlp)
    if proposal is not None and proposal.detected:
        existing = None
        if not proposal.ambiguous:
            existing = _existing_durable_match(project_root, proposal.snippet, search=search)
        if existing is not None:
            # Clause 2 (#256): the update is ALREADY captured durably — surface
            # the existing entry, create NO pending row and raise NO nudge
            # (nothing left to capture; keeps the block un-bloated).
            _emit(
                project_root,
                session_id,
                "update_intent_already_captured",
                {
                    "status": "already_captured",
                    "snippet": proposal.snippet,
                    "match": existing,
                },
            )
            blocks.append(
                f"✓ Update already recorded durably (matches existing memory: "
                f"{existing}) — no new capture needed for {proposal.snippet!r}. "
                f"Say so if this should be a NEW entry."
            )
            reminded_ids = set()
        else:
            row_id = _pdw.create_pending(
                project_root,
                session_id,
                ups_seq=turn["ups_seq"],
                snippet=proposal.snippet,
                verbs=proposal.verbs,
                objects=proposal.objects,
                suggested_target=proposal.suggested_target,
                confidence=proposal.confidence,
                ambiguous=proposal.ambiguous,
            )
            _emit(
                project_root,
                session_id,
                "update_intent_detected",
                {
                    "status": "detected",
                    "row_id": row_id,
                    "snippet": proposal.snippet,
                    "confidence": proposal.confidence,
                    "ambiguous": proposal.ambiguous,
                    "suggested_target": proposal.suggested_target,
                    "signals": list(proposal.signals),
                },
            )
            blocks.append(
                _advise_text(
                    {
                        "snippet": proposal.snippet,
                        "verbs": list(proposal.verbs),
                        "objects": list(proposal.objects),
                        "suggested_target": proposal.suggested_target,
                        "ambiguous": proposal.ambiguous,
                    },
                    new=True,
                )
            )
            reminded_ids = {row_id}
    else:
        reminded_ids = set()

    # Repeat reminders for rows still pending from earlier turns (the
    # self-sustaining part of #221 — the system reminds itself).
    for row in _pdw.list_pending(project_root, session_id):
        if row["id"] in reminded_ids:
            continue
        blocks.append(_advise_text(row, new=False))
    return blocks


def _normalize_tool(tool_name: str) -> str:
    # Hosts prefix MCP tools (mcp__aidocs__ai_backlog); take the tail.
    return (tool_name or "").rsplit("__", 1)[-1].strip()


def _looks_successful(tool_response: Any) -> bool:
    if tool_response is None:
        return False
    if isinstance(tool_response, dict) and tool_response.get("ok") is False:
        return False
    try:
        flat = json.dumps(tool_response, default=str, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        flat = str(tool_response).lower()
    return not any(m in flat for m in _FAILURE_MARKERS)


def observe_tool_result(
    project_root: Path,
    session_id: str,
    tool_name: str,
    tool_input: dict | None,
    tool_response: Any,
) -> list[int]:
    """PostToolUse entry. Satisfies pending rows on a SUCCESSFUL durable write."""
    if not session_id:
        return []
    tool = _normalize_tool(tool_name)
    if tool == "ai_task":
        if not _is_task_todo_durable(tool_input):
            return []
    elif tool in _MODE_GATED_DURABLE:
        mode = str((tool_input or {}).get("mode") or "").strip().lower()
        if mode not in _MODE_GATED_DURABLE[tool]:
            return []
    elif tool not in _ALWAYS_DURABLE:
        return []
    if not _looks_successful(tool_response):
        return []
    satisfied = _pdw.satisfy_pending(
        project_root, session_id, satisfied_by=f"{tool}:{(tool_input or {}).get('mode', '')}"
    )
    for row_id in satisfied:
        _emit(
            project_root,
            session_id,
            "update_intent_satisfied",
            {"status": "satisfied", "row_id": row_id, "satisfied_by": tool},
        )
    return satisfied


def gate_stop(project_root: Path, session_id: str) -> dict[str, Any]:
    """Stop-hook consumer (#219 PR-2 — the BLOCK stage).

    In ``nlp.update_gate=block`` mode, an unsatisfied confidence-1.0 pending
    row HOLDS the turn-seal: the operator update must land in durable storage
    (ai_backlog/ai_task todo/ai_plan/memory_capture — auto-satisfied via
    ``observe_tool_result``) before the turn may end (empire §X: captured
    BEFORE the reply that acknowledges it). Ambiguous rows (confidence < 1.0)
    only advised — they NEVER hard-block (detector doctrine: only confidence
    1.0 may gate). advise/off modes never block.

    Pure read: never advances the turn or expires rows (that is the UPS
    side's job) — a Stop check must be idempotent.
    """
    empty = {"block": False, "reason": "", "pending": []}
    if not session_id:
        return empty
    if _gate_mode(project_root, session_id) != "block":
        return empty
    gating = [
        r
        for r in _pdw.list_pending(project_root, session_id)
        if float(r.get("confidence") or 0.0) >= 1.0 and not r.get("ambiguous")
    ]
    if not gating:
        return empty
    snippets = "; ".join(str(r.get("snippet") or "") for r in gating)
    reason = (
        f"Turn held (nlp.update_gate=block): operator update(s) not yet recorded "
        f"durably — {snippets}. Record via ai_backlog / ai_task(mode='add') / ai_plan / "
        f"memory_capture before ending the turn (empire §X)."
    )
    return {"block": True, "reason": reason, "pending": [r["id"] for r in gating]}

