"""Clause 2 (#213) — tier the cross-project doctrine into GLOBAL empire LAW.

empire-doctrine is the CROSS-PROJECT scroll; its principles should surface in
EVERY project when relevant (via discover_relevant_memory / global_law_store),
without duplicating the full scroll. So each high-value principle becomes a LEAN
POINTER row: a one-line gist + "load full via ai_skill('empire-doctrine')",
keyed by DISTINCTIVE keywords (low false-positive — a noisy hint is one agents
learn to ignore). aidocs-doctrine (project-private, formerly king-doctrine) is NOT globalized.

Seed lives in code (re-runnable, durable, testable) — it also partly closes the
"doctrine has no seed" gap (#228) for the global tier.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

_LOAD = "Load full: ai_skill('empire-doctrine')."

#: Provenance marker: rows the seed owns carry this `source`. Any other source
#: means an operator (or the promotion path) wrote the row — the seed never
#: reclaims it (#480, mirroring the #479 contract in skill_store).
_SEED_SOURCE = "doctrine_global_law_seed"

#: (empire_db_path, law_id) pairs whose operator-row skip was already logged —
#: one visible line per process, not one per bootstrap open (#479 precedent).
_ENSURE_SKIP_LOGGED: set[tuple[str, str]] = set()

# (law_id, keywords, lean gist).  Keywords are distinctive on purpose — the
# adversarial test proves unrelated prompts surface nothing.
DOCTRINE_GLOBAL_LAW: tuple[tuple[str, str, str], ...] = (
    ("empire-doctrine/XII-migrate",
     "migrate, migration, half-migration, orphaning, rename, move the source",
     ("§XII migrate without orphaning: copy first -> update the discovery surface -> "
     "verify end-to-end -> delete source -> update defensive markers -> focused tests. "
     "Never source-deleted/destination-unfound.")),
    ("empire-doctrine/VIII-kind-law",
     "tyrant, hard removal, mercy, kind law",
     ("§VIII the law is kind: honest mistakes -> mercy + correction; hard removal is the "
     "last resort, never the first.")),
    ("empire-doctrine/XIV-friction",
     "third refusal, gate refuses, workaround, friction",
     ("§XIV friction is the kingdom speaking: the third refusal outweighs the first — "
     "reconsider your shape, do not bypass.")),
    ("empire-doctrine/X-total-capture",
     "uncaptured, total capture, durable storage",
     ("§X total capture: every directive -> durable storage (todo/backlog/memory); "
     "metaphors verbatim; nothing discussed but uncaptured. Memory is two-tier (120%.md §9).")),
    ("empire-doctrine/II-120pct",
     "120% enforceable, defense in depth, deterministic",
     ("§II 120% correct + enforceable + deterministic: words alone are not law — only "
     "audit/gate/schema makes doctrine binding. Enforcement: 120%.md §2/§15A/§23.")),
    ("empire-doctrine/VI-appreciation-critique",
     "rubber-stamp, rubber stamp, substantiated approval",
     ("§VI appreciation is critique: silent approval is failure; succeed at a concrete "
     "flaw / alternative / risk, or log unease.")),
    ("empire-doctrine/XIII-overrides",
     "kill_switch, free reign, override",
     ("§XIII operator overrides are signals not shortcuts: use the override for THE work "
     "AND report the gap that made it necessary. Override-as-routine undoes the kingdom.")),
    ("empire-doctrine/VII-kings-word",
     "rogue, obedience after, after rendering",
     ("§VII the Empire's word is final: before rendering, counsel welcomed; after rendering, "
     "obedience; disobedience after correction = rogue.")),
)


def _seed_row(law_id: str, keywords: str, gist: str) -> None:
    """Force-upsert ONE shipped row with the seed's provenance marker."""
    from .global_law_store import upsert_global_law

    upsert_global_law(
        law_id=law_id,
        kind="doctrine",
        content=f"empire-doctrine {gist} {_LOAD}",
        keywords=keywords,
        sovereign_owner="operator",
        source=_SEED_SOURCE,
    )


def seed_doctrine_global_law() -> int:
    """Upsert the empire-doctrine lean-pointer rows into the global LAW store.
    Idempotent (upsert by law_id). Returns the count seeded.

    FORCE path — overwrites whatever holds each law_id, including operator
    rows. Bootstrap-only by design; every recurring caller must go through
    ensure_doctrine_global_law, which respects operator ownership (#480)."""
    n = 0
    for law_id, keywords, gist in DOCTRINE_GLOBAL_LAW:
        _seed_row(law_id, keywords, gist)
        n += 1
    return n


def ensure_doctrine_global_law() -> int:
    """Row-wise idempotent bootstrap hook (#231; #480 amendment 2026-07-19 —
    the old first-law-id gate returned 0 whenever row 0 existed, so partial
    wipes never healed and payload updates never refreshed stale rows).

    Per shipped law, mirroring the #479 seed contract in
    skill_store._ensure_bundled_seed:
      * absent law_id → INSERT with source='doctrine_global_law_seed'.
      * present with the seed's source but drifted content/keywords, or
        retired → refresh/heal (the shipped payload self-heals on upgrade;
        retiring a seed row is a wipe, healed like any other loss — pinned).
      * present with any OTHER source → an operator (or the promotion path)
        owns the row: NEVER overwritten, NEVER resurrected if retired. When
        the row diverges from the shipped payload the skip is logged once
        per process — visible, never silent.
    Returns the count of rows written (0 when everything already matches)."""
    from .global_law_store import _empire_db, read_global_law

    written = 0
    for law_id, keywords, gist in DOCTRINE_GLOBAL_LAW:
        content = f"empire-doctrine {gist} {_LOAD}"
        row = read_global_law(law_id, include_retired=True)
        if row is None:
            _seed_row(law_id, keywords, gist)
            written += 1
            continue
        matches = (
            row.get("status") == "active"
            and row.get("content") == content
            and row.get("keywords") == keywords
        )
        if matches:
            continue
        if row.get("source") == _SEED_SOURCE:
            _seed_row(law_id, keywords, gist)  # heal wipe / refresh stale
            written += 1
            continue
        # Operator-owned row diverges from the shipped payload: the row wins;
        # say so once per process so bootstrap opens don't spam (#479/#480).
        key = (str(_empire_db()), law_id)
        if key not in _ENSURE_SKIP_LOGGED:
            _ENSURE_SKIP_LOGGED.add(key)
            _log.warning(
                "doctrine global-law seed: skip %s — row (source=%r, status=%r) "
                "differs from the shipped payload; the operator row holds the "
                "ground (#480: the seed never reclaims operator writes)",
                law_id, row.get("source"), row.get("status"),
            )
    return written
