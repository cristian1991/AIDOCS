"""Offline write queue for the server-authoritative backlog (P1).

Operator ruling 2026-07-21: offline writes QUEUE rather than being refused.
This does NOT resurrect the peer-merge machinery being retired — with a single
writer of record the queue is a list of INTENTS the server adjudicates, not a
second source of truth. No HLC, no LWW, no CRDT.

INTENTS, NOT ROWS
─────────────────
An entry records the operation and the fields it touched, plus the
``base_updated_at`` it was composed against — never a whole replacement row. Two
offline edits to DIFFERENT fields of the same item therefore both apply. Only a
genuine same-field collision becomes a conflict.

CONFLICTS ARE SURFACED, NEVER RESOLVED HERE
───────────────────────────────────────────
The server adjudicates; a rejected intent is retained with its reason so an
operator can act on it. Nothing is silently dropped and nothing is silently
merged — the failure mode of the old model was SILENT loss.

UNBOUND PROJECTS NEVER USE THIS. Local-only projects keep writing straight to
their local store, offline, exactly as today.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# #755/#756: the ONE canonical connect. Every site below was
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none of them
# set a single pragma, so this store ran with foreign_keys OFF (its FKs
# inert), no busy_timeout, and the default synchronous=FULL fsync tax.
# DURABILITY: AUDIT, i.e. the FULL this file already had. A queued intent
# is NOT re-derivable -- the server has never seen it and nothing else
# records it -- so a commit lost to a power cut is an operator's offline
# edit vanishing without a trace, which is the one failure this module
# was written to rule out ("nothing is silently dropped"). The same holds
# for a folded verdict: losing the DELETE re-submits an intent the server
# already applied. Not on any hot path -- it writes only when offline.
from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

_PENDING = "pending"
_CONFLICT = "conflict"

#: A conflict that nobody has ruled on yet. Deliberately a NAMED state and not
#: NULL/"" at the surface: the cutover gate asks "is every conflict classified?"
#: and an empty value is the shape a careless caller reads as "nothing here".
UNCLASSIFIED = "unclassified"

#: WHAT AN OPERATOR CAN DECIDE ABOUT A REFUSED WRITE. Closed set on purpose —
#: an unknown classification is REFUSED, never coerced or stored, because a
#: typo'd verdict that persists is worse than no verdict: it satisfies the
#: cutover gate ("classified!") while meaning nothing.
#:
#:   discard  the local intent is abandoned; the server's state stands.
#:   requeue  the intent is worth retrying (e.g. it lost a race and the base
#:            has since moved); it goes back to pending.
#:   keep     the operator has recorded the divergence deliberately and accepts
#:            it — the row stays a conflict but stops blocking a cutover.
#:
#: NONE OF THEM DELETE THE ROW. "Rejected/conflicting local write never
#: vanishes" (operator ruling 2026-08-30) applies to every branch, including
#: `discard` — discarding the INTENT is a decision, and the record of that
#: decision is the point.
CLASSIFICATIONS: frozenset[str] = frozenset({"discard", "requeue", "keep"})


def _db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS backlog_write_queue ("
        " queue_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " global_id TEXT NOT NULL,"
        " project_id TEXT NOT NULL,"
        " op TEXT NOT NULL,"
        " fields TEXT NOT NULL DEFAULT '{}',"
        " base_updated_at TEXT,"
        " state TEXT NOT NULL DEFAULT 'pending',"
        " reason TEXT,"
        " queued_at TEXT NOT NULL,"
        " classification TEXT,"
        " classified_by TEXT,"
        " classified_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backlog_queue_state "
        "ON backlog_write_queue(project_id, state, queue_id)"
    )
    # ADDITIVE MIGRATION for stores that predate classification (2026-08-30).
    # `CREATE TABLE IF NOT EXISTS` is a NO-OP on an existing table, so the three
    # columns above would never appear on any live box — and every read of them
    # would fail "no such column" on exactly the rows this feature exists for.
    #
    # ALTER-and-ignore-duplicate is the entire migration: three NULLABLE columns,
    # no backfill, no rewrite, no version stamp to keep in sync. An existing
    # conflict row reads classification=NULL, which is precisely what
    # UNCLASSIFIED means — so old rows arrive in the right state by default
    # rather than needing to be moved into it.
    for column in ("classification TEXT", "classified_by TEXT", "classified_at TEXT"):
        try:
            conn.execute(f"ALTER TABLE backlog_write_queue ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass  # already present — the only expected failure on this path


def enqueue(
    project_root: Path,
    *,
    global_id: str,
    project_id: str,
    op: str,
    fields: dict | None = None,
    base_updated_at: str = "",
) -> int:
    """Record ONE intent. Returns the queue id (0 on any failure).

    Best-effort by contract: queueing must never fail an already-committed local
    write — the drain catches up next cycle.
    """
    if not global_id or not op or not project_id:
        return 0
    try:
        db = _db_path(Path(project_root))
        db.parent.mkdir(parents=True, exist_ok=True)
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _ensure_table(conn)
            cur = conn.execute(
                "INSERT INTO backlog_write_queue "
                "(global_id, project_id, op, fields, base_updated_at, state, queued_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    str(global_id),
                    str(project_id),
                    str(op),
                    json.dumps(fields or {}, sort_keys=True, ensure_ascii=False),
                    str(base_updated_at or ""),
                    _PENDING,
                    _iso_now(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
    except Exception:  # noqa: BLE001 — never break the local write
        return 0


def pending(project_root: Path, project_id: str) -> list[dict]:
    """Queued intents awaiting the server, oldest first (submission order)."""
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            return []
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _ensure_table(conn)
            rows = conn.execute(
                "SELECT queue_id, global_id, op, fields, base_updated_at "
                "FROM backlog_write_queue WHERE project_id = ? AND state = ? "
                "ORDER BY queue_id ASC",
                (str(project_id), _PENDING),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for qid, gid, op, fields, base in rows:
        try:
            parsed = json.loads(fields) if fields else {}
        except (ValueError, TypeError):
            parsed = {}
        out.append(
            {
                "queue_id": int(qid),
                "globalId": gid,
                "op": op,
                "fields": parsed,
                "baseUpdatedAt": base or None,
            }
        )
    return out


def conflicts(project_root: Path, project_id: str) -> list[dict]:
    """Intents the SERVER refused. Retained, never dropped, so the operator can
    see exactly what did not land and why.

    `fields` IS THE INTENT AND IT WAS MISSING (2026-08-30). This projected
    queue_id/global_id/op/reason only, while the docstring above claimed the
    operator could see "exactly what did not land". They could see WHICH item and
    WHY — never WHAT was attempted, even though `fields` has been stored on the
    row since the table was created. The operator ruling is explicit: "preserve
    intent + reason as durable conflict, surface to operator." Storage kept both;
    the reader returned one.
    """
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            return []
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _ensure_table(conn)
            rows = conn.execute(
                "SELECT queue_id, global_id, op, reason, fields, "
                "classification, classified_by, classified_at "
                "FROM backlog_write_queue "
                "WHERE project_id = ? AND state = ? ORDER BY queue_id ASC",
                (str(project_id), _CONFLICT),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for q, g, o, r, f, cls, cby, cat in rows:
        try:
            parsed = json.loads(f) if f else {}
        except (ValueError, TypeError):
            # A row whose intent will not parse is still a real refusal. Report
            # it with an empty intent rather than dropping the whole conflict —
            # losing the record to protect the field would invert the priority.
            parsed = {}
        out.append(
            {
                "queue_id": int(q),
                "globalId": g,
                "op": o,
                "reason": r or "conflict",
                "fields": parsed,
                # NULL is reported as the literal UNCLASSIFIED rather than as
                # None or "": the cutover gate asks "is every conflict
                # classified?", and an empty string is the shape that reads as
                # "nothing to see" to a careless caller. A named state cannot.
                "classification": cls or UNCLASSIFIED,
                "classified_by": cby or "",
                "classified_at": cat or "",
            }
        )
    return out


def classify(
    project_root: Path,
    *,
    queue_id: int,
    classification: str,
    by: str = "",
) -> dict:
    """Record an operator's ruling on one refused write.

    THE CUTOVER GATE READS THIS (operator ruling 2026-08-30: "Existing
    unresolved conflicts may block flip until classified"). Classification is
    therefore a DECISION RECORD, not a cleanup: it says a human looked at this
    refusal and chose, and it carries who and when so the choice is auditable
    rather than merely present.

    UNKNOWN CLASSIFICATIONS ARE REFUSED, never coerced. A typo'd verdict that
    persisted would satisfy the gate while meaning nothing — the exact shape of
    a check that passes without examining anything.

    `requeue` returns the row to `pending` so the next drain retries it; the
    other verdicts leave it a conflict. NOTHING HERE DELETES A ROW: the ruling
    says a rejected write never vanishes, and that holds for `discard` too —
    discarding the INTENT is a decision, and the record of the decision is the
    point.
    """
    cls = str(classification or "").strip().lower()
    if cls not in CLASSIFICATIONS:
        return {
            "ok": False,
            "error": (
                f"unknown classification {classification!r}; one of "
                f"{sorted(CLASSIFICATIONS)}"
            ),
        }
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            return {"ok": False, "error": "no write queue on this project"}
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _ensure_table(conn)
            # Only a CONFLICT row can be classified: classifying a pending write
            # would rule on something the server has not answered yet.
            new_state = _PENDING if cls == "requeue" else _CONFLICT
            cur = conn.execute(
                "UPDATE backlog_write_queue SET classification = ?, "
                "classified_by = ?, classified_at = ?, state = ? "
                "WHERE queue_id = ? AND state = ?",
                (cls, str(by or ""), _iso_now(), new_state, int(queue_id), _CONFLICT),
            )
            conn.commit()
            if not cur.rowcount:
                return {
                    "ok": False,
                    "error": (
                        f"no conflict row with queue_id={queue_id} — it may have "
                        "been classified already, or it is still pending a server "
                        "verdict"
                    ),
                }
        return {"ok": True, "queue_id": int(queue_id), "classification": cls}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def unclassified_conflicts(project_root: Path, project_id: str) -> list[dict]:
    """Conflicts nobody has ruled on — what would BLOCK an authority flip.

    Separate from `conflicts()` because the cutover asks a narrower question
    than the read surface does: not "what is in conflict" but "what is still
    undecided". A classified conflict is a resolved piece of history; an
    unclassified one is an open decision.
    """
    return [
        row
        for row in conflicts(Path(project_root), str(project_id))
        if str(row.get("classification") or UNCLASSIFIED) == UNCLASSIFIED
    ]


def apply_verdicts(project_root: Path, verdicts: dict) -> dict:
    """Fold the server's {applied, conflicts, rejected} back into the queue.

    applied   → the intent is done; the row leaves the queue.
    conflicts → RETAINED as ``conflict`` with the server's reason (surfaced, not
                discarded — the operator decides).
    rejected  → retained as ``conflict`` too: a rejected intent that vanished
                silently would be exactly the data loss this design exists to
                prevent.
    """
    summary = {"applied": 0, "conflicts": 0, "rejected": 0}
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            return summary
        applied = {str(a.get("globalId")) for a in (verdicts.get("applied") or [])}
        conf = {
            str(c.get("globalId")): str(c.get("reason") or "conflict")
            for c in (verdicts.get("conflicts") or [])
        }
        rej = {
            str(r.get("globalId")): str(r.get("reason") or "rejected")
            for r in (verdicts.get("rejected") or [])
        }
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _ensure_table(conn)
            for gid in applied:
                cur = conn.execute(
                    "DELETE FROM backlog_write_queue WHERE global_id = ? AND state = ?",
                    (gid, _PENDING),
                )
                summary["applied"] += cur.rowcount or 0
            for gid, reason in {**rej, **conf}.items():
                cur = conn.execute(
                    "UPDATE backlog_write_queue SET state = ?, reason = ? "
                    "WHERE global_id = ? AND state = ?",
                    (_CONFLICT, reason, gid, _PENDING),
                )
                if gid in conf:
                    summary["conflicts"] += cur.rowcount or 0
                else:
                    summary["rejected"] += cur.rowcount or 0
            conn.commit()
    except Exception:  # noqa: BLE001 — folding verdicts never raises
        return summary
    return summary


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
