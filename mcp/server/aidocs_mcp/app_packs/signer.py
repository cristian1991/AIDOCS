"""AIDOCS org request signer, core -- RFC 0003 v4.6.1 §5.8, §12.3 (build plan S3).

Signer SEMANTICS are owned by AIDOCS; custody is not:

* **One trust domain per org.** Each org has one request-signing keyset and
  one current private signer (§12.3). The kid and the key are selected
  server-side from ``(org_id, purpose)`` through a :class:`SignerDirectory`
  (AIDOCS's authority). :meth:`OrgRequestSigner.sign` has no parameter that
  can nominate a key or a kid, and a claims object carrying a protected-header
  member is refused.
* **Custody** is either a :class:`SigningKeySource` -- production, the
  CodeNexus vault (:mod:`.vault_key_source`), which signs the AIDOCS-built
  signing input for the AIDOCS-chosen kid and never releases the key; the
  returned signature is verified against the PINNED public key before use --
  or a :class:`KeySource` (tests) that returns the private key for the exact
  ``(org_id, purpose, kid)``; a returned key whose public half is not the one
  AIDOCS pinned for that kid is refused. Custody never chooses the kid. The
  durable directory is ``AppAuthorityDB.key_directory()`` (public material
  only).
* **Just in time, memory only.** The key is loaded for each signature and
  dropped when it returns. The signer holds no key, writes nothing to disk and
  logs nothing.
* **Signing selects ONE current signer; verification trusts the org KEYSET.**
  :class:`OrgRequestKeyset` is the org's one request-signing keyset with
  per-kid rotation state (§5.8): ``next`` (added, trusted during the old + new
  overlap, not yet signing), ``current`` (the one signer), ``retiring`` (the
  old key after the flip, still verifiable for >= max assertion lifetime + max
  clock skew, :data:`RETIRE_GRACE_S`), ``retired`` and ``revoked`` (an
  emergency revoke refuses the kid immediately). The signer asks it only for
  the current kid; verifiers ask it for :meth:`OrgRequestKeyset.verification_keyset`.
  The signer itself exposes no keyset, so a single-key view can never pose as
  the org's trust domain.
* **Fail closed.** Any directory or key-source failure, a missing current
  signer, or a mismatched / non-P-256 key raises :class:`SignerUnavailable`.
  No replacement key is ever generated. The exception carries a fixed reason
  code only; the key source's own error text (which may contain material) is
  dropped and never chained.

The request assertion claims themselves are built by the application path
(§3.6, §12.1); this module signs what it is given under the three AIDOCS-signed
wire types (wire §1.2): tool assertion, AIDOCS -> application control
assertion, AIDOCS pairing proof. It never signs an entitlement attestation.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence

from cryptography.hazmat.primitives.asymmetric import ec

from . import jws

__all__ = [
    "KEY_CURRENT",
    "KEY_NEXT",
    "KEY_RETIRED",
    "KEY_RETIRING",
    "KEY_REVOKED",
    "MAX_ASSERTION_LIFETIME_S",
    "PURPOSE_ORG_REQUEST",
    "RETIRE_GRACE_S",
    "SIGNABLE_TYPS",
    "CurrentSigner",
    "KeySource",
    "KeysetError",
    "NextSigner",
    "OrgRequestKeyset",
    "OrgRequestSigner",
    "SignerDirectory",
    "SignerError",
    "SignerUnavailable",
    "SigningKeySource",
    "StaticSignerDirectory",
]

PURPOSE_ORG_REQUEST = "org_request_signing"
MAX_ASSERTION_LIFETIME_S = 60
# §5.8 step 4 / routine RETIRE: the old kid stays verifiable for >= max
# assertion lifetime + max clock skew (§12.3) after the flip.
RETIRE_GRACE_S = MAX_ASSERTION_LIFETIME_S + jws.MAX_CLOCK_SKEW_S

KEY_NEXT = "next"
KEY_CURRENT = "current"
KEY_RETIRING = "retiring"
KEY_RETIRED = "retired"
KEY_REVOKED = "revoked"
SIGNABLE_TYPS = frozenset({jws.TYP_REQUEST, jws.TYP_CONTROL, jws.TYP_PAIRING_PROOF})
_LIFETIME_BOUND_TYPS = frozenset({jws.TYP_REQUEST, jws.TYP_CONTROL})
_HEADER_LIKE_CLAIMS = frozenset({"alg", "kid", "typ", "jku", "jwk", "x5u", "x5c", "x5t", "x5t#S256", "crit", "cty", "b64"})
_KID_RE = re.compile(r"[A-Za-z0-9._:-]{1,100}")


class SignerError(ValueError):
    """The caller's signing request is malformed; nothing was loaded or signed."""


