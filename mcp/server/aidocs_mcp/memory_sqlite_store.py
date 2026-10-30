"""Memory sqlite storage — sqlite-canonical (Phase 8 flipped 2026-05-19).

Phase 8 (2026-05-15): introduced the sqlite-canonical memory_index table
+ dual-read path so the goggles could query sqlite OR file-backed memory.

PHASE 8 FLIP (2026-05-19): per /goal "Flip AIDOCS durable memory from
file-canonical to sqlite-canonical", the switch is now thrown. The
sqlite ``memory_index`` table is the authoritative durable-memory
store for doctrine / rules / system invariants / specs / personalities.

NO-FILE-LAYER SEAL (2026-06): the markdown layer is fully retired —
``.MEMORY/**/*.md`` is neither read nor written at runtime/bootstrap.

  - memory_capture writes the canonical sqlite row ONLY. ``export_to_markdown``
    was REMOVED 2026-05-21 — AIDOCS never writes ``.MEMORY/*.md`` exports.
  - sync_memory_files is an inert NO-OP; there is NO implicit markdown
    seed-import. A pre-existing markdown tree is absorbed ONLY by the
    EXPLICIT operator-only ``migrate_markdown_to_sqlite`` (the sole durable-
    memory markdown reader). Factory defaults ship as a SQLite seed DB
    (``seed/project_memory.sqlite3``), not markdown.
  - canonical_rows projects from memory_index instead of memory_files
    (see canonical_taxonomy.CANONICAL_VIEW_DDL).
  - MemPalace projects ACTIVE canonical rows into drawers for semantic
    retrieval; it is a projection, not a competing store.

Schema (extended for the flip):
    memory_index (
        path           TEXT PRIMARY KEY,
        kind           TEXT NOT NULL,
        content        TEXT NOT NULL,
        anchors_json   TEXT NOT NULL DEFAULT '[]',
        superseded_by  TEXT,
        source         TEXT NOT NULL DEFAULT 'capture'
            -- 'capture'        : written via memory_capture
            -- 'markdown_seed'  : imported from a .MEMORY/**/*.md file
            -- 'migration'      : copied during the 2026-05-19 flip
            -- 'sovereign'      : conductor-only doctrine
        status         TEXT NOT NULL DEFAULT 'active'
            -- 'active' | 'superseded' | 'removed'
        title          TEXT,
        checksum       TEXT NOT NULL DEFAULT ''
        body_home      TEXT NOT NULL DEFAULT 'staged'
            -- #375 Phase 3: 'staged' (sqlite content is the body's home)
            -- | 'palace' (drawer is home; flipped only on a receipted,
            -- read-back-verified landing — memory_body_staging_store)
        created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )

Read contract:
  read_entry(path) → returns the active sqlite row when present.
  list_entries() → enumerates rows for migration / audit.
  All file-backed reads inside hub.memory fall back to disk when
  the sqlite row is absent (the .MEMORY/ tree may still hold
  seeds the indexer hasn't run yet).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# #755/#756: the ONE canonical connect. memory_anchor_health migrated first
# (a COUNT-only reader, no writes, RUNTIME with no evidence-of-record
# concern). #755 sweep (2026-08-19) routes the other eleven sites here too --
# all correctly closed their handle already (try/finally), so this pass is
# PRAGMA debt only: synchronous=FULL, no busy_timeout, foreign_keys OFF.
# DURABILITY: RUNTIME for every write in this module (upsert_entry,
# mark_superseded, mark_removed, set_body_home, migrate_markdown_to_sqlite).
# memory_index rows are advisory/evidence-tier content (MemoryHint.tier =
# "evidence" -- "memory advises; it is never code truth or project law"),
# not an authorization or consumption mark: losing the last commit to a
# power cut loses (or un-tombstones) a memory hint, which is recoverable by
# recapture/re-running the migrator, not a security or audit-trail hole --
# unlike anchor_store/token/oauth/palace_control, none of which this module
# touches.
from ._sqlite_connect import connect as _canonical_connect


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def memory_unit_id(path: str) -> str:
    """Canonical MemPalace unit_id for a memory_index path."""
    return f"memory:{path}"


def memory_drawer_id(path: str) -> str:
    """Deterministic MemPalace drawer_id for a memory_index path.

    palace_ingest_from_canonical passes this to add_drawer so the drawer's
    chroma id is reproducible from the path alone. Retirement can then mark
    the exact drawer 'deleted' (PalaceStaleSignals, keyed by drawer_id)
    WITHOUT a metadata lookup, and PalaceService.search — which reads the
    same id and consults stale_signals — hides it.
    """
    return f"memdrawer:{path}"


def palace_stale_signals_db_path(project_root: Path) -> Path:
    """Shared location for the palace staleness tracker. Used by both the
    writer (retirement propagation here) and the reader (the HubContext
    stale_signals attached in palace_hub_extension), so they agree.
    """
    return project_root / ".MEMORY" / ".index" / "palace_stale_signals.sqlite3"


def palace_collection_path(project_root: Path) -> Path:
    """Chroma palace directory (mirrors palace_hub_extension's resolver)."""
    return project_root / ".MEMORY" / "palace"


class PalaceDrawerReader:
    """#202 slice 3 — AIDOCS-side drawer-content adapter over the mempalace
    PUBLIC seam (``mempalace.palace.get_collection``, the same seam
    :func:`_legacy_drawer_ids` already reads through — no chroma internals,
    no mempalace edits). Exposes the ``get_drawer_content(drawer_id=...)``
    surface that ``MemoryStore.read_memory``'s drawer-first hydration rides
    on, turning the dormant flip read-half LIVE for PalaceService installs
    (the service itself deliberately has no full-content read — RFC 003
    §8.4 keeps full drawers off the tool surface; this adapter is an
    AIDOCS-internal caller, exactly the sanctioned consumer class).

    Fail-quiet by contract: any miss/error returns ``None`` and the caller
    serves the sqlite fallback body — a drawer-first read is never weaker
    than sqlite-only. Lifecycle filtering happens on the INDEX before any
    drawer read (see read_memory), so retirement is never weakened either.
    """

    def __init__(self, project_root: Path):
        self._project_root = Path(project_root)

    def get_drawer_content(self, *, drawer_id: str) -> str | None:
        try:
            pp = palace_collection_path(self._project_root)
            if not pp.is_dir():
                return None
            from mempalace.palace import get_collection

            col = get_collection(str(pp), create=False)
            if col is None:
                return None
            result = col.get(ids=[str(drawer_id)], include=["documents"])
            docs = (
                result.get("documents")
                if isinstance(result, dict)
                else getattr(result, "documents", [])
            )
            if not docs:
                return None
            body = docs[0]
            return body if isinstance(body, str) and body else None
        except Exception:
            return None


def _legacy_drawer_ids(
    project_root: Path,
    path: str,
) -> tuple[list[str], bool]:
    """Find LEGACY (random) MemPalace drawer ids for ``path``.

    Older ingests used ``drawer_<random>`` ids, so a deterministic
    mark_deleted misses them. Look the drawers up by metadata:
    ``unit_id == memory:<path>`` OR ``source_file == <path>``, excluding
    the deterministic id.

    Returns ``(legacy_ids, lookup_ok)``. ``lookup_ok=False`` ONLY when the
    Chroma collection genuinely could not be consulted (chroma import
    failure / open error) — the caller then emits a degraded lag event so
    the operator knows old drawers may still surface. A missing palace dir
    is "available but empty" → ``([], True)``.
    """
    pp = palace_collection_path(project_root)
    if not pp.exists():
        return [], True
    try:
        from mempalace.palace import get_collection
    except Exception:
        return [], False
    try:
        col = get_collection(str(pp), create=False)
    except Exception:
        return [], False
    if col is None:
        return [], False
    det = memory_drawer_id(path)
    found: set[str] = set()
    # Two equality lookups (avoids backend $or operator-support concerns).
    for where in ({"unit_id": memory_unit_id(path)}, {"source_file": path}):
        try:
            got = col.get(where=where)
        except Exception:
            return [], False
        ids = got.get("ids") if got is not None else None
        for did in ids or []:
            sid = str(did)
            if sid and sid != det:
                found.add(sid)
    return sorted(found), True


def _emit_palace_retirement_lag(
    project_root: Path,
    path: str,
    *,
    reason: str,
) -> None:
    """Forensic degraded event: the deterministic drawer was retired but
    the LEGACY-drawer lookup could not run, so old random-id drawers for
    this path may still surface. Best-effort.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="palace_retirement_legacy_lookup_lag",
            source_kind="memory_retirement",
            capability_name="memory_retirement",
            action_kind="retire",
            target_entity=path[:300],
            status="degraded",
            payload={
                "path": path,
                "unit_id": memory_unit_id(path),
                "deterministic_drawer_id": memory_drawer_id(path),
                "deterministic_marked": True,
                "legacy_lookup_ok": False,
                "reason": reason,
                "note": "legacy random-id drawers may still surface via "
                "ai_palace_search until backfill",
            },
        )
    except Exception:
        pass


def _retire_palace_drawer(project_root: Path, path: str) -> None:
    """Best-effort: mark the MemPalace drawer(s) for ``path`` deleted so
    retired memory stops surfacing through ai_palace_search.

    Marks BOTH the deterministic drawer id (memdrawer:<path>) AND any
    LEGACY random-id drawers found by metadata lookup. The deterministic
    mark is pure sqlite (PalaceStaleSignals); the legacy lookup consults
    Chroma and, if unavailable, emits a degraded lag event rather than
    silently succeeding. Never raises — propagation must not break a
    canonical retirement.
    """
    try:
        from .palace_stale_signals import PalaceStaleSignals

        ss = PalaceStaleSignals(palace_stale_signals_db_path(project_root))
    except Exception:
        return
    unit_id = memory_unit_id(path)
    # 1. Deterministic drawer (going-forward ids).
    try:
        ss.mark_deleted(drawer_id=memory_drawer_id(path), unit_id=unit_id)
    except Exception:
        return
    # 2. Legacy random-id drawers (pre-a99f2379 ingests).
    legacy, ok = _legacy_drawer_ids(project_root, path)
    if not ok:
        _emit_palace_retirement_lag(
            project_root,
            path,
            reason="chroma_lookup_unavailable",
        )
        return
    for did in legacy:
        try:
            ss.mark_deleted(drawer_id=did, unit_id=unit_id)
        except Exception:
            pass


def recent_palace_retirement_lag_events(
    project_root: Path,
    *,
    limit: int = 50,
) -> list[dict]:
    """Recent palace_retirement_legacy_lookup_lag rows — a memory was
    retired but the legacy-drawer lookup couldn't run, so old random-id
    drawers may still surface until backfill.
    """
    return _recent_lag_events(
        project_root,
        "palace_retirement_legacy_lookup_lag",
        limit=limit,
    )


def backfill_legacy_memory_drawers(
    project_root: Path,
    palace_service,
    *,
    hub_ctx=None,
    dry_run: bool = False,
) -> dict:
    """Reconcile pre-a99f2379 random-id memory drawers to the deterministic
    scheme.

    For EVERY memory_index row (active + retired):
      - mark any legacy random-id drawers (by unit_id / source_file
        metadata) DELETED in palace_stale_signals;
      - for ACTIVE, non-superseded rows, (re)ingest a deterministic drawer
        (memdrawer:<path>) via add_drawer so the live memory is searchable
        under the stable id.

    ``dry_run=True`` performs the read-only lookups and COUNTS what would
    change (retired_legacy = legacy ids found, reingested = active rows)
    without writing any stale signal, re-ingesting, or emitting lag events.

    Returns ``{scanned, retired_legacy, reingested, failed, lookup_lag,
    dry_run}``. Best-effort per row; a lookup failure increments
    ``lookup_lag`` (and, when not dry_run, emits the degraded event).
    ``palace_service=None`` skips re-ingest (signals still written).
    """
    stats = {
        "scanned": 0,
        "retired_legacy": 0,
        "reingested": 0,
        "failed": 0,
        "lookup_lag": 0,
        "dry_run": bool(dry_run),
    }
    try:
        from .palace_stale_signals import PalaceStaleSignals

        ss = PalaceStaleSignals(palace_stale_signals_db_path(project_root))
    except Exception:
        return stats
    entries = list_entries(
        project_root,
        include_superseded=True,
        include_inactive=True,
    )
    add_drawer = getattr(palace_service, "add_drawer", None)
    for entry in entries:
        stats["scanned"] += 1
        path = entry.path
        unit_id = memory_unit_id(path)
        is_active = not entry.superseded_by and entry_status(project_root, path) == "active"
        legacy, ok = _legacy_drawer_ids(project_root, path)
        if not ok:
            stats["lookup_lag"] += 1
            if not dry_run:
                _emit_palace_retirement_lag(
                    project_root,
                    path,
                    reason="backfill_chroma_lookup_unavailable",
                )
            continue
        for did in legacy:
            if dry_run:
                stats["retired_legacy"] += 1
                continue
            try:
                ss.mark_deleted(drawer_id=did, unit_id=unit_id)
                stats["retired_legacy"] += 1
            except Exception:
                stats["failed"] += 1
        # Config-memory policy (clause 2): denied .aidocs configs never re-ingest,
        # AND any existing drawer for them is retired (soft-deleted via stale
        # signal) so prior ingests stop surfacing in search.
        if is_active and not _config_memory_ingest_allowed(path):
            stats["skipped"] = stats.get("skipped", 0) + 1
            if not dry_run:
                try:
                    ss.mark_deleted(drawer_id=memory_drawer_id(path), unit_id=unit_id)
                except Exception:
                    pass
            continue
        if is_active and (dry_run or add_drawer is not None):
            if dry_run:
                stats["reingested"] += 1
                continue
            wing, room = _wing_room_for(entry)
            try:
                # Chunking (clause 1) + force-anchor via the shared ingest helper:
                # an oversized memory becomes heading-aware child drawers instead
                # of being refused by sanitize_content.
                _ingest_memory_drawers(
                    add_drawer,
                    entry,
                    wing,
                    room,
                    added_by="legacy_backfill",
                    hub_ctx=hub_ctx,
                )
                # The deterministic drawer is the live one — clear any stale
                # signal left on it from a prior retirement of the same id.
                try:
                    ss.mark_active(drawer_id=memory_drawer_id(path))
                except Exception:
                    pass
                stats["reingested"] += 1
            except Exception:
                stats["failed"] += 1
    return stats


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_index (
            path TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            anchors_json TEXT NOT NULL DEFAULT '[]',
            superseded_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_index_kind ON memory_index(kind)")
    # 2026-05-19 flip: extend with taxonomy columns. Idempotent
    # ADD COLUMN — sqlite raises only if the column already exists,
    # so we use a try/except per column.
    for col_ddl in (
        "source TEXT NOT NULL DEFAULT 'capture'",
        "status TEXT NOT NULL DEFAULT 'active'",
        "title TEXT",
        "checksum TEXT NOT NULL DEFAULT ''",
        # #202 slice 1: explicit index→drawer pointer. Deterministic
        # (memory_drawer_id(path)) so legacy rows derive it on read.
        "drawer_id TEXT NOT NULL DEFAULT ''",
        # #375 Phase 3 (memory-home ruling 2026-07-19): where the BODY's
        # truth lives. 'staged' = sqlite content is home (default; every
        # legacy row); 'palace' = the drawer is home, flipped ONLY by the
        # receipted verify-then-retire projector (memory_body_staging_store)
        # / the one-shot migrator. Reads serve palace-home bodies drawer-
        # first with the sqlite content as fallback for staged rows.
        "body_home TEXT NOT NULL DEFAULT 'staged'",
    ):
        try:
            conn.execute(f"ALTER TABLE memory_index ADD COLUMN {col_ddl}")
        except sqlite3.OperationalError:
            # Column already exists — idempotent path.
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_index_status ON memory_index(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_index_source ON memory_index(source)")


@dataclass
class MemoryEntry:
    path: str
    kind: str
    content: str
    anchors: list[str]
    superseded_by: str = ""
    # Pointer metadata is carried by the canonical facade so read-side
    # surfacing never needs an inline query or the private memory body.
    title: str = ""
    # #202: palace drawer pointer — stored on new rows, derived
    # (memory_drawer_id(path)) for legacy rows. Never empty on a read.
    drawer_id: str = ""
    # #375 Phase 3: 'staged' (sqlite body is home) | 'palace' (drawer is
    # home, receipted). Reads use this to know where truth is.
    body_home: str = "staged"


def upsert_entry(
    project_root: Path,
    *,
    path: str,
    kind: str,
    content: str,
    anchors: list[str] | None = None,
    source: str = "capture",
    status: str = "active",
    title: str | None = None,
    checksum: str = "",
) -> bool:
    """Insert or update a memory entry in the sqlite index. Returns True
    on success. Best-effort: returns False on any sqlite error.

    Extended 2026-05-19 with the canonical-taxonomy columns
    (source, status, title, checksum). Defaults match memory_capture's
    canonical write-path (``source='capture'``, ``status='active'``).
    Migration callers pass ``source='migration'``; the legacy indexer
    passes ``source='markdown_seed'``.
    """
    db = _db_path(project_root)
    # CREATE-WRITE path: ensure the canonical index dir exists so capturing
    # memory into a not-yet-bootstrapped project succeeds (the markdown dir
    # is created by capture_memory, but .MEMORY/.index is not). This is the
    # only write helper that creates state — update-only helpers
    # (mark_superseded / mark_removed) still no-op on an absent DB.
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_table(conn)
            conn.execute(
                """
                INSERT INTO memory_index
                    (path, kind, content, anchors_json,
                     source, status, title, checksum, drawer_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    kind = excluded.kind,
                    content = excluded.content,
                    anchors_json = excluded.anchors_json,
                    source = excluded.source,
                    status = excluded.status,
                    title = excluded.title,
                    checksum = excluded.checksum,
                    drawer_id = excluded.drawer_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    path,
                    kind,
                    content,
                    json.dumps(list(anchors or [])),
                    source,
                    status,
                    title,
                    checksum,
                    memory_drawer_id(path),
                ),
            )
            conn.commit()
            return True
    except sqlite3.Error:
        return False


