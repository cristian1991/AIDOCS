"""FastMCP ToolResult -> JSON payload -- a neutral public helper.

A pure shape conversion shared by tool_interface's local dispatch and the
gate's executor (design decision 2026-10-29: no public module imports a gate
file, so the helper lives here and outer_gate_executor re-exports it). It
decides nothing.
"""

from __future__ import annotations

import json
from typing import Any


def result_payload(tool_result: Any) -> Any:
    """Extract a JSON-serializable payload from a FastMCP ToolResult. Prefer the
    structured content; then the raw .data (so a tool's own return dict -- e.g.
    ai_run's {run_id, done, tail} -- surfaces, making ai_run_output usable even for
    inline-completed runs); otherwise parse the text content blocks. Never return a
    pydantic model (only basic JSON types from .data).
    """
    sc = getattr(tool_result, "structured_content", None)
    if sc is not None:
        return sc
    data = getattr(tool_result, "data", None)
    if isinstance(data, (dict, list, str, int, float, bool)):
        try:
            json.dumps(data)
            return data
        except (TypeError, ValueError):
            pass
    blocks = getattr(tool_result, "content", None) or []
    out: list[Any] = []
    for b in blocks:
        txt = getattr(b, "text", None)
        if txt is None:
            continue
        try:
            out.append(json.loads(txt))
        except (ValueError, TypeError):
            out.append(txt)
    if len(out) == 1:
        return out[0]
    return out
