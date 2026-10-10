"""What does the MCP client ACTUALLY send us? (#758)

THE QUESTION. AIDOCS's local-HTTP path has no host-identity capture point: the
daemon is stateless by design (#435), so there is no transport session, and the
only remaining source is the query-gate bridge the claude_hook writes during UPS
-- which means a slow broker does not degrade identity, it DELETES it.

MCP 2026-07-28 removed Mcp-Session-Id and made every request self-describing via
`_meta`. The announcement documents exactly one key,
`io.modelcontextprotocol/clientInfo` = {name, version}, which is the APPLICATION
and not the conversation. But clients are free to add their own keys, and an
announcement is not an implementation. So: stop reasoning about what Claude Code
sends and RECORD what it sends.

WHAT THIS ANSWERS, per real tool call:
  * are there HTTP headers, and is anything conversation-scoped among them;
  * is there a request `_meta`, and what keys does it actually carry;
  * does the client identify itself (clientInfo) so host_kind could stop being
    sniffed from the environment (#587).

NOT A FIX AND NOT A CAPTURE PATH. This only observes. Identity must be CAPTURED
by AIDOCS and never ASSERTED by the agent (operator ruling 2026-08-06): a value
the occupant can restate is not an isolation boundary. If the probe finds a
conversation id here, that is a candidate; if it finds only clientInfo, then
#758's option (d) -- a per-connection credential, as webmcp already does -- is
the remaining answer.

OFF BY DEFAULT. One env lookup per call unless AIDOCS_META_PROBE names a file.
Never raises: a probe that breaks the server it is probing is worthless.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_ENV = "AIDOCS_META_PROBE"

# Diagnostics stay INSIDE the project (operator ruling 2026-08-06). A probe that
# writes to a drive-rooted temp path is invisible to the repo, survives cleanup,
# and can never be reviewed in a diff. Resolved from THIS FILE so it is correct
# whatever working directory the daemon was started in.
_PROBE_DIR = Path(__file__).resolve().parents[2] / "scratch"


def capture(where: str, tool_name: str = "") -> None:
    """Append one JSON line describing this request's identity-bearing surface."""
    target = os.environ.get(_ENV)
    if not target:
        return
    # A bare filename lands in the project's scratch dir; an absolute path is
    # honoured as given, so an operator can still aim it somewhere deliberate.
    out = Path(target)
    if not out.is_absolute():
        out = _PROBE_DIR / target
    target = str(out)
    record: dict[str, object] = {
        "ts": round(time.time(), 3),
        "where": where,
        "tool": tool_name,
        "pid": os.getpid(),
    }
    try:
        from fastmcp.server.dependencies import get_http_headers

        # Headers are the transport surface: Mcp-Session-Id is gone in
        # 2026-07-28, but Mcp-Method / Mcp-Name were ADDED, and a client may
        # send its own. Record them all rather than guessing which matters.
        record["headers"] = dict(get_http_headers() or {})
    except Exception as exc:  # noqa: BLE001
        record["headers_error"] = repr(exc)

    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        req = getattr(ctx, "request_context", None)
        meta = getattr(getattr(req, "meta", None), "model_dump", None)
        if callable(meta):
            record["meta"] = meta()
        else:
            record["meta"] = repr(getattr(req, "meta", None))
        # client_params carries the initialize-time clientInfo on transports
        # that still perform a handshake; empty on a stateless 2026-07-28 client.
        session = getattr(req, "session", None)
        cp = getattr(session, "client_params", None)
        record["client_params"] = repr(cp)[:600] if cp is not None else None
        record["session_id_attr"] = repr(getattr(ctx, "session_id", None))
    except Exception as exc:  # noqa: BLE001
        record["context_error"] = repr(exc)

    # #758 SEAM CHECK: did the header actually BECOME the request identity?
    # The first live run showed the header arriving on the wire and the binding
    # still not happening, so record BOTH ends of the handoff instead of
    # inferring which side dropped it.
    try:
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        record["resolved_host_session_id"] = current_calling_host_session_id()
    except Exception as exc:  # noqa: BLE001
        record["resolved_error"] = repr(exc)

    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001,S110 -- a probe must never break the caller
        pass
