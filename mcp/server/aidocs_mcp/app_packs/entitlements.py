"""Delegated entitlements: fetch / verify / cache (RFC 0003 slice S9).

Law: sealed RFC 0003 v4.6.1 §7.2-§7.6, §8.5, §11.2 step 3, §18.3; wire
companion rev 8 §5, §6 (§6.3 attestation + epoch table, §6.4 cache, §6.5
recovery-marked attestation), Appendix A.5, A.13, A.14.

* ONE grant authority (§7.2): ``delegated_application``. The application signs
  an entitlement attestation (compact ES256 JWS, ``typ``
  ``aidocs-entitlement+jwt``); AIDOCS verifies it against the BINDING's
  entitlement keyset only (§7.3, wire §1.3 kid namespace).
* Fresh-attestation checks in wire §6.3 order: typ/alg/kid, signature, aud,
  time bounds, tenant/binding/pack/contract/sub, capability vocabulary, then
  the epoch check (equal: continue; lower: refuse, refresh once, fail closed;
  higher: fail closed for the binding until trust is refreshed by a new
  ceremony).
* Cache (§7.4, wire §6.4): key ``(tenant_id, binding_id, sub,
  contract_digest)``. A hit is valid only while the kid is in the binding's
  CURRENT entitlement keyset, the cached ``revocation_epoch`` equals the
  binding's CURRENT one, and ``now < min(cached_at + TTL, exp)``; TTL <= 30 s.
  ``keyset_version`` is never read here. A hit never extends expiry; there is
  no stale-while-revalidate; a backend outage after expiry fails closed. The
  negative (unlinked / disabled) cache lives <= 5 s.
* Recovery (wire §6.5): an attestation carrying ``binding_state: "recovery"``
  is never used by the ordinary lookup and never cached; the ordinary lookup
  refuses it with ``app_backend_invalid_response``. The separate
  :meth:`EntitlementService.recheck_for_recovery` uses it only after AIDOCS
  itself (the S14 pending-record oracle) confirms that the exact
  ``mutation_ref`` is an OPEN pinned pending record under the CURRENT
  AppScope and inside THAT record's window -- checked before the call (no
  entitlements call when it already fails) and again before use. The marker
  never creates, extends or reopens a window.

The entitlements transport is an injected fetcher (S13 is later). Refusal
reasons are local diagnostics; only ``code`` is a platform code, and every
code here is AIDOCS-produced (§16.4). Nothing here echoes token bytes.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol

from . import jws
from .binding_store import BindingRecord, BindingState
from .bundle import DelegatedTerms
from .pairing import PairingRefused, parse_rfc3339
from .scope import AppScope

__all__ = [
    "APP_BACKEND_INVALID_RESPONSE",
    "APP_BACKEND_UNAVAILABLE",
    "APP_CONTRACT_MISMATCH",
    "APP_IDENTITY_UNLINKED",
    "APP_PACK_UNAVAILABLE",
    "APP_USER_DISABLED",
    "BINDING_STATE_RECOVERY",
    "CACHE_TTL_CEILING_S",
    "CACHE_TTL_DEFAULT_S",
    "DECLARED_ERROR_MAP",
    "DELEGATED_APPLICATION",
    "ENTITLEMENT_AUDIENCE",
    "ENTITLEMENT_TYP",
    "NEGATIVE_TTL_CEILING_S",
    "Entitlement",
    "EntitlementFetcher",
    "EntitlementRefused",
    "EntitlementService",
    "EntitlementTerms",
    "FetchResponse",
    "PendingRecordOracle",
    "PendingRecordView",
    "verify_attestation",
]

ENTITLEMENT_TYP = jws.TYP_ENTITLEMENT
ENTITLEMENT_AUDIENCE = "aidocs-pack-entitlements"  # §7.3
DELEGATED_APPLICATION = "delegated_application"  # §7.2: the only v1 authority
BINDING_STATE_RECOVERY = "recovery"  # wire §6.3 / §6.5
CACHE_TTL_CEILING_S = 30  # §7.4 hard ceiling
CACHE_TTL_DEFAULT_S = 15  # §7.4 recommended default
NEGATIVE_TTL_CEILING_S = 5  # §7.4

# §16.3 platform codes (AIDOCS-produced only, §16.4).
APP_BACKEND_INVALID_RESPONSE = "app_backend_invalid_response"
APP_BACKEND_UNAVAILABLE = "app_backend_unavailable"
APP_CONTRACT_MISMATCH = "app_contract_mismatch"
APP_IDENTITY_UNLINKED = "app_identity_unlinked"
APP_PACK_UNAVAILABLE = "app_pack_unavailable"
APP_USER_DISABLED = "app_user_disabled"

# wire §6.2: the entitlements endpoint's declared errors -> pre-egress refusals.
DECLARED_ERROR_MAP: Mapping[str, str] = MappingProxyType(
    {"crm_user_unlinked": APP_IDENTITY_UNLINKED, "crm_user_disabled": APP_USER_DISABLED}
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID_CLAIMS = ("iss", "jti", "sub", "app_principal_id", "tenant_id", "binding_id", "pack_id", "contract_digest")


class EntitlementRefused(Exception):
    """An entitlement resolution is refused (fail closed).

    ``code`` is the platform code; ``reason`` is a local diagnostic that names
    the failed check only. ``egress_attempted`` is False when the refusal was
    decided before any entitlements call.
    """

    def __init__(self, code: str, reason: str, *, egress_attempted: bool = True) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.egress_attempted = egress_attempted


class _EpochLower(EntitlementRefused):
    def __init__(self) -> None:
        super().__init__(APP_BACKEND_INVALID_RESPONSE, "revocation_epoch_lower")


class _EpochAhead(EntitlementRefused):
    def __init__(self) -> None:
        super().__init__(APP_BACKEND_INVALID_RESPONSE, "revocation_epoch_ahead")


class _Declared(EntitlementRefused):
    """A declared unlinked / disabled answer (wire §6.2)."""


def _invalid(reason: str, *, egress_attempted: bool = True) -> EntitlementRefused:
    return EntitlementRefused(APP_BACKEND_INVALID_RESPONSE, reason, egress_attempted=egress_attempted)


def _unavailable_pack(reason: str) -> EntitlementRefused:
    return EntitlementRefused(APP_PACK_UNAVAILABLE, reason, egress_attempted=False)


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_text(value: Any) -> bool:
    return type(value) is str and value != ""


# -- pack terms (§6.1 envelope, §7.3) ------------------------------------------


@dataclass(frozen=True)
class EntitlementTerms:
    """What the pinned bundle declares for delegated entitlements."""

    capabilities: frozenset
    audience: str
    max_attestation_ttl_seconds: int

    def __post_init__(self) -> None:
        caps = self.capabilities
        if not isinstance(caps, frozenset) or not caps or not all(_is_text(c) for c in caps):
            raise _unavailable_pack("pack_capability_vocabulary_invalid")
        if self.audience != ENTITLEMENT_AUDIENCE:
            raise _unavailable_pack("pack_entitlement_audience_invalid")
        ttl = self.max_attestation_ttl_seconds
        if not _is_int(ttl) or ttl < 1:
            raise _unavailable_pack("pack_attestation_ttl_invalid")

    @classmethod
    def from_bundle(cls, pack: Any) -> "EntitlementTerms":
        """Build the terms from S2's TYPED projection only (``PackBundle.capabilities``
        and ``PackBundle.delegated``, a :class:`~.bundle.DelegatedTerms`). The
        bundle bytes are never re-parsed here (reviewer S9-B1). Fails closed."""
        capabilities = getattr(pack, "capabilities", None)
        delegated = getattr(pack, "delegated", None)
        if (
            type(capabilities) is not tuple
            or not all(_is_text(c) for c in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise _unavailable_pack("pack_capability_vocabulary_invalid")
        if not isinstance(delegated, DelegatedTerms):
            raise _unavailable_pack("pack_not_delegated")
        return cls(
            capabilities=frozenset(capabilities),
            audience=delegated.entitlement_audience,
            max_attestation_ttl_seconds=delegated.max_attestation_ttl_seconds,
        )


# -- verified attestation --------------------------------------------------------


@dataclass(frozen=True)
class Entitlement:
    """A verified attestation's facts. ``recovery`` is True only for a
    ``binding_state: "recovery"`` attestation (never cached)."""

    tenant_id: str
    binding_id: str
    sub: str
    pack_id: str
    contract_digest: str
    app_principal_id: str
    capabilities_granted: tuple
    grants_version: int
    grants_digest: str
    revocation_epoch: int
    kid: str
    jti: str
    iat: int
    exp: int
    recovery: bool = False
    # #1134 B3 clause 5: the CRM's CURRENT seat references for this user (CAT
    # wire §4), present only when the user is seated. A bounded HINT compared
    # with the token family's references; the CRM's execution-time check rules.
    seat_assignment_id: Optional[str] = None
    seat_assignment_version: Optional[int] = None


def verify_attestation(
    token: str,
    *,
    binding: BindingRecord,
    terms: EntitlementTerms,
    sub: str,
    contract_digest: str,
    now: int,
) -> Entitlement:
    """Every wire §6.3 fresh-attestation check, in order; epoch last.

    ``binding`` supplies the CURRENT entitlement keyset and revocation epoch;
    ``contract_digest`` is the expected (pinned) digest. Raises
    :class:`EntitlementRefused` (``app_backend_invalid_response``) on any failure.
    """
    trust = binding.trust
    if trust is None:
        raise _unavailable_pack("binding_not_paired")
    try:
        verified = jws.verify_compact(token, expected_typ=ENTITLEMENT_TYP, keyset=trust.entitlement_keyset)
    except jws.JwsError:
        raise _invalid("attestation_jose_refused") from None
    p = verified.payload
    if p.get("aud") != terms.audience:
        raise _invalid("attestation_aud")
    try:
        jws.check_lifetime(p, now=now, max_lifetime_s=terms.max_attestation_ttl_seconds)
    except jws.JwsError:
        raise _invalid("attestation_time_bounds") from None
    if not now < p["exp"]:
        raise _invalid("attestation_expired")
    for name in _ID_CLAIMS:
        if not _is_text(p.get(name)):
            raise _invalid(f"attestation_claim_{name}")
    expected = {
        "iss": binding.pack_id,
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "pack_id": binding.pack_id,
        "contract_digest": contract_digest,
        "sub": sub,
    }
    for name, want in expected.items():
        if p[name] != want:
            raise _invalid(f"attestation_{name}_mismatch")
    caps = p.get("capabilities_granted")
    if not isinstance(caps, list) or not all(_is_text(c) for c in caps) or len(set(caps)) != len(caps):
        raise _invalid("attestation_capabilities_malformed")
    if caps != sorted(caps):  # wire §1.1 / §6.3: the sender sorts; never re-sorted here
        raise _invalid("attestation_capabilities_unsorted")
    if not set(caps) <= terms.capabilities:
        raise _invalid("attestation_capability_outside_vocabulary")
    if not _is_int(p.get("grants_version")) or p["grants_version"] < 0:
        raise _invalid("attestation_grants_version")
    if type(p.get("grants_digest")) is not str or not _HEX64.match(p["grants_digest"]):
        raise _invalid("attestation_grants_digest")
    epoch = p.get("revocation_epoch")
    if not _is_int(epoch) or epoch < 0:
        raise _invalid("attestation_revocation_epoch")
    if "binding_state" in p and p["binding_state"] != BINDING_STATE_RECOVERY:
        raise _invalid("attestation_binding_state")
    # #1134: both seat claims or neither; id non-empty text, version a JSON integer >= 0.
    has_sid, has_sver = "seat_assignment_id" in p, "seat_assignment_version" in p
    if has_sid != has_sver:
        raise _invalid("attestation_seat_assignment_partial")
    if has_sid and (not _is_text(p["seat_assignment_id"]) or not _is_int(p["seat_assignment_version"])
                    or p["seat_assignment_version"] < 0):
        raise _invalid("attestation_seat_assignment_malformed")
    current = trust.entitlement_revocation_epoch
    if epoch < current:
        raise _EpochLower()
    if epoch > current:
        raise _EpochAhead()
    return Entitlement(
        tenant_id=p["tenant_id"],
        binding_id=p["binding_id"],
        sub=p["sub"],
        pack_id=p["pack_id"],
        contract_digest=p["contract_digest"],
        app_principal_id=p["app_principal_id"],
        capabilities_granted=tuple(caps),
        grants_version=p["grants_version"],
        grants_digest=p["grants_digest"],
        revocation_epoch=epoch,
        kid=verified.header["kid"],
        jti=p["jti"],
        iat=p["iat"],
        exp=p["exp"],
        recovery="binding_state" in p,
        seat_assignment_id=p["seat_assignment_id"] if has_sid else None,
        seat_assignment_version=p["seat_assignment_version"] if has_sver else None,
    )


# -- seams ----------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResponse:
    """The entitlements endpoint's answer (wire §6.2): either ``attestation`` +
    ``expires_at`` (RFC 3339 UTC, whole seconds, trailing ``Z``; wire §1.1) or a
    declared ``error_code`` alone. ``expires_at`` is shape-checked only: it is
    never an authorization clock (the signed ``exp`` governs)."""

    attestation: Optional[str] = None
    error_code: Optional[str] = None
    expires_at: Any = None


class EntitlementFetcher(Protocol):
    """POST <entitlements_endpoint> with a control assertion (wire §6.1). S13."""

    def fetch(self, binding: BindingRecord, sub: str) -> FetchResponse: ...


class BindingReader(Protocol):
    def get(self, binding_id: str) -> BindingRecord: ...


@dataclass(frozen=True)
class PendingRecordView:
    """What the S14 pending-record authority reports for a ``mutation_ref``."""

    mutation_ref: str
    scope: AppScope
    binding_id: str
    pack_id: str
    contract_digest: str
    is_open: bool
    recovery_window_closes_at: int  # exclusive, integer seconds


class PendingRecordOracle(Protocol):
    def resolve(self, scope: AppScope, mutation_ref: str) -> Optional[PendingRecordView]: ...


# Binding states the §11.2 step 3 re-check may use: ACTIVE (unmarked attestation
# only) and DISABLED (the post-active state until S19; marked only).
_RECHECK_STATES = frozenset({BindingState.ACTIVE, BindingState.DISABLED})


def _record_identity(view: PendingRecordView) -> tuple:
    """The immutable identity of a pinned pending record (reviewer S9-B4). The
    record's own window bound is part of it: nothing may move it."""
    return (
        view.mutation_ref,
        view.scope,
        view.binding_id,
        view.pack_id,
        view.contract_digest,
        view.recovery_window_closes_at,
    )


