"""#1074 round 6 -- the per-install native-edge proofs (sign + verify).

Two DEDICATED object types, never an RFC 0003 wire type (coordinator Q1):

``aidocs-native-enroll+jwt``
    Proves possession of a NEW install key at the one authorization-code
    exchange that enrolls it. Header ``kid`` is the client-known RFC 7638 JWK
    thumbprint (JKT) that the authorize request bound onto the code row;
    claims ``{aud, code_sha256, jkt, iat, exp, jti}``.

``aidocs-native-proof+jwt``
    Proves, on every native ``POST /v1/mcp``, that the caller holds a
    REGISTERED install key. Header ``kid`` is the SERVER-ISSUED key id
    (``ognk_*``), never the JKT (reviewer B1); claims ``{install_id, htm, htu,
    body_sha256, ath, iat, exp, jti}``.

Bindings (coordinator Q1): ``htu`` is the SERVER-CONFIGURED canonical public
MCP URL (configured gate base + exactly ``/v1/mcp``) -- the client derives it
from its configured gate URL, the server from its own configuration, and
neither ever reads ``Host`` / ``X-Forwarded-*``. ``htm`` is exactly ``POST``.
``body_sha256`` covers the EXACT raw HTTP body bytes (never re-serialized).
``ath`` is the base64url SHA-256 of the exact bearer.

The ES256 primitives are :mod:`aidocs_mcp.app_packs.jws`'s (strict 64-octet
R||S, P-256 only). Its compact helpers police the RFC 0003 wire types, so the
header check here is this module's own. Error messages name the failed check,
never token bytes, claims or key material.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives.asymmetric import ec

from .app_packs import jws

TYP_ENROLL = "aidocs-native-enroll+jwt"
TYP_PROOF = "aidocs-native-proof+jwt"
NATIVE_TYPS = frozenset({TYP_ENROLL, TYP_PROOF})
#: The HTTP request header that carries the per-call proof.
PROOF_HEADER = "AIDOCS-Native-Proof"
#: Sealed bounds: a proof lives at most 60 s; clocks may differ by 10 s.
MAX_LIFETIME_S = 60
SKEW_S = jws.MAX_CLOCK_SKEW_S
MAX_PROOF_BYTES = 4 * 1024
#: Env key naming the gate's canonical public base URL (server side).
PUBLIC_BASE_ENV = "AIDOCS_GATE_PUBLIC_BASE"

_HEADER_MEMBERS = frozenset({"alg", "kid", "typ"})
_KID_RE = re.compile(r"[A-Za-z0-9._:-]{1,100}")
_JTI_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")


class NativeProofError(ValueError):
    """A native proof (or a claim it carries) is refused."""


# -- canonical bindings --------------------------------------------------------


def sha256_b64url(data: bytes) -> str:
    return jws.b64url_encode(hashlib.sha256(bytes(data)).digest())


def ath_for(bearer: str) -> str:
    return sha256_b64url(str(bearer or "").encode("utf-8"))


def _strip_base(base: str) -> str:
    b = str(base or "").strip().rstrip("/")
    if b.endswith("/v1/mcp"):
        b = b[: -len("/v1/mcp")]
    return b.rstrip("/")


def canonical_mcp_url(base: str) -> str:
    return _strip_base(base) + "/v1/mcp"


def canonical_token_url(base: str) -> str:
    return _strip_base(base) + "/oauth/token"


def server_public_base() -> str:
    """The gate's OWN configured public base -- never a request header."""
    configured = str(os.environ.get(PUBLIC_BASE_ENV) or "").strip()
    if configured:
        return _strip_base(configured)
    from .outer_gate_transport import DEFAULT_PUBLIC_BASE

    return _strip_base(DEFAULT_PUBLIC_BASE)


# -- JWK ----------------------------------------------------------------------


def public_jwk_of(key: ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey) -> dict:
    """The bare public JWK ``{kty, crv, x, y}`` (no kid, never ``d``)."""
    pub = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    j = jws.public_jwk(pub, "k")
    j.pop("kid", None)
    return j


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    try:
        return jws.jwk_thumbprint(jwk)
    except jws.JwsError as exc:
        raise NativeProofError("JWK is not a P-256 public key") from exc


def public_key_from_jwk(jwk: Mapping[str, Any]) -> ec.EllipticCurvePublicKey:
    try:
        return jws.public_key_from_jwk(jwk)
    except jws.JwsError as exc:
        raise NativeProofError("JWK is not a P-256 public key") from exc


# -- sign ---------------------------------------------------------------------


