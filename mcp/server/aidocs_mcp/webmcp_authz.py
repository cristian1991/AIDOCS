"""Gate-side webmcp authorization POLICY (webmcp DoD #2).

The webmcp service authorizes against codenexus.cloud identity (UserRole + an
active webmcp entitlement/license), NOT the gate's own store. This module is the
PURE POLICY: given a resolved codenexus principal, decide admit/refuse.

The CONNECTION to codenexus (a verified NextAuth session/JWT, a codenexus API
call, or a Postgres read) is a separate seam -- ``CodenexusIdentityResolver`` --
wired once the operator picks the mechanism. Until then the gate has no codenexus
binding, so service-mode auth is DENY (fail-closed via ``UnconfiguredResolver``).
Keeping the goal-specified policy here makes it testable now without coupling to a
transport, and without duplicating codenexus's roles/billing in the gate.

Roles (codenexus ``UserRole``, the PLATFORM scope): SUPER_ADMIN = platform owner,
ADMIN = platform staff, USER = everyone else. Org-ownership is a SEPARATE
dimension (``User.orgRole`` OWNER/ADMIN/OPERATOR) — a self-registered org owner is
a platform USER, and is admitted via an active webmcp seat, not via this enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"
_KNOWN_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER})


@dataclass(frozen=True)
class WebmcpPrincipal:
    """A codenexus identity resolved for a webmcp request."""

    user_id: str
    email: str
    role: str
    webmcp_entitled: bool  # has an ACTIVE webmcp License / seat in ANY of their orgs
    # NOTE: a principal carries NO single tenant. A user may belong to several orgs
    # (Organization + TeamMember); the active tenant is bound PER-REQUEST by the
    # `org_select` tool (gate transport), never derived from the identity here.


@dataclass(frozen=True)
class WebmcpAuthzDecision:
    allowed: bool
    reason: str


def authorize_webmcp(principal: WebmcpPrincipal | None) -> WebmcpAuthzDecision:
    """Gate-side webmcp admission policy (service mode). SUPER_ADMIN (platform
    owner) is always admitted; everyone else requires an ACTIVE webmcp entitlement
    (their own license, or a seat granted by their org-owner ADMIN). Fail-closed:
    no identity / unknown role / no entitlement -> refused.
    """
    if principal is None or not str(principal.user_id or "").strip():
        return WebmcpAuthzDecision(False, "no_identity")
    role = str(principal.role or "").strip().upper()
    if role not in _KNOWN_ROLES:
        return WebmcpAuthzDecision(False, f"unknown_role:{role or 'empty'}")
    if role == ROLE_SUPER_ADMIN:
        return WebmcpAuthzDecision(True, "platform_super_admin")
    if principal.webmcp_entitled:
        return WebmcpAuthzDecision(True, "active_webmcp_entitlement")
    return WebmcpAuthzDecision(False, "no_active_webmcp_entitlement")


class CodenexusIdentityResolver(Protocol):
    """Seam: resolve a presented credential (verified NextAuth session/JWT, OAuth
    subject, or a codenexus API/Postgres lookup) to a codenexus WebmcpPrincipal.

    The concrete implementation is wired ONCE the operator chooses the mechanism;
    until then the gate has no codenexus binding and service-mode auth denies.
    """

    def resolve(self, credential: str) -> WebmcpPrincipal | None: ...


class UnconfiguredResolver:
    """Default resolver: no codenexus binding yet -> resolves nothing (deny).
    Makes the unwired state explicit and fail-closed, never silently absent."""

    def resolve(self, credential: str) -> WebmcpPrincipal | None:
        return None
