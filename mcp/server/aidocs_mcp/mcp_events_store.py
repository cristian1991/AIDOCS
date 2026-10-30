"""Lane 1 MCP Events: wake bindings, subscriptions and the wake outbox (transport-free).

A wake is a HINT, never the transport. The XAACP message lives in its own durable row
that wait_next reads; this store only remembers that a subscribed ChatGPT Work chat
should be nudged to go and read it. Everything here fails toward "no wake", never
toward a lost or delayed message.

Design rev 3 (.MEMORY/sessions/ubermega/lane1-mcp-events-wake-design.md), answering
r0b1 BLOCKs be6e382d-11b, 2a9578cf-ec8 and 62ebce88-da0:

  * WHO IS WOKEN. OAuth names the USER, not a Work chat, and `openai/session` is an
    untrusted client selector. An ordinary tools/call (ai_msg wake_enroll), whose
    caller resolves the chat's EXISTING exact XAACP actor binding, records a durable
    WAKE BINDING here and gets back an opaque `binding_id`. That id is the only event
    argument, so refresh and unsubscribe can resend the original arguments days later
    (the spec requires it). Holding the id grants nothing: every use re-checks it
    against the authenticated principal, and a foreign, revoked or unknown id is
    simply not found.
  * NO UNVERIFIED SECRET. A subscription is written only after its callback passed the
    signed challenge (challenge_passed_at), so a refresh can never swap in a secret
    nobody has verified.
  * FAIL-OPEN. note_message_fail_open runs inside SAVEPOINT wake and contains every
    failure, including a refused SAVEPOINT and a raising error reporter; the caller's
    message commits as before.
  * SIGNED BYTES. At most one `open` row per subscription, and only `open` rows are
    coalesced into. The first claim serializes the body ONCE; retries resend those
    bytes under the same event_id. A message arriving after a claim opens the next
    generation.
  * ATOMIC CLAIM, OWNED SETTLEMENT. claim_due and record_attempt each run in one
    BEGIN IMMEDIATE transaction, so two workers cannot both read the same budget or
    row. Every claim carries a fresh lease token; only the holder of the current
    lease settles the event, so a late or superseded worker changes nothing.
  * BUDGET. Per subscription: one delivery per DEBOUNCE unless the sender asked for a
    wake. Per principal, across all its subscriptions: PRINCIPAL_PER_HOUR and
    PRINCIPAL_PER_DAY (budget_caps: operator-raisable up to PER_*_CEILING), counted
    when an event is first claimed. A PARKED event sent nothing, so parking gives
    its slot back and its retry is budget-checked and counted like a first claim.
    Excess just coalesces.
  * SCOPE. A subscription IS its (binding_id, event, callback): exactly the original
    events/subscribe arguments plus callback that the spec says identify it. Two
    chats on the same actor and callback hold different bindings, so they can never
    alias, refresh or unsubscribe each other (r0b1 0bcbae54-ad4).
  * REPEATS ARE IDEMPOTENT. bind_wake and subscribe insert-or-ignore first and read
    after, so an identical call that loses a race returns the winner's row instead
    of an IntegrityError.

The secret is stored only sealed (mcp_events_secret); this module never sees it in
plaintext. Expired/cancelled messages and outcome entries are filtered by the caller,
which knows the entry kind; self-sent messages are filtered here. Connections must use
sqlite3.Row as row_factory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Callable

from .mcp_events_secret import Sealed

logger = logging.getLogger("aidocs.mcp_events_store")

ROTATION_OVERLAP_SECONDS = 86400
DEBOUNCE_SECONDS = 120
PRINCIPAL_PER_HOUR = 20
PRINCIPAL_PER_DAY = 400
#: The operator may raise the per-principal caps for dogfood (r0b1 cde7a51b-31f),
#: never above these: OpenAI allows 30/h and 720/day across ALL of a user's event
#: tasks, and these keep 2/h and 20/day of that free.
ENV_PER_HOUR = "AIDOCS_EVENTS_WAKES_PER_HOUR"
ENV_PER_DAY = "AIDOCS_EVENTS_WAKES_PER_DAY"
PER_HOUR_CEILING = 28
PER_DAY_CEILING = 700
MAX_ATTEMPTS = 8
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 1800

_BINDING_FIELDS = (
    "principal_id", "org_id", "project_id", "session_id", "recipient_actor_id", "host_session_digest",
)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS mcp_event_bindings (
        binding_id TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL, org_id TEXT NOT NULL, project_id TEXT NOT NULL,
        session_id TEXT NOT NULL, recipient_actor_id TEXT NOT NULL,
        host_session_digest TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL, revoked_at REAL
    )""",
    # One live binding per chat; a revoked one may be followed by a fresh one.
    """CREATE UNIQUE INDEX IF NOT EXISTS mcp_event_bindings_one_active
        ON mcp_event_bindings (principal_id, org_id, project_id, session_id,
                               recipient_actor_id, host_session_digest)
        WHERE status = 'active'""",
    """CREATE TABLE IF NOT EXISTS mcp_event_subscriptions (
        id TEXT PRIMARY KEY,
        binding_id TEXT NOT NULL,
        principal_id TEXT NOT NULL, org_id TEXT NOT NULL, project_id TEXT NOT NULL,
        session_id TEXT NOT NULL, recipient_actor_id TEXT NOT NULL,
        event_name TEXT NOT NULL, callback_url TEXT NOT NULL,
        secret_ct BLOB NOT NULL, nonce BLOB NOT NULL, kek_id TEXT NOT NULL,
        prev_secret_ct BLOB, prev_nonce BLOB, prev_kek_id TEXT, prev_valid_until REAL,
        verified_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        refresh_before REAL NOT NULL, last_delivered_at REAL,
        cursor TEXT, status TEXT NOT NULL DEFAULT 'active',
        UNIQUE (binding_id, event_name, callback_url)
    )""",
    """CREATE TABLE IF NOT EXISTS mcp_event_outbox (
        event_id TEXT PRIMARY KEY,
        subscription_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        state TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        pending_count INTEGER NOT NULL,
        newest_message_id TEXT NOT NULL,
        body_bytes BLOB,
        lease_token TEXT,
        claimed_until REAL, first_claimed_at REAL, delivered_at REAL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL,
        last_status TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )""",
    # The coalescing invariant: at most one row a new message may still change.
    """CREATE UNIQUE INDEX IF NOT EXISTS mcp_event_outbox_one_open
        ON mcp_event_outbox (subscription_id) WHERE state = 'open'""",
)


