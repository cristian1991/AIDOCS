"""Lane 1 MCP Events: one delivery-worker tick across project stores (#1226).

The gate's loop calls tick() on a timer with the roots it serves. Each project's
wakes live in its own conductor_comms store, so a tick opens every EXISTING store
(it never creates one), sends up to `per_project` due wakes there through
mcp_events_delivery.deliver_due, and moves on. One broken store is logged and
counted, never allowed to stop the others: a wake is a hint, and a failed tick
just leaves the wakes queued for the next one.

The authority recheck is per project and injected (`authorized_for(root)` returns
the predicate deliver_due applies to each subscription). actor_still_attached is
its store-local half: a chat whose XAACP actor is no longer attached is not woken
(deliver_due then revokes the subscription). The principal's project ACL is the
gate's half and is supplied by the gate.
"""

from __future__ import annotations

import logging
import socket
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

from . import conductor_comms as cc
from .mcp_events_delivery import Delivery, deliver_due, https_post_pinned
from .mcp_events_secret import Keyring

logger = logging.getLogger("aidocs.mcp_events_worker")

PER_PROJECT_PER_TICK = 20

_COUNTERS = {
    "sent": "sent", "revoked": "revoked", "secret_unavailable": "parked",
    "authority_unknown": "parked", "callback_refused": "refused",
    # #1226: NEVER sent, so never counted as sent. The pause fence holds the wake
    # (r0b2 B6); a client-less binding issues no ticket (r0b2 B5).
    "held_paused": "parked", "binding_inactive": "parked",
    "binding_without_client": "refused",
    # r0b2 B7: the lease was lost before any POST (another worker holds it, or a
    # deactivate killed the claimed event). Nothing was sent here.
    "lease_lost": "lost",
    # r0b2 B8: over MAX_BODY_BYTES, refused locally before post().
    "body_too_large": "refused",
}
#: FAIL-CLOSED (r0b2 B7): an outcome missing from _COUNTERS is never counted as
#: sent. It lands here and is logged as an error, so a new outcome is noticed.
_UNKNOWN_COUNTER = "unknown"


def deliver_pending(
    conn: sqlite3.Connection,
    *,
    keyring: Keyring,
    now: float,
    authorized: Callable[[sqlite3.Row], bool],
    limit: int,
    post=https_post_pinned,
    resolve=socket.getaddrinfo,
    budget=None,
) -> list[Delivery]:
    """Up to `limit` delivery attempts in one store, stopping when nothing is due."""
    out: list[Delivery] = []
    while len(out) < limit:
        done = deliver_due(
            conn, keyring=keyring, now=now, authorized=authorized, post=post, resolve=resolve, budget=budget
        )
        if done is None:
            break
        out.append(done)
    return out


def actor_still_attached(project_root: Path) -> Callable[[sqlite3.Row], bool]:
    """The store-local authority half: is the subscription's XAACP actor still
    attached in its session?"""

    def check(sub) -> bool:
        return bool(
            cc._xaacp_actor_attached(project_root, str(sub["recipient_actor_id"]), str(sub["session_id"]))
        )

    return check


def tick(
    project_roots: Iterable[Path],
    *,
    keyring: Keyring,
    now: float,
    authorized_for: Callable[[Path], Callable[[sqlite3.Row], bool]],
    post=https_post_pinned,
    resolve=socket.getaddrinfo,
    per_project: int = PER_PROJECT_PER_TICK,
    budget=None,
) -> dict:
    """One pass over `project_roots`; returns counts (sent/revoked/parked/refused/
    lost/unknown, projects visited, failed_projects). Only a real POST counts as
    sent; an unmapped outcome counts as unknown and is logged as an error. `budget` is the gate's global ledger, shared
    by every project of the pass (and by concurrent passes)."""
    report = {
        "projects": 0, "sent": 0, "revoked": 0, "parked": 0, "refused": 0, "lost": 0,
        _UNKNOWN_COUNTER: 0, "failed_projects": 0,
    }
    for root in project_roots:
        root = Path(root)
        if not cc._db_path(root).exists():
            continue
        report["projects"] += 1
        try:
            conn = cc._connect(root)
        except Exception:  # noqa: BLE001 - one project never stops the tick
            logger.warning("MCP Events tick: cannot open the store of %s", root, exc_info=True)
            report["failed_projects"] += 1
            continue
        try:
            for done in deliver_pending(
                conn, keyring=keyring, now=now, authorized=authorized_for(root),
                limit=per_project, post=post, resolve=resolve, budget=budget,
            ):
                counter = _COUNTERS.get(done.outcome)
                if counter is None:
                    logger.error(
                        "MCP Events tick: unmapped delivery outcome %r for %s (counted as unknown, never sent)",
                        done.outcome, done.event_id,
                    )
                    counter = _UNKNOWN_COUNTER
                report[counter] += 1
        except Exception:  # noqa: BLE001 - one project never stops the tick
            logger.warning("MCP Events tick failed for %s", root, exc_info=True)
            report["failed_projects"] += 1
        finally:
            conn.close()
    return report
