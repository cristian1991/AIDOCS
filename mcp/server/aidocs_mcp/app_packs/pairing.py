"""The pairing ceremony, AIDOCS side -- RFC 0003 v4.6.1 §5.8; wire rev 8 §3, §4.

Offer (AIDOCS -> application) -> response (application -> AIDOCS) -> the
admin reviews the exact facts + comparison code -> confirmation (the AIDOCS
proof JWS, AIDOCS -> application). Mutual proof of possession over the exact
transcript; the binding's trust is recorded only after BOTH private keys have
proven possession (§5.8).

* The transcript bytes are RFC 8785 (JCS) via :mod:`.jcs`;
  ``transcript_sha256`` is lowercase hex SHA-256 of them; the comparison code is
  ``int(sha[:8], 16) mod 1 000 000`` as ``"ddd ddd"`` (wire §3.6). Reproduces
  wire Appendix A.1 byte for byte.
* ``pack_id`` / ``contract_digest`` / ``audience`` / ``toolspace_id`` come from
  the binding record, which pinned them FROM the S2 registry; an offer for a
  pack/digest the registry does not hold is refused. ``aidocs_origin`` /
  ``aidocs_issuer`` are this deployment's fixed identity.
* The pairing secret (>= 32 random bytes) is single-use: it is SPENT by the
  first redemption attempt that names it, whether that attempt succeeds or
  fails (§5.8, wire §3.3). Only its SHA-256 is held. Issuing a new offer for a
  binding voids that binding's earlier unspent offers.
* Refusals are ``app_pairing_invalid`` (non-disclosing), except a real
  contract-digest mismatch: ``app_contract_mismatch`` (wire §1.5, ruling O-1).
* The AIDOCS key goes through the narrow :class:`AidocsPairingKeys` seam: the
  offered public key and a proof signature. The adapter over S3 signs with the
  org's ONE current signer and the proof is re-verified under the OFFERED key,
  so a signer flip between offer and confirm fails closed.

* Org-key rotation (§5.8 steps 1-2, wire §3.7; S7-B4): a re-pair of an
  already-paired binding with ``org_key_rotation=True`` offers the org's
  UNIQUE NEXT key (S3 ``offer_next_key``) and the AIDOCS proof is signed by it
  (S3 ``sign_next_pairing_proof``). The first (bootstrap) pairing always uses
  the CURRENT key. The completed ceremony records the next kid as provisioned
  on the binding, which is what the §5.8 step-3 flip guard consults.

Rate limits (reviewer ruling on flag 7, feac1835-1f7): §5.8 requires issuance
and redemption to be rate-limited. That is NOT built here, so NO production
pairing route may be exposed until those limits land with the route wiring
(S18). Offer / review state lives behind :class:`PairingState`: production is
the durable ``AppAuthorityDB.pairing_state()`` (secret and review handle stored
as SHA-256 verifiers with expiry / spent state), where each step's state and
its DECISION row are ONE transaction; :class:`MemoryPairingState` is for tests.
"""
from __future__ import annotations

import calendar
import contextlib
import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from cryptography.hazmat.primitives.asymmetric import ec

from . import jws
from .binding_store import (
    BindingRefused,
    BindingState,
    BindingStore,
    BindingTrust,
    ControlPlaneActor,
    ControlPlaneAuditSink,
    ControlPlaneEvent,
    PackLookup,
    refusal_code_metadata,
)
from .jcs import JcsError, canonicalize
from .origin import PRODUCTION_ORIGIN_POLICY, OriginPolicy, OriginRefused, parse_origin, parse_url, same_origin
from .signer import PURPOSE_ORG_REQUEST, OrgRequestSigner, SignerDirectory

