"""Server-derived mutation identity + the pending mutation record -- RFC 0003 v4.6.1 §5.7, §11 (build plan S14).

* **Identity (§11).** The model never supplies idempotency. AIDOCS mints an
  opaque ``mutation_ref`` (model-visible, the sole caller-supplied recovery
  selector, wire §10.1) and DERIVES the ``idempotency_key`` (never
  model-visible, never generic-audited raw, never equal to the ref, wire §7)
  from exactly the durable identity: ``tenant_id``, ``sub``, ``toolspace_id``,
  ``binding_id``, ``pack_id``, ``contract_digest``, ``tool``, ``mode``, the
  RFC 8785 body digest and the ref. ``session_id``, ``actor_id``, causal
  lineage, ``host_kind`` and ``host_conversation_ref`` are facts about the
  REQUEST (assertion + audit row) and are not inputs, so the identity is
  reconstructable after the originating session is gone.
* **Record (§5.7, §11.2, wire §10.1 / §10.3).** Keyed by AppScope +
  ``mutation_ref``; pins ``binding_id``, ``binding_version``, ``pack_id``,
  ``contract_digest``, the connection generation (the binding's pairing
  transcript), the trust facts recovery needs (``aidocs_issuer``, ``audience``,
  ``application_origin``), the normalized body, the pre-call capability
  envelope, and its OWN ``recovery_window_closes_at`` (at least 7 days; nothing
  moves it). A record whose key is not the derivation of its own pinned facts
  is refused, so the same identity can never carry a different body (§11.1).
* **States (§11.2 / §11.4).** ``in_flight`` (the original call or a resolver
  resend is on the wire) -> ``outcome_unknown`` (possibly delivered, no
  receipt; the resolver owns it) -> ``resolved`` (a §13.6 receipt, immutable)
  | ``failed`` (definitely no effect) | ``expired`` (the window closed without
  a receipt) | ``terminated`` (an explicit operator terminal state, §5.7).
  ``in_flight`` and ``outcome_unknown`` are OPEN; the rest are terminal.
  Transitions are compare-and-swap on the expected state.
* **Store.** Behind the :class:`PendingStore` protocol. The durable store is
  :class:`~.authority_stores.SqlitePendingStore` in the per-org authority DB
  under the tenant home (§22.1); :class:`InMemoryPendingStore` is the test
  twin. Both apply :func:`next_pending_record`. The store is also the S9
  :class:`~.entitlements.PendingRecordOracle`.

There is no project axis anywhere here (§18.7).
"""
from __future__ import annotations

import enum
import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Protocol

from .binding_store import BindingRecord
from .entitlements import PendingRecordView
from .jcs import JcsError, canonicalize, jcs_sha256_hex
from .pairing import PairingRefused, parse_rfc3339
from .scope import AppScope
from .transport import ASSERTION_LIFETIME_S

__all__ = [
    "IDEMPOTENCY_KEY_PREFIX",
    "IN_FLIGHT_STALE_AFTER_S",
    "MUTATION_IDENTITY_VERSION",
    "MUTATION_REF_PREFIX",
    "OPEN_STATES",
    "RECOVERY_WINDOW_MIN_S",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "InMemoryPendingStore",
    "PendingRecord",
    "PendingRefused",
    "PendingState",
    "PendingStore",
    "ReceiptInvalid",
    "check_receipt",
    "derive_idempotency_key",
    "is_mutation_ref",
    "mint_mutation_ref",
    "new_pending_record",
    "next_pending_record",
]

MUTATION_REF_PREFIX = "mref_"
IDEMPOTENCY_KEY_PREFIX = "idk_"
MUTATION_IDENTITY_VERSION = "aidocs.mutation-identity/v1"
RECOVERY_WINDOW_MIN_S = 7 * 86400  # §5.7 / §11.3: at least 7 days
CLOCK_SKEW_S = 10  # wire §1.4
#: After this long an ``in_flight`` record can no longer have a live original
#: assertion at the application (lifetime + skew), so the resolver may treat it
#: as ``outcome_unknown`` without racing the first attempt.
IN_FLIGHT_STALE_AFTER_S = ASSERTION_LIFETIME_S + CLOCK_SKEW_S

