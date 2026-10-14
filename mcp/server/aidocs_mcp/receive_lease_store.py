"""DRT-11 -- receive-lifecycle lease, keyed by canonical actor route.

Why this exists
---------------
XAACP can ADDRESS an actor. It cannot make one RECEPTIVE. A message
lands in the canonical ``messages`` store whether or not anybody is
parked on ``xaacp_wait_next``; the park is what turns delivery into
receipt. So the fact that actually matters operationally is not "was it
sent" but "is this actor still carrying a receive obligation, and can we
still TELL?" -- and nothing in AIDOCS recorded that. This store does.

THREE STATES, NEVER TWO
-----------------------
The host's own ``SendMessage(notify_when_idle=True)`` got one detail
right that the plan text misses: when the signal never arrives it says
*the subscription expired* rather than going silent. That is the
unknown-is-never-laundered rule applied to a lease. So:

* ``waiting``        -- armed, parked, lease still in force.
* ``working``        -- returned from a park; OWES another arm.
* ``rearm_overdue``  -- we stopped being able to tell. Either the park's
  lease outlived its deadline (the waiter died mid-park, or the process
  went away) or the actor returned to ``working`` and never re-armed.
* ``released``       -- the obligation legitimately ended.
* ``unknown``        -- no lease row on this route. ``owes_arm`` is
  ``None``: NOT ``False`` (which would hide a dropped actor) and NOT
  ``True`` (which would cry wolf about an actor that was never in the
  scheme). Unknown fails closed by staying unknown.

WHAT THIS STORE IS NOT
----------------------
It has no authority. There is deliberately no ``kill``, ``evict``,
``transfer``, ``terminate`` or ``reassign`` verb, and a test pins their
absence. Detecting a lost lease produces an ALARM -- one per transition,
never per tick -- and nothing else. Labels may specialise by surface
(e.g. "CHATGPT CO-CONDUCTOR NEEDS RE-ARMING") because an operator
reading a dashboard needs to know which surface to go poke.

ONE ACTOR KIND
--------------
From AIDOCS's view a lane and a subagent are the same thing: a spawned
actor on a canonical route. ``actor_kind`` here is descriptive (it feeds
the alarm label and the dashboard grouping) and is NEVER consulted for a
gate decision. How the actor was spawned -- ``claude -p -r``,
``opencode -q -s``, a host-internal spawn -- does not appear in this
module at all. Seats and operator surfaces get lease rows on exactly the
same terms as spawned agents.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

# THE RETURN-KIND TUPLE WAS REMOVED, AND THE RULE IT NAMED WAS NOT.
# `RETURN_KINDS = ("message", "timeout", "error", "ups_interrupt")` sat here
# with no reader: nothing validated against it, so it documented a rule it did
# not enforce, which is the weakest possible form of that rule. The rule itself
# -- none of those returns ends the receive obligation while the actor remains
# active (constraint 4); only `release()` settles -- is stated where it is
# actually implemented, in `settle`'s docstring.
#
# WHAT IS STILL OPEN, recorded rather than quietly dropped: `settle` accepts ANY
# string as `return_kind`, terminal-looking values included, and stores it
# verbatim. Adding that validation changes the acceptance behaviour of a live
# delivery rail and was NOT taken mid-freeze. Tracked in
# `.MEMORY/sessions/ubermega/plans/drt-closure.md`.

STATE_WAITING = "waiting"
STATE_WORKING = "working"
STATE_OVERDUE = "rearm_overdue"
STATE_RELEASED = "released"
STATE_UNKNOWN = "unknown"

# How long a `working` actor may go without re-arming before we admit we
# can no longer tell. Derived from the lease it last held, so a short
# park implies a short tolerance.
DEFAULT_LEASE_SECONDS = 300.0


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS actor_receive_lease (
            session_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            lane_id TEXT NOT NULL DEFAULT '',
            actor_kind TEXT NOT NULL DEFAULT '',
            host TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            cursor INTEGER NOT NULL DEFAULT 0,
            armed_at REAL,
            lease_seconds REAL NOT NULL DEFAULT 0,
            lease_deadline REAL,
            last_return_at REAL,
            last_return_kind TEXT NOT NULL DEFAULT '',
            alarm_latched_at REAL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (session_id, actor_id, lane_id)
        )
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_actor_receive_lease_session "
        "ON actor_receive_lease (session_id, state)",
    )
    # ADDITIVE MIGRATION, NOT A RECREATE. A live box already holds
    # actor_receive_lease rows written before the park REQUEST had an
    # announce latch, and dropping or rebuilding the table to add a column
    # would discard exactly the coverage evidence this store exists to
    # keep. `CREATE TABLE IF NOT EXISTS` above cannot add a column to a
    # table that already exists, so the column is added here and the
    # duplicate-column error (the steady state, every call after the
    # first) is the expected answer, not a fault.
    try:
        conn.execute("ALTER TABLE actor_receive_lease ADD COLUMN request_announced_at REAL")
    except Exception:
        pass
    conn.commit()


def _connect(project_root: Path):
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    return _canonical_connect(db, durability=_Durability.AUDIT, row_factory=True)


def _label(row: dict[str, Any]) -> str:
    """Operator-facing alarm text. Specialises by surface, never by the
    adapter that spawned the actor."""
    host = str(row.get("host") or "").strip().lower()
    role = str(row.get("role") or "").strip().lower()
    kind = str(row.get("actor_kind") or "actor").strip().lower()
    surface = {
        "web_chatgpt": "CHATGPT",
        "chatgpt": "CHATGPT",
        "claude_code": "CLAUDE CODE",
        "web_mcp": "WEB",
    }.get(host, host.upper() or "ACTOR")
    who = role.replace("_", "-").upper() or kind.upper()
    return f"{surface} {who} NEEDS RE-ARMING"


class ReceiveLeaseStore:
    """Receive-obligation lease per canonical actor route.

    The route ``(session_id, actor_id, lane_id)`` IS the key, so one
    actor addressable on two lanes legitimately carries two leases.
    """

    def arm(
        self,
        project_root: Path,
        *,
        session_id: str,
        actor_id: str,
        lane_id: str = "",
        actor_kind: str = "",
        host: str = "",
        role: str = "",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        cursor: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record that this actor is parked and expects a delivery.

        Called BEFORE the park blocks, so a waiter that dies inside the
        park still leaves a row whose deadline will expire -- which is
        exactly how "we stopped being able to tell" gets detected
        instead of being silently lost.

        Arming also clears the alarm latch: a later drop on the same
        route must be able to alarm again (constraint 6 is once per
        TRANSITION, not once ever).
        """
        ts = float(now if now is not None else time.time())
        lease = max(0.0, float(lease_seconds or 0.0))
        with _connect(project_root) as conn:
            _init(conn)
            prior = conn.execute(
                "SELECT cursor FROM actor_receive_lease "
                "WHERE session_id=? AND actor_id=? AND lane_id=?",
                (session_id, actor_id, lane_id),
            ).fetchone()
            keep_cursor = int(prior["cursor"]) if prior is not None else 0
            new_cursor = keep_cursor if cursor is None else int(cursor)
            conn.execute(
                "INSERT INTO actor_receive_lease "
                "(session_id, actor_id, lane_id, actor_kind, host, role, state, "
                "cursor, armed_at, lease_seconds, lease_deadline, alarm_latched_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(session_id, actor_id, lane_id) DO UPDATE SET "
                "actor_kind=excluded.actor_kind, host=excluded.host, "
                "role=excluded.role, state=excluded.state, cursor=excluded.cursor, "
                "armed_at=excluded.armed_at, lease_seconds=excluded.lease_seconds, "
                "lease_deadline=excluded.lease_deadline, alarm_latched_at=NULL, "
                # ONCE PER EVENT, NOT ONCE EVER. Each arm is a NEW receive
                # obligation, so the park request it carries is new news and
                # must be announceable again. Clearing the latch here is the
                # same rule the alarm latch follows two lines up: the latch
                # records that THIS transition was announced, never that the
                # route has been announced about at some point in its life.
                "request_announced_at=NULL, "
                "updated_at=excluded.updated_at",
                (
                    session_id,
                    actor_id,
                    lane_id,
                    actor_kind,
                    host,
                    role,
                    STATE_WAITING,
                    new_cursor,
                    ts,
                    lease,
                    ts + lease,
                    ts,
                ),
            )
            conn.commit()
        return self.get(
            project_root,
            session_id=session_id,
            actor_id=actor_id,
            lane_id=lane_id,
            now=ts,
        )

    def settle(
        self,
        project_root: Path,
        *,
        session_id: str,
        actor_id: str,
        lane_id: str = "",
        return_kind: str,
        cursor: int | None = None,
        now: float | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """A park RETURNED. The actor is now working and owes another arm.

        Every return kind lands here -- message, timeout, error and
        ups_interrupt alike. None of them ends the obligation, because
        an actor that is still active still needs to be reachable. The
        deadline rolls forward by the lease it last held, so a working
        actor that never re-arms becomes ``rearm_overdue`` rather than
        sitting in ``working`` forever looking healthy.
        """
        ts = float(now if now is not None else time.time())
        kind = str(return_kind or "").strip()
        with _connect(project_root) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT * FROM actor_receive_lease "
                "WHERE session_id=? AND actor_id=? AND lane_id=?",
                (session_id, actor_id, lane_id),
            ).fetchone()
            if row is None:
                # Settling a route we never armed is still INFORMATION --
                # the actor demonstrably received something -- but we must
                # not invent an armed_at we never observed.
                conn.execute(
                    "INSERT INTO actor_receive_lease "
                    "(session_id, actor_id, lane_id, state, cursor, lease_seconds, "
                    "lease_deadline, last_return_at, last_return_kind, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        actor_id,
                        lane_id,
                        STATE_WORKING,
                        int(cursor or 0),
                        DEFAULT_LEASE_SECONDS,
                        ts + DEFAULT_LEASE_SECONDS,
                        ts,
                        kind,
                        ts,
                    ),
                )
            else:
                lease = float(row["lease_seconds"] or DEFAULT_LEASE_SECONDS)
                keep = int(row["cursor"] or 0)
                conn.execute(
                    "UPDATE actor_receive_lease SET state=?, cursor=?, "
                    "lease_deadline=?, last_return_at=?, last_return_kind=?, "
                    "alarm_latched_at=NULL, updated_at=? "
                    "WHERE session_id=? AND actor_id=? AND lane_id=?",
                    (
                        STATE_WORKING,
                        keep if cursor is None else int(cursor),
                        ts + lease,
                        ts,
                        kind,
                        ts,
                        session_id,
                        actor_id,
                        lane_id,
                    ),
                )
            conn.commit()
        return self.get(
            project_root,
            session_id=session_id,
            actor_id=actor_id,
            lane_id=lane_id,
            now=ts,
        )

    def release(
        self,
        project_root: Path,
        *,
        session_id: str,
        actor_id: str,
        lane_id: str = "",
        reason: str = "",
        now: float | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """End the receive obligation -- and it MUST end it.

        Only complete / cancel / release / handoff reach here. This is
        the other half of constraint 4: a legitimately-terminal actor
        must never be reported as broken, so release is final and an
        released route never appears in ``overdue``.
        """
        ts = float(now if now is not None else time.time())
        with _connect(project_root) as conn:
            _init(conn)
            conn.execute(
                "INSERT INTO actor_receive_lease "
                "(session_id, actor_id, lane_id, state, last_return_at, "
                "last_return_kind, lease_deadline, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(session_id, actor_id, lane_id) DO UPDATE SET "
                "state=excluded.state, last_return_at=excluded.last_return_at, "
                "last_return_kind=excluded.last_return_kind, lease_deadline=NULL, "
                "alarm_latched_at=NULL, updated_at=excluded.updated_at",
                (
                    session_id,
                    actor_id,
                    lane_id,
                    STATE_RELEASED,
                    ts,
                    str(reason or "release"),
                    ts,
                ),
            )
            conn.commit()
        return self.get(
            project_root,
            session_id=session_id,
            actor_id=actor_id,
            lane_id=lane_id,
            now=ts,
        )

    def get(
        self,
        project_root: Path,
        *,
        session_id: str,
        actor_id: str,
        lane_id: str = "",
        now: float | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Resolve the lease on ONE route. Never broadens the route."""
        ts = float(now if now is not None else time.time())
        with _connect(project_root) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT * FROM actor_receive_lease "
                "WHERE session_id=? AND actor_id=? AND lane_id=?",
                (session_id, actor_id, lane_id),
            ).fetchone()
        if row is None:
            return {
                "session_id": session_id,
                "actor_id": actor_id,
                "lane_id": lane_id,
                "state": STATE_UNKNOWN,
                "owes_arm": None,
                "why": "no_lease_row",
                "cursor": 0,
                "armed_at": None,
                "lease_deadline": None,
                "last_return_at": None,
                "last_return_kind": "",
            }
        return _project(dict(row), ts)

    def list_actors(
        self,
        project_root: Path,
        *,
        session_id: str,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Every lease row for a session -- seats and spawned agents on
        exactly the same terms. This is the dashboard surface that makes
        dropped receive coverage visible in regime B (no hooks)."""
        ts = float(now if now is not None else time.time())
        with _connect(project_root) as conn:
            _init(conn)
            rows = conn.execute(
                "SELECT * FROM actor_receive_lease WHERE session_id=? "
                "ORDER BY updated_at DESC",
                (session_id,),
            ).fetchall()
        return [_project(dict(r), ts) for r in rows]

    def overdue(
        self,
        project_root: Path,
        *,
        session_id: str,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Routes where we STOPPED BEING ABLE TO TELL. Observability
        only -- the caller may alarm, and may do nothing else."""
        return [
            r
            for r in self.list_actors(project_root, session_id=session_id, now=now)
            if r["state"] == STATE_OVERDUE
        ]

    def claim_overdue_alarms(
        self,
        project_root: Path,
        *,
        session_id: str,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Claim every notification this session's overdue transitions are owed.

        ONE PASS, ONE CONNECTION (#489). The previous shape claimed each overdue
        route with its own ``get`` and its own conditional UPDATE -- measured at
        ~1,004 connections and 501 write locks per prompt on a session whose
        latches were ALL already set. Now: one read selects only overdue routes
        whose latch is unset; when there are none, no write transaction is
        opened at all. Otherwise ONE conditional UPDATE latches exactly the
        still-unlatched overdue routes and returns them.

        Per transition, not per tick (constraint 6): the latch stays conditional
        in SQL (``alarm_latched_at IS NULL``), so two concurrent passes that both
        read the same unlatched set cannot both announce a route. ``arm`` /
        ``settle`` / ``release`` clear the latch, so a later expiry alarms again.

        Observability only: this latches a notification column and touches
        nothing else -- no state, no deadline, no actor. Informational traffic,
        never an enforced-ack rail.
        """
        ts = float(now if now is not None else time.time())
        with _connect(project_root) as conn:
            _init(conn)
            if not _select_unlatched_overdue(conn, session_id, ts):
                return []
            rows = conn.execute(
                "UPDATE actor_receive_lease SET alarm_latched_at=? "
                "WHERE session_id=? AND alarm_latched_at IS NULL "
                "AND state IN (?, ?) "
                "AND lease_deadline IS NOT NULL AND lease_deadline < ? "
                "RETURNING *",
                (ts, session_id, STATE_WAITING, STATE_WORKING, ts),
            ).fetchall()
            conn.commit()
        alarms: list[dict[str, Any]] = []
        for raw in rows:
            row = _project(dict(raw), ts)
            if row["state"] != STATE_OVERDUE:
                continue
            alarms.append(
                {
                    "label": _label(row),
                    "session_id": row["session_id"],
                    "actor_id": row["actor_id"],
                    "lane_id": row["lane_id"],
                    "state": STATE_OVERDUE,
                    "why": row["why"],
                    "host": row.get("host", ""),
                    "role": row.get("role", ""),
                    "actor_kind": row.get("actor_kind", ""),
                    "claimed_at": ts,
                }
            )
        return alarms
    def claim_park_request(
        self,
        project_root: Path,
        *,
        session_id: str,
        actor_id: str,
        lane_id: str = "",
        now: float | None = None,
        **_ignored: Any,
    ) -> bool:
        """Claim the ONE park-request announcement this ARM is owed.

        True on the first call after an arm, False on every later call
        until the next arm clears the latch. The arm EVENT is the anchor,
        and the anchor is a durable column -- not a process-memory set,
        because the hook is a fresh subprocess per call and the broker
        respawns, so anything in memory announces on every single turn.

        This is INFORMATIONAL traffic: the request asks the actor to park
        and carries no ack, no enforcement and no workflow obligation. It
        must never be folded into an enforced-workflow rail, which is a
        separate channel by law precisely so an advisory cannot acquire
        teeth by adjacency.

        The latch write IS the claim and it is conditional in SQL, so two
        concurrent readers of the same route cannot both announce. A route
        with no lease row claims NOTHING: there is no obligation to
        announce, and inventing one would announce at an actor that was
        never in the scheme.
        """
        ts = float(now if now is not None else time.time())
        try:
            with _connect(project_root) as conn:
                _init(conn)
                cur = conn.execute(
                    "UPDATE actor_receive_lease SET request_announced_at=? "
                    "WHERE session_id=? AND actor_id=? AND lane_id=? "
                    "AND state=? AND request_announced_at IS NULL",
                    (ts, session_id, actor_id, lane_id, STATE_WAITING),
                )
                conn.commit()
                return bool(cur.rowcount)
        except Exception:
            # A ledger we cannot write is not a licence to announce
            # repeatedly. Fail toward SILENCE: the obligation stays in the
            # lease row and stays queryable, and a duplicated nag on every
            # turn would train the reader to ignore the rail.
            return False


def _select_unlatched_overdue(conn: sqlite3.Connection, session_id: str, now: float) -> list[Any]:
    """READ ONLY: overdue routes whose alarm latch is still unset.

    The same predicate ``_project`` applies at read time (a waiting or working
    route past its deadline), pushed into SQL so an already-latched session
    costs one indexed read and no write lock.
    """
    return conn.execute(
        "SELECT session_id, actor_id, lane_id FROM actor_receive_lease "
        "WHERE session_id=? AND alarm_latched_at IS NULL AND state IN (?, ?) "
        "AND lease_deadline IS NOT NULL AND lease_deadline < ?",
        (session_id, STATE_WAITING, STATE_WORKING, now),
    ).fetchall()


def _project(row: dict[str, Any], now: float) -> dict[str, Any]:
    """Derive the observable state from the stored row.

    The stored ``state`` is what we last WROTE; the returned state is
    what is TRUE now. Lateness is computed at read time so no ticker is
    required for an overdue route to be visible -- a box with no daemon
    running still reports the truth.
    """
    stored = str(row.get("state") or "")
    deadline = row.get("lease_deadline")
    state = stored
    why = ""
    owes: bool | None
    if stored == STATE_RELEASED:
        owes = False
    elif stored == STATE_WAITING:
        owes = True
        if deadline is not None and now > float(deadline):
            # The park should have returned by now and did not report
            # back. We are NOT still waiting -- we can no longer tell.
            state = STATE_OVERDUE
            why = "lease_expired_while_waiting"
    elif stored == STATE_WORKING:
        owes = True
        if deadline is not None and now > float(deadline):
            state = STATE_OVERDUE
            why = "returned_but_never_rearmed"
    else:
        owes = None
        state = STATE_UNKNOWN
        why = "unrecognised_stored_state"
    return {
        "session_id": row.get("session_id", ""),
        "actor_id": row.get("actor_id", ""),
        "lane_id": row.get("lane_id", ""),
        "actor_kind": row.get("actor_kind", ""),
        "host": row.get("host", ""),
        "role": row.get("role", ""),
        "state": state,
        "stored_state": stored,
        "owes_arm": owes,
        "why": why,
        "cursor": int(row.get("cursor") or 0),
        "armed_at": row.get("armed_at"),
        "lease_seconds": float(row.get("lease_seconds") or 0.0),
        "lease_deadline": deadline,
        "last_return_at": row.get("last_return_at"),
        "last_return_kind": str(row.get("last_return_kind") or ""),
        "alarm_latched_at": row.get("alarm_latched_at"),
    }