__all__ = [
    "CODE_CONTRACT_MISMATCH",
    "CODE_PAIRING_INVALID",
    "MAX_OFFER_LIFETIME_S",
    "OFFER_FORMAT",
    "PURPOSE_AIDOCS_PROOF",
    "PURPOSE_APPLICATION_PROOF",
    "RESPONSE_FORMAT",
    "SKEW_S",
    "TRANSCRIPT_FORMAT",
    "AidocsIdentity",
    "AidocsPairingKeys",
    "MemoryPairingState",
    "OrgSignerPairingKeys",
    "PairingState",
    "PairingRefused",
    "PairingReview",
    "PairingService",
    "comparison_code",
    "parse_rfc3339",
    "proof_claims",
    "rfc3339",
    "transcript_sha256",
    "validate_transcript_shape",
]

OFFER_FORMAT = "aidocs.pairing-offer/v1"
TRANSCRIPT_FORMAT = "aidocs.pairing-transcript/v1"
RESPONSE_FORMAT = "aidocs.pairing-response/v1"
PURPOSE_APPLICATION_PROOF = "aidocs.pairing.application_proof"
PURPOSE_AIDOCS_PROOF = "aidocs.pairing.aidocs_proof"
MAX_OFFER_LIFETIME_S = 600  # §5.8 "about 10 minutes"; wire §3.1 <= 10 min
SKEW_S = jws.MAX_CLOCK_SKEW_S
SECRET_BYTES = 32
MIN_APPLICATION_NONCE_BYTES = 24
CODE_PAIRING_INVALID = "app_pairing_invalid"
CODE_CONTRACT_MISMATCH = "app_contract_mismatch"

_KEY_REF_MEMBERS = frozenset({"kid", "thumbprint"})
_TRANSCRIPT_STR_MEMBERS = frozenset(
    {
        "format", "tenant_id", "binding_id", "pack_id", "toolspace_id", "contract_digest", "application_origin",
        "audience", "link_return_uri", "aidocs_origin", "aidocs_issuer", "aidocs_nonce", "application_nonce",
        "issued_at", "expires_at",
    }
)
_TRANSCRIPT_INT_MEMBERS = frozenset({"entitlement_keyset_version", "entitlement_revocation_epoch"})
TRANSCRIPT_MEMBERS = _TRANSCRIPT_STR_MEMBERS | _TRANSCRIPT_INT_MEMBERS | {
    "aidocs_key", "application_key", "entitlement_keys",
}
_RESPONSE_MEMBERS = frozenset(
    {"format", "transcript", "application_proof", "binding_control_key", "entitlement_keys", "pairing_secret"}
)
_PROOF_MEMBERS = frozenset({"purpose", "transcript_sha256", "iat"})
_OFFER_ECHO = (  # wire §3.3 step 1: transcript members that equal the offer's
    "tenant_id", "binding_id", "pack_id", "toolspace_id", "audience", "aidocs_origin", "aidocs_issuer",
    "aidocs_nonce", "issued_at", "expires_at",
)
_RFC3339_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class PairingRefused(Exception):
    """``code`` is the wire code; ``detail`` is internal only (never returned)."""

    def __init__(self, code: str = CODE_PAIRING_INVALID, detail: str = "") -> None:
        super().__init__(code + (f": {detail}" if detail else ""))
        self.code = code
        self.detail = detail


# -- pure wire helpers ----------------------------------------------------------


def transcript_sha256(transcript: Mapping[str, Any]) -> str:
    try:
        return hashlib.sha256(canonicalize(dict(transcript))).hexdigest()
    except (JcsError, TypeError) as exc:
        raise PairingRefused(detail="transcript is not canonicalizable") from exc


def comparison_code(sha_hex: str) -> str:
    """Wire §3.6: six digits from the first 8 hex chars, shown ``ddd ddd``."""
    if type(sha_hex) is not str or not _HEX64.match(sha_hex):
        raise PairingRefused(detail="not a sha256 hex digest")
    n = int(sha_hex[:8], 16) % 1_000_000
    digits = f"{n:06d}"
    return f"{digits[:3]} {digits[3:]}"


