"""CodeNexus custody for the AIDOCS org request key -- RFC 0003 v4.6.1 §12.3 (build plan S3, I-3).

AIDOCS owns the signer SEMANTICS (kid, trust domain, rotation timing: the
directory in the org authority DB); CodeNexus owns CUSTODY and signs
mechanically. The private key never leaves CodeNexus and AIDOCS never stores it.

CodeNexus internal S2S routes (``Authorization: Bearer $AIDOCS_INTERNAL_S2S_SECRET``):

* ``POST {base}/api/internal/app-signing-keys/sign``
  ``{orgId, purpose, kid, signingInput}`` -> ``{kid, signature}`` (ES256,
  base64url raw ``R || S``, 64 octets);
* ``GET  {base}/api/internal/app-signing-keys/public?orgId&purpose&kid``
  -> ``{kid, publicJwk, state}``;
* ``POST {base}/api/internal/app-signing-keys/generate``
  ``{orgId, purpose, kid, state?}`` -> ``{kid, publicJwk}``.

``base`` and the bearer come from the SAME gate config the other CodeNexus
internal clients use (``AIDOCS_CODENEXUS_INTERNAL_URL`` /
``AIDOCS_INTERNAL_S2S_SECRET``, as in ``outer_gate_github_credential`` and the
backlog forwarder), read from the environment on EVERY call.

Rules:

* The kid is always AIDOCS's: every call names it, and an answer for any other
  kid is refused. The vault never picks a kid, a key or a rotation.
* The signature is not trusted here: :class:`~.signer.OrgRequestSigner`
  verifies it against the PINNED public JWK of that kid before use.
* No caching of anything secret: no key, no signature, no bearer.
* Generic errors only (:class:`VaultUnavailable`); nothing CodeNexus says, and
  no bearer, is reflected; the underlying exception is not chained.
* Fail closed: unconfigured, unreachable, non-200, malformed -> refused.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Optional

from ..governed_egress import EgressRefused, assert_egress_allowed
from . import jws
from .signer import KEY_CURRENT, KEY_NEXT, PURPOSE_ORG_REQUEST

__all__ = [
    "GENERATE_PATH",
    "INTERNAL_URL_ENV",
    "PUBLIC_PATH",
    "S2S_SECRET_ENV",
    "SIGN_PATH",
    "HttpCall",
    "VaultKeySource",
    "VaultUnavailable",
    "provision_org_key",
]

INTERNAL_URL_ENV = "AIDOCS_CODENEXUS_INTERNAL_URL"
S2S_SECRET_ENV = "AIDOCS_INTERNAL_S2S_SECRET"  # gitleaks:allow (env var NAME, not a value)
SIGN_PATH = "/api/internal/app-signing-keys/sign"
PUBLIC_PATH = "/api/internal/app-signing-keys/public"
GENERATE_PATH = "/api/internal/app-signing-keys/generate"
EGRESS_PURPOSE = "app_org_request_signing_custody"
_TIMEOUT_S = 8.0
_MAX_RESPONSE_BYTES = 16 * 1024
_KID_RE = re.compile(r"[A-Za-z0-9._:-]{1,100}")

#: ``(method, url, headers, body, timeout) -> (status, body bytes)``; injected in tests.
HttpCall = Callable[[str, str, Mapping[str, str], Optional[bytes], float], "tuple[int, bytes]"]


class VaultUnavailable(RuntimeError):
    """CodeNexus custody could not answer correctly. Generic by construction."""

    def __init__(self) -> None:
        super().__init__("org signing custody unavailable")


def _urllib_http(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes],
                 timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310 -- fixed internal host from operator env
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200) or 200), resp.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:  # an error body is never read
        return int(exc.code), b""


class VaultKeySource:
    """The :class:`~.signer.SigningKeySource` over CodeNexus custody (see module doc)."""

    __slots__ = ("_http", "_base", "_bearer", "_timeout")

    def __init__(
        self,
        *,
        http: Optional[HttpCall] = None,
        base_url: Optional[str] = None,
        secret: Optional[str] = None,
        timeout_s: float = _TIMEOUT_S,
    ) -> None:
        self._http = http or _urllib_http
        self._base = base_url  # None: read the gate env on every call
        self._bearer = secret  # None: read the gate env on every call
        self._timeout = float(timeout_s)

    def __repr__(self) -> str:
        return "<VaultKeySource>"

    # -- transport ------------------------------------------------------------------

    def _config(self) -> tuple[str, str]:
        base = (self._base if self._base is not None else os.environ.get(INTERNAL_URL_ENV, "")).strip()
        bearer = (self._bearer if self._bearer is not None else os.environ.get(S2S_SECRET_ENV, "")).strip()
        if not base or not bearer:
            raise VaultUnavailable()
        return base.rstrip("/"), bearer

    def _call(self, method: str, path: str, *, query: Optional[Mapping[str, str]] = None,
              body: Optional[Mapping[str, Any]] = None) -> dict:
        base, bearer = self._config()
        url = base + path + ("?" + urllib.parse.urlencode(dict(query)) if query else "")
        try:  # the one in-process egress chokepoint; the host is the operator-configured one
            assert_egress_allowed(url, purpose=EGRESS_PURPOSE, allow_hosts=[urllib.parse.urlsplit(base).hostname or ""])
        except EgressRefused:
            raise VaultUnavailable() from None
        headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json", "Cache-Control": "no-store"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
        failed = False
        status, raw = 0, b""
        try:
            status, raw = self._http(method, url, headers, data, self._timeout)
        except Exception:  # noqa: BLE001 -- unreachable custody fails closed; text dropped
            failed = True
        if failed or status != 200 or type(raw) is not bytes or len(raw) > _MAX_RESPONSE_BYTES:
            raise VaultUnavailable()
        parsed: Any = None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = None
        if not isinstance(parsed, dict):
            raise VaultUnavailable()
        return parsed

    @staticmethod
    def _ids(org_id: Any, purpose: Any, kid: Any) -> None:
        if type(org_id) is not str or not org_id or type(purpose) is not str or not purpose:
            raise VaultUnavailable()
        if type(kid) is not str or not _KID_RE.fullmatch(kid):
            raise VaultUnavailable()

    @staticmethod
    def _jwk(answer: Mapping[str, Any], kid: str) -> dict:
        jwk = answer.get("publicJwk")
        if answer.get("kid") != kid or not isinstance(jwk, Mapping) or jwk.get("kid", kid) != kid:
            raise VaultUnavailable()  # the vault never names the kid
        try:
            jws.public_key_from_jwk(jwk)
            members = {k: jwk[k] for k in ("kty", "crv", "x", "y")}
        except (jws.JwsError, KeyError):
            raise VaultUnavailable() from None
        return {**members, "kid": kid}

    # -- the custody API ------------------------------------------------------------------

    def sign(self, org_id: str, purpose: str, kid: str, signing_input: bytes) -> bytes:
        """64 raw ``R || S`` octets over exactly ``signing_input`` by ``kid``.
        NOT verified here -- the signer verifies against the pinned key."""
        self._ids(org_id, purpose, kid)
        if type(signing_input) is not bytes or not signing_input.isascii():
            raise VaultUnavailable()
        answer = self._call("POST", SIGN_PATH, body={
            "orgId": org_id, "purpose": purpose, "kid": kid, "signingInput": signing_input.decode("ascii"),
        })
        if answer.get("kid") != kid:
            raise VaultUnavailable()
        try:
            signature = jws.b64url_decode(answer.get("signature"))
        except jws.JwsError:
            raise VaultUnavailable() from None
        if len(signature) != jws.SIGNATURE_BYTES:
            raise VaultUnavailable()
        return signature

    def public_jwk(self, org_id: str, purpose: str, kid: str) -> dict:
        self._ids(org_id, purpose, kid)
        return self._jwk(self._call("GET", PUBLIC_PATH, query={"orgId": org_id, "purpose": purpose, "kid": kid}), kid)

    def generate(self, org_id: str, purpose: str, kid: str, state: Optional[str] = None) -> dict:
        """Ask custody to create a key under the AIDOCS-chosen ``kid``; returns its public JWK."""
        self._ids(org_id, purpose, kid)
        body: dict = {"orgId": org_id, "purpose": purpose, "kid": kid}
        if state is not None:
            body["state"] = state
        return self._jwk(self._call("POST", GENERATE_PATH, body=body), kid)

    def load(self, org_id: str, purpose: str, kid: str) -> Any:
        """Never: the private key does not leave CodeNexus custody."""
        raise VaultUnavailable()


def provision_org_key(directory: Any, vault: VaultKeySource, org_id: str, kid: str, *, bootstrap: bool) -> dict:
    """Create an org request key in custody under the AIDOCS-chosen ``kid`` and pin
    its public half in the AIDOCS directory: as the org's first CURRENT key
    (``bootstrap``) or as the §5.8 NEXT key. Returns the pinned public JWK."""
    jwk = vault.generate(org_id, PURPOSE_ORG_REQUEST, kid, KEY_CURRENT if bootstrap else KEY_NEXT)
    public_key = jws.public_key_from_jwk(jwk)
    if bootstrap:
        directory.bootstrap(org_id, kid, public_key)
    else:
        directory.add_next(org_id, kid, public_key)
    return jwk
