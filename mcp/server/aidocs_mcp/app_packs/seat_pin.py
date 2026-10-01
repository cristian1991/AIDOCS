"""The SEAT PIN -- RFC 0003 v4.6.1 §2.5a, §18.14, §22.1, §16 (build plan S6).

``(tenant_id, keyed_digest(host subject claim))  <->  seat``

* **Seat.** A seat is ``(tenant_id, sub)`` (:class:`~.scope.AppScope`, §2.5a).
  The SEAT itself is sourced from the CodeNexus DB (build plan R8); this module
  only keeps the PIN, a new AIDOCS store held under tenant/org authority
  (§22.1). The pin is keyed by the seat, never by ``toolspace_id``,
  ``connector_id``, ``binding_id``, the host conversation or a token, so it is
  shared across all of a seat's connectors in the org (§0.4, §18.14) and
  re-authentication / token rotation never touch it.
* **Keyed digest.** HMAC-SHA256 under a server-held, PER-ORG key
  (:class:`SeatPinKeySource`, injected; production custody is the platform
  vault, tests use a fixed key), over a domain tag, the length-prefixed
  ``tenant_id`` and the raw ``openai/subject`` claim
  (:data:`~aidocs_mcp.webmcp_identity.META_SUBJECT`). Binding the org into the
  MAC input as well as the key keeps two orgs' digests apart even if a key
  source ever returned one key for both. The raw subject is used only to
  compute the digest; it is never stored, audited or rendered.
* **One-to-one within each org, both directions** (§2.5a): at most one host
  account per seat and one seat per host account in an org. The same host
  account may hold seats in different orgs.
* **First use pins atomically.** :meth:`SeatPinStore.claim` is ONE atomic
  check-and-insert: no row for the seat and none for the digest -> insert;
  same seat and same digest -> proceed; anything else -> mismatch. Concurrent
  first calls for one seat produce exactly one pin.
* **Mismatch** (either direction) refuses ``app_seat_host_mismatch``
  (:class:`SeatHostMismatch`), never ``app_identity_unlinked``, never
  auto-rebinds, and writes a DECISION-class ``app_call_refused`` row with
  ``refusal_code = app_seat_host_mismatch`` on the AppScope ledger seam
  (:class:`SeatPinRefusalLedger`, the ``AppAuditLedger.append`` signature).
  The refusal text is the bare code: no subject, no digest, no other seat
  (§2.5a, §16.3). The row carries AppScope members plus connector/binding
  evidence; it carries NO digest (§16.1 allows "the keyed digest at most";
  [FLAGGED] the stricter reading is taken). If that row cannot be written the
  refusal still stands.
* **Missing / malformed claim** -> refused ``app_seat_host_mismatch`` with the
  same DECISION row; nothing is ever pinned from an empty value [FLAGGED
  judgment call: Appendix A H1 names this refusal "on drift"; a CRM-connector
  call without a subject claim is treated as H1 drift, fail closed].
* **Key or store failure** -> :class:`SeatPinUnavailable` (``app_internal``),
  fail closed; no pin is written.
* **Admin reset** (§2.5a, §5.1, §16.1): OWNER/ADMIN of the owning org on the
  admin-dashboard credential class only (build plan R6), through the same
  :func:`~.app_freeze.authorize_admin` rule as the S11 acts. Refusals are
  ``app_control_plane_refused`` rows; the reset itself is a WRITE-AHEAD
  ``app_seat_pin_reset`` DECISION event (:class:`SeatPinControlEvent`) through
  the S7 ``ControlPlaneAuditSink.emit`` seam: no audit, no reset. After a reset
  the next subject pins fresh.
* A pin match returns only :class:`PinOutcome`; it never establishes or raises
  identity, permission or entitlement (§2.5a, Appendix A.1).

Storage: behind the :class:`SeatPinStore` Protocol. Production is the durable
``AppAuthorityDB.seat_pins()`` (per-org SQLite under the tenant home, §22.1;
UNIQUE both ways), whose reset and its DECISION row are ONE transaction;
:class:`InMemorySeatPinStore` is for tests.
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Union

from .. import webmcp_identity
from ..execution_event_retention import EVENT_KIND_RETENTION, RetentionClass
from .app_freeze import authorize_admin, emit_control_refusal
from .authority_db import authority_transaction
from .binding_store import (
    RETENTION_DECISION,
    AdminAuthority,
    ControlPlaneActor,
    ControlPlaneAuditSink,
)
from .boundary_audit import (
    APP_PLATFORM_ERROR_CODES,
    KIND_STATUS,
    audit_metadata,
    kind_status_admissible,
)
from .scope import AppScope, ConnectionContext

__all__ = [
    "MIN_PIN_KEY_BYTES",
    "SEAT_PIN_CONTROL_KINDS",
    "SEAT_PIN_MISMATCH_CODE",
    "SEAT_PIN_RESET_KIND",
    "SEAT_PIN_UNAVAILABLE_CODE",
    "SUBJECT_CLAIM",
    "SUBJECT_CLAIM_MAX_BYTES",
    "ClaimOutcome",
    "InMemorySeatPinStore",
    "PinOutcome",
    "SeatHostMismatch",
    "SeatPin",
    "SeatPinControlEvent",
    "SeatPinKeySource",
    "SeatPinRefusalLedger",
    "SeatPinRefused",
    "SeatPinService",
    "SeatPinStore",
    "SeatPinUnavailable",
    "keyed_subject_digest",
]

#: The ONE host subject claim (Appendix A H1). Named by the #1074 module.
SUBJECT_CLAIM = webmcp_identity.META_SUBJECT
#: No sealed number bounds the claim; S6 encoding choice [FLAGGED]: a claim over
#: this many UTF-8 bytes is MALFORMED (refused, never pinned). Same bound as S15.
SUBJECT_CLAIM_MAX_BYTES = 512
#: A per-org pin key shorter than this is unusable (fail closed).
MIN_PIN_KEY_BYTES = 32

SEAT_PIN_MISMATCH_CODE = "app_seat_host_mismatch"
SEAT_PIN_UNAVAILABLE_CODE = "app_internal"
#: The §16.1 refusal kind every refused call uses (sealed table).
CALL_REFUSED_KIND = "app_call_refused"
#: The S6 control-plane audit kind (registered in ``boundary_audit.KIND_STATUS``
#: and, as DECISION, in ``execution_event_retention``). Spelling per §22.5.
SEAT_PIN_RESET_KIND = "app_seat_pin_reset"
SEAT_PIN_CONTROL_KINDS = frozenset({SEAT_PIN_RESET_KIND})

_DOMAIN = b"aidocs-app-seat-pin/v1\x00"

for _code in (SEAT_PIN_MISMATCH_CODE, SEAT_PIN_UNAVAILABLE_CODE):
    if _code not in APP_PLATFORM_ERROR_CODES:  # pragma: no cover -- vocabulary self-check
        raise RuntimeError(f"{_code!r} is not a §16.3 platform code")

Subject = Union[AppScope, ConnectionContext]


def _scope_of(subject: Any) -> AppScope:
    if type(subject) is AppScope:
        return subject
    if type(subject) is ConnectionContext:
        return subject.app_scope
    raise TypeError("the seat-pin check takes an AppScope or a ConnectionContext")


def _well_formed_subject(meta: Any) -> Optional[str]:
    """The raw ``openai/subject`` claim if well formed, else ``None``.

    Checked on the RAW value (the #1074 reader strips whitespace, which would
    let ``" s"`` and ``"s"`` collapse): a non-empty string with no surrounding
    whitespace, no control / surrogate characters, within the byte bound. No
    other claim ever stands in for it.
    """
    if not isinstance(meta, Mapping):
        return None
    claim = meta.get(SUBJECT_CLAIM)
    if type(claim) is not str or not claim or claim != claim.strip():
        return None
    if len(claim.encode("utf-8", "surrogatepass")) > SUBJECT_CLAIM_MAX_BYTES:
        return None
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in claim):
        return None
    return claim


def _usable_key(key: Any) -> bytes:
    if type(key) is not bytes or len(key) < MIN_PIN_KEY_BYTES:
        raise ValueError("the per-org seat-pin key is unusable")
    return key


def keyed_subject_digest(key: bytes, tenant_id: str, subject: str) -> str:
    """HMAC-SHA256(per-org key, domain || len(tenant) || tenant || subject), hex.

    The org is bound into the MAC input as well as the key. The digest is the
    only thing derived from the subject that is ever kept.
    """
    _usable_key(key)
    if type(tenant_id) is not str or not tenant_id or type(subject) is not str or not subject:
        raise ValueError("tenant_id and subject must be non-empty strings")
    tenant = tenant_id.encode("utf-8")
    msg = _DOMAIN + len(tenant).to_bytes(4, "big") + tenant + subject.encode("utf-8", "surrogatepass")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class PinOutcome(enum.Enum):
    """What an admitted check did. Carries no identity, permission or entitlement."""

    PINNED = "pinned"
    MATCHED = "matched"


class ClaimOutcome(enum.Enum):
    """The store's atomic check-and-insert result."""

    PINNED = "pinned"
    MATCHED = "matched"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class SeatPin:
    """One pin. The keyed digest only; there is no subject field by construction."""

    tenant_id: str
    sub: str
    digest: str
    pinned_at: int

    def __post_init__(self) -> None:
        for name in ("tenant_id", "sub"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        d = self.digest
        if type(d) is not str or len(d) != 64 or any(ch not in "0123456789abcdef" for ch in d):
            raise ValueError("digest must be a 64-hex keyed digest")
        if type(self.pinned_at) is not int or self.pinned_at < 0:
            raise ValueError("pinned_at must be a non-negative integer (epoch seconds)")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("SeatPin is a closed type and cannot be subclassed")


class SeatPinKeySource(Protocol):
    """Server-held, per-org pin key. Never selected by a model or request."""

    def pin_key(self, tenant_id: str) -> bytes: ...


class SeatPinStore(Protocol):
    def claim(self, pin: SeatPin) -> ClaimOutcome: ...

    def get_by_seat(self, tenant_id: str, sub: str) -> Optional[SeatPin]: ...

    def reset(self, tenant_id: str, sub: str, digest: str) -> Optional[SeatPin]: ...


class SeatPinRefusalLedger(Protocol):
    """The AppScope ledger seam (``AppAuditLedger.append``)."""

    def append(self, scope: AppScope, kind: str, status: str, metadata: Mapping[str, Any]) -> Any: ...


class InMemorySeatPinStore:
    """Thread-safe in-process pin store, both directions indexed (production HOLD)."""

    def __init__(self) -> None:
        self._by_seat: dict[tuple[str, str], SeatPin] = {}
        self._by_host: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def claim(self, pin: SeatPin) -> ClaimOutcome:
        """ONE atomic check-and-insert (§2.5a first use)."""
        if type(pin) is not SeatPin:
            raise TypeError("claim takes a SeatPin")
        seat_key = (pin.tenant_id, pin.sub)
        host_key = (pin.tenant_id, pin.digest)
        with self._lock:
            by_seat = self._by_seat.get(seat_key)
            by_host = self._by_host.get(host_key)
            if by_seat is None and by_host is None:
                self._by_seat[seat_key] = pin
                self._by_host[host_key] = pin.sub
                return ClaimOutcome.PINNED
            if by_seat is not None and by_seat.digest == pin.digest and by_host == pin.sub:
                return ClaimOutcome.MATCHED
            return ClaimOutcome.MISMATCH

    def get_by_seat(self, tenant_id: str, sub: str) -> Optional[SeatPin]:
        with self._lock:
            return self._by_seat.get((tenant_id, sub))

    def pins(self) -> list[SeatPin]:
        with self._lock:
            return sorted(self._by_seat.values(), key=lambda p: (p.tenant_id, p.sub))

    def reset(self, tenant_id: str, sub: str, digest: str) -> Optional[SeatPin]:
        """Remove the seat's pin iff it still carries ``digest`` (CAS)."""
        with self._lock:
            pin = self._by_seat.get((tenant_id, sub))
            if pin is None or pin.digest != digest:
                return None
            del self._by_seat[(tenant_id, sub)]
            self._by_host.pop((tenant_id, digest), None)
            return pin


class SeatHostMismatch(Exception):
    """§2.5a refusal. The text is the bare code: no subject, digest or seat."""

    code = SEAT_PIN_MISMATCH_CODE

    def __init__(self) -> None:
        super().__init__(SEAT_PIN_MISMATCH_CODE)


class SeatPinUnavailable(Exception):
    """The pin key or store could not be used; the call is refused (fail closed)."""

    code = SEAT_PIN_UNAVAILABLE_CODE

    def __init__(self) -> None:
        super().__init__(SEAT_PIN_UNAVAILABLE_CODE)


class SeatPinRefused(Exception):
    """A seat-pin control-plane act is refused. ``code`` is a LOCAL control code
    (``boundary_audit.CONTROL_REFUSAL_CODES``), never a model-facing ``app_*``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SeatPinControlEvent:
    """A §16.1 DECISION-class control-plane audit event for the S6 reset.

    Same contract as the S7 ``ControlPlaneEvent`` / S11 ``AppScopeControlEvent``
    (emitted through the same ``ControlPlaneAuditSink.emit`` seam): the kind is
    registered, ``status`` is derived from the closed kind <-> status matrix,
    and every metadata entry is allowlisted and well-shaped -- anything else
    raises.
    """

    kind: str
    tenant_id: str
    actor_user_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    retention: str = RETENTION_DECISION
    status: str = field(init=False)

    def __post_init__(self) -> None:
        if self.retention != RETENTION_DECISION:
            raise ValueError("S6 control-plane events are DECISION-class")
        if self.kind not in SEAT_PIN_CONTROL_KINDS or self.kind not in KIND_STATUS:
            raise ValueError(f"unregistered S6 control-plane audit kind {self.kind!r}")
        if EVENT_KIND_RETENTION.get(self.kind) is not RetentionClass.DECISION:
            raise ValueError(f"{self.kind!r} is not DECISION in the retention registry")
        if "status" in self.metadata:
            raise ValueError("status is derived from the kind, never supplied")
        status = KIND_STATUS[self.kind]
        md = {**dict(self.metadata), "status": status}
        if audit_metadata(md) != md:
            raise ValueError(f"metadata outside the audit allowlist or shape: {sorted(md)}")
        if not kind_status_admissible(self.kind, status):  # pragma: no cover -- matrix self-check
            raise ValueError("kind/status pair is off the closed matrix")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metadata", MappingProxyType(md))


class SeatPinService:
    """The §2.5a seat-pin check (call path) and the admin reset (control plane)."""

    def __init__(
        self,
        store: SeatPinStore,
        key_source: SeatPinKeySource,
        admin_authority: AdminAuthority,
        audit_sink: ControlPlaneAuditSink,
        refusal_ledger: SeatPinRefusalLedger,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._keys = key_source
        self._admins = admin_authority
        self._sink = audit_sink
        self._ledger = refusal_ledger
        self._clock = clock
        self._reset_lock = threading.Lock()
        check = getattr(store, "check_sink", None)
        if callable(check):
            check(audit_sink)  # ruling 2: a durable store audits on its own file

    # -- call path -------------------------------------------------------

    def check(self, subject: Subject, meta: Any) -> PinOutcome:
        """Pin on first use or confirm the pin; raise :class:`SeatHostMismatch`
        on any mismatch or a missing/malformed claim, :class:`SeatPinUnavailable`
        when the key or store cannot be used."""
        scope = _scope_of(subject)
        claim = _well_formed_subject(meta)
        if claim is None:
            self._refuse(subject, scope)
        try:
            digest = keyed_subject_digest(_usable_key(self._keys.pin_key(scope.tenant_id)), scope.tenant_id, claim)
        except Exception as exc:  # noqa: BLE001 -- unusable key fails closed
            raise SeatPinUnavailable() from exc
        del claim
        try:
            outcome = self._store.claim(SeatPin(scope.tenant_id, scope.sub, digest, int(self._clock())))
        except Exception as exc:  # noqa: BLE001 -- unknown pin state fails closed
            raise SeatPinUnavailable() from exc
        if outcome is ClaimOutcome.PINNED:
            return PinOutcome.PINNED
        if outcome is ClaimOutcome.MATCHED:
            return PinOutcome.MATCHED
        self._refuse(subject, scope)
        raise AssertionError("unreachable")  # pragma: no cover

    def _refuse(self, subject: Subject, scope: AppScope) -> None:
        """DECISION row (best effort: the refusal stands either way), then raise."""
        facts: dict[str, Any] = {
            "tenant_id": scope.tenant_id,
            "sub": scope.sub,
            "toolspace_id": scope.toolspace_id,
        }
        if type(subject) is ConnectionContext:
            facts["connector_id"] = subject.connector_id
            if subject.binding_id is not None:
                facts["binding_id"] = subject.binding_id
        facts["refusal_code"] = SEAT_PIN_MISMATCH_CODE
        try:
            self._ledger.append(scope, CALL_REFUSED_KIND, KIND_STATUS[CALL_REFUSED_KIND], audit_metadata(facts))
        except Exception:  # noqa: BLE001 -- the refusal stands even if its audit write fails
            pass
        raise SeatHostMismatch()

    # -- control plane -----------------------------------------------------

    def admin_reset(self, actor: ControlPlaneActor, tenant_id: str, sub: str) -> SeatPin:
        """Clear one seat's pin so the next subject pins fresh (§2.5a, §5.1).
        Owner/admin only; write-ahead DECISION audit."""
        if type(tenant_id) is not str or not tenant_id or type(sub) is not str or not sub:
            raise SeatPinRefused("invalid")
        facts = {"tenant_id": tenant_id, "sub": sub}
        if not authorize_admin(self._admins, actor, tenant_id):
            emit_control_refusal(self._sink, actor, "forbidden", facts)
            raise SeatPinRefused("forbidden")
        # Durable store: the read, the DECISION row and the delete are ONE transaction.
        with self._reset_lock, authority_transaction(self._store):
            try:
                pin = self._store.get_by_seat(tenant_id, sub)
            except Exception as exc:  # noqa: BLE001
                emit_control_refusal(self._sink, actor, "control_plane_refused", facts)
                raise SeatPinRefused("control_plane_refused") from exc
            if pin is None:
                emit_control_refusal(self._sink, actor, "invalid_transition", facts)
                raise SeatPinRefused("invalid_transition")
            try:
                self._sink.emit(SeatPinControlEvent(SEAT_PIN_RESET_KIND, tenant_id, actor.user_id, facts))
            except Exception as exc:  # noqa: BLE001 -- no audit, no reset
                raise SeatPinRefused("audit_unavailable") from exc
            try:
                removed = self._store.reset(tenant_id, sub, pin.digest)
            except Exception as exc:  # noqa: BLE001
                raise SeatPinRefused("control_plane_refused") from exc
            if removed is None:  # pragma: no cover -- serialized by the reset lock
                raise SeatPinRefused("invalid_transition")
            return removed