@dataclass(frozen=True)
class Binding:
    """Who a subscription wakes, fixed server-side by wake_enroll. Never caller-supplied."""

    principal_id: str
    org_id: str
    project_id: str
    session_id: str
    recipient_actor_id: str
    host_session_digest: str


@dataclass(frozen=True)
class Claim:
    event_id: str
    subscription_id: str
    body_bytes: bytes
    attempts: int
    #: Proof of holding THIS lease; record_attempt settles only with the current one.
    lease_token: str
    #: The key the wake ticket is derived under, recorded before the first POST
    #: (#1226 continuation). None on a first claim, and on a legacy row POSTed
    #: before tickets existed.
    ticket_kek_id: str | None = None


#: Columns added after the outbox first shipped (#1226 continuation, design §10):
#: the ticket's key id and its single-use consumption. Never the ticket itself.
_OUTBOX_ADDED_COLUMNS = (
    ("ticket_kek_id", "TEXT"),
    ("ticket_consumed_by", "TEXT"),
    ("ticket_consumed_at", "REAL"),
    # Operator decision B (2026-10-30): the OAuth client_id the ticket is bound to,
    # copied from the binding at ISSUANCE (record_ticket_kek). NULL = a legacy
    # ticket issued before this column: user-bearer scope until it expires.
    ("ticket_client_id", "TEXT"),
    # r0b2 B5: stamped by EVERY issuance from this code on. NULL therefore means
    # "issued before the migration" -- the ONLY case a client-less ticket is
    # honoured (legacy, until it expires). A client-less ticket WITH a version was
    # issued after the migration and is refused (ticket_unbound).
    ("ticket_issuer_version", "INTEGER"),
)
#: The issuer version this code stamps on every ticket it issues.
TICKET_ISSUER_VERSION = 2
#: Columns added to the bindings after they first shipped. client_id is the OAuth
#: client that enrolled the wake (wake_enroll); NULL = a legacy binding.
_BINDING_ADDED_COLUMNS = (
    ("client_id", "TEXT"),
    # Q1, operator decision: "the agent may deactivate or pause the wake". NULL or
    # 'active' = wakes fire; 'paused' = none is enqueued or delivered. Deactivate
    # is status='revoked' (terminal until a fresh wake_enroll).
    ("wake_state", "TEXT"),
    ("wake_state_at", "REAL"),
)
WAKE_ACTIVE = "active"
WAKE_PAUSED = "paused"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema and MIGRATE an older store in place (idempotent): every
    added column is nullable, so existing rows keep working as legacy rows."""
    for ddl in _SCHEMA:
        conn.execute(ddl)
    for table, added in (("mcp_event_outbox", _OUTBOX_ADDED_COLUMNS), ("mcp_event_bindings", _BINDING_ADDED_COLUMNS)):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in added:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@contextmanager
def _write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    """One IMMEDIATE transaction: the write lock is taken BEFORE anything is read."""
    if conn.in_transaction:
        raise RuntimeError("mcp_events_store: claim/settle must own their transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _where_binding() -> str:
    return " AND ".join(f"{f} = ?" for f in _BINDING_FIELDS)


def _binding_values(binding: Binding) -> tuple[str, ...]:
    """In _BINDING_FIELDS order."""
    return (
        binding.principal_id, binding.org_id, binding.project_id,
        binding.session_id, binding.recipient_actor_id, binding.host_session_digest,
    )


def bind_wake(conn: sqlite3.Connection, binding: Binding, *, now: float, client_id: str = "") -> str:
    """The durable, opaque binding_id for this chat's actor (the same one each time).
    The caller has already resolved `binding` from the chat's exact XAACP binding.

    `client_id` is the AUTHENTICATED OAuth client enrolling the wake (operator
    decision B): recorded once, on a new binding or a legacy one with none, and
    never overwritten, so every ticket this binding issues is bound to it."""
    values = _binding_values(binding)
    # Ignored when this chat already has an active binding (the partial unique index).
    conn.execute(
        f"INSERT OR IGNORE INTO mcp_event_bindings (binding_id, {', '.join(_BINDING_FIELDS)}, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("wb_" + secrets.token_urlsafe(18), *values, now),
    )
    binding_id = conn.execute(
        f"SELECT binding_id FROM mcp_event_bindings WHERE {_where_binding()} AND status = 'active'",
        values,
    ).fetchone()["binding_id"]
    if client_id:
        conn.execute(
            "UPDATE mcp_event_bindings SET client_id = ? WHERE binding_id = ? AND client_id IS NULL",
            (client_id, binding_id),
        )
    return binding_id


def own_active_binding(conn: sqlite3.Connection, binding: Binding) -> str | None:
    """The ACTIVE binding_id whose every field equals `binding` (the caller's own,
    derived server-side exactly as wake_enroll derives it), or None. There is no
    id argument: a caller can only ever find its own binding."""
    row = conn.execute(
        f"SELECT binding_id FROM mcp_event_bindings WHERE {_where_binding()} AND status = 'active'",
        _binding_values(binding),
    ).fetchone()
    return str(row["binding_id"]) if row is not None else None


def set_wake_state(conn: sqlite3.Connection, binding_id: str, *, state: str, now: float) -> bool:
    """Pause or unpause an ACTIVE binding's wakes. True when the binding is active."""
    if state not in (WAKE_ACTIVE, WAKE_PAUSED):
        raise ValueError("set_wake_state: state must be active or paused")
    cur = conn.execute(
        "UPDATE mcp_event_bindings SET wake_state = ?, wake_state_at = ? "
        "WHERE binding_id = ? AND status = 'active'",
        (state, now, binding_id),
    )
    return cur.rowcount == 1


