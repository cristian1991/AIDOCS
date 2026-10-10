"""#1118: ONE renewal authority for the desktop's gate credential.

A desktop sign-in used to hand the SAME single-use refresh token to two
renewers: the machine cache (renewed by this Python process family) and the
Tauri webview's webAuth (renewed from JavaScript). Whichever rotated first left
the other holding a spent token, so roughly once an hour the loser's refresh
failed and the machine lost its credential. The #1118 claim lock serialises
Python processes only, so it could not stop that race.

The webview therefore holds no refresh token. It asks this module, through
``aidocs gate-token --ensure --emit-token --json``, for a bearer: the cached one
while it is fresh, or a renewed one obtained by :func:`ensure_gate_credential`
under the claim lock. The output is ONLY ``{ok, access_token, expires_at}``,
or ``{ok: false, reason, message}``. It never carries the refresh token, the
client id or the scope baseline (contract agreed with lane 2, 0e63c178-6c2).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .gate_credential_renewal import RENEWAL_ALREADY_ATTEMPTED, ensure_gate_credential
from .operator_token_resolution import GATE_CRED_OK, cached_gate_credential, read_cache

#: A caller that lost the renewal claim waits this long, at most, for the
#: winner's renewed bearer, instead of failing while a renewal is in flight.
LOST_CLAIM_WAIT_SECONDS = 3.0
LOST_CLAIM_POLL_SECONDS = 0.25


def _ok(token: str, cache_path: str | Path | None) -> dict:
    row = read_cache(cache_path) or {}
    # The expiry belongs to the bearer it was recorded with; a row that moved
    # between the two reads reports "unknown" rather than another bearer's clock.
    same = str(row.get("gate_token") or "").strip() == token
    expires_at = str(row.get("gate_token_expires_at") or "").strip() if same else ""
    return {"ok": True, "access_token": token, "expires_at": expires_at}


def ensure_bearer(
    *,
    project_root: Path | str | None = None,
    cache_path: str | Path | None = None,
    http: Callable[[str, dict, float], tuple[int, dict]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """A usable gate bearer for the desktop webview, or a named failure."""
    outcome = ensure_gate_credential(project_root=project_root, cache_path=cache_path, http=http)
    cred = outcome.credential
    if cred.reason == GATE_CRED_OK and cred.token:
        return _ok(cred.token, cache_path)
    if outcome.reason == RENEWAL_ALREADY_ATTEMPTED:
        # Another process holds the claim (or a failure is on record). Re-read
        # the cache for a bounded moment so a renewal in flight reaches this
        # caller. Never a second refresh grant: only the claim holder presents.
        deadline = clock() + LOST_CLAIM_WAIT_SECONDS
        while clock() < deadline:
            sleep(LOST_CLAIM_POLL_SECONDS)
            cred = cached_gate_credential(cache_path)
            if cred.reason == GATE_CRED_OK and cred.token:
                return _ok(cred.token, cache_path)
    return {
        "ok": False,
        "reason": str(outcome.reason or cred.reason or "unavailable"),
        "message": str(outcome.detail or f"no usable gate credential ({cred.reason})"),
    }


def cmd_gate_token(argv: list[str]) -> int:
    """``aidocs gate-token --ensure --emit-token --json [--project-root <p>]``.

    All three flags are required: this command exists only to hand the desktop
    webview a bearer as JSON, and it prints no bearer unless asked explicitly.
    Exit 0 when a bearer is emitted, 1 on a named failure, 2 on bad usage.
    """
    import argparse
    import json

    usage = {
        "ok": False,
        "reason": "usage",
        "message": "usage: aidocs gate-token --ensure --emit-token --json [--project-root <p>]",
    }
    parser = argparse.ArgumentParser(prog="aidocs gate-token", add_help=False)
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--emit-token", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--project-root", default=None)
    # Every answer is JSON, bad usage included, so the caller always parses one.
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit:  # e.g. --project-root with no value
        print(json.dumps(usage))
        return 2
    if unknown or not (args.ensure and args.emit_token and args.json):
        print(json.dumps(usage))
        return 2
    payload = ensure_bearer(project_root=args.project_root)
    print(json.dumps(payload))
    return 0 if payload.get("ok") else 1
