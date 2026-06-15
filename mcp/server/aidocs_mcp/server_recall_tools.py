"""RFC-4 Phase C — clustered ai_recall MCP tool.

Replaces the agent's habit of choosing between ai_find (code) and
ai_palace_search (conversation memory) with ONE call that:

  1. Calls AIDOCS structural lookup (symbols + outline + text)
  2. For each structural hit, looks up palace anchors by unit_id
  3. Calls hub.palace.search() for unanchored semantic candidates
  4. Calls KG for triples involving the unit_id set
  5. CLUSTERS by unit_id — each cluster = code symbol + anchored
     drawers + KG triples about it
  6. Returns clusters with explicit ``why_this_cluster`` reasons +
     semantic_only_hits separately

Fan-out is an implementation primitive here, NOT the architecture
(per RFC-4 §2). Agents receive a clustered shape, not a merged
ranked list.

Adopt by copying to ``aidocs_mcp/server_recall_tools.py``. Wire via
``create_server()`` after the other registrar calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# tool_display.renders_as is an AIDOCS-internal decorator. When this
# module is imported from MemPalace's tree for standalone testing,
# the AIDOCS-side module isn't present — fall back to a no-op so
# the rest of the file is importable.
try:
    from .tool_display import renders_as  # type: ignore[import-not-found]
except ImportError:

    def renders_as(*args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator


def register_recall_tools(*, server: Any, hub: Any, runtime: Any) -> None:
    """Register the ai_recall tool on the FastMCP server.

    Skipped automatically when hub.palace is None (mempalace not
    installed). Also skipped if hub.code_index.vendor is missing
    (Phase 2 wiring not yet adopted on AIDOCS side).
    """
    if getattr(hub, "palace", None) is None:
        return

    from .palace_hub_extension import build_palace_context

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Clustered Recall",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("find", title="recall clusters")
    def ai_recall(
        query: str,
        limit: int = 5,
        include_semantic_only: bool = True,
        smoke: bool = False,
    ) -> Any:
        """Unified recall over code + palace + KG, clustered by unit identity.

        Returns:
          {
            "clusters": [...],          # code + anchored drawers + KG facts
            "semantic_only_hits": [...], # palace hits with no structural anchor
            "prefer_authority": "ai_find" | None,
          }

        Per RFC-4 §11.1, exact-beats-semantic: clusters sort before
        semantic_only_hits. Snippets only — full drawer content
        requires internal hub.palace.get_drawer call (RFC-4 §8.4).

        """
        ctx = build_palace_context(hub, runtime, tool_name="ai_recall")
        if smoke:
            limit = 1
            include_semantic_only = False
        result = run_clustered_recall(
            query=query,
            limit=limit,
            include_semantic_only=include_semantic_only,
            hub=hub,
            hub_ctx=ctx,
        )
        return _result_to_dict(result, smoke=smoke)


# ---------------------------------------------------------------------------
# Cluster types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterCodeRef:
    unit_id: str
    kind: str
    symbol: str
    source_file: str
    line_start: int
    line_end: int
    snippet: str = ""


@dataclass(frozen=True)
class ClusterAnchoredDrawer:
    drawer_id: str
    snippet: str
    confidence: str
    anchor_id: int


@dataclass(frozen=True)
class ClusterKGTriple:
    subject: str
    predicate: str
    object: str
    valid_from: str | None = None
    valid_to: str | None = None
    source_drawer_id: str | None = None


@dataclass(frozen=True)
class RecallCluster:
    unit_id: str
    code: ClusterCodeRef
    anchored_memory: tuple  # tuple of ClusterAnchoredDrawer
    kg_facts: tuple  # tuple of ClusterKGTriple
    score: float
    why_this_cluster: (
        tuple  # tuple of str: exact_symbol_match / anchored_drawer / kg_triple / file_path_match
    )


@dataclass(frozen=True)
class SemanticOnlyHit:
    drawer_id: str
    snippet: str
    wing: str
    source_file: str
    why: str = "semantic_only_fallback"


@dataclass(frozen=True)
class ClusteredRecallResult:
    clusters: tuple
    semantic_only_hits: tuple
    prefer_authority: str | None = None


# ---------------------------------------------------------------------------
# The clustering implementation
# ---------------------------------------------------------------------------


# Symbol-shaped query → suggests prefer_authority="ai_find" per RFC-4 §11.1
_SYMBOL_QUERY_RE = re.compile(r"^[A-Z_][\w]*(\.[\w]+)*\s*$")


def run_clustered_recall(
    *,
    query: str,
    limit: int,
    include_semantic_only: bool,
    hub: Any,
    hub_ctx: Any,
) -> ClusteredRecallResult:
    """Core clustering logic. Fan-out as primitive, cluster as result.

    Each step is best-effort: missing optional services (code_index
    not yet wired, KG empty) just yield empty contributions to the
    clusters. The shape stays consistent.
    """
    clusters_by_unit: dict[str, dict] = {}
    semantic_only: list[SemanticOnlyHit] = []
    prefer_authority: str | None = None

    # Step 0: resolve code-first intent from the code-index-derived
    # vocabulary. Used to (a) downrank cross-domain semantic hits and
    # (b) widen structural lookup with candidate symbols. Best-effort:
    # missing code_index → low-confidence intent, recall behaves as
    # before.
    intent = _resolve_intent_from_hub(hub=hub, query=query)

    # Step 1: structural symbol/outline lookup via hub.code_index (if wired)
    code_hits = _safe_code_lookup(hub=hub, hub_ctx=hub_ctx, query=query, limit=limit)
    # If the raw query produced nothing but intent surfaced candidate
    # symbols (e.g. "fix pdf render for documents" → DocumentPdfRenderer),
    # retry the structural lookup against those candidate symbols.
    if not code_hits and intent.candidate_symbols:
        seen_unit_ids: set[str] = set()
        for sym in intent.candidate_symbols[:5]:
            extra = _safe_code_lookup(
                hub=hub,
                hub_ctx=hub_ctx,
                query=sym,
                limit=limit,
            )
            for h in extra:
                if h.unit_id in seen_unit_ids:
                    continue
                seen_unit_ids.add(h.unit_id)
                code_hits.append(h)
                if len(code_hits) >= limit:
                    break
            if len(code_hits) >= limit:
                break
    for code_ref in code_hits:
        clusters_by_unit.setdefault(
            code_ref.unit_id,
            {
                "unit_id": code_ref.unit_id,
                "code": code_ref,
                "anchored": [],
                "kg": [],
                "reasons": ["exact_symbol_match"],
            },
        )

    # Step 2: anchored drawers for each unit (via anchor_store)
    anchor_store = _safe_get_anchor_store(hub)
    palace_service = getattr(hub, "palace", None)
    for unit_id, cluster in clusters_by_unit.items():
        if anchor_store is None:
            break
        try:
            code_ref: ClusterCodeRef = cluster["code"]
            anchors = anchor_store.list_anchors_for_unit(
                symbol_name=code_ref.symbol,
                file_path=code_ref.source_file,
            )
        except Exception:
            anchors = []

        for a in anchors:
            if not a.drawer_id:
                continue
            # Hydrate drawer summary
            summary = ""
            if palace_service is not None:
                try:
                    s = palace_service.get_drawer_summary(
                        drawer_id=a.drawer_id,
                        hub_ctx=hub_ctx,
                    )
                    if isinstance(s, dict):
                        summary = s.get("snippet", "")
                except Exception:
                    pass
            cluster["anchored"].append(
                ClusterAnchoredDrawer(
                    drawer_id=a.drawer_id,
                    snippet=summary[:200],
                    confidence=a.confidence,
                    anchor_id=a.anchor_id,
                ),
            )
        if cluster["anchored"] and "anchored_drawer" not in cluster["reasons"]:
            cluster["reasons"].append("anchored_drawer")

    # Step 3: KG triples involving any unit in the cluster set
    kg = getattr(hub, "kg", None) or getattr(getattr(hub, "palace", None), "kg", None)
    for unit_id, cluster in clusters_by_unit.items():
        if kg is None:
            break
        code_ref: ClusterCodeRef = cluster["code"]
        try:
            triples = kg.query_entity(code_ref.symbol)
        except Exception:
            triples = []
        for t in (triples or [])[:5]:
            cluster["kg"].append(
                ClusterKGTriple(
                    subject=(t.get("subject") if isinstance(t, dict) else "") or "",
                    predicate=(t.get("predicate") if isinstance(t, dict) else "") or "",
                    object=(t.get("object") if isinstance(t, dict) else "") or "",
                    valid_from=t.get("valid_from") if isinstance(t, dict) else None,
                    valid_to=t.get("valid_to") if isinstance(t, dict) else None,
                    source_drawer_id=t.get("source_drawer_id") if isinstance(t, dict) else None,
                ),
            )
        if cluster["kg"] and "kg_triple" not in cluster["reasons"]:
            cluster["reasons"].append("kg_triple")

    # Step 4: palace semantic search for unanchored candidates.
    # When intent is high-confidence and we have structural candidates,
    # constrain the palace search at the service boundary (true
    # constraint, not caller post-filter). If the constrained search
    # comes back empty, fall back to a broad search labeled
    # ``semantic_only_broad_fallback`` so the agent can tell which
    # path produced each hit.
    if palace_service is not None and include_semantic_only:
        try:
            from mempalace.conjoined_types import SearchResult

            restrict_sources: set[str] | None = None
            restrict_uids: set[str] | None = None
            if intent.confidence == "high":
                cand_sources: set[str] = set()
                cand_uids: set[str] = set()
                for ref in code_hits:
                    if ref.source_file:
                        cand_sources.add(ref.source_file)
                    if ref.unit_id:
                        cand_uids.add(ref.unit_id)
                cand_sources.update(intent.candidate_paths)
                if cand_sources:
                    restrict_sources = cand_sources
                if cand_uids:
                    restrict_uids = cand_uids

            used_broad_fallback = False
            search_result = palace_service.search(
                query=query,
                limit=limit,
                hub_ctx=hub_ctx,
                restrict_source_files=restrict_sources,
                restrict_unit_ids=restrict_uids,
            )
            if (
                isinstance(search_result, SearchResult)
                and not search_result.hits
                and (restrict_sources or restrict_uids)
            ):
                # Constrained search yielded nothing — fall back to
                # broad search. Under-recall is the worse failure.
                used_broad_fallback = True
                search_result = palace_service.search(
                    query=query,
                    limit=limit,
                    hub_ctx=hub_ctx,
                )

            if isinstance(search_result, SearchResult):
                anchored_drawer_ids = {
                    d.drawer_id
                    for cluster in clusters_by_unit.values()
                    for d in cluster["anchored"]
                }
                raw_semantic: list = []
                for hit in search_result.hits:
                    if hit.drawer_id in anchored_drawer_ids:
                        continue
                    if hit.unit_id and hit.unit_id in clusters_by_unit:
                        continue
                    raw_semantic.append(hit)
                # AIDOCS-installed source prefers the local recall_intent
                # module; aidocs_integration is the standalone-test
                # fallback only.
                try:
                    from .recall_intent import downrank_by_intent  # type: ignore[import-not-found]
                except ImportError:
                    try:
                        from aidocs_integration.recall_intent import downrank_by_intent
                    except ImportError:
                        downrank_by_intent = None  # type: ignore[assignment]
                if downrank_by_intent is not None and (
                    intent.domain_terms or intent.forbidden_or_conflicting_artifacts
                ):
                    raw_semantic = downrank_by_intent(raw_semantic, intent)
                # Label each hit by which path produced it.
                if used_broad_fallback:
                    why_label = "semantic_only_broad_fallback"
                elif restrict_sources or restrict_uids:
                    why_label = "semantic_only_constrained"
                else:
                    why_label = "semantic_only_fallback"
                for hit in raw_semantic:
                    semantic_only.append(
                        SemanticOnlyHit(
                            drawer_id=hit.drawer_id,
                            snippet=hit.snippet,
                            wing=hit.wing,
                            source_file=hit.source_file,
                            why=why_label,
                        ),
                    )
        except Exception:
            pass

    # Step 5: assemble RecallClusters with scores
    finalized: list[RecallCluster] = []
    for unit_id, cluster in clusters_by_unit.items():
        # Score: structural matches start at 1.0, weighted by anchored
        # drawer count + KG triple count.
        score = 1.0
        score += 0.2 * len(cluster["anchored"])
        score += 0.1 * len(cluster["kg"])
        finalized.append(
            RecallCluster(
                unit_id=unit_id,
                code=cluster["code"],
                anchored_memory=tuple(cluster["anchored"]),
                kg_facts=tuple(cluster["kg"]),
                score=score,
                why_this_cluster=tuple(cluster["reasons"]),
            ),
        )

    # Sort clusters by score descending
    finalized.sort(key=lambda c: -c.score)

    # prefer_authority hint when query looks symbol-shaped AND we have clusters
    if finalized and _SYMBOL_QUERY_RE.match(query.strip()):
        prefer_authority = "ai_find"
    elif finalized:
        # Always hint if we have structural matches — they outrank semantic
        prefer_authority = "ai_find"

    return ClusteredRecallResult(
        clusters=tuple(finalized[:limit]),
        semantic_only_hits=tuple(semantic_only[:limit]),
        prefer_authority=prefer_authority,
    )


# ---------------------------------------------------------------------------
# Safe lookups (degrade gracefully when subsystems not wired)
# ---------------------------------------------------------------------------


def _safe_code_lookup(*, hub, hub_ctx, query, limit) -> list:
    """Look up code symbols via hub.code_index. Returns ClusterCodeRef list.

    Multiple paths tried in priority order:
      1. hub.code_index.find_symbols (preferred — exact symbol match)
      2. hub.code_index.vendor.iter_units + filter by query (fallback)
      3. empty list if neither available
    """
    code_index = getattr(hub, "code_index", None)
    if code_index is None:
        return []

    # Path 1: native find_symbols
    finder = getattr(code_index, "find_symbols", None)
    if finder is not None:
        try:
            hits = finder(query=query, limit=limit)
        except Exception:
            hits = []
        return [_normalize_code_hit(h) for h in (hits or []) if h]

    # Path 2: iter_units + name match
    vendor = getattr(code_index, "vendor", None)
    if vendor is not None:
        try:
            query_lower = query.lower().strip()
            results = []
            for unit in vendor.iter_units():
                if not query_lower:
                    continue
                # match query against symbol name OR file path
                symbol_blob = (" ".join(unit.parent_chain) + " " + unit.kind).lower()
                if query_lower in symbol_blob or query_lower in unit.source_file.lower():
                    results.append(
                        ClusterCodeRef(
                            unit_id=unit.unit_id,
                            kind=unit.kind,
                            symbol=".".join(unit.parent_chain + (unit.kind,)),
                            source_file=unit.source_file,
                            line_start=unit.line_start,
                            line_end=unit.line_end,
                            snippet="",
                        ),
                    )
                    if len(results) >= limit:
                        break
            return results
        except Exception:
            return []
    return []


def _normalize_code_hit(hit: Any) -> ClusterCodeRef:
    """Coerce a code-index hit (dict or object) into ClusterCodeRef."""
    if isinstance(hit, ClusterCodeRef):
        return hit
    if isinstance(hit, dict):
        return ClusterCodeRef(
            unit_id=hit.get("unit_id", "") or "",
            kind=hit.get("kind", "") or "",
            symbol=hit.get("symbol", "") or "",
            source_file=hit.get("source_file", "") or hit.get("path", "") or "",
            line_start=int(hit.get("line_start", 0) or 0),
            line_end=int(hit.get("line_end", 0) or 0),
            snippet=hit.get("snippet", "") or "",
        )
    # Object with attrs
    return ClusterCodeRef(
        unit_id=getattr(hit, "unit_id", "") or "",
        kind=getattr(hit, "kind", "") or "",
        symbol=getattr(hit, "symbol", "") or "",
        source_file=getattr(hit, "source_file", "") or getattr(hit, "path", "") or "",
        line_start=int(getattr(hit, "line_start", 0) or 0),
        line_end=int(getattr(hit, "line_end", 0) or 0),
        snippet=getattr(hit, "snippet", "") or "",
    )


def _safe_get_anchor_store(hub):
    """Return AnchorStore from hub.memory.anchors, or None if not wired."""
    memory = getattr(hub, "memory", None)
    if memory is None:
        return None
    return getattr(memory, "anchors", None)


def _resolve_intent_from_hub(*, hub: Any, query: str):
    """Build a RecallIntent from the project's code-index-derived
    vocabulary. Degrades to a zero-vocabulary intent when hub lacks
    a code_index — recall still works, just without downrank.

    Tests can monkeypatch this function (e.g.
    ``monkeypatch.setattr(server_recall_tools, "_resolve_intent_from_hub", fake)``)
    to inject a pre-built intent without standing up a fake code_index.
    """
    # AIDOCS-installed prefers local .recall_intent; aidocs_integration
    # is the standalone-test fallback.
    try:
        from .recall_intent import (  # type: ignore[import-not-found]
            build_vocabulary,
            resolve_recall_intent,
        )
    except ImportError:
        try:
            from aidocs_integration.recall_intent import (
                build_vocabulary,
                resolve_recall_intent,
            )
        except ImportError:
            # Fall back to a minimal stub matching RecallIntent shape.
            class _StubIntent:
                raw_query = query
                action = None
                domain_terms: tuple = ()
                operation_terms: tuple = ()
                target_artifact_terms: tuple = ()
                forbidden_or_conflicting_artifacts: tuple = ()
                candidate_paths: tuple = ()
                candidate_symbols: tuple = ()
                confidence = "low"

            return _StubIntent()

    code_index = getattr(hub, "code_index", None)
    vendor = getattr(code_index, "vendor", None) if code_index is not None else None
    paths: list[str] = []
    symbols: list[tuple[str, str]] = []
    if vendor is not None:
        try:
            count = 0
            for unit in vendor.iter_units():
                count += 1
                if count > 5000:  # bounded scan for resolver budget
                    break
                source_file = getattr(unit, "source_file", "") or ""
                if source_file:
                    paths.append(source_file)
                # symbol = parent_chain joined + kind
                parent_chain = getattr(unit, "parent_chain", ()) or ()
                kind = getattr(unit, "kind", "") or ""
                if parent_chain or kind:
                    sym = ".".join(list(parent_chain) + ([kind] if kind else []))
                    if sym and source_file:
                        symbols.append((sym, source_file))
        except Exception:
            pass

    vocab = build_vocabulary(paths=paths, symbols=symbols)
    return resolve_recall_intent(query, vocabulary=vocab)


# ---------------------------------------------------------------------------
# Result → dict for MCP transport
# ---------------------------------------------------------------------------


def _trim(s: str, n: int = 80) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n] + "…"


def _result_to_dict(result: ClusteredRecallResult, smoke: bool = False) -> dict:
    if smoke:
        # Bounded payload: 1 cluster, trimmed snippets, no KG, no anchored bodies.
        # Used for transport-path bell tests where payload size must not
        # be the suspect variable.
        c = result.clusters[0] if result.clusters else None
        return {
            "smoke": True,
            "cluster_count": len(result.clusters),
            "top": None
            if c is None
            else {
                "unit_id": c.unit_id,
                "symbol": c.code.symbol,
                "source_file": c.code.source_file,
                "anchored_count": len(c.anchored_memory),
                "why": list(c.why_this_cluster),
                "snippet": _trim(c.code.snippet),
            },
            "prefer_authority": result.prefer_authority,
        }
    return {
        "clusters": [
            {
                "unit_id": c.unit_id,
                "code": {
                    "unit_id": c.code.unit_id,
                    "kind": c.code.kind,
                    "symbol": c.code.symbol,
                    "source_file": c.code.source_file,
                    "line_start": c.code.line_start,
                    "line_end": c.code.line_end,
                    "snippet": c.code.snippet,
                },
                "anchored_memory": [
                    {
                        "drawer_id": d.drawer_id,
                        "snippet": d.snippet,
                        "confidence": d.confidence,
                        "anchor_id": d.anchor_id,
                    }
                    for d in c.anchored_memory
                ],
                "kg_facts": [
                    {
                        "subject": k.subject,
                        "predicate": k.predicate,
                        "object": k.object,
                        "valid_from": k.valid_from,
                        "valid_to": k.valid_to,
                        "source_drawer_id": k.source_drawer_id,
                    }
                    for k in c.kg_facts
                ],
                "score": c.score,
                "why_this_cluster": list(c.why_this_cluster),
            }
            for c in result.clusters
        ],
        "semantic_only_hits": [
            {
                "drawer_id": h.drawer_id,
                "snippet": h.snippet,
                "wing": h.wing,
                "source_file": h.source_file,
                "why": h.why,
            }
            for h in result.semantic_only_hits
        ],
        "prefer_authority": result.prefer_authority,
    }