class KeysetError(ValueError):
    """A keyset operation is out of order or its input is malformed; nothing changed."""


class SignerUnavailable(RuntimeError):
    """The org signer cannot sign right now. Fail closed; never retried with another key."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"org request signer unavailable ({reason})")
        self.reason = reason


@dataclass(frozen=True)
class CurrentSigner:
    """AIDOCS's record of an org's current signer: the kid and its pinned public key."""

    kid: str
    public_key: ec.EllipticCurvePublicKey

    def __post_init__(self) -> None:
        if type(self.kid) is not str or not _KID_RE.fullmatch(self.kid):
            raise SignerError("kid is outside the wire kid alphabet")
        if not isinstance(self.public_key, ec.EllipticCurvePublicKey) or not isinstance(
            self.public_key.curve, ec.SECP256R1
        ):
            raise SignerError("the pinned signer key must be a P-256 public key")


@dataclass(frozen=True)
class NextSigner(CurrentSigner):
    """The org's UNIQUE next request-signing key during a §5.8 rotation (public half only)."""


class SignerDirectory(Protocol):
    """AIDOCS-side authority: which kid is the org's current signer for a purpose.

    A directory that supports rotation re-pairing also answers ``next_keys``
    (every key in the ``next`` state); the signer requires exactly one.
    """

    def current(self, org_id: str, purpose: str) -> Optional[CurrentSigner]: ...


class KeySource(Protocol):
    """Custody that hands the private key for exactly ``(org_id, purpose, kid)``
    into process memory for one signature (tests: an in-memory source)."""

    def load(self, org_id: str, purpose: str, kid: str) -> Any: ...


class SigningKeySource(Protocol):
    """Custody that SIGNS and never releases the key (production:
    :class:`~.vault_key_source.VaultKeySource`). ``kid`` is AIDOCS's choice;
    returns the 64-octet ES256 ``R || S`` over exactly ``signing_input``."""

    def sign(self, org_id: str, purpose: str, kid: str, signing_input: bytes) -> bytes: ...


class StaticSignerDirectory:
    """An in-memory :class:`SignerDirectory` over a fixed mapping."""

    __slots__ = ("_entries", "_next")

    def __init__(
        self,
        entries: Mapping[tuple[str, str], CurrentSigner],
        *,
        next_entries: Optional[Mapping[tuple[str, str], Sequence[CurrentSigner]]] = None,
    ) -> None:
        checked: dict[tuple[str, str], CurrentSigner] = {}
        for key, entry in entries.items():
            if not isinstance(entry, CurrentSigner):
                raise SignerError("directory entries must be CurrentSigner records")
            checked[key] = entry
        nxt: dict[tuple[str, str], tuple[NextSigner, ...]] = {}
        for key, seq in (next_entries or {}).items():
            if not all(isinstance(e, CurrentSigner) for e in seq):
                raise SignerError("directory entries must be CurrentSigner records")
            nxt[key] = tuple(NextSigner(kid=e.kid, public_key=e.public_key) for e in seq)
        self._entries = checked
        self._next = nxt

    def current(self, org_id: str, purpose: str) -> Optional[CurrentSigner]:
        return self._entries.get((org_id, purpose))

    def next_keys(self, org_id: str, purpose: str) -> tuple[NextSigner, ...]:
        return self._next.get((org_id, purpose), ())

    def __repr__(self) -> str:
        return f"<StaticSignerDirectory entries={len(self._entries)}>"


@dataclass
class _KeyRecord:
    public_key: ec.EllipticCurvePublicKey
    state: str
    retire_after: Optional[int] = None


def _keyset_org(org_id: Any) -> str:
    if type(org_id) is not str or not org_id:
        raise KeysetError("org_id must be a non-empty string")
    return org_id


def _keyset_kid(kid: Any) -> str:
    if type(kid) is not str or not _KID_RE.fullmatch(kid):
        raise KeysetError("kid is outside the wire kid alphabet")
    return kid


