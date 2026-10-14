"""The thin orchestration entry for a mutation mode and the platform-handled
``mutation_status`` mode -- RFC 0003 v4.6.1 §9, §11, §13.6, §16 (build plan S14).

"The write path is complete from the FIRST write" (build plan §2): the first
``crm_edit`` already runs through server-derived identity, the pending record,
``app_outcome_unknown`` and the one resolver.

:meth:`WritePath.call_mutation`, in §9 order:

1. every locally decidable precondition first (the pinned mode is a mutation
   and not platform-handled, the input schema selects exactly one branch, and
   a DRY build of the request assertion -- binding active, entitlement of this
   seat, capability envelope, conversation context, idempotency-key shape);
   a refusal here makes no audit row, no record and no egress;
2. the pre-call boundary audit row (write-ahead, §16). Failure:
   ``app_mutation_audit_failed``, NO record, NO egress;
3. the pending record (server-minted ``mutation_ref``, derived
   ``idempotency_key``, pinned binding/contract, §5.7). Failure:
   ``app_internal``, no egress;
4. the transport call. ANY ``TransportRefused`` with ``possibly_delivered``
   (3xx, post-send floor refusal, 5xx, timeout, connection loss) -- and any
   unexpected failure once the call began -- is ``app_outcome_unknown``: only
   the opaque ref is exposed and the record stays OPEN for the resolver. It is
   never retried inside the call (§11.2). A refusal that provably sent nothing
   is a definite refusal (record ``failed``);
5. a declared pack error is a definite ``application_error`` (record
   ``failed``); a success must carry a §13.6 receipt, otherwise AIDOCS claims
   no effect and answers ``app_outcome_unknown`` (record open);
6. the outcome row (best effort: an audit failure AFTER the effect never turns
   a truthful outcome into a false one; ``audit_complete`` reports it).

:meth:`WritePath.call_platform_mode` answers a mode whose bundle declares
``platform_handler = "mutation_resolver"`` (wire §10.2) from the one resolver;
its ``relative_endpoint`` is never called. The answer passes the pack's own
output floor for that mode.

A mode declaring ``file_params`` (S16, RFC §14) additionally needs the
configured ``host_file_ingress`` (else ``app_pack_unavailable``, nothing
recorded or sent). Steps 1-2 run on the arguments with the file parameter
REDACTED to ``{}``, so the dry build and the pre-call row never see the signed
URL; the host file is fetched only AFTER the pre-call row is admitted
(§14.1). A fetch refusal is a definite refusal (``app_call_refused`` with the
``host_file_*`` / ``app_validation_failed`` code; no record, no application
egress). From step 3 on, the arguments are the RELAYED ones
(:func:`~.host_file_ingress.relay_arguments`): the pending record, the
idempotency key and ``body_sha256`` pin a capability-free, content-addressed
body. The spool is destroyed in a ``finally`` whatever the outcome.

S5 admission is not built: :class:`AdmittedCall` is an already-admitted
context (seat, toolspace authority, binding, rate, freeze, entitlement).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .binding_store import BindingRecord
from .boundary_audit import audit_metadata
from .entitlements import Entitlement
from .host_file_ingress import HOST_FILE_PROVENANCE_GENERIC, HostFileRefused, host_input_schema, relay_arguments
from .jcs import JcsError, canonicalize
from .output_guard import ResponseRefused, declared_error_codes_from_bundle, guard_output, schema_accepts
from .pending import (
    RECOVERY_WINDOW_MIN_S,
    PendingRecord,
    PendingState,
    PendingStore,
    ReceiptInvalid,
    check_receipt,
    derive_idempotency_key,
    mint_mutation_ref,
    new_pending_record,
)
from .resolver import OutcomeResolver, Provenance, ResolverRefused
from .scope import AppScope
from .transport import TransportRefused, build_tool_assertion_claims

__all__ = [
    "APP_BACKEND_INVALID_RESPONSE",
    "APP_INTERNAL",
    "APP_MUTATION_AUDIT_FAILED",
    "APP_OUTCOME_UNKNOWN",
    "APP_PACK_UNAVAILABLE",
    "APP_VALIDATION_FAILED",
    "PLATFORM_HANDLER_MUTATION_RESOLVER",
    "AdmittedCall",
    "CallOutcome",
    "WritePath",
    "platform_refusal_body",
]

APP_BACKEND_INVALID_RESPONSE = "app_backend_invalid_response"
APP_INTERNAL = "app_internal"
APP_MUTATION_AUDIT_FAILED = "app_mutation_audit_failed"
APP_OUTCOME_UNKNOWN = "app_outcome_unknown"
APP_PACK_UNAVAILABLE = "app_pack_unavailable"
APP_VALIDATION_FAILED = "app_validation_failed"
PLATFORM_HANDLER_MUTATION_RESOLVER = "mutation_resolver"  # wire §10.2

# Fixed, non-disclosing model-facing messages (§16.3). Never echo backend bytes.
_MESSAGES = {
    APP_OUTCOME_UNKNOWN: (
        "The outcome of this change is unconfirmed. Do not repeat or change it; "
        "ask for its status with this mutation_ref."
    ),
    "app_mutation_not_found": "No change with this mutation_ref is available.",
    APP_MUTATION_AUDIT_FAILED: "The change was not sent: it could not be recorded.",
}
_DEFAULT_MESSAGE = "The request was not completed."


def platform_refusal_body(code: str, mutation_ref: Optional[str] = None) -> dict:
    """A platform ``app_*`` refusal envelope (§13.2 shape, AIDOCS-produced)."""
    body: dict = {"ok": False, "error": {"code": code, "message": _MESSAGES.get(code, _DEFAULT_MESSAGE)}}
    if mutation_ref is not None:
        body["mutation_ref"] = mutation_ref  # §11.2: the ONLY thing exposed
    return body


@dataclass(frozen=True)
class AdmittedCall:
    """An S5-admitted application call (taken as an explicit input here)."""

    scope: AppScope
    binding: BindingRecord
    bundle: Any
    entitlement: Entitlement
    tool: str
    mode: str
    arguments: Mapping[str, Any]
    capabilities_used: frozenset
    call_id: str
    connector_id: Optional[str] = None
    session_id: Optional[str] = None
    actor_id: Optional[str] = None
    host_kind: Optional[str] = None
    host_conversation_ref: Optional[str] = None
    #: Frozen at admission from server-authenticated state (r0b2 9cf90cf5).
    host_file_provenance: str = HOST_FILE_PROVENANCE_GENERIC


@dataclass(frozen=True)
class CallOutcome:
    """``status``: ``applied`` | ``application_error`` | ``outcome_unknown`` |
    ``refused`` | ``answered`` (platform mode). ``code`` is the §16.3 platform
    code (refused / outcome_unknown) or the declared pack code
    (application_error). ``body`` is the model-facing result."""

    status: str
    code: Optional[str]
    body: dict
    mutation_ref: Optional[str] = None
    receipt: Optional[dict] = None
    audit_complete: bool = True


def _refused(code: str) -> CallOutcome:
    return CallOutcome("refused", code, platform_refusal_body(code))


def _mode_spec(bundle: Any, tool: str, mode: str) -> Any:
    tools = getattr(bundle, "tools", None)
    tspec = tools.get(tool) if isinstance(tools, Mapping) else None
    return tspec.modes.get(mode) if tspec is not None else None


def _select_branch(mspec: Any, arguments: Mapping[str, Any]) -> Any:
    """The ONE branch the body selects (§6.3.2); None when not exactly one."""
    branches = tuple(getattr(mspec, "branches", ()) or ())
    if mspec.discriminator is None:
        return branches[0] if len(branches) == 1 else None
    if mspec.discriminator not in arguments:
        return None
    try:
        wanted = canonicalize(arguments[mspec.discriminator])
        hits = [b for b in branches if canonicalize(b.discriminator_value) == wanted]
    except (JcsError, TypeError, ValueError):
        return None
    return hits[0] if len(hits) == 1 else None


class WritePath:
    """Call a mutation mode; answer the platform-handled status mode."""

    def __init__(
        self,
        *,
        store: PendingStore,
        transport: Any,
        audit: Any,
        resolver: OutcomeResolver,
        clock: Callable[[], int],
        recovery_window_s: int = RECOVERY_WINDOW_MIN_S,
        host_file_ingress: Any = None,
    ) -> None:
        if type(recovery_window_s) is not int or recovery_window_s < RECOVERY_WINDOW_MIN_S:
            raise ValueError("the recovery window is at least 7 days (RFC §5.7)")
        self._ingress = host_file_ingress
        self._store = store
        self._transport = transport
        self._audit = audit
        self._resolver = resolver
        self._clock = clock
        self._window_s = recovery_window_s

    # -- audit ------------------------------------------------------------------

    def _row(self, scope: AppScope, kind: str, status: str, md: Mapping[str, Any]) -> bool:
        try:
            self._audit.append(scope, kind, status, dict(md))
            return True
        except Exception:  # noqa: BLE001 -- post-fact rows are best effort
            return False

    @staticmethod
    def _md(call: AdmittedCall, body_sha256: str, *, host_file: bool = False) -> dict:
        fields = {
            "tool": call.tool,
            "mode": call.mode,
            "pack_id": call.binding.pack_id,
            "binding_id": call.binding.binding_id,
            "binding_version": call.binding.binding_version,
            "contract_digest": call.binding.contract_digest,
            "input_digest": body_sha256,
            "connector_id": call.connector_id,
            "session_id": call.session_id,
            "actor_id": call.actor_id,
        }
        if host_file:  # the host-file egress authority, durable on every upload row (r0b2)
            fields["provider_class"] = call.host_file_provenance
        return audit_metadata(fields)

    def _move(self, rec: PendingRecord, to: PendingState, **extra: Any) -> None:
        try:
            self._store.transition(rec.scope, rec.mutation_ref, expected=PendingState.IN_FLIGHT, to=to,
                                   now=self._clock(), **extra)
        except Exception:  # noqa: BLE001 -- the record stays open; the resolver owns it
            pass

    def _unknown(self, rec: PendingRecord, md: dict) -> CallOutcome:
        self._move(rec, PendingState.OUTCOME_UNKNOWN)
        ok = self._row(rec.scope, "app_mutation_outcome_unknown", "outcome_unknown", md)
        return CallOutcome(
            "outcome_unknown",
            APP_OUTCOME_UNKNOWN,
            platform_refusal_body(APP_OUTCOME_UNKNOWN, rec.mutation_ref),
            mutation_ref=rec.mutation_ref,
            audit_complete=ok,
        )

    # -- the mutation mode ----------------------------------------------------------

    def call_mutation(self, call: AdmittedCall) -> CallOutcome:
        if not isinstance(call, AdmittedCall) or type(call.scope) is not AppScope:
            return _refused(APP_INTERNAL)
        mspec = _mode_spec(call.bundle, call.tool, call.mode)
        if mspec is None or mspec.platform_handler is not None or mspec.idempotency != "required":
            return _refused(APP_PACK_UNAVAILABLE)  # not a mutation mode of this contract
        arguments = call.arguments
        # RAW host arguments: file params against the host contract, the rest
        # pinned (same rule as runtime admission; the relay is re-checked
        # against the pinned schema by the transport).
        raw_schema = host_input_schema(mspec.input_schema, getattr(mspec, "file_params", ()) or ())
        if not isinstance(arguments, Mapping) or not schema_accepts(raw_schema, dict(arguments)):
            return _refused(APP_VALIDATION_FAILED)
        branch = _select_branch(mspec, arguments)
        if branch is None:
            return _refused(APP_VALIDATION_FAILED)
        file_params = tuple(getattr(mspec, "file_params", ()) or ())
        content: dict = {}
        pre_args: Mapping[str, Any] = arguments
        if file_params:
            if self._ingress is None or len(file_params) != 1:
                return _refused(APP_PACK_UNAVAILABLE)  # no host-file floor configured: never fetch
            # §14.1: the signed URL never reaches the dry build or the pre-call row.
            pre_args = {**dict(arguments), file_params[0]: {}}
            # Dry-build placeholders ONLY (shape check of the §14.3 claims); never signed or sent.
            content = {"content_sha256": "0" * 64, "content_length": 1, "content_type": "application/pdf"}
        try:
            body = canonicalize(dict(pre_args))
        except (JcsError, TypeError, ValueError):
            return _refused(APP_VALIDATION_FAILED)
        body_sha256 = hashlib.sha256(body).hexdigest()
        now = self._clock()
        ref = mint_mutation_ref()
        binding = call.binding
        try:
            key = derive_idempotency_key(
                scope=call.scope, binding_id=binding.binding_id, pack_id=binding.pack_id,
                contract_digest=binding.contract_digest, tool=call.tool, mode=call.mode,
                body_sha256=body_sha256, mutation_ref=ref,
            )
            # 1. dry build: every locally decidable assertion precondition, no side effect.
            build_tool_assertion_claims(
                binding=binding, bundle=call.bundle, scope=call.scope, entitlement=call.entitlement,
                tool=call.tool, mode=call.mode, body=pre_args, call_id=call.call_id, jti="jti_preflight",
                now=now, capabilities_used=call.capabilities_used, idempotency_key=key,
                session_id=call.session_id, actor_id=call.actor_id, host_kind=call.host_kind,
                host_conversation_ref=call.host_conversation_ref, **content,
            )
        except TransportRefused as exc:
            return _refused(exc.code)
        except Exception:  # noqa: BLE001 -- malformed context: fail closed, nothing sent
            return _refused(APP_INTERNAL)
        md = self._md(call, body_sha256, host_file=bool(file_params))
        # 2. write-ahead pre-call row.
        try:
            self._audit.append(call.scope, "app_call_admitted", "admitted", dict(md))
        except Exception:  # noqa: BLE001 -- §16 / S12: a mutation's pre-call failure
            return _refused(APP_MUTATION_AUDIT_FAILED)
        if not file_params:
            return self._send(call, branch, arguments, md, now, ref, None)
        # 2b. §14.1: the host file is fetched only once the pre-call row is admitted.
        try:
            host_file = self._ingress.fetch(arguments[file_params[0]], provenance=call.host_file_provenance)
        except HostFileRefused as exc:
            ok = self._row(call.scope, "app_call_refused", "refused", {**md, "refusal_code": exc.code})
            out = _refused(exc.code)
            return CallOutcome(out.status, out.code, out.body, audit_complete=ok)
        except Exception:  # noqa: BLE001 -- nothing reached the application
            ok = self._row(call.scope, "app_call_refused", "refused", {**md, "refusal_code": APP_INTERNAL})
            out = _refused(APP_INTERNAL)
            return CallOutcome(out.status, out.code, out.body, audit_complete=ok)
        try:
            relayed = relay_arguments(arguments, file_params, host_file)
            md = self._md(call, hashlib.sha256(canonicalize(relayed)).hexdigest(), host_file=True)
            return self._send(call, branch, relayed, md, now, ref, host_file)
        finally:
            host_file.close()  # §14.3: the ephemeral spool is destroyed, whatever happened

    def _send(
        self, call: AdmittedCall, branch: Any, arguments: Mapping[str, Any], md: dict, now: int, ref: str,
        host_file: Any,
    ) -> CallOutcome:
        """Steps 3-6: the pending record, the one transport call, the outcome."""
        binding = call.binding
        # 3. the pending record.
        try:
            record = new_pending_record(
                scope=call.scope, binding=binding, tool=call.tool, mode=call.mode, branch_id=branch.branch_id,
                reversible=branch.reversible, arguments=arguments, capabilities_used=call.capabilities_used,
                now=now, window_s=self._window_s, mutation_ref=ref,
            )
            self._store.create(record)
        except Exception:  # noqa: BLE001 -- no record, no egress
            self._row(call.scope, "app_call_refused", "refused", {**md, "refusal_code": APP_INTERNAL})
            return _refused(APP_INTERNAL)
        # 4. the one transport call; never retried here.
        extra = {"host_file": host_file} if host_file is not None else {}
        try:
            result = self._transport.call(
                binding=binding, bundle=call.bundle, scope=call.scope, entitlement=call.entitlement,
                tool=call.tool, mode=call.mode, arguments=arguments, call_id=call.call_id,
                capabilities_used=call.capabilities_used, idempotency_key=record.idempotency_key,
                session_id=call.session_id, actor_id=call.actor_id, host_kind=call.host_kind,
                host_conversation_ref=call.host_conversation_ref, **extra,
            )
        except TransportRefused as exc:
            if exc.possibly_delivered:
                return self._unknown(record, md)
            self._move(record, PendingState.FAILED, failure_code=exc.code)
            ok = self._row(call.scope, "app_call_refused", "refused", {**md, "refusal_code": exc.code})
            out = _refused(exc.code)
            return CallOutcome(out.status, out.code, out.body, audit_complete=ok)
        except Exception:  # noqa: BLE001 -- the call began: never report a definite failure
            return self._unknown(record, md)
        # 5. outcome.
        output = result.output
        if not output.ok:
            self._move(record, PendingState.FAILED, failure_code=output.error_code,
                       application_error=dict(output.body))
            ok = self._row(call.scope, "app_call_refused", "refused", md)
            return CallOutcome("application_error", output.error_code, dict(output.body), audit_complete=ok)
        try:
            receipt = check_receipt(output.body.get("receipt"), reversible=branch.reversible)
        except ReceiptInvalid:
            return self._unknown(record, md)  # §13.6: no receipt, no claim of effect
        self._move(record, PendingState.RESOLVED, receipt=receipt)
        row = {**md, **audit_metadata({"receipt_id": receipt.get("receipt_id"),
                                        "response_digest": result.response_sha256})}
        ok = self._row(call.scope, "app_mutation_applied", "applied", row)
        return CallOutcome("applied", None, dict(output.body), receipt=receipt, audit_complete=ok)

    # -- the platform-handled status mode (wire §10.2) ------------------------------------

    def call_platform_mode(self, call: AdmittedCall) -> CallOutcome:
        if not isinstance(call, AdmittedCall) or type(call.scope) is not AppScope:
            return _refused(APP_INTERNAL)
        mspec = _mode_spec(call.bundle, call.tool, call.mode)
        if mspec is None or mspec.platform_handler != PLATFORM_HANDLER_MUTATION_RESOLVER:
            return _refused(APP_PACK_UNAVAILABLE)
        arguments = call.arguments
        if not isinstance(arguments, Mapping) or not schema_accepts(mspec.input_schema, dict(arguments)):
            return _refused(APP_VALIDATION_FAILED)
        prov = Provenance(
            session_id=call.session_id, actor_id=call.actor_id, connector_id=call.connector_id,
            tool=call.tool, mode=call.mode,
        )
        try:
            answer = self._resolver.resolve(call.scope, arguments["mutation_ref"], provenance=prov)
        except ResolverRefused as exc:
            return _refused(exc.code)
        body = answer.status_body()
        try:
            guarded = guard_output(
                body, mode=mspec, declared_codes=declared_error_codes_from_bundle(call.bundle),
                capabilities_used=call.capabilities_used,
            )
        except ResponseRefused:
            return _refused(APP_BACKEND_INVALID_RESPONSE)
        return CallOutcome("answered", guarded.error_code, dict(guarded.body), receipt=answer.receipt)
