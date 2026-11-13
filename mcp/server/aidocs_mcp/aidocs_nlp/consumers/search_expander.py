"""Search expander — broaden ai_text_search queries via lemma +
semantic-category synonyms.

ai_text_search accepts `|` / `OR` for multi-term and regex=True for
patterns. The expander turns a single-word query into a `|`-joined
expansion so the operator gets matches across inflections and
related verbs.

Examples:
  "protect"   -> "protect|lock|guard|seal|shield"     (protect_verb category)
  "delete"    -> "delete|remove|drop|wipe|destroy"    (destroy_verb category)
  "edit"      -> "edit|change|modify|update|fix"      (edit_verb category)
  "fucking"   -> "fuck"                               (lemma normalization)
  "foo.py"    -> "foo.py" (path-like, no expansion)
  "user auth" -> "user auth" (multi-word phrase, no expansion)

Caller (ai_text_search) opts in via expand=True. Default off so
existing call sites stay exact.

"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..semantic_dict import SEMANTIC_CATEGORIES

if TYPE_CHECKING:
    from ..service import NLPService


# Categories worth expanding. Excluded: profanity / anger_marker
# (rage detection signals, not useful for code search), first_person
# / second_person / negation (grammar tokens, never search targets).
_EXPANDABLE_CATEGORIES = (
    "protect_verb",
    "unprotect_verb",
    "override_verb",
    "edit_verb",
    "create_verb",
    "destroy_verb",
    "execute_verb",
    "approve_verb",
    "deny_verb",
)

# Reject expansion for queries that look like a path / file token —
# the user typed an exact thing, expansion would dilute it.
_PATH_LIKE_RE = re.compile(r"[/\\]|\.[A-Za-z0-9]+$")


def expand_query(query: str, service: NLPService) -> str:
    """Return the expanded query string (possibly identical to input).

    Expansion rules:
    1. Multi-word queries: returned unchanged (user wrote a phrase).
    2. Path-like queries (contains /\\ or trailing extension):
       returned unchanged.
    3. Single-word queries: lemmatize via NLP. If lemma is in any
       expandable semantic category, return that category as
       `|`-joined alternation. Otherwise return the original.
    4. NLP unavailable: returned unchanged.

    Never raises. Output is always a valid ai_text_search query
    string (no regex metacharacters introduced beyond `|`).
    """
    if not query or not query.strip():
        return query
    stripped = query.strip()
    if " " in stripped or "\t" in stripped:
        return query
    if _PATH_LIKE_RE.search(stripped):
        return query
    substance = service.analyze_substance(stripped)
    if substance is None:
        return query
    # Get the lemma from the first verb/noun/adverb token found.
    lemma = ""
    for collection in (substance.verbs, substance.nouns, substance.adverbs):
        for tok in collection:
            lemma = tok.lemma.lower()
            break
        if lemma:
            break
    # Substance-empty fallback: spaCy occasionally tags bare imperative
    # verbs ("delete", "lock") as INTJ or X and they fall outside POS
    # buckets the Substance projection collects. For a single-token
    # query, the input IS effectively the lemma — try the category
    # match directly before giving up.
    if not lemma:
        lemma = stripped.lower()
    # Find the category this lemma belongs to.
    for cat_name in _EXPANDABLE_CATEGORIES:
        members = SEMANTIC_CATEGORIES.get(cat_name, frozenset())
        if lemma in members:
            # Sort for stable output (test-friendly + cache-friendly).
            expanded = "|".join(sorted(members))
            return expanded
    # Lemma differs from query (e.g. "protecting" -> "protect") but
    # isn't in any category. Still useful: return both as alternation.
    if lemma != stripped.lower():
        return f"{stripped}|{lemma}"
    return query


def expanded_terms(query: str, service: NLPService) -> tuple[str, ...]:
    """Return the expansion as a tuple of terms (for callers that
    want to render the expansion to the operator, not just feed it
    back into search). Includes the original query as the first
    element when it's not in the expansion.
    """
    expanded = expand_query(query, service)
    if expanded == query:
        return (query,)
    return tuple(expanded.split("|"))
