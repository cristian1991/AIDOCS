"""#1074 round 6 -- the client half of the per-install native credential.

Coordinator O1: the post-callback token exchange + enrollment is ONE Python
operation. The desktop shell only orchestrates the browser and the loopback
callback, then hands over ``code`` + ``code_verifier`` + ``redirect_uri`` +
``resource``. Python owns the pending key, the enrollment proof, the exchange
and the key activation, and returns status + identity. There is no signing
oracle: nothing here signs a caller-chosen payload.

Flow
  1. :func:`prepare_enrollment` -- chooses the key: the ACTIVE key of a
     healthy install (re-auth: same install id and key id), or a NEW pending
     key for an explicit new install. Returns the JWK thumbprint the desktop
     puts on ``/oauth/authorize`` as ``native_jkt``.
  2. the human signs in (fresh, interactive).
  3. :func:`exchange_and_enroll` -- proves possession at ``/oauth/token``; on
     the gate's confirmation activates a new key under the SERVER-ISSUED
     install id and key id, or confirms the re-authorized install.
  4. the daemon signs every native ``POST /v1/mcp`` with
     :class:`NativeRequestSigner` (see ``xaacp_authority``).

Renewal (``gate_credential_renewal``) NEVER enrolls: a refresh keeps the
install family the gate already bound; only a fresh sign-in changes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.asymmetric import ec

from . import native_proof as npf
from .gate_credential_renewal import DEFAULT_TIMEOUT, token_endpoint


def _keystore(keystore=None):
    if keystore is not None:
        return keystore
    from .native_keystore import NativeKeystore

    return NativeKeystore()


def prepare_enrollment(*, keystore=None, new_install: bool = False) -> dict:
    """Choose the key the next interactive sign-in binds (R6-P1).

    DEFAULT: an existing healthy install RE-AUTHORIZES itself with its ACTIVE
    key, so the gate reuses the same install id and key id (a new token family
    for the same install -- not a logout, not a key rotation).
    NEW INSTALL (a new pending key) only when explicitly asked, or when there
    is no active key, or the active key is missing/unreadable (never a
    plaintext fallback: an unreadable key is simply not used).

    Returns ``{jkt, public_jwk, mode}`` -- mode ``"reauth"`` or
    ``"new_install"``. Only the public half and its thumbprint leave."""
    from .native_keystore import KeystoreError

    ks = _keystore(keystore)
    if not new_install:
        try:
            reauth = ks.begin_reauth()
        except KeystoreError:
            reauth = None  # unreadable active key => the explicit re-enroll path
        if reauth is not None:
            return {**reauth, "mode": "reauth"}
    return {**ks.begin_new_install(), "mode": "new_install"}


#: Gate refusals (``native_enrollment_refused``) meaning THIS install can never
#: be re-authorized: the active key is retired and the next sign-in enrolls a
#: new install with a new key.
_INSTALL_GONE = frozenset({"install_revoked", "install_record_missing", "key_bound_to_another_principal"})


def _post_form(url: str, form: dict, timeout: float) -> tuple[int, dict]:
    from .gate_credential_renewal import _post_form as _renewal_post_form

    return _renewal_post_form(url, form, timeout)


def exchange_and_enroll(
    *,
    project_root: Path | str | None,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    resource: str,
    client_id: str,
    keystore=None,
    http: Callable[[str, dict, float], tuple[int, dict]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Exchange the authorization code AND enroll / re-authorize, in one go.

    The key proved is the one :func:`prepare_enrollment` chose (the ACTIVE key
    for a re-auth, the PENDING key for a new install); the choice is internal
    and the only thing signed is the code-bound enrollment proof.

    Returns ``{"ok": True, access_token, refresh_token, expires_in, scope,
    email, native_install_id, native_key_id, mode}`` or ``{"ok": False,
    reason, ...}``. On failure a pending new-install key is discarded and the
    active key is left untouched -- except when the gate says the install is
    gone (revoked / missing / not this user's): then the dead active key is
    retired and ``reason == "reenroll_required"`` (the next sign-in enrolls a
    new install). Persisting the gate credential beside the local operator
    token stays the sign-in command's job; it passes ``native_install_id`` /
    ``native_key_id`` to ``operator_token_resolution.write_cache``.
    """
    from .native_keystore import KeystoreError

    ks = _keystore(keystore)
    try:
        key, mode = ks.enrollment_key()
    except KeystoreError as exc:
        ks.abandon_enrollment()
        return {"ok": False, "reason": "keystore_unavailable", "detail": str(exc)}
    if key is None:
        return {"ok": False, "reason": "no_pending_key"}
    aud = token_endpoint(project_root)
    form = {
        "grant_type": "authorization_code",
        "client_id": str(client_id),
        "code": str(code),
        "redirect_uri": str(redirect_uri),
        "code_verifier": str(code_verifier),
        "resource": str(resource),
        "native_jwk": json.dumps(npf.public_jwk_of(key), sort_keys=True),
        "native_enroll_proof": npf.enrollment_proof(private_key=key, aud=aud, code=str(code)),
    }
    try:
        status, body = (http or _post_form)(aud, form, timeout)
    except Exception as exc:  # noqa: BLE001 -- unreachable is an answer, not a pass
        ks.abandon_enrollment()
        return {"ok": False, "reason": "authority_unreachable", "detail": type(exc).__name__, "mode": mode}
    body = body if isinstance(body, dict) else {}
    install_id = str(body.get("native_install_id") or "")
    key_id = str(body.get("native_key_id") or "")
    if status != 200 or not body.get("access_token"):
        refused = str(body.get("native_enrollment_refused") or "")
        ks.abandon_enrollment()
        if mode == "reauth" and refused in _INSTALL_GONE:
            ks.clear_active()
            return {"ok": False, "reason": "reenroll_required", "native_enrollment_refused": refused,
                    "status": status, "mode": mode}
        return {"ok": False, "reason": "exchange_refused", "status": status, "mode": mode,
                "error": str(body.get("error") or ""),
                "error_description": str(body.get("error_description") or "")}
    if not install_id.startswith("ogni_") or not key_id.startswith("ognk_"):
        # The gate minted tokens but did not confirm an install: the key is
        # NOT this install's identity. Keep nothing half-enrolled.
        ks.abandon_enrollment()
        return {"ok": False, "reason": "enrollment_not_confirmed", "status": status, "mode": mode}
    try:
        ks.complete_enrollment(mode, install_id, key_id)
    except KeystoreError as exc:
        ks.abandon_enrollment()
        return {"ok": False, "reason": "enrollment_not_confirmed", "detail": str(exc), "mode": mode}
    return {
        "ok": True,
        "mode": mode,
        "access_token": str(body.get("access_token") or ""),
        "refresh_token": str(body.get("refresh_token") or ""),
        "expires_in": body.get("expires_in"),
        "scope": str(body.get("scope") or ""),
        "email": str(body.get("email") or ""),
        "native_install_id": install_id,
        "native_key_id": key_id,
    }


