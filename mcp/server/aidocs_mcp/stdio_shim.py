"""Per-window stdio shim: the host's identity, captured at spawn (#758).

WHY THIS EXISTS. The daemon serves STATELESS http on purpose (#435) so a deploy
hot-swap is invisible to the client. The cost was never carried through: with no
transport session, the server has no way to learn WHICH CONVERSATION is calling.
Its only remaining source was ``query_gate.last_host_session_id``, written by the
claude_hook during UPS -- so a slow hook broker did not DEGRADE authorization, it
DELETED it. Measured 2026-08-06: `ai_session(connect)` answering
``already_active: true`` while every gated tool refused ``managed_mode_not_active``,
unfixable by restart, because a restart clears the in-process global and the bridge
is still empty.

WHAT THE PROBES SETTLED (do not re-litigate without re-running them):
  * The transport session is NOT an anchor: four tool calls produced four different
    ``ctx.session_id`` values. stateless_http mints one per REQUEST.
  * The identity was in the ENVIRONMENT the whole time. A stdio-spawned server
    receives ``CLAUDE_CODE_SESSION_ID``, and two windows in the same project carry
    DIFFERENT ids -- per-CONVERSATION, which is exactly what an isolation boundary
    needs and what a project-wide ``.mcp.json`` can never be.

THE SHAPE:

    window --stdio--> THIS SHIM (one per window, inherits the env)
                          --HTTP + X-Aidocs-Host-Session/-Host-Kind-->
                      :8748 proxy --> daemon

CAPTURED, NEVER ASSERTED (operator ruling 2026-08-06). The value lives in another
process's environment and never enters model context, so an agent cannot claim
another conductor's identity by restating it. That is why an ``ai_session`` parameter
was rejected: a boundary the occupant can restate is not a boundary.

#435 SURVIVES: the shim holds the stdio connection while the proxy flips backends
underneath, so the window never sees a deploy.

KNOWN LIMIT, inherited from the stateless daemon and already documented at
mcp_server.py:7458 -- POST-only means no standalone GET stream, so SERVER-INITIATED
messages (tools/list_changed and friends) cannot reach the client through this shim.
Tool results carry their own notifications, which is how AIDOCS already works.

FAILURE POSTURE IS THE POINT. A shim that hangs is worse than no shim: it freezes the
operator's window with no explanation. Every failure path here answers with a JSON-RPC
error naming the daemon and the remedy, and `initialize` degrades LOUDLY rather than
silently pretending the server is present.
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import hashlib
import urllib.request

# Stdlib only, deliberately: this process is spawned by the HOST, in whatever
# interpreter the host was configured with. A third-party import here turns a
# dependency drift into "MCP server failed to start" with no explanation.
# ...which is why the ONE package import below is worth its own paragraph.
# ``window_key`` is stdlib-only by the same construction (ctypes + os + sys, and
# a test parses its import list to keep it that way). It is IMPORTED rather than
# copied in because the alternative is two definitions of one identity, kept in
# step by hand -- the twin pattern this codebase already pays for elsewhere, and
# the more dangerous version of it: a duplicate that drifts would let the shim
# and the SessionStart hook name the SAME window differently, which is precisely
# the agreement the whole design rests on.
from .window_key import derive_window_key

DEFAULT_ENDPOINT = "http://127.0.0.1:8748/mcp"

HEADER_HOST_SESSION = "X-Aidocs-Host-Session"
HEADER_HOST_KIND = "X-Aidocs-Host-Kind"
HEADER_HOST_ENTRYPOINT = "X-Aidocs-Host-Entrypoint"
# #849: the caller's PROJECT, which the identity work never carried. The three
# headers above answer WHO; the daemon still had to guess WHERE, and its fallback
# is Path.cwd() (#761) -- on a SHARED daemon that is the DAEMON's directory, so
# every project served by one daemon collapsed onto one root. Measured: a connect
# from DentalClinic-WebApp answered `cross_project: DentalClinic-WebApp` while the
# caller's cwd WAS that project, then bound managed mode under a different root
# than the gate reads.
#
# Same custody as the identity headers: the value is the operator-written
# AIDOCS_PROJECT_ROOT in this process's environment (from .mcp.json). CAPTURED,
# NEVER ASSERTED -- it never enters model context, so an agent cannot claim
# another project by restating it.
HEADER_PROJECT_ROOT = "X-Aidocs-Project-Root"

# #876: WHICH WINDOW. The three identity headers name a CONVERSATION, and a
# conversation is not durable -- measured 2026-08-23 in one live window,
# `/resume` rotated it, `/clear` rotated it again, and `/mcp` respawned this
# shim onto a third value, producing FOUR distinct host ids in a single call.
# Across all three rotations the Claude Code process was unchanged, and this
# shim is one of its descendants, so it can name the window without asking
# anyone. See `window_key` for the measurement and the two-component rule.
#
# SAME CUSTODY AS THE OTHER HEADERS, by a different route: this value is derived
# from the PROCESS TREE, which is even further out of the model's reach than an
# environment variable. It never enters model context, so an agent cannot claim
# another window by restating it.
#
# PHASE 1 IS ADDITIVE: the daemon records this and nothing reads it to make a
# decision.
HEADER_WINDOW = "X-Aidocs-Window"

# #833: the transport is a THIRD in-force layer, and it was invisible.
#
# MEASURED 2026-08-19. A fix can be committed, deployed, installed, verified
# present in the artefact, and STILL not be what handles a request, because this
# shim is spawned by the HOST and lives exactly as long as that host window.
# `aidocs service restart` replaces the watchdog and the daemon; it cannot touch
# a process the host owns. On that day every live shim had started at 16:05,
# 16:16 and 16:33 while the fixed package was installed at 18:54 -- so the byte
# path kept corrupting UTF-8 (#830) after every remedy had been applied, and
# every instrument AIDOCS had reported green, because they all stop at layer 2.
#
# THIS IS A TRIPWIRE, NOT A FINGERPRINT, and the distinction is deliberate. It
# does not hash the tree: package_code_identity already does that, and
# duplicating it here would be the twin pattern this codebase keeps paying for
# -- and this module is STDLIB ONLY BY CONSTRUCTION (see the note above), so it
# must not import the machinery that owns hashing. Two stat() calls answer the
# question actually being asked: "was the package REPLACED under me?" A
# reinstall rewrites every file (measured: all 492 .py files took the same new
# mtime), so __init__.py moving is a sufficient and extremely cheap signal.
#
# What it therefore CANNOT see, stated so no caller over-reads it: a single
# edited file that leaves both stat targets untouched. That is the LOADED-drift
# question, and hook_broker.package_code_identity is the check that answers it.
HEADER_TRANSPORT_STAMP = "X-Aidocs-Transport-Stamp"

#: What the shim and the daemon must AGREE ON — the wire contract (#909).
HEADER_TRANSPORT_CONTRACT = "X-Aidocs-Transport-Contract"

#: Every header this shim can send. THE CONTRACT IS THIS SET, and the guard test
#: derives the same list from the HEADER_* constants, so adding a header without
#: adding it here FAILS rather than silently shipping an unversioned change.
_CONTRACT_HEADERS = (
    HEADER_HOST_SESSION,
    HEADER_HOST_KIND,
    HEADER_HOST_ENTRYPOINT,
    HEADER_PROJECT_ROOT,
    HEADER_WINDOW,
    HEADER_TRANSPORT_STAMP,
    HEADER_TRANSPORT_CONTRACT,
)

#: Bump ONLY for a change the header NAMES cannot express: same headers, new
#: meaning. Everything else is caught by the fingerprint below on its own.
TRANSPORT_CONTRACT_EPOCH = 1


def _contract_fingerprint() -> str:
    """A stable id for the wire contract, DERIVED so it cannot drift.

    A hand-maintained version number is a promise someone eventually forgets;
    this changes exactly when the header set changes, which is the only thing
    that can make an old shim genuinely unable to talk to a new daemon.
    """
    joined = "\n".join(sorted(h.lower() for h in _CONTRACT_HEADERS))
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
    return f"{TRANSPORT_CONTRACT_EPOCH}.{digest}"


TRANSPORT_CONTRACT = _contract_fingerprint()

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _tree_stamp(pkg_dir: str) -> str:
    """Cheap replacement-detector for a package directory. "" when unknown."""
    try:
        init_ns = os.stat(os.path.join(pkg_dir, "__init__.py")).st_mtime_ns
        self_ns = os.stat(os.path.join(pkg_dir, "stdio_shim.py")).st_mtime_ns
    except OSError:
        return ""
    return f"{init_ns}.{self_ns}"


# Captured at IMPORT, which is the whole point: it records the tree this process
# actually loaded, before anything could replace it underneath.
LOADED_TRANSPORT_STAMP = _tree_stamp(_PKG_DIR)

#: THE VERDICT BELONGS TO A REQUEST, NOT TO THE PROCESS (#902, 2026-08-24).
#:
#: This was a module-level dict that `record_transport_stamp` overwrote on every
#: call, so the reader got whichever window asked the daemon LAST. The daemon is
#: ONE SHARED PROCESS serving every window — measured on the operator's box:
#: three live agents across two AIDOCS sessions — and transport staleness is
#: explicitly PER WINDOW (see `transport_freshness`'s own reason text). So
#: `ai_version` in a current window could report a stale window's verdict, and
#: vice versa: telling the operator to reconnect a shim that is fine while
#: hiding the one that is not.
#:
#: Same defect family as #902's root cause one file over — a shared mutable
#: global standing in for per-request state in a multi-tenant daemon — and the
#: remedy is the one the identity stack already uses and proves works on this
#: transport: a request-scoped ContextVar.
#:
#: NO FALLBACK. A context that recorded nothing reports the honest unknown; it
#: does not borrow the last verdict it can find. That borrowing IS the bug.
_UNKNOWN_VERDICT: dict = {
    "fresh": None,
    "reason": "no shim request seen on this request",
    "sent": "",
    "now": "",
}

_request_transport_verdict: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_request_transport_verdict",
    default=None,
)


def transport_freshness(sent: str, contract: str = "") -> dict:
    """Can the CLIENT that sent this still TALK to this daemon? (#909)

    THE QUESTION CHANGED, and the old one was the wrong question. This used to
    compare a TREE STAMP -- the mtimes of __init__.py and stdio_shim.py -- so any
    reinstall marked every window's shim stale, and `ai_version` told the operator
    to reconnect after EVERY update. Measured: the 188->195 self-update flagged
    this window while the shim relayed perfectly throughout.

    THE SHIM IS A RELAY. It resolves identity, builds headers, forwards JSON-RPC.
    It runs no tool logic and no gate logic, and it plays NO PART IN UPDATING --
    the daemon pulls, verifies, installs and restarts itself. So a shim running
    older code is not, by itself, a problem at all. The only thing that can make
    it genuinely unable to serve is a change to WHAT IT SENDS: the header
    contract. That happened once (#902, a shim predating the window header, whose
    lease was then correctly refused) and it is rare.

    So the verdict is now about the CONTRACT, and `fresh: False` means "this shim
    cannot speak to this daemon" rather than "a file changed". Three-valued as
    before -- unknown is not a pass -- and the tree stamp is still carried, purely
    as diagnostics, because knowing the shim's tree differs is useful even when
    it is harmless.
    """
    now = _tree_stamp(_PKG_DIR)
    if not contract:
        return {
            "fresh": None,
            "reason": (
                "this shim predates the transport-contract check, so its"
                " compatibility cannot be established. It may well be relaying"
                " fine; reconnecting the AIDOCS MCP server in the host UI is what"
                " would settle it."
                if sent
                else "no shim request seen on this request"
            ),
            "sent": sent,
            "now": now,
            "contract": contract,
            "contract_now": TRANSPORT_CONTRACT,
        }
    if contract == TRANSPORT_CONTRACT:
        return {
            "fresh": True,
            "reason": "",
            "sent": sent,
            "now": now,
            "contract": contract,
            "contract_now": TRANSPORT_CONTRACT,
        }
    return {
        "fresh": False,
        "reason": (
            "the stdio shim serving this window speaks an OLDER TRANSPORT "
            f"CONTRACT ({contract}) than this daemon requires "
            f"({TRANSPORT_CONTRACT}) -- the set of identity headers it sends has "
            "changed, so this window may be misidentified or refused. This is NOT "
            "about the shim running older code in general, which is harmless and "
            "expected after every update. `aidocs service restart` cannot fix it: "
            "the shim is spawned by the host and lives as long as the window. "
            "REMEDY IS AN OPERATOR ACTION AND NO AGENT CAN PERFORM IT: a person "
            "must reconnect the AIDOCS MCP server in their host UI, which "
            "respawns this shim. Staleness is PER WINDOW."
        ),
        "sent": sent,
        "now": now,
        "contract": contract,
        "contract_now": TRANSPORT_CONTRACT,
    }


def record_transport_stamp(sent: str, contract: str = "") -> dict:
    """Daemon-side: evaluate this REQUEST's verdict and bind it to this request."""
    verdict = transport_freshness(sent, contract)
    _request_transport_verdict.set(verdict)
    return verdict


def last_transport_verdict() -> dict:
    """THIS REQUEST's transport verdict, for ai_version to report.

    Returns a COPY so a reader cannot mutate the bound verdict, and the honest
    unknown when this request recorded none — never another request's answer.
    A window relaying current code must not be told to reconnect because a
    different window is stale, and a stale one must not be reassured by a
    neighbour's success.
    """
    return dict(_request_transport_verdict.get() or _UNKNOWN_VERDICT)


_stdout_lock = threading.Lock()


def resolve_host_identity() -> tuple[str, str, str]:
    """(host_session_id, host_kind, entrypoint) from the SPAWN ENVIRONMENT.

    Ordered by host, most specific first. Every source here is written by the
    HOST into this process's environment -- none of it is reachable by the model.

    Returns empty strings when a host is unknown rather than guessing: an
    invented identity is worse than an absent one, because the gate treats
    absence as "cannot prove identity" and refuses, while a guess would
    silently attach this window to somebody else's grants.
    """
    env = os.environ

    # Claude Code (measured 2026-08-06 via a stdio probe).
    sid = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if sid:
        entry = (env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip()
        return sid, "claude_code", entry

    # OpenCode: `opencode --session <id>`.
    sid = (env.get("OPENCODE_SESSION_ID") or "").strip()
    if sid:
        return sid, "opencode", (env.get("OPENCODE_ENTRYPOINT") or "").strip()

    # Explicit override, for hosts we do not know yet and for tests. This is an
    # OPERATOR-set env var on the spawned process, not an agent-supplied argument.
    sid = (env.get("AIDOCS_HOST_SESSION_ID") or "").strip()
    if sid:
        return sid, (env.get("AIDOCS_HOST_KIND") or "unknown").strip(), ""

    return "", "", ""


def resolve_endpoint() -> str:
    """The daemon URL, with the project root carried as a query param.

    The existing registration already does this
    (``/mcp?root=D%3A%5CProjects%5CActive%5CAIDOCS``), so the shim preserves the
    convention rather than inventing a second one.
    """
    endpoint = (os.environ.get("AIDOCS_MCP_ENDPOINT") or DEFAULT_ENDPOINT).strip()
    root = (
        os.environ.get("AIDOCS_PROJECT_ROOT")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or ""
    ).strip()
    if root and "root=" not in endpoint:
        sep = "&" if "?" in endpoint else "?"
        endpoint = f"{endpoint}{sep}root={urllib.parse.quote(root, safe='')}"
    return endpoint


def _force_utf8_streams() -> None:
    """Pin stdin/stdout to UTF-8. JSON-RPC IS UTF-8; Windows stdio is not.

    #830 -- THE CORRUPTION. Measured on the runtime interpreter this shim runs
    under (CPython 3.13.12, the owned venv):

        stdin = cp1252   stdout = cp1252   utf8_mode = 0

    So every request line was decoded with cp1252 before json.loads ever saw
    it. Raw UTF-8 in a tool ARGUMENT -- the content an agent is trying to write
    -- was mangled on the way IN, and the edit tools then faithfully wrote the
    mangled text to disk. Reported from DentalClinic-WebApp as an Italian
    capital E-grave and a Romanian a-breve corrupted in shipped .resx files.

    WHY `Write` WAS CLEAN AND `ai_replace` WAS NOT, which is the observation
    that located this: Write/Read/Edit are the HOST's native tools and never
    traverse this process. Every AIDOCS edit tool is an MCP call and does. The
    reporter's "Write handles UTF-8 correctly; ai_replace / ai_batch_edit
    corrupt it" was therefore not a property of those tools at all -- it was a
    property of the transport underneath them.

    Bytes cp1252 cannot even represent (0x81, 0x8D, 0x8F, 0x90, 0x9D) become
    lone surrogates via the interpreter's surrogateescape handler, which is how
    an attempt to WRITE THIS FIX failed twice with U+DC9D (= 0x9D) in the
    payload.

    The CLI already learned this and pins its own streams
    (cli._force_utf8_stdout, whose docstring describes exactly this hazard).
    The shim -- which relays EVERY byte between the host and the daemon -- did
    not. One sibling hardened, the other not.

    errors="replace" matches that precedent and keeps a malformed frame from
    killing the transport; a replacement char is not silent, because U+FFFD is
    a mojibake signature and the edit screen refuses on it (#830 detector).
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _write(message: dict) -> None:
    """One JSON-RPC message to stdout. Serialised: threads share this pipe."""
    line = json.dumps(message)
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _error(msg_id: object, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}


def _parse_response(body: bytes, content_type: str) -> list[dict]:
    """Return the JSON-RPC messages in an HTTP response.

    Streamable HTTP answers a POST either as a single JSON object or as an SSE
    stream of ``data:`` frames, and the daemon may use either. Handling only the
    first is the kind of omission that works on every test and fails on the one
    call that matters.
    """
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    if "text/event-stream" in (content_type or "").lower():
        out: list[dict] = []
        for chunk in text.split("\n"):
            line = chunk.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                out.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        return out
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def forward(endpoint: str, headers: dict[str, str], message: dict, timeout: float) -> None:
    """POST one message; write whatever comes back.

    A NOTIFICATION (no id) expects no reply, and the daemon answers 202 with an
    empty body -- so silence here is success, not a dropped message.
    """
    msg_id = message.get("id")
    data = json.dumps(message).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            ctype = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        replies = _parse_response(body, exc.headers.get("Content-Type", "") if exc.headers else "")
        if replies:
            for reply in replies:
                _write(reply)
            return
        if msg_id is not None:
            _write(_error(msg_id, -32000, f"aidocs daemon returned HTTP {exc.code}: {endpoint}"))
        return
    except Exception as exc:  # noqa: BLE001 -- URLError, socket timeout, DNS, ...
        # THE FAILURE THAT MATTERS. If the daemon is down the operator must be
        # told, in the window, with the remedy -- never left waiting.
        if msg_id is not None:
            _write(
                _error(
                    msg_id,
                    -32001,
                    (
                        f"aidocs daemon unreachable at {endpoint} ({exc!r}). "
                        "Start it with `aidocs service start`."
                    ),
                )
            )
        return

    for reply in _parse_response(body, ctype):
        _write(reply)


def resolve_project_root() -> str:
    """The project this window belongs to, from the operator-set environment.

    `.mcp.json` already pins ``AIDOCS_PROJECT_ROOT`` per project — that is how the
    host is told which tree this shim serves. It was simply never forwarded, so
    the daemon had to guess (#849).

    Returns "" when unset rather than guessing, for the same reason
    ``resolve_host_identity`` does: an invented project is worse than an absent
    one. Absent leaves today's resolution untouched; invented would bind managed
    mode into somebody else's repository.

    NOTE the name collision with ``mcp_server_runtime_helpers.resolve_project_root``
    is deliberate — that one RESOLVES a root by discovery on the daemon side, this
    one only READS what the host declared. They are the two ends of the same wire.
    """
    return (os.environ.get("AIDOCS_PROJECT_ROOT") or "").strip()


def resolve_window() -> str:
    """The WINDOW this shim lives inside, or "" — never a substitute (#876).

    Derived by walking this process's own ancestry to the ``claude.exe`` that
    spawned it; the key is ``<host pid>:<host creation filetime>``, both halves
    always, because Windows recycles pids.

    Returns "" when the window cannot be proven — a differently-launched host, a
    wrapper, a remote session, a non-win32 box. Operator law 2026-08-23:
    "fallbacks can stamp wrong data and we cannot tell from where. identity has
    no fallback." So nothing is substituted here: not the captured conversation
    id, not the bare pid, not a placeholder. The caller sends no header at all,
    which leaves the daemon exactly where it is today.

    The try/except is the shim's standing failure posture, not a swallow: a
    process that dies while BUILDING HEADERS freezes the operator's window with
    no explanation, and an additive diagnostic header must never be able to do
    that. The degraded state is "absent", which is a state the daemon already
    handles — it is what every request before this change looked like.
    """
    try:
        return derive_window_key()[0]
    except Exception:  # noqa: BLE001 -- an unprovable window is an absent header
        return ""


def build_forward_headers() -> dict[str, str]:
    """The header set every forwarded message carries.

    Extracted from ``main`` (#849) so the wire contract is testable without
    spawning a shim: what the daemon receives is exactly what this returns.
    """
    host_session_id, host_kind, entrypoint = resolve_host_identity()
    headers = {
        "Content-Type": "application/json",
        # Both are required by streamable HTTP: the server may answer a POST
        # with either shape, so advertising only one invites a 406.
        "Accept": "application/json, text/event-stream",
    }
    # #833: sent on EVERY request, independently of host identity. A window with
    # no session id still has a transport, and a stale one corrupts its bytes
    # just the same -- gating this on host_session_id would blind exactly the
    # unidentified windows that already get the least diagnosis.
    if LOADED_TRANSPORT_STAMP:
        headers[HEADER_TRANSPORT_STAMP] = LOADED_TRANSPORT_STAMP
    # #909: the CONTRACT this shim speaks. Sent unconditionally — it is a
    # property of the code, not of the window, and a shim that cannot say which
    # contract it speaks is exactly the one the daemon must not assume about.
    headers[HEADER_TRANSPORT_CONTRACT] = TRANSPORT_CONTRACT
    # #849: the project is INDEPENDENT of identity, and deliberately not nested
    # under the `if host_session_id` branch below. A window whose host id is
    # unknown still belongs to a known project, and telling the daemon WHERE it
    # is remains correct even when WHO is unprovable — that is precisely the
    # window that gets the least diagnosis today.
    project_root = resolve_project_root()
    if project_root:
        headers[HEADER_PROJECT_ROOT] = project_root
    # #876: WHICH WINDOW — same treatment as the transport stamp above, and for
    # the same reason. A window with no session id still IS a window; gating
    # this on identity would blind exactly the unidentified windows that get the
    # least diagnosis today, and those are the ones phase 2 has to rescue. Sent
    # whenever it RESOLVES, gated on nothing else. When it does not resolve the
    # header is ABSENT rather than empty: the daemon must be able to tell "not
    # sent" from "sent blank", the same three-valued discipline
    # `transport_freshness` follows.
    window = resolve_window()
    if window:
        headers[HEADER_WINDOW] = window
    if host_session_id:
        headers[HEADER_HOST_SESSION] = host_session_id
        headers[HEADER_HOST_KIND] = host_kind or "unknown"
        if entrypoint:
            headers[HEADER_HOST_ENTRYPOINT] = entrypoint
    return headers


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    endpoint = resolve_endpoint()
    host_session_id, host_kind, entrypoint = resolve_host_identity()
    timeout = float(os.environ.get("AIDOCS_SHIM_TIMEOUT", "120"))

    # #849: one builder, so what the daemon receives is exactly what
    # build_forward_headers() returns and a test can assert the wire contract
    # without spawning a shim.
    headers = build_forward_headers()
    if not host_session_id:
        # Announce the gap on stderr (which the host logs) instead of failing:
        # the window must still work, and the gate already refuses honestly when
        # it cannot prove identity. Silent degradation is what produced #758.
        print(
            "aidocs stdio shim: no host session id in the environment "
            "(looked for CLAUDE_CODE_SESSION_ID / OPENCODE_SESSION_ID / "
            "AIDOCS_HOST_SESSION_ID). Identity-scoped tools will refuse.",
            file=sys.stderr,
        )

    if "--print-identity" in argv:
        # Diagnostic: what WOULD this window send? Answers the operator's
        # "is it even picking me up?" without starting a session.
        print(json.dumps({
            "endpoint": endpoint,
            "host_session_id": host_session_id,
            "host_kind": host_kind,
            "entrypoint": entrypoint,
            "transport_stamp": LOADED_TRANSPORT_STAMP,
        }, indent=2))
        return 0

    _force_utf8_streams()

    threads: list[threading.Thread] = []
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # not ours to interpret; the host owns its framing
        # One thread per message: the host may pipeline, and JSON-RPC permits
        # out-of-order replies. Serialising here would make a slow tool call
        # block every other request on the window.
        thread = threading.Thread(
            target=forward,
            args=(endpoint, headers, message, timeout),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
        threads = [t for t in threads if t.is_alive()]

    for thread in threads:
        thread.join(timeout=timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
