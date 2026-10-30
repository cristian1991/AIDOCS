"""Single symbol-match scorer for the code index (War AW, 2026-07-19).

Every ranked symbol surface (ai_find mode=symbols, ai_investigate's symbol
probe, ai_get_symbol_snippet's resolver) consumes THIS module — one weight
table, one score function, one tier vocabulary. Before this module, phase
membership was the only rank: Phase 2's unordered %variant% substring pass
flooded the response (#393 / #183 — "1 correct hit + ~49 unrelated DTOs, a
context-window mugging").

Weight changes are corpus-regression-tested acts: see
mcp/tests/indexing/test_symbol_ranking_golden.py before touching
SCORE_WEIGHTS.
"""

from __future__ import annotations

import re

# ── Weight table ─────────────────────────────────────────────────────────
# exact:      case-insensitive string equality OR token-normalized equality
#             (memory_kg_graph == MemoryKgGraph == "memory kg graph").
# prefix:     symbol starts with the needle.
# word_component: the full needle token sequence appears as a contiguous
#             run of the symbol's camel/snake components.
# substring:  scaled by len(needle)/len(symbol) and match position, always
#             below word_component and above any container-only hit.
# token_overlap: partial needle-token overlap (the DTO-flood band) — capped
#             at the container ceiling so ANY real symbol hit outranks it.
# container:  container-only hits are penalized below any symbol hit
#             (symbol floor is substring_floor=41; container cap is 40).
# fuzz:       rapidfuzz WRatio (>= 85 gate) mapped to <= 84 — fuzz can
#             NEVER outrank a direct (exact/strong) hit.
SCORE_WEIGHTS: dict[str, int] = {
    "exact": 100,
    "prefix": 90,
    "word_component": 85,
    "substring_base": 45,
    "substring_span": 30,
    "substring_position_penalty_max": 10,
    "substring_floor": 41,
    "substring_ceiling": 80,
    "token_overlap_span": 40,
    "container_exact": 40,
    "container_prefix": 34,
    "container_word": 30,
    "container_substring": 24,
    "fuzz_cap": 84,
    "fuzz_shift": 16,
}

TIER_EXACT = "exact"
TIER_STRONG = "strong"
TIER_RELATED = "related"
TIER_FUZZY = "fuzzy"
DIRECT_TIERS = frozenset({TIER_EXACT, TIER_STRONG})

# Strong tier threshold: word_component and prefix land here; anything
# below is "related".
_STRONG_FLOOR = SCORE_WEIGHTS["word_component"]

_SPLIT_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SPLIT_PUNCT = re.compile(r"[_\-./\s]+")


def tokenize(name: str) -> tuple[str, ...]:
    """camelCase / PascalCase / snake_case / kebab-case / dotted / spaced
    → lowercase component tuple. ``MemoryKgGraph`` → (memory, kg, graph)."""
    if not name:
        return ()
    s = _SPLIT_ACRONYM.sub(r"\1 \2", name)
    s = _SPLIT_CAMEL.sub(r"\1 \2", s)
    s = _SPLIT_PUNCT.sub(" ", s)
    return tuple(t.lower() for t in s.split() if t)


def _contiguous_run(needle_tokens: tuple[str, ...], symbol_tokens: tuple[str, ...]) -> bool:
    """True when the FULL needle token sequence appears as a contiguous run
    inside the symbol's tokens (word-boundary / camel-component match)."""
    n = len(needle_tokens)
    if not n or n > len(symbol_tokens):
        return False
    return any(
        symbol_tokens[i : i + n] == needle_tokens for i in range(len(symbol_tokens) - n + 1)
    )


def _match_one(needle: str, symbol: str) -> int:
    """Score ONE needle string against ONE symbol. 0 = no direct relation."""
    if not needle or not symbol:
        return 0
    n = needle.lower()
    s = symbol.lower()
    if s == n:
        return SCORE_WEIGHTS["exact"]
    nt = tokenize(needle)
    st = tokenize(symbol)
    if nt and nt == st:
        # Token-normalized equality: same identifier, different casing or
        # separators ("memory kg graph" == MemoryKgGraph == memory_kg_graph).
        return SCORE_WEIGHTS["exact"]
    if s.startswith(n):
        return SCORE_WEIGHTS["prefix"]
    if _contiguous_run(nt, st):
        return SCORE_WEIGHTS["word_component"]
    pos = s.find(n)
    if pos >= 0:
        ratio = len(n) / max(len(s), 1)
        raw = (
            SCORE_WEIGHTS["substring_base"]
            + round(SCORE_WEIGHTS["substring_span"] * ratio)
            - min(pos, SCORE_WEIGHTS["substring_position_penalty_max"])
        )
        return max(
            SCORE_WEIGHTS["substring_floor"],
            min(SCORE_WEIGHTS["substring_ceiling"], raw),
        )
    if nt and st:
        overlap = len(set(nt) & set(st))
        if overlap:
            # Partial-token band (the DTO flood): UserDto for "user service"
            # gets 40 * 1/2 = 20 — never above a container hit, never above
            # any full symbol hit.
            return max(1, round(SCORE_WEIGHTS["token_overlap_span"] * overlap / len(nt)))
    return 0


