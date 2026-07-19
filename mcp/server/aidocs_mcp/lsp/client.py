"""Minimal JSON-RPC 2.0 / LSP client over stdio (§XXXII door internals).

ONE server per (project_root, language), kept in a module-level pool.
Everything is fail-open and hard-timeout bounded: no server, a broken
handshake, a slow request, or any exception yields None/empty and the
process is killed — a language server never blocks or raises to the door.

Lifecycle mirrors the proven ``csharp_roslyn_client`` contract:
warm → query → drain → evict. Servers are transient cache-fillers, not
resident fixtures (evict-after-materialize).

Subprocess note: the single ``subprocess.Popen`` call site lives in
``_LspServer._spawn`` and is routed through
``shell_egress_service.audited_popen`` (ledger row per spawn) with a
passthrough lambda — the SAME pattern csharp_roslyn_client uses so the
per-callsite fingerprint + inventory static laws pass. It is registered
in shell_egress_service.LEGACY_SUBPROCESS_FINGERPRINTS and
LEGACY_SUBPROCESS_CALLSITES.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

from ..shell_egress_service import audited_popen
from . import registry
from .domain import Diagnostic, DrainReport, Language, Location, ServerSpec, SymbolInfo

# Windows: keep spawns windowless (parity with csharp_roslyn_client).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_DEFAULT_REQUEST_TIMEOUT_S = 10.0
_DEFAULT_INIT_TIMEOUT_S = 30.0
_DIAGNOSTICS_WAIT_S = 3.0

# LSP SymbolKind int -> human string (subset; unknown -> "symbol").
_SYMBOL_KIND: dict[int, str] = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class",
    6: "method", 7: "property", 8: "field", 9: "constructor", 10: "enum",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
    15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object",
    20: "key", 21: "null", 22: "enum_member", 23: "struct", 24: "event",
    25: "operator", 26: "type_parameter",
}

_SEVERITY: dict[int, str] = {1: "error", 2: "warning", 3: "information", 4: "hint"}


def _path_to_uri(path: str) -> str:
    return "file:" + pathname2url(str(Path(path).resolve()))


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    raw = unquote(parsed.path)
    # Windows: strip the leading slash on "/C:/..." style paths.
    if os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return str(Path(raw))


class _LspServer:
    """One language-server subprocess for a (project_root, language).

    Not thread-safe across concurrent queries on the same instance beyond
    the request lock; the pool hands one instance per key. Every public
    method fails open (None) and never raises.
    """

    def __init__(
        self,
        argv: list[str],
        project_root: Path,
        language: Language,
        spec: ServerSpec | None = None,
        *,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT_S,
        init_timeout: float = _DEFAULT_INIT_TIMEOUT_S,
    ) -> None:
        self._argv = list(argv)
        self._root = Path(project_root)
        self._language = language
        self._spec = spec
        self._request_timeout = request_timeout
        self._init_timeout = init_timeout
        self._proc: subprocess.Popen[bytes] | None = None
        self._id = 0
        self._send_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list[Any]]] = {}
        self._pending_lock = threading.Lock()
        # uri -> latest diagnostics params
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diag_event = threading.Event()
        self._reader: threading.Thread | None = None
        self._alive = False

    # ── spawn / lifecycle ─────────────────────────────────────────

    def _spawn(self) -> subprocess.Popen[bytes] | None:
        try:
            return audited_popen(
                self._argv,
                fingerprint=("lsp/client.py", "_spawn", "subprocess.Popen"),
                reason="lsp-language-server-spawn",
                popen=lambda *a, **kw: subprocess.Popen(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=_WIN_NO_WINDOW,
            )
        except OSError:
            return None

    def start(self) -> bool:
        """Spawn + perform the initialize/initialized handshake.

        Returns True on a live, initialized server; False (and a killed
        process) on any failure.
        """
        proc = self._spawn()
        if proc is None:
            return False
        self._proc = proc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        try:
            init = self._request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": _path_to_uri(str(self._root)),
                    "rootPath": str(self._root),
                    "capabilities": {},
                    "initializationOptions": (
                        self._spec.initialization_options if self._spec else {}
                    ),
                },
                timeout=self._init_timeout,
            )
        except Exception:  # noqa: BLE001 — fail open
            init = None
        if init is None:
            self.evict()
            return False
        self._notify("initialized", {})
        self._alive = True
        return True

    def is_alive(self) -> bool:
        return bool(self._alive and self._proc is not None and self._proc.poll() is None)

    def evict(self) -> None:
        """Best-effort graceful shutdown then hard kill. Idempotent."""
        self._alive = False
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    self._request("shutdown", None, timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._notify("exit", None)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    proc.wait(timeout=3.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            self._proc = None

    # ── framing / transport ───────────────────────────────────────

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buf = proc.stdout
        try:
            while True:
                headers: dict[bytes, bytes] = {}
                while True:
                    line = buf.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    if b":" in line:
                        k, _, v = line.partition(b":")
                        headers[k.strip().lower()] = v.strip()
                try:
                    length = int(headers.get(b"content-length", b"0"))
                except ValueError:
                    length = 0
                body = buf.read(length) if length else b""
                if not body:
                    continue
                try:
                    msg = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._dispatch(msg)
        except (OSError, ValueError):
            return

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            rid = msg.get("id")
            try:
                rid_int = int(rid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return
            with self._pending_lock:
                slot = self._pending.pop(rid_int, None)
            if slot is not None:
                evt, container = slot
                container.append(msg)
                evt.set()
            return
        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            uri = params.get("uri")
            if isinstance(uri, str):
                self._diagnostics[uri] = params.get("diagnostics") or []
                self._diag_event.set()

    def _next_id(self) -> int:
        with self._send_lock:
            self._id += 1
            return self._id

    def _write(self, msg: dict[str, Any]) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        body = json.dumps(msg).encode("utf-8")
        frame = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        try:
            with self._send_lock:
                proc.stdin.write(frame)
                proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _notify(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:
        rid = self._next_id()
        evt = threading.Event()
        container: list[Any] = []
        with self._pending_lock:
            self._pending[rid] = (evt, container)
        if not self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            with self._pending_lock:
                self._pending.pop(rid, None)
            return None
        if not evt.wait(timeout if timeout is not None else self._request_timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            return None
        if not container:
            return None
        msg = container[0]
        if "error" in msg:
            return None
        return msg.get("result")

    # ── queries ───────────────────────────────────────────────────

    def document_symbols(self, file_path: str) -> list[SymbolInfo] | None:
        if not self._open_document(file_path):
            return None
        result = self._request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": _path_to_uri(file_path)}},
        )
        if not isinstance(result, list):
            return None
        return _parse_symbols(result, file_path)

    def references(self, file_path: str, line: int, char: int) -> list[Location] | None:
        if not self._open_document(file_path):
            return None
        result = self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": _path_to_uri(file_path)},
                "position": {"line": int(line), "character": int(char)},
                "context": {"includeDeclaration": True},
            },
        )
        if not isinstance(result, list):
            return None
        out: list[Location] = []
        for item in result:
            loc = _parse_location(item)
            if loc is not None:
                out.append(loc)
        return out

    def diagnostics(self, file_path: str, content: str) -> list[Diagnostic] | None:
        uri = _path_to_uri(file_path)
        self._diag_event.clear()
        self._diagnostics.pop(uri, None)
        if not self._open_document(file_path, content=content, force=True):
            return None
        deadline = time.monotonic() + _DIAGNOSTICS_WAIT_S
        while time.monotonic() < deadline:
            if uri in self._diagnostics:
                break
            self._diag_event.wait(0.1)
            self._diag_event.clear()
        raw = self._diagnostics.get(uri)
        if raw is None:
            return None
        out: list[Diagnostic] = []
        for d in raw:
            parsed = _parse_diagnostic(d, file_path)
            if parsed is not None:
                out.append(parsed)
        return out

    def _open_document(self, file_path: str, content: str | None = None, force: bool = False) -> bool:
        if content is None:
            try:
                content = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
        params = {
            "textDocument": {
                "uri": _path_to_uri(file_path),
                "languageId": self._language.value,
                "version": 1,
                "text": content,
            }
        }
        self._notify("textDocument/didOpen", params)
        return True


# ── parsers (module-level, pure) ───────────────────────────────────


def _range_lines(rng: dict[str, Any]) -> tuple[int, int]:
    start = (rng or {}).get("start") or {}
    end = (rng or {}).get("end") or {}
    return int(start.get("line", 0)), int(end.get("line", start.get("line", 0)))


def _parse_symbols(
    items: list[Any], file_path: str, container: str | None = None
) -> list[SymbolInfo]:
    out: list[SymbolInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        kind = _SYMBOL_KIND.get(int(item.get("kind", 0) or 0), "symbol")
        if "location" in item:  # SymbolInformation
            loc = item["location"]
            path = _uri_to_path(loc.get("uri", "")) if isinstance(loc, dict) else file_path
            line, line_end = _range_lines(loc.get("range", {}) if isinstance(loc, dict) else {})
            out.append(
                SymbolInfo(name, kind, path, line, line_end, item.get("containerName") or container)
            )
        else:  # DocumentSymbol (hierarchical)
            rng = item.get("range", {})
            line, line_end = _range_lines(rng)
            out.append(SymbolInfo(name, kind, file_path, line, line_end, container))
            children = item.get("children")
            if isinstance(children, list) and children:
                out.extend(_parse_symbols(children, file_path, container=name))
    return out


def _parse_location(item: Any) -> Location | None:
    if not isinstance(item, dict):
        return None
    uri = item.get("uri")
    if not isinstance(uri, str):
        return None
    rng = item.get("range") or {}
    start = rng.get("start") or {}
    return Location(_uri_to_path(uri), int(start.get("line", 0)), int(start.get("character", 0)))


def _parse_diagnostic(d: Any, file_path: str) -> Diagnostic | None:
    if not isinstance(d, dict):
        return None
    rng = d.get("range") or {}
    start = rng.get("start") or {}
    severity = _SEVERITY.get(int(d.get("severity", 1) or 1), "error")
    code = d.get("code")
    return Diagnostic(
        path=file_path,
        line=int(start.get("line", 0)),
        severity=severity,
        message=str(d.get("message", "")),
        code=str(code) if code is not None else None,
    )


# ── module-level pool (one server per project × language) ──────────

_POOL: dict[tuple[str, Language], _LspServer] = {}
_POOL_LOCK = threading.Lock()


def warm(project_root: Path, language: Language) -> _LspServer | None:
    """Return a live server for (project, language), spawning if needed.

    Fail-open: returns None when no server binary is installed or the
    handshake fails. Reuses a pooled live server; replaces a dead one.
    """
    key = (str(project_root), language)
    with _POOL_LOCK:
        existing = _POOL.get(key)
        if existing is not None and existing.is_alive():
            return existing
        if existing is not None:
            existing.evict()
            _POOL.pop(key, None)
    resolved = registry.resolve_server(language)
    if resolved is None:
        return None
    binary, spec = resolved
    argv = [binary, *spec.args]
    server = _LspServer(argv, Path(project_root), language, spec)
    if not server.start():
        server.evict()
        return None
    with _POOL_LOCK:
        # Double-check no concurrent warm won the race.
        winner = _POOL.get(key)
        if winner is not None and winner.is_alive():
            server.evict()
            return winner
        _POOL[key] = server
    return server


def evict(project_root: Path, language: Language | None = None) -> DrainReport:
    """Drain+evict warm servers for a project (one language or all)."""
    root = str(project_root)
    evicted = 0
    langs: list[str] = []
    with _POOL_LOCK:
        keys = [
            k
            for k in _POOL
            if k[0] == root and (language is None or k[1] is language)
        ]
        servers = [(k, _POOL.pop(k)) for k in keys]
    for (_, lang), server in servers:
        try:
            server.evict()
            evicted += 1
            langs.append(lang.value)
        except Exception:  # noqa: BLE001 — eviction is best-effort
            pass
    return DrainReport(evicted=evicted, languages=tuple(langs))


def evict_all_projects() -> None:
    """Tear down every pooled server (test helper + process shutdown)."""
    with _POOL_LOCK:
        servers = list(_POOL.values())
        _POOL.clear()
    for server in servers:
        try:
            server.evict()
        except Exception:  # noqa: BLE001
            pass
