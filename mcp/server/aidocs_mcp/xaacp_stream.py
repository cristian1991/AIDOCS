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
     ``seq > durable_receive_cursor`` and CAS-stamps ``delivered_at`` -- one
     event can never be delivered twice, on one surface or two. A caller's
     ``after_cursor`` never hides an undelivered event (#1104): dedup is the
     delivery stamp, not the caller's cursor.
  3. ``durable_receive_cursor`` is the server-held low watermark: every event at
     or below it is delivered or superseded. It moves forward as events close;
     a stored value found past an open event is healed DOWN, audited (#1104).

Delivery here means HANDED OUT by the server, at most once. It is not
lossless: the claim is stamped before the response is known to reach the
consumer, so a response lost in transport loses that delivery (separate risk).

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

# ── THE ONE AUTHORITY FOR DELIVERABLE STATES (#1104) ──────────────────────
# The writer (xaacp_reply / xaacp_ack / expiry in conductor_comms) and the
# reader (claim_next's "is this outcome still the news?") used to keep two
# hand-written lists. Both now read these, so a new decision cannot be
# written by one side and silently judged stale by the other.
#: Terminal decisions an addressee may record (xaacp_reply).
DECISIONS = frozenset({"accepted", "rejected", "completed", "blocked"})
#: Every terminal state a message can reach.
TERMINAL = frozenset({*DECISIONS, "canceled", "expired"})
#: The non-terminal receipt (xaacp_ack). Never a decision.
ACKED = "acked"
#: Terminal states that ARE news to the sender. `canceled` is absent on
#: purpose: only the sender cancels, so it would be told of its own act.
ANNOUNCED_TERMINAL = frozenset(TERMINAL - {"canceled"})
_DECIDED = tuple(sorted(DECISIONS))


#: THE ONE STATE MODEL of a message, for one actor (operator: "read/unread/
#: ack/unack/responded with status ... enforce schema!!"). Closed: a value
#: outside it is refused by validate_state_value, never coerced.
XAACP_STATE_SCHEMA: dict = {
    "role": ("recipient", "sender"),
    "read": bool,
    "ack": ("acked", "unacked"),
    "response": ("accepted", "completed", "blocked", "rejected", "none"),
    "lifecycle": ("open", "expired", "cancelled"),
    "delivered": bool,
    "received": bool,
    "in_flight": bool,
    "notified": bool,
    "outcome_seen": bool,
    # r0b2 P1-4: whether the STORED status is canonical, an explicitly mapped
    # legacy spelling, or corrupt -- never projected silently to a clean state.
    "integrity": ("ok", "legacy", "corrupt"),
    "stored_status": str,
}
#: Every status a message row may canonically hold.
CANONICAL_STATUSES = frozenset({"pending", *DECISIONS, "canceled", "expired"})
#: The EXPLICIT legacy compatibility map: stored spelling -> canonical.
LEGACY_STATUS_MAP = {"cancelled": "canceled"}


def canonical_status(stored: str) -> tuple[str, str]:
    """(canonical status, integrity) for a STORED status value: 'ok' when it
    is canonical, 'legacy' when the explicit map names it, 'corrupt' otherwise
    (then the canonical value is '' -- it answers to no state)."""
    value = str(stored or "")
    if value in CANONICAL_STATUSES:
        return value, "ok"
    if value in LEGACY_STATUS_MAP:
        return LEGACY_STATUS_MAP[value], "legacy"
    return "", "corrupt"


def validate_state_value(field: str, value) -> None:
    """Refuse (ValueError) a value the closed schema does not name."""
    allowed = XAACP_STATE_SCHEMA.get(field)
    if allowed is None:
        raise ValueError(f"unknown message-state field {field!r}")
    if allowed is bool:
        if not isinstance(value, bool):
            raise ValueError(f"message-state {field} must be a boolean, got {value!r}")
        return
    if allowed is str:
        if not isinstance(value, str):
            raise ValueError(f"message-state {field} must be a string, got {value!r}")
        return
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"message-state {field} must be one of {list(allowed)}, got {value!r}")


def validate_state(state: dict) -> dict:
    """The whole state, exactly the schema's fields, every value valid."""
    if set(state) != set(XAACP_STATE_SCHEMA):
        raise ValueError(f"message-state fields must be exactly {sorted(XAACP_STATE_SCHEMA)}")
    for field, value in state.items():
        validate_state_value(field, value)
    return state


def outcome_state(status: str, acked_at: object) -> str:
    """The sender-visible state of a message it sent, or "" if nothing to say.

    ``status`` is ``decision_status or status``. THE single rule both the
    writer's read model and claim_next use to decide what an outcome event
    means now.
    """
    current = str(status or "pending")
    if current in TERMINAL:
        return current if current in ANNOUNCED_TERMINAL else ""
    return ACKED if acked_at is not None else ""


def ensure_park_epoch(conn: sqlite3.Connection) -> str:
    """#1185: this park log's epoch, minting it on first use (idempotent).

    Called inside the park registration's own write so the epoch it returns
    is the one the generation it assigns belongs to."""
    conn.execute(
        "INSERT OR IGNORE INTO xaacp_park_meta (id, epoch) VALUES (1, ?)", (uuid.uuid4().hex,)
    )
    row = conn.execute("SELECT epoch FROM xaacp_park_meta WHERE id = 1").fetchone()
    return str(row[0])


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
        # #1104 heal provenance: the `abandoned_at` a legacy rail abandonment
        # carried before the heal reopened the row (history, never a filter).
        ("abandon_healed_from", "REAL"),
        # SERVER-side vs AGENT-side delivery (operator). `delivered_at` is the
        # server handing the text out; `received_at` is proof the agent got
        # it; `receipt_lease_expires_at` bounds an unproven wait_next delivery
        # (NULL on every pre-receipt row: those are never redelivered);
        # `notified_at` is a pointer naming it (not a delivery).
        ("received_at", "REAL"),
        ("received_via", "TEXT NOT NULL DEFAULT ''"),
        ("receipt_lease_expires_at", "REAL"),
        ("notified_at", "REAL"),
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
    # The park length the LAST enter asked for and was granted (after the
    # wait_next cap): the Stop hook bounds an in-flight park by it.
    park_cols = {row[1] for row in conn.execute("PRAGMA table_info(xaacp_park_log)").fetchall()}
    for column, ddl in (
        ("last_park_requested_seconds", "REAL"),
        ("last_park_granted_seconds", "REAL"),
        # WHY the last park returned and how long it ran, for the operator.
        ("last_return_reason", "TEXT NOT NULL DEFAULT ''"),
        ("last_elapsed_seconds", "REAL"),
        # Each park's own id: only the CURRENT park may record its return, so
        # an overtaken park never closes a newer one (no negative durations).
        ("last_park_id", "TEXT NOT NULL DEFAULT ''"),
        # #1185: the actor's park GENERATION, assigned by THIS server when a
        # park registers (never from a client or host clock). The receive
        # lease compares-and-sets on it.
        ("last_park_gen", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in park_cols:
            try:
                conn.execute(f"ALTER TABLE xaacp_park_log ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:
                # A concurrent initialiser added it first: the migration is done.
                if "duplicate column" not in str(exc).lower():
                    raise
    # #1185: the park log's EPOCH -- a random id minted ONCE, when this park
    # log (this database) is first set up, and never rewritten. Generations
    # are only comparable within one epoch: a recreated park log restarts its
    # generations at 1 under a NEW epoch, and the receive lease uses the epoch
    # to tell "an older park" from "a different authority". One row (id=1);
    # INSERT OR IGNORE keeps it idempotent and concurrent-safe.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS xaacp_park_meta ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), epoch TEXT NOT NULL)"
    )
    ensure_park_epoch(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xaacp_stream_reader "
        "ON xaacp_stream (session_id, reader_actor_id, seq)"
    )
    # The bounded subject list (r0b2 P1-1): keyset order per reader, and the
    # "newest event of this message for this reader" probe.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xaacp_stream_reader_time "
        "ON xaacp_stream (session_id, reader_actor_id, created_at, message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xaacp_stream_reader_message "
        "ON xaacp_stream (session_id, reader_actor_id, message_id, seq)"
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
    healed = conn.execute(
        "SELECT 1 FROM xaacp_stream_meta WHERE key=?", (_ABANDON_HEAL_KEY,)
    ).fetchone()
    if healed is None:
        count = heal_rail_abandoned(conn)
        conn.execute(
            "INSERT OR IGNORE INTO xaacp_stream_meta (key, value) VALUES (?, ?)",
            (_ABANDON_HEAL_KEY, f"{time.time()} healed={count}"),
        )


#: One-time #1104 migration marker (see :func:`heal_rail_abandoned`).
_ABANDON_HEAL_KEY = "stream_abandon_heal_v1"

#: THE DISCRIMINATOR for "abandoned by the notice-rail retry" (#1104).
#: `abandoned_at` had exactly ONE writer in every build that shipped it: the
#: claim_next branch taken when a RAIL (any surface but wait_next) found an
#: expired, never-confirmed claim of its own at MAX_RECLAIMS. That branch ran
#: only on an OPEN row (`delivered_at IS NULL AND superseded_at IS NULL`) that
#: held a prior claim (`claim_id != ''`) from a non-wait_next surface
#: (`claim_surface`, set by that claim). A wait_next claim surfaces in the same
#: CAS, so it can never be the abandoned claim. Every clause is required:
#:   * delivered_at IS NULL    -- never delivered (not a surfaced/acted-on row)
#:   * superseded_at IS NULL   -- not voided (canceled / expired / overtaken)
#:   * claim_id != ''          -- a claim existed to be retried
#:   * claim_surface NOT IN ('', 'wait_next') -- that claim was a rail's
_RAIL_ABANDONED_SQL = (
    "abandoned_at IS NOT NULL AND delivered_at IS NULL AND superseded_at IS NULL "
    "AND claim_id != '' AND claim_surface != '' AND claim_surface != ?"
)


def heal_rail_abandoned(conn: sqlite3.Connection) -> int:
    """CONTRACT MOVED (#1104): reopen rows the old rail-retry policy abandoned.

    Abandonment closed an entry for EVERY surface -- a decision or message no
    surface ever showed was silently lost and the durable cursor moved past
    it. Duplicate delivery is safer than permanent silent loss, so each such
    row is reopened ONCE for wait_next: `abandoned_at` moves to
    `abandon_healed_from` (history) and the rest of the claim state is kept,
    so the rails still step over it (reclaim_count stays at MAX_RECLAIMS).
    A healed OUTCOME that a later state overtook is superseded, not delivered,
    by claim_next's normal overtaken check. Returns rows healed.
    """
    return int(
        conn.execute(
            "UPDATE xaacp_stream SET abandon_healed_from=abandoned_at, abandoned_at=NULL WHERE "
            + _RAIL_ABANDONED_SQL,
            (SURFACE_WAIT_NEXT,),
        ).rowcount
        or 0
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
    # Acting on it is PROOF the agent received it (agent-side delivery).
    conn.execute(
        "UPDATE xaacp_stream SET received_at=?, received_via=? WHERE message_id=? "
        "AND reader_actor_id=? AND event_kind=? AND received_at IS NULL",
        (now, via, message_id, reader_actor_id, EVENT_INCOMING),
    )


def issue_receipts(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    message_id: str,
    via: str,
    now: float,
) -> list[str]:
    """ONE RECEIPT RULE for a full-text door that is not wait_next (the
    exact-id read, the agent inbox): this reader's not-yet-received events
    for the message are SERVER-delivered under a fresh receipt lease and
    carry a receipt token. Nothing is marked read or received -- the server
    returning a result proves nothing (r0b2 re-review P0-1/P0-2). An event
    already handed out and still unproven KEEPS its token, so one delivery
    never has two rival tokens. Returns the tokens."""
    where, params, _ = _scope(session_id, actor_id, lane_id, span_all_lanes)
    rows = conn.execute(
        f"SELECT seq, claim_id, delivered_at FROM xaacp_stream WHERE {where} AND message_id=? "
        "AND superseded_at IS NULL AND received_at IS NULL",
        [*params, message_id],
    ).fetchall()
    tokens: list[str] = []
    for seq, claim_id, delivered_at in rows:
        token = str(claim_id or "") if delivered_at is not None and claim_id else uuid.uuid4().hex[:16]
        conn.execute(
            "UPDATE xaacp_stream SET claim_id=?, claimed_at=?, claim_surface=?, "
            "delivered_via=CASE WHEN delivered_at IS NULL THEN ? ELSE delivered_via END, "
            "delivered_at=COALESCE(delivered_at, ?), receipt_lease_expires_at=? "
            "WHERE seq=? AND received_at IS NULL",
            (token, now, via, via, now, now + RECEIPT_LEASE_SECONDS, int(seq)),
        )
        tokens.append(token)
    return tokens


def confirm_by_tokens(
    conn: sqlite3.Connection, *, session_id: str, actor_id: str, tokens: list[str], now: float
) -> list[tuple[str, str, str]]:
    """AGENT-side delivery proven by an ECHOED receipt token: exactly this
    reader's still-current, delivered, unreceived event per token (a
    redelivery mints a new token, so a superseded one confirms nothing).
    Returns (message_id, event_kind, state) confirmed."""
    confirmed: list[tuple[str, str, str]] = []
    for token in dict.fromkeys(tokens):
        row = conn.execute(
            "SELECT seq, message_id, event_kind, state FROM xaacp_stream WHERE session_id=? "
            "AND reader_actor_id=? AND claim_id=? AND delivered_at IS NOT NULL AND received_at IS NULL",
            (session_id, actor_id, token),
        ).fetchone()
        if row is None:
            continue
        if conn.execute(
            "UPDATE xaacp_stream SET received_at=?, received_via='receipt' WHERE seq=? AND claim_id=? "
            "AND received_at IS NULL",
            (now, int(row[0]), token),
        ).rowcount:
            confirmed.append((str(row[1]), str(row[2]), str(row[3] or "")))
    return confirmed


def message_events(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    message_id: str,
) -> list[sqlite3.Row]:
    """READ-ONLY: this reader's own stream events for one message, in order."""
    where, params, _ = _scope(session_id, actor_id, lane_id, span_all_lanes)
    return conn.execute(
        f"SELECT seq, event_kind, state FROM xaacp_stream WHERE {where} AND message_id=? ORDER BY seq",
        [*params, message_id],
    ).fetchall()


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
#: How long a wait_next delivery may stay UNPROVEN (no agent receipt) before
#: wait_next may hand it out again (#1167 direction: in flight, not lost).
RECEIPT_LEASE_SECONDS = 300.0
#: A wait_next delivery the agent never proved it received, whose receipt
#: lease ran out: redeliverable to wait_next (never to a rail).
#: #1204: INCOMING events only. An OUTCOME is news about the reader's OWN send
#: (always re-readable from that message's state); wait_next marks it received
#: at hand-out, so there is nothing to redeliver. Excluding the kind here also
#: steps over outcome rows a pre-#1204 build left in flight -- those were
#: replayed forever, overtaken ack outcomes included, because the sender had no
#: working call to confirm them.
_REDELIVERABLE_SQL = (
    "event_kind='incoming' AND delivered_at IS NOT NULL AND received_at IS NULL "
    "AND receipt_lease_expires_at IS NOT NULL "
    "AND receipt_lease_expires_at<=? AND superseded_at IS NULL AND abandoned_at IS NULL"
)
#: An unconfirmed claim may be re-claimed by a RAIL exactly this many times;
#: after that the rails step over it, but it stays OPEN for wait_next (#1104).
MAX_RECLAIMS = 1

SURFACE_WAIT_NEXT = "wait_next"


def has_claimable(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    now: float,
) -> bool:
    """READ-ONLY: does this reader have an event claim_next could claim now?
    A superset of claim_next's candidates (no watermark floor), so False
    means claim_next would return None."""
    where, params, _ = _scope(session_id, actor_id, lane_id, span_all_lanes)
    return conn.execute(
        f"SELECT 1 FROM xaacp_stream WHERE {where} AND ((" + _CLAIMABLE_SQL + ") OR ("
        + _REDELIVERABLE_SQL + ")) LIMIT 1",
        [*params, now, now],
    ).fetchone() is not None


def next_claim_lease_expiry(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    now: float,
) -> float | None:
    """READ-ONLY: the earliest future moment an open, currently-claimed event
    of this reader becomes claimable again (its claim lease runs out)."""
    where, params, _ = _scope(session_id, actor_id, lane_id, span_all_lanes)
    value = conn.execute(
        f"SELECT MIN(claim_lease_expires_at) FROM xaacp_stream WHERE {where} AND " + _OPEN_SQL
        + " AND claim_id!='' AND claim_lease_expires_at>?",
        [*params, now],
    ).fetchone()[0]
    receipt = conn.execute(
        f"SELECT MIN(receipt_lease_expires_at) FROM xaacp_stream WHERE {where} AND event_kind='incoming' "
        "AND delivered_at IS NOT NULL "
        "AND received_at IS NULL AND receipt_lease_expires_at>? AND superseded_at IS NULL",
        [*params, now],
    ).fetchone()[0]
    due = [float(v) for v in (value, receipt) if v is not None]
    return min(due) if due else None


def receive_cursor(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    now: float | None = None,
    persist: bool = True,
    audit: list | None = None,
) -> int:
    """The server-held durable receive cursor (low watermark).

    It advances as events close. CONTRACT MOVED (#1104): it is no longer
    strictly monotonic -- a stored cursor found PAST an open (undelivered)
    event is healed DOWN to the recomputed watermark, because keeping it
    would skip that event forever. ``audit`` (if given) collects
    ``("cursor_healed", healed_to, "", stored_before)`` for each heal.
    """
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
    if low is not None and stored is not None and int(stored[0]) > watermark:
        # POISONED: the stored cursor is past an undelivered event (a legacy
        # build, or a row the #1104 abandon heal reopened). Heal it down.
        if persist:
            conn.execute(
                "UPDATE xaacp_receive_cursor SET cursor=?, updated_at=? "
                "WHERE session_id=? AND actor_id=? AND lane_key=?",
                (watermark, time.time() if now is None else now, session_id, actor_id, lane_key),
            )
            if audit is not None:
                audit.append(("cursor_healed", watermark, "", int(stored[0])))
        return watermark
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
    park_guard: tuple[str, str] | None = None,
) -> sqlite3.Row | None:
    """Atomically claim the next event (contract clause 2), two-phase.

    ``park_guard`` (#1185) is ``(park_id, ...)`` for a wait_next park: every
    claim UPDATE is conditional, IN THE SAME STATEMENT, on that park still being
    this actor's current ``xaacp_park_log`` row. A park that is not (or whose
    ownership cannot be read) claims nothing: there is no check-then-claim gap.

    ``via == 'wait_next'`` claims AND surfaces in one CAS (the return is the
    surfacing). Any other surface only CLAIMS: a lease-bounded in-flight claim
    with a fresh ``claim_id``, confirmed later by :func:`confirm_surfaced`. An
    expired unconfirmed claim is re-claimable by a rail exactly
    ``MAX_RECLAIMS`` times; after that the rails step over it and it waits,
    still open, for wait_next (#1104: never closed undelivered). ``audit`` (if
    given) collects ``(state, seq, claim_id, reclaim_count)`` for every
    claimed / reclaimed / surfaced step.

    ``after_cursor`` is a SCAN position for a caller walking one batch; it is
    not a way to skip undelivered events, and wait_next never passes a
    caller-supplied value here (#1104).
    """
    notes = audit if audit is not None else []
    at = time.time() if now is None else float(now)
    where, params, _ = _scope(session_id, actor_id, lane_id, span_all_lanes)
    guard_sql = ""
    guard_params: list[object] = []
    if park_guard is not None:
        guard_sql = (
            " AND EXISTS (SELECT 1 FROM xaacp_park_log WHERE session_id=? AND actor_id=? "
            "AND last_park_id=?)"
        )
        guard_params = [session_id, actor_id, str(park_guard[0])]
    start = max(
        receive_cursor(
            conn,
            session_id=session_id,
            actor_id=actor_id,
            lane_id=lane_id,
            span_all_lanes=span_all_lanes,
            now=at,
            audit=notes,
        ),
        int(after_cursor or 0),
    )
    if via == SURFACE_WAIT_NEXT:
        # REDELIVERY FIRST (operator: server- vs agent-side delivery). A
        # wait_next delivery the agent never proved it received, whose receipt
        # lease ran out, goes out again -- oldest first, wherever the cursor is.
        # #1204: it is a REDELIVERY, never a new entry: the
        # ("redelivery_of", seq, original_claim_id, None) note names the
        # receipt it replaces, so the caller can flag it as such.
        stale = conn.execute(
            f"SELECT seq, claim_id FROM xaacp_stream WHERE {where} AND " + _REDELIVERABLE_SQL
            + " ORDER BY seq LIMIT 1",
            [*params, at],
        ).fetchone()
        if stale is not None:
            claim_id = uuid.uuid4().hex[:16]
            if conn.execute(
                "UPDATE xaacp_stream SET claim_id=?, claimed_at=?, claim_surface=?, delivered_at=?, "
                "delivered_via=?, receipt_lease_expires_at=?, reclaim_count=reclaim_count+1 "
                "WHERE seq=? AND " + _REDELIVERABLE_SQL + guard_sql,
                (claim_id, at, via, at, via, at + RECEIPT_LEASE_SECONDS, int(stale[0]), at, *guard_params),
            ).rowcount:
                notes.append(("redelivered", int(stale[0]), claim_id, None))
                notes.append(("redelivery_of", int(stale[0]), str(stale[1] or ""), None))
                return event_row(conn, int(stale[0]))
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
        surfaced = via == SURFACE_WAIT_NEXT
        if prior_claim and reclaims >= MAX_RECLAIMS and not surfaced:
            # #1104: a RAIL whose claims keep going unconfirmed gives up on this
            # event -- it steps over it WITHOUT closing it. This used to stamp
            # `abandoned_at`, which closed the event for every surface: wait_next
            # then never delivered it and the durable cursor moved past it, so a
            # decision nobody ever saw was silently gone. It stays OPEN (holding
            # the watermark) for wait_next, whose return IS the surfacing.
            start = seq
            continue
        claim_id = uuid.uuid4().hex[:16]
        next_reclaims = reclaims + (1 if prior_claim else 0)
        won = conn.execute(
            "UPDATE xaacp_stream SET claim_id=?, claimed_at=?, claim_surface=?, "
            "claim_lease_expires_at=?, reclaim_count=?, delivered_at=?, delivered_via=?, "
            "receipt_lease_expires_at=?, notified_at=COALESCE(notified_at, ?) "
            "WHERE seq=? AND claim_id=? AND " + _CLAIMABLE_SQL + guard_sql,
            (
                claim_id,
                at,
                via,
                at + max(1.0, float(lease_seconds)),
                next_reclaims,
                at if surfaced else None,
                via if surfaced else "",
                # A wait_next hand-out is SERVER-delivered; until the agent
                # proves receipt it is in flight for RECEIPT_LEASE_SECONDS.
                at + RECEIPT_LEASE_SECONDS if surfaced else None,
                # A rail claim (the tool-result pointer) NOTIFIES, never delivers.
                None if surfaced else at,
                seq,
                prior_claim,
                at,
                *guard_params,
            ),
        ).rowcount
        if not won and park_guard is not None:
            # Lost to a guard (not a race for the event)? Then this park is no
            # longer the actor's current one: stop scanning, claim nothing.
            owned = conn.execute(
                "SELECT 1 FROM xaacp_park_log WHERE session_id=? AND actor_id=? AND last_park_id=?",
                (session_id, actor_id, str(park_guard[0])),
            ).fetchone()
            if owned is None:
                return None
        if won:
            notes.append(("reclaimed" if prior_claim else "claimed", seq, claim_id, next_reclaims))
            if surfaced:
                notes.append(("surfaced", seq, claim_id, next_reclaims))
            if surfaced and candidate[1] == EVENT_OUTCOME:
                # #1204: an OUTCOME handed out by wait_next is RECEIVED at
                # hand-out (same transaction). It reports the state of the
                # reader's own send, which stays readable from the message, so
                # a lost reply loses no obligation; leaving it in flight made
                # it redeliver after every receipt lease, forever, because the
                # sender has no ack to give for its own message. An INCOMING
                # request keeps the receipt rule (crash-safe redelivery).
                conn.execute(
                    "UPDATE xaacp_stream SET received_at=?, received_via=?, receipt_lease_expires_at=NULL "
                    "WHERE seq=? AND claim_id=? AND received_at IS NULL",
                    (at, via, seq, claim_id),
                )
                notes.append(("received_on_handout", seq, claim_id, next_reclaims))
            receive_cursor(
                conn,
                session_id=session_id,
                actor_id=actor_id,
                lane_id=lane_id,
                span_all_lanes=span_all_lanes,
                now=at,
                audit=notes,
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
    return outcome_state(str(row[0] or row[1] or "pending"), row[2])


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
