"""The ONE platform outcome resolver -- RFC 0003 v4.6.1 §11.2, §11.4, §13.6 (build plan S14).

Keyed by the opaque ``mutation_ref`` under the CURRENT AppScope; returns exactly
one of ``receipt`` | ``pending`` | ``outcome_unknown`` (§11.4). Everything else
is a platform refusal outside that vocabulary.

Order (§11.2, §18.7):

0. pre-call boundary audit of this resolver call (write-ahead, §16; it is a
   read, so a failure is ``app_internal`` and nothing is looked up);
1. resolve ``mutation_ref`` under the CURRENT AppScope ``(tenant_id, sub,
   toolspace_id)``. No match -- never minted, or minted under ANY other
   tenant, sub or toolspace -- is ``app_mutation_not_found``, byte-identical in
   both cases and naming nothing of the record (§11.4, §16.3). Session, actor,
   connector and host conversation are provenance only and never compared;
2. a receipt the record already holds, or one the application's control-plane
   ``receipt_lookup`` returns (wire §10.3, by the PINNED record), is replayed
   with no new effect -- even if capability, entitlement or binding changed
   since (replay truth);
3. otherwise the current identity / link / entitlement / capability is
   re-checked (S9 ``recheck_for_recovery``: ordinary attestation on an ACTIVE
   binding, ``binding_state: "recovery"`` on a DISABLED one, wire §6.5);
4. with the PINNED binding / contract / transport material (§5.7) -- never
   today's binding: a retained material that no longer matches the pin
   (upgraded version, new contract, re-keyed connection generation) refuses
   the resend with ``app_contract_mismatch``. This is decided locally, so it
   runs before step 3's entitlements call (§9: no external request before
   every locally decidable precondition). The receipt path (2) never compares;
5. a fresh write-ahead audit row (failure: ``app_mutation_audit_failed``, no
   egress), then the SAME normalized body + SAME idempotency identity with a
   fresh assertion / jti and ``recovery: "checked_resend"`` (wire §7.1).

Bounded: at most one lookup and one resend per call. A record past its own
window is ``expired`` and answered ``outcome_unknown`` without any call (wire
§6.5, A.14). A concurrent resolver sees the claimed record ``pending``.

``ReceiptLookup`` and ``RetainedMaterial`` are injected seams: the control-plane
HTTP carrier for ``receipt_lookup`` and the durable retained-material store
are later wiring (the claim/body encoding is here: :func:`receipt_lookup_request`).
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from .binding_store import BindingRecord, BindingState
from .boundary_audit import audit_metadata
from .entitlements import EntitlementRefused
from .jcs import canonicalize
from .pending import (
    IN_FLIGHT_STALE_AFTER_S,
    PendingRecord,
    PendingRefused,
    PendingState,
    PendingStore,
    ReceiptInvalid,
    check_receipt,
)
from .scope import AppScope
from .transport import TransportRefused

__all__ = [
    "APP_CONTRACT_MISMATCH",
    "APP_INTERNAL",
    "APP_MUTATION_AUDIT_FAILED",
    "APP_MUTATION_NOT_FOUND",
    "APP_PACK_UNAVAILABLE",
    "CONTROL_ASSERTION_LIFETIME_S",
    "PURPOSE_RECEIPT_LOOKUP",
    "RESOLVER_STATUSES",
    "OutcomeResolver",
    "Provenance",
    "ReceiptLookup",
    "ReceiptLookupAnswer",
    "ResolverAnswer",
    "ResolverRefused",
    "RetainedMaterial",
    "receipt_lookup_body",
    "receipt_lookup_request",
]

APP_CONTRACT_MISMATCH = "app_contract_mismatch"
APP_INTERNAL = "app_internal"
APP_MUTATION_AUDIT_FAILED = "app_mutation_audit_failed"
APP_MUTATION_NOT_FOUND = "app_mutation_not_found"
APP_PACK_UNAVAILABLE = "app_pack_unavailable"

RESOLVER_STATUSES = ("receipt", "pending", "outcome_unknown")  # §11.4
PURPOSE_RECEIPT_LOOKUP = "receipt_lookup"  # wire §5, §10.3
CONTROL_ASSERTION_LIFETIME_S = 30  # wire §1.4 (<= 60 s); the A.4 vector's lifetime
_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_RECOVERY_STATES = frozenset({BindingState.ACTIVE, BindingState.DISABLED})


class ResolverRefused(Exception):
    """A platform refusal outside the resolver vocabulary (§11.4, §16.3).

    ``code`` is a §16.3 platform code; ``reason`` a fixed local diagnostic.
    """

    def __init__(self, code: str, reason: str, *, egress_attempted: bool = False) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.egress_attempted = egress_attempted


@dataclass(frozen=True)
class ResolverAnswer:
    """``status`` is one of :data:`RESOLVER_STATUSES`, or ``application_error``
    when a checked resend was answered with a DECLARED pack error (definitely no
    effect; the declared, contract-bound envelope passes through, §16.4).
    ``reason`` is a local diagnostic, never model-facing."""

    status: str
    receipt: Optional[dict] = None
    application_error: Optional[dict] = None
    reason: str = ""

    def status_body(self) -> dict:
        """The body of the pack's mutation-status mode (wire §10.2)."""
        if self.status == "application_error":
            return dict(self.application_error or {})
        body: dict = {"ok": True, "status": self.status}
        if self.receipt is not None:
            body["receipt"] = dict(self.receipt)
        return body