def resolve_binding(conn: sqlite3.Connection, binding_id: str, *, principal_id: str) -> Binding | None:
    """The binding behind an id, for its own principal only. Foreign, revoked and unknown
    ids are all just None (not found)."""
    row = conn.execute(
        f"SELECT {', '.join(_BINDING_FIELDS)} FROM mcp_event_bindings "
        "WHERE binding_id = ? AND principal_id = ? AND status = 'active'",
        (str(binding_id or ""), principal_id),
    ).fetchone()
    return Binding(*tuple(row)) if row is not None else None


def revoke_binding(conn: sqlite3.Connection, binding_id: str, *, now: float) -> None:
    """The chat's actor binding is gone: the id stops resolving and its subscriptions end."""
    conn.execute(
        "UPDATE mcp_event_bindings SET status = 'revoked', revoked_at = ? "
        "WHERE binding_id = ? AND status = 'active'",
        (now, binding_id),
    )
    for (sub_id,) in conn.execute(
        "SELECT id FROM mcp_event_subscriptions WHERE binding_id = ? AND status = 'active'",
        (binding_id,),
    ).fetchall():
        _end_subscription(conn, sub_id, status="revoked", reason="binding_revoked", now=now)


def subscription_key(binding_id: str, *, event_name: str, callback_url: str) -> bytes:
    """The subscription's identity (the original event arguments plus callback); also
    the AAD its sealed secret is bound to."""
    return json.dumps(
        {"binding_id": binding_id, "event_name": event_name, "callback_url": callback_url},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def subscription_id(binding_id: str, *, event_name: str, callback_url: str) -> str:
    key = subscription_key(binding_id, event_name=event_name, callback_url=callback_url)
    return "sub_" + hashlib.sha256(key).hexdigest()[:32]


def get_subscription(conn: sqlite3.Connection, sub_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM mcp_event_subscriptions WHERE id = ?", (sub_id,)).fetchone()


def subscribe(
    conn: sqlite3.Connection,
    binding_id: str,
    *,
    principal_id: str,
    event_name: str,
    callback_url: str,
    sealed: Sealed,
    refresh_before: float | None,
    challenge_passed_at: float | None,
    now: float,
) -> str | None:
    """Create or refresh the subscription for exactly this binding, event and callback,
    or None when the binding is not this principal's. Called only AFTER the callback
    passed the signed challenge with `sealed`'s secret. A new secret keeps the previous
    one valid for ROTATION_OVERLAP_SECONDS (both are signed with)."""
    if refresh_before is None:
        raise ValueError("refresh_before is required: subscriptions never live forever")
    if challenge_passed_at is None:
        raise ValueError("only a callback that passed the signed challenge is stored")
    binding = resolve_binding(conn, binding_id, principal_id=principal_id)
    if binding is None:
        return None
    sid = subscription_id(binding_id, event_name=event_name, callback_url=callback_url)
    b = asdict(binding)
    cur = conn.execute(
        "INSERT OR IGNORE INTO mcp_event_subscriptions (id, binding_id, principal_id, org_id, "
        "project_id, session_id, recipient_actor_id, event_name, callback_url, "
        "secret_ct, nonce, kek_id, verified_at, created_at, updated_at, refresh_before, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
        (
            sid, binding_id, b["principal_id"], b["org_id"], b["project_id"], b["session_id"],
            b["recipient_actor_id"], event_name, callback_url,
            sealed.ciphertext, sealed.nonce, sealed.kek_id,
            challenge_passed_at, now, now, refresh_before,
        ),
    )
    if cur.rowcount == 1:
        return sid
    current = get_subscription(conn, sid)
    rotated = bytes(current["secret_ct"]) != bytes(sealed.ciphertext)
    conn.execute(
        "UPDATE mcp_event_subscriptions SET "
        "prev_secret_ct = ?, prev_nonce = ?, prev_kek_id = ?, prev_valid_until = ?, "
        "secret_ct = ?, nonce = ?, kek_id = ?, verified_at = ?, updated_at = ?, "
        "refresh_before = ?, status = 'active' WHERE id = ? AND binding_id = ?",
        (
            current["secret_ct"] if rotated else current["prev_secret_ct"],
            current["nonce"] if rotated else current["prev_nonce"],
            current["kek_id"] if rotated else current["prev_kek_id"],
            now + ROTATION_OVERLAP_SECONDS if rotated else current["prev_valid_until"],
            sealed.ciphertext, sealed.nonce, sealed.kek_id,
            challenge_passed_at, now, refresh_before, sid, binding_id,
        ),
    )
    return sid


def _end_subscription(conn: sqlite3.Connection, sub_id: str, *, status: str, reason: str, now: float) -> None:
    conn.execute(
        "UPDATE mcp_event_subscriptions SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, sub_id),
    )
    conn.execute(
        "UPDATE mcp_event_outbox SET state = 'dead', last_status = ?, lease_token = NULL "
        "WHERE subscription_id = ? AND state IN ('open', 'claimed', 'retry')",
        (reason, sub_id),
    )


def revoke_subscription(conn: sqlite3.Connection, sub_id: str, *, reason: str, now: float) -> None:
    """Delivery-time authority loss (ACL gone): stop for good."""
    _end_subscription(conn, sub_id, status="revoked", reason=reason, now=now)


def unsubscribe(
    conn: sqlite3.Connection,
    binding_id: str,
    *,
    principal_id: str,
    event_name: str,
    callback_url: str,
    now: float,
) -> bool:
    """End the subscription these original arguments name; queued wakes are not sent."""
    if resolve_binding(conn, binding_id, principal_id=principal_id) is None:
        return False
    sid = subscription_id(binding_id, event_name=event_name, callback_url=callback_url)
    row = get_subscription(conn, sid)
    if row is None or row["status"] != "active" or row["binding_id"] != binding_id:
        return False
    _end_subscription(conn, sid, status="revoked", reason="unsubscribed", now=now)
    return True


def note_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    recipient_actor_id: str,
    sender_actor_id: str,
    message_id: str,
    now: float,
    wake: bool = False,
) -> int:
    """Record that `recipient_actor_id` has a new incoming message; returns how many
    subscriptions will be nudged. Coalesces into the subscription's open row.

    The store lives in the project's own conductor_comms database, so the project is
    the database: a message's (possibly unresolved) project id is not matched here."""
    if sender_actor_id == recipient_actor_id:
        return 0
    subs = conn.execute(
        "SELECT s.id FROM mcp_event_subscriptions s JOIN mcp_event_bindings b "
        "ON b.binding_id = s.binding_id AND b.status = 'active' "
        "AND COALESCE(b.wake_state, 'active') = 'active' "  # a paused wake enqueues nothing
        "WHERE s.session_id = ? AND s.recipient_actor_id = ? "
        "AND s.status = 'active' AND s.refresh_before > ?",
        (session_id, recipient_actor_id, now),
    ).fetchall()
    for (sub_id,) in subs:
        cur = conn.execute(
            "UPDATE mcp_event_outbox SET pending_count = pending_count + 1, "
            "newest_message_id = ?, priority = MAX(priority, ?) "
            "WHERE subscription_id = ? AND state = 'open'",
            (message_id, 1 if wake else 0, sub_id),
        )
        if cur.rowcount:
            continue
        (generation,) = conn.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 FROM mcp_event_outbox WHERE subscription_id = ?",
            (sub_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO mcp_event_outbox (event_id, subscription_id, generation, state, "
            "priority, pending_count, newest_message_id, created_at) "
            "VALUES (?, ?, ?, 'open', ?, 1, ?, ?)",
            ("evt_" + uuid.uuid4().hex, sub_id, generation, 1 if wake else 0, message_id, now),
        )
    return len(subs)


def note_message_fail_open(conn, *, on_error: Callable[[BaseException], None], **kwargs) -> int:
    """note_message inside SAVEPOINT wake. Any failure (the savepoint itself, the wake
    writes, the release) rolls back ONLY the wake work and is reported to `on_error`;
    a raising reporter is contained too. The caller's message is never affected."""

    def report(exc: BaseException) -> None:
        try:
            on_error(exc)
        except Exception:  # noqa: BLE001 - reporting a wake failure is best-effort
            pass

    try:
        conn.execute("SAVEPOINT wake")
    except Exception as exc:  # noqa: BLE001 - no savepoint, no wake work at all
        report(exc)
        return 0
    try:
        n = note_message(conn, **kwargs)
        conn.execute("RELEASE wake")
        return n
    except Exception as exc:  # noqa: BLE001 - a wake failure must never fail the message
        try:
            conn.execute("ROLLBACK TO wake")
            conn.execute("RELEASE wake")
        except Exception:  # noqa: BLE001 - already reporting the original failure
            pass
        report(exc)
        return 0


def budget_caps(env=None) -> tuple[int, int]:
    """(per hour, per day) for one principal: the defaults unless the operator set
    ENV_PER_HOUR / ENV_PER_DAY, each clamped to its ceiling. A value that is not a
    positive integer is logged and the default used."""
    env = os.environ if env is None else env

    def read(name: str, default: int, ceiling: int) -> int:
        raw = str(env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value < 1:
            logger.warning("%s=%r is not a positive integer; using %d", name, raw, default)
            return default
        return min(value, ceiling)

    return read(ENV_PER_HOUR, PRINCIPAL_PER_HOUR, PER_HOUR_CEILING), read(ENV_PER_DAY, PRINCIPAL_PER_DAY, PER_DAY_CEILING)


def _over_budget(conn: sqlite3.Connection, principal_id: str, now: float, caps: tuple[int, int]) -> bool:
    def started_since(since: float) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM mcp_event_outbox o JOIN mcp_event_subscriptions s "
            "ON s.id = o.subscription_id WHERE s.principal_id = ? AND o.first_claimed_at > ?",
            (principal_id, since),
        ).fetchone()[0]

    per_hour, per_day = caps
    return started_since(now - 3600) >= per_hour or started_since(now - 86400) >= per_day


def _backoff(attempts: int) -> float:
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS)


def claim_due(
    conn: sqlite3.Connection,
    *,
    now: float,
    serialize: Callable[[sqlite3.Row], bytes],
    lease_seconds: float = 30,
    debounce_seconds: float = DEBOUNCE_SECONDS,
    is_stale: Callable[[sqlite3.Row, str], bool] | None = None,
    reserve: Callable[[sqlite3.Row, str], bool] | None = None,
) -> Claim | None:
    """The next event the delivery worker should send, leased to it, or None.

    Runs in its own IMMEDIATE transaction. A first claim freezes the body (serialize
    runs exactly once per event); a retry or an expired lease returns the same bytes
    under a NEW lease token. `is_stale(subscription, newest_message_id)` lets the
    worker skip a wake whose messages the recipient has already received.

    `reserve(row, lease_token)` admits a first claim against the principal's budget,
    under THIS claim's lease token, so a refund can only ever give back its own
    claim's slot. The gate passes its global ledger (mcp_events_budget) here, because
    a store only sees its own project's wakes; without one, this store's own count
    is used."""
    caps = budget_caps()

    def admitted(row, token: str) -> bool:
        if reserve is not None:
            return bool(reserve(row, token))
        return not _over_budget(conn, row["principal_id"], now, caps)

    with _write_txn(conn):
        rows = conn.execute(
            "SELECT o.*, s.principal_id, s.session_id, s.event_name, s.binding_id, s.status AS sub_status, "
            "s.refresh_before, s.last_delivered_at, b.status AS binding_status, "
            "b.wake_state AS wake_state FROM mcp_event_outbox o "
            "JOIN mcp_event_subscriptions s ON s.id = o.subscription_id "
            "JOIN mcp_event_bindings b ON b.binding_id = s.binding_id "
            "WHERE o.state = 'open' OR (o.state = 'retry' AND o.next_attempt_at <= ?) "
            "OR (o.state = 'claimed' AND o.claimed_until <= ?) "
            "ORDER BY o.created_at, o.generation",
            (now, now),
        ).fetchall()
        for row in rows:
            event_id = row["event_id"]
            if row["binding_status"] == "active" and row["wake_state"] == WAKE_PAUSED:
                continue  # held while paused (never sent); unpause releases it
            if (
                row["sub_status"] != "active"
                or row["binding_status"] != "active"
                or row["refresh_before"] <= now
            ):
                conn.execute(
                    "UPDATE mcp_event_outbox SET state = 'dead', lease_token = NULL, "
                    "last_status = 'subscription_inactive' WHERE event_id = ?",
                    (event_id,),
                )
                continue
            token = uuid.uuid4().hex
            if row["state"] != "open":
                if row["attempts"] >= MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE mcp_event_outbox SET state = 'dead', lease_token = NULL, "
                        "last_status = 'max_attempts' WHERE event_id = ?",
                        (event_id,),
                    )
                    continue
                # A parked event gave its budget slot back (it sent nothing).
                if row["first_claimed_at"] is None and not admitted(row, token):
                    continue
                conn.execute(
                    "UPDATE mcp_event_outbox SET state = 'claimed', lease_token = ?, "
                    "claimed_until = ?, attempts = attempts + 1, "
                    "first_claimed_at = COALESCE(first_claimed_at, ?) WHERE event_id = ?",
                    (token, now + lease_seconds, now, event_id),
                )
                return Claim(
                    event_id, row["subscription_id"], bytes(row["body_bytes"]), row["attempts"] + 1, token,
                    row["ticket_kek_id"],
                )
            if not row["priority"]:
                last = row["last_delivered_at"]
                if row["created_at"] + debounce_seconds > now or (
                    last is not None and last + debounce_seconds > now
                ):
                    continue
            # Stale before budget: a skipped wake sends nothing, so it takes no slot.
            if is_stale is not None and is_stale(row, row["newest_message_id"]):
                conn.execute(
                    "UPDATE mcp_event_outbox SET state = 'delivered', last_status = 'stale_skipped' "
                    "WHERE event_id = ?",
                    (event_id,),
                )
                continue
            if not admitted(row, token):
                continue
            body = bytes(serialize(row))
            conn.execute(
                "UPDATE mcp_event_outbox SET state = 'claimed', body_bytes = ?, lease_token = ?, "
                "claimed_until = ?, first_claimed_at = ?, attempts = 1 WHERE event_id = ?",
                (body, token, now + lease_seconds, now, event_id),
            )
            return Claim(event_id, row["subscription_id"], body, 1, token)
    return None


