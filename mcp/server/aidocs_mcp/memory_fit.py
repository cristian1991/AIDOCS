"""Pre-add memory FIT check + empire-worthiness classification.

Empire directive 2026-07-06 (extends empire-doctrine §X/§XI to memory):
kingdom memories stay in the kingdom index/palace; portable rules and
workflows are EMPIRE-WORTHY. Before an automatic add, existing memory is
searched to decide where the new entry fits:

  duplicate  → refuse (update the existing entry, or override explicitly)
  neighbor   → proceed, surfacing the near matches for possible merge
  novel      → proceed

Empire-worthiness is a CANDIDATE flag only. Per §XIII / §XX, agent-sourced
content is evidence, never auto-promoted law: promotion to the empire tier
happens through the operator/trusted path, not here.

Pure logic — the caller supplies the search function, so this module stays
store-agnostic and unit-testable.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# Token-overlap thresholds. Deliberately coarse: the fit check is a triage
# aid, not a semantic judge — false "neighbor" beats false "duplicate".
DUPLICATE_THRESHOLD = 0.75
NEIGHBOR_THRESHOLD = 0.35

_WORD = re.compile(r"[a-z0-9][a-z0-9_\-./\\]{2,}")

# Machine/deployment-specific markers: content carrying these describes ONE
# machine or ONE deployment, not a portable law.
_ABS_PATH = re.compile(r"(?:^|[\s\"'(])(?:[A-Za-z]:\\|/(?:opt|home|etc|var|usr|tmp)/)")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# Kinds whose content is portable-by-shape (how to operate / what must hold /
# operator taste). Everything else is kingdom-scoped by definition.
_PORTABLE_KINDS = frozenset({
    "workflow-rule", "workflow_rule", "workflow", "rule", "rules",
    "process", "procedure", "ops", "runbook", "guideline", "standard",
    "convention",
    "invariant", "security", "policy", "schema", "contract", "constraint",
    "always", "must", "never",
    "preference", "style", "tone", "taste", "communication",
})


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of word tokens — 0.0..1.0."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def containment(candidate: str, existing: str) -> float:
    """Fraction of the CANDIDATE's tokens already present in the existing
    text — the dedup metric. A search row carries frame tokens (path,
    title, snippet ellipsis) that dilute symmetric Jaccard; what matters
    for "is this already captured?" is one-directional coverage."""
    tc, te = _tokens(candidate), _tokens(existing)
    if not tc or not te:
        return 0.0
    return len(tc & te) / len(tc)


def _row_text(row: Any) -> str:
    if isinstance(row, dict):
        return " ".join(str(v) for v in row.values() if isinstance(v, str))
    return str(row)


def _query_for(content: str, max_terms: int = 6) -> str:
    # The most distinctive (longest) tokens make the best search probes.
    # Tie-break lexicographically: _tokens is a SET, so a length tie under
    # a plain len key made the probe hash-order-dependent per process —
    # the same content could hit or miss the AND-search by luck.
    toks = sorted(_tokens(content), key=lambda t: (len(t), t), reverse=True)[:max_terms]
    return " ".join(toks)


def assess_fit(
    content: str,
    search: Callable[..., Any],
    *,
    limit: int = 10,
    fetch_text: Callable[[Any], str] | None = None,
) -> dict[str, Any]:
    """Classify a to-be-added memory against existing memory.

    `search(query, limit=...)` returns rows (dicts with text fields). A
    search failure degrades to 'novel' with search_degraded=True — the fit
    check is triage, never a write-blocker on a broken index.

    `fetch_text(row)` (optional) returns the FULL body behind a search row —
    search snippets are clipped windows that drop edge tokens, which can
    push a true duplicate below the bar (found live: containment 0.667 on a
    verbatim re-add). When provided, each row scores against max(row text,
    full body); a fetch failure falls back to the row text.
    """
    try:
        rows = search(_query_for(content), limit=limit) or []
        if isinstance(rows, dict):  # error-shaped payload
            rows = rows.get("result") or []
    except Exception:
        return {"verdict": "novel", "matches": [], "best_score": 0.0, "search_degraded": True}
    scored = []
    for row in rows:
        s = containment(content, _row_text(row))
        if fetch_text is not None:
            try:
                full = fetch_text(row) or ""
            except Exception:
                full = ""
            if full:
                s = max(s, containment(content, full))
        if s >= NEIGHBOR_THRESHOLD:
            entry = dict(row) if isinstance(row, dict) else {"text": str(row)}
            entry["fit_score"] = round(s, 3)
            scored.append(entry)
    scored.sort(key=lambda r: r["fit_score"], reverse=True)
    best = scored[0]["fit_score"] if scored else 0.0
    if best >= DUPLICATE_THRESHOLD:
        verdict = "duplicate"
    elif best >= NEIGHBOR_THRESHOLD:
        verdict = "neighbor"
    else:
        verdict = "novel"
    return {"verdict": verdict, "matches": scored[:5], "best_score": best}


def is_empire_candidate(kind: str, content: str) -> tuple[bool, str]:
    """(candidate, reason). A candidate is portable-by-shape AND free of
    machine/deployment-specific markers. This flags; it never promotes."""
    k = (kind or "").strip().lower()
    if k not in _PORTABLE_KINDS:
        return False, f"kind {k!r} is kingdom-scoped (not a portable rule/invariant/preference)"
    if _ABS_PATH.search(content or ""):
        return False, "content carries an absolute path (machine-specific, not portable)"
    if _IP.search(content or ""):
        return False, "content carries an IP address (deployment-specific, not portable)"
    return True, "portable kind, no machine-specific markers"
