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

Dedupe rides the session_response_ledger (War AZ #474). The ledger state is
``g<compaction-generation>|top=<reported-ids>`` on BOTH the context and the
stop surface, so a block emits once per (session, generation, reported set).

It was ``epoch|counts|top-ids``, which was wrong on both halves and is the
bug the king named on 2026-07-30 ("why is backlog stop hook not deduping
across epoch/backlog changes"):

  * ``counts`` put the volatile PAYLOAD in the key — every backlog write by
    any lane flipped the hash, so the notice could not deduplicate while the
    project was busy (measured: emit_count 3657 on one session's context row,
    48 and 38 on single-host-session stop rows);
  * ``epoch`` (``agent_memory_epoch``) also moves on a host-identity
    ROTATION, which is not news about the backlog. #565 fixed that for the
    context surface only and left the stop surface behind.

The key is now STABLE under irrelevant change (count-only churn, identity
rotation) and SENSITIVE to relevant change (a compaction — the agent lost the
roster; or a change in WHICH items are reported). Counts still render in the
BLOCK, which is rebuilt on every emission, so no number is ever stale.

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

# #755/#756: the ONE canonical connect. The single site here was
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- with no pragmas.
# It is a pure SELECT of MAX(compaction_count), so read_only=True is the
# truthful mode: sqlite itself refuses a write, and the reader still
# carries synchronous, busy_timeout and foreign_keys.
from ._sqlite_connect import connect as _canonical_connect

__all__ = [
    "MAX_BLOCK_CHARS",
    "build_backlog_summary",
    "context_backlog_block",
    "stop_backlog_reminder",
]

_KEY_CONTEXT = "backlog_surface"
_KEY_STOP = "backlog_surface_stop"
# The imperative is gated SEPARATELY from the roster (#565): a re-emitted
# roster is information (the counts changed), but re-issuing an order the
# agent already carried out is not — it teaches the agent to discount the
# whole channel (false-affordance decay, law 311bf3e6).
_KEY_INSTRUCTION = "backlog_instruction"
MAX_BLOCK_CHARS = 600
_TOP_N = 3
_TITLE_CHARS = 64
# Active work statuses only — done/rejected/removed/merged never nag.
_ACTIVE_STATUSES = ("in_progress", "open", "blocked")
_PRIORITY_ORDER = ("critical", "urgent", "high", "normal", "low", "idea")

_INSTRUCTION = (
    "INSTRUCTION: tell the user about these backlogged task(s) now — "
    "report the ACTIVE counts as shown (open / in_progress / blocked) and name "
    "the top items. Full list: ai_backlog(mode='list')."
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
    """Current agent_memory_epoch, "" when unresolvable (best-effort).

    NO PRODUCTION CALLER REMAINS IN THIS MODULE (2026-07-30). #565 moved the
    context surface to ``_resolve_compaction_generation`` and the king-directive
    fix moved the stop surface too, because this hash rotates on host-identity
    change and a rotation is not news about the backlog.

    It is retained ONLY because seven tests in
    ``tests/runtime/test_backlog_surfacing_419.py`` still
    ``monkeypatch.setattr(bs, "_resolve_epoch", ...)``, which raises
    AttributeError if the name disappears. Those patches are now no-ops.
    REPORTED, not hidden: vulture sees a monkeypatch target as a string, not a
    reference, so this will read as project-wide dead code. The correct cleanup
    is to delete this function together with those seven patches (and the
    ``monkeypatch`` parameter of the five tests that then stop using it) — left
    to the Stop-surface owner rather than done unilaterally, because that file is
    a two-owner seam.
    """
    try:
        from .helper_skill_injector import _resolve_epoch as _re

        return _re(
            project_root,
            host_kind=host_kind or None,
            host_session_id=host_session_id or None,
        )
    except Exception:
        return ""


def _resolve_compaction_generation(
    project_root: Path,
    *,
    host_kind: str = "",
    host_session_id: str = "",
) -> int:
    """Compaction generation for THIS caller (0 on any doubt).

    THE DEDUP KEY MUST NOT ROTATE (#565 <- #539) AND MUST NOT BE SHARED (#722).
    ``agent_memory_epoch`` is ``sha16(agent_context_id + compaction_count)`` and
    therefore moves for TWO unrelated reasons: a compaction (which SHOULD
    re-surface everything — the agent lost its context) and a host-identity change
    (which should surface nothing — the backlog did not change and the agent still
    has the roster). Keying on the epoch conflated them, so the ~8 reconnect
    rotations per session (#539) each looked like news.

    #565 fixed that by keying on the compaction COUNT alone — the only genuinely
    news-bearing half — and took ``MAX`` across the project's identity rows so a
    freshly-rotated host id (which starts with no row, i.e. 0) could not read as a
    generation change. That reasoning is sound and the rotation fix worked.

    BUT A PROJECT-WIDE MAX IS ALSO A SHARED ONE (#722). Every identity row counts,
    including LANE WORKERS AND SUBAGENTS. A subagent compacting its own context
    raised the max, which changed the PARENT conductor's state hash, which re-fired
    the parent's Stop block and UPS banner — on a backlog that never changed and a
    roster the parent was still holding. Measured 2026-08-01: seven consecutive Stop
    blocks in one conductor session while five subagents ran, reporting byte-
    identical items every time. Operator ruling, verbatim: "a sub-agent compaction
    SHOULD NOT affect parent!!!"

    So: WHEN THE CALLER'S IDENTITY IS KNOWN, READ THAT IDENTITY'S OWN COUNT AND
    NOTHING ELSE. Another agent's compaction is not news about this agent's roster.
    Per-identity is also monotone for a given identity — a row's own count only ever
    advances — so the rotation guarantee #565 bought is preserved WITHIN a caller,
    which is the only place it was ever needed: both surfaces key their ledger row
    on the caller too, so a rotated identity gets a fresh ledger row and a
    first-sight emission rather than an oscillating one.

    When the caller's identity is NOT known, fall back to the project-wide MAX —
    the previous behaviour, and still the safest available answer, since a lost
    generation costs one extra emission and never a lost one. Fails to 0 (never
    raises).
    """
    if host_kind and host_session_id:
        try:
            from .agent_memory_epoch import get_compaction_count

            return int(
                get_compaction_count(
                    project_root,
                    host_kind=host_kind,
                    host_session_id=host_session_id,
                )
                or 0
            )
        except Exception:
            return 0
    generation = 0
    try:
        from . import agent_memory_epoch as _ame

        with _canonical_connect(
            str(_ame._db_path(project_root)), read_only=True, row_factory=False
        ) as conn:
            row = conn.execute(
                "SELECT MAX(compaction_count) FROM agent_memory_compaction_state",
            ).fetchone()
        if row:
            generation = int(row[0] or 0)
    except Exception:
        generation = 0
    return generation


def build_backlog_summary(
    project_root: Path,
    *,
    include_instruction: bool = True,
) -> tuple[str, str] | None:
    """(block, state-signature) for the open backlog.

    None when the backlog is empty (no-padding law) or the store is
    unavailable (fail-quiet). The signature feeds the ledger dedupe:
    it changes whenever counts or the top items change.

    ``include_instruction=False`` renders the roster WITHOUT the
    tell-the-user imperative, for a re-emission whose counts changed but
    whose order the agent already obeyed (#565). The signature is
    unaffected — it describes the BACKLOG, never the rendering, so the
    two callers can never disagree about whether something changed.
    """
    try:
        from . import project_backlog_store as _pbs

        rows: list[dict[str, Any]] = []
        status_counts: dict[str, int] = {}
        for status in _ACTIVE_STATUSES:
            chunk = _pbs.list_backlog(project_root, status=status, limit=500)
            # Counted from the QUERY, not from a row field: the number then
            # cannot disagree with the set that produced it, whatever shape a
            # row happens to carry.
            status_counts[status] = len(chunk)
            rows.extend(chunk)
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
    # #805: the set is ACTIVE (open + in_progress + blocked) and is deliberately
    # so -- a nag must cover everything still being worked. Calling it "OPEN"
    # named a different, smaller set: an operator who checked with
    # ai_backlog(status='open') got a smaller number and no way to tell which
    # was broken. Measured 2026-08-17: banner 261 vs list 222.
    #
    # A metric nobody can reproduce with the obvious command trains its reader
    # to ignore it, and this one carries the criticals. So say what is counted
    # and show the split -- blocked items usually want an operator decision
    # rather than an agent, which the merged total hid.
    status_parts = [
        f"{status_counts[s]} {s}" for s in _ACTIVE_STATUSES if status_counts.get(s)
    ]
    header = (
        f"📋 ACTIVE BACKLOG — {total} item(s) "
        f"[{' / '.join(status_parts)}]: {', '.join(count_parts)}."
    )

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
    if include_instruction:
        lines.append(_INSTRUCTION)
    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:  # defense in depth; instruction is last
        block = block[:MAX_BLOCK_CHARS]

    # THE DEDUP KEY MUST NOT CONTAIN THE VOLATILE PAYLOAD (king directive
    # 2026-07-30). This was `total=N|<per-priority counts>|top=<ids>`, which put
    # the very numbers the block REPORTS into the key that decides whether to
    # report them. A signature that moves whenever the backlog moves cannot
    # deduplicate a backlog notice: it is stable only while nothing happens and
    # is guaranteed to churn precisely when the project is busy. Measured on the
    # live ledger before the fix: emit_count 3657 on one session's
    # 'backlog_surface' row, and 48 / 38 on single-host-session
    # 'backlog_surface_stop' rows, while seven lanes drove total 120 -> 142.
    #
    # That 3657 is also the proof that #565 aimed at the wrong half: it removed
    # identity ROTATION from the context key (epoch -> compaction generation) and
    # the churn continued, because rotation was never the binding constraint.
    #
    # So the signature now describes WHAT IS BEING SAID, not how much exists:
    #   STABLE under (must NOT re-emit) — a count-only change, i.e. another lane
    #     filing an item that does not enter the reported set; and any
    #     host-identity rotation (the generation half of the key handles that).
    #   SENSITIVE to (MUST re-emit) — a change in WHICH items are reported, and
    #     a compaction (the agent lost the roster; the generation half re-fires).
    # The counts still appear in the rendered BLOCK, which is rebuilt fresh on
    # every emission — dropping them from the KEY never shows a stale number, it
    # only declines to re-tell the operator the same items because a total moved.
    sig = "top=" + ",".join(str(r.get("id")) for r in top)
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
    is a lane worker, or this (compaction-generation, backlog-state) was
    already told.

    ``host_kind`` / ``host_session_id`` are accepted for signature parity
    with the other surfacers and are deliberately NOT part of the dedup
    key — see the rationale inline below (#565).
    """
    try:
        if _is_worker_context():
            return None
        built = build_backlog_summary(project_root)
        if built is None:
            return None
        block, sig = built
        # THE DEDUP KEY MUST NOT ROTATE (#565 <- #539).
        #
        # This was `should_emit(session_id, 'backlog_surface', f"{epoch}|{sig}")`.
        # The epoch is sha16(agent_context_id + compaction_count), so it moved for
        # two unrelated reasons and the state could not tell them apart:
        #   * a COMPACTION — must re-surface (the agent lost its context);
        #   * a HOST-IDENTITY change — must surface nothing (the backlog did not
        #     change and the agent still has the roster in front of it).
        # Worse, the two writers of this one row disagree about identity:
        # prompt_context_service:211 passes host_kind/host_session_id EXPLICITLY
        # from the UPS payload, while notification_injector:334 passes neither and
        # falls back to `_detect_host_kind()` (the MCP server's OWN env, which
        # need not carry the host's CLAUDE_CODE_VERSION) plus
        # `current_calling_host_session_id()` (a per-request ContextVar, else a
        # process global). Each write invalidated the other's hash on the same
        # row, so an UNCHANGED roster re-emitted on EVERY tool call — measured
        # 12/12 in test_alternating_identity_does_not_emit_every_call, ~10k
        # tokens per conductor session live, and the operator's real complaint.
        #
        # So gate on the compaction GENERATION — the only news-bearing half — and
        # leave identity out of the key entirely. Post-compaction re-emission is
        # preserved exactly (test_compaction_reemits pins it, #475/#232); a mere
        # rotation is now silent, which is the whole point.
        generation = _resolve_compaction_generation(
            project_root, host_kind=host_kind, host_session_id=host_session_id
        )
        if not _should_emit_open(
            project_root, session_id, _KEY_CONTEXT, f"g{generation}|{sig}"
        ):
            return None
        # The roster earned its place (first sight, or the counts moved). The
        # IMPERATIVE has to earn its own: re-issuing an order the agent already
        # obeyed is not information, and #565 names it as what trains an agent
        # to stop reading the channel. Once per (session, epoch) — a compaction
        # rotates the epoch, so a fresh agent is instructed again.
        instruct = _should_emit_open(
            project_root, session_id, _KEY_INSTRUCTION, f"g{generation}"
        )
        if instruct:
            return block
        lean = build_backlog_summary(project_root, include_instruction=False)
        return lean[0] if lean else block
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
        # #565 migrated the CONTEXT surface off the rotating ``agent_memory_epoch``
        # and onto the compaction GENERATION, but left THIS surface behind on the
        # old key — so the Stop hook kept re-blocking on every host-identity
        # rotation, which is not news about the backlog. Same authority, same
        # rationale as ``context_backlog_block``: the generation is the only
        # news-bearing half, and taking the MAX across the project's identity rows
        # makes it rotation-immune by construction.
        #
        # The ledger row stays keyed on ``host_session_id`` (per-session, so one
        # session can never suppress another's reminder).
        generation = _resolve_compaction_generation(
            project_root, host_kind=host_kind, host_session_id=host_session_id
        )
        if not _should_emit_strict(
            project_root, host_session_id, _KEY_STOP, f"g{generation}|{sig}"
        ):
            return None
        reason = (
            "🛑 ACTIVE BACKLOG UNREPORTED — do not end the turn silently while "
            "work is queued.\n"
            f"{block}\n"
            "Tell the user what is still active (counts + top items), then stop."
        )
        return {"decision": "block", "reason": reason}
    except Exception:
        return None