#: How long after its first claim a wake ticket can still be redeemed (design §4, §9(6)).
TICKET_TTL_SECONDS = 3600
#: Older events are not even candidates: a stale ticket is simply not found.
_TICKET_LOOKBACK_SECONDS = 86400
#: States in which the event was handed to the network at least once (§10(3)).
_REDEEMABLE_STATES = ("claimed", "retry", "delivered")


@dataclass(frozen=True)
class Redemption:
    #: redeemed | not_found | expired | client_mismatch | unbound
    outcome: str
    event_id: str = ""
    binding_id: str = ""
    binding: Binding | None = None


def redeem_ticket(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    principal_id: str,
    ticket: object,
    run_key: str,
    now: float,
    verify: Callable[[str, str | None, object], bool],
    client_id: str | None = None,
) -> Redemption:
    """Verify a wake ticket against THIS binding's sent events and consume it for
    `run_key` (design §10(2) step 2), in one IMMEDIATE transaction.

    Redeems only an event of an active binding of this principal, on an active,
    unexpired subscription, that was handed to the network at least once (attempts
    >= 1 in claimed, retry or delivered), within TICKET_TTL_SECONDS of its first
    claim, and that is unconsumed or already consumed by this same run key. Every
    other case is not_found (unknown, foreign, never sent, dead, another run's), or
    expired. `verify(event_id, kek_id, ticket)` is the keyring's constant-time check;
    the ticket itself is never written anywhere.

    OPERATOR DECISION B: a ticket bound to a client (ticket_client_id) redeems only
    for that exact authenticated `client_id`; anything else (another client, or no
    client given) is client_mismatch and consumes NOTHING. A client-less ticket is
    honoured (user-bearer scope, until it expires) ONLY when it was issued before
    the migration (no ticket_issuer_version); one issued after it is unbound and
    refused (r0b2 B5)."""
    if not run_key:
        raise ValueError("redeem_ticket: run_key is required")
    not_found = Redemption("not_found")
    with _write_txn(conn):
        binding = resolve_binding(conn, binding_id, principal_id=principal_id)
        if binding is None:
            return not_found
        rows = conn.execute(
            "SELECT o.event_id, o.ticket_kek_id, o.first_claimed_at, o.ticket_consumed_by, o.ticket_client_id, "
            "o.ticket_issuer_version "
            "FROM mcp_event_outbox o JOIN mcp_event_subscriptions s ON s.id = o.subscription_id "
            "WHERE s.binding_id = ? AND s.principal_id = ? AND s.status = 'active' AND s.refresh_before > ? "
            f"AND o.state IN ({', '.join('?' for _ in _REDEEMABLE_STATES)}) AND o.attempts >= 1 "
            "AND o.ticket_kek_id IS NOT NULL AND o.first_claimed_at IS NOT NULL AND o.first_claimed_at > ?",
            (binding_id, principal_id, now, *_REDEEMABLE_STATES, now - _TICKET_LOOKBACK_SECONDS),
        ).fetchall()
        # Check every candidate (a handful), so the time taken does not say which matched.
        match = None
        for row in rows:
            if verify(row["event_id"], row["ticket_kek_id"], ticket) and match is None:
                match = row
        if match is None:
            return not_found
        if match["first_claimed_at"] + TICKET_TTL_SECONDS < now:
            return Redemption("expired")
        bound = match["ticket_client_id"]
        if bound is None and match["ticket_issuer_version"] is not None:
            return Redemption("unbound")  # client-less but issued after the migration
        if bound is not None and (client_id is None or str(client_id) != str(bound)):
            return Redemption("client_mismatch")  # consumes nothing
        consumed_by = match["ticket_consumed_by"]
        if consumed_by is None:
            conn.execute(
                "UPDATE mcp_event_outbox SET ticket_consumed_by = ?, ticket_consumed_at = ? "
                "WHERE event_id = ? AND ticket_consumed_by IS NULL",
                (run_key, now, match["event_id"]),
            )
        elif consumed_by != run_key:
            return not_found
        return Redemption("redeemed", match["event_id"], binding_id, binding)


