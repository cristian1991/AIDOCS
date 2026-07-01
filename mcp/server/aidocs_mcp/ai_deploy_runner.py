"""ai_deploy VPS daemon I/O shell — consumes the queue and runs SIGNED deploys DEV->LIVE.

The dashboard sign endpoint advances a deploy to state=signing with signed=True (the operator typed
TOTP + password there; the artifact is signed). THIS daemon, running on the VPS as the privileged
deploy-runner, claims the oldest signed-ready deploy, SINGLE-FLIGHTS (never two deploys at once), and
runs the DEV->LIVE deploy via run_post_sign_deploy — advancing the state machine and rolling back on
a post-promote failure. It NEVER holds the signing password (the sign already happened at the
dashboard). The injected `dev_test`/`promote` runners shell the VPS-local deploy; the watch loop +
systemd unit are the thin OS wiring. claim + lifecycle here are unit-testable with zero VPS.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ai_deploy_orchestrate as orch
from . import ai_deploy_queue as queue
from . import ai_deploy_states as states


def claim_ready(queue_dir) -> "dict[str, Any] | None":
    """Single-flight claim. If ANY deploy is mid-flight (testing/promoting), return None — never start
    a second. Otherwise return the OLDEST signed-ready deploy (state=signing, signed=True) by
    requested_at, or None when there is nothing to run."""
    qd = Path(queue_dir)
    if not qd.exists():
        return None
    ready: list[dict[str, Any]] = []
    for rp in qd.glob(f"*{queue.REQUEST_SUFFIX}"):
        did = rp.name[: -len(queue.REQUEST_SUFFIX)]
        payload = queue.read_payload(qd, did)
        if not isinstance(payload, dict):
            continue
        st = str(payload.get("state") or "")
        if st in (states.TESTING, states.PROMOTING):
            return None  # a deploy is already in flight
        if st == states.SIGNING and bool(payload.get("signed", False)):
            ready.append(payload)
    if not ready:
        return None
    ready.sort(key=lambda p: (p.get("requested_at") or 0, str(p.get("deploy_id"))))
    return ready[0]


def run_once(
    queue_dir,
    *,
    dev_test: Callable[[str], "tuple[int, str]"],
    promote: Callable[[str], "tuple[int, str]"],
) -> "dict[str, Any] | None":
    """Claim one signed-ready deploy and run it DEV->LIVE. Returns the run result, or None when there
    is nothing to do (empty queue or a deploy already in flight)."""
    claimed = claim_ready(queue_dir)
    if claimed is None:
        return None
    return orch.run_post_sign_deploy(
        queue_dir, str(claimed["deploy_id"]), ref=str(claimed.get("ref") or "main"),
        dev_test=dev_test, promote=promote,
    )
