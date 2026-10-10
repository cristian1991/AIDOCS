"""R-h application policy source -- RFC 0003 v4.6.1 §3.6 R-h, §12.5 (build plan S11, M1 part).

* **Keyed ``(tenant_id, toolspace_id)``.** No project, session, lane or seat
  axis. The native factory > global > project > session cascade
  (``config_store``) is never consulted and never stands in (R-h).
* **Defaults = platform ceilings** (§12.5; build plan M1). With no stored
  tenant policy the effective policy is :data:`PLATFORM_DEFAULT_POLICY`.
* **Tighten only.** :meth:`AppPolicySource.tighten` accepts a policy only when
  EVERY field is at or below BOTH the §12.5 ceilings AND the tenant's current
  effective policy; anything else is refused ``policy_loosening`` [FLAGGED
  judgment call: the stricter reading -- once tightened, a field never rises
  again through this act; relaxing needs a separate, not-yet-built operator
  act]. On read, a stored policy is clamped to the ceilings again (defense in
  depth: a corrupt row can never loosen).
* **Control plane.** ``tighten`` is an owner/admin act on the admin-dashboard
  credential class (§5.1, R6), write-ahead DECISION-audited
  (``app_tenant_policy_tightened``) through the S7 sink seam; refusals are
  audited as ``app_control_plane_refused``.
* **Scope of M1.** Only the §12.5 rate / concurrency limits are policy fields
  (consumed by ``rate.AppRateLimiter(limits_for=source.rate_limits)``).
  Binding-specific tightening recorded against the pinned binding, and freeze
  policy, are production-gate items [FLAGGED].

Storage: :class:`InMemoryPolicyStore` behind the :class:`PolicyStore` Protocol
[FLAGGED: the same production HOLD as the in-memory binding store].
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .app_freeze import AppScopeControlEvent, authorize_admin, emit_control_refusal
from .binding_store import AdminAuthority, ControlPlaneActor, ControlPlaneAuditSink
from .rate import CEILINGS, RateLimits, clamp_to_ceilings

__all__ = [
    "PLATFORM_DEFAULT_POLICY",
    "AppPolicy",
    "AppPolicySource",
    "InMemoryPolicyStore",
    "PolicyRefused",
    "PolicyStore",
]


@dataclass(frozen=True, slots=True)
class AppPolicy:
    """Tenant policy for one application toolspace (M1: the §12.5 rate limits)."""

    rate: RateLimits

    def __post_init__(self) -> None:
        if type(self.rate) is not RateLimits:
            raise ValueError("rate must be a RateLimits value")

    def within(self, other: "AppPolicy") -> bool:
        """True iff no field of this policy is looser than ``other``."""
        return self.rate.seat.within(other.rate.seat) and self.rate.toolspace.within(other.rate.toolspace)


PLATFORM_DEFAULT_POLICY = AppPolicy(rate=CEILINGS)


class PolicyRefused(Exception):
    """A policy act is refused; ``code`` is a LOCAL control code, never ``app_*``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _key(tenant_id: Any, toolspace_id: Any) -> tuple[str, str]:
    for name, value in (("tenant_id", tenant_id), ("toolspace_id", toolspace_id)):
        if type(value) is not str or not value:
            raise ValueError(f"{name} must be a non-empty string")
    return tenant_id, toolspace_id


class PolicyStore(Protocol):
    def get(self, tenant_id: str, toolspace_id: str) -> Optional[AppPolicy]: ...

    def put(self, tenant_id: str, toolspace_id: str, policy: AppPolicy) -> None: ...


class InMemoryPolicyStore:
    """Thread-safe in-process store keyed ``(tenant_id, toolspace_id)`` (production HOLD)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], AppPolicy] = {}
        self._lock = threading.Lock()

    def get(self, tenant_id: str, toolspace_id: str) -> Optional[AppPolicy]:
        key = _key(tenant_id, toolspace_id)
        with self._lock:
            return self._rows.get(key)

    def put(self, tenant_id: str, toolspace_id: str, policy: AppPolicy) -> None:
        if type(policy) is not AppPolicy:
            raise TypeError("put takes an AppPolicy")
        key = _key(tenant_id, toolspace_id)
        with self._lock:
            self._rows[key] = policy


class AppPolicySource:
    """The ONE R-h policy authority for the application path (see module doc)."""

    def __init__(self, store: PolicyStore, admin_authority: AdminAuthority, audit_sink: ControlPlaneAuditSink) -> None:
        self._store = store
        self._admins = admin_authority
        self._sink = audit_sink
        self._lock = threading.Lock()

    def effective(self, tenant_id: str, toolspace_id: str) -> AppPolicy:
        """The stored tenant policy clamped to the ceilings, or the ceilings.
        A store failure propagates: callers fail closed."""
        stored = self._store.get(*_key(tenant_id, toolspace_id))
        if stored is None:
            return PLATFORM_DEFAULT_POLICY
        return AppPolicy(rate=clamp_to_ceilings(stored.rate))

    def rate_limits(self, tenant_id: str, toolspace_id: str) -> RateLimits:
        """``limits_for`` for :class:`~aidocs_mcp.app_packs.rate.AppRateLimiter`."""
        return self.effective(tenant_id, toolspace_id).rate

    def tighten(self, actor: ControlPlaneActor, tenant_id: str, toolspace_id: str, policy: AppPolicy) -> AppPolicy:
        """Record a tighter policy for ``(tenant_id, toolspace_id)``. Owner/admin only."""
        tenant_id, toolspace_id = _key(tenant_id, toolspace_id)
        facts = {"tenant_id": tenant_id, "toolspace_id": toolspace_id}
        if not authorize_admin(self._admins, actor, tenant_id):
            emit_control_refusal(self._sink, actor, "forbidden", facts)
            raise PolicyRefused("forbidden")
        if type(policy) is not AppPolicy:
            emit_control_refusal(self._sink, actor, "invalid", facts)
            raise PolicyRefused("invalid")
        with self._lock:
            try:
                current = self.effective(tenant_id, toolspace_id)
            except Exception as exc:  # noqa: BLE001 -- unknown current policy: refuse
                emit_control_refusal(self._sink, actor, "control_plane_refused", facts)
                raise PolicyRefused("control_plane_refused") from exc
            if not (policy.within(PLATFORM_DEFAULT_POLICY) and policy.within(current)):
                emit_control_refusal(self._sink, actor, "policy_loosening", facts)
                raise PolicyRefused("policy_loosening")
            try:
                self._sink.emit(AppScopeControlEvent("app_tenant_policy_tightened", tenant_id, actor.user_id, facts))
            except Exception as exc:  # noqa: BLE001 -- no audit, no change
                raise PolicyRefused("audit_unavailable") from exc
            self._store.put(tenant_id, toolspace_id, policy)
            return policy
