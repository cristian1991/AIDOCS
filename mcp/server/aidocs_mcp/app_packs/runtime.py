"""The per-org application runtime -- the RFC 0003 composition root (S5 admission + wiring).

The gate transport (``outer_gate_transport._ogt_app_toolspace`` /
``_ogt_app_tools_call``) reaches the application path ONLY through
:func:`runtime_for_tenant` and four :class:`AppRuntime` methods:

* :meth:`AppRuntime.active_binding` -- R-a: the UNIQUE active binding of
  ``(tenant_id, toolspace_id)`` (0 or >= 2 raises ``BindingRefused``);
* :meth:`AppRuntime.binding_is_active`;
* :meth:`AppRuntime.bundle_for` -- the registry bundle the binding pins by
  ``(pack_id, contract_digest)`` (unknown raises);
* :meth:`AppRuntime.call` -- S5 admission (R-b, §9) and dispatch to the write
  path. It never raises for an ordinary refusal: a refused call is a
  ``CallOutcome("refused", <§16.3 code>, platform_refusal_body(code))``.

**Admission order** (§3.6 R-a/R-b, §9; "no external request occurs before all
preconditions that can be decided locally"), fail closed at every step:

1. the connection is this runtime's org and the binding the grant named;
2. the mode: ``arguments["mode"]`` names a mode of that tool in the pinned
   bundle, else ``app_validation_failed``;
3. seat pin (S6): keyed digest of ``_meta["openai/subject"]`` for ``(tenant,
   sub)``; first use pins, a mismatch / missing claim is
   ``app_seat_host_mismatch``;
4. freeze (S11) on the AppScope / toolspace / tenant (``app_pack_unavailable``);
5. rate / in-flight (S10), the permit held for the whole call and released
   after it (``app_rate_limited`` with a bounded ``retry_after``);
6. the remaining LOCAL preconditions before any egress: input schema + exactly
   one branch (§6.3.2), and ``host_conversation_ref`` (S15) when the mode
   requires conversation context;
7. entitlement (S9): the seat's attestation fetched / verified / cached through
   the entitlements control call (wire §5, §6.1); ``capabilities_used`` is the
   selected branch's ``required_capabilities`` and must be granted;
8. dispatch: a ``platform_handler == "mutation_resolver"`` mode ->
   :meth:`WritePath.call_platform_mode`; ``idempotency == "required"`` (every
   mutation, ``crm_upload`` included) -> :meth:`WritePath.call_mutation`; a
   read-only mode -> the read path here (see below).

**Reads.** The write path has no read entry, so the smallest read path lives
here: the pre-call ``app_call_admitted`` row (required; failure is
``app_internal``, §16), one :meth:`ExternalAppTransport.call` with NO
idempotency key, the guarded output wrapped as ``CallOutcome("answered")``
(or ``application_error`` for a declared pack error) and a best-effort
``app_read_completed`` / ``app_call_refused`` row.

**Control calls** (entitlements, ``receipt_lookup``). There was no HTTP carrier;
:class:`_AppTransport` adds one on the SAME pinned-origin / egress / address /
bounded-exchange machinery as tool calls (it reuses the transport's private
pre-egress helpers), signing a wire §5 control assertion (``aidocs-control+jwt``)
with the org's current signer.

**Stores** are the DURABLE ones: :class:`~.authority_db.AppAuthorityDB` at
``tenant_home(gate_root, tenant)/app_authority.sqlite3`` (bindings, pairing,
seat pins, freezes, pending records, the org key directory, link rows, the
DECISION rows) and the S12 :class:`~.app_audit_ledger.AppAuditLedger` next to
it. Rate counters are in-process (S10's documented hold).

**Configuration** (read when the process configuration is first built;
:func:`reset` drops it):

* ``AIDOCS_GATE_ROOT`` -- the gate root (the transport's ``gate_root``); or
  :func:`set_gate_root`, which wins over the env var;
* ``AIDOCS_APP_PACK_BUNDLES`` -- bundle JSON path(s), ``os.pathsep``-separated;
* ``AIDOCS_APP_PACK_SEALED_V1`` -- ``pack_id=sha256[,pack_id=sha256]``: every
  bundle file must be pinned and its file SHA-256 must equal the pin (and the
  registry re-checks its contract digest as the sealed v1 pack);
* ``AIDOCS_APP_HOST_FILE_HOSTS`` -- the host-file provider allowlist
  (comma-separated exact host names, or a named provider such as ``@openai``
  that expands to its exact host set); empty = every host file is refused;
* ``AIDOCS_APP_AIDOCS_ORIGIN`` / ``AIDOCS_APP_AIDOCS_ISSUER`` -- this
  deployment's canonical AIDOCS origin and issuer for pairing (default: the
  gate's public base; issuer defaults to the origin);
* ``AIDOCS_APP_SEAT_PIN_KEY`` -- hex master key (>= 32 bytes) the per-org seat
  pin keys are derived from; absent = every call refused ``app_internal``;
* ``AIDOCS_CODENEXUS_INTERNAL_URL`` / ``AIDOCS_INTERNAL_S2S_SECRET`` -- custody
  (read by :class:`~.vault_key_source.VaultKeySource` on every call);
* ``AIDOCS_CODENEXUS_DSN`` -- the grant table (link flow) and org roles;
* ``AIDOCS_PAIR_CLI_TENANT`` -- the org :func:`pair_cli_handlers` serves.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import jws
from .app_audit_ledger import AppAuditLedger, app_audit_ledger_path
from .app_freeze import AppFreezeService, AppScopeFrozen, FreezeCheckUnavailable
from .authority_db import AppAuthorityDB, app_authority_db_path
from .binding_store import BindingRecord, BindingRefused, BindingState, BindingStore
from .boundary_audit import audit_metadata
from .control_plane import LOCAL_REFUSAL_CODE, ControlPlaneHandlers, _error
from .conversation_ref import derive_host_conversation_ref
from .entitlements import (
    DECLARED_ERROR_MAP,
    EntitlementRefused,
    EntitlementService,
    EntitlementTerms,
    FetchResponse,
)
from .host_file_ingress import (
    HOST_FILE_PROVENANCE_CHATGPT,
    HOST_FILE_PROVENANCE_GENERIC,
    HostFileIngress,
    expand_host_file_hosts,
    host_input_schema,
)
from .jcs import canonicalize
from .link import LinkService
from .origin import PRODUCTION_ORIGIN_POLICY, OriginPolicy
from .output_guard import ResponseRefused, parse_bounded_json, schema_accepts
from .pairing import AidocsIdentity, OrgSignerPairingKeys, PairingService
from .rate import AppRateAdmissionUnavailable, AppRateLimited, AppRateLimiter
from .registry import PackRegistry
from .resolver import ReceiptLookupAnswer, receipt_lookup_request
from .scope import AppScope, ConnectionContext
from .seat_pin import SeatHostMismatch, SeatPinService, SeatPinUnavailable
from .signer import OrgRequestSigner, SignerError, SignerUnavailable
from .transport import EGRESS_PURPOSE, ExternalAppTransport, TransportRefused
from .vault_key_source import VaultKeySource, provision_org_key
from .write_path import (
    APP_INTERNAL,
    APP_PACK_UNAVAILABLE,
    APP_VALIDATION_FAILED,
    PLATFORM_HANDLER_MUTATION_RESOLVER,
    AdmittedCall,
    CallOutcome,
    WritePath,
    _select_branch,
    platform_refusal_body,
)

APP_CAPABILITY_REQUIRED = "app_capability_required"
APP_CONVERSATION_CONTEXT_UNAVAILABLE = "app_conversation_context_unavailable"
APP_RATE_LIMITED = "app_rate_limited"

__all__ = [
    "ENV_AIDOCS_ISSUER",
    "ENV_AIDOCS_ORIGIN",
    "ENV_BUNDLES",
    "ENV_GATE_ROOT",
    "ENV_HOST_FILE_HOSTS",
    "ENV_PAIR_CLI_TENANT",
    "ENV_SEALED_V1",
    "ENV_SEAT_PIN_KEY",
    "AppRuntime",
    "CONTRACT_UPGRADE_ENDPOINT",
    "ContractUpgradeFailed",
    "GateBindingReader",
    "RuntimeConfig",
    "RuntimeConfigError",
    "configure",
    "control_plane_for_tenant",
    "link_service",
    "pair_cli_handlers",
    "provision_org_signer",
    "reset",
    "runtime_config",
    "runtime_for_tenant",
    "set_gate_root",
]

ENV_GATE_ROOT = "AIDOCS_GATE_ROOT"
ENV_BUNDLES = "AIDOCS_APP_PACK_BUNDLES"
ENV_SEALED_V1 = "AIDOCS_APP_PACK_SEALED_V1"
ENV_HOST_FILE_HOSTS = "AIDOCS_APP_HOST_FILE_HOSTS"
ENV_AIDOCS_ORIGIN = "AIDOCS_APP_AIDOCS_ORIGIN"
ENV_AIDOCS_ISSUER = "AIDOCS_APP_AIDOCS_ISSUER"
ENV_SEAT_PIN_KEY = "AIDOCS_APP_SEAT_PIN_KEY"
ENV_PAIR_CLI_TENANT = "AIDOCS_PAIR_CLI_TENANT"

_FALLBACK_PUBLIC_BASE = "https://mcp.codenexus.cloud"  # outer_gate_transport.DEFAULT_PUBLIC_BASE
_ADMIN_ROLES = frozenset({"OWNER", "ADMIN"})
_SEAT_PIN_KDF_DOMAIN = b"aidocs-app-seat-pin-key/v1\x00"
_CONTROL_LIFETIME_S = 30  # wire §1.4 (<= 60 s), as the resolver's receipt_lookup assertion
_PURPOSE_ENTITLEMENTS = "entitlements"  # wire §5, §6.1
# #1132 in-place contract upgrade: the application's owner-approval exchange.
# A fixed path for now; a later contract should declare it among its internal
# endpoints (flagged to the reviewer).
_PURPOSE_CONTRACT_UPGRADE = "contract_upgrade"
CONTRACT_UPGRADE_ENDPOINT = "/v1/agent/contract/upgrade"
_APP_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HEX64_DIGEST = re.compile(r"[0-9a-f]{64}")


class ContractUpgradeFailed(BindingRefused):
    """An upgrade refusal. ``code`` is the closed control-plane code;
    ``application_code`` is the APPLICATION's own declared refusal code (e.g.
    ``crm_contract_upgrade_not_approved``), shown to the operator only."""

    def __init__(self, code: str, application_code: Optional[str] = None) -> None:
        super().__init__(code)
        self.application_code = application_code
_TENANT_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class RuntimeConfigError(RuntimeError):
    """The application runtime is not (correctly) configured. Fail closed: nothing is served."""


def _now() -> int:
    return int(time.time())


def _valid_tenant_id(value: Any) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 128
        and value[0].isalnum()
        and value.isascii()
        and set(value) <= _TENANT_ID_CHARS
    )


# -- configuration ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeConfig:
    """Process-wide inputs every org runtime is built from."""

    gate_root: Path
    registry: PackRegistry
    identity: AidocsIdentity
    host_file_hosts: tuple
    key_source: Any
    seat_pin_keys: Any
    admins: Any
    origin_policy: OriginPolicy = PRODUCTION_ORIGIN_POLICY
    transport_options: Mapping[str, Any] = field(default_factory=dict)
    host_file_options: Mapping[str, Any] = field(default_factory=dict)
    clock: Callable[[], int] = _now
    rate_limits_for: Optional[Callable[[str, str], Any]] = None
    grant_reader: Any = None


_OVERRIDE_KEYS = frozenset(
    {
        "gate_root", "registry", "aidocs_origin", "aidocs_issuer", "key_source", "vault_http", "vault_base_url",
        "vault_bearer", "seat_pin_keys", "admins", "origin_policy", "transport_options", "host_file_options",
        "clock", "rate_limits_for", "grant_reader", "host_file_hosts",
    }
)

_LOCK = threading.RLock()
_OVERRIDES: dict[str, Any] = {}
_GATE_ROOT: Optional[Path] = None
_CONFIG: Optional[RuntimeConfig] = None
_RUNTIMES: dict[str, "AppRuntime"] = {}
_LINK: Optional[LinkService] = None


def _drop_caches() -> None:
    global _CONFIG, _LINK
    _CONFIG = None
    _LINK = None
    _RUNTIMES.clear()


def configure(**overrides: Any) -> None:
    """Override configuration inputs (tests, embedding). Drops every cached runtime."""
    unknown = set(overrides) - _OVERRIDE_KEYS
    if unknown:
        raise TypeError(f"unknown runtime configuration keys: {sorted(unknown)}")
    with _LOCK:
        _OVERRIDES.update(overrides)
        _drop_caches()


def set_gate_root(path: Any) -> None:
    """The transport seam: the gate names its root (wins over ``AIDOCS_GATE_ROOT``)."""
    global _GATE_ROOT
    with _LOCK:
        new = Path(path)
        if _GATE_ROOT is not None and _GATE_ROOT == new:
            return
        _GATE_ROOT = new
        _drop_caches()


def reset(*, keep_overrides: bool = False) -> None:
    """Forget every cached runtime and the configuration (a "process restart")."""
    global _GATE_ROOT
    with _LOCK:
        _drop_caches()
        if not keep_overrides:
            _OVERRIDES.clear()
            _GATE_ROOT = None


def _gate_root() -> Path:
    if _GATE_ROOT is not None:
        return _GATE_ROOT
    if _OVERRIDES.get("gate_root") is not None:
        return Path(_OVERRIDES["gate_root"])
    raw = os.environ.get(ENV_GATE_ROOT, "").strip()
    if not raw:
        raise RuntimeConfigError(f"no gate root: set {ENV_GATE_ROOT} or call set_gate_root()")
    return Path(raw)


def _pins_from_env() -> dict[str, frozenset[str]]:
    """``pack_id=sha256`` entries; a pack listed more than once pins every listed
    digest (an in-place contract upgrade installs the old and the new)."""
    pins: dict[str, set[str]] = {}
    for item in os.environ.get(ENV_SEALED_V1, "").split(","):
        item = item.strip()
        if not item:
            continue
        pack_id, sep, digest = item.partition("=")
        if not sep or not pack_id.strip() or not digest.strip():
            raise RuntimeConfigError(f"{ENV_SEALED_V1} entries are pack_id=sha256")
        pins.setdefault(pack_id.strip(), set()).add(digest.strip().lower())
    if not pins:
        raise RuntimeConfigError(f"no sealed-v1 pin: set {ENV_SEALED_V1}")
    return {pack_id: frozenset(digests) for pack_id, digests in pins.items()}


def registry_from_env() -> PackRegistry:
    """Install every configured bundle as its pinned sealed-v1 pack. Any doubt refuses."""
    from .bundle import load_bundle

    paths = [p.strip() for p in os.environ.get(ENV_BUNDLES, "").split(os.pathsep) if p.strip()]
    if not paths:
        raise RuntimeConfigError(f"no application pack bundle: set {ENV_BUNDLES}")
    pins = _pins_from_env()
    try:
        registry = PackRegistry(pins)
    except ValueError as exc:
        raise RuntimeConfigError(f"invalid {ENV_SEALED_V1}: {exc}") from None
    for path in paths:
        try:
            data = Path(path).read_bytes()
            pack = load_bundle(data)
        except Exception as exc:  # noqa: BLE001 -- unreadable / invalid bundle: serve nothing
            raise RuntimeConfigError(f"bundle {path} cannot be loaded ({type(exc).__name__})") from None
        pin = pins.get(pack.pack_id)
        if not pin:
            raise RuntimeConfigError(f"bundle {path} ({pack.pack_id}) has no sealed-v1 pin")
        if hashlib.sha256(data).hexdigest() not in pin:
            raise RuntimeConfigError(f"bundle {path} does not match its pinned sha256")
        try:
            registry.install(data, as_sealed_v1=True)
        except Exception as exc:  # noqa: BLE001 -- RegistryRefused / BundleRefused
            raise RuntimeConfigError(f"bundle {path} refused by the registry: {exc}") from None
    return registry


def _identity(policy: OriginPolicy) -> AidocsIdentity:
    origin = _OVERRIDES.get("aidocs_origin") or os.environ.get(ENV_AIDOCS_ORIGIN, "").strip()
    if not origin:
        try:
            from ..outer_gate_transport import DEFAULT_PUBLIC_BASE as origin  # noqa: N811
        except Exception:  # noqa: BLE001
            origin = _FALLBACK_PUBLIC_BASE
    issuer = _OVERRIDES.get("aidocs_issuer") or os.environ.get(ENV_AIDOCS_ISSUER, "").strip() or origin
    try:
        return AidocsIdentity(origin=origin, issuer=issuer, origin_policy=policy)
    except ValueError as exc:
        raise RuntimeConfigError(f"invalid AIDOCS identity: {exc}") from None


def _host_file_hosts() -> tuple:
    if _OVERRIDES.get("host_file_hosts") is not None:
        return tuple(_OVERRIDES["host_file_hosts"])
    raw = os.environ.get(ENV_HOST_FILE_HOSTS, "")
    return expand_host_file_hosts(raw.split(","))


class _EnvSeatPinKeys:
    """Per-org seat-pin keys derived from ONE server-held master (``AIDOCS_APP_SEAT_PIN_KEY``, hex).

    [FLAGGED] S6 names the platform vault as production custody; no vault API
    for pin keys exists yet, so the master comes from the gate environment. An
    absent / short key raises, which the seat-pin service turns into
    ``app_internal`` (fail closed, nothing pinned)."""

    def pin_key(self, tenant_id: str) -> bytes:
        master = bytes.fromhex(os.environ.get(ENV_SEAT_PIN_KEY, "").strip())
        if len(master) < 32:
            raise ValueError("seat-pin master key unconfigured")
        return hmac.new(master, _SEAT_PIN_KDF_DOMAIN + tenant_id.encode("utf-8"), hashlib.sha256).digest()


class _CodenexusAdmins:
    """OWNER / ADMIN of the org per the CodeNexus membership (the gate's own seam)."""

    def is_org_admin(self, user_id: str, tenant_id: str) -> bool:
        from .. import outer_gate_transport as gate

        for org in gate._CODENEXUS_LIST_USER_ORGS(user_id) or []:
            if org.get("org_id") == tenant_id and str(org.get("org_role") or "").upper() in _ADMIN_ROLES:
                return True
        return False


def runtime_config() -> RuntimeConfig:
    """The process configuration (built once; :func:`reset` rebuilds it)."""
    global _CONFIG
    with _LOCK:
        if _CONFIG is not None:
            return _CONFIG
        o = _OVERRIDES
        policy = o.get("origin_policy") or PRODUCTION_ORIGIN_POLICY
        key_source = o.get("key_source") or VaultKeySource(
            http=o.get("vault_http"), base_url=o.get("vault_base_url"), secret=o.get("vault_bearer")
        )
        _CONFIG = RuntimeConfig(
            gate_root=_gate_root(),
            registry=o.get("registry") or registry_from_env(),
            identity=_identity(policy),
            host_file_hosts=_host_file_hosts(),
            key_source=key_source,
            seat_pin_keys=o.get("seat_pin_keys") or _EnvSeatPinKeys(),
            admins=o.get("admins") or _CodenexusAdmins(),
            origin_policy=policy,
            transport_options=dict(o.get("transport_options") or {}),
            host_file_options=dict(o.get("host_file_options") or {}),
            clock=o.get("clock") or _now,
            rate_limits_for=o.get("rate_limits_for"),
            grant_reader=o.get("grant_reader"),
        )
        return _CONFIG


# -- the transport + its control-call carrier ---------------------------------------------------------


class _AppTransport(ExternalAppTransport):
    """:class:`ExternalAppTransport` plus the AIDOCS -> application CONTROL call (wire §5).

    Same destination rules as a tool call (pinned origin + a bundle-declared
    relative endpoint, egress chokepoint, one vetted address, bounded exchange,
    3xx/5xx refused); only the assertion differs (``aidocs-control+jwt``)."""

    def control(self, *, tenant_id: str, application_origin: str, endpoint: str, body: bytes,
                claims: Mapping[str, Any]) -> tuple[int, Any]:
        from ..governed_egress import EgressRefused, assert_egress_allowed

        origin, url = self._destination(types.SimpleNamespace(application_origin=application_origin), endpoint)
        if type(body) is not bytes or len(body) > self._config.request_body_max_bytes:
            raise TransportRefused(APP_VALIDATION_FAILED, "control_body_invalid")
        try:
            token = self._signer.sign(tenant_id, jws.TYP_CONTROL, dict(claims))
        except (SignerUnavailable, SignerError):
            raise TransportRefused(APP_INTERNAL, "request_signer_unavailable") from None
        try:
            assert_egress_allowed(url, purpose=EGRESS_PURPOSE, allow_hosts=(origin.host,), audit=self._egress_audit)
        except EgressRefused:
            raise TransportRefused(APP_PACK_UNAVAILABLE, "egress_refused") from None
        address = self._address(origin)
        status, raw = self._exchange(origin, address, endpoint, body, token, mutation=False)
        try:
            parsed = parse_bounded_json(raw, self._config.response_limits)
        except ResponseRefused as exc:
            raise TransportRefused(exc.code, exc.reason, egress_attempted=True, possibly_delivered=True) from None
        return status, parsed

    def now(self) -> int:
        return self._clock()


def _control_claims(binding: BindingRecord, *, purpose: str, sub: str, body: bytes, now: int) -> dict:
    """Wire §5 claims from the binding the call is FOR (never request input)."""
    return {
        "iss": binding.trust.aidocs_issuer,
        "aud": binding.audience,
        "iat": now,
        "exp": now + _CONTROL_LIFETIME_S,
        "jti": "jti_" + secrets.token_urlsafe(24),
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "pack_id": binding.pack_id,
        "contract_digest": binding.contract_digest,
        "binding_version": binding.binding_version,
        "purpose": purpose,
        "sub": sub,
        "call_id": "call_" + secrets.token_urlsafe(16),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


class _EntitlementFetcher:
    """S9 ``EntitlementFetcher`` over HTTP: ``POST <entitlements_endpoint>`` ``{"sub"}`` (wire §6.1)."""

    def __init__(self, runtime: "AppRuntime") -> None:
        self._rt = runtime

    def fetch(self, binding: BindingRecord, sub: str) -> FetchResponse:
        if binding.trust is None:
            raise TransportRefused(APP_PACK_UNAVAILABLE, "binding_not_paired")
        endpoint = self._rt.bundle_for(binding).delegated.entitlements_endpoint
        body = canonicalize({"sub": sub})
        claims = _control_claims(binding, purpose=_PURPOSE_ENTITLEMENTS, sub=sub, body=body,
                                 now=self._rt.transport.now())
        status, parsed = self._rt.transport.control(
            tenant_id=binding.tenant_id, application_origin=binding.application_origin, endpoint=endpoint,
            body=body, claims=claims,
        )
        if not isinstance(parsed, dict):
            return FetchResponse()  # shape refused by the service (app_backend_invalid_response)
        if status == 200 and parsed.get("ok") is True and set(parsed) <= {"ok", "attestation", "expires_at"}:
            return FetchResponse(attestation=parsed.get("attestation"), expires_at=parsed.get("expires_at"))
        err = parsed.get("error")
        if parsed.get("ok") is False and isinstance(err, dict) and type(err.get("code")) is str:
            return FetchResponse(error_code=err["code"])  # only the declared crm_user_* map (S9)
        return FetchResponse()


class _ReceiptLookup:
    """S14 ``ReceiptLookup`` over HTTP: ``POST <internal_endpoints.receipt_lookup>`` (wire §10.3)."""

    def __init__(self, runtime: "AppRuntime") -> None:
        self._rt = runtime

    def lookup(self, record: Any) -> ReceiptLookupAnswer:
        bundle = self._rt.registry.get(record.pack_id, record.contract_digest).pack
        endpoint = bundle.receipt_lookup
        if not endpoint:
            raise TransportRefused(APP_PACK_UNAVAILABLE, "receipt_lookup_undeclared")
        body, claims = receipt_lookup_request(
            record, call_id="call_" + secrets.token_urlsafe(16), jti="jti_" + secrets.token_urlsafe(24),
            now=self._rt.transport.now(),
        )
        status, parsed = self._rt.transport.control(
            tenant_id=record.scope.tenant_id, application_origin=record.application_origin, endpoint=endpoint,
            body=body, claims=claims,
        )
        if status != 200 or not isinstance(parsed, dict) or parsed.get("ok") is not True:
            return ReceiptLookupAnswer(status="invalid")
        if parsed.get("status") == "receipt" and set(parsed) == {"ok", "status", "receipt"}:
            return ReceiptLookupAnswer(status="receipt", receipt=parsed.get("receipt"))
        if parsed.get("status") == "absent" and set(parsed) == {"ok", "status"}:
            return ReceiptLookupAnswer(status="absent")
        return ReceiptLookupAnswer(status="invalid")


class _Material:
    """S14 ``RetainedMaterial``: the stored binding + the registry bundle the RECORD pins."""

    def __init__(self, runtime: "AppRuntime") -> None:
        self._rt = runtime

    def material_for(self, record: Any) -> tuple[BindingRecord, Any]:
        binding = self._rt.bindings.get(record.binding_id)
        return binding, self._rt.registry.get(record.pack_id, record.contract_digest).pack


class _TenantControlPlane(ControlPlaneHandlers):
    """The S7 handlers over ONE org's authority DB: a body naming another org is refused."""

    def __init__(self, tenant_id: str, store: BindingStore, pairing: PairingService, upgrader: Any = None,
                 previewer: Any = None) -> None:
        super().__init__(store, pairing, upgrader=upgrader, previewer=previewer)
        self._tenant = tenant_id

    def create_binding(self, actor: Any, body: Any) -> dict:
        if isinstance(body, Mapping) and body.get("tenant_id") != self._tenant:
            return _error(LOCAL_REFUSAL_CODE)
        return super().create_binding(actor, body)


def _refused(code: str) -> CallOutcome:
    return CallOutcome("refused", code, platform_refusal_body(code))


# -- the per-org runtime ----------------------------------------------------------------------------


class AppRuntime:
    """One org's application authority: durable stores, services, S5 admission, write path."""

    def __init__(self, tenant_id: str, config: RuntimeConfig) -> None:
        if not _valid_tenant_id(tenant_id):
            raise RuntimeConfigError("invalid tenant id")
        self.tenant_id = tenant_id
        self.config = config
        self.registry = config.registry
        self.db = AppAuthorityDB(app_authority_db_path(config.gate_root, tenant_id))
        self.decision_sink = self.db.decision_sink()
        self.ledger = AppAuditLedger(app_audit_ledger_path(config.gate_root, tenant_id))
        self.key_directory = self.db.key_directory()
        self.signer = OrgRequestSigner(self.key_directory, config.key_source)
        self.bindings = BindingStore(self.registry, config.admins, self.decision_sink, backend=self.db.bindings(),
                                     origin_policy=config.origin_policy)
        self.pairing = PairingService(
            self.bindings, self.registry, OrgSignerPairingKeys(self.key_directory, self.signer), config.identity,
            self.decision_sink, origin_policy=config.origin_policy, state=self.db.pairing_state(),
        )
        self.control_plane = _TenantControlPlane(tenant_id, self.bindings, self.pairing,
                                                 upgrader=self.upgrade_contract,
                                                 previewer=self.preview_contract_upgrade)
        self.seat_pins = SeatPinService(self.db.seat_pins(), config.seat_pin_keys, config.admins, self.decision_sink,
                                        self.ledger)
        self.freezes = AppFreezeService(self.db.freezes(), config.admins, self.decision_sink)
        self.rate = AppRateLimiter(limits_for=config.rate_limits_for)
        self.pending = self.db.pending()
        self.links = self.db.links()
        self.transport = _AppTransport(self.signer, clock=config.clock, **dict(config.transport_options))
        self.entitlements = EntitlementService(
            bindings=self.bindings, terms_for=self._terms, fetcher=_EntitlementFetcher(self), clock=config.clock,
        )
        from .resolver import OutcomeResolver

        self.resolver = OutcomeResolver(
            store=self.pending, entitlements=self.entitlements, receipt_lookup=_ReceiptLookup(self),
            material=_Material(self), transport=self.transport, audit=self.ledger, clock=config.clock,
        )
        ingress = HostFileIngress(allowed_hosts=config.host_file_hosts, **dict(config.host_file_options))
        self.write_path = WritePath(store=self.pending, transport=self.transport, audit=self.ledger,
                                    resolver=self.resolver, clock=config.clock, host_file_ingress=ingress)

    def __repr__(self) -> str:
        return f"<AppRuntime {self.tenant_id}>"

    # -- the transport seams ------------------------------------------------------------------

    def active_binding(self, tenant_id: str, toolspace_id: str) -> BindingRecord:
        """R-a: the UNIQUE active binding; 0 / >= 2 / another org raise ``BindingRefused``."""
        if tenant_id != self.tenant_id:
            raise BindingRefused("binding_unavailable")
        return self.bindings.resolve_active(tenant_id, toolspace_id)

    def binding_is_active(self, binding: Any) -> bool:
        return (
            isinstance(binding, BindingRecord)
            and binding.tenant_id == self.tenant_id
            and binding.state is BindingState.ACTIVE
            and binding.trust is not None
        )

    def bundle_for(self, binding: BindingRecord) -> Any:
        """The pinned bundle ``(pack_id, contract_digest)``; unknown raises ``RegistryRefused``."""
        return self.registry.get(binding.pack_id, binding.contract_digest).pack

    def _terms(self, binding: BindingRecord) -> EntitlementTerms:
        return EntitlementTerms.from_bundle(self.bundle_for(binding))

    # -- #1132 in-place contract upgrade ------------------------------------------------------

    def _upgrade_fail(self, actor: Any, code: str, rec: Optional[BindingRecord] = None,
                      app_code: Optional[str] = None) -> ContractUpgradeFailed:
        self.bindings.audit_refusal(actor, self.tenant_id, rec.binding_id if rec else None, code)
        return ContractUpgradeFailed(code, app_code)

    def _upgrade_target(self, actor: Any, binding_id: str, to_digest: str) -> tuple[BindingRecord, Any]:
        """Every upgrade check that needs no application proof (the store re-checks them all)."""
        store = self.bindings

        def fail(code: str, rec: Optional[BindingRecord] = None):
            return self._upgrade_fail(actor, code, rec)

        try:
            rec = store.get(binding_id)
        except BindingRefused as exc:
            raise ContractUpgradeFailed(exc.code) from None
        if rec.tenant_id != self.tenant_id:
            raise fail("binding_unavailable")
        try:
            store.require_admin(actor, rec.tenant_id, act="upgrade")
        except BindingRefused as exc:
            raise ContractUpgradeFailed(exc.code) from None
        if rec.state is not BindingState.ACTIVE or rec.trust is None:
            raise fail("invalid_transition", rec)
        if to_digest == rec.contract_digest:
            raise fail("contract_unchanged", rec)
        if type(to_digest) is not str or not _HEX64_DIGEST.fullmatch(to_digest):
            raise fail("pack_unknown", rec)
        try:
            entry = self.registry.get(rec.pack_id, to_digest)
        except Exception:  # noqa: BLE001 -- not installed
            raise fail("pack_unknown", rec) from None
        if entry.sealed_v1 is not True:  # r0b2 (#1132 terminal): installed is not pinned
            raise fail("contract_unsealed", rec)
        pack = entry.pack
        if (
            pack.namespace != rec.namespace
            or getattr(pack, "audience_name", None) != rec.audience
            or pack.api_version != rec.api_version
        ):
            raise fail("contract_incompatible", rec)
        return rec, pack

    def preview_contract_upgrade(self, actor: Any, binding_id: str, to_digest: str) -> dict:
        """What an upgrade WOULD change (F -> T, the tool and mode diff); runs the
        local checks only and never contacts the application."""
        rec, pack = self._upgrade_target(actor, binding_id, to_digest)
        old = self.registry.get(rec.pack_id, rec.contract_digest).pack

        def modes(p: Any) -> set[str]:
            return {f"{tool}.{mode}" for tool, spec in p.tools.items() for mode in spec.modes}

        return {
            "binding_id": rec.binding_id,
            "binding_version": rec.binding_version,
            "from_digest": rec.contract_digest,
            "to_digest": pack.contract_digest,
            "tools_added": sorted(set(pack.tools) - set(old.tools)),
            "tools_removed": sorted(set(old.tools) - set(pack.tools)),
            "modes_added": sorted(modes(pack) - modes(old)),
            "modes_removed": sorted(modes(old) - modes(pack)),
        }

    def upgrade_contract(self, actor: Any, binding_id: str, to_digest: str) -> BindingRecord:
        """Move this org's ACTIVE binding to ``to_digest`` in place.

        Every check the store will make that does not need the application's
        proof runs FIRST (:meth:`_upgrade_target`), so a doomed attempt never
        reaches the application. Then one ``contract_upgrade`` control call (the
        CURRENT pin authenticates it; a fresh nonce), the strict ``{ok, proof}``
        answer, and :meth:`BindingStore.upgrade_contract`, which re-checks
        everything and verifies the proof. Raises :class:`ContractUpgradeFailed`.
        """
        store = self.bindings
        rec, _pack = self._upgrade_target(actor, binding_id, to_digest)

        def fail(code: str, rec: Optional[BindingRecord] = None, app_code: Optional[str] = None):
            return self._upgrade_fail(actor, code, rec, app_code)

        nonce = secrets.token_urlsafe(24)
        body = canonicalize({
            "binding_id": rec.binding_id, "from_digest": rec.contract_digest,
            "to_digest": to_digest, "nonce": nonce,
        })
        claims = _control_claims(rec, purpose=_PURPOSE_CONTRACT_UPGRADE, sub=actor.user_id, body=body,
                                 now=self.transport.now())
        try:
            status, parsed = self.transport.control(
                tenant_id=rec.tenant_id, application_origin=rec.application_origin,
                endpoint=CONTRACT_UPGRADE_ENDPOINT, body=body, claims=claims,
            )
        except TransportRefused:
            raise fail("upgrade_application_refused", rec) from None
        if not (
            status == 200
            and isinstance(parsed, dict)
            and set(parsed) == {"ok", "proof"}
            and parsed["ok"] is True
            and type(parsed["proof"]) is str
        ):
            app_code = None
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(err, dict) and type(err.get("code")) is str and _APP_ERROR_CODE.fullmatch(err["code"]):
                app_code = err["code"]
            raise fail("upgrade_application_refused", rec, app_code)
        try:
            return store.upgrade_contract(
                actor, rec.binding_id, from_digest=rec.contract_digest, to_digest=to_digest,
                expected_version=rec.binding_version,
                proof=parsed["proof"], nonce=nonce, now=self.transport.now(),
            )
        except BindingRefused as exc:  # the store already DECISION-audited it
            raise ContractUpgradeFailed(exc.code) from None

    # -- S5 admission -------------------------------------------------------------------------

    def call(self, *, conn: Any, binding: Any, bundle: Any, tool: Any, arguments: Any, meta: Any,
             principal: Any, host_file_provenance: str = HOST_FILE_PROVENANCE_GENERIC) -> CallOutcome:
        """Admit and run one application tool call. Never raises (see module doc).
        ``host_file_provenance`` is derived by the transport from the validated
        OAuth client (never request meta); it is frozen onto the admitted call."""
        try:
            return self._call(conn, binding, bundle, tool, arguments, meta, principal, host_file_provenance)
        except Exception:  # noqa: BLE001 -- the runtime owns outcome truth: a crash is a refusal
            return _refused(APP_INTERNAL)

    def _refuse(self, scope: AppScope, facts: Mapping[str, Any], code: str) -> CallOutcome:
        try:
            self.ledger.append(scope, "app_call_refused", "refused",
                               audit_metadata({**dict(facts), "refusal_code": code}))
        except Exception:  # noqa: BLE001 -- the refusal stands even if its row cannot be written
            pass
        return _refused(code)

    def _call(self, conn: Any, binding: Any, bundle: Any, tool: Any, arguments: Any, meta: Any,
              principal: Any, host_file_provenance: str = HOST_FILE_PROVENANCE_GENERIC) -> CallOutcome:
        # 1. this org, this binding (never another org's ledger or authority).
        if (
            type(conn) is not ConnectionContext
            or conn.tenant_id != self.tenant_id
            or not isinstance(binding, BindingRecord)
            or binding.tenant_id != self.tenant_id
            or binding.toolspace_id != conn.toolspace_id
            or (conn.binding_id is not None and conn.binding_id != binding.binding_id)
        ):
            return _refused(APP_PACK_UNAVAILABLE)
        scope = conn.app_scope
        facts: dict[str, Any] = {
            "tool": tool, "connector_id": conn.connector_id, "binding_id": binding.binding_id,
            "binding_version": binding.binding_version, "pack_id": binding.pack_id,
            "contract_digest": binding.contract_digest,
        }
        if (
            not self.binding_is_active(binding)
            or getattr(bundle, "pack_id", None) != binding.pack_id
            or getattr(bundle, "contract_digest", None) != binding.contract_digest
        ):
            return self._refuse(scope, facts, APP_PACK_UNAVAILABLE)
        # 2. the mode of that tool in the pinned bundle.
        tools = getattr(bundle, "tools", None)
        tspec = tools.get(tool) if isinstance(tools, Mapping) and type(tool) is str else None
        mode = arguments.get("mode") if isinstance(arguments, Mapping) else None
        mspec = tspec.modes.get(mode) if tspec is not None and type(mode) is str else None
        if mspec is None:
            return self._refuse(scope, facts, APP_VALIDATION_FAILED)
        facts["mode"] = mode
        # 3. seat pin (S6); the service writes its own DECISION row on a mismatch.
        try:
            self.seat_pins.check(conn, meta)
        except SeatHostMismatch as exc:
            return _refused(exc.code)
        except SeatPinUnavailable:
            return self._refuse(scope, facts, APP_INTERNAL)
        # 4. freeze (S11).
        try:
            self.freezes.check(conn)
        except AppScopeFrozen as exc:
            return self._refuse(scope, facts, exc.code)
        except FreezeCheckUnavailable:
            return self._refuse(scope, facts, APP_INTERNAL)
        # 5. rate / in-flight (S10): held for the whole call.
        try:
            permit = self.rate.admit(conn)
        except AppRateLimited as exc:
            out = self._refuse(scope, facts, APP_RATE_LIMITED)
            out.body["error"]["retry_after"] = exc.retry_after
            return out
        except AppRateAdmissionUnavailable:
            return self._refuse(scope, facts, APP_INTERNAL)
        try:
            return self._admitted(conn, scope, facts, binding, bundle, tool, mode, mspec, dict(arguments), meta,
                                  principal, host_file_provenance)
        finally:
            permit.release()

    def _admitted(self, conn: ConnectionContext, scope: AppScope, facts: dict, binding: BindingRecord, bundle: Any,
                  tool: str, mode: str, mspec: Any, args: dict, meta: Any, principal: Any,
                  host_file_provenance: str = HOST_FILE_PROVENANCE_GENERIC) -> CallOutcome:
        # 6. every remaining LOCAL precondition before any egress (§9). The RAW
        # arguments are the host's: a declared file param is judged against the
        # host file contract (the relay is checked against the pinned schema
        # by the transport, after ingress); every other member stays pinned.
        if not schema_accepts(host_input_schema(mspec.input_schema, getattr(mspec, "file_params", ()) or ()),
                              args):
            return self._refuse(scope, facts, APP_VALIDATION_FAILED)
        branch = _select_branch(mspec, args)
        if branch is None:
            return self._refuse(scope, facts, APP_VALIDATION_FAILED)
        hcr = derive_host_conversation_ref(principal=principal, meta=meta)
        if mspec.requires_conversation_context and hcr is None:
            return self._refuse(scope, facts, APP_CONVERSATION_CONTEXT_UNAVAILABLE)
        if mspec.platform_handler is not None and mspec.platform_handler != PLATFORM_HANDLER_MUTATION_RESOLVER:
            return self._refuse(scope, facts, APP_PACK_UNAVAILABLE)
        # 7. entitlement (S9) + the branch's capability envelope.
        try:
            entitlement = self.entitlements.lookup(self.tenant_id, binding.binding_id, conn.sub,
                                                   binding.contract_digest)
        except EntitlementRefused as exc:
            return self._refuse(scope, facts, exc.code)
        used = frozenset(branch.required_capabilities)
        if not used <= set(entitlement.capabilities_granted):
            return self._refuse(scope, facts, APP_CAPABILITY_REQUIRED)
        admitted = AdmittedCall(
            scope=scope, binding=binding, bundle=bundle, entitlement=entitlement, tool=tool, mode=mode,
            arguments=args, capabilities_used=used, call_id="call_" + secrets.token_urlsafe(16),
            connector_id=conn.connector_id, host_conversation_ref=hcr,
            host_file_provenance=admitted_host_file_provenance(getattr(mspec, "file_params", ()) or (),
                                                               host_file_provenance),
        )
        # 8. dispatch.
        if mspec.platform_handler == PLATFORM_HANDLER_MUTATION_RESOLVER:
            out = self.write_path.call_platform_mode(admitted)
        elif mspec.idempotency == "required":
            out = self.write_path.call_mutation(admitted)
        else:
            out = self._read(admitted, facts)
        if out.status == "application_error" and out.code in DECLARED_ERROR_MAP:
            try:  # §8.5: AIDOCS learned the seat is unlinked / disabled
                self.entitlements.note_identity_refused(self.tenant_id, binding.binding_id, conn.sub, out.code)
            except Exception:  # noqa: BLE001
                pass
        return out

    def _read(self, call: AdmittedCall, facts: Mapping[str, Any]) -> CallOutcome:
        """The read path: required pre-call row, one transport call, no idempotency key."""
        md = audit_metadata(dict(facts))
        try:
            self.ledger.append(call.scope, "app_call_admitted", "admitted", md)
        except Exception:  # noqa: BLE001 -- §16: no pre-call row, no egress (a read: app_internal)
            return _refused(APP_INTERNAL)
        try:
            result = self.transport.call(
                binding=call.binding, bundle=call.bundle, scope=call.scope, entitlement=call.entitlement,
                tool=call.tool, mode=call.mode, arguments=call.arguments, call_id=call.call_id,
                capabilities_used=call.capabilities_used, host_conversation_ref=call.host_conversation_ref,
            )
        except TransportRefused as exc:
            return self._refuse(call.scope, md, exc.code)
        except Exception:  # noqa: BLE001
            return self._refuse(call.scope, md, "app_backend_unavailable")
        output = result.output
        row = {**md, **audit_metadata({"input_digest": result.body_sha256, "response_digest": result.response_sha256})}
        if not output.ok:
            try:
                self.ledger.append(call.scope, "app_call_refused", "refused", row)
            except Exception:  # noqa: BLE001
                pass
            return CallOutcome("application_error", output.error_code, dict(output.body))
        ok = True
        try:
            self.ledger.append(call.scope, "app_read_completed", "completed", row)
        except Exception:  # noqa: BLE001 -- a post-fact row never changes a truthful answer
            ok = False
        return CallOutcome("answered", None, dict(output.body), audit_complete=ok)


# -- the process-wide entry points -------------------------------------------------------------------


def admitted_host_file_provenance(file_params: Any, provenance: Any) -> str:
    """The provenance an admitted call carries: CHATGPT_HOST_FILE only when the
    transport derived it AND the validated mode declares a host file param;
    GENERIC otherwise (r0b2 9cf90cf5)."""
    if provenance == HOST_FILE_PROVENANCE_CHATGPT and tuple(file_params or ()):
        return HOST_FILE_PROVENANCE_CHATGPT
    return HOST_FILE_PROVENANCE_GENERIC


def runtime_for_tenant(tenant_id: str) -> AppRuntime:
    """The ONE runtime of ``tenant_id`` in this process (thread-safe, cached)."""
    if not _valid_tenant_id(tenant_id):
        raise RuntimeConfigError("invalid tenant id")
    with _LOCK:
        runtime = _RUNTIMES.get(tenant_id)
        if runtime is None:
            runtime = AppRuntime(tenant_id, runtime_config())
            _RUNTIMES[tenant_id] = runtime
        return runtime


def control_plane_for_tenant(tenant_id: str) -> ControlPlaneHandlers:
    """The S7 control-plane handlers over the SAME durable stores the gate serves."""
    return runtime_for_tenant(tenant_id).control_plane


def pair_cli_handlers() -> ControlPlaneHandlers:
    """``pair_cli --factory aidocs_mcp.app_packs.runtime:pair_cli_handlers`` (org from ``AIDOCS_PAIR_CLI_TENANT``)."""
    tenant = os.environ.get(ENV_PAIR_CLI_TENANT, "").strip()
    if not tenant:
        raise RuntimeConfigError(f"set {ENV_PAIR_CLI_TENANT} to the org the pairing script acts on")
    return control_plane_for_tenant(tenant)


def provision_org_signer(tenant_id: str, kid: str) -> Optional[dict]:
    """Operator act: create the org's request key in CodeNexus custody under the
    AIDOCS-chosen ``kid`` and pin its public half as the org's first CURRENT key.
    Idempotent: an org that already has a current signer is left unchanged (None)."""
    from .signer import PURPOSE_ORG_REQUEST

    runtime = runtime_for_tenant(tenant_id)
    if runtime.key_directory.current(tenant_id, PURPOSE_ORG_REQUEST) is not None:
        return None
    vault = runtime.config.key_source
    if not isinstance(vault, VaultKeySource):
        raise RuntimeConfigError("org signer provisioning needs CodeNexus custody (VaultKeySource)")
    return provision_org_key(runtime.key_directory, vault, tenant_id, kid, bootstrap=True)


# -- the gate-wide link service (S8) -----------------------------------------------------------------


def _grant_reader() -> Any:
    reader = runtime_config().grant_reader
    if reader is not None:
        return reader
    from .toolspace_grant import reader_from_env

    return reader_from_env()


class GateBindingReader:
    """``LinkBindingReader`` across orgs: binding_id -> org via the CodeNexus grant
    table, then THAT org's authority DB. Everything fails closed as ``BindingRefused``."""

    def get(self, binding_id: str) -> BindingRecord:
        try:
            grant = _grant_reader().resolve_by_binding(binding_id)
            rec = runtime_for_tenant(grant.tenant_id).bindings.get(binding_id)
        except BindingRefused:
            raise
        except Exception:  # noqa: BLE001 -- config / store fault: never a binding
            raise BindingRefused("binding_unavailable") from None
        if rec.binding_id != binding_id or rec.tenant_id != grant.tenant_id or rec.toolspace_id != grant.toolspace_id:
            raise BindingRefused("binding_unknown")
        return rec


class CodenexusSeatDirectory:
    """``SeatDirectory``: the CodeNexus user id IS the seat subject (as ``ConnectionContext.sub``)."""

    def seat_sub(self, user_id: str, tenant_id: str) -> Optional[str]:
        from .. import outer_gate_transport as gate

        if type(user_id) is not str or not user_id:
            return None
        res = gate._CODENEXUS_RESOLVE_SEAT(user_id, tenant_id)
        return user_id if getattr(res, "ok", False) is True else None


class _TenantRoutedLinks:
    """``LinkStore`` routed to the DURABLE link rows of the link's own org."""

    def record_link(self, link: Any) -> None:
        runtime_for_tenant(link.tenant_id).links.record_link(link)

    def get(self, tenant_id: str, binding_id: str, sub: str) -> Any:
        return runtime_for_tenant(tenant_id).links.get(tenant_id, binding_id, sub)


class _TenantRoutedSink:
    """``ControlPlaneAuditSink`` routed to the DECISION sink of the event's org."""

    def emit(self, event: Any) -> None:
        runtime_for_tenant(getattr(event, "tenant_id", None)).decision_sink.emit(event)


def link_service() -> LinkService:
    """The ONE link service of this process (``/apps/link/authorize``, ``/v1/control/app-links/redeem``).

    Link rows are durable in each org's authority DB; confirm tickets, one-time
    codes and the jti replay set stay in memory (<= 10 min / 120 s / 75 s)."""
    global _LINK
    with _LOCK:
        if _LINK is None:
            _LINK = LinkService(
                GateBindingReader(), CodenexusSeatDirectory(), _TenantRoutedLinks(), _TenantRoutedSink(),
                origin_policy=runtime_config().origin_policy,
                # r0b2 B1: the linked DECISION and the link row share the org's
                # ONE authority-DB transaction (the routed sink and link store
                # write to that same file and join the ambient transaction).
                transaction=lambda tenant_id: runtime_for_tenant(tenant_id).db.transaction(),
            )
        return _LINK
