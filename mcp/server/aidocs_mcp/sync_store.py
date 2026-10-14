"""Conflict-proof cross-agent sync for todo/backlog (memory joins later).

Design (operator 2026-07-07): git is the transport + source-of-truth, but the
physical write target is ONE IMMUTABLE FILE PER EVENT, never a shared append-log
(shared-EOF append is an N-agent merge-conflict trap). git merges disjoint file
ADDITIONS trivially, so N agents on local/web never collide.

  .MEMORY/sync/events/<stream>/<event_id>.json   (immutable; write-once)

SQLite stays a MATERIALIZED VIEW: fold(events) -> rows. Deletes are tombstones.
Conflicts resolve deterministically by (hlc, actor). No binary sqlite in git; no
WAL/SHM sharing; no N agents writing one DB.

The transport is an ABSTRACTION (SyncTransport); GitEventTransport is the first
impl. A central-API mirror can implement the same interface later WITHOUT the
fold/emit layer changing.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STREAMS = ("todo", "backlog")  # memory joins later via the same fold/transport
_OPS = frozenset({"add", "update", "complete", "delete"})


# ── Hybrid Logical Clock ────────────────────────────────────────────────────
class HybridLogicalClock:
    """Per-actor monotonic clock: wall-time when it advances, else a bumped
    counter — so events from ONE actor are always strictly ordered even within
    the same millisecond. Cross-actor ties break on the actor id at fold time.
    Serialized as zero-padded ``<wall_ms>:<counter>`` so string sort == time sort.
    """

    def __init__(self) -> None:
        self._last_wall = 0
        self._counter = 0

    def tick(self) -> str:
        wall = int(time.time() * 1000)
        if wall > self._last_wall:
            self._last_wall, self._counter = wall, 0
        else:
            self._counter += 1
        return f"{self._last_wall:015d}:{self._counter:06d}"

    def observe(self, remote_hlc: str) -> None:
        """Advance past a remote clock so locally-generated events that follow a
        just-imported remote event still sort AFTER it (HLC causality)."""
        try:
            rw, rc = (int(x) for x in str(remote_hlc).split(":", 1))
        except (ValueError, AttributeError):
            return
        if rw > self._last_wall or (rw == self._last_wall and rc > self._counter):
            self._last_wall, self._counter = rw, rc


def _hlc_key(hlc: str) -> tuple[int, int]:
    try:
        w, c = (int(x) for x in str(hlc).split(":", 1))
        return (w, c)
    except (ValueError, AttributeError):
        return (0, 0)


# ── Event model ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SyncEvent:
    event_id: str
    stream: str
    entity_id: str
    op: str
    actor: str
    hlc: str
    ts: str
    fields: dict = field(default_factory=dict)
    session_id: str = ""
    project_id: str = ""

    def validate(self) -> None:
        if self.stream not in STREAMS:
            raise ValueError(f"unknown stream {self.stream!r} (allowed: {STREAMS})")
        if self.op not in _OPS:
            raise ValueError(f"unknown op {self.op!r} (allowed: {sorted(_OPS)})")
        if not self.event_id or not self.entity_id:
            raise ValueError("event_id and entity_id are required")

    def sort_key(self) -> tuple[int, int, str]:
        w, c = _hlc_key(self.hlc)
        return (w, c, self.actor)  # actor is the deterministic cross-actor tie-break


# ── Transport abstraction ───────────────────────────────────────────────────
class SyncTransport(ABC):
    """Append immutable events + read a stream's events. Implementations must be
    APPEND-ONLY and IDEMPOTENT on event_id (write-once; re-append is a no-op)."""

    @abstractmethod
    def append(self, event: SyncEvent) -> None: ...

    @abstractmethod
    def read(self, stream: str) -> list[SyncEvent]: ...


class GitEventTransport(SyncTransport):
    """One immutable JSON file per event under .MEMORY/sync/events/<stream>/.
    Write-once (existing event_id is never rewritten), so git only ever sees NEW
    files -> disjoint additions merge with no conflict across N agents."""

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root)

    def _dir(self, stream: str) -> Path:
        return self._root / ".MEMORY" / "sync" / "events" / stream

    @staticmethod
    def _safe_name(event_id: str) -> str:
        # The event_id is the FILENAME; the HLC embeds ':' and ids may carry other
        # path-hostile chars (esp. on Windows). Sanitize for the name only — the
        # real event_id is preserved verbatim inside the JSON body.
        return re.sub(r'[:/\\*?"<>|\x00-\x1f]', "_", event_id) or "_"

    @staticmethod
    def _scrub_fields(value: object) -> object:
        """Mask credential-shaped spans in agent-authored payload text.

        Scoped to ``fields`` ON PURPOSE. event_id / entity_id / hlc / actor are
        IDENTITY and ORDERING values — masking one would change a filename, a
        fold key or a sort key and corrupt the log. ``fields`` is the only part
        an agent writes free prose into, and prose is the only place a pasted
        secret can hide.
        """
        from .output_guard import scrub_persisted_text

        if isinstance(value, str):
            return scrub_persisted_text(value)
        if isinstance(value, dict):
            return {k: GitEventTransport._scrub_fields(v) for k, v in value.items()}
        if isinstance(value, list):
            return [GitEventTransport._scrub_fields(v) for v in value]
        return value

    def append(self, event: SyncEvent) -> None:
        event.validate()
        d = self._dir(event.stream)
        d.mkdir(parents=True, exist_ok=True)
        name = self._safe_name(event.event_id)
        path = d / f"{name}.json"
        if path.exists():
            return  # write-once idempotency: an event id is immutable
        # WRITE-TIME PRIVACY FLOOR (#363's rule, applied here 2026-08-20). This
        # directory is git-committed by the backlog autosync, so an unscrubbed
        # secret in agent prose is in repository history forever — and the write
        # is the LAST point at which it can be stopped. Payload only; identity
        # and ordering values are never touched. See _scrub_fields.
        body = asdict(event)
        body["fields"] = self._scrub_fields(body.get("fields") or {})
        # atomic write: tmp + rename, so a concurrent reader never sees a partial file.
        tmp = d / f".{name}.json.tmp"
        tmp.write_text(json.dumps(body, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def read(self, stream: str) -> list[SyncEvent]:
        d = self._dir(stream)
        if not d.is_dir():
            return []
        out: list[SyncEvent] = []
        for p in d.glob("*.json"):
            try:
                out.append(SyncEvent(**json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError, OSError):
                continue  # a corrupt/foreign file must never break the fold
        return out


# ── Fold: events -> materialized rows ───────────────────────────────────────
def fold_events(events: list[SyncEvent]) -> dict[str, dict]:
    """Deterministically materialize a stream's events into {entity_id: row}.

    Events apply in (hlc, actor) order; the LAST op per entity wins (last-writer-
    wins). delete -> tombstone (dropped from the view but a later higher-hlc add
    resurrects it). add replaces; update merges fields; complete merges + marks
    status. Pure + order-independent (sorts internally) -> every agent folding
    the SAME event set gets byte-identical state.
    """
    state: dict[str, dict | None] = {}
    for ev in sorted(events, key=lambda e: e.sort_key()):
        eid = ev.entity_id
        if ev.op == "delete":
            state[eid] = None
        elif ev.op == "add":
            state[eid] = {"entity_id": eid, **ev.fields}
        elif ev.op == "update":
            base = state.get(eid) or {"entity_id": eid}
            state[eid] = {**base, **ev.fields}
        elif ev.op == "complete":
            base = state.get(eid) or {"entity_id": eid}
            state[eid] = {**base, "status": "completed", **ev.fields}
    return {eid: row for eid, row in state.items() if row is not None}


def convergent_display_ids(events: list[SyncEvent]) -> dict[str, int]:
    """Dense-rank display ids over creation-HLC order for LIVE entities.

    THE fix for the VPS↔local id divergence: a per-store AUTOINCREMENT made the
    same ``global_id`` show a different ``#N`` on each store. This derives the
    display id purely from the authoritative event log, so every store holding
    the same event set computes a byte-identical ``{entity_id: id}`` map,
    regardless of the order events arrived in.

    Rank key = ``(first-add hlc, entity_id)`` — the entity's CREATION identity,
    stable across delete→re-add (resurrection keeps the original rank) and
    totally ordered (entity_id breaks equal-hlc ties store-independently).
    Tombstoned (deleted, not resurrected) entities are excluded. 1-based.

    Convergence caveat (documented, not hidden): a late-arriving OLDER item
    shifts ranks until the event sets match — auto-sync keeps that window small,
    and ``global_id`` remains the durable cross-reference through any renumber.
    """
    live = set(fold_events(events).keys())  # non-tombstoned, order-independent
    first_add: dict[str, tuple[int, int]] = {}  # earliest ADD hlc (creation)
    earliest: dict[str, tuple[int, int]] = {}  # earliest hlc of any op (fallback)
    for ev in events:
        if ev.entity_id not in live:
            continue
        key = _hlc_key(ev.hlc)
        if ev.op == "add":
            cur = first_add.get(ev.entity_id)
            if cur is None or key < cur:
                first_add[ev.entity_id] = key
        cur = earliest.get(ev.entity_id)
        if cur is None or key < cur:
            earliest[ev.entity_id] = key

    def _rank_key(eid: str) -> tuple[tuple[int, int], str]:
        return (first_add.get(eid) or earliest.get(eid) or (0, 0), eid)

    ordered = sorted(live, key=_rank_key)
    return {eid: i + 1 for i, eid in enumerate(ordered)}


def detect_lww_field_collisions(events: list[SyncEvent]) -> list[dict]:
    """Report same-field last-writer-wins collisions across DISTINCT actors.

    Concurrent edits to different fields merge harmlessly; two actors writing the
    SAME field to DIFFERENT values is a lost update — the higher (hlc, actor)
    wins the fold and the other's value is silently overwritten. This surfaces
    each such loss so it can be audited + recovered (loser_value is preserved).

    Pure + order-independent (sorts internally by sort_key). AUDIT-ONLY: it never
    changes the fold — the LWW winner still wins. Per entity+field, the max
    (hlc, actor) event is the winner; every EARLIER event that set that field to
    a DIFFERENT value under a DIFFERENT actor is a superseded loser (one record
    each). Same-value or same-actor rewrites are not collisions (no lost intent).
    """
    # entity -> field -> list of (sort_key, actor, value, hlc) that SET the field
    setters: dict[str, dict[str, list[tuple]]] = {}
    for ev in events:
        # Only update/complete count: an `add` establishes CREATION state (a later
        # edit legitimately supersedes it — normal lifecycle, not a lost update),
        # and concurrent adds have distinct entity_ids so never collide anyway.
        if ev.op not in ("update", "complete"):
            continue
        for field_key, value in (ev.fields or {}).items():
            setters.setdefault(ev.entity_id, {}).setdefault(field_key, []).append(
                (ev.sort_key(), ev.actor, value, ev.hlc)
            )
    collisions: list[dict] = []
    for eid, fields in setters.items():
        for field_key, writes in fields.items():
            if len(writes) < 2:
                continue
            writes.sort(key=lambda w: w[0])  # ascending (hlc, actor)
            _win_sort, win_actor, win_value, win_hlc = writes[-1]
            for _lose_sort, lose_actor, lose_value, lose_hlc in writes[:-1]:
                if lose_actor == win_actor or lose_value == win_value:
                    continue  # same author revising, or no information lost
                collisions.append(
                    {
                        "entity_id": eid,
                        "field": field_key,
                        "winner_actor": win_actor,
                        "winner_hlc": win_hlc,
                        "winner_value": win_value,
                        "loser_actor": lose_actor,
                        "loser_hlc": lose_hlc,
                        "loser_value": lose_value,
                    }
                )
    collisions.sort(key=lambda c: (c["entity_id"], c["field"], c["loser_hlc"], c["loser_actor"]))
    return collisions


# ── Store-layer emit (Phase 1) ─────────────────────────────────────────────
_PROCESS_CLOCK = HybridLogicalClock()


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_actor() -> str:
    """Best-effort acting identity for an event: the WebMCP gate principal, else
    the calling local host_session, else 'local'. Never raises."""
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
            current_gate_principal,
        )

        gp = current_gate_principal()
        if isinstance(gp, dict) and gp.get("user_id"):
            return f"gate:{gp['user_id']}"
        sid = (current_calling_host_session_id() or "").strip()
        if sid:
            return f"host:{sid}"
    except Exception:
        pass
    return "local"


def emit(
    project_root: Path,
    stream: str,
    entity_id: str,
    op: str,
    fields: dict | None = None,
    *,
    actor: str = "",
    session_id: str = "",
    project_id: str = "",
    transport: SyncTransport | None = None,
) -> SyncEvent:
    """Store-layer emit (Phase 1): append a SyncEvent AFTER a successful sqlite
    mutation. The event log is the SOURCE OF TRUTH; this keeps it fed on every
    write path (tools, dashboard, migrations, imports). BEST-EFFORT by contract:
    an emit failure must NEVER fail the already-committed sqlite mutation — the
    log catches up on the next rebuild/sync. entity_id MUST be a stable GLOBAL id
    (uuid), never a local autoincrement (which collides across agents/clones).
    """
    ev = SyncEvent(
        event_id=uuid.uuid4().hex,
        stream=stream,
        entity_id=str(entity_id),
        op=op,
        actor=actor or _resolve_actor(),
        hlc=_PROCESS_CLOCK.tick(),
        ts=_iso_now(),
        fields=dict(fields or {}),
        session_id=session_id,
        project_id=project_id,
    )
    try:
        (transport or GitEventTransport(Path(project_root))).append(ev)
        # AUTHORITY RECEIPT (#376, 2026-07-13): this emit follows a SUCCESSFUL
        # local canonical sqlite mutation, so the event we just wrote is
        # AUTHORITATIVE — record a receipt keyed by its event_id. Fold-on-read
        # applies ONLY receipted events, so a git event file that merely
        # APPEARED (a fresh clone's foreign file, or a forged one with a
        # self-asserted actor + max HLC) has no receipt and can never mutate
        # canonical state. The event log is thus an OUTBOX (+ operator-approved
        # recovery source), never an inbox that auto-rewrites the gate's truth.
        record_receipt(Path(project_root), stream, ev.event_id, ev.entity_id)
    except Exception as exc:
        # Phase 1 contract: emit never breaks the already-committed sqlite
        # mutation. BUT it must not fail SILENTLY — a swallowed append means the
        # event log is now behind sqlite (the canonical source lost a write). We
        # record the failure to a durable audit sidecar so the divergence is
        # DETECTABLE (has_emit_failures / rebuild can see it) instead of a silent
        # split-brain. Best-effort: even the audit write must never raise.
        _record_emit_failure(Path(project_root), ev, exc)
    return ev


# ── Authority receipts (#376): the canonical-mutation ledger ────────────────
# Receipts live in the SAME per-project store DB (.MEMORY/.index/aidocs.sqlite3
# — NO new database) that todo/backlog materialize into. A receipt is written
# ONLY by emit(), which runs after a successful LOCAL canonical sqlite commit.
# So "receipted" == "produced by an authenticated write on THIS gate", and the
# set of receipts is the authority boundary for hydration.
def _store_db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _ensure_receipts_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_event_receipts ("
        " event_id TEXT PRIMARY KEY,"
        " stream TEXT NOT NULL,"
        " entity_id TEXT NOT NULL,"
        " recorded_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_receipts_stream "
        "ON sync_event_receipts(stream)"
    )


def record_receipt(project_root: Path, stream: str, event_id: str, entity_id: str) -> None:
    """Mark one event_id as authoritative (produced by a local canonical
    commit). Idempotent; best-effort — a receipt-write failure never breaks the
    already-committed mutation (the event file still exists and can be adopted
    via operator-approved recovery)."""
    try:
        from ._sqlite_connect import Durability, connect

        db = _store_db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        # AUDIT, on #755's standing rule "would losing this write hand back an
        # AUTHORISATION". A receipt is the only proof that THIS store produced
        # the event; split_by_authority reads it to decide what may be folded.
        # A power cut that un-writes one re-opens a settled event to adoption
        # on someone else's word — absence after a crash is itself the finding.
        # row_factory=False preserves the tuple shape the `r[0]` readers in
        # this module were written against (the helper defaults to Row).
        with connect(str(db), durability=Durability.AUDIT, row_factory=False) as conn:
            _ensure_receipts_table(conn)
            conn.execute(
                "INSERT OR IGNORE INTO sync_event_receipts "
                "(event_id, stream, entity_id, recorded_at) VALUES (?, ?, ?, ?)",
                (str(event_id), str(stream), str(entity_id), _iso_now()),
            )
            conn.commit()
    except Exception:
        pass


def authoritative_event_ids(project_root: Path, stream: str) -> set[str]:
    """The set of event_ids this store authoritatively produced for a stream."""
    try:
        from ._sqlite_connect import Durability, connect

        db = _store_db_path(project_root)
        if not db.is_file():
            return set()
        # Same store, same AUDIT class as record_receipt. NOT read_only despite
        # being a reader: _ensure_receipts_table issues CREATE TABLE, which a
        # mode=ro connection cannot do. Declaring read_only here would trade a
        # missing pragma for a broken reader.
        with connect(str(db), durability=Durability.AUDIT, row_factory=False) as conn:
            _ensure_receipts_table(conn)
            rows = conn.execute(
                "SELECT event_id FROM sync_event_receipts WHERE stream = ?",
                (str(stream),),
            ).fetchall()
        return {str(r[0]) for r in rows}
    except Exception:
        return set()


def split_by_authority(
    project_root: Path, stream: str, events: list[SyncEvent]
) -> tuple[list[SyncEvent], list[SyncEvent]]:
    """Partition a stream's events into (authoritative, incoming). Authoritative
    == carries a local receipt (produced by an authenticated write here);
    incoming == appeared in the events dir without a receipt (fresh-clone
    foreign file OR a forged one). File-contained actor/org/role/approval fields
    are NEVER consulted — authority is the receipt, not the payload."""
    receipted = authoritative_event_ids(project_root, stream)
    auth = [e for e in events if e.event_id in receipted]
    incoming = [e for e in events if e.event_id not in receipted]
    return auth, incoming


def _quarantine_log(project_root: Path, stream: str) -> Path:
    return Path(project_root) / ".MEMORY" / "sync" / f".quarantine_{stream}.jsonl"


def record_quarantine(project_root: Path, stream: str, events: list[SyncEvent]) -> None:
    """Append a durable, human-readable record of incoming (unreceipted) events
    that hydration REFUSED to apply, so the divergence is DETECTABLE (a clear
    status) rather than a silent drop. Best-effort; never raises."""
    if not events:
        return
    try:
        p = _quarantine_log(project_root, stream)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for e in events:
                fh.write(
                    json.dumps(
                        {
                            "event_id": e.event_id,
                            "stream": e.stream,
                            "entity_id": e.entity_id,
                            "op": e.op,
                            "actor": e.actor,
                            "hlc": e.hlc,
                            "refused_at": _iso_now(),
                            "reason": "no_authoritative_receipt",
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    except Exception:
        pass


def quarantined_count(project_root: Path, stream: str) -> int:
    """Number of incoming events hydration has refused (0 when none). Reads the
    quarantine log; a clear status for callers/health checks."""
    try:
        p = _quarantine_log(project_root, stream)
        if not p.is_file():
            return 0
        with p.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except Exception:
        return 0


def adopt_events_as_authoritative(
    project_root: Path, stream: str, entity_ids: set[str] | None
) -> int:
    """Declare CURRENT events authoritative (record receipts). TWO callers:

    * MIGRATION (``entity_ids`` = a set): adopt only events whose entity_id is
      already a canonical sqlite row — pre-receipt history corroborated by
      canonical state. A forged file for a non-existent entity is NEVER adopted
      (its entity_id is not a canonical row).
    * OPERATOR FULL-SNAPSHOT RECOVERY (``entity_ids`` = None): adopt EVERY
      current event file — the explicit disaster-recovery / fresh-clone
      bootstrap where the operator declares the present event log to be truth.

    Both are explicit, out-of-band operations — NEVER the fold-on-read path.
    Returns the number of newly-receipted events."""
    adopted = 0
    receipted = authoritative_event_ids(project_root, stream)
    for e in GitEventTransport(Path(project_root)).read(stream):
        if e.event_id in receipted:
            continue
        if entity_ids is None or e.entity_id in entity_ids:
            record_receipt(project_root, stream, e.event_id, e.entity_id)
            adopted += 1
    return adopted


# ── Unverified-origin ledger (#376, 2026-07-29): the sticky refusal ─────────
# An automatic syncer may adopt events that DEMONSTRABLY arrived over its
# authenticated transport. The counterpart it needs is a memory of the events
# that did NOT: an event first observed locally with no transport provenance is
# recorded here, PERMANENTLY, so a later observation cannot re-decide it.
#
# WHY STICKY IS LOAD-BEARING. The backlog syncer commits and pushes the whole
# events dir. Without this ledger, a file it refuses on one cycle is pushed by
# that same cycle and comes back "present on the remote" on the next — the
# syncer would launder the very forgery it just refused. Provenance is therefore
# decided ONCE, at first sight, and never revisited.
#
# This ledger records a REFUSAL, never a deletion: the event file itself is
# untouched on disk and stays adoptable through the explicit operator recovery
# path (adopt_events_as_authoritative), which is a deliberate human act.
def _ensure_unverified_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_event_unverified ("
        " event_id TEXT PRIMARY KEY,"
        " stream TEXT NOT NULL,"
        " entity_id TEXT NOT NULL,"
        " actor TEXT NOT NULL,"
        " hlc TEXT NOT NULL,"
        " first_seen_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_unverified_stream "
        "ON sync_event_unverified(stream)"
    )


def record_unverified(project_root: Path, stream: str, events: list[SyncEvent]) -> None:
    """Remember that these events were first seen WITHOUT transport provenance.

    Idempotent (INSERT OR IGNORE keeps the FIRST sighting, which is the point).
    Best-effort: a ledger-write failure must never break a sync cycle. Note it
    fails toward NOT recording — safe, because adoption requires POSITIVE proof
    of origin and never merely the absence of a refusal."""
    if not events:
        return
    try:
        from ._sqlite_connect import Durability, connect

        db = _store_db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        # AUDIT: this ledger records events PERMANENTLY REFUSED as
        # unproven-origin. Losing the row hands an event back its chance at
        # adoption — the authorisation test again, in its purest form.
        with connect(str(db), durability=Durability.AUDIT, row_factory=False) as conn:
            _ensure_unverified_table(conn)
            conn.executemany(
                "INSERT OR IGNORE INTO sync_event_unverified "
                "(event_id, stream, entity_id, actor, hlc, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (e.event_id, str(stream), e.entity_id, e.actor, e.hlc, _iso_now())
                    for e in events
                ],
            )
            conn.commit()
    except Exception:
        pass


def unverified_event_ids(project_root: Path, stream: str) -> set[str]:
    """Event ids permanently refused as unproven-origin for this stream."""
    try:
        from ._sqlite_connect import Durability, connect

        db = _store_db_path(project_root)
        if not db.is_file():
            return set()
        # AUDIT to match its writer; not read_only because
        # _ensure_unverified_table issues CREATE TABLE.
        with connect(str(db), durability=Durability.AUDIT, row_factory=False) as conn:
            _ensure_unverified_table(conn)
            rows = conn.execute(
                "SELECT event_id FROM sync_event_unverified WHERE stream = ?",
                (str(stream),),
            ).fetchall()
        return {str(r[0]) for r in rows}
    except Exception:
        return set()


def adopt_event_ids(project_root: Path, stream: str, event_ids: set[str]) -> int:
    """Receipt a NAMED set of events — the automatic-syncer adoption path.

    Unlike ``adopt_events_as_authoritative``, this adopts only the exact events
    the caller can PROVE arrived over its authenticated transport, and it
    refuses any event already in the unverified-origin ledger. That second check
    lives here rather than only in the caller, so the sticky refusal holds for
    every future syncer and not just the one that wrote it.

    Returns the number of newly-receipted events."""
    if not event_ids:
        return 0
    wanted = {str(x) for x in event_ids}
    receipted = authoritative_event_ids(project_root, stream)
    refused = unverified_event_ids(project_root, stream)
    adopted = 0
    for e in GitEventTransport(Path(project_root)).read(stream):
        if e.event_id in receipted or e.event_id in refused:
            continue
        if e.event_id in wanted:
            record_receipt(project_root, stream, e.event_id, e.entity_id)
            adopted += 1
    return adopted


def _emit_failure_log(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / "sync" / ".emit_failures.jsonl"


def _record_emit_failure(project_root: Path, ev: "SyncEvent", exc: BaseException) -> None:
    """Append a durable record of an emit that failed to persist, so a later
    reconcile/rebuild knows sqlite may be AHEAD of the event log. Never raises."""
    try:
        p = _emit_failure_log(project_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "event_id": ev.event_id,
            "stream": ev.stream,
            "entity_id": ev.entity_id,
            "op": ev.op,
            "hlc": ev.hlc,
            "ts": ev.ts,
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
    except Exception:
        pass


def has_emit_failures(project_root: Path) -> bool:
    """True iff any sync emit has failed to persist (sqlite may have diverged from
    the canonical event log). Callers/rebuild use this to trigger reconciliation."""
    try:
        p = _emit_failure_log(project_root)
        return p.is_file() and p.stat().st_size > 0
    except Exception:
        return False


def clear_emit_failures(project_root: Path) -> None:
    """Clear the emit-failure marker after a successful reconcile/rebuild that
    re-fed the log. Never raises."""
    try:
        _emit_failure_log(project_root).unlink(missing_ok=True)
    except Exception:
        pass


# A hydrate_fn returns this instead of an entity count to say "not now" (#503).
HYDRATE_DEFERRED = -1


def maybe_hydrate(project_root: Path, stream: str, hydrate_fn) -> None:
    """Fold-on-read trigger (Phase 2): run ``hydrate_fn(project_root)`` only when
    the stream's event count has CHANGED since the last hydrate (a cheap file-
    count watermark under .MEMORY/sync/.hydrated_<stream>), so synced remote
    events materialize into sqlite before a read WITHOUT re-folding on every read.
    Best-effort: a hydrate failure never breaks the read.

    DEFERRAL (#503): a hydrate_fn may DECLINE to apply the fold right now by
    returning ``HYDRATE_DEFERRED`` — e.g. a mutation is mid-flight, so the event
    log is momentarily behind sqlite and folding it would replay pre-write state
    OVER the write. A declined fold must NOT advance the watermark, or the events
    it skipped would never be folded again.
    """
    try:
        root = Path(project_root)
        ev_dir = root / ".MEMORY" / "sync" / "events" / stream
        if not ev_dir.is_dir():
            return
        count = sum(1 for _ in ev_dir.glob("*.json"))
        wm = root / ".MEMORY" / "sync" / f".hydrated_{stream}"
        last = -1
        if wm.exists():
            try:
                last = int(wm.read_text(encoding="utf-8").strip() or "-1")
            except (ValueError, OSError):
                last = -1
        if count != last:
            if hydrate_fn(project_root) == HYDRATE_DEFERRED:
                return  # fold declined -> leave the watermark for the next read
            wm.parent.mkdir(parents=True, exist_ok=True)
            wm.write_text(str(count), encoding="utf-8")
    except Exception:
        pass


def flush_events(project_root: Path, message: str = "sync: flush todo/backlog events") -> bool:
    """Git-commit pending event files so they SYNC across agents (the transport
    step). PATH-SCOPED to .MEMORY/sync/events so it never touches unrelated work,
    and carries its own identity so it commits even on a repo with no git user
    configured. Best-effort; returns True iff it created a commit. Push is
    separate (needs the remote's write credential — the WebMCP war's residual).
    """
    # Route through the governed git chokepoint (git_helpers.run_git_sync) rather
    # than a raw subprocess call — it is the registered, semgrep-clean git egress
    # site. run_git_sync prepends `git -c safe.directory=*` and runs in cwd, and
    # RAISES on a non-zero exit (e.g. "nothing to commit"), which we treat as
    # "no commit made" -> False.
    from .git_helpers import run_git_sync

    root = Path(project_root)
    ev = root / ".MEMORY" / "sync" / "events"
    if not ev.is_dir():
        return False
    try:
        run_git_sync(str(root), "add", "--", str(ev))
        run_git_sync(
            str(root),
            "-c", "user.email=aidocs-sync@local", "-c", "user.name=aidocs-sync",
            "commit", "-q", "-m", message, "--", str(ev),
        )
        return True
    except Exception:
        return False
