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

#: #972 wire mapper states. Neither is ``pending``, and that is the point:
#: ``apply_verdicts`` consumes EVERY pending row of an applied globalId, so an
#: intent parked in ``pending`` would vanish the moment a sibling write for the
#: same item landed.
#:
#:   blocked     the intent cannot be expressed on the authority's wire yet (an
#:               unclassified field, a create the route does not support, a
#:               merge whose target identity was never captured) — or it is
#:               held behind such an intent for the same item, because
#:               submitting later writes first would reorder them. NOT SENT.
#:               Re-evaluated on every drain, so it releases on its own once the
#:               mapper (or the route) can carry it.
#:   local_only  fully classified, but nothing in it is server item state (a
#:               reason-only annotation): retired locally WITHOUT a network
#:               call, the row and its reason kept as the receipt.
STATE_BLOCKED = "blocked"
STATE_LOCAL_ONLY = "local_only"

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


def candidates(project_root: Path, project_id: str) -> list[dict]:
    """Intents the drain must (re)consider: ``pending`` AND ``blocked``, oldest
    first. Same shape as ``pending()`` plus ``state``/``reason``.

    Blocked rows are included on purpose: a block is re-derived on every drain,
    never a permanent verdict, so an intent released by a mapper or route change
    goes out without anyone having to remember it."""
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            return []
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _ensure_table(conn)
            rows = conn.execute(
                "SELECT queue_id, global_id, op, fields, base_updated_at, state, reason "
                "FROM backlog_write_queue WHERE project_id = ? AND state IN (?, ?) "
                "ORDER BY queue_id ASC",
                (str(project_id), _PENDING, STATE_BLOCKED),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for qid, gid, op, fields, base, state, reason in rows:
        # A malformed intent is FLAGGED, never coerced to {}: the drain blocks
        # it (keeping its bytes) instead of retiring it as "nothing to send".
        parsed, malformed = _parse_fields(fields)
        out.append(
            {
                "queue_id": int(qid),
                "globalId": gid,
                "op": op,
                "fields": parsed,
                "fields_malformed": malformed,
                "baseUpdatedAt": base or None,
                "state": state,
                "reason": reason or "",
            }
        )
    return out


def blocked(project_root: Path, project_id: str) -> list[dict]:
    """Intents held off the wire (#972): never sent, so never drained. A reader
    of its own because ``pending()`` must not count them — and a readiness
    check that asked only ``pending()`` would read them as an empty outbox.

    STRICT, UNLIKE THE BEST-EFFORT READERS: a read failure RAISES. The cutover
    asks "is anything stuck?", and the best-effort readers answer a failed read
    with ``[]`` — the shape of "nothing" (r0b 0c42d3b3; the known-vs-empty
    defect #985 killed for migration debt)."""
    out: list[dict] = []
    for qid, gid, op, reason, fields in _strict_rows(
        project_root,
        "SELECT queue_id, global_id, op, reason, fields FROM backlog_write_queue "
        "WHERE project_id = ? AND state = ? ORDER BY queue_id ASC",
        (str(project_id), STATE_BLOCKED),
    ):
        parsed, malformed = _parse_fields(fields)
        out.append(
            {
                "queue_id": int(qid),
                "globalId": gid,
                "op": op,
                "reason": reason or "",
                "fields": parsed,
                "fields_malformed": malformed,
            }
        )
    return out


# ── STRICT readers for the cutover boundary (#972, r0b 9dbd2629) ─────────────
#
# Readiness asks three questions — pending depth, unclassified conflicts,
# blocked writes — and each is answered by a DIFFERENT query, so each can fail
# while the others answer (a column only one of them selects, say). The
# best-effort readers below them (`pending`, `conflicts`, ...) answer any
# failure with `[]`, which a gate reads as "none". These raise instead, so the
# boundary can say UNKNOWN. They never run `_ensure_table`: repairing the
# schema as a side effect of asking would also hide the very fault that made
# the question unanswerable. The one honest empty is a queue never created.


def _strict_rows(project_root: Path, sql: str, params: tuple) -> list[tuple]:
    db = _db_path(Path(project_root))
    if not db.is_file():
        return []
    with _canonical_connect(db, durability=_Durability.AUDIT, row_factory=False) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'backlog_write_queue'"
        ).fetchone():
            return []
        return conn.execute(sql, params).fetchall()


