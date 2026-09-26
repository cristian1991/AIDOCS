"""AND-first concept ranking over identifier tokens AND prose (#1039 A+ 5, #1040).

THE MISSES THIS FIXES (operator report 2026-09-07 + ubermega corroboration):

* ``ai_investigate("draft cleanup")`` ranked ``Document`` above
  ``StaleDraftCleanupJob`` — the container-name ranker scored on ONE term.
  The row matching "draft" AND "cleanup" must outrank the row matching only
  "document".
* the job's summary says "removes abandoned empty draft documents" — that
  prose IS the signal, and name-only matching never read it.
* ``ai_find(mode='symbols', query='update check runtime self update
  install')`` returned ~50 rows led by unrelated ``is_*`` helpers scoring on
  one term (the OR-flood).
* ``ai_search("migration sql")`` found nothing while ``tools/sql/...`` existed
  — a whole-phrase LIKE never matches path SEGMENTS.

Pure functions, no I/O, so every engine (investigate, clusters, touchpoints,
filename search) can share ONE ranking rule: rank by how many DISTINCT query
terms a row matches across its identifier tokens, path segments, container
and file summary; ties keep the engine's own order. Shipped as the ranker
FIRST, separately from any mode consolidation (#1040 review note 1) — it
breaks no caller and fixes the reported misses on its own.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "with",
        "by", "is", "are", "be", "this", "that", "from", "as", "at", "it",
    },
)
_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_MIN_PREFIX = 4


def query_terms(query: str) -> list[str]:
    """Lower-cased, de-duplicated query terms with stopwords removed.
    Identifier-shaped queries are split too, so "StaleDraftCleanup" asks for
    stale AND draft AND cleanup."""
    out: list[str] = []
    for chunk in _SPLIT_RE.split(str(query or "")):
        if not chunk:
            continue
        for piece in _CAMEL_RE.split(chunk):
            tok = piece.lower()
            if tok and tok not in _STOPWORDS and tok not in out:
                out.append(tok)
    return out


def text_tokens(*texts: str | None) -> set[str]:
    """Identifier/path/prose tokens: camelCase and snake_case split, path
    segments split, everything lower-cased."""
    toks: set[str] = set()
    for text in texts:
        if not text:
            continue
        for chunk in _SPLIT_RE.split(str(text)):
            if not chunk:
                continue
            for piece in _CAMEL_RE.split(chunk):
                if piece:
                    toks.add(piece.lower())
    return toks


def _term_matches(term: str, tokens: set[str]) -> bool:
    if term in tokens:
        return True
    # Light stemming by prefix: "document" ~ "documents", "clean" ~ "cleanup",
    # only when the shared prefix is long enough to mean something.
    if len(term) >= _MIN_PREFIX:
        for tok in tokens:
            if len(tok) >= _MIN_PREFIX and (tok.startswith(term) or term.startswith(tok)):
                return True
    return False


def terms_matched(terms: Iterable[str], tokens: set[str]) -> int:
    return sum(1 for t in terms if _term_matches(t, tokens))


def and_rank(
    rows: list[dict[str, Any]],
    terms: list[str],
    text_of: Callable[[dict[str, Any]], Iterable[str | None]],
    *,
    drop_unmatched: bool = True,
) -> list[dict[str, Any]]:
    """Stable AND-first ordering: rows matching MORE distinct terms come first;
    ties keep the incoming order. Each row is copied and stamped with
    ``terms_matched`` / ``terms_total``."""
    if not terms:
        return [dict(r, terms_matched=0, terms_total=0) for r in rows]
    scored: list[tuple[int, int, int, dict[str, Any]]] = []
    for idx, row in enumerate(rows):
        texts = [t for t in text_of(row)]
        n = terms_matched(terms, text_tokens(*texts))
        if drop_unmatched and n == 0:
            continue
        # The FIRST text is the row's IDENTITY (its symbol name, or the file
        # basename). At equal coverage a row whose own NAME carries the
        # terms outranks a sibling that only inherits them from the file it
        # lives in — StaleDraftCleanupJob above its Run() method.
        ident = terms_matched(terms, text_tokens(texts[0] if texts else ""))
        scored.append(
            (-n, -ident, idx, dict(row, terms_matched=n, identity_terms=ident, terms_total=len(terms))),
        )
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [r for _, _, _, r in scored]