# -- cache ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    entitlement: Entitlement
    cached_at: int
    expires_at: int  # min(cached_at + TTL, exp); never extended by a hit


def _entry_valid(entry: _Entry, binding: BindingRecord, now: int) -> bool:
    """§7.4: kid membership + revocation_epoch equality + expiry. Nothing else
    (``keyset_version`` must never become an input)."""
    trust = binding.trust
    if trust is None:
        return False
    kids = {k["kid"] for k in trust.entitlement_keys}
    return (
        entry.entitlement.kid in kids
        and entry.entitlement.revocation_epoch == trust.entitlement_revocation_epoch
        and now < entry.expires_at
    )


class EntitlementService:
    """Ordinary lookup + cache, stale refresh, §8.5 revocation, recovery re-check."""

    def __init__(
        self,
        *,
        bindings: BindingReader,
        terms_for: Callable[[BindingRecord], EntitlementTerms],
        fetcher: EntitlementFetcher,
        clock: Callable[[], int],
        cache_ttl_s: int = CACHE_TTL_DEFAULT_S,
        negative_ttl_s: int = NEGATIVE_TTL_CEILING_S,
        declared_errors: Mapping[str, str] = DECLARED_ERROR_MAP,
    ) -> None:
        if not _is_int(cache_ttl_s) or not 1 <= cache_ttl_s <= CACHE_TTL_CEILING_S:
            raise ValueError("cache TTL must be 1..30 s (RFC §7.4)")
        if not _is_int(negative_ttl_s) or not 0 <= negative_ttl_s <= NEGATIVE_TTL_CEILING_S:
            raise ValueError("negative cache TTL must be 0..5 s (RFC §7.4)")
        if not set(declared_errors.values()) <= {APP_IDENTITY_UNLINKED, APP_USER_DISABLED}:
            raise ValueError("declared errors map only to app_identity_unlinked / app_user_disabled")
        self._bindings = bindings
        self._terms_for = terms_for
        self._fetcher = fetcher
        self._clock = clock
        self.cache_ttl_s = cache_ttl_s
        self.negative_ttl_s = negative_ttl_s
        self._declared = MappingProxyType(dict(declared_errors))
        self._cache: dict[tuple, _Entry] = {}
        self._negative: dict[tuple, tuple[str, int]] = {}
        self._epoch_blocked: dict[str, str] = {}
        self._lock = threading.RLock()

    # -- small helpers ------------------------------------------------------------

    def _now(self) -> int:
        now = self._clock()
        if not _is_int(now):
            raise TypeError("clock must return integer seconds")
        return now

    def _binding(self, binding_id: str) -> BindingRecord:
        try:
            rec = self._bindings.get(binding_id)
        except Exception:  # noqa: BLE001 -- unknown binding fails closed
            rec = None
        if not isinstance(rec, BindingRecord):
            raise _unavailable_pack("binding_unknown")
        return rec

    def _terms(self, binding: BindingRecord) -> EntitlementTerms:
        try:
            terms = self._terms_for(binding)
        except EntitlementRefused:
            raise
        except Exception:  # noqa: BLE001 -- unknown pack terms fail closed
            terms = None
        if not isinstance(terms, EntitlementTerms):
            raise _unavailable_pack("pack_terms_unknown")
        return terms

    def _check_not_blocked(self, binding: BindingRecord) -> None:
        with self._lock:
            blocked = self._epoch_blocked.get(binding.binding_id)
            if blocked is None:
                return
            if blocked != binding.trust.transcript_sha256:
                del self._epoch_blocked[binding.binding_id]  # a new ceremony refreshed trust
                return
        raise _invalid("binding_trust_stale", egress_attempted=False)

    def _block(self, binding: BindingRecord) -> None:
        with self._lock:
            self._epoch_blocked[binding.binding_id] = binding.trust.transcript_sha256
            for key in [k for k in self._cache if k[1] == binding.binding_id]:
                del self._cache[key]

    def _call(self, binding: BindingRecord, sub: str) -> str:
        try:
            resp = self._fetcher.fetch(binding, sub)
        except Exception:  # noqa: BLE001 -- transport failure fails closed
            raise EntitlementRefused(APP_BACKEND_UNAVAILABLE, "entitlements_unreachable") from None
        if not isinstance(resp, FetchResponse) or (resp.attestation is None) == (resp.error_code is None):
            raise _invalid("entitlements_response_shape")
        if resp.error_code is not None:
            if resp.expires_at is not None:
                raise _invalid("entitlements_response_shape")
            code = self._declared.get(resp.error_code) if type(resp.error_code) is str else None
            if code is None:
                raise _invalid("entitlements_undeclared_error")
            raise _Declared(code, "entitlements_declared_error")
        if type(resp.attestation) is not str:
            raise _invalid("entitlements_response_shape")
        # wire §6.2 / §1.1: shape only. Never an authorization clock and never
        # compared with the signed exp, which alone governs.
        try:
            parse_rfc3339(resp.expires_at)
        except PairingRefused:
            raise _invalid("entitlements_expires_at_malformed") from None
        return resp.attestation

    def _fetch_verified(
        self, binding: BindingRecord, terms: EntitlementTerms, sub: str, contract_digest: str, *, epoch_retry: bool
    ) -> Entitlement:
        """One call, plus at most ONE refresh when the epoch is lower (wire §6.3)."""
        attempts = 2 if epoch_retry else 1
        for attempt in range(attempts):
            token = self._call(binding, sub)
            try:
                return verify_attestation(
                    token, binding=binding, terms=terms, sub=sub, contract_digest=contract_digest, now=self._now()
                )
            except _EpochLower:
                if attempt + 1 < attempts:
                    continue
                raise
            except _EpochAhead:
                self._block(binding)
                raise
        raise _invalid("unreachable")  # pragma: no cover

    def _admit(self, tenant_id: str, binding_id: str, contract_digest: str) -> BindingRecord:
        binding = self._binding(binding_id)
        if binding.tenant_id != tenant_id:
            raise _unavailable_pack("binding_unknown")
        if binding.state is not BindingState.ACTIVE or binding.trust is None:
            raise _unavailable_pack("binding_not_active")
        if contract_digest != binding.contract_digest:
            raise EntitlementRefused(APP_CONTRACT_MISMATCH, "contract_digest_mismatch", egress_attempted=False)
        self._check_not_blocked(binding)
        return binding

    def _check_negative(self, seat: tuple, now: int) -> None:
        with self._lock:
            hit = self._negative.get(seat)
            if hit is None:
                return
            code, until = hit
            if not now < until:
                del self._negative[seat]
                return
        raise EntitlementRefused(code, "negative_cache", egress_attempted=False)

    def _remember_negative(self, seat: tuple, code: str, now: int) -> None:
        with self._lock:
            for key in [k for k in self._cache if k[:3] == seat]:
                del self._cache[key]
            if self.negative_ttl_s > 0:
                self._negative[seat] = (code, now + self.negative_ttl_s)

    def _resolve_and_cache(
        self, binding: BindingRecord, sub: str, contract_digest: str, key: tuple, *, epoch_retry: bool
    ) -> Entitlement:
        terms = self._terms(binding)
        try:
            got = self._fetch_verified(binding, terms, sub, contract_digest, epoch_retry=epoch_retry)
        except _Declared as exc:
            self._remember_negative(key[:3], exc.code, self._now())
            raise
        if got.recovery:
            # wire §6.5: an ordinary lookup never uses and never caches a marked one.
            raise _invalid("recovery_marked_attestation_on_ordinary_lookup")
        now = self._now()
        with self._lock:
            self._cache[key] = _Entry(got, now, min(now + self.cache_ttl_s, got.exp))
        return got

    # -- public API ---------------------------------------------------------------

    def lookup(self, tenant_id: str, binding_id: str, sub: str, contract_digest: str) -> Entitlement:
        """The ordinary entitlement resolution for one call (§7.4, wire §6.4)."""
        now = self._now()
        binding = self._admit(tenant_id, binding_id, contract_digest)
        key = (tenant_id, binding_id, sub, contract_digest)
        self._check_negative(key[:3], now)
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                if _entry_valid(entry, binding, now):
                    return entry.entitlement
                del self._cache[key]
        return self._resolve_and_cache(binding, sub, contract_digest, key, epoch_retry=True)

    def refresh_after_stale(self, tenant_id: str, binding_id: str, sub: str, contract_digest: str) -> Entitlement:
        """§7.6: a stale-grant signal refreshes EXACTLY once (no retry loop)."""
        now = self._now()
        binding = self._admit(tenant_id, binding_id, contract_digest)
        key = (tenant_id, binding_id, sub, contract_digest)
        with self._lock:
            self._cache.pop(key, None)
        self._check_negative(key[:3], now)
        return self._resolve_and_cache(binding, sub, contract_digest, key, epoch_retry=False)

    def note_identity_refused(self, tenant_id: str, binding_id: str, sub: str, backend_code: str) -> None:
        """§8.5: AIDOCS learned the seat is unlinked / disabled (e.g. the backend's
        declared ``crm_user_*`` on a call). The next lookup is refused regardless of
        any cached attestation: positive entries are evicted and a <= 5 s negative
        entry is recorded."""
        code = self._declared.get(backend_code)
        if code is None:
            raise ValueError("not a declared unlinked / disabled code")
        seat = (tenant_id, binding_id, sub)
        now = self._now()
        with self._lock:
            for key in [k for k in self._cache if k[:3] == seat]:
                del self._cache[key]
            self._negative[seat] = (code, now + max(self.negative_ttl_s, 1))

    def cached_keys(self) -> tuple:
        with self._lock:
            return tuple(sorted(self._cache))

    # -- recovery (wire §6.5; RFC §11.2 step 3) ------------------------------------

    def _open_record(
        self, scope: AppScope, mutation_ref: str, pending: PendingRecordOracle, *, egress_attempted: bool
    ) -> PendingRecordView:
        try:
            view = pending.resolve(scope, mutation_ref)
        except Exception:  # noqa: BLE001 -- the pending authority failing fails closed
            view = None
        now = self._now()
        if (
            not isinstance(view, PendingRecordView)
            or view.mutation_ref != mutation_ref
            or view.scope != scope
            or view.is_open is not True
            or not _is_int(view.recovery_window_closes_at)
            or not now < view.recovery_window_closes_at
        ):
            raise _invalid("pending_record_not_open", egress_attempted=egress_attempted)
        return view

    def recheck_for_recovery(self, scope: AppScope, mutation_ref: str, pending: PendingRecordOracle) -> Entitlement:
        """The CURRENT-entitlement re-check of §11.2 step 3 for one pinned mutation.

        Uses the PINNED binding named by the pending record. Only a ``DISABLED``
        binding (the post-active state until S19 builds unbind) is eligible for a
        ``binding_state: "recovery"`` attestation; an ``ACTIVE`` binding accepts
        only an unmarked one; every other state (``PENDING_PAIRING``, ``PAIRED``:
        never live) is refused before any entitlements call (reviewer S9-B3).
        The pending record is re-read after fetch/verify and must be the SAME
        pinned record, still open and inside its window (reviewer S9-B4).
        Never reads or writes the cache.
        """
        if not isinstance(scope, AppScope):
            raise TypeError("scope must be an AppScope")
        view = self._open_record(scope, mutation_ref, pending, egress_attempted=False)
        try:
            binding = self._binding(view.binding_id)
        except EntitlementRefused:
            raise _invalid("pinned_binding_unknown", egress_attempted=False) from None
        if (
            binding.tenant_id != scope.tenant_id
            or binding.toolspace_id != scope.toolspace_id
            or binding.pack_id != view.pack_id
            or binding.trust is None
        ):
            raise _invalid("pinned_binding_mismatch", egress_attempted=False)
        if binding.state not in _RECHECK_STATES:
            raise _invalid("pinned_binding_not_recovery_eligible", egress_attempted=False)
        self._check_not_blocked(binding)
        terms = self._terms(binding)
        got = self._fetch_verified(binding, terms, scope.sub, view.contract_digest, epoch_retry=True)
        if binding.state is BindingState.ACTIVE:
            if got.recovery:
                raise _invalid("recovery_marker_on_active_binding")
        elif not got.recovery:
            raise _invalid("recovery_marker_required")
        # AIDOCS owns the window: re-read the live record before use; it must be
        # the SAME pinned record (immutable identity), still open, in its window.
        again = self._open_record(scope, mutation_ref, pending, egress_attempted=True)
        if _record_identity(again) != _record_identity(view):
            raise _invalid("pending_record_changed")
        return got
