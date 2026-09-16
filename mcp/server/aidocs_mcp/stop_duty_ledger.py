"""Stop is a SINGLE FAULT DOMAIN for nine unrelated duties. This records
which ones were actually PROVEN, so "the hook died" stops meaning "we know
nothing".

THE MEASURED DEFECT
-------------------
``ClaudeHookHandler._handle_stop`` dispatches nine independent duties and
has four early-return paths. The delegated hook process was measured dying
seven times in one day (``exit 1``, 0 bytes of stdout,
``cause=hook_crashed_before_verdict``), once on ``event=Stop``. Exactly ONE
of the nine duties has any recovery: the #444 ``recover_causal_turns``
watchdog sweep may later seal an orphaned turn. The other eight have none,
and -- this is the part this module exists for -- NOTHING RECORDED WHICH
DUTIES BECAME UNPROVEN. The honest post-mortem state was "Stop result
absent; eight duties have no recovery evidence", which is not a state any
supervisor can act on.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
It is an EXECUTION MARKER LEDGER. ``open_dispatch`` writes one row BEFORE
the first duty runs, naming the lifecycle event and the full planned duty
set. Each duty then marks itself ``proven`` or ``failed`` as it completes.
``close`` stamps the outcome and computes ``duties_unreached`` --
``planned - proven - failed`` -- which is the set that an early return
skipped by design or a crash destroyed by accident, and the row's
``outcome`` says which of those two it was.

It is NOT a behaviour change. Nothing here can block, permit, early-return
or alter a verdict. Every entry point swallows its own failure and returns
a None/empty answer, because a debt LEDGER that can fail the turn it is
accounting for is a worse defect than the missing accounting it replaces.

WHY THE ROW IS WRITTEN UP FRONT
-------------------------------
A row written at the END can only ever describe a dispatch that survived,
which is precisely the dispatch nobody needed a record of. The crash case
is the whole point: an ``in_flight`` row that is never closed IS the
evidence, and the duties it planned but never proved ARE the outstanding
debt. Same reasoning as ``receive_lease_store.arm``: the write that matters
happens before the thing that can die.

UNKNOWN STAYS UNKNOWN
---------------------
``outstanding_debt`` reports an unclosed row as ``presumed_lost`` only once
it is older than ``stale_after`` AND its pid is provably gone. A row that is
merely young is reported as ``in_flight``, not as debt -- and a pid we
cannot interrogate yields ``pid_alive: None`` and the status
``stale_unclosed``, never ``presumed_lost``. Laundering "I could not check"
into "the process is gone" would manufacture debt for every healthy
concurrent Stop on the box.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

# -- THE NINE DUTIES, ENUMERATED FROM THE CODE ------------------------
# Order is dispatch order in _handle_stop. The ids are stable strings: a
# debt row outlives the source layout, so a positional index would rot.
DUTY_TURN_END_AUDIT = "turn_end_audit"  # 1 LifecycleService.on_assistant_turn_end
DUTY_FAILURE_STEWARDSHIP = "failure_stewardship_gate"  # 2 blocks
DUTY_UPDATE_INTENT = "update_intent_durability_gate"  # 3 blocks
DUTY_DEPLOY_WAIT_NUDGE = "deploy_wait_conducting_prep"  # 4 early-returns
DUTY_BACKLOG_SURFACER = "backlog_surfacer"  # 5 early-returns
DUTY_RECEIVE_LEASE_ARM = "receive_lease_arm"  # 9a arms last
# 10 #1061 seated Consul: mirror to peer + refuse seal until parked. BLOCKS.
# Not idempotent: it judges whether a turn may seal, and that turn is gone.
DUTY_CONSUL_PARK = "consul_park_gate"

# Duties 6, 7, 8 and the blast half of 9 are INSIDE duty 1's call
# (LifecycleService.on_assistant_turn_end): the managed-session authority
# resolve, the assistant_turn_end event + gated message_text,
# _maybe_stop_capture (#316), and merge_child_envelope + stop_diagnostic
# (#1046). They are named here so the duty set reads as the full nine
# rather than the six THIS boundary can instrument directly -- a boundary
# that can only see six must not report six as the whole.
NESTED_IN_TURN_END_AUDIT = (
    "managed_session_authority",  # 6
    "assistant_turn_end_event",  # 7
    "stop_memory_capture",  # 8
    "blast_envelope_diagnostic",  # 9b
)

STOP_DUTIES = (
    DUTY_TURN_END_AUDIT,
    DUTY_FAILURE_STEWARDSHIP,
    DUTY_UPDATE_INTENT,
    DUTY_DEPLOY_WAIT_NUDGE,
    DUTY_BACKLOG_SURFACER,
    DUTY_RECEIVE_LEASE_ARM,
    DUTY_CONSUL_PARK,
)

# Duties a supervisor may SAFELY re-run out of band. Narrow on purpose.
#
# Arming a receive lease is an idempotent upsert on one route, so replaying
# it late is harmless and restores a fact that was merely missed. Nothing
# else qualifies: the two gates exist to decide whether a turn may seal and
# the turn they were judging is long gone, the two nudges are dedupe-keyed
# "once per epoch" surfaces whose window has closed, and the audit duty
# would write a turn-end event with a replay timestamp that a reader would
# take for the real one. A replay that cannot be distinguished from the
# original observation is a fabricated observation.
IDEMPOTENT_DUTIES = frozenset({DUTY_RECEIVE_LEASE_ARM})

OUTCOME_IN_FLIGHT = "in_flight"
OUTCOME_SEALED = "sealed"
OUTCOME_EARLY_RETURN = "early_return"
OUTCOME_RAISED = "raised"

# How long an unclosed row may sit before it is reported as debt rather
# than as a dispatch still running. A Stop hook lives inside a ~30s host
# budget; double it so a slow-but-alive dispatch is never called lost.
DEFAULT_STALE_AFTER = 60.0

_MAX_ERR = 300


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _connect(project_root: Path):
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    return _canonical_connect(db, durability=_Durability.AUDIT, row_factory=True)


def _init(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stop_duty_dispatch (
            dispatch_id TEXT PRIMARY KEY,
            event_name TEXT NOT NULL,
            host_session_id TEXT NOT NULL DEFAULT '',
            agent_id TEXT NOT NULL DEFAULT '',
            pid INTEGER NOT NULL DEFAULT 0,
            generation TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            finished_at REAL,
            outcome TEXT NOT NULL,
            duties_planned TEXT NOT NULL DEFAULT '[]',
            duties_proven TEXT NOT NULL DEFAULT '[]',
            duties_failed TEXT NOT NULL DEFAULT '{}',
            duties_unreached TEXT NOT NULL DEFAULT '[]',
            notes TEXT NOT NULL DEFAULT '',
            replayed TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL
        )
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stop_duty_dispatch_open "
        "ON stop_duty_dispatch (outcome, started_at)",
    )
    conn.commit()


def _loads(raw: Any, fallback):
    try:
        out = json.loads(str(raw or ""))
    except Exception:
        return fallback
    return out if isinstance(out, type(fallback)) else fallback


class Dispatch:
    """Handle on one open dispatch row. Every method is fail-quiet.

    A marker call that raises would convert an accounting problem into the
    exact hook crash this module was built to explain, so the ledger never
    gets to be the cause of the thing it measures.
    """

    def __init__(
        self,
        project_root: Path,
        dispatch_id: str,
        *,
        planned: tuple[str, ...],
    ) -> None:
        self.project_root = project_root
        self.dispatch_id = dispatch_id
        self.planned = tuple(planned)
        self.proven: list[str] = []
        self.failed: dict[str, str] = {}

    # -- markers ------------------------------------------------------
    def mark_proven(self, duty: str) -> None:
        """This duty RAN TO COMPLETION. Not "was attempted"."""
        duty = str(duty or "").strip()
        if not duty or duty in self.proven:
            return
        self.proven.append(duty)
        self._flush()

    def mark_failed(self, duty: str, exc: BaseException | str) -> None:
        """This duty was reached and did not complete. Durable DEBT.

        The cause is preserved, bounded, at the boundary that observed it
        -- the refusal-evidence rule: a known cause must never be filed as
        an unattributed absence.
        """
        duty = str(duty or "").strip()
        if not duty:
            return
        if isinstance(exc, BaseException):
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = str(exc)
        self.failed[duty] = detail[:_MAX_ERR]
        self._flush()

    def close(self, outcome: str, *, note: str = "", now: float | None = None) -> None:
        """Stamp the terminal outcome and compute what was never reached.

        ``duties_unreached`` is ``planned - proven - failed``. On
        ``early_return`` that set was skipped BY DESIGN (a blocked turn is
        not an ending turn, so its later duties are correctly absent); on
        ``raised`` the same set is the accident. The outcome column is what
        separates the two, and it is stamped by the writer that knows --
        never re-derived by a reader from the shape of the list.
        """
        ts = float(now if now is not None else time.time())
        seen = set(self.proven) | set(self.failed)
        unreached = [d for d in self.planned if d not in seen]
        try:
            with _connect(self.project_root) as conn:
                _init(conn)
                conn.execute(
                    "UPDATE stop_duty_dispatch SET outcome=?, finished_at=?, "
                    "duties_proven=?, duties_failed=?, duties_unreached=?, "
                    "notes=?, updated_at=? WHERE dispatch_id=?",
                    (
                        str(outcome or OUTCOME_SEALED),
                        ts,
                        json.dumps(self.proven),
                        json.dumps(self.failed),
                        json.dumps(unreached),
                        str(note or "")[:_MAX_ERR],
                        ts,
                        self.dispatch_id,
                    ),
                )
                conn.commit()
        except Exception:
            pass

    def _flush(self) -> None:
        ts = time.time()
        try:
            with _connect(self.project_root) as conn:
                _init(conn)
                conn.execute(
                    "UPDATE stop_duty_dispatch SET duties_proven=?, duties_failed=?, "
                    "updated_at=? WHERE dispatch_id=?",
                    (
                        json.dumps(self.proven),
                        json.dumps(self.failed),
                        ts,
                        self.dispatch_id,
                    ),
                )
                conn.commit()
        except Exception:
            pass


def open_dispatch(
    project_root: Path,
    *,
    event_name: str,
    payload: dict[str, Any] | None = None,
    duties: tuple[str, ...] = STOP_DUTIES,
    now: float | None = None,
) -> Dispatch | None:
    """Record that a Stop dispatch STARTED. Returns None if it could not.

    None means the caller runs exactly as it did before this module
    existed -- unaccounted, but unharmed. The alternative (raising) would
    make the ledger able to kill the dispatch it is accounting for.
    """
    payload = payload or {}
    ts = float(now if now is not None else time.time())
    dispatch_id = uuid.uuid4().hex
    try:
        with _connect(project_root) as conn:
            _init(conn)
            conn.execute(
                "INSERT INTO stop_duty_dispatch "
                "(dispatch_id, event_name, host_session_id, agent_id, pid, "
                "generation, started_at, outcome, duties_planned, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dispatch_id,
                    str(event_name or ""),
                    str(payload.get("session_id") or "").strip(),
                    str(payload.get("agent_id") or "").strip(),
                    int(os.getpid()),
                    str(os.environ.get("AIDOCS_RUNTIME_GENERATION") or ""),
                    ts,
                    OUTCOME_IN_FLIGHT,
                    json.dumps(list(duties)),
                    ts,
                ),
            )
            conn.commit()
    except Exception:
        return None
    return Dispatch(project_root, dispatch_id, planned=tuple(duties))


def _pid_alive(pid: int) -> bool | None:
    """True / False / None. None is NOT False.

    A pid we cannot interrogate (no permission, a platform that will not
    answer) is an unknown, and reporting it as dead would invent debt for
    every healthy concurrent Stop on the box.
    """
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _row_to_debt(row: dict[str, Any], now: float, stale_after: float) -> dict[str, Any]:
    planned = _loads(row.get("duties_planned"), [])
    proven = _loads(row.get("duties_proven"), [])
    failed = _loads(row.get("duties_failed"), {})
    unreached = _loads(row.get("duties_unreached"), [])
    outcome = str(row.get("outcome") or "")
    age = max(0.0, now - float(row.get("started_at") or now))
    alive = _pid_alive(int(row.get("pid") or 0))
    if outcome == OUTCOME_IN_FLIGHT:
        seen = set(proven) | set(failed)
        unreached = [d for d in planned if d not in seen]
        if age < stale_after:
            status = "in_flight"
        elif alive is False:
            status = "presumed_lost"
        else:
            # Old, and we could not PROVE the process is gone. Say exactly
            # that rather than picking the convenient half.
            status = "stale_unclosed"
    else:
        status = outcome
    return {
        "dispatch_id": row.get("dispatch_id"),
        "event_name": row.get("event_name"),
        "host_session_id": row.get("host_session_id"),
        "agent_id": row.get("agent_id"),
        "pid": int(row.get("pid") or 0),
        "pid_alive": alive,
        "generation": row.get("generation"),
        "started_at": row.get("started_at"),
        "age_seconds": round(age, 3),
        "outcome": outcome,
        "status": status,
        "duties_planned": planned,
        "duties_proven": proven,
        "duties_failed": failed,
        "duties_unreached": unreached,
        "nested_in_turn_end_audit": list(NESTED_IN_TURN_END_AUDIT),
        "replayable": sorted(
            {d for d in (list(failed) + list(unreached)) if d in IDEMPOTENT_DUTIES}
        ),
        "replayed": _loads(row.get("replayed"), {}),
        "notes": row.get("notes") or "",
    }


def outstanding_debt(
    project_root: Path,
    *,
    host_session_id: str = "",
    now: float | None = None,
    stale_after: float = DEFAULT_STALE_AFTER,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Stop dispatches that left a duty unproven. Observability only.

    Three shapes, never collapsed: a duty that FAILED with a recorded
    cause, a duty never REACHED because an earlier one early-returned (by
    design -- a blocked turn is not an ending turn), and a dispatch whose
    process vanished mid-flight (``presumed_lost`` / ``stale_unclosed``).
    """
    ts = float(now if now is not None else time.time())
    try:
        with _connect(project_root) as conn:
            _init(conn)
            if host_session_id:
                rows = conn.execute(
                    "SELECT * FROM stop_duty_dispatch WHERE host_session_id=? "
                    "ORDER BY started_at DESC LIMIT ?",
                    (host_session_id, max(1, int(limit)) * 4),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM stop_duty_dispatch ORDER BY started_at DESC LIMIT ?",
                    (max(1, int(limit)) * 4,),
                ).fetchall()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for raw in rows:
        d = _row_to_debt(dict(raw), ts, float(stale_after))
        if d["status"] == "in_flight":
            continue
        if not d["duties_failed"] and not d["duties_unreached"]:
            continue
        out.append(d)
        if len(out) >= max(1, int(limit)):
            break
    return out


