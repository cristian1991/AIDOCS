"""MCP-alternative suggestions for raw host tools.

Lifted from claude_hook.py 2026-05-27 (Phase 2 of the thinning).
Pre-extraction, the `_MCP_ALTERNATIVES` dict lived as a class-field
constant on ``ClaudeHookHandler`` and was never actually read by
anything — it was placeholder data prepared for a feature that
shipped via a different code path. The doctrine (advise the agent
when it reaches for `grep` / `read` / `glob` to use an indexed AIDOCS
tool instead) lives in ``agent_orchestrator._suggest_mcp_alternative``;
this module is the canonical home for the suggestion CATALOG so
both surfaces consult the same data.

Public API:
  alternatives_for(raw_tool)        → list of (tool_call, description)
  format_suggestion(raw_tool)       → multiline advisory string suitable
                                       for embedding in a tool deny / hint
                                       envelope; empty when no alternatives
                                       are catalogued for the raw tool

Why "advisory" not "block": these are hints, not gates. A `grep` call
that landed at this surface has already passed the cascade — we're
just suggesting a higher-leverage tool. The gate cascade owns the
allow/deny decision; this module owns the hint copy.
"""

from __future__ import annotations

from collections.abc import Mapping

_MCP_ALTERNATIVES: Mapping[str, tuple[tuple[str, str], ...]] = {
    "grep": (
        ('ai_find(query, mode="symbols")', "Find symbols by name, kind, or role"),
        ('ai_find(query, mode="references")', "Find all usages of a symbol"),
        ('ai_schema(query, mode="field")', "Find a DB field/column across all entities"),
        ('ai_trace(query, mode="css_class")', "Find CSS rules matching class names"),
    ),
    "read": (
        (
            'ai_bundle(path, mode="file")',
            "Understand file structure without reading the whole file",
        ),
        ("ai_get_symbol_snippet", "Read just one symbol's code at a known location"),
        ('ai_find(query, mode="symbols")', "Locate the exact symbol before reading code"),
    ),
    "glob": (
        ("ai_search", "Find files by path/summary keywords"),
        ('ai_find(query, mode="partial_group")', "Find all partial class files for a C# type"),
    ),
}


def alternatives_for(raw_tool: str) -> tuple[tuple[str, str], ...]:
    """Return the catalogued AIDOCS alternatives for a raw host tool.

    Returns an empty tuple for tools not in the catalog — callers
    interpret that as "no advisory to render, just pass."
    """
    name = (raw_tool or "").strip().lower()
    # Strip Claude Code's CamelCase if present (Grep → grep).
    return _MCP_ALTERNATIVES.get(name, ())


def format_suggestion(raw_tool: str) -> str:
    """Render the suggestion as a multi-line advisory string.

    Empty string when no alternatives exist — callers can short-
    circuit on truthiness.
    """
    alts = alternatives_for(raw_tool)
    if not alts:
        return ""
    lines = [f"Consider using these AIDOCS tools instead of {raw_tool!r}:"]
    for call, desc in alts:
        lines.append(f"  • {call} — {desc}")
    return "\n".join(lines)