def _keyset_public(key: Any) -> ec.EllipticCurvePublicKey:
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise KeysetError("keyset material must be a P-256 public key")
    return key


def _keyset_now(now: Any) -> int:
    if type(now) is not int:
        raise KeysetError("now must be integer seconds since the Unix epoch")
    return now


class OrgRequestKeyset:
    """The org's ONE request-signing keyset with rotation state (§5.8, §12.3).

    Serves two readers with different questions:

    * the signer, through :meth:`current` (the :class:`SignerDirectory`
      protocol): which ONE kid signs now;
    * verifiers, through :meth:`verification_keyset`: which kids are trusted at
      ``now`` -- ``next`` (overlap), ``current``, and ``retiring`` until
      ``flip + RETIRE_GRACE_S``. ``retired`` and ``revoked`` kids are refused.

    Transitions (routine RETIRE, §5.8 steps 1-4): :meth:`add_next` ->
    :meth:`flip` -> time. Emergency REVOKE: :meth:`revoke`, effective
    immediately; revoking the current kid leaves the org with no signer (signing
    fails closed) until an explicit :meth:`flip`. A kid or a public key is never
    reused within an org. Public keys only; this object never sees a private key.

    The §5.8 precondition of the flip (every active binding provisioned or
    disabled) belongs to the binding store and is not checked here.
    """

    __slots__ = ("_orgs", "_lock")

    def __init__(self) -> None:
        self._orgs: dict[str, dict[str, _KeyRecord]] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"<OrgRequestKeyset orgs={len(self._orgs)}>"

    def _keys(self, org_id: Any) -> dict[str, _KeyRecord]:
        keys = self._orgs.get(_keyset_org(org_id))
        if not keys:
            raise KeysetError("org has no request-signing keyset")
        return keys

    @staticmethod
    def _effective(record: _KeyRecord, now: int) -> str:
        if record.state == KEY_RETIRING and record.retire_after is not None and now > record.retire_after:
            # Monotonic: once past its grace the old kid is retired for good.
            record.state = KEY_RETIRED
            record.retire_after = None
        return record.state

    # -- transitions ---------------------------------------------------------

    def bootstrap(self, org_id: str, kid: str, public_key: ec.EllipticCurvePublicKey) -> None:
        """Install an org's first request-signing key as its current signer."""
        org_id, kid, public_key = _keyset_org(org_id), _keyset_kid(kid), _keyset_public(public_key)
        with self._lock:
            if self._orgs.get(org_id):
                raise KeysetError("org already has a request-signing keyset; rotate instead")
            self._orgs[org_id] = {kid: _KeyRecord(public_key, KEY_CURRENT)}

    def add_next(self, org_id: str, kid: str, public_key: ec.EllipticCurvePublicKey) -> None:
        """§5.8 step 1: add the new key; the old key remains the current signer."""
        kid, public_key = _keyset_kid(kid), _keyset_public(public_key)
        with self._lock:
            keys = self._keys(org_id)
            if any(r.state == KEY_NEXT for r in keys.values()):
                raise KeysetError("a rotation is already in progress")
            if kid in keys:
                raise KeysetError("kid was already used in this org")
            numbers = public_key.public_numbers()
            if any(r.public_key.public_numbers() == numbers for r in keys.values()):
                raise KeysetError("public key was already used in this org")
            keys[kid] = _KeyRecord(public_key, KEY_NEXT)

    def flip(self, org_id: str, *, now: int) -> None:
        """§5.8 step 3: the next key becomes current; the old one starts retiring."""
        now = _keyset_now(now)
        with self._lock:
            keys = self._keys(org_id)
            nxt = [kid for kid, r in keys.items() if r.state == KEY_NEXT]
            if not nxt:
                raise KeysetError("no provisioned next key to flip to")
            for record in keys.values():
                if record.state == KEY_CURRENT:
                    record.state = KEY_RETIRING
                    record.retire_after = now + RETIRE_GRACE_S
            keys[nxt[0]].state = KEY_CURRENT

    def revoke(self, org_id: str, kid: str) -> None:
        """Emergency REVOKE: the kid is refused immediately, whatever its state."""
        kid = _keyset_kid(kid)
        with self._lock:
            keys = self._keys(org_id)
            if kid not in keys:
                raise KeysetError("kid is not in this org's keyset")
            keys[kid].state = KEY_REVOKED
            keys[kid].retire_after = None

    # -- reads -----------------------------------------------------------------

    def key_state(self, org_id: str, kid: str, *, now: int) -> str:
        now = _keyset_now(now)
        with self._lock:
            keys = self._keys(org_id)
            if kid not in keys:
                raise KeysetError("kid is not in this org's keyset")
            return self._effective(keys[kid], now)

    def current(self, org_id: str, purpose: str) -> Optional[CurrentSigner]:
        """The ONE current signer (the :class:`SignerDirectory` protocol)."""
        if purpose != PURPOSE_ORG_REQUEST:
            return None
        with self._lock:
            keys = self._orgs.get(org_id) or {}
            for kid, record in keys.items():
                if record.state == KEY_CURRENT:
                    return CurrentSigner(kid=kid, public_key=record.public_key)
        return None

    def next_keys(self, org_id: str, purpose: str) -> tuple[NextSigner, ...]:
        """Every key in the ``next`` state (public half); revoked keys are excluded."""
        if purpose != PURPOSE_ORG_REQUEST:
            return ()
        with self._lock:
            keys = self._orgs.get(org_id) or {}
            return tuple(
                NextSigner(kid=kid, public_key=record.public_key)
                for kid, record in keys.items()
                if record.state == KEY_NEXT
            )

    def verification_keyset(self, org_id: str, *, now: int) -> dict[str, ec.EllipticCurvePublicKey]:
        """Kids trusted at ``now``: next, current, and retiring within the grace."""
        now = _keyset_now(now)
        with self._lock:
            keys = self._orgs.get(org_id) or {}
            return {
                kid: record.public_key
                for kid, record in keys.items()
                if self._effective(record, now) in (KEY_NEXT, KEY_CURRENT, KEY_RETIRING)
            }


