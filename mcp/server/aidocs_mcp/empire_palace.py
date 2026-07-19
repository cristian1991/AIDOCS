"""Empire palace — the machine-global MemPalace at ``~/.aidocs/mempalace``.

Umbrella #375 Phase 2 (b): the EMPIRE tier gets its own semantic palace,
one per operator machine (like ``~/.aidocs/empire.sqlite3``), holding
projections of the PUBLIC empire content only:

  - global LAW rows  (``global_law``, status='active')
  - empire SKILLS    (``empire_skills``, read_access='public', never the
    mode-gated ROLE skills, never the sovereign SOULS)

Sovereign souls are excluded BY QUERY — the SELECTs below never touch a
``read_access='sovereign-only'`` row, so a soul can never become a drawer
(pinned by test_empire_palace_phase2). Souls stay sealed behind ai_soul.

Design mirrors the kingdom palace bootstrap (palace_hub_extension):

  - adapter-based on the mempalace HIGH-LEVEL API (PalaceService) only —
    no chroma internals, no mempalace edits;
  - LAZY: nothing imports mempalace or touches ``~`` until first use
    (AQ law — heavy imports park behind first use, never at startup);
  - TIMEBOXED: creation/backfill/search run in daemon-thread timeboxes so
    a cold embedding model or wedged chroma can never hang a caller;
  - embedder identity RECORDED at creation (the War AG lesson): the
    bootstrap verifies a stored identity right after creating the empty
    collection and explicitly records one if the automatic record did not
    fire — the EmbedderIdentityUnknownWarning state must never exist for
    the empire palace.

The palace remains a REBUILDABLE PROJECTION (doctrine XXVI): canonical
homes stay ``global_law`` / ``empire_skills`` in empire.sqlite3; wiping
``~/.aidocs/mempalace`` and re-running the backfill loses nothing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Wing names inside the empire palace (drawer metadata; also the search
# result labels' provenance).
EMPIRE_LAW_WING = "empire_law"
EMPIRE_SKILL_WING = "empire_skill"

# Stamp file marking the one-shot backfill as done (lives INSIDE the palace
# dir so wiping the palace also re-arms the backfill — projection semantics).
_BACKFILL_STAMP = ".empire_backfill.json"

_CREATE_TIMEOUT_S = 20.0
_BACKFILL_TIMEOUT_S = 120.0
_SEARCH_TIMEOUT_S = 2.0

_lock = threading.Lock()
_service_cache: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Paths (fake-HOME friendly: resolved live, never cached)
# ---------------------------------------------------------------------------


def empire_palace_path() -> Path:
    """Machine-global empire palace directory.

    Honors ``AIDOCS_EMPIRE_PALACE_DIR`` (test isolation, same pattern as
    ``AIDOCS_EMPIRE_DB``); defaults to ``~/.aidocs/mempalace``. Tests that
    exercise the default path fake ``Path.home()``.
    """
    override = os.environ.get("AIDOCS_EMPIRE_PALACE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "mempalace"


def empire_kg_path() -> Path:
    return empire_palace_path() / "kg.sqlite3"


def _empire_db() -> Path:
    """The canonical empire DB (same resolution as global_law_store /
    skill_store — AIDOCS_EMPIRE_DB override, else ~/.aidocs/empire.sqlite3)."""
    override = os.environ.get("AIDOCS_EMPIRE_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "empire.sqlite3"


def _backfill_stamp_path() -> Path:
    return empire_palace_path() / _BACKFILL_STAMP


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def get_empire_palace() -> Optional[Any]:
    """Lazy machine-global PalaceService over ``empire_palace_path()``.

    Returns None when mempalace is unavailable. The service carries LIVE
    path resolvers (mirrors palace_hub_extension), so a fake-HOME swap in
    tests is honored per call. Cached per resolved override key so isolated
    tests never share a service with the operator's real palace.
    """
    key = os.environ.get("AIDOCS_EMPIRE_PALACE_DIR", "").strip() or "~default~"
    with _lock:
        if key in _service_cache:
            return _service_cache[key]
        try:
            from mempalace.service import PalaceService
        except ImportError as exc:
            logger.info("empire palace unavailable: %s", exc)
            _service_cache[key] = None
            return None
        service = PalaceService(
            palace_path_resolver=empire_palace_path,
            kg_path_resolver=empire_kg_path,
            adapter_name="mempalace-empire-global",
        )
        _service_cache[key] = service
        return service


class _NoOpBridgeTxHandle:
    """Empire-palace writes are single-store projections; the journal
    receipt lives with the kingdom bridge. Mirrors palace_hub_extension's
    no-op handle shape."""

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


def _empire_hub_ctx(op: str) -> Any:
    """Permissive standalone HubContext for the machine-global palace.

    The empire palace has no per-project gates (no project freeze, no
    protected-file registry applies to law scrolls); privacy floor stays
    'internal'. Sovereignty is enforced UPSTREAM by query — nothing
    sovereign ever reaches a write call carrying this context.
    """
    import uuid

    from mempalace.conjoined_types import HubContext

    return HubContext(
        project_root=empire_palace_path(),
        project_uuid="empire-global",
        session_id="empire",
        seat="conductor",
        lane_id=None,
        audit_event_id=f"empire-{op}-{uuid.uuid4().hex[:16]}",
        privacy_floor="internal",
        is_palace_disabled=lambda: False,
        is_frozen=lambda: False,
        is_kill_switch_active=lambda: False,
        is_protected=lambda _p: False,
        intent_grant_required=lambda _t: False,
        intent_grant_present=lambda _t: True,
        mint_audit_event=lambda **_kw: f"empire-{op}",
        record_audit_decision=lambda *_a, **_kw: None,
        bridge_tx_begin=lambda operation: _NoOpBridgeTxHandle(
            tx_id="empire-noop", operation=operation
        ),
    )


def _run_timeboxed(fn: Callable[[], Any], timeout_s: float) -> tuple[Any, bool]:
    """Daemon-thread timebox (same shape as server_code_tools._run_timeboxed):
    (result, timed_out). Exceptions inside fn degrade to (None, False)."""
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = fn()
        except Exception:  # noqa: BLE001 — best-effort; caller degrades
            logger.debug("empire palace timeboxed call failed", exc_info=True)
            box["result"] = None

    t = threading.Thread(target=_worker, name="empire-palace", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return None, True
    return box.get("result"), False


def ensure_empire_palace_created(timeout_s: float = _CREATE_TIMEOUT_S) -> dict:
    """Create (or verify) the empire palace collection, RECORDING the
    embedder identity at creation.

    The mempalace bootstrap records identity automatically only for a
    brand-new empty collection; this wrapper VERIFIES the record landed and
    explicitly records it when it did not (nameless-embedder edge), so the
    populated-but-unrecorded warning state can never arise later — the
    War AG lesson.
    """

    def _do() -> dict:
        from mempalace.palace import get_collection, set_palace_embedder_identity

        path = empire_palace_path()
        existed = path.is_dir()
        path.mkdir(parents=True, exist_ok=True)
        col = get_collection(str(path), create=True)
        if col is None:
            return {"ok": False, "reason": "collection_unavailable"}
        identity = None
        try:
            identity = col.get_stored_embedder_identity()
        except Exception:
            identity = None
        recorded_now = False
        if identity is None:
            try:
                if int(col.count() or 0) == 0:
                    set_palace_embedder_identity(str(path))
                    identity = col.get_stored_embedder_identity()
                    recorded_now = identity is not None
            except Exception:
                logger.debug("empire palace identity record failed", exc_info=True)
        return {
            "ok": True,
            "created": not existed,
            "identity_recorded": identity is not None,
            "identity_model": getattr(identity, "model_name", "") or "",
            "identity_recorded_now": recorded_now,
        }

    result, timed_out = _run_timeboxed(_do, timeout_s)
    if timed_out:
        return {"ok": False, "reason": "timeout", "timed_out": True}
    if result is None:
        return {"ok": False, "reason": "create_failed"}
    return result


# ---------------------------------------------------------------------------
# Canonical-row readers (soul exclusion lives HERE, in the query)
# ---------------------------------------------------------------------------


def _active_global_law_rows() -> list[dict]:
    """Active global law rows (canonical store's own reader — honors
    AIDOCS_EMPIRE_DB). Fail-quiet []: a store hiccup never breaks callers."""
    try:
        from .global_law_store import list_active_global_law

        return list_active_global_law()
    except Exception:
        return []


def _public_empire_skill_rows() -> list[dict]:
    """PUBLIC empire skill rows only.

    Soul exclusion by query (§XII): the WHERE clause admits ONLY
    ``read_access='public'`` — sovereign souls (read_access='sovereign-only')
    are structurally invisible to this reader. Mode-gated ROLE skills are
    excluded too (the public-door hide extends to every public projection).
    Read-only; never creates the empire DB.
    """
    import sqlite3

    db = _empire_db()
    if not db.is_file():
        return []
    try:
        from .skill_store import SkillStore

        role_ids = sorted(SkillStore._MODE_GATED_ROLE_SKILLS)
    except Exception:
        role_ids = ["co-conductor", "head-conductor", "worker"]
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            marks = ",".join("?" for _ in role_ids)
            rows = conn.execute(
                "SELECT skill_id, name, COALESCE(description,'') AS description, "
                "kind, COALESCE(tags,'') AS tags, content_text "
                "FROM empire_skills "
                f"WHERE COALESCE(read_access,'public') = 'public' "
                f"AND skill_id NOT IN ({marks}) "
                "ORDER BY skill_id",
                role_ids,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Drawer projection helpers
# ---------------------------------------------------------------------------


def _safe_room(kind: str, fallback: str) -> str:
    room = (kind or "").strip().lower().replace(" ", "-") or fallback
    return room[:100]


def _add_drawers_for(
    add_drawer: Callable[..., Any],
    *,
    wing: str,
    room: str,
    content: str,
    unit_id: str,
    drawer_id: str,
    added_by: str,
    hub_ctx: Any,
) -> None:
    """One drawer normally; heading-aware child drawers when oversized —
    same chunking contract as the kingdom ingest (shared helper)."""
    from .memory_sqlite_store import _chunk_memory_content

    chunks = _chunk_memory_content(content)
    if len(chunks) == 1:
        _raise_on_refused(
            add_drawer(
                wing=wing,
                room=room,
                content=content,
                unit_id=unit_id,
                drawer_id=drawer_id,
                added_by=added_by,
                hub_ctx=hub_ctx,
            )
        )
        return
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        _raise_on_refused(
            add_drawer(
                wing=wing,
                room=room,
                content=chunk,
                unit_id=unit_id,
                drawer_id=f"{drawer_id}#chunk{i:04d}",
                added_by=added_by,
                extra_metadata={
                    "parent_drawer_id": drawer_id,
                    "chunk_index": i,
                    "chunk_total": total,
                },
                hub_ctx=hub_ctx,
            )
        )


def _raise_on_refused(result: Any) -> None:
    if type(result).__name__ == "Refused":
        raise RuntimeError(
            "empire palace add_drawer refused: "
            f"{getattr(result, 'reason', '?')} ({getattr(result, 'detail', '')})"
        )


def law_drawer_id(law_id: str) -> str:
    return f"empire:law:{law_id}"


def skill_drawer_id(skill_id: str) -> str:
    return f"empire:skill:{skill_id}"


# ---------------------------------------------------------------------------
# (b) One-shot stamped backfill
# ---------------------------------------------------------------------------


def backfill_empire_palace(
    *,
    palace_service: Any = None,
    force: bool = False,
    timeout_s: float = _BACKFILL_TIMEOUT_S,
) -> dict:
    """Project empire.sqlite3's PUBLIC global rows into the empire palace.

    One-shot + stamped: a stamp file inside the palace dir marks completion;
    re-runs return ``already_done`` unless ``force=True``. Idempotent by
    construction — drawer ids derive from law/skill ids, and add_drawer
    upserts, so a forced re-run rewrites the same drawers.

    Souls never enter: the row readers admit only ``global_law`` and
    PUBLIC ``empire_skills`` rows (see _public_empire_skill_rows).

    ``palace_service`` overrides the lazy singleton (tests pass a stub).
    Best-effort per row; failures are tallied, never raised.
    """
    stamp = _backfill_stamp_path()
    if stamp.is_file() and not force:
        try:
            prior = json.loads(stamp.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
        return {"ok": True, "already_done": True, **prior}

    service = palace_service if palace_service is not None else get_empire_palace()
    if service is None:
        return {"ok": False, "reason": "mempalace_unavailable"}

    if palace_service is None:
        created = ensure_empire_palace_created()
        if not created.get("ok"):
            return {"ok": False, "reason": "palace_create_failed", "detail": created}

    add_drawer = getattr(service, "add_drawer", None)
    if add_drawer is None:
        return {"ok": False, "reason": "no_add_drawer_surface"}

    def _do() -> dict:
        hub_ctx = _empire_hub_ctx("backfill")
        stats = {"laws": 0, "skills": 0, "failed": 0}
        for law in _active_global_law_rows():
            lid = str(law.get("law_id") or "").strip()
            content = str(law.get("content") or "").strip()
            if not lid or not content:
                continue
            try:
                _add_drawers_for(
                    add_drawer,
                    wing=EMPIRE_LAW_WING,
                    room=_safe_room(str(law.get("kind") or ""), "law"),
                    content=content,
                    unit_id=f"global:{lid}",
                    drawer_id=law_drawer_id(lid),
                    added_by="empire_backfill",
                    hub_ctx=hub_ctx,
                )
                stats["laws"] += 1
            except Exception:
                stats["failed"] += 1
        for skill in _public_empire_skill_rows():
            sid = str(skill.get("skill_id") or "").strip()
            content = str(skill.get("content_text") or "").strip()
            if not sid or not content:
                continue
            try:
                _add_drawers_for(
                    add_drawer,
                    wing=EMPIRE_SKILL_WING,
                    room=_safe_room(str(skill.get("kind") or ""), "skill"),
                    content=content,
                    unit_id=f"skill:{sid}",
                    drawer_id=skill_drawer_id(sid),
                    added_by="empire_backfill",
                    hub_ctx=hub_ctx,
                )
                stats["skills"] += 1
            except Exception:
                stats["failed"] += 1
        return stats

    stats, timed_out = _run_timeboxed(_do, timeout_s)
    if timed_out:
        return {"ok": False, "reason": "timeout", "timed_out": True}
    if stats is None:
        return {"ok": False, "reason": "backfill_failed"}

    from datetime import datetime, timezone

    receipt = {
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "palace_path": str(empire_palace_path()),
        **stats,
    }
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        stamped = True
    except OSError:
        stamped = False
    return {"ok": True, "already_done": False, "stamped": stamped, **receipt}


# ---------------------------------------------------------------------------
# (c) Promote leg — the promoted row lands in the empire palace
# ---------------------------------------------------------------------------


def ingest_promoted_law(
    *,
    law_id: str,
    kind: str,
    content: str,
    palace_service: Any = None,
    timeout_s: float = 5.0,
) -> dict:
    """Best-effort empire-palace ingest of a freshly promoted law row.

    FAIL-QUIET BY CONTRACT: never raises — memory_promote must never fail
    (or hang: timeboxed) over a palace hiccup; the canonical global_law row
    is already sealed. Returns a small receipt either way.
    """
    try:
        lid = (law_id or "").strip()
        body = (content or "").strip()
        if not lid or not body:
            return {"ok": False, "reason": "empty_law"}
        # The promote leg never BOOTSTRAPS the palace — provisioning (with
        # its identity-recording creation + cold model load) belongs to
        # ensure_empire_palace_created/backfill. Until the operator has
        # provisioned one, promotion skips instantly.
        if palace_service is None and not empire_palace_path().is_dir():
            return {"ok": False, "reason": "palace_not_provisioned"}
        service = (
            palace_service if palace_service is not None else get_empire_palace()
        )
        if service is None:
            return {"ok": False, "reason": "mempalace_unavailable"}
        add_drawer = getattr(service, "add_drawer", None)
        if add_drawer is None:
            return {"ok": False, "reason": "no_add_drawer_surface"}

        def _do() -> dict:
            _add_drawers_for(
                add_drawer,
                wing=EMPIRE_LAW_WING,
                room=_safe_room(kind, "law"),
                content=body,
                unit_id=f"global:{lid}",
                drawer_id=law_drawer_id(lid),
                added_by="memory_promote",
                hub_ctx=_empire_hub_ctx("promote"),
            )
            return {"ok": True, "drawer_id": law_drawer_id(lid)}

        result, timed_out = _run_timeboxed(_do, timeout_s)
        if timed_out:
            return {"ok": False, "reason": "timeout"}
        if not isinstance(result, dict):
            return {"ok": False, "reason": "ingest_failed"}
        return result
    except Exception:  # noqa: BLE001 — fail-quiet is the contract
        logger.debug("ingest_promoted_law failed", exc_info=True)
        return {"ok": False, "reason": "ingest_failed"}


# ---------------------------------------------------------------------------
# (d) Retrieval lane — empire semantic hits for kingdom discovery
# ---------------------------------------------------------------------------


def empire_semantic_hits(
    prompt: str,
    *,
    limit: int = 2,
    max_distance: float = 0.85,
    palace_service: Any = None,
    timeout_s: float = _SEARCH_TIMEOUT_S,
) -> list[dict]:
    """Timeboxed empire-palace search → terse hit dicts for the kingdom
    retrieval lanes. Never raises; [] on any failure. NEVER creates the
    palace — a machine without an empire palace pays only a stat call.

    Each hit: {"path": "empire:<unit-or-drawer>", "why": snippet,
    "tier": "empire", "wing": ...}.
    """
    try:
        text = (prompt or "").strip()
        if not text or limit <= 0:
            return []
        if not empire_palace_path().is_dir():
            return []  # read path must never create the palace
        service = (
            palace_service if palace_service is not None else get_empire_palace()
        )
        if service is None:
            return []

        def _do() -> Any:
            return service.search(
                query=text,
                limit=limit,
                max_distance=max_distance,
                hub_ctx=_empire_hub_ctx("search"),
            )

        result, timed_out = _run_timeboxed(_do, timeout_s)
        if timed_out or result is None:
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for hit in getattr(result, "hits", ()) or ():
            unit = str(getattr(hit, "unit_id", "") or "")
            drawer = str(getattr(hit, "drawer_id", "") or "")
            key = unit or drawer
            if not key or key in seen:
                continue
            seen.add(key)
            snippet = str(getattr(hit, "snippet", "") or "").strip()
            out.append(
                {
                    "path": f"empire:{key}",
                    "why": snippet[:120] or "empire knowledge relevant to this prompt",
                    "tier": "empire",
                    "wing": str(getattr(hit, "wing", "") or ""),
                }
            )
            if len(out) >= limit:
                break
        return out
    except Exception:  # noqa: BLE001 — retrieval lane degrades to []
        return []
