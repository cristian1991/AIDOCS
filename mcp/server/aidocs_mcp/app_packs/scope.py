"""AppScope and ConnectionContext -- RFC 0003 v4.6.1 §3.6 (build plan S1).

``AppScope = (tenant_id, sub, toolspace_id)`` is the ONE stable application
security scope. ``(tenant_id, sub)`` is the seat (§2.5a); ``toolspace_id`` is
the stable application toolspace id (v1: ``crm``), never a connector,
credential, grant-instance or binding id.

Everything else a connection knows -- ``connector_id``, OAuth token id,
``binding_id``, ``binding_version``, ``contract_digest``, ``session_id``,
``actor_id``, ``host_conversation_ref`` -- is EVIDENCE: recorded as call facts
and row metadata, never part of a security key. A freeze, audit chain or rate
counter keyed on anything but AppScope would be laundered by a rebind, a
recreated connector or a new conversation (§3.6 R-c, R-d, R-f).

Both types are closed: frozen, slotted, not subclassable. AppScope takes
exactly its three members; any other keyword (``binding_id=...``) is a
``TypeError`` at construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["AppScope", "AppScopeError", "ConnectionContext"]


class AppScopeError(ValueError):
    """An AppScope or ConnectionContext member is missing or malformed."""


def _require_text(name: str, value: Any) -> None:
    if type(value) is not str or not value:
        raise AppScopeError(f"{name} must be a non-empty string")


def _refuse_subclass(cls: type) -> None:
    raise TypeError(f"{cls.__mro__[1].__name__} is a closed type and cannot be subclassed")


@dataclass(frozen=True, slots=True)
class AppScope:
    """The sealed application security scope ``(tenant_id, sub, toolspace_id)``."""

    tenant_id: str
    sub: str
    toolspace_id: str

    def __post_init__(self) -> None:
        _require_text("tenant_id", self.tenant_id)
        _require_text("sub", self.sub)
        _require_text("toolspace_id", self.toolspace_id)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        _refuse_subclass(cls)


@dataclass(frozen=True, slots=True)
class ConnectionContext:
    """One authenticated application connection: the scope triple plus evidence.

    ``connector_id`` is required evidence (§3.6 R-e); ``binding_id``,
    ``binding_version`` and ``host_conversation_ref`` are optional evidence
    that may be attached once known. None of them reaches :attr:`app_scope`.
    """

    tenant_id: str
    sub: str
    toolspace_id: str
    connector_id: str
    binding_id: Optional[str] = field(default=None)
    binding_version: Optional[int] = field(default=None)
    host_conversation_ref: Optional[str] = field(default=None)
    # #1134 B3: the token family's CRM seat references ``(crmSeatAssignmentId,
    # crmSeatAssignmentVersion)``, compared with the entitlement attestation at
    # tool-call time. Evidence only; never part of the scope.
    seat_assignment: Optional[tuple] = field(default=None)

    def __post_init__(self) -> None:
        _require_text("tenant_id", self.tenant_id)
        _require_text("sub", self.sub)
        _require_text("toolspace_id", self.toolspace_id)
        _require_text("connector_id", self.connector_id)
        if self.binding_id is not None:
            _require_text("binding_id", self.binding_id)
        if self.binding_version is not None and (
            type(self.binding_version) is not int or self.binding_version < 0
        ):
            raise AppScopeError("binding_version must be a non-negative integer")
        if self.host_conversation_ref is not None:
            _require_text("host_conversation_ref", self.host_conversation_ref)
        if self.seat_assignment is not None:
            sa = self.seat_assignment
            if not (isinstance(sa, tuple) and len(sa) == 2 and type(sa[1]) is int and sa[1] >= 0):
                raise AppScopeError("seat_assignment must be (id, non-negative integer version)")
            _require_text("seat_assignment id", sa[0])

    def __init_subclass__(cls, **kwargs: Any) -> None:
        _refuse_subclass(cls)

    @property
    def app_scope(self) -> AppScope:
        """The security scope of this connection; evidence never enters it."""
        return AppScope(self.tenant_id, self.sub, self.toolspace_id)
