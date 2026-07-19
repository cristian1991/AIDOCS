"""Hook broker reference client (#332, #335 Phase 3) — the thin side.

This is the function the future thin ``claude_hook`` will call INSTEAD of
importing the whole gate stack: ask the resident broker (hosted by the
watchdog, see hook_broker.py) to evaluate the hook event, and fall back
to the classic in-process path when the broker cannot answer.

CONTRACT — SECURITY FLOOR (test-pinned in tests/host/test_hook_broker.py;
non-negotiable):

  * Returns ``{"response": <hook JSON dict | None>}`` ONLY for a TRUSTED
    broker answer. ``{"response": None}`` is a real verdict ("hook has no
    output; proceed") — distinct from failure.
  * Returns ``None`` on ANY failure: no state file, dead port, connect or
    read timeout, oversized/malformed reply, ``ok: false``, protocol
    version mismatch, or an echo that does not match the session/root the
    question was about.
  * ``None`` means THE CALLER MUST EVALUATE LOCALLY (run the in-process
    ``ClaudeHookHandler`` path exactly as today). ``None`` must NEVER be
    treated as "allow" — a missing or broken broker degrades to the slow
    local gate, never to an open gate.
  * Only ever dials 127.0.0.1 (the state file carries a port, never a
    host), so a tampered state file cannot exfiltrate hook payloads.
  * Replies are trusted only for the session/root they were asked about:
    the broker echoes ``payload.session_id`` and the requested
    ``project_root``; any mismatch discards the reply.

Budget: ~50ms connect, ~2s total — a hung broker must cost less than the
cold interpreter spawn it replaces.

Pure stdlib, no aidocs imports at module scope: the whole point is that
the thin hook can import THIS without paying the gate-stack import tax.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

_PROTOCOL_VERSION = 1
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def evaluate_via_broker(
    payload: dict,
    *,
    state_path: Path | None = None,
    connect_timeout: float = 0.05,
    total_timeout: float = 2.0,
    env: dict | None = None,
):
    """Try the resident broker; return ``{"response": ...}`` or ``None``.

    ``payload`` is the EXACT JSON the claude_hook subprocess reads on
    stdin. See the module docstring for the trust/floor contract — in
    short: ``None`` == "you must evaluate locally", never "allow".
    """
    deadline = time.monotonic() + max(total_timeout, 0.01)
    try:
        # ── discovery ────────────────────────────────────────────────
        if state_path is None:
            from .hook_broker import broker_state_path  # tiny, lazy

            state_path = broker_state_path()
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        if state.get("v") != _PROTOCOL_VERSION:
            return None
        port = int(state["port"])
        token = str(state.get("token") or "")
        if not (0 < port < 65536) or not token:
            return None

        project_root = str(payload.get("cwd") or "").strip()
        request = {
            "v": _PROTOCOL_VERSION,
            "kind": "hook_eval",
            "token": token,
            "payload": payload,
            "project_root": project_root,
        }
        if env:
            request["env"] = dict(env)

        # ── round trip (hard deadline throughout) ────────────────────
        with socket.create_connection(
            ("127.0.0.1", port), timeout=max(connect_timeout, 0.001)
        ) as conn:
            conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
            buf = b""
            while b"\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                conn.settimeout(remaining)
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_RESPONSE_BYTES:
                    return None

        reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        # ── trust checks ─────────────────────────────────────────────
        if not isinstance(reply, dict):
            return None
        if reply.get("v") != _PROTOCOL_VERSION or reply.get("ok") is not True:
            return None
        if reply.get("session_id") != str(payload.get("session_id") or ""):
            return None  # answer for someone else's session — discard
        if reply.get("project_root") != project_root:
            return None  # answer for a different root — discard
        response = reply.get("response")
        if response is not None and not isinstance(response, dict):
            return None
        return {"response": response}
    except Exception:  # noqa: BLE001 — ANY failure → local evaluation
        return None
