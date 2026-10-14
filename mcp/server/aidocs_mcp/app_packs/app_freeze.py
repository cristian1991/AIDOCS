"""R-c AppScope freeze CHECK + admin clear -- RFC 0003 v4.6.1 §3.6 R-c (build plan S11, M1 part).

* **Scope.** A freeze is keyed by a :class:`FreezeTarget` built ONLY from
  AppScope members: the AppScope itself ``(tenant_id, sub, toolspace_id)``, the
  whole toolspace of a tenant ``(tenant_id, toolspace_id)`` or the whole tenant
  ``(tenant_id,)``. ``binding_id``, ``binding_version``, ``connector_id``,
  ``session_id``, ``actor_id`` and the host conversation are evidence /
  provenance (§3.6): a record may carry ``binding_id`` / ``connector_id`` as
  evidence, never as key, so a rebind, an upgrade, an unbind-rebind, a recreated
  connector or a new conversation cannot launder a freeze.
* **Check** (§9 "platform freeze / tenant policy"): :meth:`AppFreezeService.check`
  refuses a call whose tenant, toolspace or AppScope is frozen with
  :class:`AppScopeFrozen`. §16.3 has no dedicated freeze code; the refusal uses
  ``app_pack_unavailable`` [FLAGGED judgment call]: the application toolspace is
  unavailable to this caller, stated without disclosing why, and without
  collapsing into ``app_user_disabled`` (whose §7 meaning is "the APPLICATION
  disabled the linked human"). A store failure fails closed
  (:class:`FreezeCheckUnavailable`, ``app_internal``).
* **Admin clear** (§5.1, §16.1): owner/admin of the owning org only, through the
  admin-dashboard credential class (build plan R6); refusals and the clear are
  DECISION-class audit events through the S7 control-plane sink seam
  (``ControlPlaneAuditSink.emit``). The clear is WRITE-AHEAD audited: no audit
  row, no clear -- the freeze stands.
* **M1 only.** No strike or escalation producer and no admin "freeze" act exist
  here (build plan "M1: what may be minimal"); :meth:`InMemoryFreezeStore.put`
  is the storage seam those producers will write through (production gate).
* Native ``session_freeze_store`` / ``security_violation_service`` /
  ``escalation_store`` (project-root + session keyed) are never used.

Storage: behind the :class:`FreezeStore` Protocol. Production is the durable
``AppAuthorityDB.freezes()`` (per-org SQLite under the tenant home), whose clear
and its DECISION row are ONE transaction; :class:`InMemoryFreezeStore` is for
tests.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, Union

from ..execution_event_retention import EVENT_KIND_RETENTION, RetentionClass
from .authority_db import authority_transaction
from .binding_store import (
    CREDENTIAL_ADMIN_DASHBOARD,
    RETENTION_DECISION,
    AdminAuthority,
    ControlPlaneActor,
    ControlPlaneAuditSink,
    refusal_code_metadata,
)
from .boundary_audit import KIND_STATUS, audit_metadata, kind_status_admissible
from .scope import AppScope, ConnectionContext

__all__ = [
    "APP_SCOPE_CONTROL_KINDS",
    "FREEZE_REASONS",
    "FREEZE_REFUSAL_CODE",
    "AppFreezeService",
    "AppScopeControlEvent",
    "AppScopeFrozen",
    "FreezeCheckUnavailable",
    "FreezeRecord",
    "FreezeRefused",
    "FreezeStore",
    "FreezeTarget",
    "InMemoryFreezeStore",
    "authorize_admin",
    "emit_control_refusal",
]

#: §16.3 code a frozen call is refused with (see module doc).
FREEZE_REFUSAL_CODE = "app_pack_unavailable"

LEVEL_APP_SCOPE = "app_scope"
LEVEL_TOOLSPACE = "toolspace"
LEVEL_TENANT = "tenant"

#: Closed vocabulary of why a freeze exists. M1 builds no producer; strike and
#: escalation producers arrive with the production gate (complete R-c).
FREEZE_REASONS = frozenset({"operator", "strike", "escalation"})

#: The S11 control-plane audit kinds (registered in ``boundary_audit.KIND_STATUS``
#: and, as DECISION, in ``execution_event_retention``).
APP_SCOPE_CONTROL_KINDS = frozenset(
    {"app_scope_freeze_cleared", "app_tenant_policy_tightened", "app_control_plane_refused"}
)

Subject = Union[AppScope, ConnectionContext]


def _text(name: str, value: Any) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _scope_of(subject: Any) -> AppScope:
    if type(subject) is AppScope:
        return subject
    if type(subject) is ConnectionContext:
        return subject.app_scope
    raise TypeError("the freeze check takes an AppScope or a ConnectionContext")


@dataclass(frozen=True, slots=True)
class FreezeTarget:
    """What a freeze covers. AppScope members only; closed type."""

    tenant_id: str
    toolspace_id: Optional[str] = None
    sub: Optional[str] = None

    def __post_init__(self) -> None:
        _text("tenant_id", self.tenant_id)
        if self.toolspace_id is not None:
            _text("toolspace_id", self.toolspace_id)
        if self.sub is not None:
            _text("sub", self.sub)
            if self.toolspace_id is None:
                raise ValueError("a seat freeze is an AppScope freeze and needs its toolspace_id")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("FreezeTarget is a closed type and cannot be subclassed")

    @property
    def level(self) -> str:
        if self.sub is not None:
            return LEVEL_APP_SCOPE
        return LEVEL_TOOLSPACE if self.toolspace_id is not None else LEVEL_TENANT

    @property
    def key(self) -> tuple:
        if self.sub is not None:
            return (LEVEL_APP_SCOPE, self.tenant_id, self.toolspace_id, self.sub)
        if self.toolspace_id is not None:
            return (LEVEL_TOOLSPACE, self.tenant_id, self.toolspace_id)
        return (LEVEL_TENANT, self.tenant_id)

    @classmethod
    def of_scope(cls, subject: Subject) -> "FreezeTarget":
        return _scope_target(subject)

    @classmethod
    def of_toolspace(cls, tenant_id: str, toolspace_id: str) -> "FreezeTarget":
        return cls(tenant_id=tenant_id, toolspace_id=_text("toolspace_id", toolspace_id))

    @classmethod
    def of_tenant(cls, tenant_id: str) -> "FreezeTarget":
        return cls(tenant_id=tenant_id)


def _scope_target(subject: Subject) -> FreezeTarget:
    """The AppScope-level target: ``(tenant_id, sub, toolspace_id)`` and nothing else."""
    scope = _scope_of(subject)
    return FreezeTarget(tenant_id=scope.tenant_id, toolspace_id=scope.toolspace_id, sub=scope.sub)


@dataclass(frozen=True, slots=True)
class FreezeRecord:
    """One freeze. ``binding_id`` / ``connector_id`` are EVIDENCE (§3.6), not key."""

    target: FreezeTarget
    frozen_at: int
    reason: str
    binding_id: Optional[str] = None
    connector_id: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.target) is not FreezeTarget:
            raise ValueError("target must be a FreezeTarget")
        if type(self.frozen_at) is not int or self.frozen_at < 0:
            raise ValueError("frozen_at must be a non-negative integer (epoch seconds)")
        if self.reason not in FREEZE_REASONS:
            raise ValueError("reason is outside the closed freeze vocabulary")
        for name in ("binding_id", "connector_id"):
            if getattr(self, name) is not None:
                _text(name, getattr(self, name))


class FreezeStore(Protocol):
    def get(self, target: FreezeTarget) -> Optional[FreezeRecord]: ...

    def put(self, record: FreezeRecord) -> None: ...

    def delete(self, target: FreezeTarget) -> Optional[FreezeRecord]: ...


class InMemoryFreezeStore:
    """Thread-safe in-process store keyed by :attr:`FreezeTarget.key` (production HOLD)."""

    def __init__(self) -> None:
        self._rows: dict[tuple, FreezeRecord] = {}
        self._lock = threading.Lock()

    def get(self, target: FreezeTarget) -> Optional[FreezeRecord]:
        with self._lock:
            return self._rows.get(target.key)

    def put(self, record: FreezeRecord) -> None:
        if type(record) is not FreezeRecord:
            raise TypeError("put takes a FreezeRecord")
        with self._lock:
            self._rows[record.target.key] = record

    def delete(self, target: FreezeTarget) -> Optional[FreezeRecord]:
        with self._lock:
            return self._rows.pop(target.key, None)


class AppScopeFrozen(Exception):
    """The call's AppScope, toolspace or tenant is frozen. Non-disclosing text."""

    code = FREEZE_REFUSAL_CODE

    def __init__(self, level: str) -> None:
        super().__init__(FREEZE_REFUSAL_CODE)
        self.level = level  # internal only: never rendered to the model


