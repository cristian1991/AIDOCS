"""Memory-war unification (operator 2026-07-16): the ONE retrieval orchestrator.

Before this module there were four overlapping partial fusions of the same
memory kg<>index<>semantic logic, each consumer reaching memory through a
different subset, so results disagreed depending on the entry path:

  A discover_relevant_memory         keyword+law+semantic   (UPS surfacer)
  B _attach_knowledge_pointers       symbol-index+kg        (code-index tools)
  C run_clustered_recall             index+kg+semantic      (ai_recall)
  D memory_search / ai_palace_search keyword-only / semantic-only

This module is the single orchestration seam. It adds NO new retrieval
logic — every lane below is a pre-existing function, composed here and
NOWHERE else:

  law+route keyword  memory_discovery.discover_relevant_memory
  content keyword    server_memory_index_tools.merged_memory_search
  code+kg+semantic   server_recall_tools.run_clustered_recall
  kg-by-entity       memory_kg_extractor.kg_neighbors_for_query

Projections (one per consumer shape):
  unified_clustered_recall    -> ai_recall           (clusters; alias of C)
  unified_memory_search       -> ai_memory(search)   (memory-first row list)
  unified_memory_hints        -> UPS surfacer        (MemoryHint list)
  unified_knowledge_pointers  -> code-index rail     (terse pointers, cap 3)

All lane imports are lazy: they keep the module import-cycle-free
(server_memory_index_tools imports this module) and preserve the existing
monkeypatch seams (tests patch memory_discovery.discover_relevant_memory
et al. at their home modules).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Every lane degrades to [] on error: retrieval must never take a read tool
# down. Same doctrine as the lanes' original call sites.

# ---------------------------------------------------------------------------
# clustered projection — ai_recall (alias, pinned by test_unified_recall_core)
# ---------------------------------------------------------------------------

from .server_recall_tools import run_clustered_recall as unified_clustered_recall

__all__ = [
    "unified_clustered_recall",
    "unified_knowledge_pointers",
    "unified_memory_hints",
    "unified_memory_search",
]

# ---------------------------------------------------------------------------
# hint projection — the UPS surfacer lane
# ---------------------------------------------------------------------------


def unified_memory_hints(
    prompt: str,
    project_root: Path,
    *,
    action_kind: str | None = None,
    max_hints: int | None = None,
    palace: object | None = None,
    hub_ctx: object | None = None,
) -> list:
    """MemoryHint list for prompt/read-time surfacing (law > explicit >
    derived > semantic). Thin seam over discover_relevant_memory so every
    consumer imports retrieval from ONE module."""
    from . import memory_discovery as _md

    kwargs: dict[str, Any] = {
        "action_kind": action_kind,
        "palace": palace,
        "hub_ctx": hub_ctx,
    }
    if max_hints is not None:
        kwargs["max_hints"] = max_hints
    try:
        return _md.discover_relevant_memory(prompt, project_root, **kwargs)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# pointer projection — the code-index decoration rail
# ---------------------------------------------------------------------------


def unified_knowledge_pointers(
    *,
    query: str,
    project_root: Path,
    contexts: list[tuple[str, str]],
    palace: object | None = None,
    max_pointers: int = 3,
) -> list[dict[str, str]]:
    """Terse high-confidence memory/KG pointers for a code-discovery result.

    Candidate gathering moved here VERBATIM from
    read_memory_surfacer._attach_knowledge_pointers (symbol-anchored memory
    hints + KG neighbors resolved to canonical memory routes); the surfacer
    keeps only result attachment. Only pointer metadata crosses this
    boundary — never a memory body. Uncertainty, privacy findings, and
    sovereign paths drop silently.
    """
    from .memory_discovery import (
        canonical_memory_pointer_path,
        discover_memory_for_symbol,
    )
    from .output_guard import scan_text

    candidates: list[dict[str, str]] = []
    for path, symbol in contexts:
        try:
            hints = discover_memory_for_symbol(
                project_root,
                symbol,
                path,
                max_hints=6,
                max_hops=1,
            )
        except Exception:
            hints = []
        for hint in hints:
            confidence = str(getattr(hint, "confidence", "") or "")
            if confidence not in {"operator_pinned", "exact_symbol", "file_anchor"}:
                continue
            candidates.append(
                {
                    "path": str(getattr(hint, "path", "") or ""),
                    "why": str(getattr(hint, "why", "") or ""),
                    "edge": str(
                        getattr(hint, "edge", "")
                        or (
                            "code:import"
                            if getattr(hint, "hop_depth", 0)
                            else "anchor:file"
                        )
                    ),
                }
            )

    if palace is not None:
        try:
            from .memory_kg_extractor import kg_neighbors_for_query
            from .memory_sqlite_store import read_entry

            scope_query = " ".join(
                [str(query or ""), *(p for p, _ in contexts), *(s for _, s in contexts if s)]
            )[:2000]
            for fact in kg_neighbors_for_query(palace, scope_query, limit=12):
                try:
                    if float(fact.get("confidence", 0.0) or 0.0) < 0.85:
                        continue
                except (TypeError, ValueError):
                    continue
                unit = next(
                    (
                        str(fact.get(side) or "")
                        for side in ("subject", "object")
                        if str(fact.get(side) or "").startswith("memory:")
                    ),
                    "",
                )
                mem_path = canonical_memory_pointer_path(
                    unit[len("memory:") :] if unit else ""
                )
                if not mem_path:
                    continue
                entry = read_entry(project_root, mem_path)
                if entry is None:
                    continue
                why = str(entry.title or "") or mem_path.rsplit("/", 1)[-1].removesuffix(
                    ".md"
                )
                candidates.append(
                    {
                        "path": mem_path,
                        "why": why,
                        "edge": "kg:" + str(fact.get("predicate") or "related"),
                    }
                )
        except Exception:
            pass

    pointers: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for item in candidates:
        path = canonical_memory_pointer_path(item["path"])
        why = item["why"].strip()[:120]
        edge = item["edge"].strip()[:80]
        if not path or path in seen_paths or not why or not edge:
            continue
        try:
            if not scan_text(f"{path}\n{why}\n{edge}", redact=True).clean:
                continue
        except Exception:
            continue
        seen_paths.add(path)
        pointers.append(
            {
                "path": path,
                "why": why,
                "edge": edge,
                "tier": "evidence",
                "confidence": "high",
            }
        )
        if len(pointers) >= max_pointers:
            break
    return pointers


# ---------------------------------------------------------------------------
# search projection — ai_memory(search) / memory_search
# ---------------------------------------------------------------------------


def _normalise_semantic_source(raw: Any, project_root: Path) -> str:
    """Project-relative canonical pointer path for a palace hit's source_file.

    The palace stores ``source_file`` as whatever the ingest handed it —
    frequently an ABSOLUTE path — but every agent-facing read resolves
    project-relative canonical paths, so an absolute path is a handle the
    surfacing tool itself cannot dereference. Relativise against the project
    root FIRST, then hand the result to ``canonical_memory_pointer_path`` so
    its traversal / absolute / sovereign suppression still applies — this
    normalisation must never route around that boundary.

    Returns "" when no safe relative path can be derived.
    """
    from .memory_discovery import canonical_memory_pointer_path

    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    root = str(project_root).replace("\\", "/").rstrip("/")
    if root and text.casefold().startswith(root.casefold() + "/"):
        text = text[len(root) + 1 :]
    return canonical_memory_pointer_path(text)


def unified_memory_search(
    hub: Any,
    project_root: Path,
    query: str,
    limit: int = 10,
    *,
    runtime: Any = None,
) -> list[dict[str, Any]]:
    """Memory-first merged search: every lane, one ranked row list.

    Row order (exact-beats-semantic, RFC-4 §11.1):
      1. lane="exact"    content-keyword rows + scroll federation (leads)
      2. lane="route"    law/route-keyword + semantic MemoryHints (discover)
      3. lane="kg"       KG facts resolving to a canonical memory route
      4. lane="semantic" palace drawers with no structural anchor

    Non-exact lanes get a small guaranteed reserve (max(1, limit//3), the
    scroll-reserve doctrine) so exact rows can never squeeze them out
    entirely. Dedup is by canonical path / drawer id across lanes.
    """
    query = str(query or "").strip()
    if not query:
        return []
    limit = max(1, int(limit or 10))

    # Lane 1: exact content-keyword rows (+ scroll federation, #341).
    from . import server_memory_index_tools as _smit

    try:
        exact_rows = list(
            _smit.merged_memory_search(hub, project_root, query, limit=limit)
        )
    except Exception:
        exact_rows = []
    for row in exact_rows:
        if isinstance(row, dict):
            row.setdefault("lane", "exact")

    seen: set[str] = set()
    for row in exact_rows:
        if isinstance(row, dict):
            key = str(row.get("path") or row.get("drawer_id") or "")
            if key:
                seen.add(key)

    palace = getattr(hub, "palace", None)
    extra: list[dict[str, Any]] = []

    # Lane 2: law/route-keyword (+ semantic when palace wired) MemoryHints.
    for hint in unified_memory_hints(
        query, project_root, palace=palace, max_hints=max(3, limit // 2)
    ):
        path = str(getattr(hint, "path", "") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        extra.append(
            {
                "path": path,
                "title": str(getattr(hint, "why", "") or ""),
                "lane": "route",
                "severity": str(getattr(hint, "severity", "") or "normal"),
                "confidence": str(getattr(hint, "confidence", "") or ""),
                # #375 Phase 2 (d): lane tier stays legible at the search
                # surface — 'empire' rows come from the machine-global
                # palace, everything else keeps its MemoryHint tier.
                "tier": str(getattr(hint, "tier", "") or "evidence"),
            }
        )

    # Lanes 3+4: the clustered engine (index-hit spine -> anchored -> KG ->
    # palace semantic). hub_ctx is best-effort; the engine degrades per-step.
    if palace is not None:
        hub_ctx = None
        try:
            from .palace_hub_extension import build_palace_context

            hub_ctx = build_palace_context(hub, runtime, tool_name="ai_memory")
        except Exception:
            hub_ctx = None
        try:
            from . import server_recall_tools as _srt
            from .memory_discovery import canonical_memory_pointer_path

            clustered = _srt.run_clustered_recall(
                query=query,
                limit=limit,
                include_semantic_only=True,
                hub=hub,
                hub_ctx=hub_ctx,
            )
            for cluster in clustered.clusters:
                for fact in cluster.kg_facts:
                    unit = next(
                        (
                            str(side or "")
                            for side in (fact.subject, fact.object)
                            if str(side or "").startswith("memory:")
                        ),
                        "",
                    )
                    mem_path = canonical_memory_pointer_path(
                        unit[len("memory:") :] if unit else ""
                    )
                    if not mem_path or mem_path in seen:
                        continue
                    seen.add(mem_path)
                    extra.append(
                        {
                            "path": mem_path,
                            "title": f"{fact.predicate} {cluster.code.symbol}".strip(),
                            "lane": "kg",
                            "anchor_symbol": cluster.code.symbol,
                            "source_file": cluster.code.source_file,
                        }
                    )
            for hit in clustered.semantic_only_hits:
                drawer_id = str(getattr(hit, "drawer_id", "") or "")
                path = _normalise_semantic_source(
                    getattr(hit, "source_file", ""), project_root
                )
                # REGROUP — the read-back half the ingest contract promised
                # but never implemented (memory_sqlite_store.py:1029-1030
                # writes parent_drawer_id/chunk_index "so search results
                # regroup to the source memory"; nothing AIDOCS-side read it).
                # Keying on the normalised SOURCE path collapses N drawers of
                # one file — oversized-memory chunk children
                # `memdrawer:<path>#chunkNNNN` AND duplicate auto-id drawers
                # of the same file — into ONE row, without plumbing new
                # metadata through the SemanticOnlyHit seam. Dedup previously
                # keyed on drawer_id alone, which is why one file surfaced
                # repeatedly.
                # Fall back to drawer_id when no safe path could be derived,
                # so a pathless hit degrades instead of vanishing.
                group_key = path or drawer_id
                if not group_key or group_key in seen:
                    continue
                seen.add(group_key)
                extra.append(
                    {
                        "drawer_id": drawer_id,
                        "path": path,
                        "title": str(getattr(hit, "snippet", "") or "")[:160],
                        "lane": "semantic",
                        "wing": str(getattr(hit, "wing", "") or ""),
                    }
                )
        except Exception:
            pass

    if not extra:
        return exact_rows[:limit]
    # Scroll-reserve doctrine: non-exact lanes keep a small guaranteed slice.
    reserve = min(len(extra), max(1, limit // 3))
    kept = exact_rows[: max(0, limit - reserve)]
    return kept + extra[: limit - len(kept)]