def _container_score(best_container_match: int) -> int:
    """Map a symbol-style match score on the CONTAINER to the penalized
    container-only band (cap 40 — below any symbol hit, whose floor is 41)."""
    if best_container_match >= SCORE_WEIGHTS["exact"]:
        return SCORE_WEIGHTS["container_exact"]
    if best_container_match >= SCORE_WEIGHTS["prefix"]:
        return SCORE_WEIGHTS["container_prefix"]
    if best_container_match >= SCORE_WEIGHTS["word_component"]:
        return SCORE_WEIGHTS["container_word"]
    if best_container_match > 0:
        return min(SCORE_WEIGHTS["container_substring"], best_container_match)
    return 0


def tier_for_score(score: int, *, is_fuzz: bool = False) -> str:
    if is_fuzz:
        return TIER_FUZZY
    if score >= SCORE_WEIGHTS["exact"]:
        return TIER_EXACT
    if score >= _STRONG_FLOOR:
        return TIER_STRONG
    return TIER_RELATED


def score_symbol_row(
    needle: str,
    symbol: str,
    container: str = "",
    variants: tuple[str, ...] | list[str] = (),
) -> tuple[int, str]:
    """Score one candidate row. Returns (score, tier).

    ``variants`` are alternate spellings of the SAME query (CamelCase joins,
    snake joins) — the best variant wins. Container-only hits (symbol did
    not match at all) are penalized below any symbol hit.
    """
    best = _match_one(needle, symbol)
    for v in variants:
        if v != needle:
            s = _match_one(v, symbol)
            if s > best:
                best = s
    if best > 0:
        return best, tier_for_score(best)
    if container:
        cbest = _match_one(needle, container)
        for v in variants:
            if v != needle:
                s = _match_one(v, container)
                if s > cbest:
                    cbest = s
        cscore = _container_score(cbest)
        if cscore > 0:
            return cscore, TIER_RELATED
    return 0, TIER_RELATED


def fuzz_score(wratio: float) -> int:
    """Map a rapidfuzz WRatio (85..100 after the gate) into the fuzz band.

    min(84, wratio - 16) — order-preserving, hard-capped BELOW the strong
    floor (85) so fuzz can never outrank a direct hit."""
    return max(1, min(SCORE_WEIGHTS["fuzz_cap"], round(wratio) - SCORE_WEIGHTS["fuzz_shift"]))


# ── Tie-breaks ───────────────────────────────────────────────────────────

_CONTAINER_KINDS = {"class", "struct", "record", "interface", "enum", "component", "type_alias"}
_CALLABLE_KINDS = {"function", "method", "constructor", "hook", "initializer"}


def kind_rank(kind: str | None) -> int:
    """Tie-break: container/callable kinds outrank variable-ish kinds."""
    if kind in _CONTAINER_KINDS:
        return 0
    if kind in _CALLABLE_KINDS:
        return 1
    return 2


def majority_top_package(paths: list[str]) -> str | None:
    """Top-level path segment shared by the majority of (direct-hit) paths.

    Used for the locality tie-break: rows in the same top-level package as
    the query majority sort ahead of equal-score rows elsewhere."""
    counts: dict[str, int] = {}
    for p in paths:
        seg = str(p).replace("\\", "/").split("/", 1)[0]
        if seg:
            counts[seg] = counts.get(seg, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


def locality_rank(path: str, majority: str | None) -> int:
    if majority is None:
        return 0
    return 0 if str(path).replace("\\", "/").split("/", 1)[0] == majority else 1


def row_is_inside_scope(
    candidate_container: str,
    own_container: str,
    symbol: str,
) -> bool:
    """True when an outline row whose container chain is ``candidate_container``
    lies INSIDE the scope of ``own_container.symbol`` (#481).

    The line_end span law (#478) needs one question answered: "is the next
    outline row still part of this symbol's body?". The first implementation
    asked it with a NAME-membership test (``symbol in container.split(".")``),
    which is not a scope test — any later row whose chain merely mentions the
    name counted as body. That is wrong for every member whose name equals its
    container's name (i.e. every C# constructor: ``class Gate { Gate() }``):
    each following sibling member looked nested, so the constructor's span ran
    to the end of the file.

    Containment is a CHAIN test: the symbol's own scope chain must appear as a
    contiguous run inside the candidate's container chain. Contiguous-run
    matching (rather than a strict prefix) tolerates extractors that record
    namespace prefixes inconsistently between a container row and the rows
    nested inside it.
    """
    cand = [p for p in str(candidate_container or "").split(".") if p]
    if not cand:
        return False
    own = [p for p in str(own_container or "").split(".") if p]
    own.append(str(symbol))
    n = len(own)
    return any(cand[i : i + n] == own for i in range(len(cand) - n + 1))
