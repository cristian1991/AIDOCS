"""Discovery family — read-only code search / inspection tools.

These were local-only (advertised via @server.tool but absent from the gate
registry). Declaring them here as surface=BOTH promotes them onto the web
surface too — web agents get the discovery core (search, trace, investigate,
symbol/outline/dependency inspection). Each is a thin delegate to the same
local handler the stdio surface already uses; READ class auto-extends the
gate's read-exec allowlist.
"""

from __future__ import annotations

from typing import Any

from ..tool_interface import BOTH, READ, R, _READ_ANN, _delegate, tool


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_search(
    query: str = "",
    modified_since: str | None = None,
    limit: int = 10,
) -> Any:
    """Find files by name or summary. Use modified_since to filter by recency:
    'today', '1h', '24h', '7d'. Replaces Glob — returns ranked results with
    language and role info."""
    return _delegate("ai_search", query=query, modified_since=modified_since, limit=limit)


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_text_search(
    query: str,
    glob: str | None = None,
    case_sensitive: bool = False,
    regex: bool = False,
    limit: int = 50,
    include_tests: bool | None = None,
    expand: bool = False,
) -> dict:
    """Full-text search across all indexed files. Replaces Grep — returns
    matches with line numbers. Use | or OR for multi-term, regex=true for
    patterns, `glob` to scope to a subset of files, expand=true to broaden a
    single-word query via NLP lemma + semantic synonyms. include_tests
    defaults to the project's index.include_tests setting."""
    return _delegate(
        "ai_text_search",
        query=query,
        glob=glob,
        case_sensitive=case_sensitive,
        regex=regex,
        limit=limit,
        include_tests=include_tests,
        expand=expand,
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_trace(
    query: str,
    mode: str = "field_flow",
    limit: int = 50,
    max_depth: int | None = None,
    timeout: int | None = None,
) -> Any:
    """Unified trace tool — follow a field/value/service/component/route
    through the codebase. Modes include field_flow, service, model, component,
    css_class, api_to_ui, query_shape, setting; mode='references' for callers."""
    return _delegate(
        "ai_trace", query=query, mode=mode, limit=limit, max_depth=max_depth, timeout=timeout
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_investigate(
    concept: str,
    limit: int = 5,
    depth: str = "standard",
    focus: str = "general",
    timeout: int | None = None,
) -> Any:
    """Concept investigation — ranks container symbols (classes, structs,
    records, interfaces) that match the concept. Returns findings plus suggested
    next tools, paths pre-granted for follow-up reads. For a known symbol name
    use ai_find(mode='symbols'); for a concept/type/feature use this."""
    return _delegate(
        "ai_investigate", concept=concept, limit=limit, depth=depth, focus=focus, timeout=timeout
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_get_outline(path: str) -> Any:
    """Return the outline (symbols + kinds + line numbers) of a single indexed
    file — much cheaper than reading the whole file when you just need to know
    what's in it (e.g. before ai_get_symbol_snippet to pick the right symbol)."""
    return _delegate("ai_get_outline", path=path)


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_get_symbol_info(
    symbol: str,
    kind: str = "signature",
    symbols: list[str] | None = None,
    container: str | None = None,
    include_related: bool = False,
    limit: int = 20,
    path: str = "",
) -> dict:
    """Get symbol metadata without reading files. kind: signature, signatures,
    constructor, constructors, enum, properties, api. Pass `path` to
    disambiguate same-named symbols across files."""
    return _delegate(
        "ai_get_symbol_info",
        symbol=symbol,
        kind=kind,
        symbols=symbols,
        container=container,
        include_related=include_related,
        limit=limit,
        path=path,
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_get_symbol_snippet(
    path: str,
    symbol: str,
    kind: str | None = None,
    line_number: int | None = None,
) -> Any:
    """Return an exact code snippet for an indexed outline symbol."""
    return _delegate(
        "ai_get_symbol_snippet", path=path, symbol=symbol, kind=kind, line_number=line_number
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_get_dependencies(path: str) -> Any:
    """Return lightweight dependency edges for one indexed code file."""
    return _delegate("ai_get_dependencies", path=path)
