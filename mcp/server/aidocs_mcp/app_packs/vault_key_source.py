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
    "CustodyOrganizationUnmapped",
    "HttpCall",
    "VaultKeySource",
    "VaultUnavailable",
    "ensure_org_signer",
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
# CN custody's publicJwk (lib/appSigningKeys.ts): the wire members plus alg/use,
# which must carry exactly these values.
_CUSTODY_JWK_FIXED = {"alg": "ES256", "use": "sig"}
_CUSTODY_JWK_MEMBERS = frozenset({"kty", "crv", "x", "y", "kid", *_CUSTODY_JWK_FIXED})

#: ``(method, url, headers, body, timeout) -> (status, body bytes)``; injected in tests.
HttpCall = Callable[[str, str, Mapping[str, str], Optional[bytes], float], "tuple[int, bytes]"]


class VaultUnavailable(RuntimeError):
    """CodeNexus custody could not answer correctly. Generic by construction."""

    def __init__(self) -> None:
        super().__init__("org signing custody unavailable")


class CustodyOrganizationUnmapped(VaultUnavailable):
    """The AIDOCS tenant has NO CodeNexus organization in the durable map, so
    custody cannot be asked at all. Unlike an outage, a retry cannot fix it
    (r0b2 b0fecd6e-126): callers that distinguish report it non-retryable."""


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

    __slots__ = ("_http", "_base", "_bearer", "_timeout", "_organization_for")

    def __init__(
        self,
        *,
        http: Optional[HttpCall] = None,
        base_url: Optional[str] = None,
        secret: Optional[str] = None,
        timeout_s: float = _TIMEOUT_S,
        organization_for: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self._http = http or _urllib_http
        self._base = base_url  # None: read the gate env on every call
        self._bearer = secret  # None: read the gate env on every call
        self._timeout = float(timeout_s)
        # #1134: custody keys OrgAppSigningKey by the CodeNexus ORGANIZATION id
        # (FK to Organization.id), while AIDOCS names the org by its TENANT id.
        # organization_for(tenant) -> organization id (None = unmapped). Absent:
        # the AIDOCS id IS the custody id (tests; app1, where the two are equal).
        self._organization_for = organization_for

    def _custody_org(self, org_id: str) -> str:
        """The id custody knows the org by. Unmapped / lookup fault: fail closed, before any call."""
        if self._organization_for is None:
            return org_id
        try:
            organization = self._organization_for(org_id)
        except Exception:  # noqa: BLE001 -- the mapping store failed (transient): no custody call
            raise VaultUnavailable() from None
        if organization is None:
            raise CustodyOrganizationUnmapped()  # permanent until an operator maps the tenant
        if type(organization) is not str or not organization:
            raise VaultUnavailable()
        return organization

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
              body: Optional[Mapping[str, Any]] = None, expect: int = 200) -> dict:
        """One custody exchange. ONLY the contract's success status ``expect`` is an
        answer (CN lib/appSigningKeys.ts: generate 201, public / sign 200); every
        other status -- 404 unknown_org, 409 duplicate_key, 503 -- is unavailable."""
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
        if failed or status != expect or type(raw) is not bytes or len(raw) > _MAX_RESPONSE_BYTES:
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
        """Custody's publicJwk -> the pinned wire JWK ``{kty, crv, x, y, kid}``.

        CN custody answers ``{kty, crv, x, y, kid, alg: "ES256", use: "sig"}``.
        ``alg`` / ``use`` are admitted ONLY with exactly those values; any other
        member or value is refused. The projection is then validated by the
        wire-strict :func:`~.jws.public_key_from_jwk` (left strict for every other
        input). The kid must be the one AIDOCS asked for.
        """
        jwk = answer.get("publicJwk")
        if answer.get("kid") != kid or not isinstance(jwk, Mapping) or jwk.get("kid", kid) != kid:
            raise VaultUnavailable()  # the vault never names the kid
        if set(jwk) - _CUSTODY_JWK_MEMBERS:
            raise VaultUnavailable()
        for member, value in _CUSTODY_JWK_FIXED.items():
            if member in jwk and jwk[member] != value:
                raise VaultUnavailable()
        try:
            projected = {**{k: jwk[k] for k in ("kty", "crv", "x", "y")}, "kid": kid}
            jws.public_key_from_jwk(projected)
        except (jws.JwsError, KeyError):
            raise VaultUnavailable() from None
        return projected

    # -- the custody API ------------------------------------------------------------------

    def sign(self, org_id: str, purpose: str, kid: str, signing_input: bytes) -> bytes:
        """64 raw ``R || S`` octets over exactly ``signing_input`` by ``kid``.
        NOT verified here -- the signer verifies against the pinned key."""
        self._ids(org_id, purpose, kid)
        if type(signing_input) is not bytes or not signing_input.isascii():
            raise VaultUnavailable()
        answer = self._call("POST", SIGN_PATH, body={
            "orgId": self._custody_org(org_id), "purpose": purpose, "kid": kid,
            "signingInput": signing_input.decode("ascii"),
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
        query = {"orgId": self._custody_org(org_id), "purpose": purpose, "kid": kid}
        return self._jwk(self._call("GET", PUBLIC_PATH, query=query), kid)

    def generate(self, org_id: str, purpose: str, kid: str, state: Optional[str] = None) -> dict:
        """Ask custody to create a key under the AIDOCS-chosen ``kid``; returns its public JWK."""
        self._ids(org_id, purpose, kid)
        body: dict = {"orgId": self._custody_org(org_id), "purpose": purpose, "kid": kid}
        if state is not None:
            body["state"] = state
        return self._jwk(self._call("POST", GENERATE_PATH, body=body, expect=201), kid)  # 201 Created

    def load(self, org_id: str, purpose: str, kid: str) -> Any:
        """Never: the private key does not leave CodeNexus custody."""
        raise VaultUnavailable()


def ensure_org_signer(
    directory: Any,
    vault: VaultKeySource,
    org_id: str,
    kid: str,
    *,
    on_provisioned: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """Give an org that has NEVER had a request-signing key its first CURRENT key.

    The ONE implementation behind the operator act (``runtime.provision_org_signer``)
    and the #1134 automatic provisioning at a first pair-offer. Call it inside
    the org authority DB's transaction (``BEGIN IMMEDIATE``: cross-process
    serialization) so the re-checks below and the pin are atomic.

    * an org with a CURRENT signer: unchanged, returns ``None`` (no custody call);
    * an org with keys but no current one (revoked / retired): NOT provisioned
      (raises ``KeysetError`` via ``bootstrap``'s rule) -- that is operator repair;
    * otherwise custody creates the key under the AIDOCS-chosen ``kid``. If
      ``generate`` fails, custody's ``public`` answer for that kid is used when
      it exists (an earlier attempt created it, then crashed before the pin);
      only a key obtained FROM custody is ever pinned. Custody unavailable
      raises :class:`VaultUnavailable` with nothing pinned.

    ``on_provisioned(kid)`` runs after the pin, inside the caller's transaction
    (the DECISION row: no audit, no pin). Returns the pinned public JWK.
    """
    from .signer import KeysetError

    if directory.current(org_id, PURPOSE_ORG_REQUEST) is not None:
        return None
    if directory.has_keyset(org_id):
        raise KeysetError("org has request-signing keys but no current signer; operator repair")
    try:
        jwk = vault.generate(org_id, PURPOSE_ORG_REQUEST, kid, KEY_CURRENT)
    except VaultUnavailable:
        jwk = vault.public_jwk(org_id, PURPOSE_ORG_REQUEST, kid)  # raises VaultUnavailable if absent/down
    directory.bootstrap(org_id, kid, jws.public_key_from_jwk(jwk))
    if on_provisioned is not None:
        on_provisioned(kid)
    return jwk


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