def mark_superseded(
    project_root: Path,
    *,
    path: str,
    superseded_by: str,
) -> bool:
    """Mark a memory entry as superseded by another.

    Also retires the corresponding MemPalace drawer (best-effort) so the
    superseded memory stops surfacing through ai_palace_search — retirement
    must propagate to every surface, not just exact-read/discovery.
    """
    db = _db_path(project_root)
    if not db.is_file():
        return False
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_table(conn)
            cur = conn.execute(
                "UPDATE memory_index SET superseded_by = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE path = ?",
                (superseded_by, path),
            )
            conn.commit()
            updated = (cur.rowcount or 0) > 0
    except sqlite3.Error:
        return False
    if updated:
        _retire_palace_drawer(project_root, path)
    return updated


def mark_removed(project_root: Path, *, path: str) -> bool:
    """Mark a memory entry as removed (status='removed') — a tombstone, not
    a physical delete. Retires the MemPalace drawer too (best-effort).
    """
    db = _db_path(project_root)
    if not db.is_file():
        return False
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_table(conn)
            cur = conn.execute(
                "UPDATE memory_index SET status = 'removed', "
                "updated_at = CURRENT_TIMESTAMP WHERE path = ?",
                (path,),
            )
            conn.commit()
            updated = (cur.rowcount or 0) > 0
    except sqlite3.Error:
        return False
    if updated:
        _retire_palace_drawer(project_root, path)
    return updated


