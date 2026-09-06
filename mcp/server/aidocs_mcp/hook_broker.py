"""Hook broker — resident hook-evaluation endpoint (#332, #335 Phase 3).

WHY: every mutating Claude Code tool call cold-starts 2-3 fresh
``python.exe -m aidocs_mcp.claude_hook`` interpreters (~100-300ms each,
~0.5-1s/call + conhost flashes) even though a RESIDENT process with all
gate code loaded already exists. This module is the daemon side of the
thin-client fix: a tiny loopback-only TCP listener, hosted by the
watchdog process (``aidocs_service.run_watchdog``), that evaluates hook
events IN-PROCESS through the exact same core the subprocess uses.

SEAM: the watchdog — not ``mcp_server --http`` — hosts the broker.
Reasons: (a) purely ADDITIVE (one start/close pair in ``run_watchdog``;
no conductor-owned files touched), (b) the watchdog outlives daemon
crashes/overlap-restarts, so hook evaluation stays warm across deploys,
(c) same package, same user, same machine — the gate stack lazy-loads on
first request. Trade-off: the watchdog runs code from ITS start time, and a
runtime refresh restarts the daemon CHILD, never the watchdog — so a deploy
does not reach this process. That was documented here as harmless on the
grounds that "the client falls back to local evaluation on any drift-induced
failure anyway", which was FALSE: nothing failed. Stale code answered
successfully, the client's trust checks (protocol version, custody,
session/root echo) all passed, and the replaced law kept ruling — measured on
the operator box 2026-07-30 (#609). ``package_code_identity`` +
``_staleness_reason`` make the assumption true instead of assumed: this
process refuses to answer once it can no longer prove it is the code on disk,
which turns silent staleness into the ordinary local-evaluation fallback the
floor was always designed for.

ONE LOGIC, ONE HOME (Article XXII): ``evaluate_hook_event`` wraps
``claude_hook.ClaudeHookHandler.handle`` — nothing is duplicated. A fresh
handler per request mirrors the fresh-subprocess-per-event semantics.

PROTOCOL v1 (one UTF-8 JSON line each way, ``\\n``-terminated):

  request  = {"v": 1, "kind": "hook_eval", "token": "<from state file>",
              "payload": {<exact JSON the claude_hook subprocess reads
                           on stdin — hook_event_name, cwd, session_id,
                           tool_name, tool_input, ...>},
              "project_root": "<root the client derived from payload.cwd>",
              "env": {"AIDOCS_*": "..."}}   # identity bits; RECORDED in
                                            # the reply context only, NOT
                                            # applied to os.environ (a
                                            # client must not be able to
                                            # reshape the daemon's env)
  response = {"v": 1, "ok": true, "response": <hook JSON | null>,
              "session_id": "<echo of payload.session_id>",
              "project_root": "<echo of request.project_root>",
              "eval_ms": <float>}
           | {"v": 1, "ok": false, "error": "<reason>"}

DISCOVERY: ``<daemon_dir>/hook_broker.json`` =
``{"v": 1, "port": N, "pid": N, "token": "<hex>", "started_at": "..."}``.
The token is a same-user shared secret: a request without it is refused
before any evaluation runs.

SECURITY FLOOR (test-pinned in tests/host/test_hook_broker.py):
  * loopback bind ONLY — a non-loopback host raises at construction;
  * bad token / malformed request → ``ok: false``, never an evaluation;
  * the CLIENT treats any failure as None = "evaluate locally"
    (see hook_broker_client) — the broker being down can never fail-open.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

from . import hook_budget as _hook_budget  # stdlib-only constants; no cycle

PROTOCOL_VERSION = 1
STATE_FILENAME = "hook_broker.json"
# Hook payloads are small (tool_input + ids); 4MB is a generous ceiling
# that still refuses a runaway/hostile stream.
MAX_REQUEST_BYTES = 4 * 1024 * 1024
# #489: was a hardcoded 5.0 while the client gave up at 2.0, so a 2-5s request
# was abandoned by the client while this side kept computing. One source of
# truth now; the grace over the client budget covers socket read only, never
# extra compute (see hook_budget).
_CONN_TIMEOUT_S = _hook_budget.BROKER_CONN_TIMEOUT_S


# ── #489 timing ring: the queue instrument must survive its client ──────────
# queue_ms/eval_ms were returned in the REPLY only, and only on the success
# path. A request dropped as client_deadline_passed carried no timings, and even
# a successful reply goes to a client that timed out at its budget and stopped
# listening. The broker kept no record of any kind. So in every failure mode —
# including the timed_out case #489 exists to diagnose — the numbers were
# emitted to nobody and stored nowhere, which is why #489's own next step ("read
# queue_ms vs eval_ms") was never executable. Queue and compute have OPPOSITE
# fixes (concurrency vs caching), so guessing between them is how the earlier
# pass spent effort for an ~8ms and a ~16ms return.
#
# IN-MEMORY BY CONSTRUCTION: this is the hot path and DB contention is itself a
# suspect, so a row per evaluation would contaminate the measurement it exists
# to provide (#489's own METHOD WARNING: "get a measurement method that does not
# distort what it measures"). A bounded deque costs no IO.
TIMINGS_CAPACITY = 256

# Protocol kind for the read-only timings surface (#489). Same connection, same
# token gate, same loopback bind — a second listener would be a second thing to
# secure, and this one is already refused without the same-user secret.
TIMINGS_KIND = "hook_timings"


def package_code_identity(pkg_dir: str | Path | None = None) -> str | None:
    """Fingerprint the PYTHON CODE in one package directory (#609).

    THE PROBLEM THIS ANSWERS: this broker is hosted by the WATCHDOG process,
    which a runtime refresh never restarts (it replaces only the daemon child —
    ``aidocs_service.run_watchdog``), so its already-imported modules keep
    deciding every hook verdict long after the package on disk was replaced.
    Nothing in the rendezvous could reveal that: ``hook_broker.json`` carries
    only ``{v, port, pid, token, started_at}``, and ``PROTOCOL_VERSION`` is a
    constant a code update never moves. A stale broker was indistinguishable
    from a fresh one to a client that had every reason to trust it.

    WHY IT WALKS THE WHOLE TREE. The first pass fingerprinted only the FLAT
    top directory, and that was a fail-GREEN hole: ~51 of the package's ``.py``
    files live in subpackages, ``enforcement_pkg/`` — the decision code itself —
    among them. A deploy that changed only ``enforcement_pkg/decision.py`` moved
    nothing the detector looked at, so a broker still running the PREVIOUS
    enforcement code reported itself fresh and kept answering. A detector that
    says fresh when it cannot see the change launders the doubt, which is worse
    than no detector.

    Still cheap by construction — ``scandir``, names and stat only, never file
    contents — because it runs per hook event on the path #489 spent itself
    making fast. ``.py`` only: templates and package data are not the executing
    law, and charging the warm path for their churn would be a second defect.
    ``__pycache__`` is skipped for the mirror-image reason: a mere import
    rewrites it, so counting it would cry stale on a tree nobody deployed to.

    Returns None when no identity can be established (missing/unreadable
    directory, no modules). None is UNKNOWN, never "matches" — the caller
    treats it as unproven, which is the fail-closed reading.
    """
    root = Path(pkg_dir) if pkg_dir is not None else Path(__file__).resolve().parent
    rows: list[tuple[str, int, int]] = []
    # Explicit stack, not os.walk: one scandir per directory, no sorting or
    # list-building per level, and symlinked directories are NOT followed — a
    # link out of the package could otherwise make the identity depend on a
    # tree the deploy never ships (and loop forever).
    stack: list[tuple[str, str]] = [(str(root), "")]
    try:
        while stack:
            directory, prefix = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name == "__pycache__":
                        continue
                    rel = f"{prefix}{entry.name}"
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, f"{rel}/"))
                        continue
                    if not entry.name.endswith(".py"):
                        continue
                    st = entry.stat()
                    rows.append((rel, st.st_size, st.st_mtime_ns))
    except OSError:
        return None
    if not rows:
        return None
    digest = hashlib.sha256()
    for name, size, mtime_ns in sorted(rows):
        digest.update(f"{name}\0{size}\0{mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


PROVENANCE_ENFORCE_ENV = "AIDOCS_BROKER_REQUIRE_SHIPPED"


def _provenance_is_enforced() -> bool:
    """Whether an unshipped tree REFUSES rather than merely announcing itself.

    Opt-in, pending the #832 ruling on what a local runtime is installed FROM.
    Default off is not a softening of the operator's ruling -- the ruling is
    about PRODUCTION, where the runtime is a packaged artefact and this verdict
    is clean. Default ON would instead punish the dev box, whose runtime is
    installed from source and therefore can never carry a stamp, by disabling
    the warm path #489 exists to provide. Announcement happens either way.
    """
    return (os.environ.get(PROVENANCE_ENFORCE_ENV) or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def artefact_provenance_reason(code_root: str | Path | None) -> str | None:
    """#727 (B). None when the tree this broker RUNS is a packaged artefact.

    THE OPERATOR RULING THIS IMPLEMENTS (2026-08-01): "we cannot dogfood a
    'pinned security' project from the in-flight source, that's why the whole
    supervisor/deamon/watchdog flow exists", and "the committed/deployed head
    should be the 'source of truth' to compare with".

    WHAT WAS WRONG WITH THE OLD BASELINE. ``package_code_identity`` asks one
    question and asks it well: has MY tree changed since I imported it? That is
    the LOADED axis, and no filesystem check but this one can answer it. But it
    is self-referential -- the tree is compared only to ITSELF -- so a broker
    hosted out of a source checkout was perfectly self-consistent while
    enforcing code that was never built, reviewed or deployed. It reported
    healthy for the one posture the supervisor exists to prevent.

    WHY THE BUILD STAMP AND NOT A HEURISTIC. ``build_stamp`` already answers
    "which commit produced these bytes", travels INSIDE the artefact, is parsed
    and never imported, and ``process_stamp`` consumes it for identity "rather
    than inventing a parallel provenance path" (process_stamp.py:104). Sniffing
    for a ``.git`` directory above the package would have been exactly that
    parallel path, with worse answers.

    ONLY read_build_stamp, NOT build_stamp_verdict: the verdict recomputes the
    package fingerprint over every file, and this runs where a watchdog starts.
    Whether the bytes still hash to their recorded value is #627's question and
    is answered elsewhere; the question HERE is narrower -- can this artefact
    name the commit it was built from at all.

    UNKNOWN IS TREATED AS UNSHIPPED, consistent with the identity gate above
    ("a broker that cannot verify its own code cannot be trusted to PERMIT, but
    can always be trusted to refuse"). The cost of a false refusal is the warm
    path, never correctness: the caller re-evaluates locally in a fresh
    subprocess that loads the current code, so every event stays governed.
    """
    try:
        from .build_stamp import read_build_stamp

        stamp = read_build_stamp(code_root)
    except Exception:  # a broken reader must not read as good provenance
        return (
            "broker_provenance_unknown: this hook broker cannot read the build "
            f"stamp of the tree it runs ({code_root}), so it cannot show that "
            "it is executing a deployed artefact. Treated as unshipped."
        )
    unshipped = (
        "broker_unshipped_code: this hook broker is enforcing a tree that was "
        f"never produced by a packaging step ({code_root}"
    )
    tail = (
        "). AIDOCS cannot dogfood a pinned-security project from in-flight "
        "source -- that is what the supervisor/watchdog flow exists to prevent. "
        "NOT a policy decision and NOT a gate that stopped: evaluation falls "
        "back to the local evaluator, which loads the current code, so every "
        "event is still governed. Start the service under the owned runtime "
        "(`aidocs runtime --fix` provisions one) so the watchdog hosts the "
        "installed artefact."
    )
    if stamp is None:
        return unshipped + ", no _build_stamp.py" + tail
    if not str(stamp.get("commit") or ""):
        return unshipped + ", build stamp names no commit" + tail
    return None


def _ownership_refusal(code_root, why: str) -> str:
    """One wording for every ownership verdict, differing only in `why`.

    The diagnosis is carried IN the message because an operator told only
    "not the owned runtime" cannot tell an unprovisioned box from a wrong
    interpreter from an editable `.pth`, and those need different fixes --
    the same reason `supervisor_refusal` replays its `checked` list.
    """
    return (
        "broker_unowned_runtime: this hook broker cannot show that the tree it "
        f"enforces ({code_root}) is the AIDOCS-owned pinned runtime: {why}. "
        "AIDOCS cannot dogfood a pinned-security project from in-flight source "
        "-- that is what the supervisor/watchdog flow exists to prevent. NOT a "
        "policy decision and NOT a gate that stopped: evaluation falls back to "
        "the local evaluator, which loads the current code, so every event is "
        "still governed. Start the service under the owned runtime "
        "(`aidocs runtime --fix` provisions one) so the watchdog hosts the "
        "installed artefact."
    )


def runtime_ownership_reason(
    code_root: str | Path | None,
    *,
    home: Path | str | None = None,
    executable: str | None = None,
    prefix: str | None = None,
) -> str | None:
    """#727 (B), THIRD AXIS. None when the tree this broker runs IS the pinned
    runtime's -- as opposed to merely being self-consistent (LOADED) or merely
    carrying a build stamp (SHIPPED).

    WHY THIS IS NOT A NEW BASELINE FOR ``package_code_identity``. The literal
    reading of (B) -- "baseline the pinned runtime instead of ``__file__``" --
    would be a fail-GREEN regression. That function answers the LOADED axis:
    has the tree I am EXECUTING changed under me since I imported it. Hashing
    some OTHER tree while executing this one makes every edit to the executing
    tree invisible, which is precisely the hole ``BrokerChild``'s docstring
    records #609 pass 2 closing. ``__file__`` is therefore correct there and
    stays; the pinned runtime is a DIFFERENT question, asked here beside it.

    WHY IT IS NOT REDUNDANT WITH THE SHIPPED AXIS. The build stamp answers
    "which commit produced these bytes", and #832 measured that answer being
    BORROWED: a source checkout carrying leftover ``_build_stamp.py`` residue
    from an old local wheel build read CLEAN, so the posture the supervisor
    exists to prevent was invisible to the stamp alone on any box that had ever
    run a build. #832 is still OPEN, and this axis does not wait on it -- an
    editable ``.pth`` cannot put a checkout INSIDE the owned interpreter's own
    environment, and install 3 (the system Python that hosted this broker for
    six weeks) fails both halves below.

    TWO FACTS, BOTH EXACT, NEITHER A HEURISTIC:
      1. AM I THE OWNED INTERPRETER -- ``sys.executable`` sits in the same
         directory as the interpreter ``resolve_runtime`` names. Directory, not
         filename, because ``supervisor_runtime`` swaps the resolved python for
         its pythonw sibling so the detached watchdog never allocates a console
         (#249); that substitution is cosmetic and must not read as a different
         runtime.
      2. DID MY CODE COME FROM MY OWN ENVIRONMENT -- ``code_root`` lies under
         ``sys.prefix``. ``sys.prefix`` is authoritative, in-process and free;
         it needs no guess about venv-versus-standalone layout. A tree reached
         through an editable ``.pth`` or ``PYTHONPATH`` is by construction NOT
         under it, which is exactly the shape of the measured incident.

    NO HARDCODED PATH: the owned runtime comes from
    ``runtime_provisioner.resolve_runtime``, the canonical tier walk
    (operator_pin -> standalone -> venv) that ``supervisor_runtime`` already
    uses for (A), so ownership means the same thing at both ends of the system.

    ``verify=False`` DELIBERATELY: verification shells out to the candidate
    interpreter, and this runs where a watchdog starts and on the path #489 was
    spent making fast. Whether the owned runtime WORKS is (A)'s question, asked
    where a process is about to be started under it; the question HERE is only
    which paths are its own.

    UNKNOWN IS UNPROVEN, the rule both other axes already follow. A resolver
    that raises, a box with nothing provisioned, an unreadable root -- each
    yields a reason, never a clean verdict. The cost of a false refusal is the
    warm path only: the caller re-evaluates locally in a fresh subprocess that
    loads the current code, so every event stays governed.
    """
    exe = executable if executable is not None else sys.executable
    pfx = prefix if prefix is not None else sys.prefix
    try:
        from .runtime_provisioner import resolve_runtime

        rt = resolve_runtime(
            Path(home) if home is not None else Path.home(), verify=False
        )
    except Exception as exc:  # a broken resolver must not read as ownership
        return _ownership_refusal(
            code_root, f"the owned-runtime resolver failed ({exc})"
        )
    owned_path = rt.get("path") if rt.get("owned") else None
    if not owned_path:
        return _ownership_refusal(
            code_root,
            "no AIDOCS-owned runtime resolves on this box "
            f"(tier={rt.get('tier') or 'none'})",
        )
    try:
        same_runtime = (
            Path(owned_path).resolve().parent == Path(exe).resolve().parent
        )
    except (OSError, TypeError, ValueError):
        same_runtime = False
    if not same_runtime:
        return _ownership_refusal(
            code_root,
            f"this process runs {exe}, but the AIDOCS-owned runtime is "
            f"{owned_path}",
        )
    try:
        from_own_env = Path(code_root).resolve().is_relative_to(  # type: ignore[arg-type]
            Path(pfx).resolve()
        )
    except (OSError, TypeError, ValueError):
        from_own_env = False
    if not from_own_env:
        return _ownership_refusal(
            code_root,
            "the code was imported from outside this interpreter's own "
            f"environment ({pfx}) -- an editable .pth or PYTHONPATH is "
            "pointing enforcement at a tree the deploy never shipped",
        )
    return None


def _reset_timings(broker) -> None:
    """Install a fresh bounded ring (also the test seam)."""
    import collections

    broker._timings = collections.deque(maxlen=TIMINGS_CAPACITY)  # noqa: SLF001


def _record_timing(
    broker,
    *,
    event: str,
    queue_ms: float,
    eval_ms: float,
    outcome: str,
    late: bool = False,
) -> None:
    """Append one evaluation's split timing. Never raises, never does IO.

    ``late`` marks an evaluation that FINISHED after its caller's budget had
    already run out (#489). Measured live 2026-07-29, 256-sample ring: 9 of 240
    enforcement evaluations (3.75%) ran past the 2.0s enforcement budget and
    were every one of them recorded as ``outcome="ok"`` — a reply computed in
    full and delivered to a client that had already fallen back. Without this
    flag the instrument reports a budget mismatch as perfect health, which is
    the same class of blindness that made #489's queue-versus-compute question
    unanswerable in the first place.
    """
    try:
        broker._timings.append(  # noqa: SLF001
            {
                "event": event,
                "queue_ms": queue_ms,
                "eval_ms": eval_ms,
                "outcome": outcome,
                # Bool, not a duration: the question is "did the caller still
                # care", and by construction nothing here knows how long the
                # caller waited after giving up.
                "late": bool(late),
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    except Exception:  # noqa: BLE001 — diagnostics must never break the broker
        pass


def recent_timings(broker) -> "list[dict]":
    """Snapshot of the ring, oldest first. Empty when nothing has run."""
    try:
        return list(broker._timings)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return []

# ── #489 THE READER: the ring had no production consumer ────────────────────
# recent_timings() above existed referenced only by its own method wrapper and
# one test, so #489's documented next step ("read queue_ms vs eval_ms") was
# never executable and an earlier pass GUESSED — spending its effort on an ~8ms
# and a ~16ms return against a 10s budget.
#
# WHY THIS IS A PROTOCOL KIND AND NOT AN MCP TOOL. The broker is hosted by the
# WATCHDOG process (aidocs_service.run_watchdog), not the MCP daemon. A tool
# running in the daemon cannot see this deque — different process, no shared
# memory. The only ways across that boundary are a file/DB write (forbidden:
# the task and this module's own METHOD WARNING refuse IO on the recording
# path) or this socket, which already exists, is already token-gated, and is
# already loopback-only. So the reader rides the socket.
#
# COSTS THE HOT PATH NOTHING: answered without touching the evaluation gate and
# with no IO, so asking for numbers can never perturb the numbers.
# ``refused_stale_code`` (#609) is seeded here so the summary reports an
# explicit 0 when it never happened: "the broker is current" is a claim worth
# being able to READ, and an outcome that only appears once it has already gone
# wrong cannot be used to prove the healthy case.
_OUTCOMES = (
    "ok",
    "dropped_before_queue",
    "dropped_after_queue",
    "refused_stale_code",
)


def _percentile(sorted_values: "list[float]", fraction: float) -> float:
    """Nearest-rank percentile. Cheap and exact enough for <=256 samples."""
    if not sorted_values:
        return 0.0
    idx = int(round(fraction * (len(sorted_values) - 1)))
    return round(float(sorted_values[max(0, min(idx, len(sorted_values) - 1))]), 3)


def _timings_by_event(rows: "list[dict]") -> dict:
    """Per-event counts by outcome.

    Split by event because the two events under diagnosis have DIFFERENT
    budgets and different frequencies: UserPromptSubmit fires once per prompt
    with a 10s budget, PreToolUse fires on every tool call with a 2s one. A
    pooled number hides the exact asymmetry #489 is about.
    """
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        event = str(row.get("event") or "unknown")
        slot = out.setdefault(event, {})
        name = str(row.get("outcome") or "unknown")
        slot[name] = slot.get(name, 0) + 1
    return out


def _late_by_event(rows: "list[dict]") -> dict:
    """Per-event count of evaluations that finished after their caller left.

    Kept OUT of ``_timings_by_event`` deliberately: that map counts outcomes,
    and lateness is orthogonal to outcome — a row is both ``ok`` and late, and
    folding a second axis into the same dict would make the outcome counts stop
    summing to the sample count.
    """
    out: dict[str, int] = {}
    for row in rows:
        if row.get("late"):
            event = str(row.get("event") or "unknown")
            out[event] = out.get(event, 0) + 1
    return out


def summarize_timings(rows: "list[dict]") -> dict:
    """Aggregate the ring into the decision the code already states.

    THE RULE (see _process below, and #489): queue-dominant means the fix is
    CONCURRENCY; eval-dominant means the fix is the slow stage. So the summary
    reports both splits and then NAMES which one dominates, rather than leaving
    a reader to eyeball 256 rows and guess again.

    Pure function over a list of dicts — no broker, no IO, no lock. Aggregate
    counts by outcome lead, because "how often" is the question a percentile
    cannot answer: ONE 10s queue wait is noise, 40% of prompts dropped after a
    queue wait is the defect.
    """
    rows = [r for r in rows if isinstance(r, dict)]
    by_outcome: dict[str, int] = {name: 0 for name in _OUTCOMES}
    for row in rows:
        name = str(row.get("outcome") or "unknown")
        by_outcome[name] = by_outcome.get(name, 0) + 1

    def _split(subset: "list[dict]") -> dict:
        queue = sorted(float(r.get("queue_ms") or 0.0) for r in subset)
        evals = sorted(float(r.get("eval_ms") or 0.0) for r in subset)
        return {
            "samples": len(subset),
            "queue_ms": {
                "p50": _percentile(queue, 0.50),
                "p95": _percentile(queue, 0.95),
                "max": round(queue[-1], 3) if queue else 0.0,
                "total": round(sum(queue), 3),
            },
            "eval_ms": {
                "p50": _percentile(evals, 0.50),
                "p95": _percentile(evals, 0.95),
                "max": round(evals[-1], 3) if evals else 0.0,
                "total": round(sum(evals), 3),
            },
        }

    # `material` = everything that reached the queue, for the raw `overall`
    # split an operator reads during an incident. dropped_before_queue rows
    # carry 0/0 by construction (nothing had happened yet) and would dilute both
    # totals toward zero.
    material = [r for r in rows if str(r.get("outcome")) != "dropped_before_queue"]
    # THE VERDICT'S BASIS IS NARROWER (#489, 2026-07-30). Only rows where BOTH
    # phases were actually observed may vote on WHICH phase is the cost. A
    # dropped_after_queue row reports a real queue_ms and eval_ms = 0 BY
    # CONSTRUCTION — its evaluation never started — so it can only ever push the
    # share toward "queue". Including it made the instrument self-confirming: the
    # further the broker fell behind on COMPUTE, the more victims it queued, and
    # the more confidently the summary blamed the QUEUE.
    #
    # MEASURED LIVE (256-sample ring, pid 13524, 2026-07-30): queue_share 0.7003
    # / "queue_dominant" out of 186 drop rows, while the 189 completed rows read
    # PreToolUse queue p50 0ms, eval p50 7914ms — the true reading was
    # eval_dominant, and the real cost was a full-table scan of execution_events.
    # The drops are still reported in full via by_outcome/by_event; they are the
    # loudest fact in the ring. They just no longer name the culprit.
    voting = [r for r in material if str(r.get("outcome")) == "ok"]
    queue_total = sum(float(r.get("queue_ms") or 0.0) for r in voting)
    eval_total = sum(float(r.get("eval_ms") or 0.0) for r in voting)
    denominator = queue_total + eval_total
    if not voting or denominator <= 0.0:
        # A ring of nothing but drops has no opinion on queue-vs-compute. Saying
        # so is the honest answer; the drop counts above are the actionable fact.
        verdict = "no_data"
        queue_share = None
    else:
        queue_share = round(queue_total / denominator, 4)
        # 0.5 is the only defensible threshold: it is literally "more of the
        # wall clock went to waiting than to working".
        verdict = "queue_dominant" if queue_share >= 0.5 else "eval_dominant"
    return {
        "capacity": TIMINGS_CAPACITY,
        "rows": len(rows),
        "by_outcome": by_outcome,
        "by_event": _timings_by_event(rows),
        # #489 THE SECOND AXIS. An evaluation can succeed and still be useless:
        # it finished, but only after its caller had given up and fallen back.
        # Counted separately from the outcome map because it is a BUDGET
        # question, not a queue question — a fully eval-dominant, zero-queue
        # broker with a high late count is telling you the budget is wrong,
        # which is the one reading the earlier passes could not take.
        "late": sum(1 for r in rows if r.get("late")),
        "late_by_event": _late_by_event(rows),
        "overall": _split(material),
        "queue_share": queue_share,
        "verdict": verdict,
    }


# ── #489 THE FIX: per-session concurrency, not one global lock ───────────────
#
# WHAT THE OLD GLOBAL LOCK COST, MEASURED (scratch/measure_ups_queue.py — real
# broker, real loopback socket, real client, 5 concurrent actors):
#
#     per-eval compute   queue_ms p50   eval_ms p50   queue_share
#            50 ms          183.0          50.5          0.782
#           200 ms          799.8         200.3          0.793
#           400 ms         1590.5         400.4          0.794
#
# 78-83% of the wall clock went to WAITING, at every compute cost tested. In
# steady state queue_ms ~= (in-flight actors - 1) x eval_ms, which is why the
# defect appeared with FAN-OUT and correlates with actor count rather than with
# any slow stage — and why speeding a stage up buys nothing: it shrinks
# queue_ms in the same proportion, leaving the lost SHARE of the budget
# unchanged. That is the quantitative reason the earlier pass's ~8ms and ~16ms
# wins returned nothing. The rule stated at the drop site below therefore
# selects concurrency and rules out more caching.
#
# The damage was BIDIRECTIONAL and neither direction looked like a slow stage:
# one 4s UPS evaluation holding the global lock dropped 9 of 70 PreToolUse
# events past their 2s budget, and each of those clients then fell back to a
# fresh cold interpreter — exactly the cost this broker exists to remove.
#
# WHAT THE LOCK ACTUALLY PROTECTED (verified against the code, not assumed):
#   * SEC-001 privilege snapshot / SEC-002 atomic mutation / rollback. THE one
#     genuine partial-state window: during it grants are applied that a
#     route-validate block will roll back, so an evaluation observing it could
#     act on a grant that never existed. Keyed (project_root, session_id) via
#     snapshot_privilege_state(...) — it PARTITIONS by session.
#   * Causal turn mint, rotate_current_turn_id(project_root, session_id): same
#     key, same partition.
#   * One-shot grant CONSUMPTION — the double-consumption hazard that would
#     forbid concurrent readers. Verified UserPromptSubmit-side ONLY:
#     consume_sticky_grant_answers and apply_per_turn_intent_state are both UPS
#     stages in the pinned GOLDEN_UPS_TRACE
#     (tests/host/test_ups_golden_trace.py:35). Enforcement hooks READ grants;
#     they never consume them. So consumption belongs to the writer.
#   * sqlite: already prepared for this and says so at
#     _sqlite_index_store_base.py:12-17 ("connections are bound to the thread
#     that created them ... and the hook broker threads per connection"). Pool
#     is threading.local, WAL on, busy_timeout=2000, synchronous=NORMAL.
#   * The two request-scoped caches are thread-safe BY CONSTRUCTION:
#     managed_mode_service._request_mode_memo is a ContextVar;
#     intent_tokens_store._request_state is threading.local with its own
#     per-thread connection.
#   * Remaining module-global memos are input-keyed read caches — a concurrent
#     miss costs duplicated compute, never a wrong answer.
#
# SO THE SHAPE IS A PER-SESSION READERS-WRITER GATE:
#   * UserPromptSubmit — and any event NOT explicitly known to be read-only —
#     is a WRITER, exclusive for its session key. Nothing observes its
#     snapshot/rollback window. Correctness beats latency: a gate that races is
#     worse than a gate that queues.
#   * PreToolUse / PostToolUse are READERS, concurrent with each other. These
#     per-tool-call events ARE the queue (in every measured run the entire
#     backlog was PreToolUse), and they are precisely what a per-session-only
#     partition would NOT have relieved: a host assigns one session id per CLI
#     session and sub-agents inherit it, so the operator's five sub-agents share
#     one key. The reader/writer split is the fix, not an optimisation on top of
#     one.
#   * WRITER-PREFERRING, so a continuous enforcement-hook stream can never
#     starve the operator's prompt. That is the failure being fixed.
#
# WHAT REMAINS SERIALIZED: everything within one session key, plus an overall
# concurrency ceiling. Nothing is globally serialized any more.
MAX_CONCURRENT_EVALS = 8

# Ceiling on the key registry so a long-lived watchdog cannot accumulate one
# lock per session forever. Only IDLE keys are evicted (refs == 0) — evicting a
# key someone holds would hand the next arrival a DIFFERENT lock for the same
# session, which is exactly the race this gate exists to prevent.
_KEY_REGISTRY_MAX = 512

# Read-only by evidence, not by optimism. FAIL-CLOSED default: an event not
# named here is treated as a WRITER and gets the exclusive path, so a newly
# added mutating event is safe before anyone remembers to classify it.
SHARED_EVENTS = frozenset({"PreToolUse", "PostToolUse"})

UNBOUND_SESSION_KEY = "__unbound__"


def eval_key(project_root: object, session_id: object) -> str:
    """The partition key: one session of one project.

    An empty session id collapses to ``__unbound__`` DELIBERATELY rather than
    getting a key of its own. Unmanaged-project DNT grants land under a literal
    ``'__unbound__'`` bucket (hook_pipeline._ups_unbound_dnt_stage), so those
    evaluations DO share state and must therefore share a lock. Partitioning
    state that is not partitioned is how a concurrency change becomes a
    security bug.
    """
    root = str(project_root or "").strip().rstrip("\\/").lower()
    session = str(session_id or "").strip() or UNBOUND_SESSION_KEY
    return f"{root}\x00{session}"


def is_exclusive_event(event_name: object) -> bool:
    """True when this event must hold its session key EXCLUSIVELY.

    Unknown/missing event names are exclusive. Fail toward correctness: an
    unclassified event that mutates and ran shared would be a racing gate,
    while one that only reads and ran exclusive is merely slower.
    """
    return str(event_name or "").strip() not in SHARED_EVENTS


class _KeyState:
    __slots__ = ("cond", "readers", "refs", "writer", "writers_waiting")

    def __init__(self, guard: "threading.Lock") -> None:
        self.cond = threading.Condition(guard)
        self.readers = 0
        self.writer = False
        self.writers_waiting = 0
        # Held-or-waiting count, for eviction only: refs > 0 means in use.
        self.refs = 0


class EvalGate:
    """Per-key readers-writer gate with writer preference.

    Replaces ``threading.Lock()``. The property it PRESERVES: two evaluations
    that share mutable state never overlap. The property it ADDS: two that do
    not share state no longer wait for each other.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_EVALS) -> None:
        self._guard = threading.Lock()
        self._keys: dict[str, _KeyState] = {}
        # A ceiling on simultaneous evaluations. NOT a correctness device — a
        # resource one: unbounded parallel NLP pipelines would thrash a box into
        # being slower than the serial version, which would read as the fix
        # failing. Generous relative to the 5-6 actors actually observed.
        self._slots = threading.BoundedSemaphore(max_concurrent)

    def active_keys(self) -> int:
        """Live key count. Diagnostics and tests only — never a control path."""
        with self._guard:
            return len(self._keys)

    def _state(self, key: str) -> _KeyState:
        state = self._keys.get(key)
        if state is None:
            state = _KeyState(self._guard)
            self._keys[key] = state
        return state

    def _evict_idle(self) -> None:
        """Caller holds ``self._guard``. Drops unused keys only."""
        if len(self._keys) <= _KEY_REGISTRY_MAX:
            return
        for key in [k for k, s in self._keys.items() if s.refs == 0]:
            del self._keys[key]
            if len(self._keys) <= _KEY_REGISTRY_MAX:
                return

    def _acquire(self, key: str, *, exclusive: bool) -> _KeyState:
        with self._guard:
            state = self._state(key)
            state.refs += 1
            if exclusive:
                state.writers_waiting += 1
                while state.writer or state.readers:
                    state.cond.wait()
                state.writers_waiting -= 1
                state.writer = True
            else:
                # Yield to a waiting writer. THIS is what stops a continuous
                # PreToolUse stream from starving the operator's prompt.
                while state.writer or state.writers_waiting:
                    state.cond.wait()
                state.readers += 1
            return state

    def _release(self, key: str, state: _KeyState, *, exclusive: bool) -> None:
        with self._guard:
            if exclusive:
                state.writer = False
            else:
                state.readers -= 1
            state.refs -= 1
            state.cond.notify_all()
            self._evict_idle()

    @contextlib.contextmanager
    def hold(self, key: str, *, exclusive: bool):
        """Hold the gate for ``key``: exclusive = writer, otherwise shared.

        Gate FIRST, then the concurrency slot. The other order would let N
        readers queued behind one writer occupy every slot and block unrelated
        keys — reintroducing a global bottleneck through the back door.
        """
        state = self._acquire(key, exclusive=exclusive)
        try:
            self._slots.acquire()
            try:
                yield
            finally:
                self._slots.release()
        finally:
            self._release(key, state, exclusive=exclusive)


def broker_state_path(state_dir: Path | None = None) -> Path:
    """Discovery file location — lives next to the watchdog's health.json."""
    if state_dir is None:
        from .aidocs_service import daemon_dir  # lazy: avoid import cycle

        state_dir = daemon_dir()
    return Path(state_dir) / STATE_FILENAME


# ── #502: registration custody ──────────────────────────────────────────
#
# The registration file is how the hook FINDS the broker, and it names the
# port AND the bearer. It was consumed on trust, so whoever could WRITE it
# could (a) point every hook at a listener of their choosing — which reads
# the operator's prompt before the agent does — and (b) answer with
# ``response: None``, a real verdict meaning "proceed", turning a DENY into
# an allow. The client's only defences were ECHO checks (does the reply
# name the session/root we asked about?), which a rogue satisfies for free.
#
# So custody of the registration must be PROVEN before the payload leaves
# the process. The proof is OS-level and needs no new secret store: the
# registration and the directory holding it must not be a symlink/reparse
# point, and must not be writable by any principal other than their owner.
#
# HONEST LIMIT, stated so nobody mistakes this for more than it is: a
# process running as the SAME user as the daemon can still rewrite the
# registration, because it can equally read the daemon's memory. Closing
# that needs a secret store this project does not have. What this closes is
# every OTHER writer — a second local account, an over-permissive inherited
# ACL (measured live on the reference host), a junction/symlink redirect —
# and it converts an unverifiable rendezvous from a silent redirect into a
# loud, audited refusal.
#
# #332: this costs one lstat pair (POSIX) or one DACL read pair (Windows)
# per hook, on a path that already does a file read plus a socket
# round-trip. No DB, no spawn, no gate-stack import.


def registration_custody_ok(path) -> tuple[bool, str]:
    """Prove the registration cannot have been written by a foreign principal.

    Returns ``(True, detail)`` only when the registration and its directory
    both exist, are not redirects, and grant write to nobody but their
    owner. Fail closed: any error is a refusal that NAMES what failed, so
    the client can surface a remedy instead of a shrug.
    """
    path = Path(path)
    entries = [path.parent, path]
    if os.name != "nt":
        import stat as _stat

        euid = os.geteuid()
        for entry in entries:
            try:
                st = os.lstat(entry)
            except OSError as exc:
                return (False, f"{entry.name}: cannot stat ({type(exc).__name__})")
            if _stat.S_ISLNK(st.st_mode):
                return (False, f"{entry.name}: is a symlink")
            if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
                return (False, f"{entry.name}: group/other-writable")
            if st.st_uid != euid:
                return (False, f"{entry.name}: owned by uid {st.st_uid}, not us")
        return (True, "registration owned by this user, not group/other-writable")

    from . import governed_shell_broker_win as _win

    for entry in entries:
        if not entry.exists():
            return (False, f"{entry.name}: missing")
        if _win.is_reparse_point(str(entry)):
            return (False, f"{entry.name}: is a reparse point (redirect)")
        if not _win.acl_state_authority_ok(str(entry)):
            return (False, f"{entry.name}: writable by a non-owner principal")
    return (True, "registration DACL binds write to the owner, no reparse redirect")


def harden_registration_custody(path) -> None:
    """Make the registration and its directory owner-writable ONLY.

    Best-effort by design and paired with :func:`registration_custody_ok`:
    the broker hardens what it writes, the client refuses what it cannot
    verify. A host whose tree cannot be hardened therefore loses the warm
    path (falling back to local evaluation, which is slower but fully
    governed) rather than silently trusting an open rendezvous.
    """
    path = Path(path)
    if os.name != "nt":
        for entry, mode in ((path.parent, 0o700), (path, 0o600)):
            with contextlib.suppress(OSError):
                entry.chmod(mode)
        return
    from . import governed_shell_broker_win as _win

    _win.harden_path_dacl(str(path.parent), is_dir=True)
    if path.exists():
        _win.harden_path_dacl(str(path), is_dir=False)


def evaluate_hook_event(payload: dict, *, handler_factory=None):
    """In-process hook evaluation — THE core, not a copy of it.

    Wraps ``claude_hook.ClaudeHookHandler.handle`` (the same object the
    cold-start subprocess drives from stdin). A fresh handler per call
    mirrors fresh-subprocess semantics; ``handler_factory`` is the test
    seam. Returns the hook's JSON dict, or None when the hook has no
    output (Claude Code proceeds).
    """
    if handler_factory is not None:
        handler = handler_factory()
    else:
        from .claude_hook import ClaudeHookHandler  # lazy: heavy import

        handler = ClaudeHookHandler()
    return handler.handle(payload)


#: #1030 sentinel for "the generation baseline was never captured". None is a
#: REAL answer here — a legacy install with no activation pointer — so "never
#: captured" must not be able to compare equal to it.
_GENERATION_UNSET = object()


def _generation_now() -> str | None:
    """The activation pointer's generation id right now, or None.

    This is the "what SHOULD serve" side of the comparison, re-read per probe
    on purpose. None means "no generation is activated" — a legacy single-tree
    install, a legitimate and stable state, NOT an error. Anything unreadable
    also answers None: this is an infrastructure staleness probe, and one that
    raised would take the broker down over a transient file read.
    """
    try:
        from aidocs_mcp import runtime_generations

        gid, _reason = runtime_generations.read_pointer(Path.home())
        return gid or None
    except Exception:
        return None


def _generation_loaded(code_root: str | Path | None = None) -> str | None:
    """The "what IS serving" side: which generation THESE BYTES came from.

    THE BASELINE MUST NOT BE A POINTER READ. Taking it from `_generation_now()`
    at construction looked equivalent and is not: the pointer can already have
    moved by the time this process gets to __init__ — a broker spawned during a
    flip, or simply started a moment after one. Such a broker would baseline
    itself as B while actually running A, then compare B to B forever and
    report healthy while serving the old runtime. The staleness check would be
    structurally incapable of firing in exactly the window it exists for.

    The import path cannot lose that race, so the baseline is read off it.
    """
    try:
        from aidocs_mcp import runtime_generations

        return runtime_generations.loaded_generation(
            Path(code_root) / "__init__.py" if code_root else __file__
        )
    except Exception:
        return None


class HookBroker:
    """Loopback-only, token-gated, one-JSON-line-per-direction listener."""

    # #609 class defaults so the identity gate is well-defined even for a
    # broker built with ``__new__`` (several timing tests do exactly that to
    # drive ``_process`` without a socket). None = "not established yet", which
    # ``_staleness_reason`` resolves on first use — never "matches".
    _code_root: str | Path | None = None
    _code_identity: str | None = None
    _stale_announced: str | None = None
    # #727 (B). Provenance is computed LAZILY and once: it is a property of the
    # tree, not of the event, so charging the per-event path for it would be a
    # second defect. `_provenance_checked` (not a None sentinel) keeps "checked,
    # and the answer is None/clean" distinct from "never checked".
    _provenance_checked: bool = False
    _provenance: str | None = None
    # #1030: WHICH GENERATION this process is serving, captured at construction.
    _generation: object = _GENERATION_UNSET

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        state_dir: Path | None = None,
        handler_factory=None,
        code_root: str | Path | None = None,
    ) -> None:
        # Loopback by CONSTRUCTION — not by configuration. Pinned by test.
        if host != "127.0.0.1":
            raise ValueError(
                f"HookBroker binds loopback only; refusing host={host!r}"
            )
        self._host = host
        self._port = port
        self._state_dir = state_dir
        self._handler_factory = handler_factory
        # #609: WHICH code this process is running, captured at construction.
        # Bound to the tree this module was actually IMPORTED from (not to a
        # configured path) so the check stays self-consistent on a box whose
        # hooks and daemon load from different artefacts (#627) — each process
        # is asked only whether IT still matches ITS OWN source.
        self._code_root = Path(code_root) if code_root is not None else Path(__file__).resolve().parent
        self._code_identity = package_code_identity(self._code_root)
        # #1030: and WHICH GENERATION those bytes came from — read off the
        # import path, never off the pointer. See _generation_loaded.
        self._generation = _generation_loaded(self._code_root)
        self._stale_announced: str | None = None
        self._token = secrets.token_hex(16)
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._closing = threading.Event()
        # #489: WAS a single global threading.Lock, on the stated grounds that
        # "the subprocess model never ran two evaluations concurrently in one
        # process, and the underlying stores assume as much". Measured, that
        # cost 78-83% of every client's budget in pure queue wait, and the
        # premise turned out to be stale: the stores are thread-local by
        # construction and _sqlite_index_store_base.py:12-17 already names this
        # broker's per-connection threading as the reason. Now a per-session
        # readers-writer gate — see the EvalGate block above for what the lock
        # protected and how it partitions. Correctness over parallelism still
        # holds; it simply no longer requires ONE lock for the whole machine.
        self._eval_gate = EvalGate()
        # #489: split timings kept here, not only echoed to a caller that may
        # already have given up. See the timing-ring block above.
        _reset_timings(self)
        self.address: tuple[str, int] | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> HookBroker:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((self._host, self._port))
        sock.listen(16)
        self._sock = sock
        self.address = sock.getsockname()[:2]
        self._write_state_file()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="aidocs-hook-broker", daemon=True
        )
        self._accept_thread.start()
        return self

    # NOTE: `timings()` / `timings_summary()` method wrappers were DELETED
    # 2026-07-28. They were thin duplicates of the module-level
    # `recent_timings()` / `summarize_timings()`, and nothing ever called them —
    # the TIMINGS_KIND protocol handler and the tests both use the module
    # functions directly. The deploy gate's vulture stage caught them as dead
    # code and hard-failed, which is the correct verdict: a second accessor with
    # no caller is not an API, it is drift. Read the ring via `recent_timings()`.

    def close(self) -> None:
        self._closing.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        # Remove the discovery file only if it is OURS (pid match) — a
        # newer broker's file must survive an old broker's shutdown.
        try:
            path = broker_state_path(self._state_dir)
            state = json.loads(path.read_text(encoding="utf-8"))
            if int(state.get("pid") or -1) == os.getpid() and (
                state.get("token") == self._token
            ):
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def _write_state_file(self) -> None:
        path = broker_state_path(self._state_dir)
        payload = {
            "v": PROTOCOL_VERSION,
            "port": self.address[1],
            "pid": os.getpid(),
            "token": self._token,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # #609: WHICH GENERATION IS LIVE. Thirteen deploys landed without
            # anyone being able to ask this, because the rendezvous published
            # only liveness. The value here is the identity captured at
            # CONSTRUCTION — what this process LOADED — so a reader can compare
            # it with package_code_identity() on disk and learn that the broker
            # predates the shipped code WITHOUT waiting for a hook event.
            # JSON null, never "", when the tree could not be read: a present
            # but empty provenance field reads as an answer (#627), and a reader
            # that sees null must say UNKNOWN, never "current".
            "code_identity": self._code_identity,
        }
        # #502: harden the DIRECTORY before anything lands in it, so the
        # registration is never briefly readable/writable under an inherited
        # ACL, and harden the file itself after. The client refuses what it
        # cannot verify — so a broker that does not do this loses the warm
        # path (#332) rather than being trusted anyway.
        harden_registration_custody(path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)  # best-effort on Windows
        except OSError:
            pass
        tmp.replace(path)
        harden_registration_custody(path)

    # ── serving ──────────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        while not self._closing.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _addr = sock.accept()
            except OSError:
                return  # socket closed → clean shutdown
            threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(_CONN_TIMEOUT_S)
                line = self._read_line(conn)
                reply = self._process(line)
            except Exception as exc:  # noqa: BLE001 — never crash the broker
                reply = {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            try:
                conn.sendall(json.dumps(reply).encode("utf-8") + b"\n")
            except OSError:
                pass

    @staticmethod
    def _read_line(conn: socket.socket) -> bytes:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
        return buf.split(b"\n", 1)[0]

    # ── #489 STEP 4: a deadline may cost PRESENTATION, never INTENT ───

    def _rescue_durable_intent(self, payload: dict) -> None:
        """On a deadline drop, persist the prompt and its intent anyway.

        THE DEFECT THIS CLOSES. The degraded banner tells the operator that
        "Full NLP, memory, doctrine, and palace surfacing were skipped"
        (hook_broker_client.py:376) — and that set silently included INTENT
        CAPTURE. Operator law is explicit: "the prompt carries user-intent ...
        user-intent is what makes AIDOCS work." So the old drop path spent the
        one signal the whole system is built on in order to save presentational
        enrichment. A slow palace query cost the operator's intent.

        WHERE THE INTENT WAS ACTUALLY LOST — worth stating precisely, because it
        is not where it looks. The durable stages already run FIRST inside
        _run_user_prompt_core (record_user_prompt_received is stage 1 of the
        pinned GOLDEN_UPS_TRACE), so the ORDER was never wrong. The bug was that
        both deadline checks above returned a bare _refuse BEFORE
        evaluate_hook_event was ever called, so the durable head never ran at
        all. Intent was lost because the pipeline was never entered, not because
        it was ordered late. Hence the fix belongs HERE, at the drop.

        THE SPLIT. Durable = recording the prompt and capturing its intent;
        those may not be skipped by a deadline. Presentational = memory,
        doctrine and palace surfacing; those may degrade freely. This runs only
        the durable side, so the cost is a couple of small writes rather than
        the multi-second pipeline the deadline was protecting against.

        UserPromptSubmit ONLY: it is the event that carries operator intent.
        Enforcement hooks have a local-evaluation fallback that enforces
        correctly anyway, and re-running them here would be the wasted work the
        deadline guard exists to prevent.

        FAIL-QUIET. This runs on a path that is already failing; the client has
        already given up and will render the degraded banner regardless. An
        exception here must never convert a degrade into a crash.
        """
        try:
            if str(payload.get("hook_event_name") or "").strip() != "UserPromptSubmit":
                return
            if not str(payload.get("prompt") or "").strip():
                return
            if self._handler_factory is not None:
                handler = self._handler_factory()
            else:
                from .claude_hook import ClaudeHookHandler  # lazy: heavy import

                handler = ClaudeHookHandler()
            capture = getattr(handler, "capture_durable_only", None)
            if capture is None:
                return
            capture(payload)
        except Exception:  # noqa: BLE001 — a rescue must never break the drop
            pass

    def _runs_the_imported_tree(self) -> bool:
        """True when this broker's code root IS the tree this module was
        imported from -- the only posture the #727 (B) provenance rule governs.

        Keyed on the FACT, not on how the object was constructed. An earlier
        draft set a flag in __init__ when `code_root` was passed, which missed
        every broker built via `__new__` with `_code_root` assigned directly
        (several timing tests do exactly that) and wrongly demanded a build
        stamp of their fixture trees. What matters is which tree is being
        judged, and that is answerable without knowing who set it.

        A caller pointing the broker at some OTHER tree is doing so
        deliberately; asking a fixture to prove it came from a packaging step
        would be a category error.
        """
        try:
            return Path(self._code_root).resolve() == Path(__file__).resolve().parent
        except Exception:
            return False

    def _staleness_reason(self) -> str | None:
        """None when this process can PROVE it runs the code on disk (#609).

        A resident evaluator that has been overtaken by a package swap is not a
        POLICY failure and must never be reported as one — it is degraded
        INFRASTRUCTURE. So the reply says the broker is stale and names the
        remedy; it never says the action was forbidden. Refusing costs the warm
        path (the caller evaluates locally, in a fresh subprocess that loaded
        the NEW code) and nothing else: enforcement continues at full strength,
        one interpreter spawn slower, which is exactly the trade #332 made in
        the other direction when the code was current.

        UNKNOWN counts as stale. A broker that cannot verify its own code
        cannot be trusted to PERMIT, but can always be trusted to refuse
        (#589's principle; sealed law promoted-06ad3c5f61ab).
        """
        # #727 (B). PROVENANCE FIRST, and computed once. A tree that was never
        # packaged is not made trustworthy by being internally consistent, and
        # the self-comparison below is exactly that: it would report a source
        # checkout healthy forever, because the checkout does match itself.
        # Cached because provenance is a property of the TREE, not of the event
        # -- re-deriving it per hook would charge the warm path #489 was spent
        # making fast.
        # SHIPPED first, then OWNERSHIP. Both answer one question -- is this
        # broker the pinned artefact -- so they share this slot, this cache and
        # this switch; splitting them would let an operator turn on half a
        # guarantee. The stamp leads because when it IS present it gives the
        # more specific diagnosis (which commit these bytes claim); ownership
        # is what still refuses when a checkout has BORROWED a stamp, which
        # #832 measured happening on this very box.
        if not self._provenance_checked:
            self._provenance = (
                (
                    artefact_provenance_reason(self._code_root)
                    or runtime_ownership_reason(self._code_root)
                )
                if self._runs_the_imported_tree()
                else None
            )
            self._provenance_checked = True
            # ANNOUNCED ONCE whether or not it is enforced. The defect #727 (B)
            # names is the SILENCE -- a broker enforcing an unbuilt tree while
            # reporting healthy. Saying so costs nothing and is the whole value;
            # refusing is a separate, heavier decision (below).
            if self._provenance:
                with contextlib.suppress(Exception):
                    sys.stderr.write(f"[aidocs hook broker] {self._provenance}\n")
                    sys.stderr.flush()
        # REPORTED BY DEFAULT, ENFORCED ONLY ON OPT-IN.
        #
        # The operator's ruling is that enforcement must not run from in-flight
        # source. Refusing here delivers exactly that -- and on a DEV box, where
        # the runtime is installed FROM source and therefore never carries a
        # build stamp, it also disables the warm path on every event forever.
        # That is a real cost (#489 was spent making that path fast) and the
        # trade between "slow and honest" and "fast and unproven" is the
        # operator's to make, not this function's.
        #
        # MEASURED CONSEQUENCE of getting this wrong, same day: with the stale
        # stamp removed from source (#832), enforcing-by-default turned 12
        # hook-broker tests red, because the test suite imports aidocs_mcp from
        # the checkout -- which is genuinely unshipped, and genuinely should not
        # be enforcing in production. Both facts are true at once, which is why
        # this is a ruling and not a bug.
        #
        # Until #832 settles what a local runtime should be installed FROM, the
        # verdict is announced and available; set AIDOCS_BROKER_REQUIRE_SHIPPED=1
        # to make it refuse.
        if self._provenance and _provenance_is_enforced():
            self._stale_announced = "provenance"
            return self._provenance

        # #1030. THE GENERATION AXIS, AND WHY THE CHECK BELOW CANNOT SEE IT.
        #
        # The content comparison that follows detects a swap by noticing the
        # bytes under `_code_root` CHANGED. That worked because the refresh
        # rewrote the serving tree in place. Immutable generations remove that
        # mutation by design: A's tree is never touched again, so its hash is
        # stable forever and this broker would keep serving A after the flip,
        # silently and indefinitely — the detector blinded by the very fix.
        #
        # So the pointer is a SECOND, independent axis. Content answers "were
        # my bytes replaced underneath me"; the generation answers "am I still
        # the runtime this machine has activated". Both must hold. Neither
        # subsumes the other: a legacy install still mutates in place (content
        # catches it, generation is None on both sides), and a generational
        # install never mutates (generation catches it, content cannot).
        #
        # STALE, NOT FORBIDDEN — the same posture as below. The caller falls
        # back to a fresh local evaluator, which the shim has already re-execed
        # into the NEW generation, so the event is governed by B while this
        # broker steps aside. One interpreter spawn slower, never ungoverned.
        now_gen = _generation_now()
        if self._generation is _GENERATION_UNSET:
            # Skipped __init__ (several timing tests build with __new__).
            # Recover the baseline the SAME way __init__ does — from the bytes
            # that were loaded. Adopting `now_gen` here would reintroduce the
            # pointer-as-baseline defect through the back door: such a broker
            # would call itself whatever the pointer says at its first probe,
            # which is precisely the value it is supposed to be checked against.
            self._generation = _generation_loaded(self._code_root)
        if now_gen != self._generation:
            reason = (
                "broker_generation_stale: this hook broker is serving runtime "
                f"generation {self._generation or 'legacy'} but the machine has "
                f"activated {now_gen or 'legacy'} (#1030). The refresh built a "
                "new generation beside this one and flipped the activation "
                "pointer — it never restarts the watchdog that hosts this "
                "broker. NOT a policy decision and NOT a gate that stopped: "
                "hook evaluation falls back to the local evaluator, which runs "
                "under the ACTIVE generation, so every event is still governed. "
                "To restore the warm path, restart the AIDOCS service."
            )
            marker = f"generation:{now_gen or 'legacy'}"
            if self._stale_announced != marker:
                self._stale_announced = marker
                with contextlib.suppress(Exception):
                    sys.stderr.write(f"[aidocs hook broker] {reason}\n")
                    sys.stderr.flush()
            return reason

        current = package_code_identity(self._code_root)
        if current is not None:
            if self._code_identity is None:
                # No baseline yet: either construction could not read the tree,
                # or this object skipped __init__. The baseline becomes the
                # EARLIEST identity this process can establish — an unknown is
                # never allowed to pass as a match, and once a baseline exists
                # every later change is a refusal.
                self._code_identity = current
            if current == self._code_identity:
                self._stale_announced = None
                return None
        reason = (
            "broker_code_stale: this hook broker is running code that is no "
            f"longer on disk at {self._code_root} (loaded="
            f"{(self._code_identity or 'unknown')[:12]} disk="
            f"{(current or 'unknown')[:12]}). The runtime package was replaced "
            "under the live watchdog — a runtime refresh restarts the daemon "
            "child, never the watchdog that hosts this broker (#609). NOT a "
            "policy decision and NOT a gate that stopped: hook evaluation "
            "falls back to the local evaluator, which loads the CURRENT code, "
            "so every event is still governed. To restore the warm path, "
            "restart the AIDOCS service so the watchdog re-imports."
        )
        if self._stale_announced != (current or "unknown"):
            self._stale_announced = current or "unknown"
            with contextlib.suppress(Exception):
                sys.stderr.write(f"[aidocs hook broker] {reason}\n")
                sys.stderr.flush()
        return reason

    def _process(self, line: bytes) -> dict:
        def _refuse(reason: str) -> dict:
            return {"v": PROTOCOL_VERSION, "ok": False, "error": reason}

        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _refuse("malformed request (expected one JSON line)")
        if not isinstance(request, dict):
            return _refuse("malformed request (expected a JSON object)")
        if request.get("v") != PROTOCOL_VERSION:
            return _refuse(f"unsupported protocol version {request.get('v')!r}")
        kind = request.get("kind")
        if kind not in ("hook_eval", TIMINGS_KIND):
            return _refuse(f"unknown kind {kind!r}")
        # Token gate BEFORE any evaluation — compare_digest against replay
        # of the same-user shared secret from the state file.
        token = str(request.get("token") or "")
        if not secrets.compare_digest(token, self._token):
            return _refuse("bad token")
        if kind == TIMINGS_KIND:
            # THE READER (#489). Deliberately answered here, ABOVE the
            # evaluation gate and outside every lock: reading the instrument
            # must never queue behind the thing it is measuring, or the reader
            # becomes another source of the contention it exists to report.
            # Also the reason the ring is a plain deque append — an observer
            # that needs a lock is an observer that changes the measurement.
            rows = recent_timings(self)
            return {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "summary": summarize_timings(rows),
                # Raw rows too, bounded by TIMINGS_CAPACITY. The aggregate
                # answers "which fix"; the rows are what lets a reader
                # disbelieve the aggregate, which is the whole point of
                # publishing evidence instead of a conclusion.
                "timings": rows,
            }
        payload = request.get("payload")
        if not isinstance(payload, dict):
            return _refuse("malformed payload (expected a JSON object)")

        # ── #609 code-identity gate ──────────────────────────────────────
        # BEFORE any evaluation: a verdict computed from replaced code is the
        # old law deciding, and discarding it afterwards would still have let
        # it decide the latency. Deliberately BELOW the TIMINGS branch — the
        # instrument must stay readable on a stale broker, since "why did the
        # warm path stop" is exactly the question it is there to answer.
        _stale = self._staleness_reason()
        if _stale is not None:
            _record_timing(
                self,
                event=str(payload.get("hook_event_name") or ""),
                queue_ms=0.0,
                eval_ms=0.0,
                outcome="refused_stale_code",
            )
            return _refuse(_stale)

        # ── #489 deadline guard + split timings ──────────────────────────
        # Evaluations are SERIALIZED on _eval_lock (see __init__), so under real
        # hook pressure requests QUEUE here. That is the mechanism behind the
        # operator-visible `timed_out` even when every individual evaluation is
        # fast: a prompt can sit behind several tool-call hooks and blow the
        # client's budget without anything being slow.
        #
        # Two checks, deliberately: before the queue (cheap reject) and again
        # AFTER acquiring the lock, because the wait itself is what kills the
        # deadline. Running the full NLP/memory/doctrine/palace pipeline for a
        # caller that stopped listening burns the lock the next live request is
        # waiting on — the queue's own cause.
        _sent_at = request.get("sent_at_ms")
        _budget_s = request.get("client_budget_s")
        _event = str(payload.get("hook_event_name") or "")
        _exclusive = is_exclusive_event(_event)
        _key = eval_key(request.get("project_root"), payload.get("session_id"))
        if _hook_budget.client_budget_exhausted(_sent_at, _budget_s):
            # Recorded even here: a drop is EVIDENCE, and this is the outcome the
            # operator sees as `timed_out`. Dropping it silently is what made the
            # queue-vs-compute question unanswerable.
            _record_timing(
                self, event=_event, queue_ms=0.0, eval_ms=0.0, outcome="dropped_before_queue"
            )
            self._rescue_durable_intent(payload)
            return _refuse("client_deadline_passed (dropped before evaluation)")

        t_recv = time.perf_counter()
        with self._eval_gate.hold(_key, exclusive=_exclusive):
            t_start = time.perf_counter()
            if _hook_budget.client_budget_exhausted(_sent_at, _budget_s):
                # THE most diagnostic row in the system: the client's whole budget
                # went to WAITING behind other serialized evaluations, with this
                # request's own compute never starting. If these dominate the
                # ring, the fix is concurrency — never more caching (#489).
                _record_timing(
                    self,
                    event=_event,
                    queue_ms=(t_start - t_recv) * 1000.0,
                    eval_ms=0.0,
                    outcome="dropped_after_queue",
                )
                self._rescue_durable_intent(payload)
                return _refuse("client_deadline_passed (dropped after queue wait)")
            response = evaluate_hook_event(
                payload, handler_factory=self._handler_factory
            )
            t_end = time.perf_counter()
        queue_ms = (t_start - t_recv) * 1000.0
        eval_ms = (t_end - t_start) * 1000.0
        # #489 LATENESS, checked once here and nowhere else. The two guards
        # above catch a caller who left BEFORE the work; this catches the one
        # who left DURING it — an evaluation that completed in full and was
        # then delivered to a client already running its local fallback. It
        # cannot be recovered from the numbers alone, because eval_ms is only
        # comparable to a budget the row does not otherwise carry.
        _record_timing(
            self,
            event=_event,
            queue_ms=queue_ms,
            eval_ms=eval_ms,
            outcome="ok",
            late=_hook_budget.client_budget_exhausted(_sent_at, _budget_s),
        )
        return {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "response": response,
            # Echoes: the client only trusts a reply for the exact
            # session/root it asked about (floor: no cross-session reuse).
            "session_id": str(payload.get("session_id") or ""),
            "project_root": str(request.get("project_root") or ""),
            # eval_ms stays the COMPUTE cost (it used to include the lock wait,
            # which made a queued request look like a slow one). queue_ms is the
            # wait — the two must be separable to diagnose #489 at all.
            "eval_ms": eval_ms,
            "queue_ms": queue_ms,
        }
