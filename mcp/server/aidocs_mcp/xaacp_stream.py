"""THE ONE ORDERED ACTOR-MESSAGE STREAM (#1061).

Every XAACP fact an actor can be told about is ONE row here, appended in the
order it happened, keyed by session + the durable reader actor id:

  * ``incoming`` -- a message addressed to the reader (a send, or a reply sent
    with ``in_reply_to``: a reply is just a send, so there is no second kind);
  * ``outcome``  -- the other side's ack / decision / expiry ON A MESSAGE THE
    READER SENT.

``reply_to_id`` / ``correlation_id`` stay on the ``messages`` row as causal
metadata only. Thread topology is never a queue: nothing here orders or
filters by thread.

THREE SEPARATE FACTS, THREE SEPARATE HOMES
  delivery  ``xaacp_stream.delivered_at``  (this module; wait_next / claim)
  read      ``msg_reads``                  (inbox mark_read=True)
  decision  ``messages.decision_status`` / ``acked_at``
Advancing the delivery cursor never marks read or decides; reading never
delivers; a notice-style read (mark_read=False) writes nothing at all.

THE CURSOR CONTRACT
  1. ``seq`` is assigned once, at APPEND time (never at read time), AUTOINCREMENT
     so it is monotonic across the whole store and therefore per session.
  2. A claim (wait_next, or any surfacing rail that calls
     :func:`claim_next`) returns the oldest undelivered event with
     ``seq > max(durable_receive_cursor, after_cursor)`` and CAS-stamps
     ``delivered_at`` -- one event can never be delivered twice, on one surface
     or two.
  3. ``durable_receive_cursor`` is the server-held low watermark: every event at
     or below it is delivered or superseded. It only moves forward.

This module never imports ``conductor_comms``: the caller passes the open
connection, so the stream shares the message store's transaction.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

#: Retained audit event kinds this stream EMITS (execution_event_retention
#: registers them; the names are the contract between the two lanes).
AUDIT_STREAM_SEND = "stream_send"
AUDIT_STREAM_DELIVER = "stream_deliver"
AUDIT_STREAM_READ = "stream_read"
#: An ack is receipt/ownership, NOT a decision, so it has its own kind.
AUDIT_STREAM_ACK = "stream_ack"
#: Decisions (xaacp_reply) and sender cancels.
AUDIT_STREAM_DECIDE = "stream_decide"

#: Authenticated provenance of an event. ``operator`` is set only by a
#: server-side operator surface (dashboard -> XAACP); agents are ``agent``.
ORIGIN_AGENT = "agent"
ORIGIN_OPERATOR = "operator"
#: A Stop-hook turn mirror between seated Consuls (server-verified seat).
ORIGIN_HOOK = "hook"
ORIGINS = frozenset({ORIGIN_AGENT, ORIGIN_OPERATOR, ORIGIN_HOOK})

EVENT_INCOMING = "incoming"
EVENT_OUTCOME = "outcome"

#: Lane key of a cursor that spans every lane addressed to a lane-less reader.
ALL_LANES = "*"

_BACKFILL_KEY = "stream_backfill_v1"
_DECIDED = ("accepted", "rejected", "completed", "blocked")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the stream tables and migrate pre-stream rows exactly once."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xaacp_stream (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            reader_actor_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT '',
            lane_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            delivered_at REAL,
            delivered_via TEXT NOT NULL DEFAULT '',
            superseded_at REAL,
            origin TEXT NOT NULL DEFAULT 'agent',
            causal_ref TEXT NOT NULL DEFAULT '',
            UNIQUE (reader_actor_id, message_id, event_kind, state)
        )
        """
    )
    stream_cols = {row[1] for row in conn.execute("PRAGMA table_info(xaacp_stream)").fetchall()}
    if "origin" not in stream_cols:
        conn.execute("ALTER TABLE xaacp_stream ADD COLUMN origin TEXT NOT NULL DEFAULT 'agent'")
    if "causal_ref" not in stream_cols:
        conn.execute("ALTER TABLE xaacp_stream ADD COLUMN causal_ref TEXT NOT NULL DEFAULT ''")
    # TWO-PHASE DELIVERY (#1061 rework): claimed (lease-bounded, in flight) ->
    # surfaced (`delivered_at`). A row delivered before these columns existed
    # is therefore already SURFACED -- the migration needs no data rewrite.
    for column, ddl in (
        ("claim_id", "TEXT NOT NULL DEFAULT ''"),
        ("claimed_at", "REAL"),
        ("claim_surface", "TEXT NOT NULL DEFAULT ''"),
        ("claim_lease_expires_at", "REAL"),
        ("reclaim_count", "INTEGER NOT NULL DEFAULT 0"),
        ("abandoned_at", "REAL"),
    ):
        if column not in stream_cols:
            conn.execute(f"ALTER TABLE xaacp_stream ADD COLUMN {column} {ddl}")
    # IMMUTABLE PROVENANCE: origin and causal_ref are written once, at append.
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_xaacp_stream_provenance_immutable "
        "BEFORE UPDATE OF origin, causal_ref, reader_actor_id, message_id, session_id, seq "
        "ON xaacp_stream BEGIN SELECT RAISE(ABORT, 'xaacp_stream provenance is immutable'); END"
    )
    # Operator appends and hook turn mirrors are idempotent on
    # (session, origin, causal_ref) at the store.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_xaacp_stream_server_origin_ref "
        "ON xaacp_stream (session_id, origin, causal_ref) "
        "WHERE origin IN ('operator', 'hook') AND event_kind='incoming' AND causal_ref != ''"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xaacp_park_log (
            session_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            last_park_enter_at REAL,
            last_park_return_at REAL,
            last_return_kind TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY (session_id, actor_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xaacp_stream_reader "
        "ON xaacp_stream (session_id, reader_actor_id, seq)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xaacp_receive_cursor (
            session_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            lane_key TEXT NOT NULL,
            cursor INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (session_id, actor_id, lane_key)
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS xaacp_stream_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    done = conn.execute(
        "SELECT 1 FROM xaacp_stream_meta WHERE key=?", (_BACKFILL_KEY,)
    ).fetchone()
    if done is None:
        backfill(conn)
        conn.execute(
            "INSERT OR IGNORE INTO xaacp_stream_meta (key, value) VALUES (?, ?)",
            (_BACKFILL_KEY, str(time.time())),
        )


def _read_at(conn: sqlite3.Connection, message_id: str, roles: tuple[str, ...]) -> float | None:
    marks = ",".join("?" * len(roles))
    row = conn.execute(
        f"SELECT MIN(read_at) FROM msg_reads WHERE message_id=? AND role IN ({marks})",
        (message_id, *roles),
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def backfill(conn: sqlite3.Connection) -> int:
    """EMPIRE XVIII: every pre-stream XAACP row becomes stream history.

    Incoming events keep ``seq == messages.rowid`` so a cursor an actor held
    before the migration still means the same position after it. Delivery is
    SEEDED from evidence that the actor was already shown or acted on the
    message (read ledger, notice ledger, ack, its own decision), so an
    already-surfaced event is never replayed as new; a message with no such
    evidence was never provably delivered and surfaces once. Outcome events
    follow in state-time order, seeded from the outcome read / wait ledgers.
    """
    rows = conn.execute(
        "SELECT rowid AS _rowid, * FROM messages WHERE direction='xaacp' ORDER BY rowid"
    ).fetchall()
    written = 0
    for row in rows:
        mid = str(row["id"])
        target = str(row["target_actor_id"] or "")
        status = str(row["decision_status"] or row["status"] or "pending")
        if not target:
            continue
        evidence = _read_at(conn, mid, ("xaacp:" + target, "xaacp-notice:" + target))
        if evidence is None and row["acked_at"] is not None:
            evidence = float(row["acked_at"])
        if evidence is None and status in _DECIDED:
            evidence = float(row["answered_at"] or row["created_at"])
        superseded = None
        if evidence is None and status in ("canceled", "expired"):
            superseded = float(row["answered_at"] or row["created_at"])
        written += conn.execute(
            "INSERT OR IGNORE INTO xaacp_stream (seq, session_id, reader_actor_id, "
            "message_id, event_kind, state, lane_id, created_at, delivered_at, "
            "delivered_via, superseded_at) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)",
            (
                int(row["_rowid"]),
                str(row["session_id"] or ""),
                target,
                mid,
                EVENT_INCOMING,
                str(row["lane_id"] or ""),
                float(row["created_at"] or 0.0),
                evidence,
                "migration_evidence" if evidence is not None else "",
                superseded,
            ),
        ).rowcount
    outcomes: list[tuple[float, sqlite3.Row, str]] = []
    for row in rows:
        sender = str(row["sender_actor_id"] or "")
        if not sender or sender == str(row["target_actor_id"] or ""):
            continue
        status = str(row["decision_status"] or row["status"] or "pending")
        if row["acked_at"] is not None:
            outcomes.append((float(row["acked_at"]), row, "acked"))
        if status in (*_DECIDED, "expired"):
            outcomes.append((float(row["answered_at"] or row["created_at"]), row, status))
    outcomes.sort(key=lambda item: item[0])
    for at, row, state in outcomes:
        mid = str(row["id"])
        sender = str(row["sender_actor_id"])
        evidence = _read_at(
            conn,
            mid,
            (f"xaacp-outcome:{sender}:{state}", f"xaacp-wait-outcome:{sender}:{state}"),
        )
        status = str(row["decision_status"] or row["status"] or "pending")
        superseded = None
        if evidence is None and state == "acked" and status in (*_DECIDED, "expired"):
            # The ack was overtaken by the decision; the decision is the news.
            superseded = at
        written += conn.execute(
            "INSERT OR IGNORE INTO xaacp_stream (session_id, reader_actor_id, "
            "message_id, event_kind, state, lane_id, created_at, delivered_at, "
            "delivered_via, superseded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(row["session_id"] or ""),
                sender,
                mid,
                EVENT_OUTCOME,
                state,
                str(row["lane_id"] or ""),
                at,
                evidence,
                "migration_evidence" if evidence is not None else "",
                superseded,
            ),
        ).rowcount
    return written


def append(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    reader_actor_id: str,
    message_id: str,
    kind: str,
    lane_id: str,
    state: str = "",
    now: float | None = None,
    origin: str = ORIGIN_AGENT,
    causal_ref: str = "",
) -> int | None:
    """Append one event; returns its seq, or None if it already exists.

    ``origin`` is SERVER-DERIVED provenance (``agent`` | ``operator``), never a
    caller argument: no public ai_msg parameter reaches it.
    """
    at = time.time() if now is None else float(now)
    if origin not in ORIGINS:
        raise ValueError(f"unknown stream origin {origin!r}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO xaacp_stream (session_id, reader_actor_id, message_id, "
        "event_kind, state, lane_id, created_at, origin, causal_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, reader_actor_id, message_id, kind, state, lane_id, at, origin, str(causal_ref or "")),
    )
    return int(cur.lastrowid) if cur.rowcount else None


def mark_delivered_by_action(
    conn: sqlite3.Connection, *, message_id: str, reader_actor_id: str, via: str, now: float
) -> None:
    """The addressee ACTED on the message (ack/decision): it was delivered.

    Delivery only -- never read. CAS on ``delivered_at IS NULL`` so an earlier
    real delivery keeps its own timestamp and surface.
    """
    conn.execute(
        "UPDATE xaacp_stream SET delivered_at=?, delivered_via=? WHERE message_id=? "
        "AND reader_actor_id=? AND event_kind=? AND delivered_at IS NULL",
        (now, via, message_id, reader_actor_id, EVENT_INCOMING),
    )


def supersede_undelivered(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    now: float,
    kind: str = EVENT_INCOMING,
    state: str | None = None,
) -> None:
    """A never-delivered event whose news is void (canceled / expired / overtaken)."""
    sql = (
        "UPDATE xaacp_stream SET superseded_at=? WHERE message_id=? AND event_kind=? "
        "AND delivered_at IS NULL AND superseded_at IS NULL"
    )
    params: list[object] = [now, message_id, kind]
    if state is not None:
        sql += " AND state=?"
        params.append(state)
    conn.execute(sql, params)


def _scope(session_id: str, actor_id: str, lane_id: str, span_all_lanes: bool) -> tuple[str, list[object], str]:
    if span_all_lanes:
        return "session_id=? AND reader_actor_id=?", [session_id, actor_id], ALL_LANES
    return (
        "session_id=? AND reader_actor_id=? AND lane_id=?",
        [session_id, actor_id, lane_id],
        lane_id,
    )


#: An event that is neither surfaced, superseded nor abandoned. An in-flight
#: claim is still OPEN: it holds the watermark back until confirmed.
_OPEN_SQL = "delivered_at IS NULL AND superseded_at IS NULL AND abandoned_at IS NULL"
#: ...and CLAIMABLE now: open, and either never claimed or its lease ran out.
_CLAIMABLE_SQL = _OPEN_SQL + " AND (claim_id='' OR claim_lease_expires_at<=?)"

#: How long a tool-result claim may stay unconfirmed before it can be re-claimed.
CLAIM_LEASE_SECONDS = 30.0
#: An unconfirmed claim may be re-claimed exactly this many times; the next
#: expiry ABANDONS it (kept in history, never surfaced again by a claim).
MAX_RECLAIMS = 1

SURFACE_WAIT_NEXT = "wait_next"


def receive_cursor(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    now: float | None = None,
    persist: bool = True,
) -> int:
    """The server-held durable receive cursor (low watermark), advanced monotonically."""
    where, params, lane_key = _scope(session_id, actor_id, lane_id, span_all_lanes)
    low = conn.execute(
        f"SELECT MIN(seq) FROM xaacp_stream WHERE {where} AND " + _OPEN_SQL,
        params,
    ).fetchone()[0]
    if low is not None:
        watermark = int(low) - 1
    else:
        watermark = int(
            conn.execute(
                f"SELECT COALESCE(MAX(seq), 0) FROM xaacp_stream WHERE {where}", params
            ).fetchone()[0]
        )
    stored = conn.execute(
        "SELECT cursor FROM xaacp_receive_cursor WHERE session_id=? AND actor_id=? AND lane_key=?",
        (session_id, actor_id, lane_key),
    ).fetchone()
    cursor = max(watermark, int(stored[0]) if stored is not None else 0)
    if persist and (stored is None or int(stored[0]) != cursor):
        conn.execute(
            "INSERT INTO xaacp_receive_cursor (session_id, actor_id, lane_key, cursor, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, actor_id, lane_key) DO UPDATE SET "
            "cursor=MAX(cursor, excluded.cursor), updated_at=excluded.updated_at",
            (session_id, actor_id, lane_key, cursor, time.time() if now is None else now),
        )
    return cursor


def claim_next(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    after_cursor: int = 0,
    via: str,
    now: float | None = None,
    lease_seconds: float = CLAIM_LEASE_SECONDS,
    audit: list | None = None,
) -> sqlite3.Row | None:
    """Atomically claim the next event (contract clause 2), two-phase.

    ``via == 'wait_next'`` claims AND surfaces in one CAS (the return is the
    surfacing). Any other surface only CLAIMS: a lease-bounded in-flight claim
    with a fresh ``claim_id``, confirmed later by :func:`confirm_surfaced`. An
    expired unconfirmed claim is re-claimable exactly ``MAX_RECLAIMS`` times,
    then abandoned. ``audit`` (if given) collects ``(state, seq, claim_id,
    reclaim_count)`` for every claimed / reclaimed / surfaced / abandoned step.
    """
    notes = audit if audit is not None else []
    at = time.time() if now is None else float(now)
    where, params, _ = _scope(session_id, actor_id, lane_id, span_all_lanes)
    start = max(
        receive_cursor(
            conn,
            session_id=session_id,
            actor_id=actor_id,
            lane_id=lane_id,
            span_all_lanes=span_all_lanes,
            now=at,
        ),
        int(after_cursor or 0),
    )
    while True:
        candidate = conn.execute(
            f"SELECT seq, event_kind, state, message_id, claim_id, reclaim_count FROM xaacp_stream "
            f"WHERE {where} AND seq>? AND " + _CLAIMABLE_SQL + " ORDER BY seq LIMIT 1",
            [*params, start, at],
        ).fetchone()
        if candidate is None:
            return None
        seq = int(candidate[0])
        if candidate[1] == EVENT_OUTCOME and candidate[2] != _current_outcome_state(
            conn, str(candidate[3])
        ):
            # OVERTAKEN before it was ever delivered (an ack the decision already
            # superseded): the later state is the news. History keeps it.
            conn.execute(
                "UPDATE xaacp_stream SET superseded_at=? WHERE seq=? AND delivered_at IS NULL",
                (at, seq),
            )
            start = seq
            continue
        prior_claim = str(candidate[4] or "")
        reclaims = int(candidate[5] or 0)
        if prior_claim and reclaims >= MAX_RECLAIMS:
            if conn.execute(
                "UPDATE xaacp_stream SET abandoned_at=? WHERE seq=? AND claim_id=? AND " + _OPEN_SQL,
                (at, seq, prior_claim),
            ).rowcount:
                notes.append(("abandoned", seq, prior_claim, reclaims))
            start = seq
            continue
        claim_id = uuid.uuid4().hex[:16]
        next_reclaims = reclaims + (1 if prior_claim else 0)
        surfaced = via == SURFACE_WAIT_NEXT
        won = conn.execute(
            "UPDATE xaacp_stream SET claim_id=?, claimed_at=?, claim_surface=?, "
            "claim_lease_expires_at=?, reclaim_count=?, delivered_at=?, delivered_via=? "
            "WHERE seq=? AND claim_id=? AND " + _CLAIMABLE_SQL,
            (
                claim_id,
                at,
                via,
                at + max(1.0, float(lease_seconds)),
                next_reclaims,
                at if surfaced else None,
                via if surfaced else "",
                seq,
                prior_claim,
                at,
            ),
        ).rowcount
        if won:
            notes.append(("reclaimed" if prior_claim else "claimed", seq, claim_id, next_reclaims))
            if surfaced:
                notes.append(("surfaced", seq, claim_id, next_reclaims))
            receive_cursor(
                conn,
                session_id=session_id,
                actor_id=actor_id,
                lane_id=lane_id,
                span_all_lanes=span_all_lanes,
                now=at,
            )
            return event_row(conn, seq)
        start = seq  # lost the race to another surface; never deliver it twice


def confirm_surfaced(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    claim_ids: list[str],
    now: float | None = None,
) -> list[tuple[int, str]]:
    """Phase two: the claimed notice WAS rendered. Returns (seq, claim_id) confirmed.

    Scoped to the reader's own events; only the CURRENT claim of an open event
    confirms (a re-claim after lease expiry invalidates the old id). A
    confirmed event can never be claimed again.
    """
    at = time.time() if now is None else float(now)
    confirmed: list[tuple[int, str]] = []
    for claim_id in dict.fromkeys(str(c or "").strip() for c in (claim_ids or [])):
        if not claim_id:
            continue
        row = conn.execute(
            "SELECT seq, claim_surface FROM xaacp_stream WHERE session_id=? AND reader_actor_id=? "
            "AND claim_id=? AND " + _OPEN_SQL,
            (session_id, actor_id, claim_id),
        ).fetchone()
        if row is None:
            continue
        if conn.execute(
            "UPDATE xaacp_stream SET delivered_at=?, delivered_via=? WHERE seq=? AND claim_id=? AND "
            + _OPEN_SQL,
            (at, str(row[1] or ""), int(row[0]), claim_id),
        ).rowcount:
            confirmed.append((int(row[0]), claim_id))
    return confirmed


def _current_outcome_state(conn: sqlite3.Connection, message_id: str) -> str:
    row = conn.execute(
        "SELECT decision_status, status, acked_at FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    if row is None:
        return ""
    status = str(row[0] or row[1] or "pending")
    if status in (*_DECIDED, "expired"):
        return status
    return "acked" if row[2] is not None and status != "canceled" else ""


def event_row(conn: sqlite3.Connection, seq: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT m.*, s.seq AS seq, s.event_kind AS event_kind, s.state AS event_state, "
        "s.created_at AS event_created_at, s.delivered_at AS delivered_at, "
        "s.delivered_via AS delivered_via, s.reader_actor_id AS reader_actor_id, "
        "s.origin AS origin, s.causal_ref AS causal_ref, s.claim_id AS claim_id, "
        "s.claim_lease_expires_at AS claim_lease_expires_at, s.reclaim_count AS reclaim_count "
        "FROM xaacp_stream s JOIN messages m ON m.id = s.message_id WHERE s.seq=?",
        (int(seq),),
    ).fetchone()


def history(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    after_seq: int = 0,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Read-only history for one reader, in stream order. Writes nothing."""
    where, params, _ = _scope(session_id, actor_id, lane_id, span_all_lanes)
    where = " AND ".join(f"s.{clause.strip()}" for clause in where.split(" AND "))
    return conn.execute(
        "SELECT m.*, s.seq AS seq, s.event_kind AS event_kind, s.state AS event_state, "
        "s.created_at AS event_created_at, s.delivered_at AS delivered_at, "
        "s.delivered_via AS delivered_via, s.superseded_at AS superseded_at, "
        "s.reader_actor_id AS reader_actor_id, s.origin AS origin, s.causal_ref AS causal_ref "
        f"FROM xaacp_stream s JOIN messages m ON m.id = s.message_id WHERE {where} "
        "AND s.seq>? ORDER BY s.seq ASC LIMIT ?",
        # limit <= 0 is the whole history (-1 is SQLite's "no limit").
        [*params, int(after_seq or 0), int(limit) if int(limit) > 0 else -1],
    ).fetchall()