_REF_RE = re.compile(r"mref_[A-Za-z0-9_-]{16,120}")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")  # wire §7
_HEX64 = re.compile(r"[0-9a-f]{64}")


class PendingState(str, enum.Enum):
    IN_FLIGHT = "in_flight"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RESOLVED = "resolved"
    FAILED = "failed"
    EXPIRED = "expired"
    TERMINATED = "terminated"


OPEN_STATES = frozenset({PendingState.IN_FLIGHT, PendingState.OUTCOME_UNKNOWN})
TERMINAL_STATES = frozenset(PendingState) - OPEN_STATES
TRANSITIONS: Mapping[PendingState, frozenset] = MappingProxyType(
    {
        PendingState.IN_FLIGHT: frozenset(
            {PendingState.RESOLVED, PendingState.OUTCOME_UNKNOWN, PendingState.FAILED}
        ),
        PendingState.OUTCOME_UNKNOWN: frozenset(
            {
                PendingState.IN_FLIGHT,  # a resolver claims the one resend
                PendingState.RESOLVED,
                PendingState.FAILED,
                PendingState.EXPIRED,
                PendingState.TERMINATED,
            }
        ),
        PendingState.RESOLVED: frozenset(),
        PendingState.FAILED: frozenset(),
        PendingState.EXPIRED: frozenset(),
        PendingState.TERMINATED: frozenset(),
    }
)


