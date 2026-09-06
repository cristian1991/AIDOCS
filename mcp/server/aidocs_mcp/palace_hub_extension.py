"""palace_hub_extension — AIDOCS-side wiring for hub.palace and HubContext.

Adopt by copying to ``aidocs_mcp/palace_hub_extension.py``.

Two functions:

  register_palace_in_hub(hub) — attaches a PalaceService instance to
    hub.palace if mempalace is importable. Idempotent on re-call.

  build_palace_context(hub, runtime, *, tool_name) → HubContext —
    constructs the per-call HubContext (RFC 003 §6.1) carrying the
    gate primitives and audit/bridge_tx integration. Built fresh per
    inbound tool call.

This module is the ONLY file that imports mempalace from inside
AIDOCS's mcp/server. Everything else flows through hub.palace.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

class _KgQueryAdapter:
    """Thread-safe ``query_entity`` seam over the palace knowledge graph.

    Exists because #375 shipped a KG WRITER
    (``memory_kg_extractor.ingest_kg_for_entry``) and a populated graph
    with no agent-reachable reader: ``run_clustered_recall`` resolves the
    graph as ``getattr(hub, "kg", None) or getattr(hub.palace, "kg",
    None)`` and nothing in production ever set either name, so every
    RecallCluster shipped ``kg_facts=()`` and ``ai_memory(search)``'s
    ``lane="kg"`` was structurally always empty.

    Opens a SHORT-LIVED KnowledgeGraph per call through the caller's
    resolver and closes it again, so no sqlite connection is shared
    across the server's inbound-call threads (the same open/close
    discipline ``memory_kg_extractor`` already uses).

    Fails OPEN to ``[]`` on any error and NEVER creates the graph file —
    aidocs-doctrine §XXXII: a guest oracle sharpens evidence, it never
    holds the pen, and an absent guest costs zero function.
    """

    __slots__ = ("_resolve",)

    def __init__(self, kg_path_resolver: Any) -> None:
        self._resolve = kg_path_resolver

    def query_entity(
        self,
        name: str,
        as_of: str | None = None,
        direction: str = "outgoing",
    ) -> list[dict]:
        try:
            kg_path = Path(self._resolve())
            if not kg_path.is_file():
                return []  # read path must never create the graph
            from mempalace.knowledge_graph import KnowledgeGraph

            kg = KnowledgeGraph(str(kg_path))
            try:
                return list(
                    kg.query_entity(name, as_of=as_of, direction=direction) or ()
                )
            finally:
                try:
                    kg.close()
                except Exception:  # noqa: BLE001 — close must never surface
                    pass
        except Exception:  # noqa: BLE001 — KG is evidence, never law
            return []


def register_palace_in_hub(hub: Any) -> None:
    """Attach `hub.palace` (PalaceService) and `hub.palace_control`
    (PalaceControlStore) and `hub.bridge_tx` (BridgeTxJournal) to the
    AidocsServiceHub instance.

    If mempalace cannot be imported, sets hub.palace = None and logs
    once — AIDOCS continues to operate with 263 tools, palace tools
    are not registered.
    """
    try:
        from mempalace.bridge_tx import BridgeTxJournal
        from mempalace.service import PalaceService
    except ImportError as exc:
        # #913 — TWO VERY DIFFERENT FACTS ARRIVE THROUGH THIS ONE except, and
        # collapsing them is what let a half-updated runtime look healthy.
        #
        #   mempalace ABSENT  -> a SUPPORTED configuration. AIDOCS runs without
        #                        the palace tools. Expected, benign, stay quiet.
        #   mempalace SKEWED  -> installed, but missing a module THIS BUILD
        #                        imports. A DEFECT -- and the dangerous one,
        #                        because it is indistinguishable from the line
        #                        above unless something asks.
        #
        # The consequence of the second is not "no palace tools". It is that
        # hub.palace becomes None, edit_memory_gate.py:210 then skips the palace
        # consult entirely, and exact_symbol / operator_pinned blockers stop
        # being evaluated -- while the gate still answers allowed=True and the
        # sqlite-path tests stay green. A silent security-relevant downgrade,
        # announced by one INFO line that reads exactly like the benign case.
        #
        # So the verdict is asked for, and the two are logged at the severity
        # each deserves. This does NOT refuse the boot: an unreachable palace
        # must not take AIDOCS down, and that policy is unchanged. What changes
        # is that the defect is now LOUD and, via hub.palace_unavailable, legible
        # to the gate downstream instead of being inferred from a None.
        from .vendored_contract import STATE_SKEWED, check_vendored_mempalace

        verdict = check_vendored_mempalace()
        if verdict["state"] == STATE_SKEWED:
            logger.error("%s (import error: %s)", verdict["reason"], exc)
        else:
            logger.info("palace integration unavailable: %s", exc)

        hub.palace = None
        hub.palace_control = None
        hub.bridge_tx = None
        hub.kg = None
        hub.palace_unavailable = verdict
        return

    hub.palace_unavailable = None

    # Use AIDOCS's existing project_root resolver — same one
    # preflight_service uses (verified at preflight_service.py:152).
    from .mcp_server_runtime_helpers import resolve_project_root
    from .palace_control_store import PalaceControlStore

    def _resolve_palace_path() -> Path:
        return resolve_project_root() / ".MEMORY" / "palace"

    def _resolve_kg_path() -> Path:
        return resolve_project_root() / ".MEMORY" / "kg.sqlite3"

    hub.palace = PalaceService(
        palace_path_resolver=_resolve_palace_path,
        kg_path_resolver=_resolve_kg_path,
    )
    # #375 closing loop: attach the KG read handle run_clustered_recall
    # already reaches for. It shares _resolve_kg_path with PalaceService —
    # ONE resolver, one home (doctrine §XXII), so the reader can never
    # drift to a different file than ingest_kg_for_entry writes.
    hub.kg = _KgQueryAdapter(_resolve_kg_path)
    hub.palace_control = PalaceControlStore()
    # NO eager init_db here (incident 2026-08-13). create_server() runs BEFORE
    # any project is known — this daemon serves fifteen — so binding one at
    # construction is the design error, and `init_db(resolve_project_root())`
    # is what tried to create C:\Windows\System32\.MEMORY and crash-looped the
    # daemon. It was redundant anyway: set_palace_disabled() calls init_db
    # itself, and is_palace_disabled() already tolerates a missing table.
    # The two resolvers above (_resolve_palace_path / _resolve_kg_path) are the
    # pattern this line should have followed all along.
    # Check ADOPTION, not the exception. resolve_project_root() still falls back
    # to Path.cwd() when no project is found (backlog #761), so at daemon startup
    # this can be any directory the process happened to be launched from — it was
    # C:\Windows\System32 on 2026-08-13. Asking "does this root actually have a
    # .MEMORY?" is correct under EITHER resolver behaviour and does not depend on
    # a refusal that is not there yet.
    _tx_root = resolve_project_root()
    if (_tx_root / ".MEMORY").is_dir():
        hub.bridge_tx = BridgeTxJournal(
            _tx_root / ".MEMORY" / ".index" / "palace_bridge_tx.sqlite3",
        )
    else:
        # Degrade exactly as the ImportError branch above does rather than
        # refusing to boot: the daemon must come up so that per-project calls
        # (which DO carry a root) can be served.
        hub.bridge_tx = None
        logger.info(
            "palace bridge_tx deferred: %s is not an adopted project at wiring time",
            _tx_root,
        )

    # RFC-4 §3.5: wire the palace anchor store onto hub.memory.anchors.
    # Without this, check_edit_with_palace's `palace_anchor_store=...`
    # argument is None at runtime and the palace gate silently no-ops
    # — server_recall_tools._safe_get_anchor_store and
    # edit_memory_gate.check_edit_memory_gate both expect this surface.
    # Shares the AIDOCS index DB so memory_routes joins (route_id FK)
    # resolve correctly.
    from .anchor_store import AnchorStore

    try:
        # Resolver pattern (mirrors palace_path_resolver) so the
        # AnchorStore tracks the CURRENT project root rather than the
        # one resolved at create_server time. Tests / project-switching
        # workflows / multi-project agents all need this.
        def _resolve_anchors_db() -> Path:
            return hub.index.db_path(resolve_project_root())

        anchors = AnchorStore(db_path_resolver=_resolve_anchors_db)
        anchors.ensure_schema()
        hub.memory.anchors = anchors
    except Exception:
        logger.exception("AnchorStore wiring failed; palace edit-gate consult disabled")
        hub.memory.anchors = None

    # Surface unresolved transactions at boot per RFC 003 §7.4.
    try:
        unresolved = hub.bridge_tx.unresolved() if hub.bridge_tx is not None else []
        if unresolved:
            logger.warning(
                "palace bridge_tx has %d unresolved transactions; "
                "operator reconciliation required. tx_ids: %s",
                len(unresolved),
                [u["tx_id"] for u in unresolved[:5]],
            )
    except Exception:
        logger.exception("palace bridge_tx unresolved-query failed")

    logger.info("palace integration enabled (hub.palace, hub.palace_control, hub.bridge_tx)")


# ---------------------------------------------------------------------------
# Per-call HubContext factory
# ---------------------------------------------------------------------------


def build_palace_context(hub: Any, runtime: Any, *, tool_name: str, audit: bool = True):
    """Construct a HubContext for the current inbound tool call.

    Per RFC 003 §6.1, this is called fresh per call; the resulting
    context is passed to every hub.palace.* method that takes hub_ctx.

    The gate primitives are closures that consult AIDOCS's existing
    services at evaluation time, NOT at construction time — so a
    long-running tool call that spans a freeze event sees the freeze
    when its next gate-check fires.
    """
    from mempalace.conjoined_types import HubContext

    from .mcp_server_runtime_helpers import resolve_project_root

    project_root = resolve_project_root()

    # Session id / seat / lane id come from the runtime context AIDOCS
    # already maintains. The exact accessors are runtime-specific;
    # this calls them defensively.
    session_id = _safe_attr(runtime, "current_session_id", default="unknown")
    lane_id = _safe_attr(runtime, "current_lane_id", default=None)

    # Seat detection per protected_file_runtime
    try:
        from .protected_file_runtime import is_sub_agent_call

        seat = "lane" if is_sub_agent_call() else "conductor"
    except Exception:
        seat = "conductor"

    # Privacy floor — read from AIDOCS canonical config (placeholder
    # name; adjust to actual AIDOCS accessor when known).
    privacy_floor = (
        _safe_attr(
            runtime,
            "privacy_floor",
            default="internal",
        )
        or "internal"
    )

    # Audit event id minting — call AIDOCS's execution_events store.
    # audit=False is for SYNTHETIC internal traffic only (the embedder
    # warm-up): minting a real palace.call for it polluted every project's
    # execution ledger on server boot and raced into unrelated tests'
    # event windows (the Gate 2b flake, 2026-07-03). Every real tool call
    # keeps the default full audit.
    if audit:
        audit_event_id = _mint_audit_event(
            project_root,
            session_id,
            tool_name,
        )
    else:
        import uuid as _uuid

        audit_event_id = f"palace-noaudit-{_uuid.uuid4().hex[:16]}"

    # Gate primitive closures.
    def _is_palace_disabled() -> bool:
        if hub.palace_control is None:
            return False
        return hub.palace_control.is_palace_disabled(project_root)

    def _is_frozen() -> bool:
        try:
            from .freeze_service import get_existing_freeze

            return get_existing_freeze(project_root, session_id) is not None
        except Exception:
            return False

    def _kill_switch_never_active() -> bool:
        # #404: the kill switch is excised — palace gates never bypass.
        # (The callback name below is the mempalace HubContext field.)
        return False

    def _is_protected(path: Path) -> bool:
        try:
            from .protected_file_registry_store import ProtectedFileRegistryStore

            store = ProtectedFileRegistryStore()
            # Convert path to project-relative if absolute
            try:
                rel = str(path.relative_to(project_root)).replace("\\", "/")
            except (ValueError, AttributeError):
                rel = str(path).replace("\\", "/")
            # #755/#756: the ONE canonical connect. This was
            # `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION
            # context manager, which commits and NEVER closes the handle
            # -- with no pragmas. The protected-file lookup is a pure
            # SELECT, so read_only=True.
            from ._sqlite_connect import connect as _canonical_connect

            with _canonical_connect(
                str(store.db_path(project_root)),
                read_only=True,
                row_factory=False,
            ) as conn:
                row = conn.execute(
                    "SELECT 1 FROM protected_file_registry WHERE path = ? LIMIT 1",
                    (rel,),
                ).fetchone()
                return row is not None
        except Exception:
            return False

    def _intent_grant_required(_tool_name: str) -> bool:
        # Conservative default: no grant required.
        # AIDOCS-side decision when intent_grant_detector matures.
        return False

    def _intent_grant_present(_tool_name: str) -> bool:
        return True  # vacuously satisfied while _required is False

    def _record_audit_decision(event_id: str, **kwargs) -> None:
        try:
            from .execution_index_store import ExecutionIndexStore

            store = ExecutionIndexStore()
            store.record_event(
                project_root,
                event_kind="palace.decision",
                source_kind="hub.palace",
                session_id=session_id,
                event_id=event_id,
                payload=kwargs,
            )
        except Exception:
            logger.exception("record_audit_decision failed")

    def _mint(**kwargs) -> str:
        # Caller can pass extras to enrich the audit event.
        # Returns the event_id (same as audit_event_id at top of context).
        return audit_event_id

    def _bridge_tx_begin(operation: str):
        if hub.bridge_tx is None:
            # Fallback no-op handle when bridge_tx is unavailable
            return _NoOpBridgeTxHandle(tx_id="noop", operation=operation)
        return hub.bridge_tx.begin(
            operation=operation,
            audit_event_id=audit_event_id,
            session_id=session_id,
        )

    # Read-path staleness tracker (RFC-4 Phase D). Attaching it here is
    # what makes PalaceService.search HIDE drawers retired after ingest
    # (e.g. memory rows superseded/removed → memory_sqlite_store marks the
    # drawer 'deleted'). Best-effort: a tracker init failure leaves
    # stale_signals=None (search simply skips the consult).
    stale_signals = None
    try:
        from .memory_sqlite_store import palace_stale_signals_db_path
        from .palace_stale_signals import PalaceStaleSignals

        stale_signals = PalaceStaleSignals(
            palace_stale_signals_db_path(project_root),
        )
    except Exception:
        stale_signals = None

    # RFC-4 Phase A auto-anchoring wire: hand add_drawer a code-index resolver +
    # the AnchorStore.register_anchor callback so memory drawers auto-anchor to
    # code units on ingest. This is the previously-dormant wire that starved
    # memory_symbol_anchors. Best-effort: if the anchor store or code index is
    # unavailable, both stay None and extraction degrades to 'not_required'.
    _resolve_unit = None
    _register_anchor = None
    try:
        _anchors = getattr(getattr(hub, "memory", None), "anchors", None)
        if _anchors is not None and hasattr(_anchors, "register_anchor"):
            _index_db = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
            if _index_db.exists():
                from .unit_resolver import AidocsUnitResolver

                _resolve_unit = AidocsUnitResolver(
                    project_root=project_root,
                    index_db_path=_index_db,
                    project_uuid=_resolve_project_uuid(project_root),
                )
                _register_anchor = _anchors.register_anchor
    except Exception:
        _resolve_unit = None
        _register_anchor = None

    return HubContext(
        project_root=project_root,
        project_uuid=_resolve_project_uuid(project_root),
        session_id=session_id,
        seat=seat,
        lane_id=lane_id,
        audit_event_id=audit_event_id,
        privacy_floor=privacy_floor,
        is_palace_disabled=_is_palace_disabled,
        is_frozen=_is_frozen,
        is_kill_switch_active=_kill_switch_never_active,
        is_protected=_is_protected,
        intent_grant_required=_intent_grant_required,
        intent_grant_present=_intent_grant_present,
        mint_audit_event=_mint,
        record_audit_decision=_record_audit_decision,
        bridge_tx_begin=_bridge_tx_begin,
        stale_signals=stale_signals,
        resolve_unit=_resolve_unit,
        register_anchor=_register_anchor,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_attr(obj: Any, name: str, *, default: Any = None) -> Any:
    """Best-effort attribute reader supporting both attr and ().

    AIDOCS runtime accessors are sometimes methods, sometimes
    properties, sometimes plain attributes. This handles all three.
    """
    try:
        v = getattr(obj, name, None)
        if v is None:
            return default
        if callable(v):
            try:
                return v() or default
            except TypeError:
                return v
        return v
    except Exception:
        return default


def _mint_audit_event(project_root: Path, session_id: str, tool_name: str) -> str:
    """Mint a new audit event id via AIDOCS's execution_events.

    Falls back to a uuid4 prefix if the store is unavailable — the
    palace operations are still audited at the journal level via
    bridge_tx; missing execution_events entry is degraded but not
    catastrophic.
    """
    import uuid

    event_id = f"palace-{uuid.uuid4().hex[:24]}"
    try:
        from .execution_index_store import ExecutionIndexStore

        store = ExecutionIndexStore()
        store.record_event(
            project_root,
            event_kind="palace.call",
            source_kind="hub.palace",
            session_id=session_id,
            capability_name=tool_name,
            event_id=event_id,
        )
    except Exception:
        logger.exception("mint_audit_event failed for %s", tool_name)
    return event_id


def _resolve_project_uuid(project_root: Path) -> str:
    """Read or create .MEMORY/project.uuid for stable project identity."""
    uuid_path = project_root / ".MEMORY" / "project.uuid"
    try:
        if uuid_path.exists():
            text = uuid_path.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass

    import uuid as _uuid

    project_uuid = _uuid.uuid4().hex
    try:
        uuid_path.parent.mkdir(parents=True, exist_ok=True)
        uuid_path.write_text(project_uuid, encoding="utf-8")
    except OSError:
        pass
    return project_uuid


class _NoOpBridgeTxHandle:
    """Fallback handle when bridge_tx journal is unavailable."""

    def __init__(self, *, tx_id: str, operation: str):
        self.tx_id = tx_id
        self.operation = operation

    def record_palace_write(self, **_):
        pass

    def record_aidocs_write(self, **_):
        pass

    def commit(self):
        pass

    def rollback(self, reason: str):
        pass