def proof_claims(purpose: str, sha_hex: str, iat: int) -> dict:
    """Wire §3.4 proof payload."""
    return {"purpose": purpose, "transcript_sha256": sha_hex, "iat": iat}


def rfc3339(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_rfc3339(value: Any) -> int:
    """Strict wire §1.1 time: RFC 3339 UTC, whole seconds, trailing ``Z``."""
    if type(value) is not str:
        raise PairingRefused(detail="time is not a string")
    m = _RFC3339_RE.fullmatch(value)
    if m is None:
        raise PairingRefused(detail="time is not RFC 3339 UTC whole seconds")
    parts = tuple(int(g) for g in m.groups())
    ts = calendar.timegm(parts + (0, 0, 0))
    if rfc3339(ts) != value:
        raise PairingRefused(detail="time is not a real calendar instant")
    return ts


def _key_ref(value: Any) -> dict:
    if not isinstance(value, Mapping) or set(value) != _KEY_REF_MEMBERS:
        raise PairingRefused(detail="key reference members")
    if any(type(value[k]) is not str or not value[k] for k in _KEY_REF_MEMBERS):
        raise PairingRefused(detail="key reference values")
    return dict(value)


def validate_transcript_shape(transcript: Any) -> None:
    """Wire §3.2: exactly the listed members, with their types. No extras."""
    if not isinstance(transcript, Mapping) or set(transcript) != TRANSCRIPT_MEMBERS:
        raise PairingRefused(detail="transcript members")
    for name in _TRANSCRIPT_STR_MEMBERS:
        if type(transcript[name]) is not str or not transcript[name]:
            raise PairingRefused(detail=f"transcript {name}")
    for name in _TRANSCRIPT_INT_MEMBERS:
        if type(transcript[name]) is not int:
            raise PairingRefused(detail=f"transcript {name}")
    if transcript["format"] != TRANSCRIPT_FORMAT:
        raise PairingRefused(detail="transcript format")
    _key_ref(transcript["aidocs_key"])
    _key_ref(transcript["application_key"])
    ents = transcript["entitlement_keys"]
    if not isinstance(ents, list) or not ents:
        raise PairingRefused(detail="transcript entitlement_keys")
    for ref in ents:
        _key_ref(ref)


# -- AIDOCS identity + key seam -----------------------------------------------------


@dataclass(frozen=True)
class AidocsIdentity:
    """This deployment's fixed ``aidocs_origin`` (canonical, §1.6) and ``aidocs_issuer``."""

    origin: str
    issuer: str
    origin_policy: OriginPolicy = PRODUCTION_ORIGIN_POLICY

    def __post_init__(self) -> None:
        try:
            canonical = parse_origin(self.origin, policy=self.origin_policy).serialized
        except OriginRefused as exc:
            raise ValueError("aidocs_origin is not a canonical origin") from exc
        if canonical != self.origin:
            raise ValueError("aidocs_origin must be given in its serialized canonical form")
        if type(self.issuer) is not str or not self.issuer:
            raise ValueError("aidocs_issuer must be a non-empty string")


class AidocsPairingKeys(Protocol):
    """Narrow seam to the AIDOCS org request key (S3). Never nominates a kid
    from a request; never exposes a private key. ``next_key`` selects the org's
    UNIQUE next key (rotation re-pair) instead of the current signer."""

    def offer_key(self, org_id: str, *, next_key: bool = False) -> tuple[str, ec.EllipticCurvePublicKey]: ...

    def sign_proof(self, org_id: str, claims: Mapping[str, Any], *, next_key: bool = False) -> str: ...


class OrgSignerPairingKeys:
    """Adapter over S3. Current key: the directory's ONE current signer, signed
    by :meth:`OrgRequestSigner.sign`. Next key: :meth:`OrgRequestSigner.offer_next_key`
    / :meth:`OrgRequestSigner.sign_next_pairing_proof` (fail closed on no or
    ambiguous next key). Never a verification keyset, never a kid argument."""

    def __init__(self, directory: SignerDirectory, signer: OrgRequestSigner) -> None:
        self._directory = directory
        self._signer = signer

    def offer_key(self, org_id: str, *, next_key: bool = False) -> tuple[str, ec.EllipticCurvePublicKey]:
        if next_key:
            entry = self._signer.offer_next_key(org_id)
        else:
            entry = self._directory.current(org_id, PURPOSE_ORG_REQUEST)
        if entry is None:
            raise PairingRefused(detail="org has no signer for this offer")
        return entry.kid, entry.public_key

    def sign_proof(self, org_id: str, claims: Mapping[str, Any], *, next_key: bool = False) -> str:
        if next_key:
            return self._signer.sign_next_pairing_proof(org_id, claims)
        return self._signer.sign(org_id, jws.TYP_PAIRING_PROOF, claims)


# -- state -----------------------------------------------------------------------------


@dataclass
class _Offer:
    tenant_id: str
    binding_id: str
    binding_version: int
    toolspace_id: str
    pack_id: str
    contract_digest: str
    audience: str
    aidocs_kid: str
    aidocs_jwk: dict
    aidocs_nonce: str
    issued_at: int
    expires_at: int
    offer: dict  # the wire object, minus the secret
    org_key_rotation: bool = False
    spent: bool = False


@dataclass(frozen=True)
class PairingReview:
    """What the dashboard shows the admin before accepting (RFC §5.8)."""

    review_id: str
    tenant_id: str
    binding_id: str
    pack_id: str
    toolspace_id: str
    application_origin: str
    audience: str
    aidocs_fingerprint: str
    application_fingerprint: str
    entitlement_fingerprints: tuple[str, ...]
    comparison_code: str
    transcript_sha256: str


@dataclass
class _PendingReview:
    offer: _Offer
    trust: BindingTrust
    review: PairingReview


class PairingState(Protocol):
    """Where offers and reviews live. Keys are SHA-256 VERIFIERS of the secret;
    a durable state also stores the review handle only as a verifier."""

    def transaction(self) -> Any: ...

    def put_offer(self, secret_sha256: str, offer: _Offer) -> None: ...

    def spend_offer(self, secret_sha256: str) -> tuple[Optional[_Offer], bool]: ...

    def put_review(self, review_id: str, pending: _PendingReview, secret_sha256: str) -> None: ...

    def take_review(self, review_id: str) -> Optional[_PendingReview]: ...


class MemoryPairingState:
    """In-process offers / reviews (tests; the durable one is ``authority_stores``)."""

    def __init__(self) -> None:
        self._offers: dict[str, _Offer] = {}  # sha256(secret) -> offer
        self._reviews: dict[str, _PendingReview] = {}

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    def put_offer(self, secret_sha256: str, offer: _Offer) -> None:
        for other in self._offers.values():  # one live ceremony per binding
            if other.binding_id == offer.binding_id:
                other.spent = True
        self._offers[secret_sha256] = offer

    def spend_offer(self, secret_sha256: str) -> tuple[Optional[_Offer], bool]:
        offer = self._offers.get(secret_sha256)
        if offer is None:
            return None, False
        was_spent = offer.spent
        offer.spent = True
        return offer, was_spent

    def put_review(self, review_id: str, pending: _PendingReview, secret_sha256: str) -> None:
        self._reviews[review_id] = pending

    def take_review(self, review_id: str) -> Optional[_PendingReview]:
        return self._reviews.pop(review_id, None)


class PairingService:
    def __init__(
        self,
        store: BindingStore,
        packs: PackLookup,
        keys: Optional[AidocsPairingKeys],
        identity: AidocsIdentity,
        audit_sink: ControlPlaneAuditSink,
        *,
        offer_lifetime_s: int = MAX_OFFER_LIFETIME_S,
        origin_policy: OriginPolicy = PRODUCTION_ORIGIN_POLICY,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        state: Optional["PairingState"] = None,
    ) -> None:
        if type(offer_lifetime_s) is not int or not 0 < offer_lifetime_s <= MAX_OFFER_LIFETIME_S:
            raise ValueError("offer lifetime must be 1..600 s (wire §3.1)")
        self._store = store
        self._packs = packs
        self._keys = keys
        self._identity = identity
        self._sink = audit_sink
        self._lifetime = offer_lifetime_s
        self._origin_policy = origin_policy
        self._random = random_bytes
        if state is None:
            state = MemoryPairingState()
            self._durable = False
        else:
            check = getattr(state, "check_sink", None)
            if callable(check):
                check(audit_sink)  # ruling 2: durable pairing state audits on its own file
            self._durable = True
        self._state = state
        self._lock = threading.RLock()

    # -- audit -----------------------------------------------------------------------

    def _event(self, kind: str, actor: Any, offer: Optional[_Offer], tenant_id: str = "", **meta: Any):
        """A typed pairing row. With an offer it carries the typed evidence of
        ruling 7db9a488: ``org_key_rotation`` (a real bool) and ``binding_version``."""
        user = actor.user_id if isinstance(actor, ControlPlaneActor) else "<unknown>"
        md: dict = dict(meta)
        if offer is not None:
            md.update(
                toolspace_id=offer.toolspace_id,
                pack_id=offer.pack_id,
                contract_digest=offer.contract_digest,
                binding_version=offer.binding_version,
                org_key_rotation=offer.org_key_rotation,
            )
        return ControlPlaneEvent(kind, offer.tenant_id if offer else tenant_id or "<unknown>", user,
                                 offer.binding_id if offer else None, md)

    def _audit(self, kind: str, actor: Any, offer: Optional[_Offer], tenant_id: str = "", **meta: Any) -> None:
        self._sink.emit(self._event(kind, actor, offer, tenant_id, **meta))

    def _refuse(self, actor: Any, offer: Optional[_Offer], exc: Exception, tenant_id: str = "") -> PairingRefused:
        code = exc.code if isinstance(exc, PairingRefused) else CODE_PAIRING_INVALID
        # Built OUTSIDE the sink guard: app_pairing_invalid -> control_refusal_code,
        # app_contract_mismatch -> refusal_code; an unknown code raises loudly.
        event = self._event("app_pairing_refused", actor, offer, tenant_id, **refusal_code_metadata(code))
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 -- the refusal stands even if its audit write fails
            pass
        return PairingRefused(code, getattr(exc, "detail", "") or type(exc).__name__)

    def _admin(self, actor: Any, tenant_id: str) -> None:
        try:
            self._store.require_admin(actor, tenant_id, act="pair")
        except BindingRefused as exc:
            raise PairingRefused(detail="not an org admin") from exc

    # -- step 1: offer ---------------------------------------------------------------

    def issue_offer(
        self, actor: ControlPlaneActor, binding_id: str, *, now: int, org_key_rotation: bool = False
    ) -> dict:
        tenant = ""
        try:
            rec = self._store.get(binding_id)
            tenant = rec.tenant_id
            self._admin(actor, rec.tenant_id)
            if type(org_key_rotation) is not bool:
                raise PairingRefused(detail="org_key_rotation must be a boolean")
            if rec.state is BindingState.DISABLED:
                raise PairingRefused(detail="binding disabled")
            if org_key_rotation and rec.trust is None:
                raise PairingRefused(detail="a first pairing is never an org-key rotation")
            try:
                self._packs.get(rec.pack_id, rec.contract_digest)
            except Exception as exc:  # noqa: BLE001 -- unknown pack/digest fails closed
                raise PairingRefused(detail="pack/digest not in the registry") from exc
            if self._keys is None:
                raise PairingRefused(detail="no AIDOCS key seam")
            kid, public_key = self._keys.offer_key(rec.tenant_id, next_key=org_key_rotation)
            aidocs_jwk = jws.public_jwk(public_key, kid)
        except (PairingRefused, BindingRefused, jws.JwsError) as exc:
            raise self._refuse(actor, None, exc, tenant) from None
        except Exception as exc:  # noqa: BLE001 -- key seam failure fails closed
            raise self._refuse(actor, None, exc, tenant) from None
        if type(now) is not int:
            raise PairingRefused(detail="now must be integer seconds")
        secret = jws.b64url_encode(self._random(SECRET_BYTES))
        nonce = jws.b64url_encode(self._random(SECRET_BYTES))
        wire = {
            "format": OFFER_FORMAT,
            "tenant_id": rec.tenant_id,
            "binding_id": rec.binding_id,
            "pack_id": rec.pack_id,
            "toolspace_id": rec.toolspace_id,
            "contract_digest": rec.contract_digest,
            "audience": rec.audience,
            "aidocs_origin": self._identity.origin,
            "aidocs_issuer": self._identity.issuer,
            "aidocs_key": aidocs_jwk,
            "aidocs_nonce": nonce,
            "issued_at": rfc3339(now),
            "expires_at": rfc3339(now + self._lifetime),
        }
        state = _Offer(
            tenant_id=rec.tenant_id, binding_id=rec.binding_id, binding_version=rec.binding_version,
            toolspace_id=rec.toolspace_id, pack_id=rec.pack_id, contract_digest=rec.contract_digest,
            audience=rec.audience, aidocs_kid=kid, aidocs_jwk=aidocs_jwk, aidocs_nonce=nonce,
            issued_at=now, expires_at=now + self._lifetime, offer=dict(wire),
            org_key_rotation=org_key_rotation,
        )
        with self._lock, self._state.transaction():
            # [REPORTED] binding_version and the org_key_rotation flag have no allowlisted key.
            self._audit("app_pairing_offered", actor, state)
            # one live ceremony per binding: put_offer voids the binding's earlier offers
            self._state.put_offer(hashlib.sha256(secret.encode("ascii")).hexdigest(), state)
        return {**wire, "pairing_secret": secret}

    # -- step 3a: response ---------------------------------------------------------------

    def redeem_response(self, actor: ControlPlaneActor, response: Any, *, now: int) -> PairingReview:
        secret = response.get("pairing_secret") if isinstance(response, Mapping) else None
        if type(secret) is not str or not secret.isascii():
            raise self._refuse(actor, None, PairingRefused(detail="no pairing secret"))
        key = hashlib.sha256(secret.encode("ascii")).hexdigest()
        with self._lock:
            # ATOMIC + committed: SPENT by this attempt, success or failure (§5.8).
            offer, was_spent = self._state.spend_offer(key)
            if offer is None or was_spent:
                raise self._refuse(actor, offer, PairingRefused(detail="secret unknown or spent"))
        try:
            self._admin(actor, offer.tenant_id)
            if type(now) is not int or now > offer.expires_at:
                raise PairingRefused(detail="offer expired")
            trust, review = self._verify(offer, response, now)
        except PairingRefused as exc:
            raise self._refuse(actor, offer, exc) from None
        except Exception as exc:  # noqa: BLE001 -- any malformed input fails closed
            raise self._refuse(actor, offer, exc) from None
        with self._lock, self._state.transaction():
            self._state.put_review(review.review_id, _PendingReview(offer, trust, review), key)
            self._audit("app_pairing_responded", actor, offer)
        return review

    def _verify(self, offer: _Offer, response: Mapping[str, Any], now: int) -> tuple[BindingTrust, PairingReview]:
        if set(response) != _RESPONSE_MEMBERS or response["format"] != RESPONSE_FORMAT:
            raise PairingRefused(detail="response members")
        t = response["transcript"]
        validate_transcript_shape(t)
        # 1. the transcript echoes the offer exactly
        for name in _OFFER_ECHO:
            if t[name] != offer.offer[name]:
                raise PairingRefused(detail=f"transcript {name} differs from the offer")
        if t["aidocs_key"] != {"kid": offer.aidocs_kid, "thumbprint": jws.jwk_thumbprint(offer.aidocs_jwk)}:
            raise PairingRefused(detail="transcript aidocs_key differs from the offer")
        if t["contract_digest"] != offer.contract_digest:
            raise PairingRefused(CODE_CONTRACT_MISMATCH, "contract digest mismatch")
        # 2. origins (§1.6): exact tuple equality with the binding's pinned origin
        rec = self._store.get(offer.binding_id)
        try:
            app_origin = parse_origin(t["application_origin"], policy=self._origin_policy)
            pinned = parse_origin(rec.application_origin, policy=self._origin_policy)
            link_origin, _path = parse_url(t["link_return_uri"], policy=self._origin_policy)
        except OriginRefused as exc:
            raise PairingRefused(detail="origin profile") from exc
        if not same_origin(app_origin, pinned) or not same_origin(link_origin, app_origin):
            raise PairingRefused(detail="origin mismatch")
        if t["entitlement_keyset_version"] < 1 or t["entitlement_revocation_epoch"] < 0:
            raise PairingRefused(detail="keyset version / epoch")
        if len(jws.b64url_decode(t["application_nonce"])) < MIN_APPLICATION_NONCE_BYTES:
            raise PairingRefused(detail="application nonce too short")
        # 3. keys: exactly the transcript's, one to one; control and entitlement disjoint
        bc_jwk = response["binding_control_key"]
        bc_pub = jws.public_key_from_jwk(bc_jwk)
        bc_ref = {"kid": bc_jwk.get("kid"), "thumbprint": jws.jwk_thumbprint(bc_jwk)}
        if bc_ref != t["application_key"]:
            raise PairingRefused(detail="binding-control key differs from the transcript")
        ent_jwks = response["entitlement_keys"]
        refs = t["entitlement_keys"]
        if not isinstance(ent_jwks, list) or len(ent_jwks) != len(refs):
            raise PairingRefused(detail="entitlement keys differ from the transcript")
        by_kid = {r["kid"]: r["thumbprint"] for r in refs}
        seen: set[str] = set()
        for jwk in ent_jwks:
            kid = jwk.get("kid") if isinstance(jwk, Mapping) else None
            if kid not in by_kid or kid in seen or jws.jwk_thumbprint(jwk) != by_kid[kid]:
                raise PairingRefused(detail="entitlement keys differ from the transcript")
            seen.add(kid)
        if len(by_kid) != len(refs):
            raise PairingRefused(detail="duplicate entitlement kid")
        if bc_ref["kid"] in by_kid or bc_ref["thumbprint"] in by_kid.values():
            raise PairingRefused(detail="binding-control and entitlement keys share a key or kid")
        # 4. the application proof over the exact transcript
        sha = transcript_sha256(t)
        try:
            verified = jws.verify_compact(
                response["application_proof"], expected_typ=jws.TYP_PAIRING_PROOF, keyset={bc_ref["kid"]: bc_pub}
            )
        except jws.JwsError as exc:
            raise PairingRefused(detail="application proof") from exc
        self._check_proof(verified.payload, PURPOSE_APPLICATION_PROOF, sha, offer)
        try:
            trust = BindingTrust(
                aidocs_issuer=t["aidocs_issuer"],
                aidocs_origin=t["aidocs_origin"],
                aidocs_kid=offer.aidocs_kid,
                aidocs_thumbprint=t["aidocs_key"]["thumbprint"],
                link_return_uri=t["link_return_uri"],
                binding_control_key=dict(bc_jwk),
                entitlement_keys=tuple(dict(k) for k in ent_jwks),
                entitlement_keyset_version=t["entitlement_keyset_version"],
                entitlement_revocation_epoch=t["entitlement_revocation_epoch"],
                transcript_sha256=sha,
            )
        except BindingRefused as exc:
            raise PairingRefused(detail=f"trust: {exc.code}") from exc
        review = PairingReview(
            review_id=secrets.token_urlsafe(18),
            tenant_id=offer.tenant_id,
            binding_id=offer.binding_id,
            pack_id=offer.pack_id,
            toolspace_id=offer.toolspace_id,
            application_origin=app_origin.serialized,
            audience=offer.audience,
            aidocs_fingerprint=t["aidocs_key"]["thumbprint"],
            application_fingerprint=bc_ref["thumbprint"],
            entitlement_fingerprints=tuple(r["thumbprint"] for r in refs),
            comparison_code=comparison_code(sha),
            transcript_sha256=sha,
        )
        return trust, review

    @staticmethod
    def _check_proof(payload: Mapping[str, Any], purpose: str, sha: str, offer: _Offer) -> None:
        if set(payload) != _PROOF_MEMBERS:
            raise PairingRefused(detail="proof members")
        if payload["purpose"] != purpose:
            raise PairingRefused(detail="proof purpose")
        if payload["transcript_sha256"] != sha:
            raise PairingRefused(detail="proof transcript hash")
        iat = payload["iat"]
        if type(iat) is not int or not offer.issued_at - SKEW_S <= iat <= offer.expires_at + SKEW_S:
            raise PairingRefused(detail="proof iat outside the offer window")

    # -- step 3b: admin accepts; AIDOCS proves possession -----------------------------------

    def confirm(self, actor: ControlPlaneActor, review_id: str, *, now: int) -> str:
        with self._lock:
            pending = self._state.take_review(review_id) if type(review_id) is str else None
        if pending is None:
            raise self._refuse(actor, None, PairingRefused(detail="review unknown or used"))
        offer = pending.offer
        try:
            self._admin(actor, offer.tenant_id)
            if type(now) is not int or not offer.issued_at - SKEW_S <= now <= offer.expires_at + SKEW_S:
                raise PairingRefused(detail="confirmation outside the offer window")
            claims = proof_claims(PURPOSE_AIDOCS_PROOF, pending.review.transcript_sha256, now)
            if self._keys is None:
                raise PairingRefused(detail="no AIDOCS key seam")
            token = self._keys.sign_proof(offer.tenant_id, claims, next_key=offer.org_key_rotation)
            # the proof must verify under the OFFERED key, as the AIDOCS proof
            offered = {offer.aidocs_kid: jws.public_key_from_jwk(offer.aidocs_jwk)}
            try:
                verified = jws.verify_compact(token, expected_typ=jws.TYP_PAIRING_PROOF, keyset=offered)
            except jws.JwsError as exc:
                raise PairingRefused(detail="AIDOCS proof is not by the offered key") from exc
            if verified.payload != claims:
                raise PairingRefused(detail="AIDOCS proof payload")
            if self._durable:
                # ONE transaction: the trust install, its row and the confirmation row.
                try:
                    with self._state.transaction():
                        self._store.install_trust(actor, offer.binding_id, pending.trust,
                                                  expected_version=offer.binding_version)
                        self._audit("app_pairing_confirmed", actor, offer)
                except BindingRefused as exc:
                    raise PairingRefused(detail=f"install: {exc.code}") from exc
                return token
            try:
                self._store.install_trust(actor, offer.binding_id, pending.trust,
                                          expected_version=offer.binding_version)
            except BindingRefused as exc:
                raise PairingRefused(detail=f"install: {exc.code}") from exc
        except PairingRefused as exc:
            raise self._refuse(actor, offer, exc) from None
        except Exception as exc:  # noqa: BLE001 -- signer / seam failure fails closed
            raise self._refuse(actor, offer, exc) from None
        self._audit("app_pairing_confirmed", actor, offer)
        return token
