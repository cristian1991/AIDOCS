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
import re

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
#
# Doctrine split 2026-09: ids are STABLE and numeral-free; each gist opens with
# ``[<article title>]`` naming the empire-doctrine heading it points at. Pointers
# bind by id/title, never by ordinal — a reorder or renumber of the scroll
# changes nothing here (tests/memory/test_doctrine_global_law_stable_ids.py).
DOCTRINE_GLOBAL_LAW: tuple[tuple[str, str, str], ...] = (
    ("empire-doctrine/migrate-without-orphaning",
     "migrate, migration, half-migration, orphaning, rename, move the source",
     ("[Migrate without orphaning] copy first -> update the discovery surface -> "
     "verify end-to-end -> delete source -> update defensive markers -> focused tests. "
     "Never source-deleted/destination-unfound.")),
    ("empire-doctrine/kind-law",
     "tyrant, hard removal, mercy, kind law",
     ("[Operator authority] the law is kind: honest mistakes -> mercy + correction; "
     "hard removal is the last resort, never the first.")),
    ("empire-doctrine/friction",
     "third refusal, gate refuses, workaround, friction",
     ("[Friction is the kingdom speaking] the third refusal outweighs the first — "
     "reconsider your shape, do not bypass.")),
    ("empire-doctrine/total-capture",
     "uncaptured, total capture, durable storage",
     ("[Total capture and the backlog ledger] every directive -> durable storage "
     "(todo/backlog/memory); metaphors verbatim; nothing discussed but uncaptured. "
     "Memory is two-tier (120%.md §9).")),
    ("empire-doctrine/120pct-enforceable",
     "120% enforceable, defense in depth, deterministic",
     ("[120% correct, enforceable, deterministic] words alone are not law — only "
     "audit/gate/schema makes doctrine binding. Enforcement: 120%.md §2/§15A/§23.")),
    ("empire-doctrine/appreciation-is-critique",
     "rubber-stamp, rubber stamp, substantiated approval",
     ("[Bounded co-deliberation] appreciation is critique: silent approval is failure; "
     "succeed at a concrete flaw / alternative / risk, or log unease.")),
    ("empire-doctrine/overrides-are-signals",
     "kill_switch, free reign, override",
     ("[Operator authority] overrides are signals not shortcuts: use the override for "
     "THE work AND report the gap that made it necessary. Override-as-routine undoes "
     "the kingdom.")),
    ("empire-doctrine/empire-word-final",
     "rogue, obedience after, after rendering",
     ("[Operator authority] the Empire's word is final: before rendering, counsel "
     "welcomed; after rendering, obedience; disobedience after correction = rogue.")),
)

#: THE one canonical legacy alias map (old numeral-keyed id -> stable id).
#: global_law_store.read_global_law consults it, so reads by old law_id and
#: ``global:<old id>`` memory paths keep resolving to the new row.
LEGACY_LAW_ID_ALIASES: dict[str, str] = {
    "empire-doctrine/XII-migrate": "empire-doctrine/migrate-without-orphaning",
    "empire-doctrine/VIII-kind-law": "empire-doctrine/kind-law",
    "empire-doctrine/XIV-friction": "empire-doctrine/friction",
    "empire-doctrine/X-total-capture": "empire-doctrine/total-capture",
    "empire-doctrine/II-120pct": "empire-doctrine/120pct-enforceable",
    "empire-doctrine/VI-appreciation-critique": "empire-doctrine/appreciation-is-critique",
    "empire-doctrine/XIII-overrides": "empire-doctrine/overrides-are-signals",
    "empire-doctrine/VII-kings-word": "empire-doctrine/empire-word-final",
}

_TITLE_RE = re.compile(r"^\[([^\]]+)\]")
_HEADING_RE = re.compile(r"^## ([IVXLC]+)\. (.+?)\s*$", re.M)


def canonical_law_id(law_id: str) -> str:
    """Map a legacy numeral id to its stable id; anything else is returned as-is."""
    lid = (law_id or "").strip()
    return LEGACY_LAW_ID_ALIASES.get(lid, lid)


def article_title(gist: str) -> str | None:
    """The empire-doctrine article title a gist names (``[Title] ...``)."""
    m = _TITLE_RE.match(gist or "")
    return m.group(1).strip() if m else None


def resolve_article(law_id: str, scroll_text: str) -> dict | None:
    """Resolve a pointer to its article in ``scroll_text`` BY TITLE (never by
    ordinal). Returns {title, numeral, body} or None when the title is absent
    — an unknown is refused, never guessed."""
    lid = canonical_law_id(law_id)
    gist = next((g for i, _k, g in DOCTRINE_GLOBAL_LAW if i == lid), None)
    title = article_title(gist or "")
    if not title:
        return None
    heads = list(_HEADING_RE.finditer(scroll_text or ""))
    for idx, m in enumerate(heads):
        if m.group(2) != title:
            continue
        start = m.end()
        nxt = re.search(r"^## ", scroll_text[start:], re.M)
        end = start + nxt.start() if nxt else len(scroll_text)
        return {"title": title, "numeral": m.group(1), "body": scroll_text[start:end]}
    return None


def legacy_operator_rows() -> list[dict]:
    """Operator-owned rows still held under a LEGACY id. The seed never
    rewrites or retires them; this is the queryable report so the operator
    decides (each row carries the ``canonical_law_id`` it would move to)."""
    from .global_law_store import read_global_law

    out: list[dict] = []
    for old, new in LEGACY_LAW_ID_ALIASES.items():
        row = read_global_law(old, include_retired=True, follow_alias=False)
        if row is None or row.get("source") == _SEED_SOURCE:
            continue
        out.append({**row, "canonical_law_id": new})
    return out


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


def _migrate_legacy_ids() -> int:
    """Retire SEED-OWNED active rows under legacy ids (soft — history kept).
    Operator-owned legacy rows are never touched: logged once, and listed by
    legacy_operator_rows(). Returns the count retired."""
    from .global_law_store import _empire_db, read_global_law, retire_global_law

    retired = 0
    for old in LEGACY_LAW_ID_ALIASES:
        row = read_global_law(old, include_retired=True, follow_alias=False)
        if row is None:
            continue
        if row.get("source") == _SEED_SOURCE:
            if row.get("status") == "active" and retire_global_law(old):
                retired += 1
            continue
        key = (str(_empire_db()), old)
        if key not in _ENSURE_SKIP_LOGGED:
            _ENSURE_SKIP_LOGGED.add(key)
            _log.warning(
                "doctrine global-law seed: legacy id %s is operator-owned "
                "(source=%r) — NOT migrated to %s; operator decides "
                "(see legacy_operator_rows())",
                old, row.get("source"), LEGACY_LAW_ID_ALIASES[old],
            )
    return retired


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
    Legacy numeral ids (doctrine split): seed-owned rows are retired; operator
    rows are reported, never touched (_migrate_legacy_ids).
    Returns the count of rows written or retired (0 when everything matches)."""
    from .global_law_store import _empire_db, read_global_law

    written = _migrate_legacy_ids()
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
