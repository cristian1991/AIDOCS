"""The toolspace grant reader -- RFC 0003 v4.6.1 §3.5 C1-C2, §3.6 R-e (build plan S4a).

A CRM connector is a separate MCP resource, ``/v1/apps/{connector_id}/mcp``.
The CONNECTION itself -- the presented connector id and the exact OAuth
resource its token is bound to -- resolves ``(tenant_id, toolspace_id,
connector_id)`` from the canonical CodeNexus grant (ruling R7; r0b2 99e62e49):
since #1134 the ``AppInstallation`` + ``AppBindingGrant`` tables first, the
legacy ``AppToolspaceGrant`` row only for keys the new tables do not know (see
:class:`CodenexusToolspaceGrantReader`). Nothing else can: never a selected org, a last-used org, a
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
authority (S4b), and the TRANSITIONAL app admission (a live CodeNexus
OWNER/ADMIN of ``organization_id``; #1134 A7, until Phase B AppSeatBinding)
is re-checked per request (transport). No WebMCP seat or license applies.
"""
from __future__ import annotations

import dataclasses
import os
import re
import threading
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
    "GateOrganizationMap",
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

# -- #1134: the NEW tables (CodeNexus migration 20261004120000_add_app_installation) --------------
# The active-join semantics are CodeNexus scripts/backfill-app-installation.ts
# ``compareReaders`` (NEW_SELECT), verbatim except the column ORDER, which follows
# this reader's row shape (orgId, toolspaceId, connectorId, bindingId, resource).
_NEW_SELECT = (
    'SELECT i."organizationId" AS "orgId", i."toolspaceId", i."connectorId", g."bindingId", i."resource" '
    'FROM "AppInstallation" i JOIN "AppBindingGrant" g '
    "ON g.\"installationId\" = i.\"id\" AND g.\"state\" = 'active' "
    "WHERE i.\"endedAt\" IS NULL AND i.\"status\" = 'active'"
)

_NEW_GRANT_SQL = _NEW_SELECT + ' AND i."connectorId" = %s AND i."resource" = %s LIMIT 2'

_NEW_GRANT_BY_BINDING_SQL = _NEW_SELECT + ' AND g."bindingId" = %s LIMIT 2'

# Does the NEW model know this key at all, in ANY state? connectorId, resource and
# bindingId are unique over ALL rows there (never reused, even after an install
# ends or a binding is revoked), so a hit means the new tables are authoritative
# for it and the legacy table must not answer.
# These two checks MUST stay on the RAW tables with NO status/state filter: an
# AppInstallation in any status (incl. disabled/ended) and an AppBindingGrant in
# any state (incl. revoked) count as known. Never point them at a view -- the
# future AppInstallationProjection omits revoked grants by design, which would
# let a revoked key fall back to an active legacy row.
_NEW_KNOWS_CONNECTOR_SQL = 'SELECT 1 FROM "AppInstallation" WHERE "connectorId" = %s OR "resource" = %s LIMIT 1'

_NEW_KNOWS_BINDING_SQL = 'SELECT 1 FROM "AppBindingGrant" WHERE "bindingId" = %s LIMIT 1'

# -- the legacy fallback -------------------------------------------------------------------------
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
    """One active grant row, echoed exactly.

    ``tenant_id`` is the AIDOCS tenant (AIOS routing). ``organization_id`` is the
    row's CodeNexus organization: the ONLY id any CodeNexus call (seat,
    membership) may use (#1134 A7). Empty means unknown: CodeNexus checks fail closed.
    """

    tenant_id: str
    toolspace_id: str
    connector_id: str
    binding_id: str
    resource: str
    organization_id: str = ""


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


def _grant_for_binding(row: Any, *, binding_id: str) -> Optional[ToolspaceGrant]:
    try:
        tenant_id, toolspace_id, cid, bid, res = (str(v or "") for v in row)
    except (TypeError, ValueError):
        return None
    if bid != binding_id or toolspace_id != V1_TOOLSPACE or not tenant_id or not valid_connector_id(cid):
        return None
    return ToolspaceGrant(tenant_id, toolspace_id, cid, bid, res)


def _rows(cur: Any, sql: str, params: tuple) -> list:
    cur.execute(sql, params)
    return list(cur.fetchall() or [])


_MAP_UNKNOWN = "unknown"
_MAP_UNAVAILABLE = "unavailable"


