"""The §9d GENERATION-SWITCH ATTESTATION -- #1134 B6 (WIRE-1134 §9d, R-20; r0b2 7317170d-d16).

CodeNexus switches ``AppInstallation.currentGeneration`` (CAS + audit) ONLY on
an AIDOCS attestation that the successor binding is ``active_eligible`` for the
exact organization / installation / toolspace / pack / contract / origin. The
attestation authorizes ONLY that trust switch: it grants no seat and admits
no user.

* **Key.** An AIDOCS-ONLY ES256 key of purpose
  ``generation_switch_attestation``. It is NOT the org request signer (whose
  custody is CodeNexus's and whose ``SIGNABLE_TYPS`` exclude this typ) and it
  is never held by CodeNexus: CN pins only the PUBLIC JWK / RFC 7638
  thumbprint (operator-provisioned). Rotation with overlap: one ``current``
  signing key plus published ``overlap`` public keys
  (:class:`GenerationAttestationKeyring`). The private key is loaded just in
  time per signature and its public half is checked against the pinned JWK;
  any mismatch or fault fails closed (:class:`AttestationUnavailable`).
* **Wire.** Compact JWS, header exactly ``{"alg": "ES256", "kid": <attestation
  kid>, "typ": "aidocs-generation-attestation+jwt"}``; claims EXACTLY
  :data:`CLAIMS`: ``iss`` (the AIDOCS issuer), ``aud`` = :data:`AUDIENCE`,
  ``iat``, ``exp`` (``exp - iat`` = 60), ``jti``, ``purpose`` =
  ``generation_switch_attestation``, ``issued_at`` (= ``iat``), ``status`` =
  ``active_eligible``, and the §9d facts ``organization_id``,
  ``aidocs_tenant_id``, ``installation_id``, ``toolspace_id``,
  ``predecessor_binding_id`` / ``predecessor_binding_generation`` (both JSON
  ``null`` for the first switch; otherwise CodeNexus's CAS anchor, signed
  exactly as given and never looked up -- it may name the SAME binding with an
  older generation, the in-place upgrade law, r0b2 1ec01c05-470),
  ``successor_binding_id``, ``successor_binding_generation``, ``pack_id``,
  ``contract_digest``, ``application_origin``, ``event_id``.
* **Request.** The platform-control action (``generation-attest``, C14 auth)
  is wired by the transport; it hands this module the VERIFIED platform actor
  (credential ``platform_admin_s2s``, ``authority_act`` =
  :data:`AUTHORITY_ACT`, bound to the resolved tenant) and the closed request
  body (:data:`REQUEST_MEMBERS`). AIDOCS checks the SUCCESSOR against its own
  binding store (the exact current ACTIVE + SEALED authority for the canonical
  facts); it signs in memory first and then writes the durable
  ``app_generation_attested`` DECISION row (no row, no attestation returned).

:func:`verify_generation_attestation` is the reference verifier CodeNexus
mirrors (and the shared vectors run through).
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import jws
from .boundary_audit import audit_metadata
from .binding_store import (
    CREDENTIAL_PLATFORM_ADMIN_S2S,
    BindingRecord,
    BindingRefused,
    BindingState,
    BindingStore,
    ControlPlaneActor,
)

__all__ = [
    "AUDIENCE",
    "AUTHORITY_ACT",
    "CLAIMS",
    "ENV_KEY_FILE",
    "ENV_KID",
    "ENV_OVERLAP_JWKS",
    "KEY_PURPOSE",
    "LIFETIME_S",
    "PURPOSE",
    "REQUEST_MEMBERS",
    "STATUS_ACTIVE_ELIGIBLE",
    "TYP",
    "AttestationRefused",
    "AttestationUnavailable",
    "GenerationAttestation",
    "GenerationAttestationKeyring",
    "KEYRING_STATE",
    "attest_generation_switch",
    "attestation_keyring",
    "generation_attest_handler",
    "keyring_from_env",
    "promote_next_attestation_key",
    "stage_next_attestation_key",
    "verify_generation_attestation",
]

TYP = jws.TYP_GENERATION_ATTESTATION
PURPOSE = "generation_switch_attestation"
KEY_PURPOSE = PURPOSE
AUDIENCE = "codenexus-generation-switch"
STATUS_ACTIVE_ELIGIBLE = "active_eligible"
LIFETIME_S = 60
AUTHORITY_ACT = "generation-attest"
ENV_KEY_FILE = "AIDOCS_GENERATION_ATTESTATION_KEY_FILE"
ENV_KID = "AIDOCS_GENERATION_ATTESTATION_KID"
ENV_OVERLAP_JWKS = "AIDOCS_GENERATION_ATTESTATION_OVERLAP_JWKS"

REQUEST_MEMBERS = frozenset({
    "installation_id", "toolspace_id", "predecessor_binding_id", "predecessor_binding_generation",
    "successor_binding_id", "successor_binding_generation", "pack_id", "contract_digest",
    "application_origin", "event_id",
})
CLAIMS = frozenset({
    "iss", "aud", "iat", "exp", "jti", "purpose", "issued_at", "status",
    "organization_id", "aidocs_tenant_id", "installation_id", "toolspace_id",
    "predecessor_binding_id", "predecessor_binding_generation",
    "successor_binding_id", "successor_binding_generation",
    "pack_id", "contract_digest", "application_origin", "event_id",
})
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_GENERATION_RE = re.compile(r"gen_[A-Za-z0-9_-]{43}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ORIGIN_RE = re.compile(r"https://[!-~]{1,512}")


class AttestationRefused(ValueError):
    """The attestation is refused; ``reason`` is a typed internal code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AttestationUnavailable(RuntimeError):
    """The attestation key cannot sign right now. Fail closed; never another key."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"generation attestation unavailable ({reason})")
        self.reason = reason


@dataclass(frozen=True)
class GenerationAttestation:
    token: str
    header: dict
    claims: dict


def _plain_jwk(jwk: Mapping[str, Any]) -> dict:
    try:
        key = jws.public_key_from_jwk(jwk)
        kid = jwk.get("kid")
        return jws.public_jwk(key, kid)
    except (jws.JwsError, TypeError):
        raise AttestationUnavailable("jwk_invalid") from None


class GenerationAttestationKeyring:
    """The AIDOCS generation-attestation keyset: ONE current signer + overlap public keys.

    ``key_source(kid) -> P-256 private key`` is called just in time for each
    signature; nothing is cached.
    """

    def __init__(
        self,
        *,
        current_jwk: Mapping[str, Any],
        key_source: Callable[[str], Any],
        overlap_jwks: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._current = _plain_jwk(current_jwk)
        overlap = [_plain_jwk(j) for j in overlap_jwks]
        kids = [self._current["kid"], *(j["kid"] for j in overlap)]
        thumbs = [jws.jwk_thumbprint(j) for j in (self._current, *overlap)]
        if len(set(kids)) != len(kids) or len(set(thumbs)) != len(thumbs):
            raise AttestationUnavailable("keyring_duplicate")
        self._overlap = tuple(overlap)
        self._source = key_source

    @property
    def current_kid(self) -> str:
        return self._current["kid"]

    def jwks(self) -> dict:
        """The published JWKS (current first, then the overlap keys): what CodeNexus pins."""
        return {"keys": [dict(self._current), *(dict(j) for j in self._overlap)]}

    def sign(self, claims: Mapping[str, Any]) -> tuple[str, dict]:
        header = {"alg": jws.ALG, "kid": self._current["kid"], "typ": TYP}
        try:
            key = self._source(self._current["kid"])
        except Exception:  # noqa: BLE001 -- custody fault: the reason text may hold material
            raise AttestationUnavailable("key_source") from None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise AttestationUnavailable("key_invalid")
        if jws.public_jwk(key.public_key(), self._current["kid"]) != self._current:
            raise AttestationUnavailable("key_mismatch")
        try:
            token = jws.sign_compact(header, dict(claims), key)
        except jws.JwsError:
            raise AttestationUnavailable("sign_failed") from None
        return token, header


# -- gate-held custody (r0b2 f52809cd-58b) --------------------------------------------------------------------
#
# The attestation key is a DEDICATED AIDOCS key in the gate key store
# (:mod:`.gate_key_store`, the ``~/.aidocs/trust`` 0600 pattern), independent of
# the S2S secret. Files:
#   generation-attestation-<kid>.pem          PKCS#8 private key, owner-only
#   generation-attestation-keyring-v1.json    {"current", "next", "retiring"} (public material)
# Rotation is an explicit transition: stage NEXT (published, never signs) ->
# promote (NEXT signs; the old current stays published as ``retiring`` until
# ``retire_after``). The published JWKS is always derived from this state.

KEYRING_STATE = "generation-attestation-keyring-v1.json"
KEYRING_FORMAT = "aidocs.generation-attestation-keyring/v1"
DEFAULT_OVERLAP_S = 7 * 24 * 3600
MIN_OVERLAP_S = LIFETIME_S + jws.MAX_CLOCK_SKEW_S + 1


def _store(store: Any) -> Any:
    from .gate_key_store import GateKeyStore

    return store if isinstance(store, GateKeyStore) else GateKeyStore(store)


def _pem_name(kid: str) -> str:
    return f"generation-attestation-{kid}.pem"


def _new_key(store: Any) -> dict:
    """Generate a P-256 key, store its PEM (owner-only) and return its public JWK (kid = thumbprint)."""
    key = ec.generate_private_key(ec.SECP256R1())
    kid = jws.jwk_thumbprint(jws.public_jwk(key.public_key(), "k"))
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    store.create_if_absent(_pem_name(kid), pem)
    del pem, key
    return {"kid": kid, "public_jwk": jws.public_jwk(_load_private(store, kid).public_key(), kid)}


def _load_private(store: Any, kid: str) -> Any:
    raw = store.read(_pem_name(kid))
    if raw is None:
        raise KeyError("kid")
    return serialization.load_pem_private_key(raw, password=None)


def _dump_state(state: Mapping[str, Any]) -> bytes:
    import json

    return json.dumps(state, sort_keys=True, separators=(",", ":")).encode("ascii")


def _parse_state(raw: bytes) -> dict:
    import json

    try:
        state = json.loads(raw)
        if state.get("format") != KEYRING_FORMAT:
            raise ValueError("format")
        for entry in [state["current"], *([state["next"]] if state.get("next") else []), *state["retiring"]]:
            pub = _plain_jwk(entry["public_jwk"])
            if pub["kid"] != entry["kid"] or jws.jwk_thumbprint(pub) != entry["kid"]:
                raise ValueError("kid")
    except AttestationUnavailable:
        raise
    except Exception:  # noqa: BLE001 -- a corrupt keyring is never replaced silently
        raise AttestationUnavailable("keyring_corrupt") from None
    return state


def _state(store: Any) -> dict:
    """The keyring state, provisioning the FIRST key atomically if absent."""
    raw = store.read(KEYRING_STATE)
    if raw is None:
        mine = _new_key(store)
        raw = store.create_if_absent(KEYRING_STATE, _dump_state(
            {"format": KEYRING_FORMAT, "current": mine, "next": None, "retiring": []}))
        state = _parse_state(raw)
        if state["current"]["kid"] != mine["kid"]:
            store.delete(_pem_name(mine["kid"]))  # another worker won; drop our orphan
        return state
    return _parse_state(raw)


def attestation_keyring(
    store: Any,
    *,
    environ: Optional[Mapping[str, str]] = None,
    now: Optional[int] = None,
) -> GenerationAttestationKeyring:
    """THE production keyring, zero manual steps.

    ``store`` is a :class:`~.gate_key_store.GateKeyStore` (or its directory).
    ``AIDOCS_GENERATION_ATTESTATION_KEY_FILE`` / ``_KID`` are an OPTIONAL
    override (:func:`keyring_from_env`, nothing is provisioned). Otherwise the
    first call auto-provisions the key (atomic; racing workers converge), and
    later calls / restarts load the same kid. Published = current + staged next
    + retiring keys whose ``retire_after`` is still in the future.
    """
    import time

    from .gate_key_store import GateKeyStoreUnavailable

    env = os.environ if environ is None else environ
    if env.get(ENV_KEY_FILE) or env.get(ENV_KID):
        return keyring_from_env(env)
    at = int(time.time()) if now is None else now
    st = _store(store)
    try:
        state = _state(st)
    except (GateKeyStoreUnavailable, OSError, ValueError):
        raise AttestationUnavailable("key_store") from None
    current = state["current"]
    published = ([state["next"]["public_jwk"]] if state.get("next") else []) + [
        r["public_jwk"] for r in state["retiring"] if r["retire_after"] > at
    ]

    def load(want: str) -> Any:
        if want != current["kid"]:
            raise KeyError("kid")
        return _load_private(st, want)

    return GenerationAttestationKeyring(current_jwk=current["public_jwk"], key_source=load, overlap_jwks=published)


def stage_next_attestation_key(store: Any) -> str:
    """Rotation step 1: create the NEXT key (published for pinning, never signs). Idempotent."""
    from .gate_key_store import GateKeyStoreUnavailable

    st = _store(store)
    try:
        with st.locked():
            state = _state(st)
            if state.get("next"):
                return state["next"]["kid"]
            state["next"] = _new_key(st)
            st.replace(KEYRING_STATE, _dump_state(state))
            return state["next"]["kid"]
    except (GateKeyStoreUnavailable, OSError, ValueError):
        raise AttestationUnavailable("key_store") from None


def promote_next_attestation_key(store: Any, *, now: int, overlap_s: int = DEFAULT_OVERLAP_S) -> str:
    """Rotation step 2: NEXT becomes current; the old current stays published
    (``retiring``) until ``now + overlap_s``. Refused without a staged NEXT or
    with an overlap shorter than one attestation lifetime + skew."""
    from .gate_key_store import GateKeyStoreUnavailable

    if type(overlap_s) is not int or overlap_s < MIN_OVERLAP_S or type(now) is not int:
        raise AttestationUnavailable("overlap_too_short")
    st = _store(store)
    try:
        with st.locked():
            state = _state(st)
            if not state.get("next"):
                raise AttestationUnavailable("no_next_key")
            old = state["current"]
            state["retiring"] = [r for r in state["retiring"] if r["retire_after"] > now] + [
                {**old, "retire_after": now + overlap_s}
            ]
            state["current"], state["next"] = state["next"], None
            st.replace(KEYRING_STATE, _dump_state(state))
            return state["current"]["kid"]
    except (GateKeyStoreUnavailable, OSError, ValueError):
        raise AttestationUnavailable("key_store") from None


def keyring_from_env(environ: Optional[Mapping[str, str]] = None) -> GenerationAttestationKeyring:
    """Production keyring: ``AIDOCS_GENERATION_ATTESTATION_KEY_FILE`` (a PKCS#8 PEM
    P-256 private key held ONLY by AIDOCS), ``AIDOCS_GENERATION_ATTESTATION_KID``
    and optional ``AIDOCS_GENERATION_ATTESTATION_OVERLAP_JWKS`` (a JSON array of
    the previous public JWKs kept published during a rotation overlap)."""
    import json

    env = os.environ if environ is None else environ
    path, kid = env.get(ENV_KEY_FILE, ""), env.get(ENV_KID, "")
    if not path or not kid:
        raise AttestationUnavailable("not_configured")
    pem = Path(path)

    def load(want: str) -> Any:
        if want != kid:
            raise KeyError("kid")
        return serialization.load_pem_private_key(pem.read_bytes(), password=None)

    try:
        current = jws.public_jwk(load(kid).public_key(), kid)
        overlap = json.loads(env.get(ENV_OVERLAP_JWKS) or "[]")
        if type(overlap) is not list:
            raise ValueError("overlap")
    except AttestationUnavailable:
        raise
    except Exception:  # noqa: BLE001 -- unreadable / malformed key or config fails closed
        raise AttestationUnavailable("key_file") from None
    return GenerationAttestationKeyring(current_jwk=current, key_source=load, overlap_jwks=overlap)


# -- attest ---------------------------------------------------------------------------------------------------


def _check_request(req: Any) -> dict:
    if not isinstance(req, Mapping) or set(req) != REQUEST_MEMBERS:
        raise AttestationRefused("attestation_request_invalid")
    r = dict(req)
    for name in ("installation_id", "toolspace_id", "successor_binding_id", "pack_id", "event_id"):
        if type(r[name]) is not str or not _ID_RE.fullmatch(r[name]):
            raise AttestationRefused("attestation_request_invalid")
    if type(r["successor_binding_generation"]) is not str or not _GENERATION_RE.fullmatch(
        r["successor_binding_generation"]
    ):
        raise AttestationRefused("attestation_request_invalid")
    if type(r["contract_digest"]) is not str or not _HEX64.fullmatch(r["contract_digest"]):
        raise AttestationRefused("attestation_request_invalid")
    if type(r["application_origin"]) is not str or not _ORIGIN_RE.fullmatch(r["application_origin"]):
        raise AttestationRefused("attestation_request_invalid")
    # r0b2 1ec01c05-470: the predecessor pair is CodeNexus's CAS anchor / request context.
    # SHAPE + pairing only (both null, or a well-formed id + gen_ generation); it is never
    # looked up and no historical state is claimed. The SAME binding is allowed (the in-place
    # upgrade law: same bnd_, reseal G1 -> G2) iff the generations differ.
    pid, pgen = r["predecessor_binding_id"], r["predecessor_binding_generation"]
    if (pid is None) != (pgen is None):
        raise AttestationRefused("attestation_request_invalid")
    if pid is not None and (
        type(pid) is not str or not _ID_RE.fullmatch(pid)
        or type(pgen) is not str or not _GENERATION_RE.fullmatch(pgen)
        or (pid == r["successor_binding_id"] and pgen == r["successor_binding_generation"])
    ):
        raise AttestationRefused("attestation_request_invalid")
    return r


def _get(store: BindingStore, binding_id: str, tenant_id: str) -> Optional[BindingRecord]:
    try:
        rec = store.get(binding_id)
    except BindingRefused:
        return None
    return rec if rec.tenant_id == tenant_id else None


def attest_generation_switch(
    store: BindingStore,
    keyring: GenerationAttestationKeyring,
    *,
    actor: ControlPlaneActor,
    organization_id: str,
    tenant_id: str,
    request: Mapping[str, Any],
    issuer: str,
    now: int,
    jti: Optional[str] = None,
) -> GenerationAttestation:
    """Attest that ``request.successor_*`` is the CURRENT sealed, ACTIVE binding
    for the exact org/tenant/toolspace/pack/contract/origin, then sign.

    Refusals raise :class:`AttestationRefused` (typed ``reason``; audited as
    ``generation_attestation_refused``); a key or audit fault raises
    :class:`AttestationUnavailable`.
    """
    def refuse(reason: str, binding_id: Optional[str] = None) -> AttestationRefused:
        code = "forbidden" if reason == "forbidden" else "generation_attestation_refused"
        store.audit_refusal(actor, tenant_id if type(tenant_id) is str and tenant_id else "<unknown>",
                            binding_id, code)
        return AttestationRefused(reason)

    if not (
        isinstance(actor, ControlPlaneActor)
        and actor.credential_class == CREDENTIAL_PLATFORM_ADMIN_S2S
        and actor.authority_tenant_id == tenant_id
        and actor.authority_act == AUTHORITY_ACT
    ):
        raise refuse("forbidden")
    if type(organization_id) is not str or not _ID_RE.fullmatch(organization_id) or type(now) is not int:
        raise refuse("attestation_request_invalid")
    if type(issuer) is not str or not issuer:
        raise AttestationUnavailable("issuer_not_configured")
    try:
        r = _check_request(request)
    except AttestationRefused as exc:
        raise refuse(exc.reason) from None
    succ = _get(store, r["successor_binding_id"], tenant_id)
    if succ is None:
        raise refuse("attestation_binding_unknown")
    sid = succ.binding_id
    if succ.trust is None or succ.trust.binding_generation != r["successor_binding_generation"]:
        raise refuse("attestation_generation_mismatch", sid)
    if (
        succ.state is not BindingState.ACTIVE
        or succ.toolspace_id != r["toolspace_id"]
        or succ.pack_id != r["pack_id"]
        or succ.contract_digest != r["contract_digest"]
        or succ.application_origin != r["application_origin"]
    ):
        raise refuse("attestation_not_eligible", sid)
    # The predecessor is signed exactly as given (r0b2 1ec01c05-470): AIDOCS attests ONLY that
    # the successor is its exact current ACTIVE + SEALED authority for the canonical facts.
    claims = {
        "iss": issuer,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + LIFETIME_S,
        "jti": jti if jti is not None else secrets.token_urlsafe(24),
        "purpose": PURPOSE,
        "issued_at": now,
        "status": STATUS_ACTIVE_ELIGIBLE,
        "organization_id": organization_id,
        "aidocs_tenant_id": tenant_id,
        "installation_id": r["installation_id"],
        "toolspace_id": r["toolspace_id"],
        "predecessor_binding_id": r["predecessor_binding_id"],
        "predecessor_binding_generation": r["predecessor_binding_generation"],
        "successor_binding_id": sid,
        "successor_binding_generation": r["successor_binding_generation"],
        "pack_id": r["pack_id"],
        "contract_digest": r["contract_digest"],
        "application_origin": r["application_origin"],
        "event_id": r["event_id"],
    }
    # TRUTH BEFORE GREEN (r0b2 review of 0f9255092): sign IN MEMORY first; a key fault
    # leaves NO completed row. Only then the durable DECISION row; if THAT fails the
    # token is discarded: an attestation never leaves without its audit row.
    try:
        token, header = keyring.sign(claims)
    except AttestationUnavailable:
        raise
    except Exception:  # noqa: BLE001 -- key / custody fault: nothing attested, nothing recorded as done
        raise AttestationUnavailable("key_unavailable") from None
    try:
        store._emit(  # noqa: SLF001 -- the store's typed DECISION seam, same file as the bindings
            "app_generation_attested", actor, succ,
            **audit_metadata({
                "binding_generation": r["successor_binding_generation"], "event_id": r["event_id"],
                "installation_id": r["installation_id"], "attestation_kid": header.get("kid"),
            }),
        )
    except Exception:  # noqa: BLE001 -- no durable row: the signed token is dropped here
        del token
        raise AttestationUnavailable("audit_unavailable") from None
    return GenerationAttestation(token=token, header=header, claims=claims)


def generation_attest_handler(
    store_for_tenant: Callable[[str], BindingStore],
    keyring: Any,
    *,
    issuer: str,
) -> Callable[..., str]:
    """The ``AppAccessControl(generation_attest=...)`` handler: ``(actor, *,
    organization_id, request, now) -> compact JWS``. ``keyring`` is a
    :class:`GenerationAttestationKeyring` or a zero-argument callable returning
    one (so a rotation is picked up per call). The tenant is the VERIFIED
    actor's ``authority_tenant_id``, never request input."""

    def handle(actor: ControlPlaneActor, *, organization_id: str, request: Mapping[str, Any], now: int) -> str:
        tenant = getattr(actor, "authority_tenant_id", "")
        try:
            store = store_for_tenant(tenant)
        except Exception:  # noqa: BLE001 -- unknown authority is non-disclosing
            raise AttestationRefused("attestation_binding_unknown") from None
        ring = keyring() if callable(keyring) and not isinstance(keyring, GenerationAttestationKeyring) else keyring
        return attest_generation_switch(
            store, ring, actor=actor, organization_id=organization_id, tenant_id=tenant,
            request=request, issuer=issuer, now=now,
        ).token

    return handle