def record_ticket_kek(conn: sqlite3.Connection, claim: Claim, kek_id: str) -> bool:
    """Record the key the wake ticket is derived under, before the first POST, so every
    retry derives the same ticket. Lease-fenced: only the current holder writes, and
    only once. False means the lease was lost (send nothing).

    This is the ticket's ISSUANCE, so it also binds the ticket to the client that
    enrolled the wake (the binding's client_id; operator decision B) and stamps
    TICKET_ISSUER_VERSION. A binding WITHOUT a client issues nothing (r0b2 B5):
    the caller checks binding_client first; this refuses it again here."""
    client_sql = (
        "(SELECT b.client_id FROM mcp_event_subscriptions s "
        "JOIN mcp_event_bindings b ON b.binding_id = s.binding_id "
        "WHERE s.id = mcp_event_outbox.subscription_id)"
    )
    with _write_txn(conn):
        cur = conn.execute(
            f"UPDATE mcp_event_outbox SET ticket_kek_id = ?, ticket_client_id = {client_sql}, "
            "ticket_issuer_version = ? "
            "WHERE event_id = ? AND state = 'claimed' "
            f"AND lease_token = ? AND ticket_kek_id IS NULL AND COALESCE({client_sql}, '') != ''",
            (kek_id, TICKET_ISSUER_VERSION, claim.event_id, claim.lease_token),
        )
        return cur.rowcount == 1