def _check_header(header: Mapping[str, Any]) -> None:
    if not isinstance(header, Mapping) or set(header) != _HEADER_MEMBERS:
        raise NativeProofError("protected header members must be exactly alg, kid, typ")
    if type(header["alg"]) is not str or header["alg"] != jws.ALG:
        raise NativeProofError("alg is not ES256")
    if type(header["typ"]) is not str or header["typ"] not in NATIVE_TYPS:
        raise NativeProofError("typ is not a native proof type")
    if type(header["kid"]) is not str or not _KID_RE.fullmatch(header["kid"]):
        raise NativeProofError("kid is outside the kid alphabet")


def sign(typ: str, kid: str, claims: Mapping[str, Any], private_key: ec.EllipticCurvePrivateKey) -> str:
    header = {"alg": jws.ALG, "kid": kid, "typ": typ}
    _check_header(header)
    try:
        si = jws.signing_input(header, dict(claims))
        return si.decode("ascii") + "." + jws.b64url_encode(jws.es256_sign_raw(si, private_key))
    except jws.JwsError as exc:
        raise NativeProofError(str(exc)) from exc


def request_proof(
    *,
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str,
    install_id: str,
    htu: str,
    body: bytes,
    bearer: str,
    now: int | None = None,
    lifetime: int = MAX_LIFETIME_S,
) -> str:
    iat = int(time.time()) if now is None else int(now)
    return sign(TYP_PROOF, key_id, {
        "install_id": str(install_id),
        "htm": "POST",
        "htu": canonical_mcp_url(htu),
        "body_sha256": sha256_b64url(body),
        "ath": ath_for(bearer),
        "iat": iat,
        "exp": iat + int(lifetime),
        "jti": uuid.uuid4().hex,
    }, private_key)


def enrollment_proof(
    *,
    private_key: ec.EllipticCurvePrivateKey,
    aud: str,
    code: str,
    now: int | None = None,
) -> str:
    jkt = jwk_thumbprint(public_jwk_of(private_key))
    iat = int(time.time()) if now is None else int(now)
    return sign(TYP_ENROLL, jkt, {
        "aud": str(aud),
        "code_sha256": sha256_b64url(str(code).encode("utf-8")),
        "jkt": jkt,
        "iat": iat,
        "exp": iat + MAX_LIFETIME_S,
        "jti": uuid.uuid4().hex,
    }, private_key)


# -- verify -------------------------------------------------------------------


def _refuse_duplicates(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise NativeProofError("duplicate member name")
        out[k] = v
    return out


def _parse(raw: bytes, what: str) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_refuse_duplicates)
    except NativeProofError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise NativeProofError(f"{what} is not valid JSON") from exc
    if type(value) is not dict:
        raise NativeProofError(f"{what} is not a JSON object")
    return value


def verify(
    token: str,
    *,
    expected_typ: str,
    key_for_kid: Callable[[str], ec.EllipticCurvePublicKey | None],
) -> tuple[dict, dict]:
    """Size/JOSE -> key lookup -> signature -> payload parse. Returns
    ``(header, claims)``. Claim checks are the caller's (order law)."""
    if expected_typ not in NATIVE_TYPS:
        raise NativeProofError("expected typ is not a native proof type")
    if type(token) is not str or not token.isascii() or not token or len(token) > MAX_PROOF_BYTES:
        raise NativeProofError("proof is not ASCII text within the size bound")
    parts = token.split(".")
    if len(parts) != 3:
        raise NativeProofError("proof is not a three-segment compact serialization")
    h_b64, p_b64, s_b64 = parts
    try:
        header = _parse(jws.b64url_decode(h_b64), "protected header")
    except jws.JwsError as exc:
        raise NativeProofError("protected header is not base64url") from exc
    _check_header(header)
    if header["typ"] != expected_typ:
        raise NativeProofError("typ names another object")
    key = key_for_kid(header["kid"])
    if key is None:
        raise NativeProofError("kid is not a registered key")
    try:
        signature = jws.b64url_decode(s_b64)
        good = jws.es256_verify_raw((h_b64 + "." + p_b64).encode("ascii"), signature, key)
    except jws.JwsError as exc:
        raise NativeProofError("signature is malformed") from exc
    if not good:
        raise NativeProofError("signature does not verify")
    try:
        claims = _parse(jws.b64url_decode(p_b64), "payload")
    except jws.JwsError as exc:
        raise NativeProofError("payload is not base64url") from exc
    return header, claims


def check_lifetime(claims: Mapping[str, Any], *, now: int | None = None) -> None:
    try:
        jws.check_lifetime(claims, now=int(time.time()) if now is None else int(now),
                           max_lifetime_s=MAX_LIFETIME_S, skew_s=SKEW_S)
    except jws.JwsError as exc:
        raise NativeProofError(str(exc)) from exc


def check_jti(claims: Mapping[str, Any]) -> str:
    jti = claims.get("jti")
    if type(jti) is not str or not _JTI_RE.fullmatch(jti):
        raise NativeProofError("jti is missing or malformed")
    return jti