@dataclass(frozen=True)
class ReceiptLookupAnswer:
    """The application's answer to ``receipt_lookup`` (wire §10.3): ``receipt``
    (with the §13.6 receipt) or ``absent``. Anything else is unusable."""

    status: str
    receipt: Optional[Mapping[str, Any]] = None


class ReceiptLookup(Protocol):
    """POST <internal_endpoints.receipt_lookup> for the PINNED record (wire §10.3)."""

    def lookup(self, record: PendingRecord) -> ReceiptLookupAnswer: ...


class RetainedMaterial(Protocol):
    """The retained immutable binding + bundle for a record (§5.7)."""

    def material_for(self, record: PendingRecord) -> tuple[BindingRecord, Any]: ...


class RecoveryEntitlements(Protocol):
    def recheck_for_recovery(self, scope: AppScope, mutation_ref: str, pending: Any) -> Any: ...


class AuditSink(Protocol):
    def append(self, scope: AppScope, kind: str, status: str, metadata: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class Provenance:
    """Facts about the RECOVERING request: audit / assertion provenance only,
    never a scope or identity input (§11.2)."""

    session_id: Optional[str] = None
    actor_id: Optional[str] = None
    connector_id: Optional[str] = None
    tool: Optional[str] = None
    mode: Optional[str] = None

    def metadata(self) -> dict:
        return audit_metadata(
            {
                "session_id": self.session_id,
                "actor_id": self.actor_id,
                "connector_id": self.connector_id,
                "tool": self.tool,
                "mode": self.mode,
            }
        )


# -- receipt lookup encoding (wire §5, §10.3, A.4, A.9) ------------------------------------


def receipt_lookup_body(idempotency_key: str) -> bytes:
    """The JCS body ``{"idempotency_key": ...}`` (wire §10.3, A.9)."""
    if type(idempotency_key) is not str or not _KEY_RE.fullmatch(idempotency_key):
        raise ValueError("idempotency_key is not wire-shaped (wire §7, A.10)")
    return canonicalize({"idempotency_key": idempotency_key})


def receipt_lookup_request(record: PendingRecord, *, call_id: str, jti: str, now: int) -> tuple[bytes, dict]:
    """Body + control-assertion claims (wire §5) for one lookup, from the PINNED
    record only: its issuer, audience, binding, binding version and contract."""
    body = receipt_lookup_body(record.idempotency_key)
    if not all(type(v) is str and v for v in (call_id, jti)) or type(now) is not int:
        raise ValueError("call facts malformed")
    claims = {
        "iss": record.aidocs_issuer,
        "aud": record.audience,
        "iat": now,
        "exp": now + CONTROL_ASSERTION_LIFETIME_S,
        "jti": jti,
        "tenant_id": record.scope.tenant_id,
        "binding_id": record.binding_id,
        "pack_id": record.pack_id,
        "contract_digest": record.contract_digest,
        "binding_version": record.binding_version,
        "purpose": PURPOSE_RECEIPT_LOOKUP,
        "sub": record.scope.sub,
        "call_id": call_id,
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
    return body, claims


# -- the resolver ----------------------------------------------------------------------------


def _unknown(reason: str) -> ResolverAnswer:
    return ResolverAnswer("outcome_unknown", reason=reason)


def _receipt(receipt: dict, reason: str) -> ResolverAnswer:
    return ResolverAnswer("receipt", receipt=receipt, reason=reason)


class OutcomeResolver:
    """The one resolver semantic (§11.4). Every dependency is an explicit input."""

    def __init__(
        self,
        *,
        store: PendingStore,
        entitlements: RecoveryEntitlements,
        receipt_lookup: ReceiptLookup,
        material: RetainedMaterial,
        transport: Any,
        audit: AuditSink,
        clock: Callable[[], int],
        call_id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._store = store
        self._ents = entitlements
        self._lookup = receipt_lookup
        self._material = material
        self._transport = transport
        self._audit = audit
        self._clock = clock
        self._call_id = call_id_factory or (lambda: "call_" + secrets.token_urlsafe(16))

    # -- audit ------------------------------------------------------------------

    def _audit_required(self, scope: AppScope, md: Mapping[str, Any], *, code: str) -> None:
        try:
            self._audit.append(scope, "app_call_admitted", "admitted", dict(md))
        except Exception:  # noqa: BLE001 -- any audit failure: no egress (§16)
            raise ResolverRefused(code, "pre_call_audit_failed") from None

    def _audit_best_effort(self, scope: AppScope, kind: str, status: str, md: Mapping[str, Any]) -> bool:
        try:
            self._audit.append(scope, kind, status, dict(md))
            return True
        except Exception:  # noqa: BLE001 -- a post-fact row never changes the truthful answer
            return False

    @staticmethod
    def _record_md(rec: PendingRecord, base: Mapping[str, Any]) -> dict:
        return {
            **dict(base),
            **audit_metadata(
                {
                    "tool": rec.tool,
                    "mode": rec.mode,
                    "pack_id": rec.pack_id,
                    "binding_id": rec.binding_id,
                    "contract_digest": rec.contract_digest,
                    "input_digest": rec.body_sha256,
                }
            ),
        }

    # -- public -----------------------------------------------------------------

    def resolve(self, scope: AppScope, mutation_ref: Any, *, provenance: Optional[Provenance] = None) -> ResolverAnswer:
        if type(scope) is not AppScope:
            raise TypeError("scope must be an AppScope")
        prov = provenance if isinstance(provenance, Provenance) else Provenance()
        base = prov.metadata()
        # 0. write-ahead row for this (read) call: fails closed as app_internal.
        self._audit_required(scope, base, code=APP_INTERNAL)
        # 1. the CURRENT AppScope + ref select the record, or nothing at all.
        rec = self._store.get(scope, mutation_ref) if type(mutation_ref) is str else None
        if rec is None:
            self._audit_best_effort(scope, "app_call_refused", "refused", {**base, "refusal_code": APP_MUTATION_NOT_FOUND})
            raise ResolverRefused(APP_MUTATION_NOT_FOUND, "mutation_ref_not_resolvable")
        return self._resolve_record(rec, prov, self._record_md(rec, base))

    # -- the record ---------------------------------------------------------------

    def _resolve_record(self, rec: PendingRecord, prov: Provenance, md: dict) -> ResolverAnswer:
        now = self._clock()
        if rec.state is PendingState.RESOLVED:
            return _receipt(rec.receipt, "receipt_held")
        if rec.state is PendingState.FAILED:
            if rec.application_error is not None:
                return ResolverAnswer("application_error", application_error=rec.application_error,
                                      reason="declared_application_error")
            raise ResolverRefused(rec.failure_code, "mutation_not_applied")
        if rec.state is PendingState.EXPIRED:
            return _unknown("recovery_window_closed")
        if rec.state is PendingState.TERMINATED:
            return _unknown("terminated_without_receipt")
        if rec.state is PendingState.IN_FLIGHT:
            if now < rec.state_since + IN_FLIGHT_STALE_AFTER_S:
                return ResolverAnswer("pending", reason="in_flight")
            rec = self._move(rec, PendingState.OUTCOME_UNKNOWN, now)
            if rec is None or rec.state is not PendingState.OUTCOME_UNKNOWN:
                return ResolverAnswer("pending", reason="in_flight")
        # rec is OUTCOME_UNKNOWN
        if not now < rec.recovery_window_closes_at:
            # AIDOCS owns the window (wire §6.5): no lookup, no entitlements call.
            self._move(rec, PendingState.EXPIRED, now)
            return _unknown("recovery_window_closed")
        claimed = self._move(rec, PendingState.IN_FLIGHT, now)
        if claimed is None or claimed.state is not PendingState.IN_FLIGHT or claimed.state_since != now:
            return ResolverAnswer("pending", reason="claimed_elsewhere")
        try:
            return self._recover(claimed, prov, md)
        finally:
            self._release(claimed)

    def _move(self, rec: PendingRecord, to: PendingState, now: int, **extra: Any) -> Optional[PendingRecord]:
        try:
            return self._store.transition(rec.scope, rec.mutation_ref, expected=rec.state, to=to, now=now, **extra)
        except PendingRefused:
            return self._store.get(rec.scope, rec.mutation_ref)

    def _release(self, claimed: PendingRecord) -> None:
        """Hand an unresolved claim back to ``outcome_unknown`` (only OUR claim)."""
        cur = self._store.get(claimed.scope, claimed.mutation_ref)
        if cur is not None and cur.state is PendingState.IN_FLIGHT and cur.state_since == claimed.state_since:
            self._move(cur, PendingState.OUTCOME_UNKNOWN, self._clock())

    def _resolved(self, rec: PendingRecord, receipt: dict, md: dict, reason: str) -> ResolverAnswer:
        done = self._move(rec, PendingState.RESOLVED, self._clock(), receipt=receipt)
        if done is not None and done.state is PendingState.RESOLVED:
            receipt = done.receipt
        row = {**md, **audit_metadata({"receipt_id": receipt.get("receipt_id")})}
        self._audit_best_effort(rec.scope, "app_mutation_applied", "applied", row)
        return _receipt(receipt, reason)

    def _recover(self, rec: PendingRecord, prov: Provenance, md: dict) -> ResolverAnswer:
        # 2. receipt replay via the application's control-plane lookup (pinned record).
        try:
            answer = self._lookup.lookup(rec)
        except Exception:  # noqa: BLE001 -- unreachable lookup: never resend blind
            return _unknown("receipt_lookup_unavailable")
        status = getattr(answer, "status", None) if isinstance(answer, ReceiptLookupAnswer) else None
        if status == "receipt":
            try:
                receipt = check_receipt(answer.receipt, reversible=rec.reversible)
            except ReceiptInvalid:
                return _unknown("receipt_lookup_invalid")
            return self._resolved(rec, receipt, md, "receipt_replayed")
        if status != "absent" or answer.receipt is not None:
            return _unknown("receipt_lookup_invalid")
        # 4. (local precondition) the pinned material, never today's binding.
        binding, bundle = self._pinned_material(rec)
        # 3. current identity / link / entitlement / capability.
        try:
            entitlement = self._ents.recheck_for_recovery(rec.scope, rec.mutation_ref, self._store)
        except EntitlementRefused as exc:
            raise ResolverRefused(exc.code, exc.reason, egress_attempted=exc.egress_attempted) from None
        # 5. write-ahead, then the exact resend.
        self._audit_required(rec.scope, md, code=APP_MUTATION_AUDIT_FAILED)
        try:
            result = self._transport.call(
                binding=binding,
                bundle=bundle,
                scope=rec.scope,
                entitlement=entitlement,
                tool=rec.tool,
                mode=rec.mode,
                arguments=rec.arguments,
                call_id=self._call_id(),
                capabilities_used=frozenset(rec.capabilities_used),
                idempotency_key=rec.idempotency_key,
                session_id=prov.session_id,
                actor_id=prov.actor_id,
                recovery=True,
            )
        except TransportRefused as exc:
            if exc.possibly_delivered:
                self._audit_best_effort(rec.scope, "app_mutation_outcome_unknown", "outcome_unknown", md)
                return _unknown("resend_outcome_unknown")
            raise ResolverRefused(exc.code, exc.reason, egress_attempted=exc.egress_attempted) from None
        except Exception:  # noqa: BLE001 -- unexpected after the resend began: never a definite failure
            self._audit_best_effort(rec.scope, "app_mutation_outcome_unknown", "outcome_unknown", md)
            return _unknown("resend_failed_unexpectedly")
        out = result.output
        if not out.ok:
            self._move(rec, PendingState.FAILED, self._clock(), failure_code=out.error_code,
                       application_error=dict(out.body))
            self._audit_best_effort(rec.scope, "app_call_refused", "refused", md)
            return ResolverAnswer("application_error", application_error=dict(out.body),
                                  reason="declared_application_error")
        try:
            receipt = check_receipt(out.body.get("receipt"), reversible=rec.reversible)
        except ReceiptInvalid:
            self._audit_best_effort(rec.scope, "app_mutation_outcome_unknown", "outcome_unknown", md)
            return _unknown("resend_without_receipt")
        return self._resolved(rec, receipt, md, "receipt_from_resend")

    def _pinned_material(self, rec: PendingRecord) -> tuple[BindingRecord, Any]:
        try:
            binding, bundle = self._material.material_for(rec)
        except Exception:  # noqa: BLE001 -- retained material gone: fail closed
            raise ResolverRefused(APP_PACK_UNAVAILABLE, "pinned_material_unavailable") from None
        if not isinstance(binding, BindingRecord) or binding.trust is None:
            raise ResolverRefused(APP_PACK_UNAVAILABLE, "pinned_material_unavailable")
        pinned = (
            rec.binding_id,
            rec.scope.tenant_id,
            rec.scope.toolspace_id,
            rec.binding_version,
            rec.pack_id,
            rec.contract_digest,
            rec.application_origin,
            rec.audience,
            rec.aidocs_issuer,
            rec.connection_generation,
            rec.pack_id,
            rec.contract_digest,
        )
        retained = (
            binding.binding_id,
            binding.tenant_id,
            binding.toolspace_id,
            binding.binding_version,
            binding.pack_id,
            binding.contract_digest,
            binding.application_origin,
            binding.audience,
            binding.trust.aidocs_issuer,
            binding.trust.transcript_sha256,
            getattr(bundle, "pack_id", None),
            getattr(bundle, "contract_digest", None),
        )
        if pinned != retained:
            # §5.7: never replay an old mutation against a new contract / binding.
            raise ResolverRefused(APP_CONTRACT_MISMATCH, "pinned_material_changed")
        tools = getattr(bundle, "tools", None)
        tspec = tools.get(rec.tool) if isinstance(tools, Mapping) else None
        mspec = tspec.modes.get(rec.mode) if tspec is not None else None
        if (
            mspec is None
            or rec.tool not in binding.tool_names
            or mspec.idempotency != "required"
            or mspec.platform_handler is not None
        ):
            raise ResolverRefused(APP_CONTRACT_MISMATCH, "pinned_mode_changed")
        if binding.state not in _RECOVERY_STATES:
            raise ResolverRefused(APP_PACK_UNAVAILABLE, "pinned_binding_not_recovery_eligible")
        return binding, bundle
