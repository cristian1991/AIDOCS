"""Mid-flight UPS adapter — recover UserPromptSubmit parity for interrupt/queued prompts.

Claude Code fires the UserPromptSubmit hook ONLY at turn boundaries (see the CC source:
`handlePromptSubmit` → `executeUserInput` → the single `execPromptHook`/`executeUser
PromptSubmitHooks` site). A prompt the operator submits WHILE a turn is running is
ENQUEUED and later drained mid-turn into a `queued_command` attachment ("The user sent a
new message while you were working: …", messages.ts) via `getQueuedCommandAttachments`
(INLINE_NOTIFICATION_MODES = {'prompt','task-notification'}). That path never touches the
UPS hook, so a mid-flight operator prompt bypasses the ENTIRE UPS pipeline: the pre-flight
hostile-prompt judge (security gate), memory/doctrine surfacing, and user-intent grants.

CC persists each such submission in the session transcript as (verified against a real
transcript):

    {"type":"queue-operation","operation":"enqueue","content":<prompt>,"timestamp":<iso>}

`PreToolUse` is the ONLY hook CC fires mid-turn, so this adapter runs there. At each
PreToolUse it reads the transcript, finds `enqueue` operations newer than a per-session
cursor, runs the UPS pipeline on each exactly once, and maps the outcome onto the
PreToolUse envelope:

  * a HOSTILE mid-flight prompt → `permissionDecision: deny` — the pending tool is blocked
    and the prompt is judged BEFORE the agent acts on it (recovers "block-before-action"),
  * memory/doctrine/intent CONTEXT → `additionalContext`,
  * benign → passthrough (None), letting the normal pre-tool gate proceed.

This is the claude-adapter's mid-flight arm of UPS; no Claude Code fork is required.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

_ENQUEUE = "enqueue"
_BLOCKED_BY = "mid_flight_preflight"


@dataclass
class MidFlightVerdict:
    """Normalised UPS verdict for one mid-flight prompt."""

    block: bool
    reason: str = ""
    context_blocks: tuple = field(default_factory=tuple)


# ── pure: detection ──────────────────────────────────────────────────────────
def extract_mid_flight_enqueues(lines, after_ts: str = "") -> "list[tuple[str, str]]":
    """Return ``[(timestamp, content), …]`` for every ``queue-operation`` ``enqueue``
    with non-empty content and ``timestamp > after_ts`` (lexicographic ISO-8601 compare —
    correct for CC's zero-padded UTC stamps). Only ``enqueue`` counts as a NEW mid-flight
    prompt; ``remove``/``dequeue`` are drains, and non-queue entries are ignored. Garbage
    lines are skipped, never raised on — a hook must not wedge the host on a bad line."""
    out: list[tuple[str, str]] = []
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "queue-operation":
            continue
        if str(d.get("operation") or "") != _ENQUEUE:
            continue
        content = str(d.get("content") or "").strip()
        if not content:
            continue
        ts = str(d.get("timestamp") or "")
        if after_ts and ts <= after_ts:
            continue
        out.append((ts, content))
    return out


# ── pure: outcome → PreToolUse envelope ──────────────────────────────────────
def to_pretooluse_envelope(block: bool, reason: str, context_blocks) -> "dict | None":
    """Map a mid-flight UPS outcome onto a CC PreToolUse ``hookSpecificOutput`` envelope,
    matching the shapes ``claude_hook._handle_pre_tool_use`` already renders."""
    if block:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason or "mid-flight prompt blocked by pre-flight",
                "blocked_by": _BLOCKED_BY,
            }
        }
    blocks = [b for b in (context_blocks or ()) if b]
    if blocks:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "\n".join(blocks),
            }
        }
    return None


# ── cursor: judge each mid-flight prompt exactly once ────────────────────────
class _CursorStore:
    """Per-session high-water timestamp so a given mid-flight prompt is UPS-judged once,
    not re-judged on every subsequent PreToolUse. sqlite-first (doctrine); the file lives
    beside the other per-session index state."""

    def __init__(self, project_root: Path) -> None:
        self._db = Path(project_root) / ".MEMORY" / ".index" / "mid_flight_cursor.sqlite3"

    def _conn(self):
        import sqlite3

        self._db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mid_flight_cursor "
            "(session_id TEXT PRIMARY KEY, last_ts TEXT NOT NULL)"
        )
        return conn

    def get(self, session_id: str) -> str:
        if not session_id:
            return ""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT last_ts FROM mid_flight_cursor WHERE session_id=?", (session_id,)
                ).fetchone()
            return str(row[0]) if row and row[0] else ""
        except Exception:
            return ""

    def set(self, session_id: str, last_ts: str) -> None:
        if not session_id or not last_ts:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO mid_flight_cursor (session_id, last_ts) VALUES (?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET last_ts=excluded.last_ts",
                    (session_id, last_ts),
                )
                conn.commit()
        except Exception:
            pass


def _default_judge(runtime):
    """Run the REAL UPS pre-flight pipeline on a mid-flight prompt — the same
    ``PromptMutator.preflight_judge`` the turn-boundary UserPromptSubmit uses, so the
    security gate + memory/doctrine surfacing are byte-identical across both paths."""
    from .prompt_mutator import PromptMutator

    pm = PromptMutator(runtime)

    def _judge(prompt: str, project_root: Path) -> MidFlightVerdict:
        try:
            r = pm.preflight_judge(prompt=prompt, project_root=project_root)
        except Exception:
            # A security judge that cannot run fails CLOSED for the hostile axis is the
            # turn-boundary contract, but a mid-flight adapter must never wedge the host:
            # surface nothing (the normal pre-tool gate still runs) rather than raise.
            return MidFlightVerdict(block=False)
        return MidFlightVerdict(
            block=(getattr(r, "decision", "") == "block"),
            reason=str(getattr(r, "block_reason", "") or ""),
            context_blocks=tuple(getattr(r, "additional_context_blocks", ()) or ()),
        )

    return _judge


def evaluate(runtime, project_root, payload, *, judge=None, cursor_store=None) -> "dict | None":
    """The claude-adapter mid-flight UPS entry, called from PreToolUse.

    Reads the transcript, judges each mid-flight ``enqueue`` newer than the session cursor,
    and returns a PreToolUse envelope (deny on the FIRST hostile prompt; else merged
    ``additionalContext``; else None). The cursor advances so each prompt is judged once.
    ``judge``/``cursor_store`` are injectable for testing; both default to the live
    UPS pipeline + the sqlite cursor.
    """
    tpath = str((payload or {}).get("transcript_path") or "")
    if not tpath or not os.path.exists(tpath):
        return None
    sid = str((payload or {}).get("session_id") or (payload or {}).get("sessionId") or "")
    store = cursor_store or _CursorStore(Path(project_root))
    after = store.get(sid)
    try:
        with open(tpath, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return None

    enqueues = extract_mid_flight_enqueues(lines, after_ts=after)
    if not enqueues:
        return None

    judge = judge or _default_judge(runtime)
    context: list[str] = []
    newest = after
    for ts, content in enqueues:
        if ts and (not newest or ts > newest):
            newest = ts
        v = judge(content, project_root)
        if v.block:
            # Advance the cursor past the blocker too — a re-judge on the next PreToolUse
            # would just re-deny; the operator resolves the freeze out of band.
            store.set(sid, newest)
            return to_pretooluse_envelope(True, v.reason, ())
        if v.context_blocks:
            context.extend(v.context_blocks)
    store.set(sid, newest)
    return to_pretooluse_envelope(False, "", tuple(context))