def binding_client(conn: sqlite3.Connection, subscription_id: str) -> str:
    """The client_id of the binding behind a subscription, or "" (none, unknown)."""
    row = conn.execute(
        "SELECT b.client_id FROM mcp_event_subscriptions s JOIN mcp_event_bindings b "
        "ON b.binding_id = s.binding_id WHERE s.id = ?",
        (subscription_id,),
    ).fetchone()
    return str(row[0] or "") if row is not None else ""


def drop_unbound_claim(conn: sqlite3.Connection, claim: Claim) -> bool:
    """r0b2 B5: a binding without a client issues NO ticket. The claimed event is
    dropped (dead, binding_without_client), lease-fenced; nothing was sent. The
    chat re-enrolls (wake_enroll fills its binding's client) to be woken again."""
    with _write_txn(conn):
        cur = conn.execute(
            "UPDATE mcp_event_outbox SET state = 'dead', lease_token = NULL, claimed_until = NULL, "
            "last_status = 'binding_without_client' WHERE event_id = ? AND state = 'claimed' AND lease_token = ?",
            (claim.event_id, claim.lease_token),
        )
        return cur.rowcount == 1


def wake_hold_reason(conn: sqlite3.Connection, subscription_id: str) -> str:
    """r0b2 B6, the pre-POST fence: "" when the binding may still be woken, else
    "paused" or "binding_inactive". Read fresh, immediately before the POST, so a
    pause or deactivate committed after the claim stops the send."""
    row = conn.execute(
        "SELECT b.status, b.wake_state FROM mcp_event_subscriptions s JOIN mcp_event_bindings b "
        "ON b.binding_id = s.binding_id WHERE s.id = ?",
        (subscription_id,),
    ).fetchone()
    if row is None or row[0] != "active":
        return "binding_inactive"
    return "paused" if row[1] == WAKE_PAUSED else ""


