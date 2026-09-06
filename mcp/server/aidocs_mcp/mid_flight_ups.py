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
cursor, replays the FULL UserPromptSubmit pipeline on each exactly once (as of
2026-07-28, #563: the same ``hook_pipeline.run_user_prompt`` a turn-boundary prompt
rides — before that only the stage-3 hostile judge ran here), and maps the outcome
onto the PreToolUse envelope:

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

# #476 — a mid-turn operator message whose TREATMENT fails must say so.
# The host delivers the raw text either way (CC drains its own queue), so the
# agent WILL act on an operator instruction that never got memory surfacing,
# doctrine, or an intent grant. No delivery GUARANTEE is available at this seam
# — the adapter is fail-open by design and must never wedge the host — so the
# contract is the honest one: never silent, always a named remedy.
_TREATMENT_FAILED = (
    "MID-TURN OPERATOR MESSAGE — TREATMENT FAILED (reported, not silent).\n"
    "The operator submitted this message while the turn was running. Its text "
    "reached you through the host queue, but the AIDOCS user-prompt pipeline "
    "could not run on it, so NO memory surfacing, doctrine, or intent grant was "
    "applied to it.\n"
    "REMEDY: re-route the message through aidocs_handle_prompt before acting on "
    "it.\nMESSAGE: {excerpt}"
)


def treatment_failed_notice(prompt: str) -> str:
    """Render the visible failure notice for one untreated mid-turn message."""
    excerpt = " ".join(str(prompt or "").split())
    if len(excerpt) > 200:
        excerpt = excerpt[:197] + "..."
    return _TREATMENT_FAILED.format(excerpt=excerpt or "(empty)")


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
        # #814/#755: the canonical connect establishes journal_mode=WAL (in the
        # FILE HEADER, so once per file), synchronous, busy_timeout and
        # foreign_keys=ON. On the default rollback journal a writer takes an
        # EXCLUSIVE lock over the whole file, so a concurrent reader cannot
        # overlap and gets SQLITE_BUSY — surfaced as a bare "database is
        # locked". This store sits on the governed prompt/tool path, which is
        # exactly where #746 observed that degradation.
        #
        # It also fixes a second bug for free: callers use `with self._conn()`,
        # and a plain sqlite3 connection's `with` is a TRANSACTION context
        # manager that never CLOSES the handle. ClosingConnection commits and
        # then closes (#756).
        from ._sqlite_connect import connect as _canonical_connect

        self._db.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(self._db)
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


def _synth_user_prompt_submit(prompt: str, project_root, payload) -> dict:
    """Build the ``UserPromptSubmit`` payload for a mid-turn prompt.

    Same SHAPE the host sends at a turn boundary, because the river reads the
    payload it is given: ``session_id`` is carried through so the origin gate
    (``hook_pipeline._ups_origin_gate``) resolves the same host session, and
    ``source_surface`` / ``delivery`` are deliberately ABSENT — those fields
    disqualify authority when non-empty, and a mid-turn operator submit is the
    same direct human submit as a turn-boundary one (the HOST wrote the
    ``queue-operation`` record this prompt came from; nothing here is relayed by
    an agent). ``mid_flight`` is provenance only: no gate reads it, and the
    adapter is reached exclusively from PreToolUse, so it cannot recurse.
    """
    src = payload or {}
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "cwd": str(project_root),
        "session_id": str(src.get("session_id") or src.get("sessionId") or ""),
        "transcript_path": str(src.get("transcript_path") or ""),
        "mid_flight": True,
    }


def verdict_from_ups_envelope(env) -> MidFlightVerdict:
    """Map the turn-boundary UPS envelope (``PromptSubmitResult.to_claude_envelope``)
    onto a mid-flight verdict: ``decision == "block"`` → block + reason;
    ``additionalContext`` → one context block; anything else → benign."""
    if not isinstance(env, dict):
        return MidFlightVerdict(block=False)
    if str(env.get("decision") or "") == "block":
        return MidFlightVerdict(block=True, reason=str(env.get("reason") or ""))
    hso = env.get("hookSpecificOutput")
    ctx = str(hso.get("additionalContext") or "") if isinstance(hso, dict) else ""
    return MidFlightVerdict(block=False, context_blocks=(ctx,) if ctx else ())


