"""Razor outline extractor (Roslyn-backed via tools/aidocs-csharp-outliner).

Doctrine (2026-05-28): Razor (.cshtml / .razor) outline extraction
runs through the Roslyn daemon when available. The daemon's ext-hint
field lets it route the request to RazorExtractor on the .NET side
WITHOUT a temp file. Returns the AIDOCS-canonical (symbol, kind,
line, container, is_partial) tuple list, or empty when the Roslyn
tool isn't installed.

Why a SEPARATE dispatcher for .cshtml: the legacy regex extractor
(in code_index_outline_service.py:_extract_razor_outline) catches
AIDOCS-domain patterns Roslyn doesn't try for: Lang.T(...) i18n keys,
partials, RenderSection, asp-page-handler forms, Component.InvokeAsync.
On DentalClinic-WebApp's Edit.cshtml (2754L), the regex extractor
produces 172 entries vs Roslyn's 6 — they're COMPLEMENTARY, not
redundant. The `merge_cshtml_outline` helper here unions both.

Performance (measured 2026-05-28 on DentalClinic-WebApp/Edit.cshtml):
    single-shot Roslyn:  260 ms / file
    daemon Roslyn warm:   19 ms / file  ← faster than regex (22 ms)
    regex:                22 ms / file
"""

from __future__ import annotations


def extract_razor_outline(text: str) -> list[tuple[str, str, int, str | None, bool]]:
    """Roslyn-only extract from .razor source (Blazor file)."""
    return _roslyn_razor_outline(text, ext=".razor")


def extract_cshtml_outline(text: str) -> list[tuple[str, str, int, str | None, bool]]:
    """Roslyn-only extract from .cshtml source (ASP.NET Razor view)."""
    return _roslyn_razor_outline(text, ext=".cshtml")


def merge_cshtml_outline(
    text: str,
    regex_rows: list[tuple[str, str, int, str | None, bool]] | None = None,
) -> list[tuple[str, str, int, str | None, bool]]:
    """Return Roslyn outline UNIONED with the regex outline.

    Doctrine: Roslyn covers the C#-side declarations (@page, @model,
              @inject, @code/@functions block symbols). Regex covers
              the AIDOCS-domain patterns (Lang.T keys, partials,
              sections, forms). Both are first-class search anchors
              for different user intents. Always run both, dedup by
              (symbol, kind, line).
    Why:      on a real cshtml file (DentalClinic Edit.cshtml: 2754L),
              Roslyn alone returns 6 entries, regex alone returns 172,
              and the union returns 178 with zero overlap — the two
              extractors are non-overlapping by design.
    Apply:    callers pass pre-computed regex_rows when they already
              have them (avoids re-running regex). When None, this
              function does NOT run regex (regex is the caller's
              domain — different code path); pass-through is via the
              ``regex_rows`` parameter.
    """
    roslyn_rows = extract_cshtml_outline(text)
    if regex_rows is None:
        return roslyn_rows
    # Dedup by full tuple (symbol, kind, line, container, is_partial).
    # Order: roslyn first (rare, semantic) then regex (common, domain).
    seen: set[tuple[str, str, int]] = set()
    merged: list[tuple[str, str, int, str | None, bool]] = []
    for row in roslyn_rows:
        key = (row[0], row[1], row[2])
        if key not in seen:
            seen.add(key)
            merged.append(row)
    for row in regex_rows:
        key = (row[0], row[1], row[2])
        if key not in seen:
            seen.add(key)
            merged.append(row)
    return merged


def _roslyn_razor_outline(
    text: str,
    *,
    ext: str,
) -> list[tuple[str, str, int, str | None, bool]]:
    """Daemon-preferred Razor outline path.

    Calls into csharp_roslyn_client with the extension hint, which
    routes through the daemon (fast) or single-shot subprocess
    (slower fallback) depending on availability. Returns [] when
    Roslyn isn't installed at all — caller has to live without the
    Roslyn entries (regex still works).
    """
    try:
        from ..csharp_roslyn_client import is_available, roslyn_outline
    except Exception:
        return []
    if not is_available():
        return []
    rows = roslyn_outline(text, ext=ext)
    return rows if rows is not None else []
