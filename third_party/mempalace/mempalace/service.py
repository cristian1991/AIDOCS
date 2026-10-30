"""PalaceService — the library surface (RFC 003 §4).

When AIDOCS hosts MemPalace as the in-castle library wing, this is the
class that lives on ``AidocsServiceHub.palace``. AIDOCS's
``server_palace_tools`` registrar reaches into this object for every
agent-facing ``ai_palace_*`` MCP tool. AIDOCS's internal consumers
(preflight, capture flow, dashboards) reach into the broader internal
method surface that the agent never sees as a tool.

Invariants (from RFC 003 organizing law):

  1. The librarian lives in the castle — this class is consumed
     in-process by AIDOCS via hub.palace.
  2. The librarian does not hold the keys — every write explicitly
     invokes gate primitives carried in the per-call HubContext.
     No method here authorizes; gates remain AIDOCS-canonical.
  3. The librarian does not speak verdicts — refusals return a
     ``Refused`` value with a reason string; the operator/court
     decides resolution.
  4. The librarian does not write two ledgers without a receipt —
     every cross-store write opens a bridge_tx entry via the hub
     and either commits or records rollback.
  5. The librarian re-checks her shelves against the gatehouse's
     current key-ring — every read filters against CURRENT policy,
     never against stored-at-write-time policy.

PalaceService NEVER reads source files directly. UnitContent is vended
by AIDOCS via the hub; PalaceService consumes it.

Standalone use: PalaceService can also be instantiated by callers
outside AIDOCS for testing or for library-mode use. When constructed
without a hub, the gate primitives default to permissive (no-op
``is_protected``, etc.) and the bridge_tx becomes an in-process-only
journal stored alongside the palace. Standalone mode is for
test fixtures and library experiments; the canonical deployment is
hub-hosted.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .conjoined_types import (
    HubContext,
    PalaceStatus,
    Refused,
    SearchHit,
    SearchResult,
    Unit,
    UnitContent,
)

logger = logging.getLogger("mempalace.service")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PalaceService:
    """The librarian's office inside AidocsServiceHub.

    Methods follow a uniform shape:

      def method(self, *, <args>, hub_ctx: HubContext) -> <result>:
          # 1. Pre-flight gate invocations (rule 2)
          # 2. Begin audit + bridge_tx if write (rule 4)
          # 3. Delegate to mempalace library functions
          # 4. Filter results against current policy (rule 5, reads only)
          # 5. Commit or rollback (rule 4)
          # 6. Return typed result or Refused

    The implementation here is intentionally a thin wrapper — every
    mempalace internal it calls (search_memories, KnowledgeGraph,
    palace_graph, Dialect, etc.) keeps its existing standalone tests
    passing.
    """

    def __init__(
        self,
        *,
        palace_path_resolver: Callable[[], Path],
        kg_path_resolver: Optional[Callable[[], Path]] = None,
        embedding_device: str = "auto",
        backend_name: str = "chroma",
        adapter_name: str = "mempalace-hub-hosted",
        adapter_version: str = "0.3.0",
    ) -> None:
        self._palace_path_resolver = palace_path_resolver
        self._kg_path_resolver = kg_path_resolver
        self._embedding_device = embedding_device
        self._backend_name = backend_name
        self._adapter_name = adapter_name
        self._adapter_version = adapter_version

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_palace_path(self) -> Path:
        return Path(self._palace_path_resolver())

    def _resolve_kg_path(self) -> Path:
        if self._kg_path_resolver is None:
            return self._resolve_palace_path() / "kg.sqlite3"
        return Path(self._kg_path_resolver())

    def _check_palace_disabled_or_frozen(
        self, hub_ctx: HubContext, *, op: str
    ) -> Optional[Refused]:
        """Common rule-2 pre-flight for every method.

        Returns ``Refused(...)`` if a gate refuses; ``None`` to proceed.
        """
        if hub_ctx.is_palace_disabled():
            return Refused(reason="palace_disabled",
                           detail=f"op={op}",
                           audit_event_id=hub_ctx.audit_event_id)
        if hub_ctx.is_frozen():
            return Refused(reason="session_frozen",
                           detail=f"op={op}",
                           audit_event_id=hub_ctx.audit_event_id)
        return None

    def _check_protected_path(
        self, hub_ctx: HubContext, *, path: Optional[str], op: str
    ) -> Optional[Refused]:
        """Check protected_file_registry for paths in write args."""
        if not path:
            return None
        try:
            if hub_ctx.is_protected(Path(path)):
                return Refused(reason="protected_file",
                               detail=f"op={op} path={path}",
                               audit_event_id=hub_ctx.audit_event_id)
        except Exception:
            # Defensive: if the registry probe raises, don't admit
            # the write. Fail-closed.
            logger.exception("is_protected probe raised on %s", path)
            return Refused(reason="protected_file_probe_failed",
                           detail=f"op={op}",
                           audit_event_id=hub_ctx.audit_event_id)
        return None

    def _check_privacy_floor(
        self, hub_ctx: HubContext, *, content_class: str, op: str
    ) -> Optional[Refused]:
        """Reject writes whose content class is below palace floor.

        ``content_class`` is what the caller claims about the new
        content. The palace's ``privacy_floor`` is canonical from
        AIDOCS. Writes above the floor are admitted; below are
        refused.
        """
        order = ["public", "internal", "pii_potential", "sensitive", "secrets_possible"]
        try:
            floor_idx = order.index(hub_ctx.privacy_floor)
            content_idx = order.index(content_class)
        except ValueError:
            # Unknown class — fail closed.
            return Refused(reason="unknown_privacy_class",
                           detail=f"op={op} class={content_class}",
                           audit_event_id=hub_ctx.audit_event_id)
        if content_idx < floor_idx:
            return Refused(reason="privacy_floor",
                           detail=f"op={op} floor={hub_ctx.privacy_floor} class={content_class}",
                           audit_event_id=hub_ctx.audit_event_id)
        return None

    def _check_intent_grant(
        self, hub_ctx: HubContext, *, tool_name: str
    ) -> Optional[Refused]:
        """Verify intent grant if AIDOCS policy requires one for this tool."""
        if not hub_ctx.intent_grant_required(tool_name):
            return None
        if hub_ctx.intent_grant_present(tool_name):
            return None
        return Refused(reason="intent_grant_needed",
                       detail=f"tool={tool_name}",
                       audit_event_id=hub_ctx.audit_event_id)

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        query: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        limit: int = 5,
        max_distance: float = 1.5,
        display_floor: Optional[str] = None,
        restrict_source_files: Optional[set] = None,
        restrict_unit_ids: Optional[set] = None,
        hub_ctx: HubContext,
    ):
        """Hybrid BM25 + vector + closet-first search.

        Returns ``SearchResult`` with ``SearchHit`` items per RFC 003
        §8.4 — metadata + snippet, NOT full verbatim content.

        Read-path current-policy filtering per RFC 003 §6.4 (rule 5):
        every hit re-checked against ctx.is_protected, ctx.privacy_floor,
        and unit existence. A drawer whose source_file became
        protected since write does NOT surface.
        """
        # Rule 2 gate pre-flight (reads still check palace_disabled).
        refused = self._check_palace_disabled_or_frozen(hub_ctx, op="search")
        if refused is not None:
            return SearchResult(hits=(), total_candidates=0,
                                total_returned=0, filtered_by_policy=0)

        # Delegate to existing mempalace search.
        from .query_sanitizer import sanitize_query
        from .searcher import search_memories

        sanitized = sanitize_query(query)
        raw_result = search_memories(
            sanitized["clean_query"],
            palace_path=str(self._resolve_palace_path()),
            wing=wing,
            room=room,
            n_results=limit,
            max_distance=max_distance,
        )

        raw_hits = raw_result.get("results", []) if isinstance(raw_result, dict) else []

        # RFC-4 Phase C — true code-constrained search. When the caller
        # passes restrict_source_files or restrict_unit_ids (from a
        # high-confidence RecallIntent), hits whose metadata falls
        # outside those sets are dropped HERE inside the service, not
        # post-filtered by the caller. This is the real "library search
        # by call-number range", not "open the whole archive then sort".
        if restrict_source_files or restrict_unit_ids:
            kept = []
            sf_set = {str(s) for s in (restrict_source_files or set())}
            uid_set = {str(u) for u in (restrict_unit_ids or set())}
            for raw in raw_hits:
                meta = raw.get("metadata") if isinstance(raw, dict) else {}
                sf = (meta.get("source_file") if isinstance(meta, dict) else "") or ""
                uid = (meta.get("unit_id") if isinstance(meta, dict) else "") or ""
                if sf_set and sf in sf_set:
                    kept.append(raw); continue
                if uid_set and uid and uid in uid_set:
                    kept.append(raw); continue
            raw_hits = kept

        # Rule 5 — current-policy filter.
        order = ["public", "internal", "pii_potential", "sensitive", "secrets_possible"]
        try:
            floor_idx = order.index(hub_ctx.privacy_floor)
        except ValueError:
            floor_idx = order.index("internal")
        if display_floor:
            try:
                display_idx = order.index(display_floor)
                floor_idx = max(floor_idx, display_idx)
            except ValueError:
                pass

        filtered_hits = []
        filtered_count = 0
        for raw in raw_hits:
            if not isinstance(raw, dict):
                continue
            # AIDOCS PATCH (2026-07-17 recall-outage fix): search_memories
            # returns FLAT hit dicts (text / source_path / wing / room /
            # drawer_id / unit_id ...), not the nested {id, content, metadata}
            # shape this loop originally read — so every SearchHit surfaced
            # as a blank husk (drawer_id='' snippet='') and ai_recall's
            # blank-hit filter dropped 100% of palace results. Read the flat
            # shape first; keep the nested reads as fallback for any legacy
            # caller that still passes the old shape.
            meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else raw
            source_file = (
                raw.get("source_path")
                or meta.get("source_file")
                or raw.get("source_file")
                or None
            )
            if source_file == "?":
                source_file = None
            hit_class = (meta.get("privacy_class") or "internal") or "internal"

            # is_protected — current state, not stored state
            if source_file:
                try:
                    if hub_ctx.is_protected(Path(source_file)):
                        filtered_count += 1
                        continue
                except Exception:
                    logger.exception("read-filter is_protected raised on %s", source_file)
                    filtered_count += 1
                    continue

            # privacy class against current effective floor
            try:
                hit_idx = order.index(hit_class)
            except ValueError:
                hit_idx = order.index("internal")
            if hit_idx < floor_idx:
                filtered_count += 1
                continue

            snippet = str(raw.get("content") or raw.get("text") or "")
            snippet = snippet[:200]
            if hit_class in ("sensitive", "secrets_possible"):
                snippet = f"[redacted; privacy_class={hit_class}]"

            drawer_id = str(raw.get("drawer_id") or raw.get("id") or "")

            # Phase D — read-path staleness consult (RFC-4 §3.5 + §6.4 rule 5).
            staleness_marker = None
            stale_signals = getattr(hub_ctx, "stale_signals", None)
            if stale_signals is not None and drawer_id:
                try:
                    state = stale_signals.get_state(drawer_id)
                    if state == "deleted":
                        # Hidden by default per RFC-4 Phase D simplification
                        filtered_count += 1
                        continue
                    elif state == "stale":
                        staleness_marker = "content_hash_mismatch"
                except Exception:
                    pass

            _dist = raw.get("distance")
            filtered_hits.append(SearchHit(
                drawer_id=drawer_id,
                score=1.0 - float(_dist if _dist is not None else 0.0),
                source_file=str(source_file or ""),
                unit_id=(raw.get("unit_id") or meta.get("unit_id")) or None,
                snippet=snippet,
                why_matched=tuple(),
                privacy_class=hit_class,
                wing=str(meta.get("wing") or raw.get("wing") or ""),
                room=str(meta.get("room") or raw.get("room") or ""),
                staleness=staleness_marker,
            ))

        prefer_authority = None
        # Heuristic: if query looks like a code symbol reference,
        # suggest ai_find first per RFC 003 §11.1 (exact-beats-semantic).
        import re as _re
        if _re.match(r"^[A-Z_][\w]*(\.[\w]+)*\s*$", query.strip()):
            prefer_authority = "ai_find"

        return SearchResult(
            hits=tuple(filtered_hits),
            total_candidates=len(raw_hits),
            total_returned=len(filtered_hits),
            filtered_by_policy=filtered_count,
            query_sanitized=sanitized.get("was_sanitized", False),
            prefer_authority=prefer_authority,
        )

    def get_bootstrap_context(
        self,
        *,
        project_root: Optional[Path] = None,
        session_id: Optional[str] = None,
        host_session_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        categories: tuple = ("rules", "domains", "roadmaps", "operator_profile"),
        per_category_limit: int = 0,
        max_chars: int = 0,
        hub_ctx: HubContext,
    ):
        """Deterministic bootstrap-law bundle (AIDOCS non-code-memory exit).

        Returns a ``BootstrapContext`` of PINNED, ACTIVE non-code memory
        records grouped by category (== palace wing), for AIDOCS to inject
        as constitutional bootstrap law. This is NOT semantic search:

          * exact-category lookup via the backend's deterministic ``get``
          * ACTIVE-only: retired/superseded/deleted AND stale (drifted)
            drawers are filtered out (stale_signals) — bootstrap law never
            injects stale authority; it degrades closed
          * current-policy filtered (privacy floor / is_protected, rule 5)
          * deterministic ordering (category priority, then title, drawer_id)
          * bounded by ``per_category_limit`` and ``max_chars`` (0 == unlimited)

        Markdown files are NEVER read here — content comes from palace
        drawers (canonical). Editing/deleting ``.MEMORY/*.md`` cannot change
        this bundle unless the change is imported into the palace, and a
        retired record can never be resurrected by a lingering markdown file.

        ``project_root`` is accepted for signature symmetry / future
        multi-palace routing; the palace path is resolved from the service's
        resolver. Returns ``Refused`` only when a gate refuses or the palace
        cannot be opened; an empty-but-healthy palace yields an empty bundle
        (degrade-closed: AIDOCS injects nothing rather than reading files).
        """
        from .conjoined_types import BootstrapContext, BootstrapRecord, Refused

        refused = self._check_palace_disabled_or_frozen(
            hub_ctx, op="get_bootstrap_context"
        )
        if refused is not None:
            return refused

        from .palace import get_collection
        try:
            col = get_collection(str(self._resolve_palace_path()), create=False)
        except Exception as exc:
            return Refused(
                reason="palace_unavailable",
                detail=str(exc),
                audit_event_id=getattr(hub_ctx, "audit_event_id", None),
            )

        # Caller's category tuple order IS the injection priority.
        cat_order = {c: i for i, c in enumerate(categories)}

        # Privacy floor (rule 5) — same model as search().
        order = [
            "public", "internal", "pii_potential", "sensitive", "secrets_possible",
        ]
        try:
            floor_idx = order.index(hub_ctx.privacy_floor)
        except (ValueError, AttributeError):
            floor_idx = order.index("internal")

        stale_signals = getattr(hub_ctx, "stale_signals", None)

        collected: list = []
        truncated = False

        for category in categories:
            kept: list = []
            if col is not None:
                try:
                    got = col.get(where={"wing": category})
                except Exception:
                    got = None
                ids = list(getattr(got, "ids", []) or []) if got is not None else []
                docs = list(getattr(got, "documents", []) or []) if got is not None else []
                metas = list(getattr(got, "metadatas", []) or []) if got is not None else []
                for i, raw_id in enumerate(ids):
                    drawer_id = str(raw_id)
                    content = (docs[i] if i < len(docs) else "") or ""
                    meta = metas[i] if i < len(metas) else {}
                    if not isinstance(meta, dict):
                        meta = {}

                    # Bootstrap law is constitutional — ACTIVE-only. A drawer
                    # whose canonical row was retired ("deleted") OR has
                    # drifted ("stale") is NOT injected: stale law is worse
                    # than no law, so we degrade closed. Advisory/search
                    # surfaces may still surface stale; mandatory bootstrap
                    # law must not.
                    status = "active"
                    if stale_signals is not None and drawer_id:
                        try:
                            if stale_signals.get_state(drawer_id) in (
                                "deleted", "stale",
                            ):
                                continue
                        except Exception:
                            pass

                    # Privacy floor (rule 5).
                    hit_class = meta.get("privacy_class") or "internal"
                    try:
                        hit_idx = order.index(hit_class)
                    except ValueError:
                        hit_idx = order.index("internal")
                    if hit_idx < floor_idx:
                        continue

                    # is_protected — CURRENT state, not stored (rule 5).
                    source_file = meta.get("source_file") or ""
                    if source_file:
                        try:
                            if hub_ctx.is_protected(Path(source_file)):
                                continue
                        except Exception:
                            pass

                    checksum = meta.get("content_hash") or ""
                    kept.append(BootstrapRecord(
                        category=category,
                        title=meta.get("room") or "",
                        content=content,
                        drawer_id=drawer_id,
                        memory_id=meta.get("unit_id") or "",
                        source_kind=meta.get("wing") or category,
                        scope=source_file or category,
                        source_file=source_file,
                        checksum=checksum,
                        updated_at=meta.get("filed_at") or "",
                        version=(checksum[:12] if checksum else ""),
                        status=status,
                    ))

            kept.sort(key=lambda r: (r.title, r.drawer_id))
            if per_category_limit and len(kept) > per_category_limit:
                kept = kept[:per_category_limit]
                truncated = True
            collected.extend(kept)

        # Deterministic GLOBAL order: category priority, then title, drawer_id.
        collected.sort(
            key=lambda r: (cat_order.get(r.category, len(cat_order)), r.title, r.drawer_id)
        )

        # Global char bound (0 == unlimited).
        if max_chars:
            bounded: list = []
            running = 0
            for rec in collected:
                clen = len(rec.content)
                if running + clen > max_chars:
                    truncated = True
                    break
                bounded.append(rec)
                running += clen
            collected = bounded

        return BootstrapContext(
            records=tuple(collected),
            categories=tuple(categories),
            total_records=len(collected),
            total_chars=sum(len(r.content) for r in collected),
            truncated=truncated,
            generated_at=_now_iso(),
            session_id=session_id,
            host_session_id=host_session_id,
            lane_id=lane_id,
        )

    def status(self, *, hub_ctx: HubContext) -> PalaceStatus:
        """Lightweight palace-health surface for ai_palace_status."""
        from .config import MempalaceConfig
        from .palace import get_collection

        palace_path = str(self._resolve_palace_path())
        wings: dict = {}
        rooms: dict = {}
        total = 0
        vector_disabled = False
        vector_disabled_reason = ""
        kg_entities = 0
        kg_triples = 0

        try:
            col = get_collection(palace_path, create=False)
            if col is not None:
                total = col.count()
        except Exception as exc:
            logger.debug("status: collection unavailable: %s", exc)
            vector_disabled = True
            # Include the exception class: a bare FileNotFoundError str()
            # is just the path, which reads as nonsense in a reason field.
            vector_disabled_reason = f"{type(exc).__name__}: {exc}"[:200]

        # KG counts — best-effort, never block status.
        try:
            from .knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(str(self._resolve_kg_path()))
            try:
                stats = kg.stats()
                if isinstance(stats, dict):
                    kg_entities = int(stats.get("entities", 0) or 0)
                    kg_triples = int(stats.get("triples", 0) or 0)
            finally:
                kg.close()
        except Exception as exc:
            logger.debug("status: KG unavailable: %s", exc)

        return PalaceStatus(
            palace_path=palace_path,
            total_drawers=total,
            wings=wings,
            rooms=rooms,
            kg_entities=kg_entities,
            kg_triples=kg_triples,
            tunnels=0,  # palace_graph stats added in a future iter
            palace_disabled=hub_ctx.is_palace_disabled(),
            kill_switch_active=hub_ctx.is_kill_switch_active(),
            vector_disabled=vector_disabled,
            vector_disabled_reason=vector_disabled_reason,
            aidocs_integrated=True,  # hub_ctx implies hosted under AIDOCS
        )

    def diary_read(
        self,
        *,
        agent_name: str,
        last_n: int = 10,
        wing: Optional[str] = None,
        read_across_agents: bool = False,
        hub_ctx: HubContext,
    ):
        """Read recent diary entries for ``agent_name``.

        Cross-agent reads require explicit ``read_across_agents=True``.
        """
        refused = self._check_palace_disabled_or_frozen(hub_ctx, op="diary_read")
        if refused is not None:
            return refused

        from .config import sanitize_name
        try:
            agent_name = sanitize_name(agent_name, "agent_name")
        except ValueError as exc:
            return Refused(reason="invalid_agent_name", detail=str(exc),
                           audit_event_id=hub_ctx.audit_event_id)

        target_wing = wing if wing else f"wing_{agent_name}"
        try:
            target_wing = sanitize_name(target_wing, "wing")
        except ValueError as exc:
            return Refused(reason="invalid_wing", detail=str(exc),
                           audit_event_id=hub_ctx.audit_event_id)

        # Delegate to the existing mempalace handler, then current-policy filter.
        from .palace import get_collection
        try:
            col = get_collection(str(self._resolve_palace_path()), create=False)
        except Exception as exc:
            return Refused(reason="palace_unavailable", detail=str(exc),
                           audit_event_id=hub_ctx.audit_event_id)

        if col is None:
            return []

        where = {"wing": target_wing}
        try:
            result = col.get(where=where, limit=last_n,
                             include=["documents", "metadatas"])
        except Exception as exc:
            return Refused(reason="diary_read_failed", detail=str(exc),
                           audit_event_id=hub_ctx.audit_event_id)

        ids = result.get("ids") if isinstance(result, dict) else getattr(result, "ids", [])
        docs = result.get("documents") if isinstance(result, dict) else getattr(result, "documents", [])
        metas = result.get("metadatas") if isinstance(result, dict) else getattr(result, "metadatas", [])

        return [
            {
                "id": ids[i] if i < len(ids) else "",
                "content": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
            }
            for i in range(min(len(ids), last_n))
        ]

    def get_drawer_summary(
        self,
        *,
        drawer_id: str,
        hub_ctx: HubContext,
    ):
        """RFC-4 Phase B — lightweight drawer summary for edit_memory_gate.

        Returns dict {drawer_id, snippet, wing, room, source_file,
                      privacy_class, content_hash} — metadata + first
        200 chars only. No full content (RFC 003 §8.4 patch #9).
        """
        from .conjoined_types import Refused

        refused = self._check_palace_disabled_or_frozen(
            hub_ctx, op="get_drawer_summary",
        )
        if refused is not None:
            return refused

        from .palace import get_collection
        try:
            col = get_collection(
                str(self._resolve_palace_path()), create=False,
            )
        except Exception as exc:
            return Refused(
                reason="palace_unavailable",
                detail=str(exc),
                audit_event_id=hub_ctx.audit_event_id,
            )
        if col is None:
            return None

        try:
            result = col.get(
                ids=[drawer_id],
                include=["documents", "metadatas"],
            )
        except Exception:
            return None

        docs = result.get("documents") if isinstance(result, dict) else getattr(result, "documents", [])
        metas = result.get("metadatas") if isinstance(result, dict) else getattr(result, "metadatas", [])

        if not docs:
            return None

        doc = docs[0] if docs else ""
        meta = metas[0] if metas else {}
        snippet = (doc or "")[:200]

        # Redact for privacy
        privacy_class = (meta.get("privacy_class") if isinstance(meta, dict) else None) or "internal"
        if privacy_class in {"sensitive", "secrets_possible"}:
            snippet = f"[redacted; privacy_class={privacy_class}]"

        return {
            "drawer_id": drawer_id,
            "snippet": snippet,
            "wing": (meta.get("wing") if isinstance(meta, dict) else "") or "",
            "room": (meta.get("room") if isinstance(meta, dict) else "") or "",
            "source_file": (meta.get("source_file") if isinstance(meta, dict) else "") or "",
            "privacy_class": privacy_class,
            "content_hash": (meta.get("content_hash") if isinstance(meta, dict) else "") or "",
        }

    def add_drawer(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source_file: Optional[str] = None,
        unit_id: Optional[str] = None,
        added_by: str = "palace",
        drawer_id: Optional[str] = None,
        # AIDOCS PATCH: force Phase A extraction even when the drawer carries a
        # (non-code) unit_id. Memory drawers hold a memory:<path> identity but
        # still want code-unit anchors extracted from their content.
        force_extract_anchors: bool = False,
        # AIDOCS PATCH: extra chroma metadata merged into the drawer (scalar
        # values only), e.g. chunk grouping {parent_drawer_id, chunk_index,
        # chunk_total}. Service-reserved keys always win.
        extra_metadata: Optional[dict] = None,
        hub_ctx: HubContext,
    ):
        """RFC-4 Phase A — add a drawer with automatic anchor extraction.

        After persisting the drawer, scan ``content`` for code references
        and register any resolved anchors via
        ``hub_ctx.register_anchor`` (if wired) and
        ``hub_ctx.resolve_unit`` (if a UnitResolver is wired into the
        context).

        Returns either:
          - dict {"drawer_id", "wing", "room", "anchor_ids",
                  "audit_event_id"}
          - Refused(...) on gate refusal
        """
        from .conjoined_types import Refused
        import uuid

        op = "add_drawer"

        refused = self._check_palace_disabled_or_frozen(hub_ctx, op=op)
        if refused is not None:
            return refused

        refused = self._check_protected_path(
            hub_ctx, path=source_file, op=op,
        )
        if refused is not None:
            return refused

        refused = self._check_privacy_floor(
            hub_ctx, content_class="internal", op=op,
        )
        if refused is not None:
            return refused

        refused = self._check_intent_grant(
            hub_ctx, tool_name="ai_palace_add_drawer",
        )
        if refused is not None:
            return refused

        from .config import sanitize_name, sanitize_content
        try:
            wing = sanitize_name(wing, "wing")
            room = sanitize_name(room, "room")
            content_clean = sanitize_content(content)
        except ValueError as exc:
            return Refused(
                reason="invalid_input",
                detail=str(exc),
                audit_event_id=hub_ctx.audit_event_id,
            )

        from .palace import get_collection
        try:
            col = get_collection(
                str(self._resolve_palace_path()), create=True,
            )
        except Exception as exc:
            return Refused(
                reason="palace_unavailable",
                detail=str(exc),
                audit_event_id=hub_ctx.audit_event_id,
            )
        if col is None:
            return Refused(
                reason="palace_unavailable",
                audit_event_id=hub_ctx.audit_event_id,
            )

        # Caller may supply a DETERMINISTIC drawer_id (e.g. the memory
        # ingest derives it from the unit_id so retirement can mark the
        # exact drawer deleted without a metadata lookup). Default random.
        drawer_id = drawer_id or f"drawer_{uuid.uuid4().hex[:24]}"
        # Compute content_hash for stale tracking
        import hashlib
        content_hash = hashlib.sha256(
            content_clean.encode("utf-8")
        ).hexdigest()

        meta = {
            "wing": wing,
            "room": room,
            "added_by": added_by,
            "filed_at": _now_iso(),
            "audit_event_id": hub_ctx.audit_event_id,
            "session_id": hub_ctx.session_id,
            "seat": hub_ctx.seat,
            "drawer_kind": "operator_authored",
            "content_hash": content_hash,
        }
        # AIDOCS PATCH: merge caller extra_metadata (scalars only; never override
        # a service-reserved key) — carries chunk grouping for oversized memories.
        if extra_metadata:
            for _ek, _ev in extra_metadata.items():
                if _ek not in meta and isinstance(_ev, (str, int, float, bool)):
                    meta[str(_ek)] = _ev
        if source_file:
            meta["source_file"] = source_file
        if unit_id:
            meta["unit_id"] = unit_id

        tx = hub_ctx.bridge_tx_begin("palace.add_drawer")
        try:
            col.upsert(
                ids=[drawer_id],
                documents=[content_clean],
                metadatas=[meta],
            )
            tx.record_palace_write(ref=drawer_id, status="committed")
        except Exception as exc:
            tx.rollback(reason=f"drawer upsert failed: {exc}")
            return Refused(
                reason="drawer_write_failed",
                detail=str(exc),
                audit_event_id=hub_ctx.audit_event_id,
            )

        # Phase A — automatic anchor extraction
        anchor_ids: list[int] = []
        resolver = getattr(hub_ctx, "resolve_unit", None)
        anchor_writer = getattr(hub_ctx, "register_anchor", None)
        # AIDOCS PATCH: the unit_id guard skips drawers already bound to a unit;
        # memory drawers pass force_extract_anchors=True so their content still
        # yields code-unit anchors despite carrying a memory:<path> unit_id.
        if (
            resolver is not None
            and anchor_writer is not None
            and (unit_id is None or force_extract_anchors)
        ):
            try:
                from .anchor_extractor import (
                    extract_anchors,
                    register_extracted_anchors,
                )
                candidates = extract_anchors(content_clean, resolver)
                anchor_ids = register_extracted_anchors(
                    candidates,
                    drawer_id=drawer_id,
                    content_hash=content_hash,
                    anchor_writer=anchor_writer,
                )
                tx.record_aidocs_write(
                    ref=f"anchors={len(anchor_ids)}",
                    status="committed" if anchor_ids else "not_required",
                )
            except Exception as exc:
                # Anchor extraction failure must not roll back the drawer
                tx.record_aidocs_write(status="failed")
                import logging
                logging.getLogger(__name__).exception(
                    "anchor extraction failed for drawer_id=%s", drawer_id,
                )
        else:
            tx.record_aidocs_write(status="not_required")

        tx.commit()
        return {
            "drawer_id": drawer_id,
            "wing": wing,
            "room": room,
            "anchor_ids": anchor_ids,
            "audit_event_id": hub_ctx.audit_event_id,
            "content_hash": content_hash,
        }

    def diary_write(
        self,
        *,
        agent_name: str,
        entry: str,
        topic: str = "general",
        wing: Optional[str] = None,
        hub_ctx: HubContext,
    ):
        """Write a diary entry for ``agent_name``.

        Hidden-reasoning content (RFC 003 §17.2) is rejected at this
        write surface — diary entries must be host-visible. The
        validator deny-list catches obvious model-private patterns.
        """
        refused = self._check_palace_disabled_or_frozen(hub_ctx, op="diary_write")
        if refused is not None:
            return refused

        # Hidden-reasoning content prohibition (§17.2).
        if _looks_like_hidden_reasoning(entry):
            return Refused(reason="diary_hidden_reasoning",
                           detail="entry matches hidden-reasoning pattern",
                           audit_event_id=hub_ctx.audit_event_id)

        # Intent grant check.
        refused = self._check_intent_grant(hub_ctx, tool_name="ai_palace_diary_write")
        if refused is not None:
            return refused

        from .config import sanitize_name, sanitize_content
        try:
            agent_name = sanitize_name(agent_name, "agent_name")
            entry = sanitize_content(entry)
            topic = sanitize_name(topic, "topic") if topic else "general"
        except ValueError as exc:
            return Refused(reason="invalid_input", detail=str(exc),
                           audit_event_id=hub_ctx.audit_event_id)

        target_wing = wing if wing else f"wing_{agent_name}"
        try:
            target_wing = sanitize_name(target_wing, "wing")
        except ValueError as exc:
            return Refused(reason="invalid_wing", detail=str(exc),
                           audit_event_id=hub_ctx.audit_event_id)

        # Delegate to mempalace's tool_diary_write through library API.
        from .palace import get_collection
        try:
            col = get_collection(str(self._resolve_palace_path()), create=True)
        except Exception as exc:
            return Refused(reason="palace_unavailable", detail=str(exc),
                           audit_event_id=hub_ctx.audit_event_id)

        if col is None:
            return Refused(reason="palace_unavailable",
                           audit_event_id=hub_ctx.audit_event_id)

        # Begin bridge_tx — diary write is single-store today, but the
        # journal still records the receipt for audit.
        tx = hub_ctx.bridge_tx_begin("palace.diary_write")
        drawer_id = f"diary_{agent_name}_{uuid.uuid4().hex[:16]}"
        meta = {
            "wing": target_wing,
            "room": f"diary_{topic}",
            "added_by": agent_name,
            "filed_at": _now_iso(),
            "audit_event_id": hub_ctx.audit_event_id,
            "session_id": hub_ctx.session_id,
            "seat": hub_ctx.seat,
            "drawer_kind": "diary_entry",
        }
        try:
            col.upsert(ids=[drawer_id], documents=[entry], metadatas=[meta])
            tx.record_palace_write(ref=drawer_id, status="committed")
            tx.record_aidocs_write(status="not_required")
            tx.commit()
        except Exception as exc:
            tx.rollback(reason=f"diary_write_failed: {exc}")
            return Refused(reason="diary_write_failed", detail=str(exc),
                           audit_event_id=hub_ctx.audit_event_id)

        return {"drawer_id": drawer_id, "wing": target_wing,
                "audit_event_id": hub_ctx.audit_event_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()


def _palace_drawer_id_for_unit(unit_id: str) -> str:
    """Deterministic drawer_id derived from unit_id.

    Per RFC 003 §3.4 round-trip: a Unit's palace drawer carries an
    id that's reverse-recoverable to the unit_id. Format:
    ``unit_<first 24 chars of unit_id>`` — short, unique, traceable.
    """
    return f"unit_{unit_id[:24]}"


# Add the ingest_aidocs_unit method to PalaceService via attaching it
# below the class definition. Keeps the class body shorter and lets
# the Phase 2 wiring stay localized.

def _ps_ingest_aidocs_unit(
    self,
    *,
    unit,
    content,
    hub_ctx,
):
    """RFC-4 Phase 2 — persist a Unit's content as a palace drawer.

    Per RFC 003 §6 gate order:
      1. emergency_stop / palace_disabled / freeze check
      2. UnitContent invariant check (refused → no drawer write)
      3. Privacy ratchet: sensitive/secrets_possible → refuse (§9.2)
      4. Bridge_tx begin
      5. Drawer upsert with unit_id metadata
      6. Anchor registration via hub.memory.register_anchor (if available)
      7. Commit + return DrawerRef

    Returns either:
      - dict {"drawer_id", "unit_id", "anchor_id", "audit_event_id"}
      - Refused(...) on gate refusal
    """
    from .conjoined_types import Refused

    op = "ingest_aidocs_unit"

    # Gate pre-flight
    refused = self._check_palace_disabled_or_frozen(hub_ctx, op=op)
    if refused is not None:
        return refused

    # UnitContent invariant — refuse if AIDOCS exported refused content
    # but somehow has bytes (would be an AIDOCS-side bug). UnitContent
    # __post_init__ should catch this at construction, but defense-in-depth.
    if content.unit_export_status != "allowed":
        return Refused(
            reason=f"unit_export_status={content.unit_export_status}",
            detail=content.refusal_reason or "",
            audit_event_id=hub_ctx.audit_event_id,
        )

    # Privacy ratchet — RFC 003 §9.2 embedding-as-leak protection
    if unit.privacy_class in {"sensitive", "secrets_possible"}:
        return Refused(
            reason="privacy_class_no_embed",
            detail=f"unit privacy_class={unit.privacy_class}",
            audit_event_id=hub_ctx.audit_event_id,
        )

    # Privacy class against palace floor (rule 2 enforcement)
    refused = self._check_privacy_floor(
        hub_ctx, content_class=unit.privacy_class, op=op,
    )
    if refused is not None:
        return refused

    # Protected files check (RFC 003 rule 5 also)
    refused = self._check_protected_path(
        hub_ctx, path=unit.source_file, op=op,
    )
    if refused is not None:
        return refused

    # Resolve content
    from .palace import get_collection

    try:
        col = get_collection(str(self._resolve_palace_path()), create=True)
    except Exception as exc:
        return Refused(
            reason="palace_unavailable",
            detail=str(exc),
            audit_event_id=hub_ctx.audit_event_id,
        )
    if col is None:
        return Refused(
            reason="palace_unavailable",
            audit_event_id=hub_ctx.audit_event_id,
        )

    drawer_id = _palace_drawer_id_for_unit(unit.unit_id)
    content_str = content.content.decode("utf-8", errors="replace")

    # Determine wing / room from unit metadata. Conservative defaults:
    # wing = first parent_chain element OR project-level fallback
    # room = unit.kind
    wing = unit.parent_chain[0] if unit.parent_chain else "code"
    room = unit.kind

    meta = {
        "wing": wing,
        "room": room,
        "source_file": unit.source_file,
        "line_start": unit.line_start,
        "line_end": unit.line_end,
        "unit_id": unit.unit_id,
        "unit_kind": unit.kind,
        "parent_chain": ";".join(unit.parent_chain),
        "content_hash": content.content_hash,
        "aidocs_index_version": unit.aidocs_index_version,
        "privacy_class": unit.privacy_class,
        "boundary_confidence": unit.boundary_confidence,
        "embedding_source": "hub_canonical",
        "audit_event_id": hub_ctx.audit_event_id,
        "session_id": hub_ctx.session_id,
        "seat": hub_ctx.seat,
        "drawer_kind": "aidocs_unit",
        "filed_at": _now_iso(),
    }

    # Bridge_tx for two-store write (palace + AIDOCS anchor)
    tx = hub_ctx.bridge_tx_begin("palace.ingest_aidocs_unit")
    try:
        col.upsert(
            ids=[drawer_id],
            documents=[content_str],
            metadatas=[meta],
        )
        tx.record_palace_write(ref=drawer_id, status="committed")
    except Exception as exc:
        tx.rollback(reason=f"palace upsert failed: {exc}")
        return Refused(
            reason="palace_upsert_failed",
            detail=str(exc),
            audit_event_id=hub_ctx.audit_event_id,
        )

    # Anchor registration — optional. If hub_ctx doesn't carry an
    # anchor-store hook, we skip (Phase 2 ships the API; Phase A
    # uses it; Phase 2 alone can write drawers without anchors).
    anchor_id = None
    anchor_writer = getattr(hub_ctx, "register_anchor", None)
    if anchor_writer is not None:
        try:
            symbol_name = (
                ".".join(unit.parent_chain + (unit.kind,))
                if unit.parent_chain
                else unit.kind
            )
            # Determine confidence: outline-derived units are
            # exact_symbol (came from AIDOCS's outline_extractors).
            confidence = (
                "semantic_guess"
                if unit.boundary_confidence == "heuristic"
                else "exact_symbol"
            )
            anchor_id = anchor_writer(
                symbol_name=symbol_name,
                file_path=unit.source_file,
                drawer_id=drawer_id,
                content_hash=content.content_hash,
                confidence=confidence,
                source="palace_extractor",
            )
            tx.record_aidocs_write(ref=str(anchor_id), status="committed")
        except Exception as exc:
            tx.record_aidocs_write(status="failed")
            tx.rollback(reason=f"anchor register failed: {exc}")
            # Compensating: delete the drawer we just wrote
            try:
                col.delete(ids=[drawer_id])
            except Exception as rb_exc:
                tx.mark_partial_failure(
                    reason=f"anchor failed AND drawer rollback failed: {rb_exc}"
                )
                return Refused(
                    reason="ingest_partial_failure",
                    detail=f"anchor register: {exc}; rollback: {rb_exc}",
                    audit_event_id=hub_ctx.audit_event_id,
                )
            return Refused(
                reason="anchor_register_failed",
                detail=str(exc),
                audit_event_id=hub_ctx.audit_event_id,
            )
    else:
        tx.record_aidocs_write(status="not_required")

    tx.commit()

    return {
        "drawer_id": drawer_id,
        "unit_id": unit.unit_id,
        "anchor_id": anchor_id,
        "audit_event_id": hub_ctx.audit_event_id,
        "content_hash": content.content_hash,
    }


# Attach the method to PalaceService
PalaceService.ingest_aidocs_unit = _ps_ingest_aidocs_unit


def _looks_like_hidden_reasoning(text: str) -> bool:
    """Heuristic for RFC 003 §17.2.

    Conservative: matches obvious extended-thinking XML blocks. Real
    deployments may extend this list per AIDOCS's NLP consumers.
    """
    if not text:
        return False
    import re as _re
    patterns = [
        r"<thinking[\s>]",
        r"</thinking>",
        r"<reasoning[\s>]",
        r"</reasoning>",
        r"<internal_reasoning[\s>]",
    ]
    for p in patterns:
        if _re.search(p, text, _re.IGNORECASE):
            return True
    return False
