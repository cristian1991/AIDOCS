"""AIDOCS APPLICATION BINDING store -- the Connected Apps control plane (S7).

Terminology pin (build plan): the AIDOCS APPLICATION BINDING is the mandatory
control-plane record tenant/toolspace -> unique active binding -> origin /
audience / trust / contract (RFC 0003 v4.6.1 §3.5, §5). It is NOT the CRM
conversation bind and NOT a #1074 project/session binding.

Law:

* §3.5 M1 / §3.4 -- exactly one ACTIVE binding per ``(tenant_id,
  toolspace_id)``; :meth:`BindingStore.resolve_active` fails closed on zero or
  on more than one match.
* §3.3 -- namespace ownership and tool names are unique WITHIN ONE
  TOOLSPACE; a collision fails at BINDING ACTIVATION (never install-wide);
  reuse across tenants and toolspaces is allowed. ``ai_`` is reserved to the
  native toolspace, so a pack whose namespace is ``ai`` never activates.
* §5.1 -- ordinary control acts use the owning org's admin-dashboard
  credential. The platform-owned App-access orchestrator may use the distinct
  platform-admin S2S credential only when that credential is cryptographically
  bound to this exact target tenant and act. Connector / OAuth MCP credentials
  are refused by construction.
* §5.2 -- a binding pins ``pack_id``, ``api_version`` and ``contract_digest``
  from the S2 registry; ``audience`` is the pinned bundle's ``audience_name``
  (§6.1), never admin input.
* §5.6 -- a new binding goes live only after a completed pairing (§5.8).
* §5.8 -- the binding-control key and the entitlement keyset never share a
  physical key or a kid.
* §16.1 -- every control-plane transition, and every refusal of one, is a
  ``DECISION`` audit event, through a typed sink. The event is written BEFORE
  the state changes; if it cannot be written nothing changes (fail closed).
  The AppScope ledger is S12; :class:`ControlPlaneAuditSink` is the seam.

Persistence: behind :class:`BindingBackend`. Production is the durable
``AppAuthorityDB.bindings()`` backend (``authority_stores``; one SQLite file per
org under the tenant home, §22.1) paired with that SAME file's decision sink:
each transition's checks, DECISION row and state write are ONE ``BEGIN
IMMEDIATE`` transaction, so the row and the state commit together or not at
all. The default :class:`MemoryBindingBackend` is for tests; ``records=``
reloads saved state into it (and is how a corrupt 2-active state is exercised).
"""
from __future__ import annotations

import contextlib
import enum
import re
import secrets
import threading
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from . import jws
from .boundary_audit import (
    CONTROL_REFUSAL_CODES,
    KIND_STATUS,
    audit_metadata,
    is_platform_error_code,
    kind_status_admissible,
)
from .origin import PRODUCTION_ORIGIN_POLICY, OriginPolicy, OriginRefused, parse_origin, parse_url

__all__ = [
    "CREDENTIAL_ADMIN_DASHBOARD",
    "CREDENTIAL_BINDING_CONTROL",
    "CREDENTIAL_PAIRING_BOOTSTRAP",
    "CREDENTIAL_PLATFORM_ADMIN_S2S",
    "RESERVED_NATIVE_NAMESPACE",
    "RETENTION_DECISION",
    "AdminAuthority",
    "BindingBackend",
    "BindingRecord",
    "BindingRefused",
    "BindingState",
    "BindingStore",
    "BindingTrust",
    "ControlPlaneActor",
    "ControlPlaneAuditSink",
    "ControlPlaneEvent",
    "ListAuditSink",
    "MemoryBindingBackend",
    "OrgKeyFlipper",
    "PackLookup",
    "carry_sealed_generation",
]

CREDENTIAL_ADMIN_DASHBOARD = "admin_dashboard"
CREDENTIAL_PAIRING_BOOTSTRAP = "pairing_bootstrap"
CREDENTIAL_PLATFORM_ADMIN_S2S = "platform_admin_s2s"
# #1134 B4/B6: an application act proven by a VERIFIED binding-control-key
# assertion (the seal). Stamped only by binding_seal after verification.
CREDENTIAL_BINDING_CONTROL = "binding_control_assertion"
RESERVED_NATIVE_NAMESPACE = "ai"  # §3.3: ``ai_`` is reserved to the native toolspace
RETENTION_DECISION = "DECISION"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# WIRE-1134 §1: binding_generation = ``gen_`` + 32 random bytes base64url (opaque).
_GENERATION_RE = re.compile(r"^gen_[A-Za-z0-9_-]{43}$")
# The S7 control-plane audit kinds, exactly as registered in the closed
# boundary_audit.KIND_STATUS matrix (S12-B5/B6) and execution_event_retention.
CONTROL_PLANE_KINDS = frozenset(
    {
        "app_binding_created",
        "app_binding_trust_installed",
        "app_binding_activated",
        "app_binding_transitioned_disabled",
        "app_binding_contract_upgraded",
        "app_pairing_offered",
        "app_pairing_responded",
        "app_pairing_confirmed",
        "app_pairing_refused",
        "app_org_signer_flipped",
        "app_org_signer_provisioned",
        "app_control_plane_refused",
        "app_binding_authority_sealed",
        "app_generation_attested",
    }
)


