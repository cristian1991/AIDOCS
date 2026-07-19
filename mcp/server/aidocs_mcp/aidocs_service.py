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
    health.json   — {port, pid, watchdog_pid, status, started_at, release_marker}
    watchdog.log  — small rotating log (2 files)
    stop.flag     — presence asks the watchdog to exit cleanly
"""

from __future__ import annotations

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


def daemon_dir() -> Path:
    base = Path(os.environ.get("AIDOCS_DAEMON_DIR") or (Path.home() / ".aidocs" / "daemon"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def health_path() -> Path:
    return daemon_dir() / "health.json"


def stop_flag_path() -> Path:
    return daemon_dir() / "stop.flag"


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
    """Written by the daemon on bind (status=up) and by the watchdog on state
    changes (down / crash_looped / stopped). Hooks read this to nudge."""
    payload = {
        "port": port,
        "pid": pid,
        "watchdog_pid": extra.pop("watchdog_pid", None),
        "status": status,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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


def daemon_command(port: int) -> list[str]:
    return [windowless_python(), "-m", "aidocs_mcp.mcp_server", "--http", "--port", str(port)]


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

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="aidocs-proxy", daemon=True)
        self._thread.start()
        self._bound.wait(timeout=10)
        if self._bind_error is not None:
            raise RuntimeError(
                f"proxy could not bind 127.0.0.1:{self.port} — is a pre-proxy "
                f"daemon still holding it? ({self._bind_error})"
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

            return audited_popen(
                daemon_command(backend_port),
                fingerprint=("aidocs_service.py", "spawn", "subprocess.Popen"),
                reason="watchdog-daemon-supervision",
                popen=lambda *a, **kw: subprocess.Popen(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
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
    # Old backends awaiting a connection-aware drain (#432): (proc, backend, deadline).
    pending_drains: list[tuple] = []
    backoff = _BACKOFF_START_S
    marker = _release_marker()
    iterations = 0
    proxy.start()  # owns 127.0.0.1:<port> until the watchdog exits
    _log(f"watchdog up (port={port})")
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
                from .hook_broker import HookBroker

                candidate = HookBroker()
            broker = candidate.start() or candidate
            addr = getattr(broker, "address", None)
            _log(f"hook broker up (addr={addr})")
        except Exception as exc:  # noqa: BLE001 — broker is optional infra
            broker = None
            _log(f"hook broker failed to start: {exc!r} (hooks stay local)")
    # Release-channel check on start + every _UPDATE_CHECK_INTERVAL_S (check-
    # only; fail-soft — see check_for_update).
    try:
        update_check()
    except Exception:  # noqa: BLE001 — a broken checker must not stop supervision
        pass
    last_update_check = clock()

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

    try:
        while True:
            if max_iterations is not None and iterations >= max_iterations:
                return "stopped"
            iterations += 1

            backend = alloc_port()
            proc = spawn(backend)
            proxy.set_backend(backend)
            _log(f"daemon spawned pid={getattr(proc, 'pid', '?')} backend={backend}")
            while proc.poll() is None:
                if stop_flag_path().exists():
                    _log("stop.flag — terminating daemon")
                    _reap_pending(force=True)  # drain any deferred old backends too
                    _drain(proc, 15)
                    write_daemon_health(port=port, pid=0, status="stopped")
                    return "stopped"
                current = _release_marker()
                if current and marker and current != marker:
                    _log("release marker changed — overlap-restart onto new code")
                    marker = current
                    iterations += 1  # graceful spawns count toward the bound too
                    new_backend = alloc_port()
                    new_proc = spawn(new_backend)
                    if ready_wait(new_backend, new_proc):
                        # New daemon is serving: flip the proxy FIRST so the
                        # public port never stops answering, THEN drain the old.
                        proxy.set_backend(new_backend)
                        write_daemon_health(
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
                        backoff = _BACKOFF_START_S
                        _log(f"overlap-restart complete backend={new_backend}; old backend deferred-drain")
                    else:
                        # New build never became ready — keep the old daemon
                        # serving (same spirit as the breaker: no downtime for
                        # a broken deploy). Next marker change retries.
                        _log("new daemon failed readiness — keeping old daemon")
                        _drain(new_proc, 15)
                if clock() - last_update_check >= _UPDATE_CHECK_INTERVAL_S:
                    last_update_check = clock()
                    try:
                        update_check()
                    except Exception:  # noqa: BLE001
                        pass
                _reap_pending()  # terminate deferred old backends once their streams close
                sleep(_MARKER_POLL_S)

            # Daemon exited on its own = crash. Breaker, then backoff-restart.
            now = clock()
            crashes = [t for t in crashes if now - t <= _BREAKER_WINDOW_S]
            crashes.append(now)
            _log(f"daemon exited rc={proc.returncode} (crash {len(crashes)}/{_BREAKER_CRASHES})")
            if len(crashes) >= _BREAKER_CRASHES:
                _log("circuit breaker OPEN — build looks broken, not restarting")
                write_daemon_health(port=port, pid=0, status="crash_looped")
                return "crash_looped"
            write_daemon_health(port=port, pid=0, status="down")
            sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP_S)
    finally:
        if broker is not None:
            try:
                broker.close()
            except Exception:  # noqa: BLE001 — shutdown must stay clean
                pass
        proxy.close()


def request_stop() -> None:
    stop_flag_path().write_text("stop", encoding="utf-8")


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
    update = read_update_state()
    if update is not None:
        health["update"] = update
    return health


# ── Auto-update CHECKER (Empire directive 2026-07-06) ─────────────────────────
# The marker-watch above already drains+restarts when new code lands; this is
# the other half — is a newer RELEASE published on the channel? Check-only by
# law: no pip, no artifact fetch (aidocs-doctrine §XXIV — installs are signed/
# verified operator paths, never watchdog side effects). Fail-soft: a dead
# network never breaks the watchdog, the CLI, or the dashboard.

UPDATE_CHANNEL_DEFAULT = "https://api.github.com/repos/cristian1991/AIDOCS/releases/latest"
_UPDATE_CHECK_INTERVAL_S = 6 * 3600.0


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