def _aidocs_tenant(organizations: Any, organization_id: str) -> tuple[str, str]:
    """CodeNexus ``orgId`` -> the AIDOCS tenant, through the org->tenant map.

    Returns ``("ok", tenant)``, ``("unknown", "")`` for an unmapped (or malformed)
    organization, or ``("unavailable", "")`` when the map is missing or faults --
    a fault is NEVER reported as unmapped.
    """
    if organizations is None:
        return _MAP_UNAVAILABLE, ""
    from .org_tenant_map import OrganizationMappingRefused  # lazily: this module stays dependency-free

    try:
        tenant = organizations.resolve(organization_id)
    except OrganizationMappingRefused as exc:
        return (_MAP_UNKNOWN if exc.code == "organization_invalid" else _MAP_UNAVAILABLE), ""
    except Exception:  # noqa: BLE001 -- we could not ask: never a tenant
        return _MAP_UNAVAILABLE, ""
    if tenant is None:
        return _MAP_UNKNOWN, ""
    if type(tenant) is not str or not tenant:
        return _MAP_UNAVAILABLE, ""
    return "ok", tenant


_GATE_MAPS: dict[str, Any] = {}
_GATE_MAPS_LOCK = threading.Lock()


class GateOrganizationMap:
    """The production org->tenant resolver: the durable, install-wide, write-once
    ``SqliteOrganizationTenantMap`` under the gate root (the file the S18 bind and the
    audited operator seed write). This reader only ever calls ``resolve``.

    Resolved lazily per call, so a missing gate root or store fault surfaces as a
    fault (unavailable), never as "unmapped".
    """

    def resolve(self, organization_id: str) -> Optional[str]:
        from .org_tenant_map import SqliteOrganizationTenantMap, org_tenant_map_db_path
        from .runtime import runtime_config

        path = str(org_tenant_map_db_path(runtime_config().gate_root))
        with _GATE_MAPS_LOCK:
            store = _GATE_MAPS.get(path)
            if store is None:
                store = _GATE_MAPS[path] = SqliteOrganizationTenantMap(path)
        return store.resolve(organization_id)


