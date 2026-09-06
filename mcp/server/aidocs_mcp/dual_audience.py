"""Dual-audience output helpers for mutating MCP tools.

Every mutation returns two views:
  - agent: structured ack (✓ / ✓ N / ✗ <reason>) — 1-3 tokens
  - operator: TextContent lines — formatted for humans

Subagent-only tools (lane_*, agent_worker_*, conductor_lane_*) use
`ok_sub` / `fail_sub` — these pass empty content_blocks so the agent
gets pure ack with zero decorative punctuation or echoed input.

Read-only list modes do NOT go through here — they keep structured
dicts so downstream code can iterate.
"""

from __future__ import annotations

from typing import Any

from mcp.types import TextContent

from .tool_display import edit_result


def ok(
    *,
    tool_name: str,
    started_at: float | None = None,
    pretty_lines: list[str] | None = None,
    structured: dict[str, Any] | None = None,
) -> Any:
    """Successful dual-audience result.

    Agent sees: "✓" (or "✓ N" when structured["count"] is set, or
                "✓ dry-run" when structured["dry_run"] is True).
    Operator sees: pretty_lines rendered as TextContent blocks.
    """
    blocks = [TextContent(type="text", text=line) for line in (pretty_lines or [])]
    payload = {"ok": True}
    if structured:
        payload.update(structured)
    return edit_result(
        content_blocks=blocks,
        structured=payload,
        tool_name=tool_name,
        started_at=started_at,
    )


def fail(
    *,
    tool_name: str,
    error: str,
    started_at: float | None = None,
    extra_structured: dict[str, Any] | None = None,
) -> Any:
    """Failed dual-audience result. Agent sees "✗ <error>", operator same."""
    struct: dict[str, Any] = {"ok": False, "error": error}
    if extra_structured:
        struct.update(extra_structured)
    return edit_result(
        content_blocks=[TextContent(type="text", text=f"✗ {error}")],
        structured=struct,
        tool_name=tool_name,
        started_at=started_at,
    )


def ok_sub(
    *,
    tool_name: str,
    started_at: float | None = None,
    structured: dict[str, Any] | None = None,
) -> Any:
    """Subagent-facing success. No operator pretty lines, just the ack.

    Rationale: lane workers and background agents don't need a
    human-readable summary — the operator sees the lane's own output
    stream. Pure ack keeps the subagent context minimal.
    """
    payload = {"ok": True}
    if structured:
        payload.update(structured)
    return edit_result(
        content_blocks=[],
        structured=payload,
        tool_name=tool_name,
        started_at=started_at,
    )


def fail_sub(
    *,
    tool_name: str,
    error: str,
    started_at: float | None = None,
    extra_structured: dict[str, Any] | None = None,
) -> Any:
    """Subagent-facing failure. Bare '✗ <error>' — no echoed input,
    no nested quotes, no parens. See dual-audience rule in
    .MEMORY/rules/standards.md.
    """
    struct: dict[str, Any] = {"ok": False, "error": error}
    if extra_structured:
        struct.update(extra_structured)
    return edit_result(
        content_blocks=[TextContent(type="text", text=f"✗ {error}")],
        structured=struct,
        tool_name=tool_name,
        started_at=started_at,
    )


def fmt_tags(tags: list[str] | None) -> str:
    """Format a tag list for operator-facing lines. Empty → empty string."""
    if not tags:
        return ""
    return " [" + ", ".join(tags) + "]"


# ── Backward-compat aliases (2026-04-25 consolidation) ─────────────
# `dual_audience_helpers` module was created in error before this
# richer module was discovered. Aliases preserve the
# server_todo_backlog_tools.py import shape while funneling every
# mutating-tool caller into this single module.
#
# Pre-consolidation, ok_edit accepted an `ack` kwarg that the
# implementation ignored (`edit_result` synthesizes the ack from the
# structured payload). The wrapper below accepts + drops it so the
# swap is a no-op for existing callers.


def ok_edit(
    *,
    ack: str = "",
    pretty_lines: list[str],
    structured: dict[str, Any],
    tool_name: str,
    started_at: float,
) -> Any:
    """Alias for `ok` with legacy `ack` kwarg accepted (ignored).

    Kept for backward compatibility with callers that predate the
    module consolidation. New code should call `ok(...)` directly.
    """
    return ok(
        tool_name=tool_name,
        started_at=started_at,
        pretty_lines=pretty_lines,
        structured=structured,
    )


def fail_edit(
    *,
    error: str,
    tool_name: str,
    started_at: float,
    extra_structured: dict[str, Any] | None = None,
) -> Any:
    """Alias for `fail`. Backward-compat with legacy caller."""
    return fail(
        tool_name=tool_name,
        error=error,
        started_at=started_at,
        extra_structured=extra_structured,
    )