def entry_status(project_root: Path, path: str) -> str | None:
    """Return the lifecycle status of a memory_index row: 'active',
    'superseded', or 'removed' — or None when the path is ABSENT from the
    index entirely. Superseded rows report 'superseded' even when their
    raw ``status`` column is still 'active' (the supersede path sets
    ``superseded_by`` without touching ``status``). Lets the read surface
    distinguish "deliberately inactive" (suppress) from "not indexed yet"
    (legitimate disk fallback).
    """
    db = _db_path(project_root)
    if not db.is_file():
        return None
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT COALESCE(status, 'active'), "
                "COALESCE(superseded_by, '') FROM memory_index WHERE path = ?",
                (path,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    status = str(row[0] or "active")
    if str(row[1] or ""):
        return "superseded"
    return status


def read_entry(
    project_root: Path,
    path: str,
    *,
    include_inactive: bool = False,
) -> MemoryEntry | None:
    """Read one entry by path. Returns None when absent.

    Lifecycle (Phase-8): by default a SUPERSEDED or non-'active' (removed)
    row is treated as absent and returns None — matching list_entries'
    default and the no-self-surfacing-of-dead-memory contract. Pass
    ``include_inactive=True`` (audit/debug only) to read the row regardless
    of lifecycle status.
    """
    db = _db_path(project_root)
    if not db.is_file():
        return None
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT path, kind, content, anchors_json, "
                "COALESCE(superseded_by, ''), COALESCE(status, 'active'), "
                "COALESCE(title, ''), COALESCE(drawer_id, ''), "
                "COALESCE(body_home, 'staged') "
                "FROM memory_index WHERE path = ?",
                (path,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    superseded_by = str(row[4] or "")
    status = str(row[5] or "active")
    if not include_inactive and (superseded_by or status != "active"):
        # Deliberately inactive memory must not surface through the
        # canonical read path. Audit/debug callers pass include_inactive.
        return None
    try:
        anchors = json.loads(row[3] or "[]")
        if not isinstance(anchors, list):
            anchors = []
    except Exception:
        anchors = []
    return MemoryEntry(
        path=str(row[0]),
        kind=str(row[1]),
        content=str(row[2]),
        anchors=[str(a) for a in anchors],
        superseded_by=superseded_by,
        title=str(row[6] or ""),
        drawer_id=str(row[7] or "") or memory_drawer_id(str(row[0])),
        body_home=str(row[8] or "staged"),
    )


def set_body_home(project_root: Path, path: str, home: str) -> bool:
    """Flip the body-home marker for one memory_index row. Only the
    receipted projector / migrator may flip to 'palace' (verify-then-mark
    — the caller must hold a landing receipt first)."""
    if home not in ("staged", "palace"):
        raise ValueError(f"invalid body_home {home!r}; must be 'staged' or 'palace'")
    db = _db_path(project_root)
    if not db.is_file():
        return False
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_table(conn)
            cur = conn.execute(
                "UPDATE memory_index SET body_home = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE path = ?",
                (home, path),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
    except sqlite3.Error:
        return False


def get_body_home(project_root: Path, path: str) -> str | None:
    """Body-home marker for one row ('staged' | 'palace'), None when the
    path is absent from the index."""
    db = _db_path(project_root)
    if not db.is_file():
        return None
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT COALESCE(body_home, 'staged') FROM memory_index WHERE path = ?",
                (path,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None else None


def list_entries(
    project_root: Path,
    *,
    include_superseded: bool = False,
    include_inactive: bool = False,
) -> list[MemoryEntry]:
    """List entries. Active rows by default; pass include_superseded /
    include_inactive for audits / migration verification.

    Phase-8 hardening (2026-05-19): the default filter now excludes
    BOTH ``superseded_by != NULL`` rows AND ``status != 'active'``
    rows. MemPalace ingestion calls this with defaults, so removed
    or superseded canonical rows never get re-ingested.
    """
    db = _db_path(project_root)
    if not db.is_file():
        return []
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_table(conn)
            sql = (
                "SELECT path, kind, content, anchors_json, "
                "COALESCE(superseded_by, ''), COALESCE(title, '') FROM memory_index"
            )
            where: list[str] = []
            if not include_superseded:
                where.append("(superseded_by IS NULL OR superseded_by = '')")
            if not include_inactive:
                where.append("status = 'active'")
            if where:
                sql += " WHERE " + " AND ".join(where)
            rows = conn.execute(sql).fetchall()
    except sqlite3.Error:
        return []
    out: list[MemoryEntry] = []
    for row in rows:
        try:
            anchors = json.loads(row[3] or "[]")
            if not isinstance(anchors, list):
                anchors = []
        except Exception:
            anchors = []
        out.append(
            MemoryEntry(
                path=str(row[0]),
                kind=str(row[1]),
                content=str(row[2]),
                anchors=[str(a) for a in anchors],
                superseded_by=str(row[4] or ""),
                title=str(row[5] or ""),
                drawer_id=memory_drawer_id(str(row[0])),
            ),
        )
    return out


# ---------------------------------------------------------------------------
# Phase-8 flip helpers (2026-05-19)
# ---------------------------------------------------------------------------


_ARCHIVE_PREFIXES: tuple[str, ...] = ("archive/", "sessions/", ".index/")

# Old-kingdom drain (#142, 2026-07-18): a drained mirror carries ONLY this
# header naming its canonical memory_index row. The migrator must SKIP such
# files — re-importing a tombstone stub would clobber the canonical row.
# Census pin: mcp/tests/host/test_old_kingdom_drain.py.
TOMBSTONE_MARKER = "# TOMBSTONE — drained old-kingdom mirror"
_TRACKED_SUFFIXES: tuple[str, ...] = (".md", ".aidocs")
_SOVEREIGN_PATHS: frozenset[str] = frozenset(
    {
        "skills/head-conductor.md",
        "skills/co-conductor.md",
    },
)


def _kind_for_path(rel: str) -> str:
    """Match the legacy IndexStore._kind_for taxonomy so memory_index
    rows are interchangeable with what the markdown indexer would
    produce.
    """
    head = rel.split("/", 1)[0] if "/" in rel else ""
    if head == "domains":
        return "domain"
    if head == "rules":
        return "rule"
    if head == "system":
        return "system"
    if head == "specs":
        return "spec"
    if head == "roadmaps":
        return "roadmap"
    if head == "daily":
        return "daily"
    if head == ".aidocs":
        return "aidocs"
    if head == "sessions":
        return "session"
    if head == "config":
        return "config"
    if head == "skills":
        return "sovereign"
    if head == "related-projects":
        return "related_project"
    return "memory"


def migrate_markdown_to_sqlite(project_root: Path) -> dict:
    """EXPLICIT, OPERATOR-ONLY one-shot legacy importer (SQLite-only doctrine,
    2026-06). Walks ``.MEMORY/**/*.{md,aidocs}`` and ensures each non-archive,
    non-session file has a row in the canonical ``memory_index``.

    This is the SOLE path that reads loose ``.MEMORY`` markdown into the index.
    The RUNTIME never invokes it implicitly — reads/search/discovery/gates serve
    canonical ``memory_index`` only, ``sync_memory_files`` is a no-op, and there
    is no disk/frontmatter fallback. An operator runs this once to absorb a
    pre-existing markdown tree; afterward the markdown is inert.

    Idempotent — re-running is a no-op for paths already present and updates
    content+checksum for paths whose markdown drifted. Returns
    ``{inserted, updated, unchanged, skipped}`` for operator visibility.

    Behavior:
      - Files under ``archive/`` / ``sessions/`` / ``.index/`` are skipped.
      - Sovereign paths get ``source='sovereign'``; everything else
        ``source='migration'``, ``status='active'``.
    """
    import hashlib

    memory_root = project_root / ".MEMORY"
    if not memory_root.is_dir():
        return {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    db = _db_path(project_root)
    if not db.parent.is_dir():
        return stats
    with _canonical_connect(str(db), row_factory=False) as conn:
        _ensure_table(conn)
        # Snapshot existing checksums to detect drift.
        existing: dict[str, tuple[str, str]] = {}
        for r in conn.execute("SELECT path, COALESCE(checksum, ''), content FROM memory_index"):
            existing[str(r[0])] = (str(r[1]), str(r[2]))

        for path in sorted(memory_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _TRACKED_SUFFIXES:
                continue
            rel = path.relative_to(memory_root).as_posix()
            if any(rel.startswith(p) for p in _ARCHIVE_PREFIXES):
                stats["skipped"] += 1
                continue
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except Exception:
                stats["skipped"] += 1
                continue
            if text.lstrip().startswith(TOMBSTONE_MARKER):
                # Drained old-kingdom mirror (#142) — canonical content
                # lives in the memory_index row; never absorb the stub.
                stats["skipped"] += 1
                continue
            checksum = hashlib.sha256(raw).hexdigest()
            kind = _kind_for_path(rel)
            source = "sovereign" if rel in _SOVEREIGN_PATHS else "migration"
            # Title heuristic: first '#' line, otherwise None.
            title: str | None = None
            for line in text.splitlines():
                line_s = line.strip()
                if line_s.startswith("#"):
                    title = line_s.lstrip("#").strip() or None
                    break

            prior = existing.get(rel)
            if prior is None:
                stats["inserted"] += 1
            elif prior[0] == checksum:
                stats["unchanged"] += 1
                continue
            else:
                stats["updated"] += 1

            conn.execute(
                """
                INSERT INTO memory_index
                    (path, kind, content, anchors_json,
                     source, status, title, checksum)
                VALUES (?, ?, ?, '[]', ?, 'active', ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    kind = excluded.kind,
                    content = excluded.content,
                    source = CASE
                        -- Preserve 'capture' provenance: a row that was
                        -- captured shouldn't get demoted to 'migration'
                        -- by a later re-run.
                        WHEN memory_index.source = 'capture' THEN 'capture'
                        WHEN memory_index.source = 'sovereign' THEN 'sovereign'
                        ELSE excluded.source
                    END,
                    status = excluded.status,
                    title = excluded.title,
                    checksum = excluded.checksum,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (rel, kind, text, source, title, checksum),
            )
        conn.commit()
    return stats


# export_to_markdown was REMOVED 2026-05-21 (no-file-layer doctrine).
# Memory is canonical in sqlite ONLY; AIDOCS never writes .MEMORY/*.md
# exports. Human-readable views are rendered from sqlite on demand by the
# dashboard; user downloads are temporary artifacts AIDOCS never stores or
# re-reads.


_LOCUS_SAFE_RE = __import__("re").compile(r"[^A-Za-z0-9_-]+")


def _safe_locus(name: str, *, default: str) -> str:
    """Coerce a derived wing/room into MemPalace's sanitize_name charset.

    Path separators and other invalid chars (e.g. the leading '.' in a
    '.aidocs/…' path, or the '/' in a nested room) collapse to '-', so the
    drawer ingest cannot be silently refused by sanitize_name — every active
    memory becomes a drawer. Falls back to ``default`` if nothing is left.
    """
    cleaned = _LOCUS_SAFE_RE.sub("-", (name or "")).strip("-")
    return cleaned or default


def _wing_room_for(entry) -> tuple[str, str]:
    """Map a memory_index path → (wing, room) per MemPalace's loci model.

    Wing = top-level folder; Room = remaining path minus extension. Both are
    coerced into sanitize_name's safe charset (see _safe_locus) so dotted or
    nested paths ('.aidocs/personalities/cat.md') still ingest.
    Top-level files (INDEX.md, CHANGELOG.md) land in wing='root'.
    """
    path = entry.path
    if "/" not in path:
        stem = path.rsplit(".", 1)[0]
        return ("root", _safe_locus(stem, default="root"))
    wing, _, rest = path.partition("/")
    stem = rest.rsplit(".", 1)[0]
    return (_safe_locus(wing, default="root"), _safe_locus(stem, default="root"))


# Drawer content cap (mirrors mempalace sanitize_content default). A memory over
# this is split into heading-aware child drawers so it ingests + stays
# semantically searchable instead of failing sanitize_content.
_DRAWER_CONTENT_CAP = 100_000
_CHUNK_TARGET_CHARS = 6_000
_CHUNK_MAX_CHARS = 8_000


def _chunk_memory_content(content: str) -> list[str]:
    """Split ``content`` into heading-aware chunks ONLY when it exceeds the
    drawer content cap; otherwise return ``[content]`` unchanged so normal-sized
    memories keep their single deterministic drawer. Chunks target ~6KB (<= 8KB),
    split before markdown headings so each chunk is a coherent section; a section
    larger than the max is hard-split by size.
    """
    import re

    text = content or ""
    if len(text) <= _DRAWER_CONTENT_CAP:
        return [text]
    sections = [p for p in re.split(r"(?m)^(?=#{1,6}\s)", text) if p]
    if not sections:
        sections = [text]
    chunks: list[str] = []
    buf = ""
    for sec in sections:
        if len(sec) > _CHUNK_MAX_CHARS:
            if buf:
                chunks.append(buf)
                buf = ""
            for j in range(0, len(sec), _CHUNK_MAX_CHARS):
                chunks.append(sec[j : j + _CHUNK_MAX_CHARS])
            continue
        if buf and len(buf) + len(sec) > _CHUNK_TARGET_CHARS:
            chunks.append(buf)
            buf = sec
        else:
            buf += sec
    if buf:
        chunks.append(buf)
    return chunks or [text]


def _ingest_memory_drawers(
    add_drawer, entry, wing: str, room: str, *, added_by: str, hub_ctx
) -> None:
    """Project one canonical memory into the palace: 1 drawer normally, or N
    child drawers when oversized. Child drawers carry parent_drawer_id /
    chunk_index / chunk_total so search results regroup to the source memory.
    Child ids derive deterministically from the parent memory_drawer_id.
    """
    parent = memory_drawer_id(entry.path)
    unit = memory_unit_id(entry.path)
    chunks = _chunk_memory_content(entry.content)
    if len(chunks) == 1:
        _raise_on_refused(
            add_drawer(
                wing=wing,
                room=room,
                content=entry.content,
                source_file=entry.path,
                unit_id=unit,
                drawer_id=parent,
                added_by=added_by,
                force_extract_anchors=True,
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
                source_file=entry.path,
                unit_id=unit,
                drawer_id=f"{parent}#chunk{i:04d}",
                added_by=added_by,
                force_extract_anchors=True,
                extra_metadata={
                    "parent_drawer_id": parent,
                    "chunk_index": i,
                    "chunk_total": total,
                },
                hub_ctx=hub_ctx,
            )
        )


def _raise_on_refused(result) -> None:
    """PalaceService.add_drawer signals gate/write failure by RETURNING a
    Refused object, not raising — pre-fix an ingest counted a refusal as
    success (silent projection loss). Duck-typed so stubs/None still pass."""
    if type(result).__name__ == "Refused":
        raise RuntimeError(
            "palace add_drawer refused: "
            f"{getattr(result, 'reason', '?')} ({getattr(result, 'detail', '')})"
        )


_CONFIG_PREFIX = ".aidocs/"
# Commissioning KNOWLEDGE docs worth recalling semantically. Everything else
# under .aidocs/ (the inert index.aidocs marker, personality presets, and any
# secret/operator-local config) is denied by default.
_CONFIG_INGEST_ALLOW = (
    ".aidocs/coding-standards",
    ".aidocs/global-instructions",
    ".aidocs/memory-system",
)


def _config_memory_ingest_allowed(path: str) -> bool:
    """EXPLICIT config-memory ingestion policy (clause 2), independent of the
    protected-file security gate. Real memories (not under .aidocs/) always
    ingest. Under .aidocs/, ONLY allow-listed commissioning knowledge docs become
    drawers; everything else — the inert index.aidocs marker, personality
    presets, secret/operator-local config — is DENIED by default, so nothing
    config-ish is silently made searchable. Approving a new config doc is an
    explicit allow-list edit, not an accident of which gate happened to fire.
    """
    norm = (path or "").replace("\\", "/").lstrip("/")
    if not norm.startswith(_CONFIG_PREFIX):
        return True
    low = norm.lower()
    return any(low.startswith(a) for a in _CONFIG_INGEST_ALLOW)


def palace_ingest_from_canonical(
    project_root: Path,
    palace_service,
    *,
    only_kinds: tuple[str, ...] | None = None,
    hub_ctx=None,
) -> dict:
    """Push every active sqlite memory_index row into MemPalace via the
    provided ``palace_service`` (typically ``hub.palace``). Returns
    ``{ingested, skipped, failed, kg_triples}`` for operator visibility.

    #382: this bulk path doubles as the palace-KG BACKFILL adapter — every
    ingested entry's deterministic facts (extract_kg_facts over the
    canonical row) are written to kg_entities/kg_triples too, so the graph
    is derivable from canonical data alone and no longer depends on each
    memory's capture-time async projection having succeeded.

    The contract: read canonical rows from sqlite, NOT from any
    .MEMORY/**/*.md files (there is no markdown layer — it was retired
    under the no-file-layer seal). The palace's source-of-truth pipeline
    starts at memory_index; MemPalace is a semantic PROJECTION over those
    canonical rows, never a competing store.

    Calls ``palace_service.add_drawer(wing, room, content, source_file,
    unit_id, added_by, hub_ctx)`` — the canonical MemPalace write
    surface (``mempalace.service.PalaceService.add_drawer``). ``wing``
    and ``room`` are derived from the memory_index path via
    :func:`_wing_room_for`; ``unit_id`` is the canonical stable_key
    (``memory:<path>``); ``source_file`` is the canonical memory_index
    path (``rules/x.md`` etc.) carried as a stable identifier for
    traceability — NOT a path to any on-disk markdown file.

    Best-effort: failures from add_drawer are caught and tallied as
    ``failed``. ``palace_service=None`` returns zeros (mempalace not
    installed). When the service lacks ``add_drawer`` entirely, every
    entry is reported as failed so callers can see the wiring gap.

    ``only_kinds`` lets callers ingest a subset (e.g. ``('domain',
    'rule')`` when refreshing doctrine). ``hub_ctx`` is forwarded to
    add_drawer; tests pass a contract-compatible stub, production
    passes the real HubContext built by palace_hub_extension.
    """
    stats = {"ingested": 0, "skipped": 0, "failed": 0, "kg_triples": 0}
    if palace_service is None:
        return stats
    entries = list_entries(project_root)
    add_drawer = getattr(palace_service, "add_drawer", None)
    if add_drawer is None:
        stats["failed"] = len(entries)
        return stats
    for entry in entries:
        if only_kinds is not None and entry.kind not in only_kinds:
            stats["skipped"] += 1
            continue
        if entry.superseded_by:
            stats["skipped"] += 1
            continue
        if not _config_memory_ingest_allowed(entry.path):
            stats["skipped"] += 1
            continue
        wing, room = _wing_room_for(entry)
        try:
            _ingest_memory_drawers(
                add_drawer,
                entry,
                wing,
                room,
                added_by="canonical_taxonomy",
                hub_ctx=hub_ctx,
            )
            stats["ingested"] += 1
        except Exception:
            stats["failed"] += 1
            continue
        # #382: the bulk canonical ingest IS the KG backfill adapter.
        # Pre-fix only the capture-time single-row path (palace_ingest_entry)
        # populated kg_entities/kg_triples, so every memory captured before
        # the #206 wiring — or whose async capture-time projection died —
        # stayed OFF the graph forever (the diagnosed thin/empty kingdom
        # KG). Facts derive from the CANONICAL sqlite row, deterministic and
        # idempotent (KG dedup makes re-ingest 0-cost). Best-effort: a KG
        # hiccup never fails the drawer ingest.
        try:
            from .memory_kg_extractor import ingest_kg_for_entry

            stats["kg_triples"] += ingest_kg_for_entry(
                palace_service,
                path=entry.path,
                kind=entry.kind,
                content=entry.content,
                anchors=entry.anchors,
            )
        except Exception:
            pass
    return stats

def palace_ingest_entry(
    project_root: Path,
    palace_service,
    path: str,
    *,
    hub_ctx=None,
) -> bool:
    """Project ONE canonical memory_index row into the palace.

    Capture-time companion to :func:`palace_ingest_from_canonical` —
    called right after memory_capture upserts its row so the palace
    projection stays incremental instead of waiting for a (first-ever
    only) bootstrap sync. Best-effort: returns False on any failure;
    the canonical sqlite row is already durable and the caller surfaces
    the lag, never blocks the capture.
    """
    if palace_service is None:
        return False
    add_drawer = getattr(palace_service, "add_drawer", None)
    if add_drawer is None:
        return False
    entry = read_entry(project_root, path)
    if entry is None or entry.superseded_by:
        return False
    if not _config_memory_ingest_allowed(entry.path):
        return True  # intentionally not a palace drawer (config policy), not a lag
    wing, room = _wing_room_for(entry)
    try:
        _ingest_memory_drawers(
            add_drawer,
            entry,
            wing,
            room,
            added_by="memory_capture",
            hub_ctx=hub_ctx,
        )
    except Exception:
        return False
    # #206: populate the palace KG alongside the drawer projection —
    # entities/relations extracted from the captured memory, bilateral
    # provenance (drawer_id + unit_id). Best-effort: a KG hiccup never
    # fails the ingest (the drawer is already projected).
    try:
        from .memory_kg_extractor import ingest_kg_for_entry

        ingest_kg_for_entry(
            palace_service,
            path=entry.path,
            kind=entry.kind,
            content=entry.content,
            anchors=entry.anchors,
        )
    except Exception:
        pass
    return True


def memory_anchor_health(
    project_root: Path, *, palace_drawers: int | None = None
) -> dict:
    """Operator-visible probe: is the memory-to-code-unit anchor wire feeding, or
    dormant? Surfaces active-memory vs anchored-memory vs total-anchor counts so a
    starved memory_symbol_anchors table (the dormant-wire signature that hid the
    3-of-82 starvation) is impossible to miss.
    """
    out: dict = {
        "active_memories": 0,
        "anchored_memories": 0,
        "total_anchors": 0,
        "palace_drawers": palace_drawers,
        "coverage_pct": 0,
        "wire": "unknown",
    }
    try:
        db = _db_path(project_root)
        # COUNT-only with a short busy timeout — this runs on the ai_palace_status
        # hot path and must NEVER block it (an earlier list_entries-based version
        # could stall status under DB contention with the live server). Three
        # cheap COUNTs, no row loading.
        with _canonical_connect(str(db), timeout=2.0, row_factory=False) as c:
            out["total_anchors"] = c.execute(
                "SELECT COUNT(*) FROM memory_symbol_anchors"
            ).fetchone()[0]
            out["anchored_memories"] = c.execute(
                "SELECT COUNT(DISTINCT drawer_id) FROM memory_symbol_anchors"
            ).fetchone()[0]
            try:
                out["active_memories"] = c.execute(
                    "SELECT COUNT(*) FROM memory_index "
                    "WHERE superseded_by IS NULL OR superseded_by = ''"
                ).fetchone()[0]
            except Exception:
                out["active_memories"] = c.execute(
                    "SELECT COUNT(*) FROM memory_index"
                ).fetchone()[0]
        if out["active_memories"]:
            out["coverage_pct"] = round(
                100 * out["anchored_memories"] / out["active_memories"]
            )
        # With auto-anchoring live a real fraction of memories anchor; near-zero
        # coverage means the wire is dormant again.
        out["wire"] = "live" if out["coverage_pct"] >= 10 else "starved"
    except Exception:
        out["wire"] = "error"
    return out


def _recent_lag_events(
    project_root: Path,
    event_kind: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Generic recent-lag-events query. Backs the
    recent_export_lag_events / recent_route_lag_events /
    recent_anchor_lag_events helpers; same shape, different
    event_kind filter.
    """
    db = _db_path(project_root)
    if not db.is_file():
        return []
    try:
        with _canonical_connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT event_id, target_entity, status, observed_at, "
                "chain_seq, payload_json FROM execution_events "
                "WHERE event_kind = ? "
                "ORDER BY rowid DESC LIMIT ?",
                (event_kind, max(1, int(limit))),
            ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except Exception:
            payload = {}
        out.append(
            {
                "event_id": r["event_id"],
                "path": payload.get("path") or r["target_entity"],
                "observed_at": r["observed_at"],
                "chain_seq": r["chain_seq"],
                "status": r["status"],
                "payload": payload,
            },
        )
    return out


def recent_route_lag_events(
    project_root: Path,
    *,
    limit: int = 50,
) -> list[dict]:
    """Recent memory_route_lag rows — capture succeeded but route
    registration (keyword discovery wiring) failed. Operators
    looking up the memory by keyword may miss it until backfill.
    """
    return _recent_lag_events(
        project_root,
        "memory_route_lag",
        limit=limit,
    )


def recent_anchor_lag_events(
    project_root: Path,
    *,
    limit: int = 50,
) -> list[dict]:
    """Recent memory_anchor_lag rows — capture succeeded but one
    or more symbol anchors failed to register. Symbol-led
    discovery may miss the row until backfill.
    """
    return _recent_lag_events(
        project_root,
        "memory_anchor_lag",
        limit=limit,
    )


def recent_export_lag_events(
    project_root: Path,
    *,
    limit: int = 50,
) -> list[dict]:
    """Query execution_events for recent memory_export_lag rows.

    Dashboard / operator visibility into Phase-8 markdown export lag:
    rows where the canonical sqlite write landed but the markdown
    export at .MEMORY/<path> did not. Returns the most recent first
    (descending chain_seq, capped at ``limit``).

    Each row carries:
      - path           — relative .MEMORY path
      - sqlite_checksum — sha256 of the canonical content
      - error_class    — exception class that broke the export
      - observed_at    — ISO timestamp
      - status         — 'degraded' (constant for this event_kind)

    Empty list when no events / store missing / sqlite hiccup.
    """
    db = _db_path(project_root)
    if not db.is_file():
        return []
    try:
        with _canonical_connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT event_id, target_entity, status, observed_at, "
                "chain_seq, payload_json FROM execution_events "
                "WHERE event_kind = 'memory_export_lag' "
                "ORDER BY rowid DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except Exception:
            payload = {}
        out.append(
            {
                "event_id": r["event_id"],
                "path": payload.get("path") or r["target_entity"],
                "sqlite_checksum": payload.get("sqlite_checksum"),
                "error_class": payload.get("error_class"),
                "observed_at": r["observed_at"],
                "chain_seq": r["chain_seq"],
                "status": r["status"],
            },
        )
    return out