def _parse_fields(raw: str | None) -> tuple[dict, bool]:
    """(parsed, malformed). A stored intent that no longer parses is reported as
    MALFORMED — never silently turned into {}, which the wire mapper would read
    as "nothing to send" and retire (r0b 9dbd2629 (2))."""
    if not raw:
        return {}, False
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}, True
    return (parsed, False) if isinstance(parsed, dict) else ({}, True)


def pending_strict(project_root: Path, project_id: str) -> list[dict]:
    """``pending()`` for the cutover boundary: raises instead of answering []."""
    return [
        {"queue_id": int(q), "globalId": g, "op": o, "baseUpdatedAt": b or None}
        for q, g, o, _f, b in _strict_rows(
            project_root,
            "SELECT queue_id, global_id, op, fields, base_updated_at "
            "FROM backlog_write_queue WHERE project_id = ? AND state = ? "
            "ORDER BY queue_id ASC",
            (str(project_id), _PENDING),
        )
    ]


def unclassified_conflicts_strict(project_root: Path, project_id: str) -> list[dict]:
    """Conflicts nobody has ruled on — what would BLOCK an authority flip.

    Separate from `conflicts()` because the cutover asks a narrower question
    than the read surface does: not "what is in conflict" but "what is still
    undecided". A classified conflict is a resolved piece of history; an
    unclassified one is an open decision.

    STRICT, for the cutover boundary: raises instead of answering [], so a
    failed read is "unknown", never "none". Same row shape as ``conflicts()``.
    This is the only reader: the non-strict twin had no caller left after
    #972 (8dff199a9) and was deleted (vulture, deploy dev-200)."""
    out: list[dict] = []
    for q, g, o, r, f, cls, cby, cat in _strict_rows(
        project_root,
        "SELECT queue_id, global_id, op, reason, fields, "
        "classification, classified_by, classified_at FROM backlog_write_queue "
        "WHERE project_id = ? AND state = ? ORDER BY queue_id ASC",
        (str(project_id), _CONFLICT),
    ):
        if (cls or UNCLASSIFIED) != UNCLASSIFIED:
            continue
        parsed, _malformed = _parse_fields(f)
        out.append(
            {
                "queue_id": int(q),
                "globalId": g,
                "op": o,
                "reason": r or "conflict",
                "fields": parsed,
                "classification": UNCLASSIFIED,
                "classified_by": cby or "",
                "classified_at": cat or "",
            }
        )
    return out


def set_state(project_root: Path, queue_id: int, state: str, reason: str = "") -> bool:
    """Move ONE undecided row between pending / blocked / local_only.

    Only a row the server has not answered (pending or blocked) may move: a
    conflict is an operator's decision (``classify``), never the mapper's. The
    ``fields`` column is never touched — the queue keeps the store's canonical
    bytes, whatever the wire shape of the day is."""
    if state not in (_PENDING, STATE_BLOCKED, STATE_LOCAL_ONLY):
        return False
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            return False
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _ensure_table(conn)
            cur = conn.execute(
                "UPDATE backlog_write_queue SET state = ?, reason = ? "
                "WHERE queue_id = ? AND state IN (?, ?)",
                (state, str(reason or "") or None, int(queue_id), _PENDING, STATE_BLOCKED),
            )
            conn.commit()
            return bool(cur.rowcount)
    except Exception:  # noqa: BLE001
        return False


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


def _is_row_id(value: object) -> bool:
    """A real queue row id: a positive int. bool is an int subclass and is
    excluded — True must never read as row 1."""
    return type(value) is int and value > 0


def _is_identity(value: object) -> bool:
    """A real globalId: a non-empty str. Nothing is coerced to one."""
    return isinstance(value, str) and value != ""