def replay_idempotent(
    project_root: Path,
    *,
    dispatch_id: str = "",
    stale_after: float = DEFAULT_STALE_AFTER,
    now: float | None = None,
    arm=None,
) -> dict[str, Any]:
    """Re-run the IDEMPOTENT duties a dead dispatch never proved.

    Currently exactly one: ``receive_lease_arm``. The others are
    deliberately not replayable (see ``IDEMPOTENT_DUTIES``) -- a replayed
    gate verdict judges a turn that has already sealed, and a replayed
    audit event is indistinguishable from a real observation.

    ``arm`` is injectable for tests. A replay is STAMPED into the row so a
    supervisor cannot double-credit one, and nothing here ever marks a
    non-idempotent duty discharged: it is returned in ``outstanding``,
    which is the honest end state for a duty no one can safely re-run.
    """
    ts = float(now if now is not None else time.time())
    debts = outstanding_debt(project_root, now=ts, stale_after=stale_after, limit=200)
    if dispatch_id:
        debts = [d for d in debts if d["dispatch_id"] == dispatch_id]
    replayed: list[dict[str, Any]] = []
    still_owed: list[dict[str, Any]] = []
    for d in debts:
        todo = [x for x in d["replayable"] if x not in d["replayed"]]
        unsafe = [
            x
            for x in (list(d["duties_failed"]) + list(d["duties_unreached"]))
            if x not in IDEMPOTENT_DUTIES
        ]
        if unsafe:
            still_owed.append(
                {
                    "dispatch_id": d["dispatch_id"],
                    "event_name": d["event_name"],
                    "host_session_id": d["host_session_id"],
                    "status": d["status"],
                    "duties": unsafe,
                    "why": "not idempotent - cannot be replayed without fabricating "
                    "an observation; exposed as durable debt instead",
                },
            )
        for duty in todo:
            if duty != DUTY_RECEIVE_LEASE_ARM:
                continue
            ok, detail = _replay_arm(project_root, d, arm=arm)
            stamp = {"at": ts, "ok": ok, "detail": detail}
            _stamp_replay(project_root, str(d["dispatch_id"]), duty, stamp)
            replayed.append({"dispatch_id": d["dispatch_id"], "duty": duty, **stamp})
    return {
        "ok": True,
        "replayed": replayed,
        "outstanding": still_owed,
        "checked": len(debts),
    }