class PendingRefused(Exception):
    """A pending-record act is refused. ``code`` is a LOCAL code, never model-facing."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class ReceiptInvalid(ValueError):
    """A backend receipt lacks the §13.6 shape; it is NOT a receipt."""


def _text(value: Any) -> bool:
    return type(value) is str and value != ""


def _int(value: Any) -> bool:
    return type(value) is int


# -- identity (§11) ---------------------------------------------------------------------


def mint_mutation_ref() -> str:
    """An opaque, server-minted, unguessable ``mutation_ref`` (wire §10.1)."""
    return MUTATION_REF_PREFIX + secrets.token_urlsafe(24)


def is_mutation_ref(value: Any) -> bool:
    return type(value) is str and _REF_RE.fullmatch(value) is not None


def derive_idempotency_key(
    *,
    scope: AppScope,
    binding_id: str,
    pack_id: str,
    contract_digest: str,
    tool: str,
    mode: str,
    body_sha256: str,
    mutation_ref: str,
) -> str:
    """The durable §11 identity as the wire ``idempotency_key`` (wire §7).

    SHA-256 over RFC 8785 bytes of exactly the identity members; no request
    fact (session, actor, host, connector, call) is an input.
    """
    if type(scope) is not AppScope:
        raise PendingRefused("record_invalid", "scope must be an AppScope")
    for name, value in (("binding_id", binding_id), ("pack_id", pack_id), ("tool", tool), ("mode", mode)):
        if not _text(value):
            raise PendingRefused("record_invalid", f"{name} must be a non-empty string")
    for name, value in (("contract_digest", contract_digest), ("body_sha256", body_sha256)):
        if type(value) is not str or not _HEX64.fullmatch(value):
            raise PendingRefused("record_invalid", f"{name} must be 64 lowercase hex")
    if not is_mutation_ref(mutation_ref):
        raise PendingRefused("record_invalid", "mutation_ref malformed")
    material = {
        "v": MUTATION_IDENTITY_VERSION,
        "tenant_id": scope.tenant_id,
        "sub": scope.sub,
        "toolspace_id": scope.toolspace_id,
        "binding_id": binding_id,
        "pack_id": pack_id,
        "contract_digest": contract_digest,
        "tool": tool,
        "mode": mode,
        "body_sha256": body_sha256,
        "mutation_ref": mutation_ref,
    }
    return IDEMPOTENCY_KEY_PREFIX + jcs_sha256_hex(material)


# -- receipts (§13.6) -------------------------------------------------------------------


def _rfc3339(value: Any, name: str) -> None:
    try:
        parse_rfc3339(value)
    except PairingRefused:
        raise ReceiptInvalid(f"{name} is not RFC 3339 UTC (wire §1.1)") from None


def check_receipt(receipt: Any, *, reversible: bool) -> dict:
    """The §13.6 floor every receipt must meet before AIDOCS claims an effect.

    ``receipt_id``, commit truth (``committed_at``), ``already_applied`` and,
    for a reversible branch (§6.3.1), ``undo_available_until`` +
    ``undo_surface``. Exact domain fields stay pack-defined (the output schema
    already validated them). Returns a JSON-clean copy.
    """
    if not isinstance(receipt, Mapping):
        raise ReceiptInvalid("receipt is not an object")
    if not _text(receipt.get("receipt_id")):
        raise ReceiptInvalid("receipt_id missing")
    _rfc3339(receipt.get("committed_at"), "committed_at")
    if type(receipt.get("already_applied")) is not bool:
        raise ReceiptInvalid("already_applied missing")
    if reversible:
        _rfc3339(receipt.get("undo_available_until"), "undo_available_until")
        if not _text(receipt.get("undo_surface")):
            raise ReceiptInvalid("undo_surface missing")
    try:
        return json.loads(canonicalize(dict(receipt)))
    except (JcsError, TypeError, ValueError):
        raise ReceiptInvalid("receipt is not canonical JSON") from None


def _json_copy(value: Any) -> Any:
    return None if value is None else json.loads(canonicalize(dict(value)))


# -- the record ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingRecord:
    """One pinned pending mutation (§5.7, §11.2). Immutable; the store replaces it."""

    mutation_ref: str
    idempotency_key: str
    scope: AppScope
    binding_id: str
    binding_version: int
    pack_id: str
    contract_digest: str
    connection_generation: str
    application_origin: str
    audience: str
    aidocs_issuer: str
    tool: str
    mode: str
    branch_id: str
    reversible: bool
    body: bytes = field(repr=False)
    body_sha256: str
    capabilities_used: tuple
    created_at: int
    recovery_window_closes_at: int
    state: PendingState = PendingState.IN_FLIGHT
    state_since: Optional[int] = None
    receipt: Optional[dict] = None
    failure_code: Optional[str] = None
    application_error: Optional[dict] = None

    def __post_init__(self) -> None:
        bad = self._problem()
        if bad:
            raise PendingRefused("record_invalid", bad)
        if self.state_since is None:
            object.__setattr__(self, "state_since", self.created_at)

    def _problem(self) -> str:
        if type(self.scope) is not AppScope:
            return "scope"
        for name in ("binding_id", "pack_id", "application_origin", "audience", "aidocs_issuer", "tool", "mode",
                     "branch_id"):
            if not _text(getattr(self, name)):
                return name
        if not _int(self.binding_version) or self.binding_version < 1:
            return "binding_version"
        for name in ("contract_digest", "connection_generation", "body_sha256"):
            if type(getattr(self, name)) is not str or not _HEX64.fullmatch(getattr(self, name)):
                return name
        if type(self.reversible) is not bool:
            return "reversible"
        if type(self.body) is not bytes or hashlib.sha256(self.body).hexdigest() != self.body_sha256:
            return "body"  # §11.1: the digest in the identity IS this body
        try:
            if canonicalize(json.loads(self.body)) != self.body:
                return "body_not_jcs"
        except (JcsError, TypeError, ValueError):
            return "body_not_jcs"
        caps = self.capabilities_used
        if type(caps) is not tuple or not caps or not all(_text(c) for c in caps) or list(caps) != sorted(set(caps)):
            return "capabilities_used"
        if not _int(self.created_at) or not _int(self.recovery_window_closes_at):
            return "times"
        if self.recovery_window_closes_at < self.created_at + RECOVERY_WINDOW_MIN_S:
            return "recovery_window_below_7_days"
        if not isinstance(self.state, PendingState):
            return "state"
        if self.state_since is not None and not _int(self.state_since):
            return "state_since"
        if (self.receipt is not None) != (self.state is PendingState.RESOLVED):
            return "receipt_state"
        if self.state is PendingState.FAILED and not _text(self.failure_code):
            return "failure_code"
        try:
            expected = derive_idempotency_key(
                scope=self.scope,
                binding_id=self.binding_id,
                pack_id=self.pack_id,
                contract_digest=self.contract_digest,
                tool=self.tool,
                mode=self.mode,
                body_sha256=self.body_sha256,
                mutation_ref=self.mutation_ref,
            )
        except PendingRefused as exc:
            return str(exc)
        if self.idempotency_key != expected or not _KEY_RE.fullmatch(self.idempotency_key):
            return "idempotency_key_not_the_identity"
        return ""

    @property
    def arguments(self) -> dict:
        """The pinned normalized arguments (a fresh copy)."""
        return json.loads(self.body)

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def view(self) -> PendingRecordView:
        """The S9 :class:`~.entitlements.PendingRecordView` of this record."""
        return PendingRecordView(
            mutation_ref=self.mutation_ref,
            scope=self.scope,
            binding_id=self.binding_id,
            pack_id=self.pack_id,
            contract_digest=self.contract_digest,
            is_open=self.is_open,
            recovery_window_closes_at=self.recovery_window_closes_at,
        )


def new_pending_record(
    *,
    scope: AppScope,
    binding: BindingRecord,
    tool: str,
    mode: str,
    branch_id: str,
    reversible: bool,
    arguments: Mapping[str, Any],
    capabilities_used: Iterable[str],
    now: int,
    window_s: int = RECOVERY_WINDOW_MIN_S,
    mutation_ref: Optional[str] = None,
) -> PendingRecord:
    """Open a record pinned to ``binding`` for one mutation (state ``in_flight``)."""
    if not isinstance(binding, BindingRecord) or binding.trust is None:
        raise PendingRefused("record_invalid", "binding without paired trust")
    if type(scope) is not AppScope or (scope.tenant_id, scope.toolspace_id) != (
        binding.tenant_id,
        binding.toolspace_id,
    ):
        raise PendingRefused("record_invalid", "scope is not this binding's")
    if not _int(window_s) or window_s < RECOVERY_WINDOW_MIN_S:
        raise PendingRefused("record_invalid", "recovery window below 7 days")
    if not _int(now):
        raise PendingRefused("record_invalid", "now")
    if not isinstance(arguments, Mapping):
        raise PendingRefused("record_invalid", "arguments")
    try:
        body = canonicalize(dict(arguments))
    except (JcsError, TypeError, ValueError):
        raise PendingRefused("record_invalid", "arguments not canonicalizable") from None
    body_sha256 = hashlib.sha256(body).hexdigest()
    ref = mint_mutation_ref() if mutation_ref is None else mutation_ref
    key = derive_idempotency_key(
        scope=scope,
        binding_id=binding.binding_id,
        pack_id=binding.pack_id,
        contract_digest=binding.contract_digest,
        tool=tool,
        mode=mode,
        body_sha256=body_sha256,
        mutation_ref=ref,
    )
    caps = list(capabilities_used)
    return PendingRecord(
        mutation_ref=ref,
        idempotency_key=key,
        scope=scope,
        binding_id=binding.binding_id,
        binding_version=binding.binding_version,
        pack_id=binding.pack_id,
        contract_digest=binding.contract_digest,
        connection_generation=binding.trust.transcript_sha256,
        application_origin=binding.application_origin,
        audience=binding.audience,
        aidocs_issuer=binding.trust.aidocs_issuer,
        tool=tool,
        mode=mode,
        branch_id=branch_id,
        reversible=reversible,
        body=body,
        body_sha256=body_sha256,
        capabilities_used=tuple(sorted(set(caps))) if len(set(caps)) == len(caps) else tuple(caps),
        created_at=now,
        recovery_window_closes_at=now + window_s,
    )


# -- the store ----------------------------------------------------------------------------


class PendingStore(Protocol):
    """The pending-record authority. Also the S9 ``PendingRecordOracle``."""

    def create(self, record: PendingRecord) -> None: ...

    def get(self, scope: AppScope, mutation_ref: str) -> Optional[PendingRecord]: ...

    def transition(
        self,
        scope: AppScope,
        mutation_ref: str,
        *,
        expected: PendingState,
        to: PendingState,
        now: int,
        receipt: Optional[Mapping[str, Any]] = None,
        failure_code: Optional[str] = None,
        application_error: Optional[Mapping[str, Any]] = None,
    ) -> PendingRecord: ...

    def resolve(self, scope: AppScope, mutation_ref: str) -> Optional[PendingRecordView]: ...


def next_pending_record(
    cur: Optional[PendingRecord],
    *,
    expected: PendingState,
    to: PendingState,
    now: int,
    receipt: Optional[Mapping[str, Any]] = None,
    failure_code: Optional[str] = None,
    application_error: Optional[Mapping[str, Any]] = None,
) -> PendingRecord:
    """The ONE transition rule every pending store applies (compare-and-swap on
    ``expected``, the closed :data:`TRANSITIONS` table). Returns the new record."""
    if not isinstance(expected, PendingState) or not isinstance(to, PendingState) or not _int(now):
        raise PendingRefused("invalid_transition")
    if cur is None:
        raise PendingRefused("not_found")
    if cur.state is not expected:
        raise PendingRefused("state_conflict", f"{cur.state.value} != {expected.value}")
    if to not in TRANSITIONS[cur.state]:
        raise PendingRefused("invalid_transition", f"{cur.state.value} -> {to.value}")
    if to is PendingState.RESOLVED and receipt is None:
        raise PendingRefused("invalid_transition", "resolved needs a receipt")
    if to is PendingState.FAILED and not _text(failure_code):
        raise PendingRefused("invalid_transition", "failed needs a code")
    changes = {f.name: getattr(cur, f.name) for f in fields(cur)}
    changes.update(
        state=to,
        state_since=now,
        receipt=_json_copy(receipt) if to is PendingState.RESOLVED else None,
        failure_code=failure_code if to is PendingState.FAILED else None,
        application_error=_json_copy(application_error) if to is PendingState.FAILED else None,
    )
    return PendingRecord(**changes)


class InMemoryPendingStore:
    """In-process pending records keyed by ``(AppScope, mutation_ref)`` (tests).

    The durable store is :class:`~.authority_stores.SqlitePendingStore` in the
    org authority DB (``AppAuthorityDB.pending()``); it commits a record before
    the write path's egress (write-ahead).
    """

    def __init__(self) -> None:
        self._records: dict[tuple[AppScope, str], PendingRecord] = {}
        self._refs: set[str] = set()
        self._keys: set[str] = set()
        self._lock = threading.RLock()

    def create(self, record: PendingRecord) -> None:
        if not isinstance(record, PendingRecord):
            raise PendingRefused("record_invalid", "not a PendingRecord")
        if record.state is not PendingState.IN_FLIGHT:
            raise PendingRefused("record_invalid", "a record opens in_flight")
        with self._lock:
            if record.mutation_ref in self._refs:
                raise PendingRefused("mutation_ref_collision")
            if record.idempotency_key in self._keys:
                raise PendingRefused("idempotency_key_collision")
            self._records[(record.scope, record.mutation_ref)] = record
            self._refs.add(record.mutation_ref)
            self._keys.add(record.idempotency_key)

    def get(self, scope: AppScope, mutation_ref: str) -> Optional[PendingRecord]:
        """Only a record under EXACTLY this AppScope; anything else is None."""
        if type(scope) is not AppScope or type(mutation_ref) is not str:
            return None
        with self._lock:
            return self._records.get((scope, mutation_ref))

    def transition(
        self,
        scope: AppScope,
        mutation_ref: str,
        *,
        expected: PendingState,
        to: PendingState,
        now: int,
        receipt: Optional[Mapping[str, Any]] = None,
        failure_code: Optional[str] = None,
        application_error: Optional[Mapping[str, Any]] = None,
    ) -> PendingRecord:
        if not isinstance(expected, PendingState) or not isinstance(to, PendingState) or not _int(now):
            raise PendingRefused("invalid_transition")
        with self._lock:
            cur = self._records.get((scope, mutation_ref)) if type(scope) is AppScope else None
            new = next_pending_record(cur, expected=expected, to=to, now=now, receipt=receipt,
                                      failure_code=failure_code, application_error=application_error)
            self._records[(scope, mutation_ref)] = new
            return new

    def resolve(self, scope: AppScope, mutation_ref: str) -> Optional[PendingRecordView]:
        """The S9 ``PendingRecordOracle`` seam."""
        rec = self.get(scope, mutation_ref)
        return rec.view() if rec is not None else None
