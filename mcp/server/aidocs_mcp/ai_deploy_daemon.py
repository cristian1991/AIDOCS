"""The root deploy-runner daemon's PURE core (the queue consumer).

ai_deploy (app) writes a request to the queue; THIS is the root-side counterpart that picks it up
and runs the crown deploy. The daemon is the ONLY privileged component — so it NEVER trusts the
queue file blindly: it RE-VALIDATES every request (ref allowlist + AIDOCS_PRIVATE binding of the
tree it is about to deploy) before doing anything, single-flights so two deploys never overlap, and
records status + log back to the queue.

This module is the pure, dependency-injected core: claim + re-validate + lifecycle. The thin I/O
shell — the watch loop, the real git fetch/checkout, the actual `deploy_aidocs_gate.sh --deploy
--skip-sign` subprocess, and the systemd unit — is wired at the VPS install and kept OUT of here so
the security-relevant logic is unit-testable. The injected `deploy_runner(ref) -> (rc, log)` is what
finally shells the deploy; the injected `origin_resolver() -> str` reports the bound tree's origin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .ai_deploy_authority import DEFAULT_ALLOWED_REFS, origin_is_aidocs_private
from .ai_deploy_queue import LOG_SUFFIX, REQUEST_SUFFIX, STATUS_SUFFIX

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_QUEUED = "queued"


def _status_of(queue_dir: Path, deploy_id: str) -> str:
    sp = queue_dir / f"{deploy_id}{STATUS_SUFFIX}"
    if sp.exists():
        return sp.read_text(encoding="utf-8").strip()
    return ""


def write_status(queue_dir: str | Path, deploy_id: str, status: str) -> None:
    """Daemon-owned status write (atomic tmp+rename)."""
    qd = Path(queue_dir)
    tmp = qd / f".{deploy_id}{STATUS_SUFFIX}.tmp"
    tmp.write_text(status, encoding="utf-8")
    tmp.replace(qd / f"{deploy_id}{STATUS_SUFFIX}")


def write_log(queue_dir: str | Path, deploy_id: str, log: str) -> None:
    """Daemon-owned log write."""
    (Path(queue_dir) / f"{deploy_id}{LOG_SUFFIX}").write_text(log, encoding="utf-8")


def claim_next(queue_dir: str | Path) -> dict[str, Any] | None:
    """Single-flight claim: if ANY request is already running, return None (never overlap two
    deploys). Otherwise pick the OLDEST queued request (by requested_at), mark it running, and
    return its payload. Returns None when there is nothing to do."""
    qd = Path(queue_dir)
    if not qd.exists():
        return None

    # single-flight: a running deploy blocks new claims.
    for rp in qd.glob(f"*{REQUEST_SUFFIX}"):
        did = rp.name[: -len(REQUEST_SUFFIX)]
        if _status_of(qd, did) == STATUS_RUNNING:
            return None

    queued: list[dict[str, Any]] = []
    for rp in qd.glob(f"*{REQUEST_SUFFIX}"):
        did = rp.name[: -len(REQUEST_SUFFIX)]
        if _status_of(qd, did) != STATUS_QUEUED:
            continue
        try:
            payload = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("deploy_id"):
            queued.append(payload)

    if not queued:
        return None
    queued.sort(key=lambda p: (p.get("requested_at") or 0, str(p.get("deploy_id"))))
    chosen = queued[0]
    write_status(qd, str(chosen["deploy_id"]), STATUS_RUNNING)
    return chosen


def validate_request(
    request: dict[str, Any] | None,
    *,
    bound_origin: str | None,
    allowed_refs: "frozenset[str] | set[str]" = DEFAULT_ALLOWED_REFS,
) -> dict[str, Any]:
    """Defense-in-depth re-validation on the ROOT side. The gate already authorized the trigger,
    but the daemon re-checks, fail-closed, the two things it can verify locally before running a
    privileged deploy: (1) the ref is allowlisted, (2) the tree it is about to deploy is actually
    AIDOCS_PRIVATE. A malformed/foreign request never reaches the deploy."""
    if not isinstance(request, dict):
        return {"ok": False, "reason": "request is not a JSON object"}
    ref = str(request.get("ref") or "").strip()
    if ref not in allowed_refs:
        return {"ok": False, "reason": f"daemon refuses ref {ref!r}: not in allowlist {sorted(allowed_refs)}"}
    if not origin_is_aidocs_private(bound_origin):
        return {
            "ok": False,
            "reason": f"daemon refuses: bound tree origin {bound_origin!r} is not AIDOCS_PRIVATE",
        }
    return {"ok": True, "ref": ref}


def run_request(
    request: dict[str, Any] | None,
    *,
    queue_dir: str | Path,
    bound_origin: str | None,
    deploy_runner: Callable[[str], "tuple[int, str]"],
    allowed_refs: "frozenset[str] | set[str]" = DEFAULT_ALLOWED_REFS,
) -> dict[str, Any]:
    """Validate then run, recording the result to the queue. `deploy_runner(ref) -> (rc, log)` is
    injected (the real one shells `deploy_aidocs_gate.sh --deploy --skip-sign` after a fetch+checkout
    of `ref`). On a validation failure NOTHING is run. The final status (ok|failed) + log are written
    to the queue for ai_deploy_output."""
    deploy_id = str((request or {}).get("deploy_id") or "")
    v = validate_request(request, bound_origin=bound_origin, allowed_refs=allowed_refs)
    if not v["ok"]:
        if deploy_id:
            write_log(queue_dir, deploy_id, f"VALIDATION FAILED: {v['reason']}\n")
            write_status(queue_dir, deploy_id, STATUS_FAILED)
        return {"status": STATUS_FAILED, "reason": v["reason"], "deploy_id": deploy_id}

    try:
        rc, log = deploy_runner(v["ref"])
    except Exception as exc:  # a runner crash is a failed deploy, never an unhandled daemon death
        if deploy_id:
            write_log(queue_dir, deploy_id, f"DEPLOY RUNNER CRASHED: {exc}\n")
            write_status(queue_dir, deploy_id, STATUS_FAILED)
        return {"status": STATUS_FAILED, "reason": f"deploy runner crashed: {exc}", "deploy_id": deploy_id}

    status = STATUS_OK if rc == 0 else STATUS_FAILED
    if deploy_id:
        write_log(queue_dir, deploy_id, log or "")
        write_status(queue_dir, deploy_id, status)
    return {"status": status, "rc": rc, "deploy_id": deploy_id}