def apply_verdicts(project_root: Path, verdicts: dict, *, submitted: list[dict]) -> dict:
    """Fold the server's {applied, conflicts, rejected} back into the queue —
    onto EXACTLY the rows that were submitted, by queue_id.

    applied   → the intent is done; that row leaves the queue.
    conflicts → RETAINED as ``conflict`` with the server's reason (surfaced, not
                discarded — the operator decides).
    rejected  → retained as ``conflict`` too: a rejected intent that vanished
                silently would be exactly the data loss this design exists to
                prevent.

    #972 CORRELATION. This used to fold by globalId alone: one `applied` DELETED
    every pending row of the item — writes the server never saw, dropped as done
    — and one refusal buried every one of them as a conflict. The route answers
    each intent exactly once, keyed by globalId, and the drain now sends at most
    ONE intent per item; so `submitted` (the batch actually sent, REQUIRED —
    there is no item-wide fallback) maps each answered globalId to one queue_id,
    and only that row is touched. Every submitted row is accounted for exactly
    once:

      applied / conflicts / rejected   settled, as above
      unanswered                       no verdict came back → stays pending,
                                       retried next cycle (queue_ids)
      ambiguous_verdicts               more than one verdict for one submitted
                                       item, or one item submitted twice — a
                                       contract violation; guessing which applies
                                       would be the old defect, so NOTHING of
                                       that item is settled (globalIds)
      unmatched_verdicts               a verdict for an item that was not in
                                       the batch → touches nothing (globalIds)
      state_mismatch                   the submitted queue_id does not (or no
                                       longer) name a PENDING row of that same
                                       globalId — gone, blocked, or the pair
                                       itself is wrong → touches nothing
                                       (queue_ids). The mutation binds BOTH ids,
                                       so a malformed pair can never settle
                                       another item's row (r0b 2b6b43a4).
      invalid_submitted                a submitted entry that does not parse
                                       (no/non-integer queue_id, no globalId) →
                                       settles nothing (the entries themselves)
      invalid_verdicts                 a verdict entry that is not an object
                                       with a globalId → ignored, named
    """
    summary: dict = {
        "applied": 0,
        "conflicts": 0,
        "rejected": 0,
        "unanswered": [],
        "ambiguous_verdicts": [],
        "unmatched_verdicts": [],
        "state_mismatch": [],
        "invalid_submitted": [],
        "invalid_verdicts": [],
    }
    # Parsed ENTRY BY ENTRY, and every malformed one NAMED rather than raised:
    # this function's contract is "never raises", and an unparsable entry must
    # neither escape as an exception nor settle anything (r0b e5138586).
    #
    # IDENTITY IS CHECKED BY TYPE, NEVER BY COERCIBILITY (r0b 03eda539):
    # str(None) is "None", int(True) is 1, int(1.7) is 1 — each would turn a
    # malformed entry into a valid-looking one that could match a real row. A
    # queue_id is a positive int (bool excluded); a globalId is a non-empty str,
    # used exactly as given.
    by_gid: dict[str, list[int]] = {}
    for s in submitted or []:
        qid = s.get("queue_id") if isinstance(s, dict) else None
        gid = s.get("globalId") if isinstance(s, dict) else None
        if not (_is_row_id(qid) and _is_identity(gid)):
            summary["invalid_submitted"].append(s)
            continue
        by_gid.setdefault(gid, []).append(qid)
    answers: dict[str, list[tuple[str, str]]] = {}
    body = verdicts if isinstance(verdicts, dict) else {}
    for bucket, default in (("applied", ""), ("conflicts", "conflict"), ("rejected", "rejected")):
        entries = body.get(bucket) or []
        for v in entries if isinstance(entries, list) else [entries]:
            if not (isinstance(v, dict) and _is_identity(v.get("globalId"))):
                summary["invalid_verdicts"].append(v)
                continue
            answers.setdefault(v["globalId"], []).append(
                (bucket, str(v.get("reason") or default))
            )
    summary["unmatched_verdicts"] = sorted(g for g in answers if g not in by_gid)
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            summary["unanswered"] = [q for qids in by_gid.values() for q in qids]
            return summary
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _ensure_table(conn)
            for gid, qids in by_gid.items():
                got = answers.get(gid, [])
                if not got:
                    summary["unanswered"].extend(qids)
                    continue
                if len(got) > 1 or len(qids) > 1:
                    summary["ambiguous_verdicts"].append(gid)
                    continue
                (bucket, reason), qid = got[0], qids[0]
                if bucket == "applied":
                    cur = conn.execute(
                        "DELETE FROM backlog_write_queue "
                        "WHERE queue_id = ? AND global_id = ? AND state = ?",
                        (qid, gid, _PENDING),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE backlog_write_queue SET state = ?, reason = ? "
                        "WHERE queue_id = ? AND global_id = ? AND state = ?",
                        (_CONFLICT, reason, qid, gid, _PENDING),
                    )
                if cur.rowcount:
                    summary[bucket] += cur.rowcount
                else:
                    summary["state_mismatch"].append(qid)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — folding verdicts never raises
        summary["error"] = type(exc).__name__
        return summary
    summary["ambiguous_verdicts"].sort()
    return summary


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
