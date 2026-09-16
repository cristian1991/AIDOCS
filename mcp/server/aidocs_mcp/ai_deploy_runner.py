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

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ai_deploy_orchestrate as orch
from . import ai_deploy_queue as queue
from . import ai_deploy_states as states


def claim_ready(
    queue_dir,
    *,
    runner_id: "str | None" = None,
    now: "float | None" = None,
    lease_ttl: "float | None" = None,
) -> "dict[str, Any] | None":
    """Single-flight, ATOMICALLY-LEASED claim (Blocker H). Scan for the OLDEST signed-ready deploy
    (state=signing, signed=True) and return it ONLY after winning an O_EXCL lease — so two runners
    can never claim the same deploy. Single-flight: any deploy mid-flight (testing/promoting) OR
    under an active lease blocks a new claim. Returns None when there is nothing claimable.

    runner_id/now default to this process + wall clock; tests inject them for determinism."""
    qd = Path(queue_dir)
    if not qd.exists():
        return None
    now = time.time() if now is None else now
    runner_id = runner_id or f"runner-{os.getpid()}"
    ttl = queue.DEFAULT_LEASE_TTL if lease_ttl is None else lease_ttl
    ready: list[dict[str, Any]] = []
    for rp in qd.glob(f"*{queue.REQUEST_SUFFIX}"):
        did = rp.name[: -len(queue.REQUEST_SUFFIX)]
        payload = queue.read_payload(qd, did)
        if not isinstance(payload, dict):
            continue
        st = str(payload.get("state") or "")
        if st in (states.TESTING, states.PROMOTING):
            return None  # a deploy is already in flight
        if queue.lease_active(qd, did, now):
            return None  # a runner already holds a deploy — single-flight
        if st == states.SIGNING and bool(payload.get("signed", False)):
            ready.append(payload)
    if not ready:
        return None
    ready.sort(key=lambda p: (p.get("requested_at") or 0, str(p.get("deploy_id"))))
    for cand in ready:
        did = str(cand["deploy_id"])
        if queue.claim_lease(qd, did, runner_id=runner_id, now=now, ttl=ttl):
            return cand  # won the atomic lease
    return None  # every ready deploy was leased by a concurrent runner in the race


def _no_verified_receipt(_payload: "dict[str, Any]") -> bool:
    """Fail-closed default receipt verifier (Blocker D). The privileged runner must INDEPENDENTLY
    verify an IMMUTABLE artifact receipt — never the unprivileged app's mutable `signed`/`approved`
    fields, which live in the same security domain that wrote the request. No real verifier is wired
    until the artifact-receipt producer exists (deploy-plan Phase 4), so the default REFUSES every
    deploy. The production runner injects the real cryptographic (signature + digest) verifier."""
    return False


def run_once(
    queue_dir,
    *,
    dev_test: Callable[[str], "tuple[int, str]"],
    promote: Callable[[str], "tuple[int, str]"],
    origin: "str | None",
    verify_receipt: "Callable[[dict[str, Any]], bool] | None" = None,
    allowed_refs=None,
    runner_id: "str | None" = None,
    now: "float | None" = None,
    lease_ttl: "float | None" = None,
) -> "dict[str, Any] | None":
    """Claim one signed-ready deploy (atomically leased) and run it DEV->LIVE. Before running, the
    privileged runner (1) RE-VALIDATES the claimed request against the tree it will deploy — ref
    allowlist + AIDOCS_PRIVATE `origin` (authority.validate_deploy_tree) — and (2) INDEPENDENTLY
    VERIFIES an immutable artifact receipt via `verify_receipt`, NEVER trusting the app-written
    `signed` flag as deploy authority (Blocker D). `verify_receipt` defaults to fail-closed
    (_no_verified_receipt) until the Phase-4 receipt producer exists; the production runner injects
    the real cryptographic verifier. Any check failing marks the deploy FAILED and it is NEVER run
    (dev_test/promote not invoked). RELEASES the lease on a terminal state. Returns the run result,
    or None when there is nothing to do."""
    from . import ai_deploy_authority as authority

    refs = authority.DEFAULT_ALLOWED_REFS if allowed_refs is None else allowed_refs
    verifier = _no_verified_receipt if verify_receipt is None else verify_receipt
    claimed = claim_ready(queue_dir, runner_id=runner_id, now=now, lease_ttl=lease_ttl)
    if claimed is None:
        return None
    did = str(claimed["deploy_id"])

    def _refuse(refusal: str, reason: str) -> "dict[str, Any]":
        queue.update_request_fields(queue_dir, did, state=states.FAILED)
        queue.write_log(queue_dir, did, f"REFUSED ({refusal}): {reason}\n")
        return {"ok": False, "refusal": refusal, "reason": reason, "state": states.FAILED, "deploy_id": did}

    try:
        v = authority.validate_deploy_tree(claimed, bound_origin=origin, allowed_refs=refs)
        if not v["ok"]:
            return _refuse("validation_failed", v["reason"])
        if not verifier(claimed):
            return _refuse(
                "unverified_artifact_receipt",
                "no independently-verified artifact receipt — the app-written signed flag is not "
                "trusted as deploy authority (Blocker D); the immutable receipt producer is Phase 4",
            )
        return orch.run_post_sign_deploy(
            queue_dir, did, ref=str(claimed.get("ref") or "main"),
            dev_test=dev_test, promote=promote,
        )
    finally:
        queue.release_lease(queue_dir, did)