class FreezeCheckUnavailable(Exception):
    """The freeze state could not be read; the call is refused (fail closed)."""

    code = "app_internal"


class FreezeRefused(Exception):
    """A freeze control-plane act is refused. ``code`` is a LOCAL control code
    (``boundary_audit.CONTROL_REFUSAL_CODES``), never a model-facing ``app_*``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AppScopeControlEvent:
    """A §16.1 DECISION-class control-plane audit event for the S11 acts.

    Same contract as the S7 ``ControlPlaneEvent`` (emitted through the same
    ``ControlPlaneAuditSink.emit`` seam): the kind is registered, ``status`` is
    derived from the closed kind <-> status matrix, and every metadata entry is
    allowlisted and well-shaped -- anything else raises.
    """

    kind: str
    tenant_id: str
    actor_user_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    retention: str = RETENTION_DECISION
    binding_id: Optional[str] = None
    status: str = field(init=False)

    def __post_init__(self) -> None:
        if self.retention != RETENTION_DECISION:
            raise ValueError("S11 control-plane events are DECISION-class")
        if self.kind not in APP_SCOPE_CONTROL_KINDS or self.kind not in KIND_STATUS:
            raise ValueError(f"unregistered S11 control-plane audit kind {self.kind!r}")
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


def authorize_admin(admins: AdminAuthority, actor: Any, tenant_id: str) -> bool:
    """§5.1: an OWNER/ADMIN of ``tenant_id`` on the admin-dashboard credential
    class (R6: an MCP connector / OAuth credential never qualifies). An
    authority failure is a refusal."""
    if not isinstance(actor, ControlPlaneActor) or actor.credential_class != CREDENTIAL_ADMIN_DASHBOARD:
        return False
    try:
        return admins.is_org_admin(actor.user_id, tenant_id) is True
    except Exception:  # noqa: BLE001 -- authority failure fails closed
        return False


def emit_control_refusal(sink: ControlPlaneAuditSink, actor: Any, code: str, facts: Mapping[str, Any]) -> None:
    """Best-effort DECISION row for a refused S11 act; the refusal stands either way.
    ``facts`` must carry ``tenant_id``; unshaped facts are dropped, never coerced."""
    user = actor.user_id if isinstance(actor, ControlPlaneActor) else "<unknown>"
    md = {**audit_metadata(facts), **refusal_code_metadata(code)}
    try:
        sink.emit(AppScopeControlEvent("app_control_plane_refused", str(facts["tenant_id"]), user, md))
    except Exception:  # noqa: BLE001 -- the refusal stands even if its audit write fails
        pass


def _target_facts(target: FreezeTarget) -> dict:
    facts: dict = {"tenant_id": target.tenant_id}
    if target.toolspace_id is not None:
        facts["toolspace_id"] = target.toolspace_id
    if target.sub is not None:
        facts["sub"] = target.sub
    return facts


class AppFreezeService:
    """The R-c admission check and the M1 admin clear (see module doc)."""

    def __init__(self, store: FreezeStore, admin_authority: AdminAuthority, audit_sink: ControlPlaneAuditSink) -> None:
        self._store = store
        self._admins = admin_authority
        self._sink = audit_sink
        self._lock = threading.Lock()
        check = getattr(store, "check_sink", None)
        if callable(check):
            check(audit_sink)  # ruling 2: a durable store audits on its own file

    def check(self, subject: Subject) -> None:
        """Raise :class:`AppScopeFrozen` if the tenant, the tenant's toolspace or
        the AppScope is frozen; :class:`FreezeCheckUnavailable` if unknown."""
        scope = _scope_of(subject)
        targets = (
            FreezeTarget.of_tenant(scope.tenant_id),
            FreezeTarget.of_toolspace(scope.tenant_id, scope.toolspace_id),
            _scope_target(subject),
        )
        for target in targets:
            try:
                hit = self._store.get(target)
            except Exception as exc:  # noqa: BLE001 -- unknown freeze state fails closed
                raise FreezeCheckUnavailable("app_internal") from exc
            if hit is not None:
                raise AppScopeFrozen(target.level)

    def admin_clear(self, actor: ControlPlaneActor, target: FreezeTarget) -> FreezeRecord:
        """Lift one freeze. Owner/admin only; write-ahead DECISION audit."""
        if type(target) is not FreezeTarget:
            raise FreezeRefused("invalid")
        facts = _target_facts(target)
        if not authorize_admin(self._admins, actor, target.tenant_id):
            emit_control_refusal(self._sink, actor, "forbidden", facts)
            raise FreezeRefused("forbidden")
        # Durable store: the read, the DECISION row and the delete are ONE transaction.
        with self._lock, authority_transaction(self._store):
            try:
                record = self._store.get(target)
            except Exception as exc:  # noqa: BLE001
                emit_control_refusal(self._sink, actor, "control_plane_refused", facts)
                raise FreezeRefused("control_plane_refused") from exc
            if record is None:
                emit_control_refusal(self._sink, actor, "invalid_transition", facts)
                raise FreezeRefused("invalid_transition")
            evidence = {k: v for k, v in (("binding_id", record.binding_id),
                                          ("connector_id", record.connector_id)) if v is not None}
            try:
                event = AppScopeControlEvent(
                    "app_scope_freeze_cleared", target.tenant_id, actor.user_id, {**facts, **evidence},
                    binding_id=record.binding_id,
                )
                self._sink.emit(event)
            except Exception as exc:  # noqa: BLE001 -- no audit, no clear
                raise FreezeRefused("audit_unavailable") from exc
            self._store.delete(target)
            return record