def _replay_arm(project_root: Path, debt: dict[str, Any], *, arm=None) -> tuple[bool, str]:
    try:
        if arm is not None:
            arm(project_root, debt)
            return True, "replayed via injected arm"
        from . import receive_lease_stop as _rls

        row = _rls.arm_on_stop(
            project_root,
            event_name=str(debt.get("event_name") or "Stop"),
            payload={
                "session_id": debt.get("host_session_id") or "",
                "agent_id": debt.get("agent_id") or "",
            },
        )
        if row is None:
            # Unresolvable route. NOT a success, and not laundered into one.
            return False, "route unresolvable at replay time - lease still unarmed"
        return True, "receive lease armed"
    except Exception as exc:  # noqa: BLE001 - a replay must not fail the sweep
        return False, f"{type(exc).__name__}: {exc}"[:_MAX_ERR]


def _stamp_replay(project_root: Path, dispatch_id: str, duty: str, stamp: dict) -> None:
    try:
        with _connect(project_root) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT replayed FROM stop_duty_dispatch WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            cur = _loads(row["replayed"] if row else "{}", {})
            cur[duty] = stamp
            conn.execute(
                "UPDATE stop_duty_dispatch SET replayed=?, updated_at=? WHERE dispatch_id=?",
                (json.dumps(cur), time.time(), dispatch_id),
            )
            conn.commit()
    except Exception:
        pass
