"""The toolspace grant reader -- RFC 0003 v4.6.1 §3.5 C1-C2, §3.6 R-e (build plan S4a).

A CRM connector is a separate MCP resource, ``/v1/apps/{connector_id}/mcp``.
The CONNECTION itself -- the presented connector id and the exact OAuth
resource its token is bound to -- resolves ``(tenant_id, toolspace_id,
connector_id)`` from the canonical CodeNexus ``AppToolspaceGrant`` row (ruling
R7; r0b2 99e62e49). Nothing else can: never a selected org, a last-used org, a
project or a session.

Read LIVE on every request (a revoke takes effect on the next request) through
the gate's read-only CodeNexus role. Fail closed:

* no active row, or a row that does not echo exactly the presented facts
  -> ``unknown`` (non-disclosing);
* more than one active row -> ``ambiguous``, even though the SQL uniqueness
  should make that impossible;
* any datasource fault, or no datasource configured -> ``unavailable``.

The grant is authority for WHICH tenant/toolspace/binding a connection names;
the binding itself is verified separately against the AIDOCS binding
authority (S4b), and seat membership is re-checked per request (transport).
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "GRANT_AMBIGUOUS",
    "GRANT_OK",
    "GRANT_UNAVAILABLE",
    "GRANT_UNKNOWN",
    "V1_TOOLSPACE",
    "CodenexusToolspaceGrantReader",
    "GrantResolution",
    "ToolspaceGrant",
    "app_resource",
    "reader_from_env",
    "valid_connector_id",
]

GRANT_OK = "ok"
GRANT_UNKNOWN = "unknown"
GRANT_AMBIGUOUS = "ambiguous"
GRANT_UNAVAILABLE = "unavailable"

#: v1 knows exactly one application toolspace.
V1_TOOLSPACE = "crm"

#: Operator-minted, opaque, never model-supplied. Lower-case so the path and the
#: resource URI have one spelling.
_CONNECTOR_ID_RE = re.compile(r"cnx_[a-z0-9]{16,40}")

_DSN_ENV = "AIDOCS_CODENEXUS_DSN"

_GRANT_SQL = (
    'SELECT "orgId", "toolspaceId", "connectorId", "bindingId", "resource" '
    'FROM "AppToolspaceGrant" '
    'WHERE "connectorId" = %s AND "resource" = %s AND "revokedAt" IS NULL '
    "LIMIT 2"
)

_GRANT_BY_BINDING_SQL = (
    'SELECT "orgId", "toolspaceId", "connectorId", "bindingId", "resource" '
    'FROM "AppToolspaceGrant" '
    'WHERE "bindingId" = %s AND "revokedAt" IS NULL '
    "LIMIT 2"
)


def valid_connector_id(value: Any) -> bool:
    """True only for a well-formed connector id (exact match, no surrounding text)."""
    return type(value) is str and _CONNECTOR_ID_RE.fullmatch(value) is not None


def app_resource(public_base: str, connector_id: str) -> str:
    """The exact OAuth resource URI of one connector: ``{base}/v1/apps/{id}/mcp``."""
    if not valid_connector_id(connector_id):
        raise ValueError("malformed connector id")
    return f"{str(public_base).rstrip('/')}/v1/apps/{connector_id}/mcp"


@dataclass(frozen=True, slots=True)
class ToolspaceGrant:
    """One active grant row, echoed exactly."""

    tenant_id: str
    toolspace_id: str
    connector_id: str
    binding_id: str
    resource: str


@dataclass(frozen=True, slots=True)
class GrantResolution:
    status: str
    grant: Optional[ToolspaceGrant] = None

    @property
    def ok(self) -> bool:
        return self.status == GRANT_OK and self.grant is not None


def _grant_from_row(row: Any, *, connector_id: str, resource: str) -> Optional[ToolspaceGrant]:
    try:
        tenant_id, toolspace_id, cid, binding_id, res = (str(v or "") for v in row)
    except (TypeError, ValueError):
        return None
    if cid != connector_id or res != resource or toolspace_id != V1_TOOLSPACE:
        return None
    if not tenant_id or not binding_id:
        return None
    return ToolspaceGrant(tenant_id, toolspace_id, cid, binding_id, res)


class CodenexusToolspaceGrantReader:
    """Reads ``AppToolspaceGrant`` through the gate's read-only CodeNexus role."""

    def __init__(self, dsn: str = "", connect: Callable[[], Any] | None = None) -> None:
        self._dsn = dsn
        self._connect = connect

    def _conn(self) -> Any:
        if self._connect is not None:
            return self._connect()
        if not self._dsn:
            raise RuntimeError("no CodeNexus datasource configured")
        import psycopg2  # lazily: only the real binding needs it

        return psycopg2.connect(self._dsn)

    def resolve(self, *, connector_id: str, resource: str) -> GrantResolution:
        if not valid_connector_id(connector_id) or type(resource) is not str or not resource:
            return GrantResolution(GRANT_UNKNOWN)
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_GRANT_SQL, (connector_id, resource))
            rows = list(cur.fetchall() or [])
        except Exception:  # noqa: BLE001 -- we could not ask: never a grant
            return GrantResolution(GRANT_UNAVAILABLE)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        if len(rows) > 1:
            return GrantResolution(GRANT_AMBIGUOUS)
        if not rows:
            return GrantResolution(GRANT_UNKNOWN)
        grant = _grant_from_row(rows[0], connector_id=connector_id, resource=resource)
        if grant is None:
            return GrantResolution(GRANT_UNKNOWN)
        return GrantResolution(GRANT_OK, grant)

    def resolve_by_binding(self, binding_id: str) -> ToolspaceGrant:
        """The ONE active grant naming ``binding_id`` (which org's authority DB holds it).

        Used by the gate-wide link flow (§8.2), whose requests carry a binding id
        and no connection. Fail closed, non-disclosing: no row, more than one
        active row, a row that does not echo the binding / v1 toolspace, or any
        datasource fault raises ``BindingRefused("binding_unknown")``.
        """
        from .binding_store import BindingRefused  # lazily: this module stays dependency-free

        if type(binding_id) is not str or not binding_id:
            raise BindingRefused("binding_unknown")
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_GRANT_BY_BINDING_SQL, (binding_id,))
            rows = list(cur.fetchall() or [])
        except Exception:  # noqa: BLE001 -- we could not ask: never a binding
            raise BindingRefused("binding_unknown") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        if len(rows) != 1:
            raise BindingRefused("binding_unknown")
        try:
            tenant_id, toolspace_id, cid, bid, res = (str(v or "") for v in rows[0])
        except (TypeError, ValueError):
            raise BindingRefused("binding_unknown") from None
        if bid != binding_id or toolspace_id != V1_TOOLSPACE or not tenant_id or not valid_connector_id(cid):
            raise BindingRefused("binding_unknown")
        return ToolspaceGrant(tenant_id, toolspace_id, cid, bid, res)


def reader_from_env(env: Mapping[str, str] | None = None) -> CodenexusToolspaceGrantReader:
    """The reader bound to the configured CodeNexus DSN (none -> always unavailable)."""
    source = os.environ if env is None else env
    return CodenexusToolspaceGrantReader(dsn=str(source.get(_DSN_ENV, "") or "").strip())