@dataclass(frozen=True)
class NativeRequestSigner:
    """Signs THIS install's native requests. Holds the key in-process only."""

    install_id: str
    key_id: str
    private_key: ec.EllipticCurvePrivateKey

    def sign(self, *, htu: str, body: bytes, bearer: str) -> str:
        return npf.request_proof(
            private_key=self.private_key,
            key_id=self.key_id,
            install_id=self.install_id,
            htu=htu,
            body=body,
            bearer=bearer,
        )

    def __repr__(self) -> str:  # never print key material
        return f"NativeRequestSigner(install_id={self.install_id!r}, key_id={self.key_id!r})"


def load_request_signer(*, cache_path: str | Path | None = None, keystore=None) -> NativeRequestSigner | None:
    """The signer for the CURRENT gate credential, or None.

    The cached credential's install family and the keystore's active key must
    name the SAME install and key; any disagreement, a missing key or an
    unreadable keystore means no signer -- the gate then answers
    native_edge_unverified (fail closed), never a guess."""
    from .native_keystore import KeystoreError
    from .operator_token_resolution import native_install_material

    material = native_install_material(cache_path)
    if not material.get("install_id") or not material.get("key_id"):
        return None
    try:
        act = _keystore(keystore).active()
    except KeystoreError:
        return None
    if act is None or act.install_id != material["install_id"] or act.key_id != material["key_id"]:
        return None
    return NativeRequestSigner(install_id=act.install_id, key_id=act.key_id, private_key=act.private_key)