def park_claim(conn: sqlite3.Connection, claim: Claim, *, until: float, reason: str) -> bool:
    """Give a claimed event back WITHOUT spending its attempt: nothing was sent, and the
    cause (a key missing from the keyring, say) is an outage the operator can fix, not
    a verdict on the subscription. It is retried at `until` with the same bytes. If it
    was never POSTed it also gives its budget slot back (the retry is counted when it
    is claimed); one that already reached the callback keeps its slot."""
    with _write_txn(conn):
        cur = conn.execute(
            "UPDATE mcp_event_outbox SET state = 'retry', lease_token = NULL, claimed_until = NULL, "
            "next_attempt_at = ?, attempts = MAX(attempts - 1, 0), "
            "first_claimed_at = CASE WHEN attempts <= 1 THEN NULL ELSE first_claimed_at END, last_status = ? "
            "WHERE event_id = ? AND state = 'claimed' AND lease_token = ?",
            (until, reason, claim.event_id, claim.lease_token),
        )
        return cur.rowcount == 1


def record_attempt(conn: sqlite3.Connection, claim: Claim, *, status_code: int, now: float) -> bool:
    """Settle a delivery attempt. Only the holder of the event's CURRENT lease can: a
    superseded, late or repeated settlement changes nothing and returns False.
    2xx means ChatGPT RECEIVED the event, nothing more."""
    with _write_txn(conn):
        row = conn.execute(
            "SELECT attempts FROM mcp_event_outbox "
            "WHERE event_id = ? AND state = 'claimed' AND lease_token = ?",
            (claim.event_id, claim.lease_token),
        ).fetchone()
        if row is None:
            return False
        attempts = row["attempts"]
        if 200 <= status_code < 300:
            conn.execute(
                "UPDATE mcp_event_outbox SET state = 'delivered', lease_token = NULL, "
                "delivered_at = ?, last_status = ? WHERE event_id = ?",
                (now, str(status_code), claim.event_id),
            )
            conn.execute(
                "UPDATE mcp_event_subscriptions SET last_delivered_at = ? WHERE id = ?",
                (now, claim.subscription_id),
            )
        elif status_code == 410:
            conn.execute(
                "UPDATE mcp_event_outbox SET state = 'dead', lease_token = NULL, last_status = '410' "
                "WHERE event_id = ?",
                (claim.event_id,),
            )
            _end_subscription(conn, claim.subscription_id, status="expired", reason="callback_gone", now=now)
        elif status_code == 413 or attempts >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE mcp_event_outbox SET state = 'dead', lease_token = NULL, last_status = ? "
                "WHERE event_id = ?",
                (str(status_code), claim.event_id),
            )
        else:
            conn.execute(
                "UPDATE mcp_event_outbox SET state = 'retry', lease_token = NULL, claimed_until = NULL, "
                "next_attempt_at = ?, last_status = ? WHERE event_id = ?",
                (now + _backoff(attempts), str(status_code), claim.event_id),
            )
        return True
