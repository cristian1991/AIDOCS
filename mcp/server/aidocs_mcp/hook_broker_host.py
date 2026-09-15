"""Hook-broker HOST process (#609 lifecycle half).

WHY THIS PROCESS EXISTS. The broker used to live in the watchdog, built once
before the supervise loop. The overlap-restart replaces only the daemon BACKEND
CHILD, so a deploy never re-imported the broker's code: measured on the
reference host, thirteen "overlap-restart onto new code" lines against exactly
one "hook broker up". Detection (#609 pass 1/2) made that visible — the broker
proves per event that it still matches the tree on disk and REFUSES when it
does not — but a refusing broker is a lost warm path (#332), and nothing short
of a human restart could give it back.

The only way to PROVE a process runs the shipped code is to have it IMPORT the
shipped code: a fresh interpreter. An in-process rebuild cannot prove anything,
because ``package_code_identity`` reads DISK — a rebuilt object inside the old
interpreter would recompute the DISK identity, match it, and declare itself
fresh while still executing the previous generation's module objects. That is
the exact fail-green hole pass 2 closed; re-opening it in the lifecycle half
would be worse than the refusal it replaced.

So the broker is hosted HERE, in a child the watchdog supervises, and the
deploy edge spawns a replacement (see ``aidocs_service.BrokerChild``). This
module is deliberately tiny: construct, start, block. Everything it publishes —
port, pid, token, and the ``code_identity`` this process LOADED — goes into the
rendezvous file by ``HookBroker.start()``, which is also how the parent proves
the child is fresh before adopting it.
"""

from __future__ import annotations

import sys
import threading


def main(argv: list[str] | None = None) -> int:
    """Run one broker until the parent terminates us.

    FAIL-QUIET: a broker that cannot bind exits non-zero and says why on
    stderr (the parent redirects it to a log). It never retries and never
    half-starts — hooks then stay on their local in-process path, which is
    slower and fully governed, exactly the fallback #332 traded against.
    """
    del argv  # no options: the rendezvous carries everything a client needs
    from .hook_broker import HookBroker

    try:
        broker = HookBroker().start()
    except Exception as exc:  # noqa: BLE001 — report, do not crash-loop
        sys.stderr.write(f"[aidocs hook broker host] failed to start: {exc!r}\n")
        sys.stderr.flush()
        return 1
    sys.stderr.write(f"[aidocs hook broker host] up addr={broker.address}\n")
    sys.stderr.flush()
    try:
        # Park forever. The parent owns this process's lifetime: it terminates
        # us only AFTER the replacement has published the rendezvous, and only
        # after a drain grace, so an evaluation mid-decision is never severed.
        threading.Event().wait()
    except KeyboardInterrupt:  # pragma: no cover — console-signal path only
        pass
    finally:
        broker.close()
    return 0


if __name__ == "__main__":  # pragma: no cover — process entry point
    raise SystemExit(main(sys.argv[1:]))
