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
first request. Trade-off (documented): the watchdog runs code from ITS
start time; a deploy is picked up when the watchdog restarts (the client
falls back to local evaluation on any drift-induced failure anyway).

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

import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = 1
STATE_FILENAME = "hook_broker.json"
# Hook payloads are small (tool_input + ids); 4MB is a generous ceiling
# that still refuses a runaway/hostile stream.
MAX_REQUEST_BYTES = 4 * 1024 * 1024
_CONN_TIMEOUT_S = 5.0


def broker_state_path(state_dir: Path | None = None) -> Path:
    """Discovery file location — lives next to the watchdog's health.json."""
    if state_dir is None:
        from .aidocs_service import daemon_dir  # lazy: avoid import cycle

        state_dir = daemon_dir()
    return Path(state_dir) / STATE_FILENAME


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


class HookBroker:
    """Loopback-only, token-gated, one-JSON-line-per-direction listener."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        state_dir: Path | None = None,
        handler_factory=None,
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
        self._token = secrets.token_hex(16)
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._closing = threading.Event()
        # Hook evaluations are serialized: the subprocess model never ran
        # two evaluations concurrently in one process, and the underlying
        # stores assume as much. Correctness over parallelism.
        self._eval_lock = threading.Lock()
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
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)  # best-effort on Windows
        except OSError:
            pass
        tmp.replace(path)

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
        if request.get("kind") != "hook_eval":
            return _refuse(f"unknown kind {request.get('kind')!r}")
        # Token gate BEFORE any evaluation — compare_digest against replay
        # of the same-user shared secret from the state file.
        token = str(request.get("token") or "")
        if not secrets.compare_digest(token, self._token):
            return _refuse("bad token")
        payload = request.get("payload")
        if not isinstance(payload, dict):
            return _refuse("malformed payload (expected a JSON object)")

        t0 = time.perf_counter()
        with self._eval_lock:
            response = evaluate_hook_event(
                payload, handler_factory=self._handler_factory
            )
        return {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "response": response,
            # Echoes: the client only trusts a reply for the exact
            # session/root it asked about (floor: no cross-session reuse).
            "session_id": str(payload.get("session_id") or ""),
            "project_root": str(request.get("project_root") or ""),
            "eval_ms": (time.perf_counter() - t0) * 1000.0,
        }
