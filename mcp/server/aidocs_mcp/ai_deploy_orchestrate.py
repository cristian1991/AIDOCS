"""ai_deploy end-to-end orchestration — sign-then-deploy, advancing the state machine.

After the dashboard sign-session (ai_deploy_signsession.process_sign_submit) advances a deploy
awaiting_2fa -> signing, THIS runs the privileged half:
  1. materialize the signing key from the (password-unlocked) blob, sign the release  [SIGNING]
  2. run the DEV test suite                                                            [TESTING]
  3. promote DEV -> LIVE                                                                [PROMOTING]
  -> ok, or -> failed (sign / DEV-test failure: nothing promoted), or -> rolled_back (a post-promote
     failure that returned LIVE to the previous release).

`signer`, `dev_test`, and `promote` are INJECTED so the security + state logic is unit-testable with
zero VPS. The plaintext signing key never outlives the materialize-with-block (tmpfs + wipe), and the
blob/password reach here only transiently — no signing key is ever persisted on the VPS.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import ai_deploy_queue as queue
from . import ai_deploy_states as states
from .ai_deploy_keymat import materialized_key

REFUSE_UNKNOWN_DEPLOY = "unknown_deploy_id"
REFUSE_NOT_SIGNING = "deploy_not_signing"
REFUSE_SIGN_FAILED = "sign_failed"


def run_deploy(
    queue_dir,
    deploy_id: str,
    *,
    ref: str,
    blob: bytes,
    password: str,
    key_dir,
    signer: Callable[[str], Any],
    dev_test: Callable[[str], "tuple[int, str]"],
    promote: Callable[[str], "tuple[int, str]"],
) -> dict[str, Any]:
    """From state=signing: materialize+sign, run DEV tests, then promote DEV->LIVE, advancing the
    state at every step. `signer(key_pem_path)` signs (raises on failure); `dev_test(ref)`/
    `promote(ref)` return (rc, log) with rc==0 == success. A sign or DEV-test failure -> FAILED
    (never promoted); a promote failure -> ROLLED_BACK. Returns the final state + the deploy log."""
    payload = queue.read_payload(queue_dir, deploy_id)
    if payload is None:
        return {"ok": False, "refusal": REFUSE_UNKNOWN_DEPLOY}
    if str(payload.get("state") or "") != states.SIGNING:
        return {"ok": False, "refusal": REFUSE_NOT_SIGNING, "state": str(payload.get("state") or "")}

    def _to(new_state: str) -> None:
        queue.update_request_fields(queue_dir, deploy_id, state=new_state)

    # 1. SIGN — key materialized to tmpfs for the with-block ONLY, then securely wiped.
    try:
        with materialized_key(blob, password, key_dir=key_dir) as keypath:
            signer(str(keypath))
    except Exception as exc:  # noqa: BLE001 — any sign/unlock failure fails the deploy, closed
        _to(states.FAILED)
        return {"ok": False, "refusal": REFUSE_SIGN_FAILED, "detail": repr(exc), "state": states.FAILED}

    # 2. DEV test  (signing -> testing)
    _to(states.advance(states.SIGNING, states.TESTING))
    rc, log = dev_test(ref)
    if rc != 0:
        _to(states.advance(states.TESTING, states.FAILED))
        return {"ok": False, "rc": rc, "state": states.FAILED, "log": log}

    # 3. Promote DEV -> LIVE  (testing -> promoting -> ok / rolled_back)
    _to(states.advance(states.TESTING, states.PROMOTING))
    rc, plog = promote(ref)
    final = states.advance(states.PROMOTING, states.OK if rc == 0 else states.ROLLED_BACK)
    _to(final)
    return {"ok": rc == 0, "rc": rc, "state": final, "log": (log or "") + (plog or "")}


REFUSE_BLOB_UNAVAILABLE = "blob_unavailable"


def dashboard_sign_and_deploy(
    queue_dir,
    deploy_id: str,
    *,
    ref: str,
    presented_cred: str,
    totp_seed: str,
    totp_code: str,
    password: str,
    key_dir,
    signer: Callable[[str], Any],
    dev_test: Callable[[str], "tuple[int, str]"],
    promote: Callable[[str], "tuple[int, str]"],
    now: float,
    blob_source: str = "",
    blob_path: str = "",
) -> dict[str, Any]:
    """The full dashboard-backend handler the sign-flow UI calls. (1) verifies the 2-factor sign
    submit (one-time link + TOTP) and consumes the link; (2) fetches the encrypted blob from the
    configured source; (3) signs + runs the DEV->LIVE deploy. The operator password reaches ONLY this
    call (sign time) and is wiped with the materialized key; it is never persisted. A 2FA failure
    fetches/signs nothing; a missing blob -> FAILED before any sign. Returns the final state."""
    from . import ai_deploy_blobsource as blobs
    from . import ai_deploy_signsession as ss

    verdict = ss.process_sign_submit(
        queue_dir, deploy_id, presented_cred=presented_cred, totp_seed=totp_seed,
        totp_code=totp_code, now=now,
    )
    if not verdict["ok"]:
        return verdict
    try:
        blob = blobs.fetch_blob(source=blob_source, path=blob_path)
    except blobs.BlobSourceError as exc:
        queue.update_request_fields(queue_dir, deploy_id, state=states.FAILED)
        return {"ok": False, "refusal": REFUSE_BLOB_UNAVAILABLE, "detail": str(exc), "state": states.FAILED}
    return run_deploy(
        queue_dir, deploy_id, ref=ref, blob=blob, password=password, key_dir=key_dir,
        signer=signer, dev_test=dev_test, promote=promote,
    )


REFUSE_NOT_SIGNED = "deploy_not_signed"


def run_post_sign_deploy(
    queue_dir,
    deploy_id: str,
    *,
    ref: str,
    dev_test: Callable[[str], "tuple[int, str]"],
    promote: Callable[[str], "tuple[int, str]"],
) -> dict[str, Any]:
    """The VPS daemon's half: from a SIGNED deploy (state=signing, signed=True) run the DEV test then
    promote DEV->LIVE, advancing signing->testing->promoting->ok / failed / rolled_back. NO sign, NO
    password, NO blob — the artifact was signed at the dashboard (where the operator typed the
    password); the daemon never holds the signing password. A DEV-test failure -> FAILED (never
    promoted); a promote failure -> ROLLED_BACK."""
    payload = queue.read_payload(queue_dir, deploy_id)
    if payload is None:
        return {"ok": False, "refusal": REFUSE_UNKNOWN_DEPLOY}
    if str(payload.get("state") or "") != states.SIGNING:
        return {"ok": False, "refusal": REFUSE_NOT_SIGNING, "state": str(payload.get("state") or "")}
    if not bool(payload.get("signed", False)):
        return {"ok": False, "refusal": REFUSE_NOT_SIGNED, "state": states.SIGNING}

    def _to(new_state: str) -> None:
        queue.update_request_fields(queue_dir, deploy_id, state=new_state)

    _to(states.advance(states.SIGNING, states.TESTING))
    rc, log = dev_test(ref)
    if rc != 0:
        _to(states.advance(states.TESTING, states.FAILED))
        return {"ok": False, "rc": rc, "state": states.FAILED, "log": log}
    _to(states.advance(states.TESTING, states.PROMOTING))
    rc, plog = promote(ref)
    final = states.advance(states.PROMOTING, states.OK if rc == 0 else states.ROLLED_BACK)
    _to(final)
    return {"ok": rc == 0, "rc": rc, "state": final, "log": (log or "") + (plog or "")}
