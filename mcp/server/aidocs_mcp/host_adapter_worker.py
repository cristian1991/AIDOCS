"""Resident host-adapter worker: the warm transport for the OpenCode plugin.

WHY. The OpenCode plugin (``data/opencode_plugin.js``) used to evaluate every
hook by cold-starting ``python -m aidocs_mcp.host_adapter_cli <event>``: a new
interpreter plus the whole AIDOCS import for each prompt, model request and
tool call, run synchronously inside OpenCode. Claude Code's hooks avoid that
cost through the watchdog's hook broker (``hook_broker.py``). This worker is
the OpenCode equivalent: ONE interpreter per plugin instance, spawned hidden by
the plugin, that answers every event IN-PROCESS through exactly the code the
cold CLI runs (``host_adapter_cli.evaluate_event``).

WHY NOT THE HOOK BROKER ITSELF. The broker evaluates Claude Code hook payloads
only (``hook_broker.evaluate_hook_event`` wraps ``ClaudeHookHandler``; its
``_process`` accepts the kinds ``hook_eval`` and ``hook_timings`` only), and its
security floor proves custody of the registration file with a Windows DACL read
(``registration_custody_ok`` via ``governed_shell_broker_win``) that Node cannot
perform without spawning a process, which is the cost being removed. This
worker needs no rendezvous at all: the plugin spawned it and holds both ends of
its pipes, so there is no port, token or registration that anyone else could
point somewhere else.

PROTOCOL v1, one UTF-8 JSON line each way on stdin/stdout:

  worker -> {"v": 1, "ready": true, "pid": N}            once, at start
  plugin -> {"v": 1, "id": N, "event_kind": "...", "payload": {...}}
  worker -> {"v": 1, "id": N, "result": <the CLI's exact JSON envelope>}
          | {"v": 1, "id": N, "refused": "<reason>"}     NOT evaluated

A ``refused`` reply is a proof that the event was never evaluated, so the
plugin may evaluate it on the cold path instead. ``stale_code`` is refused, and
the worker exits, as soon as the package on disk no longer matches the code
this process imported (the hook broker's #609 rule, same fingerprint): a
resident process must never keep answering with replaced law.

Handlers never write to the protocol stream: ``main`` points ``sys.stdout`` at
stderr before anything is imported, and the protocol uses the original stream.
"""

from __future__ import annotations

import io
import json
import os
import sys
from typing import Callable, TextIO

PROTOCOL_VERSION = 1
REFUSED_STALE_CODE = "stale_code"
REFUSED_BAD_PROTOCOL = "bad_protocol"
REFUSED_MALFORMED = "malformed_request"


def _write(out: TextIO, obj: dict) -> None:
    out.write(json.dumps(obj) + "\n")
    out.flush()


def serve(
    stdin: TextIO,
    stdout: TextIO,
    *,
    code_identity: Callable[[], str | None] | None = None,
    evaluate: Callable[[str, dict], tuple[str, int]] | None = None,
) -> int:
    """Answer requests until stdin closes (0) or the code goes stale (3)."""
    if code_identity is None:
        from .hook_broker import package_code_identity as code_identity
    if evaluate is None:
        from .host_adapter_cli import evaluate_event as evaluate

    baseline = code_identity()
    _write(stdout, {"v": PROTOCOL_VERSION, "ready": True, "pid": os.getpid()})
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _write(stdout, {"v": PROTOCOL_VERSION, "id": None, "refused": REFUSED_MALFORMED})
            continue
        if not isinstance(request, dict):
            _write(stdout, {"v": PROTOCOL_VERSION, "id": None, "refused": REFUSED_MALFORMED})
            continue
        rid = request.get("id")
        if request.get("v") != PROTOCOL_VERSION:
            _write(stdout, {"v": PROTOCOL_VERSION, "id": rid, "refused": REFUSED_BAD_PROTOCOL})
            continue
        # #609 rule, checked BEFORE evaluation: None is "cannot prove", never
        # "matches", so an unprovable identity refuses too.
        current = code_identity()
        if baseline is None or current != baseline:
            _write(stdout, {"v": PROTOCOL_VERSION, "id": rid, "refused": REFUSED_STALE_CODE})
            return 3
        event_kind = str(request.get("event_kind") or "")
        payload = request.get("payload")
        # evaluate_event owns every failure envelope (a non-object payload
        # included), so the verdict semantics are the cold CLI's, byte for byte.
        text, _code = evaluate(event_kind, payload)
        stdout.write('{"v": %d, "id": %s, "result": %s}\n' % (PROTOCOL_VERSION, json.dumps(rid), text))
        stdout.flush()
    return 0


def main() -> int:
    protocol_out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n", write_through=True)
    protocol_in = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    # Nothing a handler prints may reach the protocol stream.
    sys.stdout = sys.stderr
    from .process_bootstrap import run_process_bootstrap

    run_process_bootstrap()
    return serve(protocol_in, protocol_out)


if __name__ == "__main__":
    raise SystemExit(main())
