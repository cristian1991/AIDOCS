"""Single entry point for recording tool-call events into execution_events.

Before this module four writers composed ExecutionIndexStore.record_event
by hand with per-site ceremony, which fragmented the event_kind vocabulary,
made metrics reconciliation guesswork, and meant every new per-call feature
(token accounting, turn history, journal mirroring) had to remember to plug
into all four sites. Routing everything through `record()` here keeps the
vocabulary under one schema, makes it trivial to add cross-cutting fields,
and leaves `ExecutionIndexStore.record_event` as a low-level sqlite write
primitive rather than a shared interface.

Why not just call record_event directly? Two reasons:
  1. Consistent phase → event_kind mapping so dashboards don't have to
     branch on free-form strings.
  2. source_kind defaults per caller class so adding a new writer doesn't
     introduce yet another string variant by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Canonical phase → event_kind mapping. Existing dashboards + metrics
# parsers expect these strings, so the phase API hides them behind a
# stable vocabulary any writer can emit.
_PHASE_EVENT_KIND: dict[str, str] = {
    "started": "tool_call_started",
    "completed": "tool_call_completed",
    "failed": "tool_call_failed",
    "blocked": "tool_policy_block",
    "advisory": "judge_advisory",
    "guard_finding": "output_guard_finding",
    # Orchestrator-emitted markers keep their own kinds — the caller
    # passes one of these phase strings when they're the right shape.
    "test_runner_invocation": "test_runner_invocation",
    "test_retry_reset": "test_retry_reset",
    "test_retry_block": "test_retry_block",
    "bash_policy_block": "bash_policy_block",
    "bash_allowlist_block": "bash_allowlist_block",
    "bash_denylist_block": "bash_denylist_block",
    "raw_tool_block": "raw_tool_block",
    "lane_tool_block": "lane_tool_block",
    "judge_block": "judge_block",
    "pretooluse": "pretooluse",
    "sessionstart": "sessionstart",
    "userpromptsubmit": "userpromptsubmit",
    "prompt_classified": "prompt_classified",
    "postcompact": "postcompact",
    "native_tool_use": "native_tool_use",
    "raw_tool_grant": "raw_tool_grant",
    "lane_raw_tool_grant": "lane_raw_tool_grant",
}


def record(
    hub: Any,
    project_root: Path,
    *,
    phase: str,
    name: str | None,
    payload: dict[str, Any] | None = None,
    session_id: str | None = None,
    source: str = "mcp_call",
    action_kind: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    target_entity: str | None = None,
    observed_at: str | None = None,
) -> str:
    """Record a single event via ExecutionIndexStore.

    phase: one of the keys in _PHASE_EVENT_KIND. Unknown phases are
      written as-is to the event_kind column — the caller gets a
      non-standard kind but the row still lands, so a newly-invented
      kind never silently drops.
    name: capability_name / tool_name. None for prompt-level events.
    payload: arbitrary dict, merged into payload_json. Dedup at the
      INSERT boundary (content-addressed event_id + 2s bucket) means
      identical wire-duplicates collapse to one row.
    source: the module writing this event. Defaults to "mcp_call" for
      the wrapper's common path; orchestrator and claude_hook override.
    """
    event_kind = _PHASE_EVENT_KIND.get(phase, phase)
    return hub.execution.record_event(
        project_root,
        event_kind=event_kind,
        source_kind=source,
        session_id=session_id,
        capability_name=name,
        action_kind=action_kind,
        target_entity=target_entity,
        status=status,
        payload=payload or {},
        run_id=run_id,
        observed_at=observed_at,
    )


def record_run(
    hub: Any,
    project_root: Path,
    *,
    name: str | None,
    session_id: str | None = None,
    status: str = "started",
    metadata: dict[str, Any] | None = None,
    run_id: str | None = None,
    source: str = "mcp_call",
    run_kind: str = "mcp_tool_invocation",
    completed_at: str | None = None,
    ad_hoc: bool = True,
) -> str:
    # Separate helper because execution_runs is a lifecycle-tracking
    # table (one row per logical call, updated on status transitions),
    # not an event stream. The wrapper calls this twice per invocation
    # (started + completed) with the same run_id; the store's ON
    # CONFLICT DO UPDATE refreshes status/completed_at on the second
    # call without creating a twin.
    return hub.execution.record_run(
        project_root,
        run_kind=run_kind,
        source_kind=source,
        session_id=session_id,
        capability_name=name,
        status=status,
        ad_hoc=ad_hoc,
        metadata=metadata or {},
        run_id=run_id,
        completed_at=completed_at,
    )
