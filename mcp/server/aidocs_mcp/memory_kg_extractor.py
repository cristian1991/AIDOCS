"""#206 Memory Slice 5 — palace KG population at capture time.

The palace knowledge graph (``entities``/``triples`` in ``<palace>/
kg.sqlite3``, counted as kg_entities/kg_triples on ai_palace_status) had
NO production writer: the schema, ``add_triple`` provenance columns and
the adapter seam all existed, unused. This module is the first writer.

Extraction is DETERMINISTIC (no model, no NLP dependency): the memory
unit itself, its kind, its anchored symbols, mentioned file paths and
backticked identifiers. Substance-level extraction (todo #96) can later
replace ``extract_kg_facts`` without touching the wiring.

Provenance is bilateral per RFC-4 §3 Phase E: every triple carries
``source_drawer_id`` (the palace drawer projecting this memory) and
``source_unit_id`` (the canonical ``memory:<path>`` unit).

Fail-quiet everywhere: KG population is a discoverability nicety layered
on the capture path — it must never fail a capture or an ingest.
"""

from __future__ import annotations

import re

_FILE_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:py|ts|tsx|js|jsx|rs|go|cs|md|toml|yaml|yml|json|sql)\b"
)
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{2,80})`")
_MAX_FACTS = 24
_ADAPTER_NAME = "aidocs_memory_capture"


def extract_kg_facts(
    path: str,
    *,
    kind: str = "",
    content: str = "",
    anchors: list[str] | tuple[str, ...] = (),
) -> list[tuple[str, str, str]]:
    """Deterministic (subject, predicate, object) facts for one memory.

    Subject is always the canonical memory unit (``memory:<path>``).
    Order-stable, deduped, capped at ``_MAX_FACTS``.
    """
    rel = (path or "").replace("\\", "/").lstrip("/")
    if not rel:
        return []
    subject = f"memory:{rel}"
    facts: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(pred: str, obj: str) -> None:
        obj = (obj or "").strip()
        if not obj or len(facts) >= _MAX_FACTS:
            return
        fact = (subject, pred, obj)
        if fact in seen:
            return
        seen.add(fact)
        facts.append(fact)

    if (kind or "").strip():
        _add("is_kind", kind.strip())
    for a in anchors or ():
        _add("anchors_symbol", str(a))
    text = content or ""
    for m in _FILE_RE.finditer(text):
        token = m.group(0).replace("\\", "/")
        # skip the memory's own path — self-mention is noise
        if token != rel:
            _add("mentions_file", token)
    for m in _BACKTICK_RE.finditer(text):
        _add("mentions", m.group(1))
    return facts


def _open_kg(palace_service):
    """Real mempalace KnowledgeGraph via the palace service's kg-path
    seam (PalaceService._resolve_kg_path), or None. Fail-quiet.
    """
    resolver = getattr(palace_service, "_resolve_kg_path", None)
    if resolver is None:
        return None
    try:
        kg_path = resolver()
        from mempalace.knowledge_graph import KnowledgeGraph

        return KnowledgeGraph(str(kg_path))
    except Exception:
        return None


def ingest_kg_for_entry(
    palace_service,
    *,
    path: str,
    kind: str = "",
    content: str = "",
    anchors: list[str] | tuple[str, ...] = (),
) -> int:
    """Write this memory's facts into the palace KG with bilateral
    provenance. Returns triples written (dedup makes re-ingest 0-cost);
    0 on any failure — never raises.
    """
    try:
        facts = extract_kg_facts(path, kind=kind, content=content, anchors=anchors)
        if not facts:
            return 0
        kg = _open_kg(palace_service)
        if kg is None:
            return 0
        from .memory_sqlite_store import memory_drawer_id, memory_unit_id

        rel = (path or "").replace("\\", "/").lstrip("/")
        written = 0
        try:
            for subject, predicate, obj in facts:
                try:
                    kg.add_triple(
                        subject,
                        predicate,
                        obj,
                        confidence=0.9,
                        source_file=rel,
                        source_drawer_id=memory_drawer_id(rel),
                        adapter_name=_ADAPTER_NAME,
                        source_unit_id=memory_unit_id(rel),
                    )
                    written += 1
                except Exception:
                    continue
        finally:
            try:
                kg.close()
            except Exception:
                pass
        return written
    except Exception:
        return 0


def kg_neighbors_for_query(
    palace_service,
    query: str,
    *,
    limit: int = 8,
) -> list[dict]:
    """Read-side surfacing (#206 closing loop): facts touching entities
    named in a search query, so ai_palace_search can attach KG context
    when the graph is populated. [] on any error / empty KG.
    """
    try:
        q = (query or "").strip()
        if not q:
            return []
        kg = _open_kg(palace_service)
        if kg is None:
            return []
        # Candidate entity names: word-ish tokens (identifiers, dotted or
        # slashed names) longer than 3 chars, plus the full query. Capped.
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]{3,120}", q)
        candidates = list(dict.fromkeys([q, *tokens]))[:8]
        out: list[dict] = []
        seen: set[tuple] = set()
        try:
            for name in candidates:
                try:
                    for fact in kg.query_entity(name, direction="both"):
                        key = (
                            fact.get("subject"),
                            fact.get("predicate"),
                            fact.get("object"),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(fact)
                        if len(out) >= limit:
                            return out
                except Exception:
                    continue
        finally:
            try:
                kg.close()
            except Exception:
                pass
        return out
    except Exception:
        return []