def _default_judge(runtime, payload=None):
    """Run the REAL, WHOLE UPS river on a mid-flight prompt.

    MEASURED BEHAVIOUR, and the correction of a lying docstring (#563/#564,
    2026-07-28). This function used to call ONLY
    ``PromptMutator.preflight_judge`` while claiming that "the security gate +
    memory/doctrine surfacing are byte-identical across both paths". They were
    not. ``preflight_judge`` is stage **3 of the 19** pinned in
    ``tests/host/test_ups_golden_trace.py::GOLDEN_UPS_TRACE``, so a mid-turn
    prompt reached the hostile-prompt judge and nothing else: the prompt was
    never recorded, freeze/unfreeze was never resolved from chat, and NO grant
    stage ran — no user-intent tool grants, no per-turn intent state, no
    sticky/soul/dnt/config-set/lane-exit grants, no ``intent_phrase_dispatch``
    (which is why a mid-turn operator phrase never minted anything), no
    notifications drain. On a clean prompt ``preflight_judge`` returns an empty
    result, so surfacing produced nothing either.

    NOW — primary path: ``hook_pipeline.run_user_prompt``, the exact callable
    ``ClaudeHookHandler._handle_user_prompt_submit`` delegates to (that method's
    whole body is this one call). A mid-flight prompt is replayed as a
    synthesized ``UserPromptSubmit`` payload, so it rides all 19 stages in the
    pinned order, under the same SEC-001/SEC-002 snapshot transaction and the
    same origin gate. A prompt is a prompt (operator ruling, 2026-07-27).

    FALLBACK — ``preflight_judge`` alone, reached ONLY when the river itself
    raises. Fail-open must never wedge the host, but a prompt that cannot be
    judged must not silently become an allow-with-no-intent either: the HOSTILE
    axis is re-run on the narrow path so a hostile mid-flight prompt still
    DENIES even when the full river is unavailable. If that ALSO fails the
    adapter still allows (never wedge the host) but surfaces
    ``treatment_failed_notice`` — an untreated operator instruction is announced
    with a named remedy rather than passed off as clean (#476).
    """

    def _judge(prompt: str, project_root: Path) -> MidFlightVerdict:
        try:
            from . import hook_pipeline as _hp

            env = _hp.run_user_prompt(
                runtime,
                Path(project_root),
                _synth_user_prompt_submit(prompt, project_root, payload),
                host_kind="claude_code",
            )
        except Exception:
            try:
                from .prompt_mutator import PromptMutator

                r = PromptMutator(runtime).preflight_judge(
                    prompt=prompt, project_root=project_root
                )
            except Exception:
                # #476: the last resort used to be a bare benign verdict — the
                # operator's mid-turn instruction got ZERO treatment and nobody
                # was told. Stay fail-open on delivery, but SAY SO.
                return MidFlightVerdict(
                    block=False,
                    context_blocks=(treatment_failed_notice(prompt),),
                )
            return MidFlightVerdict(
                block=(getattr(r, "decision", "") == "block"),
                reason=str(getattr(r, "block_reason", "") or ""),
                context_blocks=tuple(getattr(r, "additional_context_blocks", ()) or ()),
            )
        return verdict_from_ups_envelope(env)

    return _judge


def evaluate(runtime, project_root, payload, *, judge=None, cursor_store=None) -> "dict | None":
    """The claude-adapter mid-flight UPS entry, called from PreToolUse.

    Reads the transcript, judges each mid-flight ``enqueue`` newer than the session cursor,
    and returns a PreToolUse envelope (deny on the FIRST hostile prompt; else merged
    ``additionalContext``; else None). The cursor advances so each prompt is judged once.
    ``judge``/``cursor_store`` are injectable for testing; the default judge is the
    WHOLE live UPS pipeline (``_default_judge`` → ``hook_pipeline.run_user_prompt``,
    all 19 GOLDEN_UPS_TRACE stages), and the default cursor is the sqlite one.
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

    judge = judge or _default_judge(runtime, payload)
    context: list[str] = []
    newest = after
    for ts, content in enqueues:
        if ts and (not newest or ts > newest):
            newest = ts
        try:
            v = judge(content, project_root)
        except Exception:
            # #476: a judge that RAISES used to propagate into the caller's
            # blanket fail-open, erasing the whole batch with nothing said.
            # The message is marked seen (no infinite re-notice) and its lost
            # treatment is announced with a named remedy.
            context.append(treatment_failed_notice(content))
            continue
        if v.block:
            # Advance the cursor past the blocker too — a re-judge on the next PreToolUse
            # would just re-deny; the operator resolves the freeze out of band.
            store.set(sid, newest)
            return to_pretooluse_envelope(True, v.reason, ())
        if v.context_blocks:
            context.extend(v.context_blocks)
    store.set(sid, newest)
    return to_pretooluse_envelope(False, "", tuple(context))
