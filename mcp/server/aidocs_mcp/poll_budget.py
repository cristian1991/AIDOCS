"""Shared poll budget for ai_run_status + ai_run_output.

Problem: ai_run_output rate-limits blocking waits (wait_seconds>0),
but ai_run_status is unrated. An agent that wants to know when a run
finishes can alternate calls to both tools and poll faster than the
notify-on-done path ever fires — defeating the whole point of notify.

Fix: both tools share a per-run poll counter. Once the counter hits
MAX_POLLS_PER_RUN while the run is still executing, further polls
refuse with the standard "wait for the notification" message. The
counter auto-resets the moment the run finishes (operator asking about
a completed run is always legitimate).

State is in-process memory. Good enough: the MCP server is one process,
and the budget is advisory against an agent that's chasing its own
tail. Agents that DO restart lose the counter — intentional, so a
restart gets a fresh budget.
"""

from __future__ import annotations

import threading
from typing import Any

# Tunable: how many poll calls (status + non-blocking output combined)
# an agent gets per run before the refusal fires. 3 is enough for
# "check if it started, tail output, confirm running" without letting
# a tight polling loop slip through.
MAX_POLLS_PER_RUN = 3

_counter_lock = threading.Lock()
_poll_counts: dict[str, int] = {}


def _reset(run_id: str) -> None:
    with _counter_lock:
        _poll_counts.pop(run_id, None)


def record_poll(run_id: str) -> int:
    """Increment the poll counter for `run_id` and return the new count."""
    with _counter_lock:
        _poll_counts[run_id] = _poll_counts.get(run_id, 0) + 1
        return _poll_counts[run_id]


def peek(run_id: str) -> int:
    with _counter_lock:
        return _poll_counts.get(run_id, 0)


def reset_on_done(run_id: str, run_is_done: bool) -> None:
    """Drop the counter once the run finishes so post-completion reads
    (agent fetching the final tail) aren't rate-limited.
    """
    if run_is_done:
        _reset(run_id)


def evaluate_poll_budget(
    run_id: str,
    run_is_done: bool,
    *,
    tool_name: str,
) -> dict[str, Any]:
    """Shared rate-limit decision for ai_run_status and
    ai_run_output non-blocking reads.

    Returns:
      {"ok": True} when the caller can proceed.
      {"ok": False, "error": ..., "blocked_by": "poll_budget",
       "polls_so_far": N, "max_polls": MAX_POLLS_PER_RUN} when refused.

    """
    if run_is_done:
        _reset(run_id)
        return {"ok": True}
    count = record_poll(run_id)
    if count <= MAX_POLLS_PER_RUN:
        return {"ok": True, "polls_so_far": count, "max_polls": MAX_POLLS_PER_RUN}
    return {
        "ok": False,
        "blocked_by": "poll_budget",
        "error": (
            f"Polling budget exhausted for run {run_id!r} "
            f"({count}/{MAX_POLLS_PER_RUN} via {tool_name}). "
            "Notify-on-done is universal — you'll get a 📣 "
            "notification block in your next tool response when this "
            "run finishes. Do other work until then. Budget resets "
            "automatically once the run completes."
        ),
        "polls_so_far": count,
        "max_polls": MAX_POLLS_PER_RUN,
        "run_id": run_id,
    }