def refusal_code_metadata(code: str) -> dict:
    """The ONE typed refusal-reason field of a refused row (ruling 7db9a488):
    a §16.3 platform code goes under ``refusal_code``; an AIDOCS control-plane
    reason (every S7 local code, plus the wire §1.5 ``app_pairing_invalid``)
    goes under ``control_refusal_code``, the closed ``CONTROL_REFUSAL_CODES``.
    Any other code is a programming error and raises -- never silently dropped."""
    if is_platform_error_code(code):
        return {"refusal_code": code}
    if type(code) is str and code in CONTROL_REFUSAL_CODES:
        return {"control_refusal_code": code}
    raise ValueError(f"refusal code {code!r} is in neither closed vocabulary")


def _binding_evidence(rec: "BindingRecord") -> dict:
    """Typed control-plane evidence for a binding row (ruling 7db9a488)."""
    md: dict = {"binding_version": rec.binding_version, "binding_state": rec.state.value}
    if rec.trust is not None:
        md["entitlement_keyset_version"] = rec.trust.entitlement_keyset_version
        md["entitlement_revocation_epoch"] = rec.trust.entitlement_revocation_epoch
    return md


class BindingRefused(Exception):
    """A binding act is refused. ``code`` is a stable LOCAL code for the handler
    layer; it is never a model-facing ``app_*`` code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BindingState(str, enum.Enum):
    PENDING_PAIRING = "pending_pairing"
    PAIRED = "paired"
    ACTIVE = "active"
    DISABLED = "disabled"
    # unbind (+ post-unbind recovery) is slice S19 and is not built here.


@dataclass(frozen=True)
class ControlPlaneActor:
    """Who performs a control-plane act.

    credential_class and the optional authority binding are set by the
    authenticated transport, never by a request body. authority_tenant_id /
    authority_act are empty for ordinary dashboard actors; for
    platform_admin_s2s they are mandatory and are part of the signed S2S
    assertion.
    """

    user_id: str
    credential_class: str
    authority_tenant_id: str = ""
    authority_act: str = ""


class AdminAuthority(Protocol):
    """Server-side: is ``user_id`` an OWNER/ADMIN of org ``tenant_id``?"""

    def is_org_admin(self, user_id: str, tenant_id: str) -> bool: ...


class PackLookup(Protocol):
    """The one S2 registry read the store needs (fails closed on unknown)."""

    def get(self, pack_id: str, contract_digest: str) -> Any: ...


@dataclass(frozen=True)
class ControlPlaneEvent:
    """A §16.1 DECISION-class control-plane audit event.

    ``kind`` must be a registered S7 kind; ``status`` is NOT caller input -- it
    is the matrix value ``KIND_STATUS[kind]`` (``completed``, or ``refused`` for
    the ``*_refused`` kinds) and is also written into the metadata. Every
    metadata entry must be allowlisted AND well-shaped (``boundary_audit``);
    anything else is a programming error and raises, never silently dropped.
    """

    kind: str
    tenant_id: str
    actor_user_id: str
    binding_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    retention: str = RETENTION_DECISION
    status: str = field(init=False)

    def __post_init__(self) -> None:
        if self.retention != RETENTION_DECISION:
            raise ValueError("control-plane events are DECISION-class")
        if self.kind not in CONTROL_PLANE_KINDS or self.kind not in KIND_STATUS:
            raise ValueError(f"unregistered control-plane audit kind {self.kind!r}")
        status = KIND_STATUS[self.kind]
        if "status" in self.metadata:
            raise ValueError("status is derived from the kind, never supplied")
        md = {**dict(self.metadata), "status": status}
        if audit_metadata(md) != md:
            raise ValueError(f"metadata outside the audit allowlist or shape: {sorted(md)}")
        if not kind_status_admissible(self.kind, status):  # pragma: no cover -- matrix self-check
            raise ValueError("kind/status pair is off the closed matrix")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metadata", MappingProxyType(md))


class ControlPlaneAuditSink(Protocol):
    def emit(self, event: ControlPlaneEvent) -> None: ...


class ListAuditSink:
    """In-memory sink (tests and the pre-S12 default)."""

    def __init__(self) -> None:
        self.events: list[ControlPlaneEvent] = []

    def emit(self, event: ControlPlaneEvent) -> None:
        self.events.append(event)


def _jwk_identity(jwk: Mapping[str, Any]) -> tuple[str, str]:
    try:
        thumb = jws.jwk_thumbprint(jwk)
    except jws.JwsError as exc:
        raise BindingRefused("key_invalid") from exc
    kid = jwk.get("kid")
    if type(kid) is not str or not kid:
        raise BindingRefused("key_invalid")
    return kid, thumb


@dataclass(frozen=True)
class BindingTrust:
    """The paired trust relation of one binding (RFC §5.3, §5.8; wire §3.2).

    Public material only: AIDOCS persists only the application's public keys.
    """

    aidocs_issuer: str
    aidocs_origin: str
    aidocs_kid: str
    aidocs_thumbprint: str
    link_return_uri: str
    binding_control_key: Mapping[str, Any]
    entitlement_keys: tuple[Mapping[str, Any], ...]
    entitlement_keyset_version: int
    entitlement_revocation_epoch: int
    transcript_sha256: str
    # #1134 B4/B6 (WIRE-1134 §9a): the AIDOCS-sealed opaque generation and the
    # authority digest over it. Both None until the one-time seal (or a v2
    # pairing); both set together. Stored trust loads via ``**d``, so a row
    # written before the seal simply has neither.
    binding_generation: Optional[str] = None
    authority_sha256: Optional[str] = None

    def __post_init__(self) -> None:
        if (self.binding_generation is None) != (self.authority_sha256 is None):
            raise BindingRefused("trust_invalid")
        if self.binding_generation is not None and (
            type(self.binding_generation) is not str or not _GENERATION_RE.match(self.binding_generation)
            or type(self.authority_sha256) is not str or not _HEX64.match(self.authority_sha256)
        ):
            raise BindingRefused("trust_invalid")
        if not isinstance(self.entitlement_keys, tuple) or not self.entitlement_keys:
            raise BindingRefused("entitlement_keyset_invalid")
        bc_kid, bc_thumb = _jwk_identity(self.binding_control_key)
        ent_kids: set[str] = set()
        ent_thumbs: set[str] = set()
        for jwk in self.entitlement_keys:
            kid, thumb = _jwk_identity(jwk)
            if kid in ent_kids or thumb in ent_thumbs:
                raise BindingRefused("entitlement_keyset_invalid")
            ent_kids.add(kid)
            ent_thumbs.add(thumb)
        # §5.8 / §7.3: no physical key and no kid shared between the two purposes.
        if bc_kid in ent_kids or bc_thumb in ent_thumbs:
            raise BindingRefused("key_reuse")
        for name in ("entitlement_keyset_version", "entitlement_revocation_epoch"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise BindingRefused("trust_invalid")
        if self.entitlement_keyset_version < 1:
            raise BindingRefused("trust_invalid")
        if type(self.transcript_sha256) is not str or not _HEX64.match(self.transcript_sha256):
            raise BindingRefused("trust_invalid")
        object.__setattr__(self, "binding_control_key", MappingProxyType(dict(self.binding_control_key)))
        object.__setattr__(
            self, "entitlement_keys", tuple(MappingProxyType(dict(k)) for k in self.entitlement_keys)
        )

    @property
    def binding_control_keyset(self) -> dict:
        """The ONE keyset for application -> AIDOCS objects (wire §1.3)."""
        return {self.binding_control_key["kid"]: jws.public_key_from_jwk(self.binding_control_key)}

    @property
    def entitlement_keyset(self) -> dict:
        """The ONE keyset for entitlement attestations (wire §1.3)."""
        return {k["kid"]: jws.public_key_from_jwk(k) for k in self.entitlement_keys}


def carry_sealed_generation(prior: Optional[BindingTrust], new: BindingTrust) -> BindingTrust:
    """WIRE-1134 §9a "key rotation never reseals": a trust re-install that
    carries no generation of its own (an org-key or binding-control re-key)
    keeps the prior sealed generation + authority digest. A new trust that
    carries its own generation (a v2 re-pair = a new generation) wins."""
    if prior is None or prior.binding_generation is None or new.binding_generation is not None:
        return new
    return replace(new, binding_generation=prior.binding_generation, authority_sha256=prior.authority_sha256)


@dataclass(frozen=True)
class BindingRecord:
    binding_id: str
    tenant_id: str
    toolspace_id: str
    pack_id: str
    contract_digest: str
    api_version: str
    namespace: str
    tool_names: frozenset
    application_origin: str
    audience: str
    state: BindingState
    binding_version: int
    trust: Optional[BindingTrust] = None
    # Every AIDOCS org request kid this binding's application was provisioned
    # with by a completed ceremony (§5.8 rotation: the application trusts old +
    # new). The flip precondition reads it; retirement pruning is not built here.
    provisioned_aidocs_kids: frozenset = frozenset()


class OrgKeyFlipper(Protocol):
    """The narrow S3 keyset surface the guarded flip needs (``OrgRequestKeyset``)."""

    def key_state(self, org_id: str, kid: str, *, now: int) -> str: ...

    def flip(self, org_id: str, *, now: int) -> None: ...


def _entitlement_identities(trust: BindingTrust) -> frozenset:
    """Each entitlement key as (kid, RFC 7638 thumbprint): a same-kid new key is a replacement."""
    return frozenset((k["kid"], jws.jwk_thumbprint(k)) for k in trust.entitlement_keys)


def _rotation_counter_refusal(prior: BindingTrust, new: BindingTrust) -> Optional[str]:
    """Wire §4 (RFC §5.8, §7.4) counters across a re-pair. None = allowed.

    * neither counter ever decreases;
    * an ADDED entitlement key => ``entitlement_keyset_version`` STRICTLY increases;
    * a REMOVED (distrusted) key => ``entitlement_revocation_epoch`` STRICTLY
      increases, which invalidates every cached attestation of the binding;
    * a replacement is both; an unchanged keyset (e.g. an AIDOCS org-key or a
      binding-control-key re-pair) may keep both counters.
    """
    if (
        new.entitlement_keyset_version < prior.entitlement_keyset_version
        or new.entitlement_revocation_epoch < prior.entitlement_revocation_epoch
    ):
        return "epoch_regressed"
    before, after = _entitlement_identities(prior), _entitlement_identities(new)
    if after - before and not new.entitlement_keyset_version > prior.entitlement_keyset_version:
        return "keyset_version_not_bumped"
    if before - after and not new.entitlement_revocation_epoch > prior.entitlement_revocation_epoch:
        return "revocation_epoch_not_bumped"
    return None


def _text(value: Any, code: str = "invalid") -> str:
    if type(value) is not str or not _ID_RE.match(value):
        raise BindingRefused(code)
    return value


class BindingBackend(Protocol):
    """Where binding rows live. ``transaction()`` is the ONE transaction a
    transition's checks, DECISION row and write share (a no-op in memory)."""

    def transaction(self) -> Any: ...

    def get(self, binding_id: str) -> Optional[BindingRecord]: ...

    def all(self) -> Iterable[BindingRecord]: ...

    def insert(self, rec: BindingRecord) -> None: ...

    def replace(self, new: BindingRecord, *, expected_version: int) -> None: ...


