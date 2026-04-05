"""Tests for MCP tool call token estimation."""

from __future__ import annotations

import json
from pathlib import Path

from aidocs_mcp.execution_index_store import ExecutionIndexStore


def test_token_estimates_aggregate_from_completed_events(tmp_path: Path) -> None:
    """Token estimates are summed from completed tool call payloads."""
    store = ExecutionIndexStore()
    store.init_db(tmp_path)

    # Record a completed event with token estimates
    store.record_event(
        tmp_path,
        event_kind="tool_call_completed",
        source_kind="mcp_call",
        capability_name="code_find",
        action_kind="mcp_tool_call",
        status="completed",
        payload={
            "tool_name": "code_find",
            "tokens_in_estimate": 500,
            "tokens_out_estimate": 120,
        },
    )
    store.record_event(
        tmp_path,
        event_kind="tool_call_completed",
        source_kind="mcp_call",
        capability_name="code_get_lines",
        action_kind="mcp_tool_call",
        status="completed",
        payload={
            "tool_name": "code_get_lines",
            "tokens_in_estimate": 300,
            "tokens_out_estimate": 80,
        },
    )

    summary = store.query_execution_summary(tmp_path)
    tokens = summary["token_estimates"]

    assert tokens["tokens_in"] == 800
    assert tokens["tokens_out"] == 200
    assert tokens["total"] == 1000


def test_token_estimates_zero_when_no_completed_events(tmp_path: Path) -> None:
    """Token estimates are zero when there are no completed events."""
    store = ExecutionIndexStore()
    store.init_db(tmp_path)

    # Record a started event (no token data)
    store.record_event(
        tmp_path,
        event_kind="tool_call_started",
        source_kind="mcp_call",
        capability_name="code_find",
    )

    summary = store.query_execution_summary(tmp_path)
    tokens = summary["token_estimates"]

    assert tokens["tokens_in"] == 0
    assert tokens["tokens_out"] == 0
    assert tokens["total"] == 0


def test_token_estimates_filter_by_session(tmp_path: Path) -> None:
    """Token estimates respect session_id filter."""
    store = ExecutionIndexStore()
    store.init_db(tmp_path)

    store.record_event(
        tmp_path,
        event_kind="tool_call_completed",
        source_kind="mcp_call",
        session_id="session-a",
        payload={"tokens_in_estimate": 100, "tokens_out_estimate": 50},
    )
    store.record_event(
        tmp_path,
        event_kind="tool_call_completed",
        source_kind="mcp_call",
        session_id="session-b",
        payload={"tokens_in_estimate": 200, "tokens_out_estimate": 75},
    )

    summary_a = store.query_execution_summary(tmp_path, session_id="session-a")
    summary_b = store.query_execution_summary(tmp_path, session_id="session-b")
    summary_all = store.query_execution_summary(tmp_path)

    assert summary_a["token_estimates"]["tokens_in"] == 100
    assert summary_b["token_estimates"]["tokens_in"] == 200
    assert summary_all["token_estimates"]["tokens_in"] == 300


def test_token_estimation_formula() -> None:
    """Token estimation uses ~4 chars per token."""
    test_args = {"query": "AccessGate", "mode": "symbols", "limit": 50}
    args_json = json.dumps(test_args, default=str)
    estimated_tokens_out = max(1, len(args_json) // 4)

    # {"query": "AccessGate", "mode": "symbols", "limit": 50} is ~55 chars -> ~13 tokens
    assert 10 <= estimated_tokens_out <= 20

    # A large result
    large_result = "x" * 4000
    estimated_tokens_in = max(1, len(large_result) // 4)
    assert estimated_tokens_in == 1000
