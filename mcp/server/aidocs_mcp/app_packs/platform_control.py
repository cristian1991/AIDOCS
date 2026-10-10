"""CodeNexus -> AIDOCS platform-owner control assertion for S18 App access.

This is deliberately separate from the RFC 0003 ES256 application wire and
from connector OAuth. CodeNexus authenticates/step-ups the human platform owner
and sends a short-lived internal assertion binding that verified human to one
exact CodeNexus ORGANIZATION, App-access action and raw HTTP request body.

#1134 target namespace: CodeNexus signs and owns ``organization_id``. AIDOCS
alone resolves it to its own tenant through the write-once org->tenant map
(:mod:`.org_tenant_map`). ``bind`` is the one authenticated mapping-creating
act; every other act requires an existing mapping. A ``tenant_id`` claim is
not part of this wire.

The shared internal S2S secret is never sent as this credential. It derives a
purpose-specific HMAC key so this token cannot be confused with existing raw
Bearer uses of AIDOCS_INTERNAL_S2S_SECRET.

Verification order is security-significant:
  server secret -> framing/header/signature -> exact claims -> principal/aud/
  action -> body digest -> lifetime -> resolve organization (lookup only)
  -> before_replay admission (body JSON, rate) -> issue mapping (bind only)
  -> replay consume LAST -> ControlPlaneActor.

A bad body, target/action mismatch, unmapped organization or malformed token
therefore never spends a valid JTI and never creates a mapping. Replay is per
RESOLVED AIDOCS tenant.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .binding_store import CREDENTIAL_PLATFORM_ADMIN_S2S, ControlPlaneActor
from .jcs import JcsError, canonicalize
from .jws import b64url_decode, b64url_encode

__all__ = [
    "PLATFORM_CONTROL_ACTIONS",
    "PLATFORM_CONTROL_MAX_LIFETIME_S",
    "PLATFORM_CONTROL_REFUSAL_CODES",
    "PLATFORM_CONTROL_RETRYABLE_CODES",
    "PLATFORM_CONTROL_SERVER_FAULT_CODES",
    "TYP_PLATFORM_CONTROL",
    "MemoryPlatformReplayStore",
    "OrganizationTenantResolver",
    "PlatformControlRefused",
    "PlatformControlVerified",
    "PlatformReplayStore",
    "ReplayStoreUnavailable",
    "UnknownAuthority",
    "body_sha256",
    "derive_s2s_key",
    "require_platform_secret",
    "sign_platform_control",
    "verify_platform_control",
    "verify_platform_control_target",
]

TYP_PLATFORM_CONTROL = "codenexus-platform-control+jwt"
PLATFORM_CONTROL_MAX_LIFETIME_S = 60
PLATFORM_CONTROL_SKEW_S = 10
PLATFORM_CONTROL_ACTIONS = frozenset(
    {"bind", "pair-offer", "pair-confirm", "pair-status", "activate", "disable", "attestation-keys",
     "generation-attest"}
)
_MAPPING_ACTION = "bind"
_AUTHORITY_ACT = {
    "bind": "bind",
    "pair-offer": "pair",
    "pair-confirm": "pair",
    "pair-status": "pair",  # read-only; the same pair authority over the same tenant
    "activate": "activate",
    "disable": "disable",
    # #1134 B6: read-only; CodeNexus fetches the PUBLIC generation-attestation JWKS to pin.
    "attestation-keys": "attestation-keys",
    # #1134 B6 (WIRE §9d): CodeNexus asks AIDOCS to attest a generation switch.
    "generation-attest": "generation-attest",
}

_ALG = "HS256"
_ISS = "codenexus"
_ROLE = "platform_owner"
_DOMAIN = b"aidocs/codenexus-platform-control/v1"
_MAX_TOKEN_BYTES = 4096
_MIN_SECRET_BYTES = 32
_HEADER_MEMBERS = frozenset({"alg", "typ"})
_CLAIM_MEMBERS = frozenset(
    {
        "iss",
        "aud",
        "sub",
        "role",
        "iat",
        "exp",
        "jti",
        "organization_id",
        "action",
        "body_sha256",
    }
)
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


# WIRE-1134 §10.1: one typed INTERNAL reason per refusal (audit/vectors only).
# The wire stays non-disclosing; the HTTP mapping lives in app_access.
MALFORMED = "platform_control_malformed"
HEADER_INVALID = "platform_control_header_invalid"
SIGNATURE_INVALID = "platform_control_signature_invalid"
CLAIMS_INVALID = "platform_control_claims_invalid"
PRINCIPAL_INVALID = "platform_control_principal_invalid"
AUDIENCE_MISMATCH = "platform_control_audience_mismatch"
ACTION_MISMATCH = "platform_control_action_mismatch"
BODY_MISMATCH = "platform_control_body_mismatch"
LIFETIME_INVALID = "platform_control_lifetime_invalid"
BODY_INVALID = "platform_control_body_invalid"  # authenticated, but the body is not a JSON object (400)
AUTHORITY_UNKNOWN = "platform_control_authority_unknown"  # no AIDOCS authority for the target (401)
REPLAYED = "platform_control_replayed"
REPLAY_UNAVAILABLE = "platform_control_replay_unavailable"
AUTHORITY_UNAVAILABLE = "platform_control_authority_unavailable"  # the org->tenant map faulted
UNAVAILABLE = "platform_control_unavailable"  # the S2S secret is missing or weak
PLATFORM_CONTROL_REFUSAL_CODES = frozenset(
    {
        MALFORMED,
        HEADER_INVALID,
        SIGNATURE_INVALID,
        CLAIMS_INVALID,
        PRINCIPAL_INVALID,
        AUDIENCE_MISMATCH,
        ACTION_MISMATCH,
        BODY_MISMATCH,
        LIFETIME_INVALID,
        BODY_INVALID,
        AUTHORITY_UNKNOWN,
        REPLAYED,
        REPLAY_UNAVAILABLE,
        AUTHORITY_UNAVAILABLE,
        UNAVAILABLE,
    }
)
# 503 app_internal. Only the TRANSIENT infrastructure faults are retryable; a
# missing secret is a deployment fault a retry cannot fix.
PLATFORM_CONTROL_SERVER_FAULT_CODES = frozenset({REPLAY_UNAVAILABLE, AUTHORITY_UNAVAILABLE, UNAVAILABLE})
PLATFORM_CONTROL_RETRYABLE_CODES = frozenset({REPLAY_UNAVAILABLE, AUTHORITY_UNAVAILABLE})


class PlatformControlRefused(ValueError):
    """A platform-control credential failed closed. Never carries token bytes.

    ``code`` is one member of the closed :data:`PLATFORM_CONTROL_REFUSAL_CODES`.
    """

    def __init__(self, message: str, *, code: str) -> None:
        if code not in PLATFORM_CONTROL_REFUSAL_CODES:
            raise ValueError("unknown platform-control refusal code")
        super().__init__(message)
        self.code = code


class ReplayStoreUnavailable(RuntimeError):
    """The replay authority cannot record a verifier (capacity, storage). A fault, never a replay."""


class UnknownAuthority(LookupError):
    """No AIDOCS authority exists for the resolved target: a normal, non-disclosing refusal."""


class PlatformReplayStore(Protocol):
    def consume(self, jti: str, *, keep_until: int, now: int) -> bool:
        """True = recorded fresh; False = DUPLICATE only. Any inability RAISES."""
        ...


class OrganizationTenantResolver(Protocol):
    """See :mod:`.org_tenant_map`: ``resolve`` -> tenant | None; ``issue`` -> tenant. Faults raise."""

    def resolve(self, organization_id: str) -> Optional[str]: ...

    def issue(self, organization_id: str, *, now: int) -> str: ...


class MemoryPlatformReplayStore:
    """Bounded in-process replay store for tests/non-production composition.

    Capacity exhaustion fails CLOSED by RAISING :class:`ReplayStoreUnavailable`.
    Unlike an ordinary cache, it never evicts an unexpired verifier because
    eviction would re-admit a replay. ``False`` means a duplicate and nothing else.
    """

    def __init__(self, *, max_keys: int = 10_000) -> None:
        if type(max_keys) is not int or max_keys < 1:
            raise ValueError("max_keys must be a positive integer")
        self._max = max_keys
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    def consume(self, jti: str, *, keep_until: int, now: int) -> bool:
        if type(jti) is not str or not _ID_RE.fullmatch(jti):
            raise ValueError("jti is not a valid verifier key")
        if type(keep_until) is not int or type(now) is not int or keep_until < now:
            raise ValueError("replay window is invalid")
        with self._lock:
            expired = [key for key, expiry in self._used.items() if expiry < now]
            for key in expired:
                self._used.pop(key, None)
            if jti in self._used:
                return False
            if len(self._used) >= self._max:
                raise ReplayStoreUnavailable("replay store is at capacity")
            self._used[jti] = keep_until
            return True


def body_sha256(body: bytes) -> str:
    if type(body) is not bytes:
        raise PlatformControlRefused("request body must be bytes", code=MALFORMED)
    return hashlib.sha256(body).hexdigest()


def _secret_bytes(secret: Any) -> bytes:
    if type(secret) is not str:
        raise PlatformControlRefused("platform control is unavailable", code=UNAVAILABLE)
    try:
        raw = secret.encode("utf-8")
    except UnicodeError as exc:
        raise PlatformControlRefused("platform control is unavailable", code=UNAVAILABLE) from exc
    if len(raw) < _MIN_SECRET_BYTES:
        raise PlatformControlRefused("platform control is unavailable", code=UNAVAILABLE)
    return raw


def derive_s2s_key(secret: Any, message: bytes) -> bytes:
    """A purpose-specific 32-byte key derived from the shared S2S secret:
    ``HMAC-SHA256(key=UTF-8(secret), msg=message)``. ``message`` carries the
    purpose domain (and any scope, e.g. a tenant). A missing / weak secret
    raises ``platform_control_unavailable``; the secret itself never leaves."""
    if type(message) is not bytes or not message:
        raise ValueError("derivation message must be non-empty bytes")
    return hmac.new(_secret_bytes(secret), message, hashlib.sha256).digest()


def _key(secret: Any) -> bytes:
    return derive_s2s_key(secret, _DOMAIN)


def _mac(secret: Any, signing_input: bytes) -> bytes:
    return hmac.new(_key(secret), signing_input, hashlib.sha256).digest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PlatformControlRefused("duplicate member name", code=MALFORMED)
        out[key] = value
    return out


def _constant(_name: str) -> Any:
    raise PlatformControlRefused("non-finite JSON number", code=MALFORMED)


def _parse(segment: str, what: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = b64url_decode(segment)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except PlatformControlRefused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PlatformControlRefused(f"{what} is invalid", code=MALFORMED) from exc
    if type(value) is not dict:
        raise PlatformControlRefused(f"{what} is not an object", code=MALFORMED)
    try:
        if canonicalize(value) != raw:
            raise PlatformControlRefused(f"{what} is not canonical", code=MALFORMED)
    except JcsError as exc:
        raise PlatformControlRefused(f"{what} is not canonical", code=MALFORMED) from exc
    return value, raw


def _signing_input(header: Mapping[str, Any], claims: Mapping[str, Any]) -> bytes:
    try:
        h = canonicalize(dict(header))
        p = canonicalize(dict(claims))
    except (JcsError, TypeError, ValueError) as exc:
        raise PlatformControlRefused("platform control object is not canonicalizable", code=MALFORMED) from exc
    return f"{b64url_encode(h)}.{b64url_encode(p)}".encode("ascii")


def sign_platform_control(secret: Any, claims: Mapping[str, Any]) -> str:
    """Reference signer for the internal wire and tests.

    Production CodeNexus implements these exact bytes on its side; AIDOCS never
    signs platform-control assertions.
    """
    if not isinstance(claims, Mapping):
        raise PlatformControlRefused("claims are not an object", code=MALFORMED)
    header = {"alg": _ALG, "typ": TYP_PLATFORM_CONTROL}
    si = _signing_input(header, claims)
    token = si.decode("ascii") + "." + b64url_encode(_mac(secret, si))
    if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:
        raise PlatformControlRefused("platform control token is too large", code=MALFORMED)
    return token


def _id(value: Any, name: str) -> str:
    if type(value) is not str or not _ID_RE.fullmatch(value):
        raise PlatformControlRefused(f"{name} is invalid", code=CLAIMS_INVALID)
    return value


def require_platform_secret(secret: Any) -> None:
    """Raise ``platform_control_unavailable`` when the S2S secret is missing or weak.

    A deployment fault (503), decided before any caller byte is examined so the
    typed reason never depends on the header, the token or the body.
    """
    _secret_bytes(secret)


@dataclass(frozen=True)
class PlatformControlVerified:
    """The authenticated actor plus the SIGNED organization it was resolved from."""

    actor: ControlPlaneActor
    organization_id: str


def verify_platform_control(token: Any, **kwargs: Any) -> ControlPlaneActor:
    """As :func:`verify_platform_control_target`, returning only the actor."""
    return verify_platform_control_target(token, **kwargs).actor


def verify_platform_control_target(
    token: Any,
    *,
    secret: Any,
    expected_audience: str,
    expected_action: str,
    body: bytes,
    now: int,
    organizations: OrganizationTenantResolver,
    replay_for_tenant: Callable[[str], Optional[PlatformReplayStore]],
    before_replay: Callable[[ControlPlaneActor], None] | None = None,
) -> PlatformControlVerified:
    """Authenticate, resolve the organization to its AIDOCS tenant, consume the jti LAST.

    ``before_replay`` receives the authenticated actor. On a first ``bind`` of an
    unmapped organization its ``authority_tenant_id`` is ``""``: the mapping is
    issued only AFTER the callback admits the request.

    ``replay_for_tenant`` returns the tenant's store, or ``None`` / raises
    :class:`UnknownAuthority` when no authority exists (a normal refusal); any
    other exception is an infrastructure fault (503).
    """
    require_platform_secret(secret)
    if type(token) is not str or not token.isascii() or len(token.encode("ascii")) > _MAX_TOKEN_BYTES:
        raise PlatformControlRefused("platform control token is invalid", code=MALFORMED)
    parts = token.split(".")
    if len(parts) != 3:
        raise PlatformControlRefused("platform control token is invalid", code=MALFORMED)
    h64, p64, s64 = parts
    header, _ = _parse(h64, "header")
    if set(header) != _HEADER_MEMBERS or header.get("alg") != _ALG or header.get("typ") != TYP_PLATFORM_CONTROL:
        raise PlatformControlRefused("platform control header is invalid", code=HEADER_INVALID)

    # Authenticate the payload bytes BEFORE any payload member becomes authority.
    try:
        signature = b64url_decode(s64)
    except ValueError as exc:
        raise PlatformControlRefused("platform control signature is invalid", code=SIGNATURE_INVALID) from exc
    si = f"{h64}.{p64}".encode("ascii")
    if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(signature, _mac(secret, si)):
        raise PlatformControlRefused("platform control signature is invalid", code=SIGNATURE_INVALID)

    claims, _ = _parse(p64, "claims")
    if set(claims) != _CLAIM_MEMBERS:
        raise PlatformControlRefused("platform control claims are invalid", code=CLAIMS_INVALID)
    if claims.get("iss") != _ISS or claims.get("role") != _ROLE:
        raise PlatformControlRefused("platform control principal is invalid", code=PRINCIPAL_INVALID)
    if type(expected_audience) is not str or not expected_audience or claims.get("aud") != expected_audience:
        raise PlatformControlRefused("platform control audience is invalid", code=AUDIENCE_MISMATCH)
    if expected_action not in PLATFORM_CONTROL_ACTIONS or claims.get("action") != expected_action:
        raise PlatformControlRefused("platform control action is invalid", code=ACTION_MISMATCH)

    sub = _id(claims.get("sub"), "sub")
    organization_id = _id(claims.get("organization_id"), "organization_id")
    jti = _id(claims.get("jti"), "jti")
    digest = claims.get("body_sha256")
    if type(digest) is not str or not _HEX64_RE.fullmatch(digest) or digest != body_sha256(body):
        raise PlatformControlRefused("platform control body binding is invalid", code=BODY_MISMATCH)

    iat, exp = claims.get("iat"), claims.get("exp")
    if type(iat) is not int or type(exp) is not int:
        raise PlatformControlRefused("platform control lifetime is invalid", code=LIFETIME_INVALID)
    if not 0 < exp - iat <= PLATFORM_CONTROL_MAX_LIFETIME_S:
        raise PlatformControlRefused("platform control lifetime is invalid", code=LIFETIME_INVALID)
    if type(now) is not int or iat > now + PLATFORM_CONTROL_SKEW_S or now > exp + PLATFORM_CONTROL_SKEW_S:
        raise PlatformControlRefused("platform control lifetime is invalid", code=LIFETIME_INVALID)

    # Authenticated from here on. Resolve the signed organization (lookup only).
    tenant_id = _resolve(organizations, organization_id)
    if tenant_id is None and expected_action != _MAPPING_ACTION:
        raise PlatformControlRefused("platform control target is unknown", code=AUTHORITY_UNKNOWN)

    act = _AUTHORITY_ACT[expected_action]
    # A transport may run authenticated, server-derived pre-state admission
    # (the JSON body shape, §5.8 pairing issuance limits) here. A refusal MUST
    # propagate without spending the assertion or creating a mapping.
    if before_replay is not None:
        before_replay(ControlPlaneActor(sub, CREDENTIAL_PLATFORM_ADMIN_S2S, tenant_id or "", act))

    if tenant_id is None:  # the authenticated, admitted first bind: AIDOCS issues the tenant
        tenant_id = _issue(organizations, organization_id, now)

    # Replay is the LAST authority check, per RESOLVED AIDOCS tenant.
    try:
        replay = replay_for_tenant(tenant_id)
    except UnknownAuthority as exc:
        raise PlatformControlRefused("platform control target is unknown", code=AUTHORITY_UNKNOWN) from exc
    except Exception as exc:  # noqa: BLE001 -- replay authority failure fails closed
        raise PlatformControlRefused(
            "platform control replay authority is unavailable", code=REPLAY_UNAVAILABLE
        ) from exc
    if replay is None:
        raise PlatformControlRefused("platform control target is unknown", code=AUTHORITY_UNKNOWN)
    try:
        consumed = replay.consume(jti, keep_until=exp + PLATFORM_CONTROL_SKEW_S, now=now)
    except Exception as exc:  # noqa: BLE001 -- full / storage fault: never reported as a replay
        raise PlatformControlRefused(
            "platform control replay authority is unavailable", code=REPLAY_UNAVAILABLE
        ) from exc
    if consumed is False:
        raise PlatformControlRefused("platform control assertion was already used", code=REPLAYED)
    if consumed is not True:
        raise PlatformControlRefused("platform control replay authority is unavailable", code=REPLAY_UNAVAILABLE)

    actor = ControlPlaneActor(
        user_id=sub,
        credential_class=CREDENTIAL_PLATFORM_ADMIN_S2S,
        authority_tenant_id=tenant_id,
        authority_act=act,
    )
    return PlatformControlVerified(actor, organization_id)


def _resolve(organizations: Any, organization_id: str) -> Optional[str]:
    try:
        tenant = organizations.resolve(organization_id)
    except Exception as exc:  # noqa: BLE001 -- mapping authority failure fails closed (503)
        raise PlatformControlRefused(
            "platform control target authority is unavailable", code=AUTHORITY_UNAVAILABLE
        ) from exc
    if tenant is not None and (type(tenant) is not str or not _TENANT_RE.fullmatch(tenant)):
        raise PlatformControlRefused("platform control target authority is unavailable", code=AUTHORITY_UNAVAILABLE)
    return tenant


def _issue(organizations: Any, organization_id: str, now: int) -> str:
    try:
        tenant = organizations.issue(organization_id, now=now)
    except Exception as exc:  # noqa: BLE001 -- mapping authority failure fails closed (503)
        raise PlatformControlRefused(
            "platform control target authority is unavailable", code=AUTHORITY_UNAVAILABLE
        ) from exc
    if type(tenant) is not str or not _TENANT_RE.fullmatch(tenant):
        raise PlatformControlRefused("platform control target authority is unavailable", code=AUTHORITY_UNAVAILABLE)
    return tenant
