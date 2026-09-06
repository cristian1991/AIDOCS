"""AIDOCS-owned daemon supervisor (#249) — autorestart for the local HTTP server.

Claude Code auto-reconnects HTTP MCP servers but NEVER restarts a crashed one
(and never reconnects stdio at all) — so AIDOCS supplies its own supervision:

    watchdog (this module)  ->  daemon (python -m aidocs_mcp.mcp_server --http)
        restart on crash w/ capped backoff
        crash-loop circuit breaker (no restart storms on a broken build)
        release-marker watch -> ZERO-DOWNTIME overlap restart on deploy: the
                                watchdog owns 127.0.0.1:<port> via a tiny
                                loopback proxy; daemons bind ephemeral backend
                                ports behind it. New daemon up + ready FIRST,
                                proxy flips atomically, THEN the old drains —
                                the listener never closes, editors keep tools.
        health file the hooks read to nudge the operator when everything is down

Pure stdlib, cross-platform. State lives in ~/.aidocs/daemon/:
    health.json   — {port, pid, watchdog_pid, status, started_at, heartbeat_at,
                     stale_after_s, last_exit, release_marker}. A HEARTBEAT
                     (#591): rewritten every supervision poll by the live
                     watchdog, and STALE — neither up nor down — to any reader
                     once heartbeat_at is older than stale_after_s.
    daemon.out    — the child's own stdout+stderr; its tail is quoted into the
                     watchdog log on EVERY daemon exit, rc=0 included.
    watchdog.log  — small rotating log (2 files)
    stop.flag     — presence asks the watchdog to exit cleanly
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_logger = logging.getLogger(__name__)

# #431: benign client-disconnect family. A peer dropping mid-pipe (deploy
# hot-swap restarting editors' connections, curl --max-time smoke probes,
# editor window closes) is NORMAL connection lifecycle on this loopback
# proxy — WinError 10054 / ECONNRESET / broken pipe. Caught NARROWLY at
# the pipe seams and logged as one terse debug line; every other
# exception keeps its full traceback (never blanket-swallow).
_BENIGN_DISCONNECTS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

# Backoff: 1s -> 2s -> 4s ... capped. Circuit breaker: too many crashes in the
# window means the build is broken — stop restarting and say so.
_BACKOFF_START_S = 1.0
_BACKOFF_CAP_S = 60.0
_BREAKER_CRASHES = 5
_BREAKER_WINDOW_S = 300.0
_MARKER_POLL_S = 5.0
# Overlap restart (#249b): how long a freshly spawned daemon gets to bind its
# backend port before we give up and keep the old one serving.
_READY_TIMEOUT_S = 60.0
_READY_POLL_S = 0.25
# Connection-aware overlap drain (#432, 2026-07-17): on a code hot-swap the proxy
# flips NEW connections to the new backend, but an operator's LIVE MCP stream
# stays piped to the OLD backend. Terminating it immediately (the old behavior)
# severs that stream — MCP is a persistent connection, not a stateless poll — so
# every deploy dropped the operator's session ("MCP disconnected" → /mcp reconnect,
# despite the "no reconnect" claim). So we DEFER killing an old backend until its
# live connection count drains to zero (natural session end), capped so a leaked
# connection can never pin an old backend forever.
_OVERLAP_DRAIN_MAX_S = 1800.0

# ── A FAILED HOT-SWAP MUST RETRY ITSELF (#903, 2026-08-24) ───────────────────
# The readiness-failure branch below says "Next marker change retries" — and the
# marker had ALREADY been advanced before the attempt, so `current != marker`
# was false from then on and the retry it promises could never happen. One failed
# swap left the daemon on old code PERMANENTLY.
#
# That is not theoretical. Its own comment records three consecutive silent
# failures across two days ("the operator ran old code for two days"), and it is
# the state measured on the operator's box on 2026-08-24: build 188 installed at
# 13:16Z, daemon still serving the process it booted at 00:46Z, ai_version
# reporting running.known=false. The cause was upstream (package-trust drift made
# the new daemon fail readiness) but the reason it PERSISTED for twelve hours is
# here: nothing ever tried again.
#
# ALIVE-BUT-STALE IS THE FAILURE MODE, not "process killed". The old daemon keeps
# serving, so every health check is green and nothing looks broken — which is why
# it went unnoticed for a day. Backed off exponentially and capped, because the
# opposite failure (a daemon that cannot start, retried hard) is a restart storm.
#
# THE FIRST RETRY IS SHORT ON PURPOSE (#903, retuned 2026-08-25 from a measured
# update). The commonest cause of a failed swap is not a broken build at all — it
# is a RACE that cannot be closed: the release marker is this package's own
# __init__.py mtime, which pip changes DURING the install, while the trust row is
# written by the installing process AFTERWARDS. So the watchdog can always see
# "new code" before "new trust", spawn into it, and be refused by the integrity
# check with "package drift ... re-record via `aidocs runtime --record-package`".
# Measured on the 188->195 update: marker changed at 04:01:27, readiness failed
# rc=3 at 04:01:30, and the state was settled within seconds.
#
# Sixty seconds of KNOWN staleness to recover from a few seconds of unavoidable
# skew is the wrong trade. Five seconds covers the race; a genuinely broken build
# still doubles away to the cap, so storm protection is unchanged in the case it
# was written for.
_SWAP_RETRY_START_S = 5.0
_SWAP_RETRY_MAX_S = 900.0

# ── hook-broker host child (#609 lifecycle) ──────────────────────────────────
# The broker used to be built in THIS process, once, before the supervise loop,
# so a deploy — which replaces only the daemon backend child — never re-imported
# it. Thirteen overlap-restarts against one "hook broker up" on the reference
# host. Detection (#609 pass 1/2) made the resulting staleness honest (the
# broker refuses and clients fall back to a fresh local interpreter) but nothing
# could make it FRESH again short of a human restart.
#
# Freshness is only provable BY CONSTRUCTION: a new interpreter that imported
# the shipped tree. So the broker is hosted in a supervised child and the deploy
# edge spawns a replacement. How long that child gets to publish its rendezvous
# before we conclude it never came up:
# Measured on the reference host: a broker host child binds and publishes in
# ~0.25s. 10s is 40x headroom and is also the WORST CASE the supervisor will
# wait before getting on with the daemon — a broker that cannot come up must
# never delay the thing it is optional to.
_BROKER_PUBLISH_TIMEOUT_S = 10.0
_BROKER_PUBLISH_POLL_S = 0.25
# How long the SUPERSEDED broker keeps running after the new one has taken over
# the rendezvous. No new client can reach it (the rendezvous names the new pid),
# so this window exists solely for evaluations already mid-decision: their code
# cannot be swapped underneath them — a process's modules are immutable for its
# lifetime — but severing the socket would strand the caller, so we let them
# finish instead. Well beyond the client's own budget and the broker's
# _CONN_TIMEOUT_S.
_BROKER_DRAIN_S = 120.0

# ── liveness (#591) ──────────────────────────────────────────────────────────
# health.json is a HEARTBEAT, not a footprint. The live watchdog rewrites it on
# every supervision poll with a REAL pid; readers treat it as STALE — neither up
# nor down — once older than a small multiple of that interval. "Unknown is not
# a pass" applies to liveness: a health file that has stopped being written says
# nothing about the daemon, and must not be read as either answer.
_HEALTH_HEARTBEAT_S = _MARKER_POLL_S
# How often the watchdog checks whether the hook broker's loaded code still
# matches the tree on disk (#609 self-heal). Cheap: one directory fingerprint,
# off the per-event path entirely, and it only ever acts on the deploy edge.
# 30s is chosen against the WORST case it has to cover — an operator editing the
# runtime package in a live session — where the alternative was a permanently
# degraded warm path until someone restarted the service by hand.
_BROKER_CODE_POLL_S = 30.0
_HEALTH_STALE_AFTER_S = 60.0
# Minimum-uptime rule (#591): a child that exits within this window of its spawn
# is a FAILED START, not a run to retry instantly — whatever its return code.
# rc=0 does not mean "worked"; the daemon is supposed to serve forever, so ANY
# self-exit is unexpected and a fast one means the start itself never took.
_MIN_UPTIME_S = 10.0
_FAILED_START_LIMIT = 5
# How much of the child's own log to quote when it exits, so the watchdog log
# can always answer "why did it stop?" — including on a clean rc=0 exit.
_EXIT_TAIL_BYTES = 4096
_EXIT_TAIL_LINES = 3
# Bind retry (#591): a restart gesture races the old proxy's listener release,
# and Windows answers WSAEADDRINUSE (10048) for a short window after the socket
# closes. Retry briefly instead of dying on the first refusal.
_BIND_RETRIES = 10
_BIND_RETRY_DELAY_S = 1.0
_PORT_FREE_TIMEOUT_S = 20.0


def daemon_dir() -> Path:
    base = Path(os.environ.get("AIDOCS_DAEMON_DIR") or (Path.home() / ".aidocs" / "daemon"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def health_path() -> Path:
    return daemon_dir() / "health.json"


def stop_flag_path() -> Path:
    return daemon_dir() / "stop.flag"


def _verify_stop_signal():
    """Read + verify the stop flag's ATTRIBUTION (#623).

    Returns a ``daemon_lifecycle_authority.LifecycleVerdict``. An unattributed
    or forged signal returns a REFUSING verdict and is audited, so the answer
    to "who stopped governance" always exists — which it did not when a stop
    was a bare ``write_text("stop")``.

    Never raises: a verification error refuses, and refusing means the
    enforcement daemon KEEPS RUNNING. That is the safe direction here, and it
    is the opposite of the usual fail-closed reflex precisely because the thing
    being protected IS the gate.
    """
    from .daemon_lifecycle_authority import (
        EVENT_LIFECYCLE_REQUESTED,
        EVENT_UNATTRIBUTED_SIGNAL,
        LifecycleVerdict,
        REASON_MALFORMED,
        audit,
        verify,
    )

    try:
        raw = stop_flag_path().read_text(encoding="utf-8")
    except OSError:
        return LifecycleVerdict(authorised=False, reason_code=REASON_MALFORMED)
    verdict = verify(raw)
    audit(
        verdict,
        event_kind=(
            EVENT_LIFECYCLE_REQUESTED if verdict.authorised else EVENT_UNATTRIBUTED_SIGNAL
        ),
    )
    return verdict


def refresh_request_path() -> Path:
    """The EXPLICIT ask for a runtime refresh (#569), same idiom as stop.flag.

    DETECT ALWAYS, REFRESH ONLY WHEN CALLED. The watchdog must never reinstall
    unprompted: it would be silently swapping the package that ENFORCES, i.e.
    changing a user's gate underneath them mid-session. So freshness is reported
    continuously (service_status → `aidocs service status`, and health.json on a
    refresh-driven restart) while the install happens only for a caller who asked
    — today the crown gate's deploy, later an auto-update agent.
    """
    return daemon_dir() / "refresh.request"


def _log(msg: str) -> None:
    log = daemon_dir() / "watchdog.log"
    try:
        if log.exists() and log.stat().st_size > 512_000:
            log.replace(daemon_dir() / "watchdog.log.1")  # tiny 2-file rotation
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def write_daemon_health(*, port: int, pid: int, status: str = "up", **extra: object) -> None:
    """Written by the daemon on bind (status=up) and by the watchdog on EVERY
    supervision poll (#591) plus state changes (down / crash_looped / stopped).

    ``heartbeat_at`` is the load-bearing field: it is the wall-clock instant of
    THIS write, so a reader can tell a live signal from a footprint left by the
    last process that happened to touch the file. Without it the file could only
    ever answer "what did someone write once", which is how it came to say
    ``status=down, pid=0`` while the daemon was serving tool calls (#591 D1).
    """
    payload = {
        "port": port,
        "pid": pid,
        "watchdog_pid": extra.pop("watchdog_pid", None),
        "status": status,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "heartbeat_at": time.time(),
        "heartbeat_interval_s": _HEALTH_HEARTBEAT_S,
        "stale_after_s": _HEALTH_STALE_AFTER_S,
        **extra,
    }
    tmp = health_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(health_path())


def read_daemon_health() -> dict | None:
    try:
        return json.loads(health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def health_age_seconds(health: dict | None, *, now: float | None = None) -> float | None:
    """Seconds since the health file was last written, or None if unknowable.

    None is a real answer here — a health file from before the heartbeat existed
    (or a hand-written one) carries no write instant, and guessing an age would
    manufacture the very false confidence #591 is about.
    """
    if not isinstance(health, dict):
        return None
    beat = health.get("heartbeat_at")
    try:
        beat = float(beat)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0.0, (time.time() if now is None else now) - beat)


def health_is_stale(health: dict | None, *, now: float | None = None) -> bool:
    """True when the heartbeat has stopped (or never existed) — i.e. the file
    reports NOTHING trustworthy about liveness right now."""
    age = health_age_seconds(health, now=now)
    if age is None:
        return True  # no heartbeat at all == cannot be trusted as live
    try:
        limit = float(health.get("stale_after_s") or _HEALTH_STALE_AFTER_S)
    except (TypeError, ValueError):
        limit = _HEALTH_STALE_AFTER_S
    return age > limit


def _daemon_output_tail(
    *, max_bytes: int = _EXIT_TAIL_BYTES, max_lines: int = _EXIT_TAIL_LINES
) -> str:
    """The child's own last words (#591 D3/D4).

    The daemon's stdout+stderr go to daemon.out; on ANY exit — including rc=0 —
    the supervisor quotes the tail so the watchdog log can say WHY the child
    stopped instead of only that it did. Never raises: a missing/unreadable log
    degrades to an explicit 'no output', never to silence."""
    path = daemon_dir() / "daemon.out"
    try:
        with path.open("rb") as fh:
            try:
                fh.seek(-max_bytes, os.SEEK_END)
            except OSError:
                fh.seek(0)
            blob = fh.read()
    except OSError:
        return "no daemon.out"
    lines = [ln.strip() for ln in blob.decode("utf-8", "replace").splitlines() if ln.strip()]
    if not lines:
        return "daemon.out empty"
    return " | ".join(lines[-max_lines:])[:600]


def wait_for_port_free(
    port: int,
    *,
    timeout: float = _PORT_FREE_TIMEOUT_S,
    host: str = "127.0.0.1",
    sleep=time.sleep,
    clock=time.monotonic,
) -> bool:
    """Block until nothing holds ``host:port``, or the timeout lapses (#591 D5).

    The bind attempt IS the probe: on Windows a listener still owning the port
    answers WSAEADDRINUSE, which is exactly the failure a back-to-back
    stop/start would otherwise hit inside the freshly spawned watchdog — where
    it kills the supervisor instead of being retried by the caller who asked."""
    deadline = clock() + max(0.0, timeout)
    while True:
        try:
            with socket.socket() as probe:
                probe.bind((host, port))
            return True
        except OSError:
            if clock() >= deadline:
                return False
            sleep(min(0.5, max(0.05, timeout / 20)))


def _release_marker() -> str:
    """Cheap staleness signal: the installed package dir's __init__ mtime. A
    deploy/pip-install touches it; the watchdog drains+restarts on change."""
    try:
        return str(Path(__file__).with_name("__init__.py").stat().st_mtime_ns)
    except OSError:
        return ""


def windowless_python() -> str:
    """The console-less interpreter (pythonw.exe) next to the current one on
    Windows, so spawned daemons/watchdogs never flash a Terminal tab. python.exe
    is a console-subsystem binary — Windows (esp. Windows Terminal as default)
    allocates a console window for it even under CREATE_NO_WINDOW in some
    configs; pythonw.exe is GUI-subsystem and gets NO console, ever. Falls back
    to sys.executable when pythonw is absent or off-Windows."""
    if os.name == "nt":
        cand = Path(sys.executable).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return sys.executable


def supervisor_runtime(*, verify: bool = True) -> dict:
    """WHICH INTERPRETER THE ENFORCEMENT SUPERVISOR MAY RUN UNDER -- and whether
    that interpreter is OWNED, which the caller must then decide on.

    Used by both gestures that start the watchdog: the transient
    `service start` / `service restart` spawn, and the persisted logon task /
    Startup launcher. ONE resolver on purpose (#727): these were two call sites
    answering one question, and the copy that was not hardened is the one that
    rotted.

    THE WATCHDOG HOSTS THE HOOK BROKER, so whichever interpreter it starts
    under IS the enforcement runtime. That makes ownership a SECURITY property,
    not a preference, and it is why this returns the whole verdict rather than a
    bare path: a caller handed only a string cannot fail closed, because it
    cannot tell a pinned runtime from a fallback.

    DELEGATES TO resolve_runtime, the canonical tier walk: operator_pin
    (AIDOCS_PYTHON) -> standalone -> venv, VERIFYING that each candidate really
    imports aidocs_mcp, and yielding tier='none' / owned=False when none does.
    The first version of this function asked `venv_python()` alone, which
    answers only the venv tier -- so a box provisioned STANDALONE, a box pinned
    via AIDOCS_PYTHON, and a box whose owned runtime is present but BROKEN all
    looked identical to "nothing here" and fell back silently to the ambient
    interpreter. That is the defect this function exists to prevent,
    reintroduced one tier over.

    `allow_ambient` is deliberately NOT passed: the ambient interpreter is
    whatever PATH resolved, which is #727 (A) verbatim.

    MEASURED 2026-08-18, live on the operator's box:
        @echo off
        start "" /min "C:\\Python314\\python.exe" -m aidocs_mcp.cli service run --port 8748
    written 2026-07-06 -- six weeks of the watchdog (which HOSTS THE HOOK
    BROKER) running under the system Python instead of the AIDOCS-owned
    runtime. A persisted launcher is the worse case of the same defect: a
    transient spawn under the wrong python lasts until reboot, a baked launcher
    lasts until someone reinstalls it. It is also why the operator saw a console
    window -- python.exe is console-subsystem, and `start /min` minimises a
    window rather than preventing one.

    On Windows the resolved path is swapped for its pythonw.exe sibling when one
    exists -- GUI-subsystem, so a detached watchdog never allocates a console
    (#249). That substitution is cosmetic and never changes `owned`.

    Returns the resolve_runtime dict: path, owned, tier, verified, degraded,
    reason, checked. `checked` records every tier tried AND why it was rejected,
    so a refusal can tell the operator what was actually wrong instead of only
    that something was.
    """
    try:
        from .runtime_provisioner import resolve_runtime

        res = dict(resolve_runtime(Path.home(), verify=verify))
    except Exception as exc:  # a broken resolver must never read as a good runtime
        return {
            "path": None,
            "owned": False,
            "tier": "none",
            "verified": False,
            "degraded": False,
            "reason": f"resolver_failed: {exc}",
            "checked": [],
        }
    path = res.get("path")
    if path and os.name == "nt":
        cand = Path(path).with_name("pythonw.exe")
        if cand.exists():
            res["path"] = str(cand)
    return res


def supervisor_refusal(rt: dict) -> str:
    """The message shown when the supervisor may NOT start. LAW 311bf3e6: a
    named remedy must be REACHABLE, so this names `aidocs runtime --fix`, which
    exists (cli.py cmd_runtime) and is the same argv runtime_refresh uses to
    provision (PROVISION_ARGV). It also replays `checked` -- an operator told
    only "no owned runtime" cannot tell a missing venv from a broken one.
    """
    tier = rt.get("tier") or "none"
    why = rt.get("reason") or "no_verified_owned_runtime"
    lines = [
        "REFUSING to start the enforcement supervisor: no VERIFIED owned runtime.",
        "  The watchdog hosts the hook broker, so the interpreter it runs under IS",
        "  the enforcement runtime. Starting it under an unowned interpreter would",
        "  silently make whatever PATH resolved into the thing that enforces.",
        f"  resolver verdict: tier={tier} reason={why}",
    ]
    for row in rt.get("checked") or []:
        lines.append(
            f"    tried {row.get('tier')} ({row.get('source')}): {row.get('reason')}"
        )
    lines.append("  provision one with:  aidocs runtime --fix")
    return "\n".join(lines)


def supervisor_identity() -> str:
    """WHICH supervisor this is: interpreter, tree, code generation, provenance.

    #726 acceptance bullet 4, unmet until now. The only startup line was
    `watchdog up (port=N)`, which cannot distinguish "the fix cannot load" from
    "the fix failed" -- and the supervisor is the ONE process a runtime refresh
    cannot replace (it restarts the daemon child, never its own host), so its
    staleness is the longest-lived and was the least visible. The BROKER already
    logs its generation (loaded=/disk=); its host did not log anything about
    itself.

    Four facts, because each answers a question that was asked out loud during
    real incidents:
      interp  WHICH PYTHON -- #727: `aidocs service restart` used to bind the
              supervisor to whatever PATH resolved, so the enforcing process ran
              a source checkout. Seeing the interpreter in the log settles that
              in one line instead of a CommandLine query.
      pkg     WHICH TREE it imported from.
      gen     the SAME package_code_identity the broker reports, so a reader can
              compare the two directly rather than by eye.
      prov    shipped / unshipped -- whether that tree was produced by a
              packaging step (#727 B).

    FAIL-SOFT ABSOLUTELY. Returns a bracketed marker on any error and never
    raises: a supervisor that dies while describing itself is strictly worse
    than one that starts without a description, which is the same rule the
    health-write path already follows.
    """
    try:
        from .hook_broker import artefact_provenance_reason, package_code_identity

        from . import runtime_generations

        pkg = Path(__file__).resolve().parent
        gen = (package_code_identity(pkg) or "unknown")[:12]
        prov = "unshipped" if artefact_provenance_reason(pkg) else "shipped"
        # #1030: WHICH GENERATION, read off the import path rather than the
        # pointer. The supervisor outlives every flip, so "what should serve"
        # and "what this process IS" diverge here more than anywhere else —
        # and it is the process whose staleness is longest-lived. Reporting
        # the pointer would print the answer the reader already has.
        rgen = runtime_generations.loaded_generation(__file__) or "legacy"
        return f"[interp={sys.executable} pkg={pkg} gen={gen} rgen={rgen} {prov}]"
    except Exception:  # noqa: BLE001 -- describing yourself must never be fatal
        return "[identity unavailable]"


def child_python() -> tuple[str, str]:
    """``(interpreter, generation_id_or_reason)`` for a SUPERVISED CHILD (#1030).

    Serves the daemon AND the broker host. It was named `daemon_python` while
    only the daemon used it, which is how `broker_command` kept its own
    `windowless_python()` and went on launching generation A after every flip.
    One resolver, both children.

    THE WATCHDOG IS THE ONE PROCESS A REFRESH NEVER REPLACES — it restarts its
    daemon child, never its own host. So `sys.executable` here is the
    generation the watchdog STARTED under, and using it means every child
    spawned after an activation still runs the old runtime, indefinitely,
    including across restarts of the daemon that were meant to pick up the new
    code. The child must come from the generation the machine has ACTIVATED,
    not from the one its parent happens to be living in.

    RESOLVED ONCE, HERE, AND THE ID TRAVELS WITH THE ARGV. The pointer can move
    between this decision and the child's first breath; re-reading it later to
    ask "which generation is that child?" would answer about the pointer rather
    than about the child. The id resolved here is what gets attested.

    FALLS BACK TO THIS INTERPRETER ONLY WHEN NO GENERATION IS ACTIVATED — a
    legacy install, where this process's own python IS the runtime. A pointer
    that exists but cannot be served resolves to no interpreter at all rather
    than to a different one; the caller refuses, because starting the
    enforcement daemon under a runtime the operator did not activate is exactly
    the substitution the tier walk exists to prevent.
    """
    try:
        from . import runtime_generations
        from .runtime_provisioner import _python_in

        # ONE POINTER READ, AND EVERYTHING BELOW COMES OUT OF IT. The first cut
        # called `serving_venv()` and then `read_pointer()` again to label the
        # interpreter it had just resolved. A flip landing between those two
        # reads returns A's interpreter LABELLED B — or the legacy python once
        # B is active — and the wrong LABEL is the more dangerous half: every
        # downstream attestation then agrees with itself while naming a runtime
        # the child is not running. `Serving` carries the id, so there is
        # nothing left to look up.
        served = runtime_generations.serving_venv(Path.home())
        if served.venv is None:
            if served.reason == runtime_generations.REASON_NO_POINTER_NO_TREE:
                return windowless_python(), ""
            return "", served.reason
        if not served.generation_id:
            # Legacy tree: no generation to name, and this process's own
            # interpreter is the runtime.
            return windowless_python(), ""
        found = _python_in(served.venv)
        if not found:
            return "", "generation_venv_has_no_interpreter"
        if os.name == "nt":
            cand = Path(found).with_name("pythonw.exe")
            if cand.exists():
                found = str(cand)
        return found, served.generation_id
    except Exception as exc:  # noqa: BLE001
        # A broken resolver must never read as "use whatever we are running".
        return "", f"generation_resolver_failed: {exc}"


def daemon_command(port: int, python: str) -> list[str]:
    """Argv for the daemon child, under an EXPLICIT python.

    RESOLVED ONCE, PASSED DOWN — the interpreter is a required argument and
    there is deliberately no default. The first cut called `daemon_python()`
    here as a convenience, while the spawn path ALSO called it to validate. Two
    reads of a pointer that "can move between any two instants" is exactly the
    hazard this whole design exists to remove: the check could pass, the
    pointer break, and this call then fall back to `windowless_python()` — the
    supervisor's own interpreter, i.e. generation A — with the validation
    already satisfied. The commit that introduced it stated the invariant and
    then broke it one function over.

    A required argument makes that unrepresentable: whoever spawns must hold a
    resolved value, and there is no second read to disagree with the first.
    """
    return [python, "-m", "aidocs_mcp.mcp_server", "--http", "--port", str(port)]


def _free_loopback_port() -> int:
    """Ask the OS for a free ephemeral loopback port for a backend daemon."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_backend_ready(port: int, proc, *, timeout: float = _READY_TIMEOUT_S) -> bool:
    """True once the backend daemon ACCEPTS on its loopback port (i.e. uvicorn
    is bound and serving), False if it exits or the timeout lapses. Real time
    on purpose — this is the production readiness probe; tests inject theirs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(_READY_POLL_S)
    return False


# How long the port-owner resolver may take. It only ever runs on a path that
# is ALREADY failing, so it stays capped: a diagnostic must never become the
# reason the supervisor hangs.
#
# RAISED 3.0 -> 15.0, 2026-08-19, on measurement. At 3.0 this cap did not
# bound a pathology, it CAUSED one:
#
#   netstat -ano -p tcp on a loaded Windows box: 5.08s / 3.96s / 3.60s
#
# Every run over budget, so the resolver timed out, the TimeoutExpired was
# swallowed by the fail-quiet except, and the bind-failure message degraded to
# "could not resolve the holding process" -- the exact speculation-shaped
# surface #568 D4 was written to replace. It failed on a LOADED box, which is
# precisely when two supervisors race for a port, so the diagnostic was absent
# exactly when it was needed and present only when it was not.
#
# psutil would make this instant, and the branch above prefers it -- but psutil
# is NOT installed in the dev venv, NOT installed in the owned runtime, and NOT
# declared in pyproject. That fast path is therefore dead in practice on every
# machine measured, and Windows always pays the netstat spawn.
#
# 15s is still bounded and still cannot hang the supervisor. The trade is
# explicit: this path has ALREADY failed and is about to raise, so spending a
# few more seconds to name the holder is strictly better than raising sooner
# with nothing to say.
_PORT_OWNER_TIMEOUT_S = 15.0


def _listening_inodes_from_proc(port: int) -> set[str]:
    """Socket inodes LISTENing on ``port`` per /proc/net/tcp{,6}. Linux only.

    The kernel writes the local address as HEX ``ADDR:PORT``; state ``0A`` is
    TCP_LISTEN. Returns an empty set on any read/parse failure — this feeds a
    fail-quiet diagnostic, never a decision.
    """
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[1:]
        except Exception:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            local = fields[1]
            if ":" not in local:
                continue
            try:
                if int(local.rsplit(":", 1)[1], 16) != port:
                    continue
            except ValueError:
                continue
            inodes.add(fields[9])
    return inodes


def _pid_listening_on_via_proc(port: int) -> int | None:
    """The pid owning a LISTEN socket on ``port``, read from /proc. Linux only.

    Resolves the socket inode from /proc/net/tcp{,6} and then finds which
    process has it open by walking /proc/<pid>/fd. Only same-user processes
    expose those links to a non-root reader; an unreadable one is skipped, so
    the answer is either correct or None — never a guess.
    """
    inodes = _listening_inodes_from_proc(port)
    if not inodes:
        return None
    targets = {f"socket:[{i}]" for i in inodes}
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except Exception:
        return None
    for pid_s in pids:
        fd_dir = f"/proc/{pid_s}/fd"
        try:
            fds = os.listdir(fd_dir)
        except Exception:
            continue  # not ours / vanished — unreadable is not an answer
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") in targets:
                    return int(pid_s)
            except Exception:
                continue
    return None


def _pid_listening_on(port: int) -> int | None:
    """The pid holding 127.0.0.1:<port>, or None if it cannot be determined."""
    try:  # psutil is optional here, as everywhere else in this tree
        import psutil  # type: ignore[import-not-found]

        for conn in psutil.net_connections(kind="tcp"):
            laddr = getattr(conn, "laddr", None)
            if laddr and getattr(laddr, "port", None) == port and conn.pid:
                return int(conn.pid)
        return None
    except Exception:
        pass
    # Linux: resolve from /proc directly. MEASURED 2026-08-01 — the previous
    # POSIX branch shelled out to `lsof`, and NEITHER psutil (not a declared
    # dependency) NOR lsof (not installed on a minimal server image) exists on
    # the VPS, so the whole resolver returned None there and the bind-failure
    # message degraded to "could not resolve the holder" on exactly the box
    # where an operator most needs the pid. /proc needs no external binary, no
    # spawn and no audit row. Only same-user processes expose their fd links,
    # which is precisely the supervisor's own case (it collides with its own
    # stale daemon). Anything unreadable is skipped, so this stays fail-quiet.
    if sys.platform.startswith("linux"):
        pid = _pid_listening_on_via_proc(port)
        if pid:
            return pid
    if sys.platform == "win32":
        cmd = ["netstat", "-ano", "-p", "tcp"]
    else:
        cmd = ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"]
    # #345/XXI: routed through audited_run so this diagnostic spawn lands a
    # process-audit ledger row like every other. The inner passthrough lambda IS
    # the registered AST callsite ('aidocs_service.py', '_pid_listening_on',
    # 'subprocess.run') with its LEGACY_SUBPROCESS_FINGERPRINTS entry. encoding
    # is pinned (#684): text=True alone decodes through the ANSI codepage.
    # CREATE_NO_WINDOW because the supervisor runs under pythonw — an unflagged
    # console child pops a VISIBLE WINDOW on the operator's screen, and this
    # fires exactly when a bind has already failed.
    from .shell_egress_service import audited_run

    _win_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        out = audited_run(
            cmd,
            fingerprint=("aidocs_service.py", "_pid_listening_on", "subprocess.run"),
            reason="bind-failure-port-owner-diagnostic",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PORT_OWNER_TIMEOUT_S,
            creationflags=_win_no_window,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        fields = line.split()
        if sys.platform == "win32":
            if len(fields) < 5 or "LISTEN" not in fields[3].upper():
                continue
            if not fields[1].endswith(f":{port}"):
                continue
            candidate = fields[-1]
        else:
            if len(fields) < 2 or fields[1].upper() == "PID":
                continue
            candidate = fields[1]
        try:
            return int(candidate)
        except ValueError:
            continue
    return None


def port_owner(port: int) -> str | None:
    """Name the process holding 127.0.0.1:<port> — ``"pid 1234 (python.exe)"``.

    #568 D4 / #569 R2. The bind-failure message used to ASK "is a pre-proxy
    daemon still holding it?" — a speculation printed in the one place an
    operator most needs a fact. The data is available, so resolve it.

    Returns None when the holder cannot be determined, and the caller then says
    so plainly. Unknown is reported as unknown; it is never re-dressed as a
    guess. This resolver fails QUIET on purpose: its only caller is an
    already-fatal path, so a resolver that raised or blocked would degrade a
    loud, correct failure rather than improve it.
    """
    if not port:
        return None
    pid = _pid_listening_on(int(port))
    if not pid:
        return None
    name = None
    try:
        import psutil  # type: ignore[import-not-found]

        name = psutil.Process(pid).name()
    except Exception:
        name = None
    return f"pid {pid} ({name})" if name else f"pid {pid}"


class LoopbackProxy:
    """Tiny asyncio TCP forwarder that OWNS 127.0.0.1:<port> for the whole
    watchdog lifetime and pipes each new connection to the CURRENT backend
    daemon. ``set_backend`` flips atomically: new connections reach the new
    daemon; in-flight ones keep their pipe to the old until it drains. The
    listening socket never closes, so editor clients never see refused.
    Loopback-only by construction — no new network exposure."""

    def __init__(self, port: int):
        self.port = port
        self._backend: int | None = None
        self._loop = None  # asyncio loop + Event live on the proxy thread
        self._closing = None
        self._thread: threading.Thread | None = None
        self._bound = threading.Event()
        self._bind_error: BaseException | None = None
        # Live client-connection count per backend port (#432). Read by the
        # watchdog reaper from another thread; dict get + int are atomic under
        # the GIL and the counts are advisory (poll-based), so no lock is needed.
        self._active: dict[int, int] = {}

    def active_for(self, backend_port: int) -> int:
        """How many client connections are still piped to ``backend_port``."""
        return int(self._active.get(backend_port, 0))

    def set_backend(self, port: int) -> None:
        self._backend = port  # single attribute write — atomic under the GIL

    def start(
        self,
        *,
        retries: int = _BIND_RETRIES,
        delay: float = _BIND_RETRY_DELAY_S,
        sleep=time.sleep,
    ) -> None:
        """Own 127.0.0.1:<port>, retrying a still-held port briefly (#591 D5).

        The FIRST bind refusal is not evidence that the port is permanently
        taken — it is the normal shape of a restart: `service stop` is async, so
        the outgoing watchdog's listener may still be closing when the incoming
        one binds, and Windows answers WSAEADDRINUSE (10048) for a short window
        after. The old behaviour turned that ordinary race into a dead
        supervisor (`RuntimeError: proxy could not bind ...`), which is the
        worst possible response: the machine loses AIDOCS entirely because two
        events landed a second apart. We retry with a bounded backoff and only
        then give up — same failure, same message, just no longer on the first
        try.
        """
        attempts = max(1, int(retries))
        for attempt in range(1, attempts + 1):
            self._bound = threading.Event()
            self._bind_error = None
            self._thread = threading.Thread(target=self._run, name="aidocs-proxy", daemon=True)
            self._thread.start()
            self._bound.wait(timeout=10)
            if self._bind_error is None:
                return
            if attempt >= attempts:
                break
            _log(
                f"proxy bind on {self.port} refused (attempt {attempt}/{attempts}): "
                f"{type(self._bind_error).__name__} — retrying in {delay}s"
            )
            sleep(delay)
        # #568 D4: NAME the holder or admit we do not know it. The previous
        # message asked "is a pre-proxy daemon still holding it?" — a guess in
        # the place an operator most needs a fact, and three wrong diagnoses
        # were made off exactly that kind of surface (#569).
        owner = port_owner(self.port)
        held_by = f"held by {owner}" if owner else "could not resolve the holding process"
        raise RuntimeError(
            f"proxy could not bind 127.0.0.1:{self.port} after {attempts} attempt(s) — "
            f"{held_by} ({self._bind_error})"
        )

    def close(self) -> None:
        if self._loop is not None and self._closing is not None:
            try:
                self._loop.call_soon_threadsafe(self._closing.set)
            except RuntimeError:
                pass  # loop already gone
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        import asyncio

        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # noqa: BLE001 — surfaced via start()
            self._bind_error = exc
            _log(f"proxy on {self.port} exited: {type(exc).__name__}: {exc}")
        finally:
            self._bound.set()

    async def _serve(self) -> None:
        import asyncio

        self._loop = asyncio.get_running_loop()
        self._closing = asyncio.Event()
        server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = int(server.sockets[0].getsockname()[1])  # resolves port=0
        self._bound.set()
        async with server:
            await self._closing.wait()

    async def _handle(self, client_reader, client_writer) -> None:
        import asyncio

        backend = self._backend
        if backend is None:
            client_writer.close()
            return
        try:
            backend_reader, backend_writer = await asyncio.open_connection("127.0.0.1", backend)
        except OSError:
            client_writer.close()  # backend down == refused, same as today
            return
        peer = "?"
        try:
            info = client_writer.get_extra_info("peername")
            if info:
                peer = f"{info[0]}:{info[1]}"
        except Exception:  # noqa: BLE001 — peer label is cosmetic
            pass
        # Count this live connection against its backend (#432) so the watchdog
        # reaper won't terminate an old backend while an MCP stream is still on it.
        self._active[backend] = self._active.get(backend, 0) + 1
        try:
            await asyncio.gather(
                self._pipe(
                    client_reader, backend_writer,
                    peer=peer, backend=backend, direction="client->backend",
                ),
                self._pipe(
                    backend_reader, client_writer,
                    peer=peer, backend=backend, direction="backend->client",
                ),
            )
        except Exception:
            # #431: anything the pipes did NOT classify as a benign
            # disconnect is a real fault — keep the full traceback.
            _logger.exception(
                "proxy pipe failed peer=%s backend=%s", peer, backend
            )
        finally:
            remaining = self._active.get(backend, 0) - 1
            if remaining > 0:
                self._active[backend] = remaining
            else:
                self._active.pop(backend, None)

    @staticmethod
    async def _pipe(
        reader,
        writer,
        *,
        peer: str = "?",
        backend: int = 0,
        direction: str = "?",
    ) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except _BENIGN_DISCONNECTS as exc:
            # #431: client disconnect mid-pipe (WinError 10054 / broken pipe)
            # is normal lifecycle — ONE terse line at debug, no traceback.
            _logger.debug(
                "proxy pipe reset peer=%s backend=%s direction=%s: %r",
                peer,
                backend,
                direction,
                exc,
            )
        finally:
            try:
                writer.close()
            except OSError:
                pass


#: Causal-turn recovery sweep (#444). The prompt boundary seals a turn the
#: moment the next operator instruction supersedes it; this periodic pass is
#: the BACKSTOP for turns no boundary can ever reach — the last turn of a
#: session nobody came back to, a client disconnect, an agent crash, a server
#: restart, a missing Stop hook. Spec invariant 10: a Stop hook may assist a
#: seal but is never the only sealer.
_TURN_RECOVERY_INTERVAL_S = 900.0
#: How long a session's CURRENT turn must sit idle before the backstop is
#: allowed to seal it. Generous on purpose: sealing live work would be worse
#: than sealing late, and the boundary already handles the common case.
_TURN_RECOVERY_IDLE_S = 3600


def recover_causal_turns(
    *,
    roots=None,
    idle_seconds: int = _TURN_RECOVERY_IDLE_S,
    store=None,
) -> dict:
    """Seal abandoned causal turns across the registered projects (#444).

    The watchdog-facing entry point for ``CausalTurnStore.recover_open_turns``.
    ``roots`` defaults to the install-wide known-projects registry; ``store``
    is an injection seam. FAIL-SOFT PER PROJECT: one locked/corrupt DB is
    logged and counted, never allowed to abort the sweep of the others — but
    nothing is swallowed silently.
    """
    from datetime import UTC, datetime, timedelta

    if store is None:
        from .causal_turn_store import CausalTurnStore

        store = CausalTurnStore()
    errors = 0
    if roots is None:
        try:
            from .known_projects_store import KnownProjectsStore

            roots = [
                Path(row["project_root"])
                for row in KnownProjectsStore().list_projects()
            ]
        except Exception as exc:  # noqa: BLE001
            _log(f"turn recovery: project registry unreadable: {exc!r}")
            return {"projects": 0, "sealed": 0, "orphans": 0, "errors": 1}
    cutoff = (
        (datetime.now(UTC) - timedelta(seconds=max(0, int(idle_seconds))))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    projects = sealed = orphans = 0
    for root in roots:
        projects += 1
        try:
            out = store.recover_open_turns(Path(root), stale_before=cutoff)
            sealed += len(out.get("sealed_turns") or ())
            orphans += int(out.get("orphans_classified") or 0)
        except Exception as exc:  # noqa: BLE001 — per-project isolation
            errors += 1
            _log(f"turn recovery FAILED for {root}: {exc!r}")
    if sealed or orphans or errors:
        _log(
            f"turn recovery: projects={projects} sealed={sealed} "
            f"orphans_classified={orphans} errors={errors}"
        )
    return {
        "projects": projects,
        "sealed": sealed,
        "orphans": orphans,
        "errors": errors,
    }


def broker_command(python: str) -> list[str]:
    """Argv for the hook-broker host child (#609), under an EXPLICIT python.

    #1030: this used `windowless_python()` — the watchdog's OWN interpreter —
    so after an A→B flip the long-lived watchdog kept launching broker A. The
    broker would then correctly notice it was stale and decline every event to
    the local evaluator, which is safe but means THE WARM PATH COULD NEVER
    BECOME B until someone restarted the service. Detecting the staleness is
    not the same as ending it.

    The interpreter is a REQUIRED ARGUMENT, not a default, for the same reason
    as `daemon_command`: the caller resolves once and passes the answer down,
    so no two reads of the pointer can disagree within one spawn.
    """
    return [python, "-m", "aidocs_mcp.hook_broker_host"]


def _broker_log_handle():
    """One append handle for the broker host's stdout+stderr, opened lazily and
    REUSED across respawns — a per-deploy handle would leak one fd per deploy on
    a process that is meant to run for weeks. Same reasoning (and the same
    must-not-inherit-the-detached-watchdog's-handles requirement) as daemon.out.
    """
    handle = _BROKER_LOG.get("h")
    if handle is None or handle.closed:
        handle = (daemon_dir() / "hook_broker.out").open("a", encoding="utf-8", buffering=1)
        _BROKER_LOG["h"] = handle
    return handle


_BROKER_LOG: dict = {}


def _spawn_broker_process():
    """Spawn one hook-broker host interpreter.

    #335: routed through audited_popen so the spawn lands a process-audit
    ledger row; the inner passthrough lambda IS the registered AST callsite
    ('aidocs_service.py', '_spawn_broker_process', 'subprocess.Popen') with its
    LEGACY_SUBPROCESS_FINGERPRINTS row. Fixed argv, no shell, no agent input.
    """
    from .shell_egress_service import audited_popen

    _win_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    log = _broker_log_handle()
    # #1030: the broker host is a supervised child like the daemon, and gets the
    # SAME treatment — one resolution of the activated generation, frozen, then
    # spawned. Previously it took the watchdog's own interpreter, so after a
    # flip the warm path was permanently generation A: the broker would notice
    # it was stale and decline every event to the local evaluator (safe), but
    # nothing could ever promote it to B short of a service restart. Detecting
    # staleness is not the same as ending it.
    _py, _gen = child_python()
    if not _py:
        raise RuntimeError(
            "REFUSING to spawn the hook broker: the activated runtime "
            f"generation cannot be served ({_gen}). The warm path stays down "
            "and hook events are evaluated locally, which is slower and still "
            "governed. Repair with: aidocs runtime --fix"
        )
    return audited_popen(
        broker_command(_py),
        fingerprint=("aidocs_service.py", "_spawn_broker_process", "subprocess.Popen"),
        reason="watchdog-hook-broker-host",
        popen=lambda *a, **kw: subprocess.Popen(*a, **kw),
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        creationflags=_win_no_window,
    )


def _package_identity_on_disk() -> str | None:
    """The identity of the tree THIS module was imported from, read fresh.

    Deliberately the same function the broker itself uses, so parent and child
    are answering the same question. HONEST LIMIT: the watchdog runs the OLD
    generation of ``package_code_identity``, so if a deploy changes the identity
    ALGORITHM the child's published value will not match what this computes, no
    child will ever be adopted, and the warm path stays retired until the
    service restarts. That is the safe direction — an unprovable swap is
    refused, never assumed — and it is loud in the watchdog log.
    """
    from .hook_broker import package_code_identity

    return package_code_identity(Path(__file__).resolve().parent)


class BrokerChild:
    """Hosts the hook broker in a SUPERVISED CHILD and re-spawns it on deploy.

    #609 lifecycle. Three questions had to be answered before this existed, and
    the answers are why it is a child process rather than a rebuilt object:

    1. REBUILD IN PLACE OR RE-EXEC? Neither. An in-place rebuild cannot PROVE
       anything: ``package_code_identity`` reads DISK, so a new HookBroker built
       inside the old interpreter recomputes the DISK identity, matches it, and
       declares itself fresh while still executing the previous generation's
       already-imported modules — the exact fail-green hole #609 pass 2 closed.
       Re-exec'ing the watchdog would prove freshness, but it drops everything
       this process holds that nothing else does: the LoopbackProxy's listening
       socket on the public port (the "listener never closes" promise), the
       supervised child's Popen handle (a re-exec orphans a running daemon it
       can no longer poll or drain), the deferred-drain list, the crash window
       and failed-start counters that arm the breaker, the release marker and
       the refresh provenance. A child process drops NONE of that, and its
       worst failure — no broker — is exactly the local-evaluation fallback the
       system already runs on.
    2. IN-FLIGHT EVALUATIONS. Cannot tear, by construction: code is never
       replaced under a running evaluation, because a process's modules are
       immutable for its lifetime. The superseded interpreter keeps its own
       code and finishes what it started; it is terminated only after
       ``_BROKER_DRAIN_S``, and only new work goes to the new child.
    3. TWO BROKERS ON ONE RENDEZVOUS. Yes, briefly — both are listening between
       the new child's bind and the old one's reap. Clients reach exactly one:
       the rendezvous file is a single last-write-wins pointer, the new child
       publishes it at start, and a client only ever talks to the pid it read
       there. The old broker is unreachable-by-discovery from that instant, and
       ``HookBroker.close`` already refuses to unlink a rendezvous whose pid and
       token are not its own, so an old broker's exit cannot strand the new one.
       And a client that DID grab the old pointer microseconds earlier reaches a
       broker whose own staleness gate now refuses it — never a stale verdict.

    ADOPTION REQUIRES PROOF. The child publishes the identity it LOADED; the
    parent compares it with the tree on disk. No proof — no rendezvous, a dead
    child, a mismatch, an unreadable tree — means the new child is killed and
    the old one kept. The old one refuses every event (its own #609 gate), which
    is the pre-existing, honest outcome. Fail-quiet, never fail-green.

    COST: all of this happens on the DEPLOY EDGE. The per-event path is
    untouched — no work is added to hook evaluation by this class.
    """

    def __init__(
        self,
        *,
        spawn=None,
        state_dir: Path | None = None,
        disk_identity=None,
        clock=time.monotonic,
        sleep=time.sleep,
        publish_timeout: float = _BROKER_PUBLISH_TIMEOUT_S,
        drain_s: float = _BROKER_DRAIN_S,
        emit=None,
    ) -> None:
        self._spawn = spawn or _spawn_broker_process
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._disk_identity = disk_identity or _package_identity_on_disk
        self._clock = clock
        self._sleep = sleep
        self._publish_timeout = publish_timeout
        self._drain_s = drain_s
        self._emit = emit or _log
        self._proc = None
        self._state: dict | None = None
        self._draining: list[tuple] = []
        self.address: tuple[str, int] | None = None

    # ── rendezvous ───────────────────────────────────────────────────

    def _state_path(self) -> Path:
        from .hook_broker import broker_state_path

        return broker_state_path(self._state_dir)

    def _read_state(self) -> dict | None:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _restore_state(self) -> None:
        """Put the rendezvous back to the broker that is actually serving.

        A rejected child may already have published itself before we could
        judge it. Leaving that pointer would send every hook to a port we are
        about to close — a warm path that fails is worse than one that is
        absent, because the client pays the round-trip before falling back.
        """
        path = self._state_path()
        try:
            if self._state is None:
                path.unlink(missing_ok=True)
                return
            from .hook_broker import harden_registration_custody

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            harden_registration_custody(path)
        except OSError as exc:
            self._emit(f"hook broker: could not restore the rendezvous: {exc!r}")

    # ── proof ────────────────────────────────────────────────────────

    def _clear_state(self) -> None:
        """Retire the rendezvous BEFORE spawning a replacement.

        This is what makes the next publication provably the NEW child's, and
        it is the reason we do not compare pids: on Windows a venv's
        ``pythonw.exe`` is a launcher stub that runs the real interpreter as its
        own child, so ``Popen.pid`` is the stub while ``os.getpid()`` in the
        broker is the interpreter (measured on this host: the live daemon is
        pid 41232 with the actual server at 45752). A pid equality check
        therefore never matched and every adoption timed out.

        Clearing first also removes the two-brokers-one-rendezvous window
        outright: from here until the new child publishes, discovery finds
        NOTHING and hooks evaluate locally — which is what the superseded
        broker would have forced anyway, since it refuses every event once the
        code under it changed. A brief absence beats a pointer to a refuser.
        """
        with contextlib.suppress(OSError):
            self._state_path().unlink(missing_ok=True)

    def _await_publication(self, proc) -> dict | None:
        """The new child's own rendezvous, or None if it never published one."""
        superseded = int((self._state or {}).get("pid") or -1)
        deadline = self._clock() + self._publish_timeout
        while True:
            state = self._read_state()
            if state is not None and int(state.get("pid") or -1) != superseded:
                return state
            if proc.poll() is not None:
                return None  # died before it could say anything
            if self._clock() >= deadline:
                return None
            self._sleep(_BROKER_PUBLISH_POLL_S)

    def _proven_fresh(self, state: dict | None) -> bool:
        if not state:
            return False
        loaded = state.get("code_identity")
        try:
            on_disk = self._disk_identity()
        except Exception as exc:  # noqa: BLE001 — an unreadable tree is UNKNOWN
            self._emit(f"hook broker: cannot read the tree to compare: {exc!r}")
            return False
        # UNKNOWN IS NOT A PASS (#589). ``code_identity`` is JSON null, never
        # "", precisely so an absent answer cannot be mistaken for one.
        if not loaded or not on_disk:
            return False
        return loaded == on_disk

    def _terminate(self, proc) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001 — best effort; then insist
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ── lifecycle ────────────────────────────────────────────────────

    def _adopt(self, proc, state: dict) -> None:
        self._proc = proc
        self._state = state
        port = state.get("port")
        self.address = ("127.0.0.1", int(port)) if port else None

    def start(self) -> BrokerChild:
        self._clear_state()
        proc = self._spawn()
        state = self._await_publication(proc)
        if not self._proven_fresh(state):
            self._terminate(proc)
            self._restore_state()
            self._emit(
                "hook broker host could not prove it loaded the code on disk — "
                "killed; hooks stay on their local (fresh-interpreter) path"
            )
            return self
        self._adopt(proc, state)
        return self

    def refresh_for_deploy(self) -> bool:
        """THE DEPLOY EDGE. True only when a PROVEN-FRESH child took over."""
        self._clear_state()
        proc = self._spawn()
        state = self._await_publication(proc)
        if not self._proven_fresh(state):
            self._terminate(proc)
            self._restore_state()
            self._emit(
                "hook broker: the replacement could not prove it loaded the "
                "shipped code — killed, keeping the previous broker (which "
                "refuses every event until it can be replaced)"
            )
            return False
        old = self._proc
        self._adopt(proc, state)
        if old is not None:
            # Deferred, not immediate: see _BROKER_DRAIN_S. New work already
            # goes to the new child — the rendezvous names it.
            self._draining.append((old, self._clock() + self._drain_s))
        self._emit(f"hook broker refreshed onto the shipped code (addr={self.address})")
        return True

    def maybe_refresh_on_code_change(self) -> bool:
        """SELF-HEAL THE WARM PATH: respawn when the tree no longer matches.

        WHY THIS IS NOT THE "never one we decided to do" RULE. That rule
        (``_consume_refresh_request``) governs REINSTALLING the package, and it
        is right: auto-installing would swap the code that ENFORCES underneath a
        running session. This method installs nothing. The package on disk has
        ALREADY changed and is ALREADY in force — every hook that falls back to
        the local evaluator is running it this second. All this does is stop the
        resident evaluator from being the one component still executing the
        previous generation.

        WITHOUT IT THE WARM PATH CANNOT RECOVER, EVER. A runtime refresh restarts
        the daemon child, never the watchdog that hosts this broker (#609), so a
        stale broker stayed stale until a human restarted the service by hand.
        Measured 2026-08-03: `loaded=c65c26230005` held across five deploys and
        an entire session of commits while `disk=` changed on every one, and the
        operator was told to "restart the AIDOCS service" on every prompt.
        A supervisor that watches a child go stale and waits to be asked is not
        supervising it.

        SAFE BY CONSTRUCTION, and none of that safety is new: refresh_for_deploy
        adopts ONLY a child that PROVES it loaded the tree on disk, kills one
        that cannot, keeps the old broker on failure, and drains rather than
        tears. Its worst outcome is the no-broker fallback the system already
        runs on. UNKNOWN IS NOT A CHANGE: an unreadable tree or a missing
        identity returns False and leaves the broker alone, so a transient read
        error can never trigger a respawn storm.
        """
        if self._proc is None or self._state is None:
            return False  # nothing adopted yet; start() owns that path
        loaded = self._state.get("code_identity")
        try:
            on_disk = self._disk_identity()
        except Exception as exc:  # noqa: BLE001 — unreadable tree is UNKNOWN
            self._emit(f"hook broker: cannot read the tree to compare: {exc!r}")
            return False
        if not loaded or not on_disk or loaded == on_disk:
            return False
        self._emit(
            f"hook broker: package changed on disk (loaded={str(loaded)[:12]} "
            f"disk={str(on_disk)[:12]}) — respawning onto the current code"
        )
        return self.refresh_for_deploy()

    def reap(self, now: float | None = None) -> None:
        """Terminate superseded children whose drain grace has elapsed."""
        if not self._draining:
            return
        now = self._clock() if now is None else now
        survivors: list[tuple] = []
        for proc, deadline in self._draining:
            if proc.poll() is not None:
                continue
            if now >= deadline:
                self._terminate(proc)
            else:
                survivors.append((proc, deadline))
        self._draining[:] = survivors

    def close(self) -> None:
        drained, self._draining = self._draining, []
        for proc, _deadline in drained:
            self._terminate(proc)
        proc, self._proc = self._proc, None
        if proc is not None:
            self._terminate(proc)
            # A terminated child never runs its own cleanup, so retire the
            # rendezvous here: a pointer to a dead port makes every hook pay a
            # refused connection before falling back. Absent beats broken.
            self._state = None
            self._clear_state()


def run_watchdog(
    port: int = 8748,
    *,
    spawn=None,
    sleep=time.sleep,
    clock=time.monotonic,
    max_iterations: int | None = None,
    update_check=None,
    proxy=None,
    alloc_port=None,
    ready_wait=None,
    hook_broker=None,
    turn_recovery=None,
    runtime_refresh=None,
) -> str:
    """Supervise the daemon until stop.flag appears or the breaker trips.

    ``spawn``/``sleep``/``clock``/``max_iterations`` are injection seams so the
    restart/backoff/breaker LOGIC is testable without real processes or time;
    ``proxy``/``alloc_port``/``ready_wait`` are the overlap-restart seams
    (#249b) so swap ORDERING is testable without real sockets. ``spawn`` takes
    the backend port to serve on. Returns 'stopped' | 'crash_looped'.

    Topology (#249b, zero-downtime deploys): the watchdog owns the public
    127.0.0.1:<port> through ``LoopbackProxy`` — its listener never closes —
    and every daemon binds a fresh ephemeral backend port behind it. On a
    release-marker change the NEW daemon is spawned and readiness-checked
    FIRST, the proxy flips atomically, and only THEN is the old one drained.
    """
    # Registered legacy callsite (LEGACY_SUBPROCESS_CALLSITES): the watchdog
    # spawning the daemon it supervises — fixed argv, no shell, no agent input;
    # detached infra with no runtime context, so agent-shell egress law does
    # not apply.
    # Windowless daemon spawn (Windows). Two things are required TOGETHER and
    # neither suffices alone (empirically verified 2026-07-12):
    #   1. windowless_python() (pythonw.exe) inside daemon_command — python.exe
    #      is console-subsystem, so Windows Terminal (as default terminal app)
    #      opens a visible tab for it EVEN under CREATE_NO_WINDOW. pythonw is
    #      GUI-subsystem and never gets a console.
    #   2. EXPLICIT stdout/stderr -> a log file + DEVNULL stdin, HERE. The
    #      console-less daemon cannot inherit the DETACHED watchdog's std
    #      handles, so FastMCP/uvicorn's startup-banner write to stdout crashes
    #      it immediately (observed: rc=1 crash-loop -> circuit breaker OPEN).
    #      An explicit file destination makes the daemon self-sufficient.
    # creationflags=0 is a POSIX no-op, so this stays cross-platform safe. The
    # log handle is opened ONCE per watchdog run and shared across (overlap-)
    # restarts, so there is no per-spawn handle leak.
    _win_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    if spawn is None:
        _daemon_log = (daemon_dir() / "daemon.out").open("a", encoding="utf-8", buffering=1)
        def spawn(backend_port):
            # #335 Phase 1: routed through audited_popen so every daemon
            # spawn lands a process-audit ledger row (pure observability).
            # The inner passthrough lambda IS the registered legacy AST
            # callsite ('aidocs_service.py','spawn','subprocess.Popen') —
            # the fingerprint doctrine gate keeps seeing the identical
            # semantic callsite, and all Popen kwargs (windowless
            # creationflags + daemon.out stdout/stderr + DEVNULL stdin)
            # pass through audited_popen UNCHANGED.
            from .shell_egress_service import audited_popen

            # #1030: REFUSE RATHER THAN SUBSTITUTE. If the machine has
            # activated a generation that cannot be served, there is no
            # interpreter to start the enforcement daemon under — and starting
            # it under THIS process's python would silently run a runtime the
            # operator did not activate. On a migrated box that is the
            # pre-migration runtime, so the daemon would come back up enforcing
            # old code with every surface reporting healthy. A daemon that does
            # not start is loud; one running the wrong code is not.
            # ONE READ, and the value it produced is what gets spawned. The
            # first cut validated here and let `daemon_command` resolve AGAIN,
            # so a pointer that broke between the two reads passed the check
            # and then fell back to the supervisor's own interpreter.
            _py, _gen = child_python()
            if not _py:
                raise RuntimeError(
                    "REFUSING to spawn the AIDOCS daemon: the activated runtime "
                    f"generation cannot be served ({_gen}). The supervisor will "
                    "not start enforcement under a runtime that was not "
                    "activated. Repair with: aidocs runtime --fix"
                )

            return audited_popen(
                daemon_command(backend_port, _py),
                fingerprint=("aidocs_service.py", "spawn", "subprocess.Popen"),
                reason="watchdog-daemon-supervision",
                popen=lambda *a, **kw: subprocess.Popen(*a, **kw),
                stdout=_daemon_log,
                stderr=_daemon_log,
                stdin=subprocess.DEVNULL,
                creationflags=_win_no_window,
            )
    update_check = update_check if update_check is not None else check_for_update
    alloc_port = alloc_port or _free_loopback_port
    ready_wait = ready_wait or _wait_backend_ready
    proxy = proxy or LoopbackProxy(port)
    stop_flag_path().unlink(missing_ok=True)
    crashes: list[float] = []
    # #591 D2/D3: consecutive FAILED STARTS — children that died inside
    # _MIN_UPTIME_S of their spawn. Tracked apart from the windowed crash list
    # because they are a different fact: a crash after an hour of service is a
    # daemon that fell over, a crash after 200ms is a start that never took, and
    # only the second one is guaranteed to repeat forever if we keep retrying.
    # Reset to zero the moment a child survives the minimum uptime.
    failed_starts = 0
    # Old backends awaiting a connection-aware drain (#432): (proc, backend, deadline).
    pending_drains: list[tuple] = []
    backoff = _BACKOFF_START_S
    marker = _release_marker()
    iterations = 0
    # #569: refresh provenance has to be STICKY, not transient. health.json is
    # last-write-wins and the routine health writes below fire on later loop
    # iterations, so a ``runtime`` block written ONLY at the restart site is erased
    # by the very next poll — the operator loses the record of why the daemon
    # bounced. Every write_daemon_health call in this function therefore goes
    # through _health(), which re-attaches the last refresh's axis/verdict/code.
    last_refresh: dict | None = None

    def _health(**kw) -> None:
        if last_refresh:
            kw.setdefault(
                "runtime",
                {k: last_refresh.get(k) for k in ("axis", "verdict", "code")},
            )
        try:
            write_daemon_health(**kw)
        except OSError as exc:
            # #591: the heartbeat now writes on EVERY poll, so a transient disk
            # error is no longer a once-per-restart event. Reporting liveness
            # must never be able to END liveness — a supervisor that dies because
            # it could not write a status file is worse than a missing status
            # file, which readers already treat as stale.
            _log(f"health write failed: {exc!r} (supervision continues)")

    proxy.start()  # owns 127.0.0.1:<port> until the watchdog exits
    _log(f"watchdog up (port={port}) {supervisor_identity()}")
    # ── hook broker (#332, #335 Phase 3) ────────────────────────────────
    # The watchdog process hosts the resident hook-evaluation endpoint so
    # claude_hook can become a thin client instead of a per-call cold
    # interpreter. FAIL-SOFT by design: a broker that cannot start must
    # never stop supervision — hooks simply stay on their local in-process
    # path (the client's None-→-evaluate-locally floor; never fail-open).
    # ``hook_broker`` injection seam: None = real broker, False = disabled,
    # object = test double with start()/close().
    broker = None
    if hook_broker is not False:
        try:
            candidate = hook_broker
            if candidate is None:
                # #609: a CHILD, not an object in this process. The watchdog is
                # never restarted by a deploy, so a broker built here could
                # never become current again — see BrokerChild for why an
                # in-place rebuild cannot prove freshness and a re-exec would
                # cost supervision state nothing else holds.
                candidate = BrokerChild()
            broker = candidate.start() or candidate
            addr = getattr(broker, "address", None)
            _log(f"hook broker up (addr={addr})")
        except Exception as exc:  # noqa: BLE001 — broker is optional infra
            broker = None
            _log(f"hook broker failed to start: {exc!r} (hooks stay local)")
    # Release-channel check on start + every _UPDATE_CHECK_INTERVAL_S (the check
    # itself is check-only and fail-soft — see check_for_update).
    #
    # #868 CHANNEL B: the verdict is now CONSUMED. Both call sites used to call
    # `update_check()` and throw the result away, which is exactly why
    # update.json was computed and nothing ever acted on it. `act_on_update_state`
    # applies the operator's policy (auto | notify | pinned, default notify) and
    # — only under `auto`, only on a clean verdict — ASKS via the same producer
    # the deploy channel uses. One update path for both channels (doctrine XXII);
    # this function still installs nothing itself.
    def _update_check_and_act() -> None:
        # ── THE BUILD AXIS IS ASKED FIRST (#868) ──────────────────────────
        #
        # The release check compares the published VERSION, and a deploy that
        # ships code without bumping semver does not move it. Measured on the
        # operator's box: build 186 -> 187 at version 2.5.1 -> 2.5.1, and this
        # very loop logged "already current — nothing to do" while the daemon
        # served old code and every restart died rc=3. The build is the axis
        # that moved, so it is the one asked first.
        #
        # It only ACTS on a clean, positive answer; an error (no local stamp,
        # authority unreachable) falls through to the release check rather than
        # being treated as either "current" or "behind". Logged either way —
        # a check nobody can see is how this stayed invisible for a day.
        try:
            _build = check_build_axis()
            _log(
                f"build-axis: current={_build.get('current_build')} "
                f"deployed={_build.get('latest_build')} "
                f"update={_build.get('update_available')} "
                f"error={_build.get('error') or '-'}",
            )
            if _build.get("update_available") and not _build.get("error"):
                _log(f"update-policy: {act_on_update_state(_build)}")
                return
        except Exception:  # noqa: BLE001 — a broken checker must not stop supervision
            pass
        try:
            _log(f"update-policy: {act_on_update_state(update_check())}")
        except Exception:  # noqa: BLE001 — a broken checker must not stop supervision
            pass

    _update_check_and_act()
    last_update_check = clock()
    last_broker_code_check = clock()
    # ── causal-turn recovery sweep (#444) ───────────────────────────────
    # ``turn_recovery`` seam: None = the real sweeper, False = disabled,
    # callable = test double. Runs once on entry then on its own interval —
    # the watchdog is the right home for periodic work (never a hot path),
    # and it is the one process that outlives daemon restarts, so a turn
    # orphaned BY a restart still gets sealed. FAIL-SOFT: a sweep failure
    # is logged and supervision continues.
    sweep = None
    if turn_recovery is not False:
        sweep = turn_recovery if turn_recovery is not None else recover_causal_turns

    def _sweep_turns() -> None:
        if sweep is None:
            return
        try:
            sweep()
        except Exception as exc:  # noqa: BLE001 — never stop supervision
            _log(f"causal-turn recovery sweep failed: {exc!r}")

    _sweep_turns()
    last_turn_sweep = clock()
    # ── LOCAL ENFORCEMENT RUNTIME refresh (#569) ────────────────────────
    # The watchdog owns refresh because the refresher must OUTLIVE the refreshed: a
    # process cannot cleanly restart itself, so the daemon is a TARGET and this
    # process is the only local one that is not. Operator 2026-07-28: "maybe just
    # have 1 process handle this, same as deamon, because dev aidocs doesnt have the
    # gate at all, but it should be able to update and refresh its code" — a
    # gate-only fix repaired exactly one private machine.
    # ``runtime_refresh`` seam: None = the real refresher, False = disabled,
    # callable = test double. FAIL-SOFT like every other periodic job here.
    refresher = None
    if runtime_refresh is not False:
        if runtime_refresh is not None:
            refresher = runtime_refresh
        else:

            def refresher(**kw):
                from .runtime_refresh import refresh_runtime

                return refresh_runtime(emit=_log, **kw)

    def _consume_refresh_request() -> dict | None:
        """Perform an EXPLICITLY REQUESTED refresh; never one we decided to do.

        Auto-reinstalling would silently swap the package that ENFORCES, changing a
        user's gate underneath them mid-session — so detection is continuous and the
        install waits for a caller (the crown gate's deploy today). The request is
        consumed BEFORE the work runs: a refresh that dies mid-install must not
        re-trigger itself on every poll and turn one bad install into a loop.
        """
        req = refresh_request_path()
        if not req.exists():
            return None
        try:
            body = json.loads(req.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            body = {}  # a truncated/hand-written request still means "refresh"
        try:
            req.unlink(missing_ok=True)
        except OSError:
            pass
        if refresher is None:
            _log("runtime refresh requested but the refresher is disabled — ignoring")
            return None
        try:
            result = refresher(check_only=bool(body.get("check_only")))
        except Exception as exc:  # noqa: BLE001 — never stop supervision
            _log(f"runtime refresh FAILED: {exc!r}")
            return None
        _log(
            f"runtime refresh: verdict={result.get('verdict')} axis={result.get('axis')} "
            f"code={result.get('code')}"
        )
        # The reinstall alone already made the HOOKS current (fresh subprocess per
        # event). The daemon is long-lived, so it only picks the new code up through
        # the overlap restart below — and only when the refresh actually succeeded:
        # restarting onto a failed install buys downtime risk for nothing.
        result["restart_daemon"] = bool(body.get("restart_daemon", True)) and (
            result.get("code") == 0
        )
        return result

    def _drain(p, timeout: float) -> None:
        p.terminate()
        try:
            p.wait(timeout=timeout)
        except Exception:
            p.kill()

    def _reap_pending(force: bool = False) -> None:
        """Terminate deferred old backends (#432) once their live MCP streams
        have closed (proxy.active_for == 0) or the drain cap has elapsed — so a
        code hot-swap never severs the operator's in-flight session. ``force``
        drains everything (shutdown). A proxy without ``active_for`` (test double
        / pre-#432) reports 0 → immediate reap == the old terminate-now behavior."""
        if not pending_drains:
            return
        _active_for = getattr(proxy, "active_for", None)
        survivors: list[tuple] = []
        for old_proc, old_backend, deadline in pending_drains:
            if old_proc.poll() is not None:
                continue  # already exited
            idle = _active_for is None or int(_active_for(old_backend)) == 0
            if force or idle or clock() >= deadline:
                _drain(old_proc, 30)
            else:
                survivors.append((old_proc, old_backend, deadline))
        pending_drains[:] = survivors

    # #903: a hot-swap that failed readiness stays OWED until it succeeds.
    swap_pending = False
    swap_retry_at = 0.0
    swap_backoff = _SWAP_RETRY_START_S

    try:
        while True:
            if max_iterations is not None and iterations >= max_iterations:
                return "stopped"
            iterations += 1

            backend = alloc_port()
            spawned_at = clock()
            proc = spawn(backend)
            proxy.set_backend(backend)
            _log(f"daemon spawned pid={getattr(proc, 'pid', '?')} backend={backend}")
            # #591 D1: the ORDINARY (re)spawn used to write no health at all —
            # only the overlap-restart path did. So after any crash-restart the
            # file kept whatever the crash branch last wrote (status=down,
            # pid=0) for as long as the new daemon served: a liveness file that
            # said DOWN while tool calls were being answered.
            _health(
                port=port,
                pid=getattr(proc, "pid", 0),
                status="up",
                backend_port=backend,
                watchdog_pid=os.getpid(),
            )
            while proc.poll() is None:
                if stop_flag_path().exists():
                    # #623: VERIFY BEFORE OBEYING. The flag's mere existence is
                    # no longer authority — that was the whole defect. A signal
                    # with no attribution, a forged MAC, or an expired
                    # timestamp is IGNORED and named in the audit ledger, so a
                    # bare `write_text("stop")` by anything that can reach the
                    # daemon dir stops nothing and stops being invisible.
                    verdict = _verify_stop_signal()
                    if verdict.authorised:
                        _log(
                            f"stop.flag — terminating daemon (authorised: "
                            f"{verdict.actor_kind}:{verdict.actor_uid})"
                        )
                        _reap_pending(force=True)  # drain deferred old backends too
                        _drain(proc, 15)
                        _health(port=port, pid=0, status="stopped")
                        return "stopped"
                    _log(
                        f"stop.flag REFUSED — {verdict.reason_code}: "
                        f"{verdict.detail or 'unattributed lifecycle signal'}. "
                        f"Daemon KEEPS RUNNING. Use `aidocs service stop` as an "
                        f"authenticated principal."
                    )
                    # Clear the rejected signal so a permanently-unauthorised
                    # flag cannot re-log every poll, and so the next legitimate
                    # request starts from a clean slate.
                    stop_flag_path().unlink(missing_ok=True)
                    # DELIBERATELY NO `continue` HERE. A refused stop must not
                    # skip the rest of supervision: `continue` would bypass the
                    # heartbeat write, the deferred-backend reaper, the update
                    # check AND the poll sleep — so an unauthorised flag would
                    # silently degrade the supervisor into a busy spin, turning a
                    # refused stop into a denial of service against the very
                    # daemon this gate exists to protect. Fall through instead.
                current = _release_marker()
                # #569: a just-completed refresh reinstalled the OWNED VENV, which makes the
                # hooks current immediately (fresh subprocess per event) but leaves the
                # long-lived daemon on old code — and a venv reinstall does NOT move the
                # source marker this loop watches, so the refresh must ask for the restart
                # itself. Reuses the proven overlap path below rather than adding a second
                # restart route: new daemon ready FIRST, proxy flips, old one drains.
                refreshed = _consume_refresh_request()
                _refresh_restart = bool(refreshed and refreshed.get("restart_daemon"))
                _marker_moved = bool(current and marker and current != marker)
                # #903: `swap_pending` keeps a FAILED swap owed. Without it the
                # marker was advanced on ATTEMPT (below), so the "next marker
                # change retries" this branch promises could never fire — the
                # marker never moved again, and the daemon stayed on old code
                # until a human restarted it.
                _want_swap = _refresh_restart or _marker_moved or swap_pending
                if _want_swap and clock() >= swap_retry_at:
                    _log(
                        "runtime refreshed — overlap-restart so the daemon runs the new code too"
                        if _refresh_restart
                        else (
                            # Deliberately does NOT say "failed readiness": #726's
                            # guard requires every line carrying that phrase to
                            # quote the child's last words, and rightly so. This
                            # is the RETRY ANNOUNCEMENT, not a failure report —
                            # the failure was already reported, with its output.
                            "retrying the hot-swap the previous attempt could not "
                            "complete — the daemon is ALIVE BUT STALE, which no "
                            "health check reports"
                            if swap_pending and not _marker_moved
                            else "release marker changed — overlap-restart onto new code"
                        )
                    )
                    marker = current or marker
                    # #609 THE DEPLOY EDGE REACHES THE BROKER. Before this, the
                    # branch below replaced the daemon child and nothing else,
                    # so the resident hook evaluator kept running whatever the
                    # watchdog imported at its own startup — thirteen deploys
                    # deep on the reference host. FAIL-SOFT and BOUNDED: the
                    # broker is optional infrastructure, so a host that cannot
                    # come back fresh (or one that predates this lever, or one
                    # that raises) must never stop supervision — hooks simply
                    # stay on their local fresh-interpreter path. This is the
                    # ONLY place the swap happens: the per-event path pays
                    # nothing for it.
                    _refresh = getattr(broker, "refresh_for_deploy", None)
                    if _refresh is not None:
                        try:
                            if not _refresh():
                                _log(
                                    "hook broker did NOT come back fresh — the "
                                    "resident evaluator keeps refusing (hooks "
                                    "evaluate locally, still fully governed)"
                                )
                        except Exception as exc:  # noqa: BLE001 — optional infra
                            _log(f"hook broker refresh FAILED: {exc!r} (hooks stay local)")
                    iterations += 1  # graceful spawns count toward the bound too
                    new_backend = alloc_port()
                    new_proc = spawn(new_backend)
                    if ready_wait(new_backend, new_proc):
                        # New daemon is serving: flip the proxy FIRST so the
                        # public port never stops answering, THEN drain the old.
                        proxy.set_backend(new_backend)
                        # Record WHY and on which axis, so health.json shows that this
                        # restart was a refresh rather than a mystery bounce. Held in
                        # ``last_refresh`` (not passed once here) because _health re-attaches
                        # it to every later write — otherwise the next poll's routine write
                        # silently erases the provenance this line exists to record.
                        if refreshed:
                            last_refresh = refreshed
                        _health(
                            port=port,
                            pid=getattr(new_proc, "pid", 0),
                            status="up",
                            backend_port=new_backend,
                            watchdog_pid=os.getpid(),
                        )
                        # DEFER the old backend's kill (#432): keep it serving so
                        # the operator's LIVE MCP stream (still piped to it) is not
                        # severed. The reaper terminates it once its connections
                        # drain to zero (natural session end) or the cap elapses.
                        pending_drains.append((proc, backend, clock() + _OVERLAP_DRAIN_MAX_S))
                        proc, backend = new_proc, new_backend
                        spawned_at = clock()  # the new child's own uptime clock
                        backoff = _BACKOFF_START_S
                        # #903: the debt is settled only HERE, on a daemon that
                        # actually became ready — never on the attempt.
                        swap_pending = False
                        swap_retry_at = 0.0
                        swap_backoff = _SWAP_RETRY_START_S
                        _log(f"overlap-restart complete backend={new_backend}; old backend deferred-drain")
                    else:
                        # New build never became ready — keep the old daemon
                        # serving (same spirit as the breaker: no downtime for
                        # a broken deploy). Next marker change retries.
                        # SAY WHY (#726). This branch used to log the bare
                        # sentence and nothing else, so a readiness failure left
                        # NO recoverable evidence — and _wait_backend_ready
                        # returns False for TWO opposite reasons: the child
                        # EXITED, or it never bound before the deadline. Those
                        # need different fixes and the log could not tell them
                        # apart. Measured 2026-08-01: three consecutive deploys
                        # failed here across two days (07-31 01:58, 08-01 15:36,
                        # 08-01 20:55), every one silent, and the cause could not
                        # be reconstructed afterwards from any artifact — the
                        # operator ran old code for two days. The crash path
                        # already quotes the child's last words via
                        # _daemon_output_tail(); this path must too, or the
                        # module docstring's promise ("its tail is quoted into
                        # the watchdog log on EVERY daemon exit") is false here.
                        rc = new_proc.poll()
                        why = (
                            f"child exited rc={rc}"
                            if rc is not None
                            else f"never bound within {_READY_TIMEOUT_S:.0f}s"
                        )
                        # #903: STAY OWED. Keeping the old daemon is right (no
                        # downtime for a broken build), but forgetting is not:
                        # before this, the marker had already been advanced above,
                        # so nothing ever tried again and the box served old code
                        # until a human noticed. Retry on our own clock, backed
                        # off and capped — a daemon that genuinely cannot start
                        # must not become a restart storm.
                        swap_pending = True
                        swap_retry_at = clock() + swap_backoff
                        # NAME THE LIKELY CAUSE. "failed readiness" plus a drift
                        # complaint reads as a broken build and sends an operator
                        # to --record-package; it is usually the install/trust
                        # skew described at _SWAP_RETRY_START_S, which the retry
                        # below settles on its own. Saying so is the difference
                        # between a scary log line and an informative one.
                        _tail = _daemon_output_tail()
                        _likely = (
                            " — this looks like the post-install trust skew, which "
                            "the retry settles; no operator action expected"
                            if "package drift" in str(_tail).lower()
                            else ""
                        )
                        _log(
                            f"new daemon failed readiness ({why}) — keeping old "
                            f"daemon (ALIVE BUT STALE: it serves, so nothing looks "
                            f"broken); retrying in {swap_backoff:.0f}s{_likely}; "
                            f"last words: {_tail}"
                        )
                        swap_backoff = min(swap_backoff * 2, _SWAP_RETRY_MAX_S)
                        _drain(new_proc, 15)
                if clock() - last_update_check >= _UPDATE_CHECK_INTERVAL_S:
                    last_update_check = clock()
                    _update_check_and_act()
                if clock() - last_turn_sweep >= _TURN_RECOVERY_INTERVAL_S:
                    last_turn_sweep = clock()
                    _sweep_turns()
                _reap_pending()  # terminate deferred old backends once their streams close
                # #609: same idea one layer over — a broker superseded by a
                # deploy is terminated only once its drain grace has elapsed, so
                # an evaluation that was already mid-decision finishes on the
                # code it started with instead of being severed.
                _reap = getattr(broker, "reap", None)
                if _reap is not None:
                    try:
                        _reap()
                    except Exception as exc:  # noqa: BLE001 — optional infra
                        _log(f"hook broker reap failed: {exc!r}")
                # #609 SELF-HEAL — THE SUPERVISOR NOTICES, NOBODY IS ASKED.
                # A broker overtaken by a package swap can never recover on its
                # own: a runtime refresh restarts the daemon child, never the
                # watchdog that hosts the broker. Until now the only cure was a
                # human running `aidocs service restart`, and the degraded
                # banner said so on every prompt. Supervising a child means
                # noticing it went stale, not waiting to be told.
                if clock() - last_broker_code_check >= _BROKER_CODE_POLL_S:
                    last_broker_code_check = clock()
                    _self_heal = getattr(broker, "maybe_refresh_on_code_change", None)
                    if _self_heal is not None:
                        try:
                            _self_heal()
                        except Exception as exc:  # noqa: BLE001 — optional infra
                            _log(f"hook broker self-heal failed: {exc!r}")
                # #591 D1 THE HEARTBEAT. Rewritten every poll from the LIVE
                # supervisor with the real daemon pid, so the file answers "is
                # this signal current?" and not merely "what did someone write
                # once". A reader that finds no fresh beat must say STALE.
                _health(
                    port=port,
                    pid=getattr(proc, "pid", 0),
                    status="up",
                    backend_port=backend,
                    watchdog_pid=os.getpid(),
                )
                sleep(_MARKER_POLL_S)

            # The daemon exited on its own. It is supposed to serve until asked
            # to stop, so EVERY self-exit is unexpected — rc=0 included; a clean
            # return code says the process chose to stop, not that stopping was
            # correct. #591 D3/D4: say WHY (quote the child's own last output)
            # and distinguish a FAILED START (died inside _MIN_UPTIME_S) from a
            # daemon that fell over after real service.
            now = clock()
            uptime = max(0.0, now - spawned_at)
            rc = proc.returncode
            tail = _daemon_output_tail()
            crashes = [t for t in crashes if now - t <= _BREAKER_WINDOW_S]
            crashes.append(now)
            failed_start = uptime < _MIN_UPTIME_S
            failed_starts = failed_starts + 1 if failed_start else 0
            kind = "FAILED START" if failed_start else ("clean exit" if rc == 0 else "crash")
            _log(
                f"daemon exited rc={rc} after {uptime:.1f}s — {kind} "
                f"(crash {len(crashes)}/{_BREAKER_CRASHES}"
                + (f", failed-start {failed_starts}/{_FAILED_START_LIMIT}" if failed_start else "")
                + f"); last output: {tail}"
            )
            last_exit = {
                "rc": rc,
                "uptime_s": round(uptime, 3),
                "classification": kind,
                "tail": tail,
                "crashes_in_window": len(crashes),
                "consecutive_failed_starts": failed_starts,
            }
            if failed_starts >= _FAILED_START_LIMIT:
                # GIVE UP LOUDLY (#591 D3). Retrying a start that has never once
                # survived _MIN_UPTIME_S is not supervision, it is a spin — and
                # an invisible one. Stop, and leave the reason where the operator
                # and the gate's half-open notice both read it (health.json).
                _log(
                    f"GIVING UP — {failed_starts} consecutive failed starts "
                    f"(each under {_MIN_UPTIME_S}s). The daemon is NOT running and "
                    f"will not be restarted. Last output: {tail}"
                )
                _health(
                    port=port, pid=0, status="crash_looped",
                    reason="failed_start_loop", last_exit=last_exit,
                )
                return "crash_looped"
            if len(crashes) >= _BREAKER_CRASHES:
                _log(f"circuit breaker OPEN — build looks broken, not restarting; last output: {tail}")
                _health(
                    port=port, pid=0, status="crash_looped",
                    reason="crash_breaker", last_exit=last_exit,
                )
                return "crash_looped"
            _health(port=port, pid=0, status="down", last_exit=last_exit)
            sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP_S)
    finally:
        if broker is not None:
            try:
                broker.close()
            except Exception:  # noqa: BLE001 — shutdown must stay clean
                pass
        proxy.close()


def request_stop(request: object | None = None) -> None:
    """Write an ATTRIBUTED stop signal (#623).

    WAS: ``stop_flag_path().write_text("stop")`` — stopping the governance
    daemon was a bare file write, so anything that could write the daemon
    directory could disable every gate on the machine, silently and
    unattributably. The CLI was a convenience, not a gate.

    NOW the signal itself carries authority: ``request`` is a
    ``daemon_lifecycle_authority.LifecycleRequest`` minted only after a real
    permission check, and the watchdog VERIFIES it before acting (see the
    stop-flag branch in ``run_watchdog``). Curing this at the CLI alone would
    have left the flag writable by anything, so the failure would simply have
    MOVED to the next writer.

    ``request=None`` still writes the legacy bare flag ON PURPOSE. It is what
    an ungoverned caller produces, and the consumer now REFUSES exactly that
    shape and audits it as ``daemon_stop_unattributed``. Keeping the shape
    writable while making it ineffective is what turns the old interface into
    evidence instead of a hole — and it keeps every existing caller honest
    rather than crashing it into a false sense of having been migrated.
    """
    if request is None:
        stop_flag_path().write_text("stop", encoding="utf-8")
        return
    stop_flag_path().write_text(request.serialize(), encoding="utf-8")  # type: ignore[attr-defined]


def request_runtime_refresh(*, check_only: bool = False, restart_daemon: bool = True) -> Path:
    """Ask the watchdog to bring the enforcement runtime to parity (#569).

    ``restart_daemon`` covers the two capabilities in risk order. The reinstall alone already
    makes the HOOKS current IMMEDIATELY and carries no restart risk — every hook entry in
    ~/.claude/settings.json is a FRESH SUBPROCESS per event
    (runtime/venv/Scripts/pythonw.exe -m aidocs_mcp.claude_hook), so the next invocation
    loads the new code with no session interruption. The daemon is the separate case: it is
    a long-lived process, so it needs the watchdog's existing overlap-restart to pick new
    code up. Callers that only care about hook law can pass restart_daemon=False.
    """
    payload = {
        "check_only": bool(check_only),
        "restart_daemon": bool(restart_daemon),
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = refresh_request_path()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _pid_alive(pid: int) -> bool:
    """READ-ONLY liveness probe.

    NEVER use os.kill(pid, 0) here: on Windows any non-CTRL signal value is
    passed to TerminateProcess — the 'probe' KILLS the daemon (found live
    2026-07-06: `aidocs service status` terminated the daemon it was checking).
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)  # POSIX: sig 0 is a true no-op existence check
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def service_status() -> dict:
    """Status for `aidocs service status` + the hook nudge: merges the health
    file with a liveness probe of the recorded pid."""
    health = read_daemon_health() or {"status": "never_started"}
    pid = health.get("pid") or 0
    health["daemon_alive"] = _pid_alive(int(pid)) if pid else False
    if health.get("status") == "up" and not health["daemon_alive"]:
        health["status"] = "down"  # stale health file — daemon died with watchdog
    # #591 D1: a heartbeat that stopped is NOT a liveness answer. An "up" older
    # than stale_after_s means the supervisor stopped writing — the pid may even
    # still exist (a wedged process, a recycled pid) — so the honest report is
    # "stale", never a confident up. Unknown is not a pass. Deliberate terminal
    # states (stopped / crash_looped / down) stay as written: they are the last
    # process's FINAL word about itself, and ageing does not make them less true.
    health["health_age_s"] = health_age_seconds(health)
    health["stale"] = health_is_stale(health)
    if health["stale"] and health.get("status") == "up":
        health["status"] = "stale"
    update = read_update_state()
    if update is not None:
        health["update"] = update
    # #569 DETECT ALWAYS: the runtime the HOOKS execute is a different artifact from the
    # source the daemon imports, and nothing used to report on it — so `aidocs setup` could
    # print "trust chain proven end-to-end" over a two-day-stale enforcement package. This is
    # the reporting half; the install only ever happens on an explicit request (see
    # refresh_request_path). Fail-soft, and it names the axis it answered.
    try:
        from .runtime_refresh import freshness_report

        health["runtime"] = freshness_report()
    except Exception as exc:  # noqa: BLE001 — status must never raise
        health["runtime"] = {
            "axis": None,
            "fresh": None,
            "note": f"freshness unavailable: {type(exc).__name__}",
        }
    # #489 THE READER. The hook broker's queue/compute split lives in a bounded
    # in-memory ring inside the WATCHDOG process, which no other process can
    # read — so #489's documented next step ("read queue_ms vs eval_ms") had
    # never been executable and an earlier pass optimised on a guess. This folds
    # it in exactly like health["runtime"] above: fail-soft, never raises, and
    # reached over the broker's existing token-gated loopback socket rather than
    # by adding a DB write to the hot path being measured.
    #
    # `verdict` is the actionable field: "queue_dominant" means the fix is
    # concurrency, "eval_dominant" means it is the slow stage. Two causes with
    # opposite fixes, now distinguishable without a restart.
    try:
        from .hook_broker_client import fetch_broker_timings

        # include_rows=False: status carries the SUMMARY, not the ring. The
        # 256 raw rows were ~67% of this payload and every question they can
        # answer is already answered by summary.overall / late_by_event /
        # verdict, computed from those same rows.
        health["hook_broker"] = fetch_broker_timings(include_rows=False)
    except Exception as exc:  # noqa: BLE001 — status must never raise
        health["hook_broker"] = {
            "available": False,
            "reason": f"exception:{type(exc).__name__}",
        }
    # #838: IS ANYONE CALLING IT? The block above reports whether the broker is
    # UP. It cannot report whether the host is configured to USE it, and those
    # look identical from here: a broker with an empty ring reads as "quiet",
    # not as "unreachable by design".
    #
    # MEASURED 2026-08-19 on the operator's own box -- broker up, listening,
    # custody-verified, 0 of 256 ring samples after ninety minutes of heavy tool
    # use, because ~/.claude/settings.json declared NO hooks at all. Nothing
    # anywhere said so. The MCP-side gates were live throughout, which is
    # precisely what made it invisible: enforcement APPEARED to be working.
    try:
        from .claude_hooks_install import claude_hooks_status

        health["host_hooks"] = claude_hooks_status()
    except Exception as exc:  # noqa: BLE001 — status must never raise
        health["host_hooks"] = {
            "installed": False,
            "reason": f"exception:{type(exc).__name__}",
        }
    return health


# ── Auto-update CHECKER (Empire directive 2026-07-06) ─────────────────────────
# The marker-watch above already drains+restarts when new code lands; this is
# the other half — is a newer RELEASE published on the channel? Check-only by
# law: no pip, no artifact fetch (aidocs-doctrine §XXIV — installs are signed/
# verified operator paths, never watchdog side effects). Fail-soft: a dead
# network never breaks the watchdog, the CLI, or the dashboard.

UPDATE_CHANNEL_DEFAULT = "https://api.github.com/repos/cristian1991/AIDOCS/releases/latest"
#: How often the watchdog asks whether a newer build/release exists.
#:
#: SIX HOURS WAS RIGHT WHEN A PUSH EXISTED. Until the split (operator law
#: 2026-08-24, "why does deploy control what local processes do?"), a deploy
#: REACHED INTO the operator's box at Gate 5c and installed there directly, so
#: pickup latency was ~0 and this poll only had to catch the public release
#: channel eventually. Deleting that reach without shortening this would have
#: traded one bug for a worse one: an update mechanism that is architecturally
#: correct and takes up to six hours to notice its own deploy.
#:
#: THE POLL IS NOW THE ONLY WAY A BOX LEARNS. 15 minutes is the smallest number
#: that stays honest about cost: one ~4s-timeout GET per interval per install
#: (96/day) against an authority that is the operator's own hub by default.
#: Anything shorter buys minutes and spends someone else's bandwidth; anything
#: longer makes "AIDOCS updates itself" a claim the operator cannot observe.
#:
#: This changes only WHEN the question is asked. Whether anything is INSTALLED
#: is still the operator's policy (default "notify" — see UPDATE_POLICY_DEFAULT),
#: so a shorter poll can never turn into a surprise swap of the enforcing package.
_UPDATE_CHECK_INTERVAL_S = 15 * 60.0


def update_state_path() -> Path:
    return daemon_dir() / "update.json"


def _current_version() -> str:
    try:
        import aidocs_mcp

        return str(getattr(aidocs_mcp, "__version__", "") or "")
    except Exception:  # noqa: BLE001 — checker is fail-soft
        return ""


def _version_tuple(v: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2' -> (1,2,3) / (1,2). Raises ValueError on junk."""
    v = v.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts = tuple(int(p) for p in v.split(".") if p != "")
    if not parts:
        raise ValueError(f"not a version: {v!r}")
    return parts


def _default_fetch(url: str, timeout: float) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "aidocs-update-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https channel URL
        return resp.read().decode("utf-8", errors="replace")


def _installed_build() -> int | None:
    """The BUILD this box is running, from the artefact's own stamp, or None.

    Seam, so a test can say what the box is without installing one.
    """
    try:
        from .build_stamp import read_build_stamp
        from .runtime_provisioner import installed_package_dir

        stamp = read_build_stamp(installed_package_dir()) or {}
        build = stamp.get("build")
        return int(build) if isinstance(build, int) else None
    except Exception:  # noqa: BLE001 — no stamp is an honest unknown, never a number
        return None


def _fetch_authority_axes(*args, **kwargs) -> dict:
    """Seam over build_authority.fetch_authority_axes (it raises by contract)."""
    from .build_authority import authority_url, fetch_authority_axes

    return fetch_authority_axes(kwargs.pop("base_url", None) or authority_url(), **kwargs)


def check_build_axis() -> dict:
    """Is this box running the build the AUTHORITY says is deployed? (#868)

    THE AXIS THAT ACTUALLY MOVES. `check_for_update` compares the published
    VERSION, and a deploy that ships code without bumping semver does not move
    it: measured on the operator's box, build 186 -> 187 with version 2.5.1 ->
    2.5.1, and the watchdog's refresh reported "already current — nothing to do"
    while the daemon sat on the old code and every restart died rc=3. A version
    comparison cannot see a build bump, so the mechanism that exists to remove
    manual restarts could never fire on the commonest kind of deploy.

    ONE INTEGER AGAINST ONE INTEGER, which is the whole point of the campaign's
    "stamp/build comparison against the authority" — and it is answerable on a
    CLIENT machine with no checkout, no git and no ship stage, where every
    tree-diffing answer is structurally unavailable.

    FAIL-CLOSED, because what this feeds is INSTALLING REMOTE CODE:
      * no local stamp -> we cannot prove we are behind. Reported as an error and
        NOT as an update; a fetch-and-install must never ride on a guess.
      * authority unreachable or malformed -> the reason is recorded and nothing
        is claimed. `act_on_update_state` already refuses an errored verdict.
    Never raises.
    """
    out: dict = {
        "current_build": None,
        "latest_build": None,
        "update_available": False,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error": None,
    }
    current = _installed_build()
    out["current_build"] = current
    try:
        # ── ASK THE AUTHORITY THROUGH THE ONE RESOLVER (#903, 2026-08-24) ──
        #
        # This used to read `axes["deployed"]["build"]` directly, and MEASURED
        # AGAINST THE LIVE AUTHORITY that field is the STRING "UNVERIFIED" — the
        # real integer (188) is under `running`. The `isinstance(latest, int)`
        # guard then failed, latest_build was None, and every single call
        # returned "the authority named no deployed build — nothing to compare".
        # THE UPDATER COULD NEVER FIRE, ON ANY BOX. Built, wired, and inert —
        # the exact #575 shape this campaign exists to end, one axis over.
        #
        # `_deployed_from_authority` is the resolver ai_version already uses, and
        # it encodes the reason: "MEMORY BEATS DISK. The gate's `running` axis is
        # frozen at boot and says what is actually serving; its `deployed` axis
        # is read from disk and says what was put there." A client asking "what
        # build does the authority SERVE" must ask the same way, or the two
        # surfaces disagree about the same server — which is how this stayed
        # invisible while ai_version cheerfully reported build 188.
        from . import _deployed_from_authority
        from .build_authority import authority_url

        url = authority_url()
        resolved = _deployed_from_authority(_fetch_authority_axes(), url, "authority unreachable")
        latest = resolved.get("build")
        # `_axis_build` yields an int or the STRING "UNVERIFIED" — an unusable
        # answer must stay unusable here. Unknown is not a pass.
        out["latest_build"] = (
            latest if isinstance(latest, int) and not isinstance(latest, bool) else None
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract, like check_for_update
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if current is None:
        out["error"] = (
            "this runtime carries no build stamp, so it cannot prove which build "
            "it is running — refusing to call that an update"
        )
        return out
    if out["latest_build"] is None:
        out["error"] = "the authority named no deployed build — nothing to compare"
        return out
    out["update_available"] = out["latest_build"] != current
    return out


#: The operator's answer to "what should AIDOCS do when a newer release exists?"
#: DEFAULT IS `notify`, DELIBERATELY. #868's directive ("no more manual") is met
#: for the channel that caused the pain — the PRIVATE deploy, which now asks for
#: its own refresh. Defaulting every install to fetching and installing REMOTE
#: code without opt-in is a product decision, not an implementation detail.
UPDATE_POLICY_DEFAULT = "notify"


def _update_policy_setting() -> str:
    """The operator's STANDING update policy from the config store, or "".

    Config seam, same shape as build_authority._setting: factory + global layers
    (this question has no project scope in practice, but the setting is declared
    for both so a single project can differ). Never raises - a config read must
    not decide whether the enforcing package gets replaced, and a failure here
    returns "" so the caller falls through to the fail-closed default rather than
    to the most permissive reading.
    """
    try:
        from .config import get_setting

        return str(get_setting("runtime.update_policy", project_root=None, default="") or "")
    except Exception:  # noqa: BLE001 - a broken config read must never widen policy
        return ""
_UPDATE_POLICIES = ("auto", "notify", "pinned")


def act_on_update_state(
    state: dict | None,
    *,
    policy: str | None = None,
    request=None,
) -> str:
    """Consume the release-channel verdict per the operator's policy (#868 ch. B).

    ONE UPDATE PATH FOR BOTH CHANNELS (doctrine XXII). This installs nothing. It
    ASKS, through the same `request_runtime_refresh` the deploy uses, and the one
    consumer — which restarts only on ``code == 0`` and via the overlap path that
    defers the old backend's kill (#432) — does the work. The channels differ only
    in what triggers them, never in how the swap happens.

    FAIL-CLOSED ON EVERY UNCERTAINTY, because the thing being swapped is the
    package that ENFORCES:
      * ``pinned``            — the operator said no. A pin a heuristic can
                                override is not a pin.
      * an ERRORED check      — the channel was unreachable, so nothing was
                                learned. `check_for_update` is fail-soft and
                                records the error rather than raising, so a DNS
                                blip must not be allowed to decide when the gate
                                is replaced. Unknown is not a pass.
      * a MISSING verdict     — no state on record is not "nothing to do".
      * an UNKNOWN policy     — a typo'd setting fails CLOSED and NAMES itself.
                                Falling back to the most permissive reading is
                                how a setting becomes a suggestion.

    Returns the decision as a string so the caller can log WHY, never a bare
    bool: "requested" / "notify" / "pinned" / "current" / "unverified" /
    "policy_unknown:<value>". Never raises.
    """
    # WHERE THE POLICY LIVES, in precedence order (#903, 2026-08-25):
    #   1. an explicit argument      - a caller that already decided
    #   2. AIDOCS_UPDATE_POLICY      - a ONE-PROCESS override, for a shell
    #   3. runtime.update_policy      - THE OPERATOR'S STANDING CHOICE (config)
    #   4. UPDATE_POLICY_DEFAULT      - 'notify', the fail-closed default
    #
    # Until now only 2 and 4 existed, which made "set the policy to auto" a shell
    # variable that died with the process: the one switch that turns updates
    # hands-free could not outlive a service restart, so it was never really set.
    # A standing choice belongs in the config store like every other one.
    chosen = (
        policy
        or os.environ.get("AIDOCS_UPDATE_POLICY", "")
        or _update_policy_setting()
    ).strip().lower()
    chosen = chosen or UPDATE_POLICY_DEFAULT
    if chosen not in _UPDATE_POLICIES:
        return f"policy_unknown:{chosen}"
    if chosen == "pinned":
        return "pinned"
    if not isinstance(state, dict) or state.get("error"):
        return "unverified"
    if not state.get("update_available"):
        return "current"
    if chosen != "auto":
        return "notify"
    try:
        (request or request_runtime_refresh)(restart_daemon=True)
    except Exception:  # noqa: BLE001 — a failed ask must never stop supervision
        return "unverified"
    return "requested"


def check_for_update(*, fetch=None, timeout: float = 5.0) -> dict:
    """Compare the running version against the release channel's latest tag.

    Returns {current, latest, update_available, channel, checked_at, error,
    release_url} and persists the result to update.json (merged into
    service_status for the CLI, the hooks, and the dashboard). NEVER raises;
    NEVER installs.
    """
    fetch = fetch or _default_fetch
    channel = os.environ.get("AIDOCS_UPDATE_CHANNEL_URL", "").strip() or UPDATE_CHANNEL_DEFAULT
    current = _current_version()
    result: dict = {
        "current": current,
        "latest": None,
        "update_available": False,
        "channel": channel,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "release_url": None,
        "error": None,
    }
    try:
        payload = json.loads(fetch(channel, timeout))
        tag = str(payload.get("tag_name") or "").strip()
        if not tag:
            raise ValueError("channel payload has no tag_name")
        latest = ".".join(str(p) for p in _version_tuple(tag))
        result["latest"] = latest
        result["release_url"] = payload.get("html_url")
        result["update_available"] = bool(
            current and _version_tuple(latest) > _version_tuple(current)
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        result["error"] = f"{type(exc).__name__}: {exc}"
    try:
        tmp = update_state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
        tmp.replace(update_state_path())
    except OSError:
        pass
    _log(
        f"update-check: current={current or '?'} latest={result['latest'] or '?'} "
        f"available={result['update_available']} error={result['error'] or '-'}"
    )
    return result


def read_update_state() -> dict | None:
    try:
        return json.loads(update_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