class CodenexusToolspaceGrantReader:
    """Reads the toolspace grant through the gate's read-only CodeNexus role.

    #1134 reader switch. Precedence, per lookup key:

    1. The NEW tables (``AppInstallation`` status=active, endedAt NULL, JOIN
       ``AppBindingGrant`` state=active). Exactly one row -> that grant (it must
       echo the presented facts, else unknown); more than one -> ambiguous.
    2. If the join has nothing but the new tables KNOW the key in any state (a
       not-yet-active, disabled or ended installation; a pending or revoked
       binding) -> unknown. The new tables are authoritative for every key they
       hold, so a stale active legacy row can never resurrect a paused/revoked one.
    3. Otherwise the legacy ``AppToolspaceGrant`` row (app1 until its copy-first
       backfill), under the old rules -- and only if the new tables know NEITHER
       of its keys (connector/resource nor binding), so a legacy row can never
       shadow, or be spliced onto, a key the new model owns.

    Any datasource fault on any of these reads (including the role being denied
    the new tables) is unavailable: we never fall back because we could not ask.

    The AIDOCS tenant is NEVER the CodeNexus org id as such, and never CodeNexus's
    ``aidocsTenantId`` (evidence only, not read): the chosen row's ``orgId`` -- new
    tables or legacy alike -- is mapped through ``organizations`` (the durable
    org->tenant map; :class:`GateOrganizationMap` in production). app1 is
    identity-seeded there, so its result is unchanged. Unmapped -> unknown; a map
    fault or no map -> unavailable. Neither ever falls back to another row.
    """

    def __init__(
        self,
        dsn: str = "",
        connect: Callable[[], Any] | None = None,
        organizations: Any = None,
    ) -> None:
        self._dsn = dsn
        self._connect = connect
        self._organizations = organizations

    def _conn(self) -> Any:
        if self._connect is not None:
            return self._connect()
        if not self._dsn:
            raise RuntimeError("no CodeNexus datasource configured")
        import psycopg2  # lazily: only the real binding needs it

        return psycopg2.connect(self._dsn)

    def _read(self, work: Callable[[Any], Any]) -> Any:
        """Run ``work(cursor)`` on one live connection; any fault propagates, the connection always closes."""
        conn = None
        try:
            conn = self._conn()
            return work(conn.cursor())
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def resolve(self, *, connector_id: str, resource: str) -> GrantResolution:
        if not valid_connector_id(connector_id) or type(resource) is not str or not resource:
            return GrantResolution(GRANT_UNKNOWN)

        def work(cur: Any) -> GrantResolution:
            new_rows = _rows(cur, _NEW_GRANT_SQL, (connector_id, resource))
            if new_rows:
                rows = new_rows
            elif _rows(cur, _NEW_KNOWS_CONNECTOR_SQL, (connector_id, resource)):
                return GrantResolution(GRANT_UNKNOWN)  # the new tables own it; it is not active
            else:
                rows = _rows(cur, _GRANT_SQL, (connector_id, resource))
            if len(rows) > 1:
                return GrantResolution(GRANT_AMBIGUOUS)
            if not rows:
                return GrantResolution(GRANT_UNKNOWN)
            grant = _grant_from_row(rows[0], connector_id=connector_id, resource=resource)
            if grant is None:
                return GrantResolution(GRANT_UNKNOWN)
            if not new_rows and _rows(cur, _NEW_KNOWS_BINDING_SQL, (grant.binding_id,)):
                return GrantResolution(GRANT_UNKNOWN)  # a legacy row naming a binding the new tables own
            return GrantResolution(GRANT_OK, grant)

        try:
            found = self._read(work)
        except Exception:  # noqa: BLE001 -- we could not ask: never a grant
            return GrantResolution(GRANT_UNAVAILABLE)
        if not found.ok:
            return found
        status, tenant = _aidocs_tenant(self._organizations, found.grant.tenant_id)
        if status == _MAP_UNKNOWN:
            return GrantResolution(GRANT_UNKNOWN)
        if status != "ok":
            return GrantResolution(GRANT_UNAVAILABLE)
        return GrantResolution(GRANT_OK, dataclasses.replace(
            found.grant, tenant_id=tenant, organization_id=found.grant.tenant_id,
        ))

    def resolve_by_binding(self, binding_id: str) -> ToolspaceGrant:
        """The ONE active grant naming ``binding_id`` (which org's authority DB holds it).

        Used by the gate-wide link flow (§8.2), whose requests carry a binding id
        and no connection. Same precedence as :meth:`resolve` (new tables first,
        legacy fallback only for keys the new tables do not know). Fail closed,
        non-disclosing: no row, more than one active row, a row that does not echo
        the binding / v1 toolspace, or any datasource fault raises
        ``BindingRefused("binding_unknown")``.
        """
        from .binding_store import BindingRefused  # lazily: this module stays dependency-free

        if type(binding_id) is not str or not binding_id:
            raise BindingRefused("binding_unknown")

        def work(cur: Any) -> Optional[ToolspaceGrant]:
            new_rows = _rows(cur, _NEW_GRANT_BY_BINDING_SQL, (binding_id,))
            if new_rows:
                rows = new_rows
            elif _rows(cur, _NEW_KNOWS_BINDING_SQL, (binding_id,)):
                return None  # the new tables own it; it is not active
            else:
                rows = _rows(cur, _GRANT_BY_BINDING_SQL, (binding_id,))
            if len(rows) != 1:
                return None
            grant = _grant_for_binding(rows[0], binding_id=binding_id)
            if grant is None:
                return None
            if not new_rows and _rows(cur, _NEW_KNOWS_CONNECTOR_SQL, (grant.connector_id, grant.resource)):
                return None  # a legacy row naming a connector the new tables own
            return grant

        try:
            grant = self._read(work)
        except Exception:  # noqa: BLE001 -- we could not ask: never a binding
            raise BindingRefused("binding_unknown") from None
        if grant is None:
            raise BindingRefused("binding_unknown")
        status, tenant = _aidocs_tenant(self._organizations, grant.tenant_id)
        if status == _MAP_UNKNOWN:
            raise BindingRefused("binding_unknown")
        if status != "ok":
            raise BindingRefused("binding_unavailable")
        return dataclasses.replace(grant, tenant_id=tenant, organization_id=grant.tenant_id)


def reader_from_env(env: Mapping[str, str] | None = None) -> CodenexusToolspaceGrantReader:
    """The reader bound to the configured CodeNexus DSN (none -> always unavailable)."""
    source = os.environ if env is None else env
    return CodenexusToolspaceGrantReader(
        dsn=str(source.get(_DSN_ENV, "") or "").strip(),
        organizations=GateOrganizationMap(),
    )