class MemoryBindingBackend:
    """In-process rows (tests). ``records`` reloads saved state -- including a
    corrupt 2-active state, which the durable backend's index makes impossible."""

    def __init__(self, records: Iterable[BindingRecord] = ()) -> None:
        self._records: dict[str, BindingRecord] = {}
        for rec in records:
            if not isinstance(rec, BindingRecord) or rec.binding_id in self._records:
                raise ValueError("records must be distinct BindingRecord rows")
            self._records[rec.binding_id] = rec

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    def get(self, binding_id: str) -> Optional[BindingRecord]:
        return self._records.get(binding_id)

    def all(self) -> tuple[BindingRecord, ...]:
        return tuple(self._records.values())

    def insert(self, rec: BindingRecord) -> None:
        if rec.binding_id in self._records:
            raise BindingRefused("binding_id_collision")
        self._records[rec.binding_id] = rec

    def replace(self, new: BindingRecord, *, expected_version: int) -> None:
        cur = self._records.get(new.binding_id)
        if cur is None or cur.binding_version != expected_version:
            raise BindingRefused("version_conflict")
        self._records[new.binding_id] = new


class BindingStore:
    """Authority for AIDOCS application bindings (see module doc)."""

    def __init__(
        self,
        packs: PackLookup,
        admin_authority: AdminAuthority,
        audit_sink: ControlPlaneAuditSink,
        *,
        records: Iterable[BindingRecord] = (),
        id_factory: Optional[Callable[[], str]] = None,
        origin_policy: OriginPolicy = PRODUCTION_ORIGIN_POLICY,
        backend: Optional["BindingBackend"] = None,
        upgrade_jtis: Any = None,
    ) -> None:
        self._packs = packs
        self._admins = admin_authority
        self._sink = audit_sink
        self._ids = id_factory or (lambda: "bnd_" + secrets.token_urlsafe(18))
        self._origin_policy = origin_policy
        if backend is None:
            backend = MemoryBindingBackend(records)
        else:
            if tuple(records):
                raise ValueError("records= seeds the in-memory backend only")
            check = getattr(backend, "check_sink", None)
            if callable(check):
                check(audit_sink)  # ruling 2: a durable backend audits on its own file
        self._backend = backend
        self._lock = threading.RLock()
        if upgrade_jtis is None:
            from .contract_upgrade import JtiCache

            upgrade_jtis = JtiCache()
        self._upgrade_jtis = upgrade_jtis

    # -- reads ----------------------------------------------------------------

    def get(self, binding_id: str) -> BindingRecord:
        with self._lock:
            rec = self._backend.get(binding_id)
        if rec is None:
            raise BindingRefused("binding_unknown")
        return rec

    def records(self) -> tuple[BindingRecord, ...]:
        with self._lock:
            return tuple(self._backend.all())

    def resolve_active(self, tenant_id: str, toolspace_id: str) -> BindingRecord:
        """§3.5 M1: the UNIQUE active binding; zero or 2+ fails closed."""
        hits = [
            r for r in self.records()
            if r.tenant_id == tenant_id and r.toolspace_id == toolspace_id and r.state is BindingState.ACTIVE
        ]
        if not hits:
            raise BindingRefused("binding_unavailable")
        if len(hits) > 1:
            raise BindingRefused("binding_ambiguous")
        return hits[0]

    def _transaction(self):
        """The backend's ONE transaction (durable) or a no-op (in-memory)."""
        return self._backend.transaction()

    @contextlib.contextmanager
    def locked_transaction(self):
        """The store lock + the backend's ONE transaction, for a service that
        must put its own durable writes (on the SAME authority file) into the
        same commit as a binding transition (#1134 seal)."""
        with self._lock, self._transaction():
            yield

    # -- authority + audit ------------------------------------------------------

    def require_admin(self, actor: Any, tenant_id: str, *, act: str) -> None:
        """§5.1 admin-only; refusals are DECISION-audited (best effort)."""
        ok = False
        if isinstance(actor, ControlPlaneActor):
            if actor.credential_class == CREDENTIAL_ADMIN_DASHBOARD:
                try:
                    ok = self._admins.is_org_admin(actor.user_id, tenant_id) is True
                except Exception:  # noqa: BLE001 -- authority failure fails closed
                    ok = False
            elif actor.credential_class == CREDENTIAL_PLATFORM_ADMIN_S2S:
                # This is an object-capability stamped only after the transport
                # verifies the short-lived platform-control assertion. Both
                # target and act are re-checked here at the state authority.
                ok = actor.authority_tenant_id == tenant_id and actor.authority_act == act
        if not ok:
            self.audit_refusal(actor, tenant_id, None, "forbidden")
            raise BindingRefused("forbidden")

    def audit_refusal(self, actor: Any, tenant_id: str, binding_id: Optional[str], code: str,
                      kind: str = "app_control_plane_refused", **meta: Any) -> None:
        user = actor.user_id if isinstance(actor, ControlPlaneActor) else "<unknown>"
        # Built OUTSIDE the sink guard: an unknown code or unshaped evidence raises loudly.
        event = ControlPlaneEvent(kind, str(tenant_id), user, binding_id, {**meta, **refusal_code_metadata(code)})
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 -- the refusal stands even if its audit write fails
            pass

    def _emit(self, kind: str, actor: ControlPlaneActor, rec: BindingRecord, **meta: Any) -> None:
        event = ControlPlaneEvent(
            kind,
            rec.tenant_id,
            actor.user_id,
            rec.binding_id,
            {
                "toolspace_id": rec.toolspace_id,
                "pack_id": rec.pack_id,
                "contract_digest": rec.contract_digest,
                **_binding_evidence(rec),
                **meta,
            },
        )
        try:
            self._sink.emit(event)
        except Exception as exc:  # noqa: BLE001 -- no audit, no transition
            raise BindingRefused("audit_unavailable") from exc

    def _refuse(self, actor: Any, rec: BindingRecord, code: str) -> BindingRefused:
        self.audit_refusal(
            actor, rec.tenant_id, rec.binding_id, code, toolspace_id=rec.toolspace_id, **_binding_evidence(rec)
        )
        return BindingRefused(code)

    def _commit(self, kind: str, actor: ControlPlaneActor, new: BindingRecord, *,
                prior: Optional[BindingRecord] = None, **meta: Any) -> BindingRecord:
        """The DECISION row, then the state write -- in the caller's ONE transaction
        (durable backend): both commit or neither does. A storage refusal
        (version moved, a second ACTIVE) rolls the row back and is itself audited."""
        self._emit(kind, actor, new, **meta)  # write-ahead; raises -> nothing changed
        try:
            if prior is None:
                self._backend.insert(new)
            else:
                self._backend.replace(new, expected_version=prior.binding_version)
        except BindingRefused as exc:
            raise self._refuse(actor, prior or new, exc.code) from None
        except Exception:  # noqa: BLE001 -- storage failure: the transaction rolls back
            raise self._refuse(actor, prior or new, "control_plane_refused") from None
        return new

    def _pack(self, pack_id: str, contract_digest: str) -> Any:
        try:
            entry = self._packs.get(pack_id, contract_digest)
        except Exception as exc:  # noqa: BLE001 -- unknown pack/digest fails closed
            raise BindingRefused("pack_unknown") from exc
        pack = getattr(entry, "pack", None)
        if pack is None or pack.pack_id != pack_id or pack.contract_digest != contract_digest:
            raise BindingRefused("pack_unknown")
        return pack

    # -- transitions (§5.1) -------------------------------------------------------

    def create_binding(
        self,
        actor: ControlPlaneActor,
        *,
        tenant_id: str,
        pack_id: str,
        contract_digest: str,
        application_origin: str,
    ) -> BindingRecord:
        """bind: explicitly create a NEW pending binding (manual/admin semantics)."""
        return self._create_binding(
            actor,
            tenant_id=tenant_id,
            pack_id=pack_id,
            contract_digest=contract_digest,
            application_origin=application_origin,
            reuse_exact=False,
        )

    def ensure_binding(
        self,
        actor: ControlPlaneActor,
        *,
        tenant_id: str,
        pack_id: str,
        contract_digest: str,
        application_origin: str,
    ) -> BindingRecord:
        """S18 bind: atomically reuse one exact live binding or create it once.

        This method is intentionally platform-S2S-only. It exists for
        lost-response/retry safety and does NOT alter the dashboard/manual
        create contract. Disabled rows are not silently resurrected: a fresh
        grant after revocation gets a fresh binding.
        """
        tenant = _text(tenant_id)
        if not (
            isinstance(actor, ControlPlaneActor)
            and actor.credential_class == CREDENTIAL_PLATFORM_ADMIN_S2S
        ):
            self.audit_refusal(actor, tenant, None, "forbidden")
            raise BindingRefused("forbidden")
        return self._create_binding(
            actor,
            tenant_id=tenant,
            pack_id=pack_id,
            contract_digest=contract_digest,
            application_origin=application_origin,
            reuse_exact=True,
        )

    def _create_binding(
        self,
        actor: ControlPlaneActor,
        *,
        tenant_id: str,
        pack_id: str,
        contract_digest: str,
        application_origin: str,
        reuse_exact: bool,
    ) -> BindingRecord:
        """Validate registry-derived binding facts, then create/reuse atomically.

        There is no admin-selected toolspace: §3.6 makes toolspace_id the same
        value the pack namespace is admitted under. Audience and all tool facts
        are likewise copied only from the validated PackBundle.
        """
        tenant_id = _text(tenant_id)
        self.require_admin(actor, tenant_id, act="bind")
        try:
            if type(contract_digest) is not str or not _HEX64.match(contract_digest):
                raise BindingRefused("pack_unknown")
            pack = self._pack(_text(pack_id, "pack_unknown"), contract_digest)
            toolspace_id = _text(getattr(pack, "namespace", None), "pack_unknown")
            audience = getattr(pack, "audience_name", None)
            if type(audience) is not str or not audience:
                raise BindingRefused("pack_unknown")
            try:
                origin = parse_origin(application_origin, policy=self._origin_policy).serialized
            except OriginRefused as exc:
                raise BindingRefused("origin_invalid") from exc
        except BindingRefused as exc:
            self.audit_refusal(actor, tenant_id, None, exc.code)
            raise

        with self._lock, self._transaction():
            if reuse_exact:
                exact = [
                    rec
                    for rec in self._backend.all()
                    if rec.tenant_id == tenant_id
                    and rec.toolspace_id == toolspace_id
                    and rec.pack_id == pack.pack_id
                    and rec.contract_digest == pack.contract_digest
                    and rec.application_origin == origin
                    and rec.state is not BindingState.DISABLED
                ]
                if len(exact) > 1:
                    self.audit_refusal(
                        actor,
                        tenant_id,
                        None,
                        "binding_ambiguous",
                        toolspace_id=toolspace_id,
                    )
                    raise BindingRefused("binding_ambiguous")
                if exact:
                    return exact[0]

            binding_id = self._ids()
            if self._backend.get(binding_id) is not None:
                raise BindingRefused("binding_id_collision")
            rec = BindingRecord(
                binding_id=binding_id,
                tenant_id=tenant_id,
                toolspace_id=toolspace_id,
                pack_id=pack.pack_id,
                contract_digest=pack.contract_digest,
                api_version=pack.api_version,
                namespace=pack.namespace,
                tool_names=frozenset(pack.tools),
                application_origin=origin,
                audience=audience,
                state=BindingState.PENDING_PAIRING,
                binding_version=1,
            )
            return self._commit("app_binding_created", actor, rec)

    def install_trust(
        self, actor: ControlPlaneActor, binding_id: str, trust: BindingTrust, *, expected_version: int
    ) -> BindingRecord:
        """Record the trust a completed pairing proved (§5.8). Called by the
        pairing service on confirm; also the re-key / rotate path for an active
        binding (the state is kept)."""
        rec = self.get(binding_id)
        self.require_admin(actor, rec.tenant_id, act="pair")
        if not isinstance(trust, BindingTrust):
            raise self._refuse(actor, rec, "trust_invalid")
        with self._lock, self._transaction():
            rec = self.get(binding_id)
            if rec.binding_version != expected_version:
                raise self._refuse(actor, rec, "version_conflict")
            if rec.state is BindingState.DISABLED:
                raise self._refuse(actor, rec, "invalid_transition")
            try:
                link_origin, _ = parse_url(trust.link_return_uri, policy=self._origin_policy)
                app_origin = parse_origin(rec.application_origin, policy=self._origin_policy)
            except OriginRefused:
                raise self._refuse(actor, rec, "origin_invalid") from None
            if link_origin != app_origin:
                raise self._refuse(actor, rec, "origin_invalid")
            trust = carry_sealed_generation(rec.trust, trust)
            if rec.trust is not None:
                code = _rotation_counter_refusal(rec.trust, trust)
                if code is not None:
                    raise self._refuse(actor, rec, code)
            state = BindingState.ACTIVE if rec.state is BindingState.ACTIVE else BindingState.PAIRED
            new = replace(
                rec,
                trust=trust,
                state=state,
                binding_version=rec.binding_version + 1,
                provisioned_aidocs_kids=rec.provisioned_aidocs_kids | {trust.aidocs_kid},
            )
            return self._commit("app_binding_trust_installed", actor, new, prior=rec)  # counters via _binding_evidence

    def activate(self, actor: ControlPlaneActor, binding_id: str) -> BindingRecord:
        """Manual/admin activate: a real transition, never an idempotent shortcut."""
        return self._activate(actor, binding_id, idempotent=False, allow_disabled=True)

    def ensure_active(self, actor: ControlPlaneActor, binding_id: str) -> BindingRecord:
        """S18 grant completion: platform-only and retry-safe."""
        rec = self.get(binding_id)
        if not (
            isinstance(actor, ControlPlaneActor)
            and actor.credential_class == CREDENTIAL_PLATFORM_ADMIN_S2S
        ):
            self.audit_refusal(actor, rec.tenant_id, rec.binding_id, "forbidden")
            raise BindingRefused("forbidden")
        return self._activate(actor, binding_id, idempotent=True, allow_disabled=False)

    def _activate(
        self,
        actor: ControlPlaneActor,
        binding_id: str,
        *,
        idempotent: bool,
        allow_disabled: bool,
    ) -> BindingRecord:
        rec = self.get(binding_id)
        self.require_admin(actor, rec.tenant_id, act="activate")
        with self._lock, self._transaction():
            rec = self.get(binding_id)
            if rec.trust is None:
                raise self._refuse(actor, rec, "not_paired")
            allowed = {BindingState.PAIRED, BindingState.ACTIVE}
            if allow_disabled:
                allowed.add(BindingState.DISABLED)
            if rec.state not in allowed:
                raise self._refuse(actor, rec, "invalid_transition")
            if rec.state is BindingState.ACTIVE and not idempotent:
                raise self._refuse(actor, rec, "invalid_transition")
            try:
                self._pack(rec.pack_id, rec.contract_digest)
            except BindingRefused:
                raise self._refuse(actor, rec, "pack_unknown") from None
            if rec.namespace == RESERVED_NATIVE_NAMESPACE or any(
                name.startswith(RESERVED_NATIVE_NAMESPACE + "_") for name in rec.tool_names
            ):
                raise self._refuse(actor, rec, "namespace_reserved")
            peers = [
                r for r in self._backend.all()
                if r.binding_id != rec.binding_id
                and r.tenant_id == rec.tenant_id
                and r.toolspace_id == rec.toolspace_id
                and r.state is BindingState.ACTIVE
            ]
            if any(p.namespace == rec.namespace for p in peers):
                raise self._refuse(actor, rec, "namespace_collision")
            if any(p.tool_names & rec.tool_names for p in peers):
                raise self._refuse(actor, rec, "tool_name_collision")
            if peers:
                raise self._refuse(actor, rec, "active_binding_exists")
            if rec.state is BindingState.ACTIVE:
                return rec
            new = replace(rec, state=BindingState.ACTIVE, binding_version=rec.binding_version + 1)
            return self._commit("app_binding_activated", actor, new, prior=rec)

    # -- §5.8 org signer flip precondition ------------------------------------------

    def flip_blockers(self, tenant_id: str, new_kid: str) -> tuple[str, ...]:
        """ACTIVE bindings of the org NOT yet provisioned with ``new_kid``.

        §5.8 step 3: flip only when every active binding is provisioned (or
        explicitly disabled). Empty tuple = the precondition holds.
        """
        with self._lock:
            return tuple(
                sorted(
                    r.binding_id for r in self._backend.all()
                    if r.tenant_id == tenant_id
                    and r.state is BindingState.ACTIVE
                    and new_kid not in r.provisioned_aidocs_kids
                )
            )

    def flip_org_signer(
        self, actor: ControlPlaneActor, keyset: OrgKeyFlipper, tenant_id: str, new_kid: str, *, now: int
    ) -> None:
        """The guarded routine flip: under the store lock (so no binding can be
        activated or re-keyed in between), refuse unless ``new_kid`` is the org's
        NEXT key and no ACTIVE binding lacks it; then flip the S3 keyset.

        Durable: with the SQLite backend and the SQLite key directory of the SAME
        authority DB, the guard reads, the DECISION row and the directory flip
        are ONE transaction (a crash leaves either all or none).
        """
        tenant_id = _text(tenant_id)
        self.require_admin(actor, tenant_id, act="rotate")
        with self._lock, self._transaction():
            try:
                state = keyset.key_state(tenant_id, new_kid, now=now)
            except Exception:  # noqa: BLE001 -- unknown kid / keyset fails closed
                state = None
            if state != "next":
                self.audit_refusal(actor, tenant_id, None, "flip_no_next_key")
                raise BindingRefused("flip_no_next_key")
            if self.flip_blockers(tenant_id, new_kid):
                self.audit_refusal(actor, tenant_id, None, "flip_bindings_unprovisioned")
                raise BindingRefused("flip_bindings_unprovisioned")
            try:
                self._sink.emit(
                    ControlPlaneEvent("app_org_signer_flipped", tenant_id, actor.user_id, None, {})
                )
            except Exception as exc:  # noqa: BLE001 -- no audit, no flip
                raise BindingRefused("audit_unavailable") from exc
            try:
                keyset.flip(tenant_id, now=now)
            except Exception:  # noqa: BLE001 -- the transaction (row + flip) rolls back
                self.audit_refusal(actor, tenant_id, None, "control_plane_refused")
                raise BindingRefused("control_plane_refused") from None

    def upgrade_contract(
        self,
        actor: ControlPlaneActor,
        binding_id: str,
        *,
        from_digest: str,
        to_digest: str,
        expected_version: int,
        proof: Any,
        nonce: str,
        now: int,
    ) -> BindingRecord:
        """In-place contract upgrade (operator 2026-10-01): an ACTIVE binding
        moves F -> T keeping its id, pairing trust, seats and connector.

        Requires an org admin; ``expected_version``; T installed, different, and
        keeping the pack identity (pack_id / namespace / audience; the native
        namespace stays reserved); and the application's owner-approval proof
        (:func:`~.contract_upgrade.verify_upgrade_proof`). One transaction:
        DECISION row + state write, or nothing.
        """
        from .contract_upgrade import ContractUpgradeRefused, verify_upgrade_proof

        rec = self.get(binding_id)
        self.require_admin(actor, rec.tenant_id, act="upgrade")
        with self._lock, self._transaction():
            rec = self.get(binding_id)
            if rec.binding_version != expected_version or rec.contract_digest != from_digest:
                raise self._refuse(actor, rec, "version_conflict")
            if rec.state is not BindingState.ACTIVE:
                raise self._refuse(actor, rec, "invalid_transition")
            if to_digest == rec.contract_digest:
                raise self._refuse(actor, rec, "contract_unchanged")
            try:
                if type(to_digest) is not str or not _HEX64.match(to_digest):
                    raise BindingRefused("pack_unknown")
                pack = self._pack(rec.pack_id, to_digest)
            except BindingRefused:
                raise self._refuse(actor, rec, "pack_unknown") from None
            # r0b2 (#1132 terminal): installed is NOT enough -- the exact
            # (pack_id, T) entry must be operator-sealed (pinned) v1.
            try:
                sealed = self._packs.get(rec.pack_id, to_digest).sealed_v1 is True
            except Exception:  # noqa: BLE001 -- unknown fails closed
                sealed = False
            if not sealed:
                raise self._refuse(actor, rec, "contract_unsealed")
            tool_names = frozenset(pack.tools)
            if (
                pack.namespace != rec.namespace
                or getattr(pack, "audience_name", None) != rec.audience
                # r0b2 (#1132): first cut = no api_version drift, until a
                # compatibility rule is deliberately defined.
                or pack.api_version != rec.api_version
                or pack.namespace == RESERVED_NATIVE_NAMESPACE
                or any(name.startswith(RESERVED_NATIVE_NAMESPACE + "_") for name in tool_names)
            ):
                raise self._refuse(actor, rec, "contract_incompatible")
            try:
                claims = verify_upgrade_proof(
                    proof, binding=rec, to_digest=to_digest, nonce=nonce, now=now, jtis=self._upgrade_jtis
                )
            except ContractUpgradeRefused:
                raise self._refuse(actor, rec, "upgrade_proof_invalid") from None
            # #1134 r0b2 (b78c8c86-c65): a sealed generation is sealed over the
            # contract_digest; an upgrade X -> Y makes that seal NON-CURRENT (its
            # DECISION row + receipt stay as history) until Y is freshly sealed,
            # which mints a NEW generation. binding_version is a separate domain.
            trust = rec.trust
            if trust is not None and trust.binding_generation is not None:
                trust = replace(trust, binding_generation=None, authority_sha256=None)
            new = replace(
                rec,
                contract_digest=pack.contract_digest,
                api_version=pack.api_version,
                tool_names=tool_names,
                binding_version=rec.binding_version + 1,
                trust=trust,
            )
            return self._commit(
                "app_binding_contract_upgraded", actor, new, prior=rec,
                from_digest=rec.contract_digest, to_digest=new.contract_digest, proof_jti=claims["jti"],
            )

    def disable(self, actor: ControlPlaneActor, binding_id: str) -> BindingRecord:
        """Manual/admin disable: only live/paired bindings transition to disabled."""
        return self._disable(actor, binding_id, idempotent=False, allow_pending=False)

    def ensure_disabled(self, actor: ControlPlaneActor, binding_id: str) -> BindingRecord:
        """S18 revoke/compensation: platform-only, retry-safe, may retire pending."""
        rec = self.get(binding_id)
        if not (
            isinstance(actor, ControlPlaneActor)
            and actor.credential_class == CREDENTIAL_PLATFORM_ADMIN_S2S
        ):
            self.audit_refusal(actor, rec.tenant_id, rec.binding_id, "forbidden")
            raise BindingRefused("forbidden")
        return self._disable(actor, binding_id, idempotent=True, allow_pending=True)

    def _disable(
        self,
        actor: ControlPlaneActor,
        binding_id: str,
        *,
        idempotent: bool,
        allow_pending: bool,
    ) -> BindingRecord:
        rec = self.get(binding_id)
        self.require_admin(actor, rec.tenant_id, act="disable")
        with self._lock, self._transaction():
            rec = self.get(binding_id)
            if idempotent and rec.state is BindingState.DISABLED:
                return rec
            allowed = {BindingState.ACTIVE, BindingState.PAIRED}
            if allow_pending:
                allowed.add(BindingState.PENDING_PAIRING)
            if rec.state not in allowed:
                raise self._refuse(actor, rec, "invalid_transition")
            new = replace(rec, state=BindingState.DISABLED, binding_version=rec.binding_version + 1)
            # r0b2 1f36de37-024: the renamed kind (never the legacy ``app_binding_disabled``).
            return self._commit("app_binding_transitioned_disabled", actor, new, prior=rec)

    def commit_authority_seal(
        self,
        actor: ControlPlaneActor,
        prior: BindingRecord,
        *,
        binding_generation: str,
        authority_sha256: str,
        **meta: Any,
    ) -> BindingRecord:
        """#1134 B4/B6 (WIRE-1134 §9a step 3): write the sealed generation +
        authority digest into the binding's trust and advance V -> V+1 (CAS),
        with the write-ahead ``app_binding_authority_sealed`` DECISION row.

        ONLY :class:`~.binding_seal.BindingSealService` calls this, inside
        :meth:`locked_transaction`, after it verified the binding-control
        proof; the actor is the binding-control credential it stamped. Fails
        closed unless ``prior`` is still current, ACTIVE, paired and unsealed.
        """
        if not (isinstance(actor, ControlPlaneActor) and actor.credential_class == CREDENTIAL_BINDING_CONTROL):
            raise BindingRefused("forbidden")
        with self._lock, self._transaction():
            rec = self.get(prior.binding_id)
            if rec.binding_version != prior.binding_version:
                raise self._refuse(actor, rec, "version_conflict")
            if rec.state is not BindingState.ACTIVE or rec.trust is None:
                raise self._refuse(actor, rec, "invalid_transition")
            if rec.trust.binding_generation is not None:
                raise self._refuse(actor, rec, "app_binding_already_sealed")
            try:
                trust = replace(rec.trust, binding_generation=binding_generation, authority_sha256=authority_sha256)
            except BindingRefused:
                raise self._refuse(actor, rec, "trust_invalid") from None
            new = replace(rec, trust=trust, binding_version=rec.binding_version + 1)
            return self._commit(
                "app_binding_authority_sealed", actor, new, prior=rec,
                binding_generation=binding_generation, authority_sha256=authority_sha256, **meta,
            )
