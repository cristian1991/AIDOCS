"""Outline-symbol identifier hygiene (War AW, 2026-07-19).

The code index carried garbage symbols — truncated fragments and
punctuation-bearing strings emitted by indexer edge cases (e.g. the TSX
outline pass writing ``emoryKgNo (``-style fragments). Garbage rows poison
every ranked search downstream, so validation now happens at OUTLINE-WRITE
time: every symbol must pass a language-aware identifier grammar or be
repaired; unrepairable rows are dropped before they reach code_outlines.

Consumers:
- code_index_sync_service: filters parsed outline rows through
  ``clean_outline_symbol`` before INSERT.
- mcp/scripts/repair_outline_symbols_2026_07_19.py: one-shot stamped sweep
  for a pre-existing (live) index — conductor-run only.
- mcp/tests/indexing/test_outline_symbol_hygiene.py: census both ways.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

# Default identifier grammar: letters/underscore/$ start, word chars after,
# with dotted segments allowed (C# explicit interface impls "IFoo.Bar",
# namespaced outline symbols). Covers Python/TS/JS/C#/Rust/Go identifiers.
_DEFAULT_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$")

# CSS-family symbols legitimately carry hyphens (`.user-card`,
# `--theme-color` custom properties, mixins).
_CSS_IDENT = re.compile(r"^-{0,2}[A-Za-z_][A-Za-z0-9_-]*$")
_CSS_KINDS = frozenset(
    {"css_class", "css_variable", "css_id", "keyframes", "mixin"}
)

# Kinds whose symbols ARE language identifiers — the grammar is enforced
# here and ONLY here. Structural outline kinds carry non-identifier
# symbols by design (initializer "document:DOMContentLoaded", page_route
# "/patients/list", media_query "(max-width: 768px)", data_attribute
# "item-id", api_call "/api/patients/42", state "state@L12", code_block
# "@functions", ...) and are exempt from identifier validation.
_IDENTIFIER_KINDS = frozenset(
    {
        "class",
        "struct",
        "record",
        "interface",
        "enum",
        "function",
        "method",
        "property",
        "field",
        "constant",
        "enum_member",
        "variable",
        "component",
        "hook",
        "context_provider",
        "type_alias",
        "constructor",
        "delegate",
        "event",
        "module",
        "namespace",
        "trait",
        "impl",
        "macro",
    }
)

# Repair: strip junk from the edges, then take the leading identifier run
# (cut at the first "(" / space / operator). Minimum surviving length 2 —
# a one-char remnant of a truncated fragment is noise, not a symbol.
_EDGE_JUNK = re.compile(r"^[^A-Za-z0-9_$-]+|[^A-Za-z0-9_$.-]+$")
_LEAD_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.]*")
_LEAD_CSS = re.compile(r"^-{0,2}[A-Za-z_][A-Za-z0-9_-]*")
_MIN_REPAIRED_LEN = 2


def _pattern_for_kind(kind: str | None) -> re.Pattern[str]:
    return _CSS_IDENT if kind in _CSS_KINDS else _DEFAULT_IDENT


def kind_is_validated(kind: str | None) -> bool:
    """True when the identifier grammar applies to this outline kind.

    ``None`` (unknown/bare validation) IS validated with the default
    grammar; structural kinds (routes, initializers, media queries, ...)
    are exempt — their symbols are not identifiers by design."""
    return kind is None or kind in _IDENTIFIER_KINDS or kind in _CSS_KINDS


def is_valid_symbol(symbol: str, kind: str | None = None) -> bool:
    """True when ``symbol`` passes the identifier grammar for its kind.

    Structural (non-identifier) kinds only require a non-empty symbol."""
    if not symbol:
        return False
    if not kind_is_validated(kind):
        return True
    return _pattern_for_kind(kind).match(symbol) is not None


def repair_symbol(symbol: str, kind: str | None = None) -> str | None:
    """Best-effort repair of a near-identifier symbol.

    Strips edge junk (quotes, parens, whitespace) and truncates at the
    first non-identifier character. Returns None when nothing
    identifier-shaped of length >= 2 survives.
    """
    if not symbol:
        return None
    cleaned = _EDGE_JUNK.sub("", symbol.strip())
    if is_valid_symbol(cleaned, kind):
        return cleaned if len(cleaned) >= _MIN_REPAIRED_LEN else None
    lead = _LEAD_CSS if kind in _CSS_KINDS else _LEAD_IDENT
    m = lead.match(cleaned)
    if not m:
        return None
    candidate = m.group(0).rstrip(".")
    if len(candidate) >= _MIN_REPAIRED_LEN and is_valid_symbol(candidate, kind):
        return candidate
    return None


def clean_outline_symbol(symbol: str, kind: str | None = None) -> str | None:
    """Write-time gate: valid symbols pass through unchanged, near-valid
    ones are repaired, garbage returns None (row must be dropped)."""
    if is_valid_symbol(symbol, kind):
        return symbol
    return repair_symbol(symbol, kind)


def census_non_identifier_symbols(
    conn: sqlite3.Connection,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Scan code_outlines for symbols that fail the identifier grammar.

    Returns offending rows (path, symbol, kind, line_number, repaired) —
    ``repaired`` is the repair result or None when the row is only
    droppable. Empty list == clean index.
    """
    offenders: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT path, symbol, kind, line_number FROM code_outlines ORDER BY path, line_number",
    ):
        symbol = row["symbol"] if isinstance(row, sqlite3.Row) else row[1]
        kind = row["kind"] if isinstance(row, sqlite3.Row) else row[2]
        if is_valid_symbol(symbol or "", kind):
            continue
        offenders.append(
            {
                "path": row["path"] if isinstance(row, sqlite3.Row) else row[0],
                "symbol": symbol,
                "kind": kind,
                "line_number": int(
                    row["line_number"] if isinstance(row, sqlite3.Row) else row[3]
                ),
                "repaired": repair_symbol(symbol or "", kind),
            },
        )
        if len(offenders) >= limit:
            break
    return offenders
