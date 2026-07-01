"""Clause 2 (#213) — tier the cross-project doctrine into GLOBAL empire LAW.

empire-doctrine is the CROSS-PROJECT scroll; its principles should surface in
EVERY project when relevant (via discover_relevant_memory / global_law_store),
without duplicating the full scroll. So each high-value principle becomes a LEAN
POINTER row: a one-line gist + "load full via ai_skill('empire-doctrine')",
keyed by DISTINCTIVE keywords (low false-positive — a noisy hint is one agents
learn to ignore). king-doctrine (project-specific) is NOT globalized.

Seed lives in code (re-runnable, durable, testable) — it also partly closes the
"doctrine has no seed" gap (#228) for the global tier.
"""

from __future__ import annotations

_LOAD = "Load full: ai_skill('empire-doctrine')."

# (law_id, keywords, lean gist).  Keywords are distinctive on purpose — the
# adversarial test proves unrelated prompts surface nothing.
DOCTRINE_GLOBAL_LAW: tuple[tuple[str, str, str], ...] = (
    ("empire-doctrine/XII-migrate",
     "migrate, migration, half-migration, orphaning, rename, move the source",
     "§XII migrate without orphaning: copy first -> update the discovery surface -> "
     "verify end-to-end -> delete source -> update defensive markers -> focused tests. "
     "Never source-deleted/destination-unfound."),
    ("empire-doctrine/VIII-kind-law",
     "tyrant, hard removal, mercy, kind law",
     "§VIII the law is kind: honest mistakes -> mercy + correction; hard removal is the "
     "last resort, never the first."),
    ("empire-doctrine/XIV-friction",
     "third refusal, gate refuses, workaround, friction",
     "§XIV friction is the kingdom speaking: the third refusal outweighs the first — "
     "reconsider your shape, do not bypass."),
    ("empire-doctrine/X-total-capture",
     "uncaptured, total capture, durable storage",
     "§X total capture: every directive -> durable storage (todo/backlog/memory); "
     "metaphors verbatim; nothing discussed but uncaptured. Memory is two-tier (120%.md §9)."),
    ("empire-doctrine/II-120pct",
     "120% enforceable, defense in depth, deterministic",
     "§II 120% correct + enforceable + deterministic: words alone are not law — only "
     "audit/gate/schema makes doctrine binding. Enforcement: 120%.md §2/§15A/§23."),
    ("empire-doctrine/VI-appreciation-critique",
     "rubber-stamp, rubber stamp, substantiated approval",
     "§VI appreciation is critique: silent approval is failure; succeed at a concrete "
     "flaw / alternative / risk, or log unease."),
    ("empire-doctrine/XIII-overrides",
     "kill_switch, free reign, override",
     "§XIII operator overrides are signals not shortcuts: use the override for THE work "
     "AND report the gap that made it necessary. Override-as-routine undoes the kingdom."),
    ("empire-doctrine/VII-kings-word",
     "rogue, obedience after, after rendering",
     "§VII the king's word is final: before rendering, counsel welcomed; after rendering, "
     "obedience; disobedience after correction = rogue."),
)


def seed_doctrine_global_law() -> int:
    """Upsert the empire-doctrine lean-pointer rows into the global LAW store.
    Idempotent (upsert by law_id). Returns the count seeded."""
    from .global_law_store import upsert_global_law

    n = 0
    for law_id, keywords, gist in DOCTRINE_GLOBAL_LAW:
        upsert_global_law(
            law_id=law_id,
            kind="doctrine",
            content=f"empire-doctrine {gist} {_LOAD}",
            keywords=keywords,
            sovereign_owner="operator",
            source="doctrine_global_law_seed",
        )
        n += 1
    return n


def ensure_doctrine_global_law() -> int:
    """Idempotent bootstrap hook (#231): seed the doctrine global-law rows only
    when the global tier is missing them — a fresh install or a wiped empire
    self-heals; an already-seeded empire pays one cheap existence check and
    seeds nothing. Returns the count seeded (0 when already present)."""
    from .global_law_store import read_global_law

    first_law_id = DOCTRINE_GLOBAL_LAW[0][0]
    if read_global_law(first_law_id) is not None:
        return 0
    return seed_doctrine_global_law()