# -- the reference verifier (what CodeNexus mirrors) ---------------------------------------------------------


def verify_generation_attestation(
    token: str, *, jwks: Mapping[str, Any], issuer: str, now: int,
) -> dict:
    """Verify an attestation against the PINNED JWKS; returns the claims or
    raises :class:`AttestationRefused`. Checks: typ, kid in the pinned set,
    ES256 signature, exact claim set, purpose, aud, iss, status, lifetime
    (``0 < exp - iat <= 60``, skew 10 s), ``issued_at == iat``."""
    try:
        keys = jwks.get("keys") if isinstance(jwks, Mapping) else None
        keyset = {k["kid"]: jws.public_key_from_jwk(k) for k in keys}  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        raise AttestationRefused("jwks_invalid") from None
    try:
        verified = jws.verify_compact(token, expected_typ=TYP, keyset=keyset)
    except jws.JwsError:
        raise AttestationRefused("jose") from None
    c = verified.payload
    if set(c) != CLAIMS:
        raise AttestationRefused("claim_members")
    try:
        jws.check_lifetime(c, now=now, max_lifetime_s=LIFETIME_S)
    except jws.JwsError:
        raise AttestationRefused("lifetime") from None
    if c["purpose"] != PURPOSE or c["aud"] != AUDIENCE or c["iss"] != issuer:
        raise AttestationRefused("purpose_aud_iss")
    if c["status"] != STATUS_ACTIVE_ELIGIBLE:
        raise AttestationRefused("status")
    if type(c["issued_at"]) is not int or c["issued_at"] != c["iat"]:
        raise AttestationRefused("issued_at")
    if type(c["jti"]) is not str or not _ID_RE.fullmatch(c["jti"]):
        raise AttestationRefused("jti")
    if type(c["successor_binding_generation"]) is not str or not _GENERATION_RE.fullmatch(
        c["successor_binding_generation"]
    ):
        raise AttestationRefused("generation")
    return dict(c)
