"""ES256 compact JWS -- RFC 0003 v4.6.1 §12.3; wire companion rev 8 §1.1-§1.4.

The one signing primitive of the application trust wire (build plan S3):

* JWS Compact Serialization with an ATTACHED payload, ``alg = "ES256"``,
  P-256 only. Detached payloads are never used (wire §1.2).
* Protected header members are EXACTLY ``alg``, ``kid`` and ``typ``; any
  other member (``crit``, ``jku``, ``jwk``, ``x5u``, ``x5c``, ...) is refused.
* Header and payload bytes are the RFC 8785 (JCS) form produced by
  :mod:`aidocs_mcp.app_packs.jcs` -- there is no second canonicalizer. A
  verifier checks the bytes it received and never re-canonicalizes.
* The signature is exactly 64 octets ``R || S`` (32-octet big-endian each).
  ASN.1/DER and every other length are refused.
* Duplicate member names in the header or the payload are refused (wire §1.1).
* The encoded JWS is at most 8 KiB.

Key SELECTION is not done here: :func:`verify_compact` takes the one keyset
that the object's ``typ`` and binding select (wire §1.3 kid namespace), and a
kid outside it is unknown. Signing semantics (which org key, which kid) live
in :mod:`aidocs_mcp.app_packs.signer`.

Error messages name the failed check only; they never echo token bytes, claim
values or key material.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from .jcs import JcsError, canonicalize

__all__ = [
    "ALG",
    "MAX_CLOCK_SKEW_S",
    "MAX_ENCODED_BYTES",
    "SIGNATURE_BYTES",
    "TYP_CONTROL",
    "TYP_ENTITLEMENT",
    "TYP_PAIRING_PROOF",
    "TYP_REQUEST",
    "WIRE_TYPS",
    "JwsError",
    "VerifiedJws",
    "b64url_decode",
    "b64url_encode",
    "check_lifetime",
    "es256_sign_raw",
    "es256_verify_raw",
    "jwk_thumbprint",
    "public_jwk",
    "public_key_from_jwk",
    "sign_compact",
    "signing_input",
    "verify_compact",
]

ALG = "ES256"
SIGNATURE_BYTES = 64
MAX_ENCODED_BYTES = 8 * 1024
MAX_CLOCK_SKEW_S = 10

TYP_REQUEST = "aidocs-request+jwt"
TYP_CONTROL = "aidocs-control+jwt"
TYP_ENTITLEMENT = "aidocs-entitlement+jwt"
TYP_PAIRING_PROOF = "aidocs-pairing-proof+jwt"
WIRE_TYPS = frozenset({TYP_REQUEST, TYP_CONTROL, TYP_ENTITLEMENT, TYP_PAIRING_PROOF})

_HEADER_MEMBERS = frozenset({"alg", "kid", "typ"})
_KID_RE = re.compile(r"[A-Za-z0-9._:-]{1,100}")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class JwsError(ValueError):
    """The JWS (or a key / claim it depends on) is refused."""


@dataclass(frozen=True)
class VerifiedJws:
    header: dict
    payload: dict


# -- base64url ---------------------------------------------------------------


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict base64url without padding; only the one canonical spelling."""
    if type(text) is not str or not text.isascii() or not _B64URL_RE.fullmatch(text):
        raise JwsError("segment is not unpadded base64url")
    if len(text) % 4 == 1:
        raise JwsError("segment is not unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise JwsError("segment is not unpadded base64url") from exc
    if b64url_encode(raw) != text:
        raise JwsError("segment is not canonical base64url")
    return raw


# -- keys --------------------------------------------------------------------


def _require_p256_private(key: Any) -> ec.EllipticCurvePrivateKey:
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise JwsError("signing key is not a P-256 private key")
    return key


def _require_p256_public(key: Any) -> ec.EllipticCurvePublicKey:
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise JwsError("verification key is not a P-256 public key")
    return key


def _coord(jwk: Mapping[str, Any], name: str) -> int:
    value = jwk.get(name)
    if type(value) is not str:
        raise JwsError(f"JWK member {name} is missing")
    raw = b64url_decode(value)
    if len(raw) != 32:
        raise JwsError(f"JWK member {name} is not a 32-octet P-256 coordinate")
    return int.from_bytes(raw, "big")


def public_key_from_jwk(jwk: Mapping[str, Any]) -> ec.EllipticCurvePublicKey:
    """A P-256 public key from ``{"kty":"EC","crv":"P-256",["kid",]"x","y"}`` (wire §1.3)."""
    if not isinstance(jwk, Mapping):
        raise JwsError("JWK is not an object")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise JwsError("JWK is not kty EC / crv P-256")
    if set(jwk) - {"kty", "crv", "kid", "x", "y"}:
        raise JwsError("JWK carries a member outside kty/crv/kid/x/y")
    if "kid" in jwk and (type(jwk["kid"]) is not str or not _KID_RE.fullmatch(jwk["kid"])):
        raise JwsError("JWK kid is outside the wire kid alphabet")
    x, y = _coord(jwk, "x"), _coord(jwk, "y")
    try:
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except ValueError as exc:
        raise JwsError("JWK point is not on P-256") from exc


def public_jwk(public_key: ec.EllipticCurvePublicKey, kid: str) -> dict:
    key = _require_p256_public(public_key)
    if type(kid) is not str or not _KID_RE.fullmatch(kid):
        raise JwsError("kid is outside the wire kid alphabet")
    nums = key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "x": b64url_encode(nums.x.to_bytes(32, "big")),
        "y": b64url_encode(nums.y.to_bytes(32, "big")),
    }


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638 thumbprint (SHA-256, base64url) over ``crv``, ``kty``, ``x``, ``y``."""
    public_key_from_jwk(jwk)  # validates shape and point
    members = {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]}
    return b64url_encode(hashlib.sha256(canonicalize(members)).digest())


# -- raw ES256 ---------------------------------------------------------------


def es256_sign_raw(data: bytes, private_key: ec.EllipticCurvePrivateKey) -> bytes:
    """ECDSA P-256 / SHA-256 over ``data``; returns the 64-octet ``R || S``."""
    key = _require_p256_private(private_key)
    r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def es256_verify_raw(data: bytes, signature: bytes, public_key: ec.EllipticCurvePublicKey) -> bool:
    """True only for a 64-octet ``R || S`` that verifies; DER is never accepted."""
    key = _require_p256_public(public_key)
    if type(signature) is not bytes or len(signature) != SIGNATURE_BYTES:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (0 < r < _P256_ORDER and 0 < s < _P256_ORDER):
        return False
    try:
        key.verify(encode_dss_signature(r, s), data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


# -- header / JSON -----------------------------------------------------------


def _check_header(header: Mapping[str, Any]) -> None:
    if not isinstance(header, Mapping) or set(header) != _HEADER_MEMBERS:
        raise JwsError("protected header members must be exactly alg, kid, typ")
    if header["alg"] != ALG or type(header["alg"]) is not str:
        raise JwsError("alg is not ES256")
    if type(header["typ"]) is not str or header["typ"] not in WIRE_TYPS:
        raise JwsError("typ is not a wire object type")
    if type(header["kid"]) is not str or not _KID_RE.fullmatch(header["kid"]):
        raise JwsError("kid is outside the wire kid alphabet")


def _refuse_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise JwsError("duplicate member name")
        out[key] = value
    return out


def _refuse_constant(_name: str) -> Any:
    raise JwsError("non-finite number literal")


def _parse_object(raw: bytes, what: str) -> dict:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant)
    except JwsError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise JwsError(f"{what} is not valid JSON") from exc
    if type(value) is not dict:
        raise JwsError(f"{what} is not a JSON object")
    return value


# -- compact JWS -------------------------------------------------------------


def signing_input(header: Mapping[str, Any], payload: Mapping[str, Any]) -> bytes:
    """``BASE64URL(JCS(header)) "." BASE64URL(JCS(payload))`` as ASCII bytes."""
    try:
        h = canonicalize(dict(header))
        p = canonicalize(dict(payload))
    except JcsError as exc:
        raise JwsError("header or payload is not canonicalizable JSON") from exc
    return (b64url_encode(h) + "." + b64url_encode(p)).encode("ascii")


def sign_compact(header: Mapping[str, Any], payload: Mapping[str, Any], private_key: ec.EllipticCurvePrivateKey) -> str:
    """Sign ``payload`` under ``header``; returns the compact JWS text."""
    _check_header(header)
    _require_p256_private(private_key)
    if not isinstance(payload, Mapping):
        raise JwsError("payload is not a JSON object")
    si = signing_input(header, payload)
    token = si.decode("ascii") + "." + b64url_encode(es256_sign_raw(si, private_key))
    if len(token) > MAX_ENCODED_BYTES:
        raise JwsError("encoded JWS exceeds 8 KiB")
    return token


def verify_compact(
    token: str,
    *,
    expected_typ: str,
    keyset: Mapping[str, ec.EllipticCurvePublicKey],
) -> VerifiedJws:
    """Verify a compact JWS against the ONE keyset its typ and binding select.

    Order: size, shape, header (members, alg, typ, kid), key lookup, signature,
    then the payload. Claims (``aud``, ``jti``, ``body_sha256``, ...) are the
    caller's object-specific checks; :func:`check_lifetime` covers §1.4.
    """
    if expected_typ not in WIRE_TYPS:
        raise JwsError("expected typ is not a wire object type")
    if type(token) is not str or not token.isascii() or len(token) > MAX_ENCODED_BYTES:
        raise JwsError("JWS is not ASCII text within 8 KiB")
    parts = token.split(".")
    if len(parts) != 3:
        raise JwsError("JWS is not a three-segment compact serialization")
    h_b64, p_b64, s_b64 = parts
    header = _parse_object(b64url_decode(h_b64), "protected header")
    _check_header(header)
    if header["typ"] != expected_typ:
        raise JwsError("typ names another object")
    key = keyset.get(header["kid"]) if isinstance(keyset, Mapping) else None
    if key is None:
        raise JwsError("kid is unknown in the selected keyset")
    _require_p256_public(key)
    signature = b64url_decode(s_b64)
    if len(signature) != SIGNATURE_BYTES:
        raise JwsError("signature is not exactly 64 octets R || S")
    if not es256_verify_raw((h_b64 + "." + p_b64).encode("ascii"), signature, key):
        raise JwsError("signature does not verify")
    payload = _parse_object(b64url_decode(p_b64), "payload")
    return VerifiedJws(header=header, payload=payload)


# -- lifetime (wire §1.4) ----------------------------------------------------


def check_lifetime(
    payload: Mapping[str, Any],
    *,
    now: int,
    max_lifetime_s: int,
    skew_s: int = MAX_CLOCK_SKEW_S,
) -> None:
    """Refuse unless ``0 < exp - iat <= max_lifetime_s`` and ``now`` is inside
    ``[iat - skew, exp + skew]``. Skew is capped at the sealed 10 s."""
    if type(skew_s) is not int or not 0 <= skew_s <= MAX_CLOCK_SKEW_S:
        raise JwsError("clock skew exceeds the sealed 10 s bound")
    iat = payload.get("iat")
    exp = payload.get("exp")
    if type(iat) is not int or type(exp) is not int:
        raise JwsError("iat and exp must be integer seconds")
    if not 0 < exp - iat <= max_lifetime_s:
        raise JwsError("exp - iat is outside the object's lifetime bound")
    if iat > now + skew_s:
        raise JwsError("iat is beyond the clock skew in the future")
    if now > exp + skew_s:
        raise JwsError("the object has expired")