def _check_claims(typ: Any, claims: Any) -> None:
    if typ not in SIGNABLE_TYPS:
        raise SignerError("typ is not an AIDOCS-signed wire type")
    if not isinstance(claims, Mapping):
        raise SignerError("claims must be a JSON object")
    if _HEADER_LIKE_CLAIMS & set(claims):
        raise SignerError("claims may not carry protected-header members")
    iat = claims.get("iat")
    if type(iat) is not int:
        raise SignerError("iat must be integer seconds")
    if typ in _LIFETIME_BOUND_TYPS:
        exp = claims.get("exp")
        if type(exp) is not int or not 0 < exp - iat <= MAX_ASSERTION_LIFETIME_S:
            raise SignerError("exp - iat must be within the 60 s assertion lifetime")


class OrgRequestSigner:
    """Signs AIDOCS wire objects with the org's current request-signing key.

    One purpose-specific exception (ruling feac1835-1f7, S7-B4): during a §5.8
    rotation the AIDOCS pairing proof for re-pairing is signed by the org's
    UNIQUE ``next`` key through :meth:`sign_next_pairing_proof`. The next kid
    is derived server-side; no method takes a kid.
    """

    __slots__ = ("_directory", "_key_source")

    def __init__(self, directory: SignerDirectory, key_source: KeySource) -> None:
        self._directory = directory
        self._key_source = key_source

    def __repr__(self) -> str:
        return "<OrgRequestSigner>"

    @staticmethod
    def _check_org(org_id: Any) -> str:
        if type(org_id) is not str or not org_id:
            raise SignerError("org_id must be a non-empty string")
        return org_id

    def _next(self, org_id: Any) -> NextSigner:
        self._check_org(org_id)
        failed = False
        entries: Any = None
        try:
            lookup = getattr(self._directory, "next_keys", None)
            entries = tuple(lookup(org_id, PURPOSE_ORG_REQUEST)) if lookup is not None else ()
        except Exception:  # noqa: BLE001 -- any directory failure fails closed
            failed = True
        if failed:
            raise SignerUnavailable("directory_failed")
        if not entries:
            raise SignerUnavailable("no_next_signer")
        if len(entries) != 1:
            raise SignerUnavailable("ambiguous_next_signer")
        entry = entries[0]
        if not isinstance(entry, CurrentSigner):
            raise SignerUnavailable("no_next_signer")
        return NextSigner(kid=entry.kid, public_key=entry.public_key)

    def offer_next_key(self, org_id: str, /) -> NextSigner:
        """The org's UNIQUE next key (kid + public key) for rotation re-pairing.

        Public material only; custody is not touched. Fails closed with
        :class:`SignerUnavailable` when there is no next key or more than one.
        """
        return self._next(org_id)

    def sign_next_pairing_proof(self, org_id: str, claims: Mapping[str, Any], /) -> str:
        """Sign an AIDOCS pairing proof with the org's UNIQUE next key.

        The typ is fixed to the pairing proof; the kid is the server-derived
        next kid. The loaded key must match that kid's pinned public half.
        """
        _check_claims(jws.TYP_PAIRING_PROOF, claims)
        entry = self._next(org_id)
        return self._sign_with(org_id, entry, jws.TYP_PAIRING_PROOF, claims)

    def _current(self, org_id: Any) -> CurrentSigner:
        self._check_org(org_id)
        failed = False
        entry: Any = None
        try:
            entry = self._directory.current(org_id, PURPOSE_ORG_REQUEST)
        except Exception:  # noqa: BLE001 -- any directory failure fails closed
            failed = True
        if failed:
            raise SignerUnavailable("directory_failed")
        if not isinstance(entry, CurrentSigner):
            raise SignerUnavailable("no_current_signer")
        return entry

    def sign(self, org_id: str, typ: str, claims: Mapping[str, Any], /) -> str:
        """Sign ``claims`` as ``typ`` for ``org_id``. The kid is never the caller's."""
        _check_claims(typ, claims)
        entry = self._current(org_id)
        return self._sign_with(org_id, entry, typ, claims)

    def _sign_with(self, org_id: str, entry: CurrentSigner, typ: str, claims: Mapping[str, Any]) -> str:
        """Load exactly ``entry.kid`` from custody, check its public half, sign.

        A :class:`SigningKeySource` (custody that signs and never releases the
        key) takes the mechanical path instead: see :meth:`_sign_remote`."""
        if callable(getattr(self._key_source, "sign", None)):
            return self._sign_remote(org_id, entry, typ, claims)
        failed = False
        key: Any = None
        try:
            key = self._key_source.load(org_id, PURPOSE_ORG_REQUEST, entry.kid)
        except Exception:  # noqa: BLE001 -- custody failure fails closed, text dropped
            failed = True
        try:
            if failed:
                raise SignerUnavailable("key_source_failed")
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
                raise SignerUnavailable("key_source_returned_no_p256_key")
            if key.public_key().public_numbers() != entry.public_key.public_numbers():
                raise SignerUnavailable("key_mismatch")
            header = {"alg": jws.ALG, "kid": entry.kid, "typ": typ}
            jws_failed = False
            try:
                token = jws.sign_compact(header, claims, key)
            except jws.JwsError:
                jws_failed = True
            if jws_failed:
                raise SignerError("claims could not be signed as a wire JWS")
            return token
        finally:
            key = None

    def _sign_remote(self, org_id: str, entry: CurrentSigner, typ: str, claims: Mapping[str, Any]) -> str:
        """Mechanical signing by custody (the CodeNexus vault).

        AIDOCS builds the header (its own kid) and the byte-exact signing input;
        custody returns 64 raw ``R || S`` octets for exactly that kid and input.
        The signature is VERIFIED against the PINNED public key of ``entry.kid``
        before it is used: a wrong key, a wrong kid or a malformed answer fails
        closed. Nothing is cached; custody's error text is dropped.
        """
        header = {"alg": jws.ALG, "kid": entry.kid, "typ": typ}
        try:
            si = jws.signing_input(header, claims)
        except jws.JwsError:
            raise SignerError("claims could not be signed as a wire JWS") from None
        failed = False
        signature: Any = None
        try:
            signature = self._key_source.sign(org_id, PURPOSE_ORG_REQUEST, entry.kid, si)
        except Exception:  # noqa: BLE001 -- custody failure fails closed, text dropped
            failed = True
        if failed:
            raise SignerUnavailable("key_source_failed")
        if type(signature) is not bytes or len(signature) != jws.SIGNATURE_BYTES:
            raise SignerUnavailable("signature_malformed")
        if not jws.es256_verify_raw(si, signature, entry.public_key):
            raise SignerUnavailable("signature_mismatch")
        token = si.decode("ascii") + "." + jws.b64url_encode(signature)
        if len(token) > jws.MAX_ENCODED_BYTES:
            raise SignerError("claims could not be signed as a wire JWS")
        return token
