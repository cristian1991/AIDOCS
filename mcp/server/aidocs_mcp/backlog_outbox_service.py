"""The backlog OUTBOX drain, as its own service (P1 step 1).

OPERATOR RULING 2026-08-30: "Split sitter first: outbox drain becomes
independent service; then kill Git replication half."

WHY THE ORDER IS THAT WAY ROUND. `BacklogSyncSitter` does TWO unrelated jobs:
git replication of the event-file stream, and driving `backlog_hub_client.
drain_queue` — the HTTPS submit of the local SQLite outbox. Deleting "the
sitter" to retire the git transport would therefore delete THE ONLY THING THAT
FLUSHES THE OUTBOX, and the failure would be silent: writes would keep landing
locally and simply never reach the server. Separating them first makes the later
deletion a deletion rather than an amputation.

WHAT MOVED AND WHAT DID NOT. This is a MOVE, not a rewrite: `drain_once` is the
body that lived in `backlog_sync_sitter._hub_backlog_snapshot`, and that function
now delegates here. One implementation, two callers — the sitter (until its git
half is retired) and this module's own service loop. Copying it instead would
have produced the twin-drift this codebase keeps paying for.

THE DRAIN DOES NOT DEPEND ON THE GIT CYCLE, which is what makes the split safe.
`_emit_backlog` enqueues straight into `backlog_write_queue` (a local SQLite
table) on every backlog write; nothing about that path reads or waits for the
event-file replication. The sitter's own comment "Runs AFTER the git cycle so the
outbox is already complete" describes `_vps_hub_reconcile` — the #442 EVENT-STREAM
outbox, a different mechanism that this module deliberately does not touch.

STILL P0 SEMANTICS. This changes WHERE the drain lives, not WHAT IT MEANS. Local
`project_backlog` remains the writer of record; the server snapshot remains a
separate cache; a rejected write is still advisory. The authority flip, its
clean-queue precondition, and durable conflict records are later steps and are
deliberately absent here — a step that both moves code and changes meaning is
one nobody can review.

NO SERVICE CLASS HERE YET, AND THAT IS THE CORRECTION. The first draft shipped a
`BacklogOutboxService` poll loop plus a per-root registry, and Gate 1d refused
it: "unused function 'instance_for'". The gate was right, and the law is this
repo's own (183074ae — a capability with no consumer is not a capability).

The runtime half of "becomes an independent service" cannot land alone. Starting
a second poller while the sitter still drives `drain_once` puts TWO drains on ONE
queue, which risks double-submitting the same intents; and stopping the sitter's
drive is precisely the change this step promised not to make. So the service and
the removal of the sitter's drive are ONE step, and it is not this one. Shipping
the class early would have been "built, wired, and inert" — the exact shape #575
and #741 exist to complain about, committed while writing a brief about it.

What this step delivers is the STRUCTURAL half: the drain has one home, outside
the module scheduled for deletion, with the sitter delegating to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def drain_once(project_root: Path) -> dict:
    """Submit the outbox, refresh the snapshot, report convergence + conflicts.

    Ordered deliberately: DRAIN, then refresh, then status — so the reported
    state reflects THIS cycle's writes instead of lagging one cycle behind.

    Unbound / local-only projects are a no-op and never touch the network.
    NEVER RAISES: a drain that fails must not take down whatever is driving it,
    and an offline drain must never look like a successful one — `drain_queue`
    leaves the queue intact and says why.
    """
    try:
        from . import backlog_hub_client as _hub

        if not _hub.is_bound(project_root):
            return {"bound": False}
        out: dict[str, Any] = {"drain": _hub.drain_queue(project_root)}
        out.update(_hub.refresh_cache(project_root))
        out["status"] = _hub.cache_status(project_root)
        # Conflicts are SURFACED, never silently discarded — operator ruling
        # 2026-08-30: "Rejected/conflicting local write never vanishes: preserve
        # intent + reason as durable conflict, surface to operator."
        from . import backlog_write_queue as _q

        _org, _pid = _hub.binding(project_root)
        out["conflicts"] = _q.conflicts(project_root, _pid)
        return out
    except Exception as exc:  # noqa: BLE001 — a drain never breaks its driver
        return {"bound": False, "error": type(exc).__name__}


def cutover_readiness(project_root: Path) -> dict:
    """Can the authority flip run yet, and if not, WHAT is blocking it.

    OPERATOR RULING 2026-08-30: "Cutover: require pending queue drained before
    authority flip. Existing unresolved conflicts may block flip until
    classified."

    TWO BLOCKERS, AND THEY ARE DIFFERENT KINDS OF NOT-READY:

      pending writes      — the flip is a moving target. A write authored under
                            LOCAL authority that lands after the server becomes
                            authoritative has no rule saying which wins. Waiting
                            for the drain removes the case entirely rather than
                            adjudicating it.
      unclassified        — a refusal nobody has ruled on. Flipping over it would
      conflicts             bury a local intent under a server value with no
                            record that anyone chose, which is the clause
                            "never silently keep rejected optimistic local row as
                            truth" read in the other direction.

    UNKNOWABLE IS NOT READY. If the queue depth cannot be determined, this
    reports `ready: False` with that as the reason. A precondition satisfied by
    ignorance is the shape that lets a flip proceed over an unknown queue — and
    a 0 returned from a failed count is indistinguishable from a real 0 to
    every caller downstream.

    UNBOUND PROJECTS ARE NOT READY EITHER, and for a reason worth stating: a
    project with no server binding has no authority to flip TO. That is not a
    blocker to clear, it is a different situation, and it is reported as its own
    reason rather than as an empty queue that looks ready.

    THE `sync` BLOCK (#1002 gap 3) rides along on every answer: sitter running,
    last cycle result, the vps_hub skip reason, the last recorded drain outcome
    and the pending count. And a pending blocker on a surface that CANNOT drain
    says so and names the remedy, instead of "drain the outbox first".
    """
    out: dict[str, Any] = {
        "ready": False,
        "pending": None,
        "unclassified_conflicts": 0,
        "blockers": [],
    }
    try:
        from . import backlog_hub_client as _hub
        from . import backlog_write_queue as _q

        # THE `sync` BLOCK (#1002 gap 3), on EVERY answer bound or not: the
        # sitter's last cycle, the vps_hub skip reason, and the last recorded
        # drain outcome. They were "detectable, not silent" by design and
        # silent to every agent in practice — no tool read them.
        out["sync"] = _sync_block(project_root)

        # ATTRIBUTION IS NOT A FAILURE-ONLY CONCERN (#972). This was spliced in
        # only on the unbound branch below, so a SUCCESSFUL answer never said
        # which resolver produced it — measured on the operator's gate, where
        # `ready: true, pending: 0` came back carrying no context at all. That
        # is this item's own defect surviving in its diagnostics: #972 exists
        # because TWO authorities could answer "which project/org am I on", and
        # a bound answer that does not name its source is exactly the state in
        # which a silent second authority is undetectable. "Right now, from the
        # wrong authority" is the shape that rots without ever failing.
        try:
            out["binding_context"] = _hub.binding_context(Path(project_root))
        except Exception:  # noqa: BLE001 — a failed annotation annotates nothing
            out["binding_context"] = None

        _org, project_id = _hub.binding(Path(project_root))
        if not project_id:
            # WHICH unbound, not merely unbound. The operator gave this ruling
            # one layer down, for `converged: None`: "don't collapse all ... into
            # one state. Need explicit reason ... Otherwise new label still hides
            # cause." It binds here for the same reason — `binding()` comes back
            # empty by several routes and they have DIFFERENT REPAIRS (connect
            # the project vs. set the S2S credentials), so one sentence for all
            # of them sends the reader nowhere.
            #
            # `_unbound_reason` already walks binding()'s own order and
            # REASON_REMEDY already carries the repair for each. This consumed
            # NEITHER, and wrote its own flat string instead — a discriminator
            # with no caller sitting beside a remedy nobody could reach. That
            # matters most exactly here: the cutover's acceptance step IS a
            # gate-side call to this function, so an undifferentiated "not bound"
            # is the step meant to PRODUCE evidence emitting a sentence instead.
            try:
                reason = _hub._unbound_reason(Path(project_root))
                remedy = _hub.REASON_REMEDY.get(reason, "")
            except Exception:  # noqa: BLE001
                # UNKNOWABLE IS NOT READY applies to the reason too. A failed
                # lookup annotates nothing and must never soften the verdict it
                # was called to explain — adding a call is how a readiness gate
                # acquires a truthy default.
                reason, remedy = "", ""
            out["reason"] = reason
            # `binding_context` is already set ABOVE, for every answer bound or
            # not — one call, one home. It used to be spliced in here, which is
            # what made success unattributed; resolving it twice would also ask
            # the resolver the same question twice per readiness call.
            blocker = (
                "this project is not bound to a server backlog — there is no "
                "authority to flip to"
            )
            if remedy:
                blocker = f"{blocker}; {reason}: {remedy}"
            elif reason:
                blocker = f"{blocker} ({reason}; no remedy on record)"
            else:
                blocker = (
                    f"{blocker}; and WHY it is unbound could not be determined "
                    "— that unknown is itself the first thing to repair"
                )
            out["blockers"] = [blocker]
            return out

        try:
            pending = len(_q.pending(Path(project_root), project_id) or [])
        except Exception:  # noqa: BLE001
            pending = None
        out["pending"] = pending
        # The sync block's `pending` is "filled in by the caller once the depth
        # is counted" — on EVERY answer. It used to be written only inside the
        # `elif pending:` branch below, so an EMPTY queue left it None, which
        # is the "could not count" value one surface over.
        if isinstance(out.get("sync"), dict):
            out["sync"]["pending"] = pending

        unclassified = _q.unclassified_conflicts(Path(project_root), project_id)
        out["unclassified_conflicts"] = len(unclassified)
        # THE BODIES, NOT ONLY THE HANDLES. Step 3 of the cutover is "classify
        # every conflict" and the verdicts are discard|requeue|keep — choosing
        # among those from a bare integer is not a decision. The rows are already
        # in hand here (op, reason, the stored `fields` intent); projecting the
        # id out of each and dropping the rest is the very thing `conflicts()`
        # confesses one module over: "Storage kept both; the reader returned one."
        #
        # AND THIS IS THE ONLY SURFACE THAT CAN SHOW SOME OF THEM.
        # `local_unaccepted` excludes rejected UPDATEs (the item stays
        # authoritative, so it belongs in `items`) and rows with no globalId.
        # Both still BLOCK the flip. Without this they would block it from
        # behind a surface nobody can read — an operator ruling on something no
        # view would show them, which is `classify()`'s own "check that passes
        # without examining anything".
        #
        # `unclassified_queue_ids` is kept as the compact handle for
        # classify_conflict and is derived from the SAME list in the next line,
        # so the two cannot drift apart.
        if unclassified:
            out["unclassified"] = unclassified
        out["unclassified_queue_ids"] = [r.get("queue_id") for r in unclassified]

        blockers: list[str] = []
        if pending is None:
            blockers.append(
                "the outbox depth could not be determined — 'unknown' is not "
                "'empty', and a flip must not proceed over a queue nobody counted"
            )
        elif pending:
            sync = out.get("sync") if isinstance(out.get("sync"), dict) else {}
            drain = sync.get("drain") if isinstance(sync.get("drain"), dict) else {}
            drain_reason = str(drain.get("reason") or "")
            # "Drain first" is honest only where a drain CAN happen. When the
            # last drain recorded that no credential can carry the queue on
            # this surface, the instruction must name the cause and the
            # repair — telling the operator to do the impossible sends them
            # nowhere (#1002 gap 1, measured: 55 intents, every poll).
            cannot_drain = {
                getattr(_hub, "REASON_UNCONFIGURED", "hub_unconfigured"),
                getattr(_hub, "REASON_GATE_CREDENTIAL", "gate_credential_unusable"),
            }
            if drain_reason and drain_reason in cannot_drain:
                remedy = str(drain.get("remedy") or "")
                blockers.append(
                    f"{pending} write(s) still un-submitted and this surface cannot "
                    f"drain them: {drain_reason}"
                    + (f" ({drain.get('credential')})" if drain.get("credential") else "")
                    + (f" — {remedy}" if remedy else "")
                )
            else:
                blockers.append(
                    f"{pending} write(s) still un-submitted; drain the outbox first "
                    "so nothing authored under local authority lands after the flip"
                    + (f" (last drain: {drain_reason})" if drain_reason else "")
                )
        if unclassified:
            blockers.append(
                f"{len(unclassified)} conflict(s) nobody has ruled on — classify "
                "each with ai_backlog(mode='classify_conflict', id=<queue_id>, "
                "reason=discard|requeue|keep)"
            )
        out["blockers"] = blockers
        out["ready"] = not blockers
        return out
    except Exception as exc:  # noqa: BLE001
        out["blockers"] = [f"readiness could not be established: {type(exc).__name__}"]
        return out


def _sync_block(project_root: Path) -> dict:
    """What an agent can know about WHY the outbox is (not) moving (#1002 gap 3).

      sitter_running   is a BacklogSyncSitter polling this root at all
      last_cycle       its last `sync_once` summary: ok / errors / deferred_by /
                       trigger — None until a cycle has run
      vps_hub          the #442 event-stream reconcile's block from that cycle
                       (enabled / skipped=<reason> / error)
      drain            the last recorded `drain_queue` outcome — reason, route,
                       credential state, remedy, timestamps; attempted=False
                       when no drain has ever recorded itself
      pending          filled in by the caller once the depth is counted

    Never raises: a diagnostic that takes the readiness answer down with it
    has made things less observable, not more.
    """
    block: dict[str, Any] = {
        "sitter_running": False,
        "last_cycle": None,
        "vps_hub": None,
        "drain": None,
        "pending": None,
    }
    try:
        from . import backlog_hub_client as _hub
        from . import backlog_sync_sitter as _sitter

        st = _sitter.backlog_sync_status(project_root)
        if isinstance(st, dict):
            block["sitter_running"] = bool(st.get("running"))
            last = st.get("last_result")
            if isinstance(last, dict) and last:
                block["last_cycle"] = {
                    "ok": last.get("ok"),
                    "errors": list(last.get("errors") or []),
                    "deferred_by": str(last.get("deferred_by") or ""),
                    "trigger": str(last.get("trigger") or ""),
                }
                vps = last.get("vps_hub")
                block["vps_hub"] = vps if isinstance(vps, dict) else None
        _org, pid = _hub.binding(Path(project_root))
        if pid:
            block["drain"] = _hub.last_drain_outcome(Path(project_root), pid)
    except Exception as exc:  # noqa: BLE001
        block["error"] = type(exc).__name__
    return block


def unaccepted_local_writes(project_root: Path) -> list[dict]:
    """Refused CREATEs — local rows the server never accepted.

    OPERATOR RULING 2026-08-30: "rejected CREATE must not enter authoritative
    backlog counts/status filters. Surface it as separate
    local_unaccepted/refused conflict object attached to read response."

    WHY A REFUSED CREATE IS NOT A LIST ROW. An earlier proposal was to show it in
    `items`, flagged. That is wrong, and the reason is arithmetic: a flagged row
    is still A ROW. It would be counted by `count`, matched by `status=open` and
    `priority=critical`, and folded into every tally the backlog reports — so the
    local surface would disagree with the server about how many items exist,
    which is the divergence this cutover exists to REMOVE, reintroduced by the
    very view meant to be honest about it.

    HOW A REFUSAL IS TOLD FROM A REJECTED UPDATE, without asking the server: the
    P0 cache (`backlog_server_cache`) is keyed per item, so a conflicted intent
    whose global_id has NO cached row is one the server has never accepted —
    a refused create. One whose id IS cached is a rejected UPDATE: the ITEM is
    authoritative even though the attempted change was not, so it stays in
    `items` showing the SERVER's value, and does not belong here.

    Offline-safe by construction: `refresh_cache` returns early on an
    unreachable hub BEFORE it clears the cache, so the last known-good snapshot
    survives and this classification keeps working while offline.

    NEVER RAISES, and returns [] when it cannot classify — a read surface must
    not break because a conflict view could not be built.
    """
    try:
        from . import backlog_hub_client as _hub
        from . import backlog_write_queue as _q

        _org, project_id = _hub.binding(Path(project_root))
        if not project_id:
            return []  # unbound: nothing was ever submitted, so nothing refused
        out: list[dict] = []
        for row in _q.conflicts(Path(project_root), project_id) or []:
            gid = str(row.get("globalId") or "")
            if not gid:
                continue
            if _hub.cached_updated_at(Path(project_root), gid):
                continue  # the server knows this item — a rejected UPDATE
            out.append(
                {
                    "globalId": gid,
                    "op": row.get("op"),
                    "reason": row.get("reason"),
                    "intent": row.get("fields") or {},
                    "queue_id": row.get("queue_id"),
                    # WHAT THE CUTOVER GATE WILL READ, shown here so an operator
                    # can see what still blocks a flip without a second call —
                    # and so `queue_id` has an obvious use: it is the handle for
                    # ai_backlog(mode='classify_conflict').
                    "classification": row.get("classification") or "unclassified",
                    "classified_by": row.get("classified_by") or "",
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return []

