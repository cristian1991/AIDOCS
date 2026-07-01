"""The ai_deploy request queue — the single trust boundary between the gate (running as the
unprivileged `app` user) and the root deploy-runner daemon.

ai_deploy (app) only ever WRITES a request file here; the root daemon READS it, validates it
again, runs the crown deploy, and writes back status + log. `app` never runs root, never holds
the ssh/signing keys — the queue file is the entire interface. Keeping the boundary this thin is
what lets the daemon be the sole privileged component.

This module is pure file I/O against a given queue dir (no daemon, no VPS) so it is unit-testable
in isolation. On-disk contract (one set per deploy_id):

    <queue>/<deploy_id>.request.json   request payload (ai_deploy writes; daemon reads)
    <queue>/<deploy_id>.status         queued|running|ok|failed  (daemon-owned after pickup)
    <queue>/<deploy_id>.log            deploy output              (daemon-owned)

Writes are atomic (tmp + rename) so the daemon never reads a half-written request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUEST_SUFFIX = ".request.json"
STATUS_SUFFIX = ".status"
LOG_SUFFIX = ".log"

STATUS_QUEUED = "queued"


def enqueue_deploy(
    queue_dir: str | Path,
    *,
    deploy_id: str,
    ref: str,
    principal_user: str | None,
    requested_at: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically write a deploy request. Returns ``ok=True`` + the request path, or
    ``ok=False`` with ``error='duplicate_request'`` if this deploy_id is already queued
    (never silently overwrite an in-flight request)."""
    qd = Path(queue_dir)
    qd.mkdir(parents=True, exist_ok=True)
    req_path = qd / f"{deploy_id}{REQUEST_SUFFIX}"
    if req_path.exists():
        return {"ok": False, "error": "duplicate_request", "deploy_id": deploy_id}

    payload: dict[str, Any] = {
        "deploy_id": deploy_id,
        "ref": ref,
        "principal_user": principal_user,
        "requested_at": requested_at,
        "status": STATUS_QUEUED,
    }
    if extra:
        # never let extra clobber the canonical identity fields
        for k, v in extra.items():
            payload.setdefault(k, v)

    tmp = qd / f".{deploy_id}{REQUEST_SUFFIX}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(req_path)  # atomic on POSIX
    (qd / f"{deploy_id}{STATUS_SUFFIX}").write_text(STATUS_QUEUED, encoding="utf-8")
    return {"ok": True, "deploy_id": deploy_id, "status": STATUS_QUEUED, "request_path": str(req_path)}


def read_request_owner(queue_dir: str | Path, deploy_id: str) -> "str | None":
    """The principal_user that enqueued ``deploy_id`` (the deploy's OWNER), or None when no
    request exists / it is unreadable. Used to restrict ai_deploy_output reads to the owner
    or a super_admin. Reads only the request payload — never the (daemon-owned) log."""
    qd = Path(queue_dir)
    rp = qd / f"{deploy_id}{REQUEST_SUFFIX}"
    if not rp.exists():
        return None
    try:
        return str(json.loads(rp.read_text(encoding="utf-8")).get("principal_user") or "")
    except Exception:
        return None


def read_deploy_output(queue_dir: str | Path, deploy_id: str) -> dict[str, Any]:
    """Read the daemon-written status + log for a deploy_id. ``ok=False`` /
    ``error='unknown_deploy_id'`` when no request exists for it."""
    qd = Path(queue_dir)
    req_path = qd / f"{deploy_id}{REQUEST_SUFFIX}"
    if not req_path.exists():
        return {"ok": False, "error": "unknown_deploy_id", "deploy_id": deploy_id}

    # Rich stepwise state (awaiting_2fa / signing / testing / promoting / ok / failed / rolled_back)
    # lives in the request payload; the coarse daemon status file is the back-compat fallback.
    state = ""
    try:
        state = str(json.loads(req_path.read_text(encoding="utf-8")).get("state") or "")
    except Exception:
        state = ""

    status = "unknown"
    sp = qd / f"{deploy_id}{STATUS_SUFFIX}"
    if sp.exists():
        status = sp.read_text(encoding="utf-8").strip() or "unknown"

    log = ""
    lp = qd / f"{deploy_id}{LOG_SUFFIX}"
    if lp.exists():
        log = lp.read_text(encoding="utf-8", errors="replace")

    return {"ok": True, "deploy_id": deploy_id, "state": state or status, "status": status, "log": log}


def read_payload(queue_dir: str | Path, deploy_id: str) -> "dict[str, Any] | None":
    """The full request payload for a deploy_id, or None if absent/unreadable. The gate sign-session
    endpoint reads state + sign_token_sha256 + sign_link_consumed from it."""
    rp = Path(queue_dir) / f"{deploy_id}{REQUEST_SUFFIX}"
    if not rp.exists():
        return None
    try:
        payload = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def update_request_fields(queue_dir: str | Path, deploy_id: str, **fields: Any) -> bool:
    """Atomically merge `fields` into the deploy's request payload (tmp+rename). Returns False if the
    request is absent/unreadable. The sign endpoint + daemon use it to advance `state` and flip
    `sign_link_consumed` (one-time). Callers pass only the fields they intend to change."""
    qd = Path(queue_dir)
    rp = qd / f"{deploy_id}{REQUEST_SUFFIX}"
    payload = read_payload(qd, deploy_id)
    if payload is None:
        return False
    payload.update(fields)
    tmp = qd / f".{deploy_id}{REQUEST_SUFFIX}.upd.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(rp)  # atomic on POSIX
    return True
