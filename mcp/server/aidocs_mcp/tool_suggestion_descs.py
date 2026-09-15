"""Terse usage one-liners for UPS tool suggestions.

Operator directive 2026-06-11: the NLP UPS surfacing told agents WHICH
tools to use but not HOW or WHY — bare names force the agent to guess
what the polymorphic params mean (the same gap the @modes `desc` work
closed at the schema layer). This catalog is the hook-side mirror:
static, import-cheap (no schema build, no sqlite), one line per tool.

Keep entries under ~110 chars: UPS context is token-budgeted and the
line is space-joined by some hosts. Unknown tools fall back to the bare
name, so this map never gates which tools may be suggested.
"""

from __future__ import annotations

TOOL_USAGE_DESCS: dict[str, str] = {
    "ai_find": (
        "code search: mode=symbols(query=name) | references(query=SYMBOL→callers) | "
        "dependencies(query=FILE PATH→import edges) | text(literal)"
    ),
    "ai_investigate": "concept → ranked container classes; for function names use ai_find",
    "ai_trace": "flow tracing: mode=references|field_flow|service|model|api_to_ui; query=symbol/field/route",
    "ai_bundle": "structural overview: mode=file(target=PATH) | symbol(NAME) | subsystem(concept) | partial(C# type)",
    "ai_get_lines": (
        "line-range read; file must be DISCOVERED by a search tool first "
        "(known_exact_path=true only for paths the user named verbatim)"
    ),
    "ai_get_outline": "one file → symbols+kinds+line numbers; far cheaper than reading it",
    "ai_get_symbol_snippet": "qualified symbol name → its exact source",
    "ai_get_dependencies": "FILE PATH → import/dependency edges",
    "ai_search": "find files by name/content summary",
    "ai_text_search": "full-text search over indexed source",
    "ai_schema": "DB schema: entities, columns, relationships",
    "ai_replace": (
        "unified edit: mode=anchor(between two unique anchors, PRIMARY) | "
        "string(old≤configured cap, default 1000) | symbol('Class.method'+new_body) | lines"
    ),
    "ai_batch_edit": "atomic multi-file line edits, bottom-up",
    "ai_create_file": "new file on a managed project (raw Write is blocked)",
    "ai_run": "governed shell; detached runs notify on completion — read output AFTER the \U0001f4e3 notify via ai_run_output",
    "ai_run_output": "COMPLETED run → log tail (raw_output=true for verbatim bytes)",
    "ai_task": "lifecycle: begin before non-trivial work → update at steps → complete with result+evidence; todos via add|list|remove + update with scope='task'|'session' (#83)",
    "memory_capture": "save a durable fact (kind=invariant|caveat|infrastructure|preference|workflow-rule)",
    "memory_search": "keyword search over durable memory",
    "ai_recall": "semantic recall over memory + palace",
    "ai_index_sync": "re-sync the code index after EXTERNAL file changes (edits auto-reindex)",
    "ai_git": "governed git operations (status/diff/commit/push)",
}


def describe_tools(names: list[str]) -> str:
    """Render suggested tools WITH usage one-liners.

    Tools without a catalog entry keep their bare name so the
    suggestion still surfaces. Returns a single line (hosts space-join
    context fragments).
    """
    parts = []
    for n in names:
        desc = TOOL_USAGE_DESCS.get(n)
        parts.append(f"{n} [{desc}]" if desc else n)
    return ", ".join(parts) + " suggested"
