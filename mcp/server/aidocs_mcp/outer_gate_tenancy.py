"""Execution-layer multi-tenancy for the Outer Gate (webmcp service).

Identity/entitlement isolation (WHO may connect) is codenexus's job. THIS module is
the EXECUTION/DATA isolation seam: it maps a validated principal's ``tenant_id`` (the
codenexus org = ``orgOwnerId or user_id``) to a private on-disk home, so each org's
project registry, selected-project state, cloned repos, and per-org "global" config
live under a directory no other tenant can name.

Hard rules:
  * tenant_id comes ONLY from a validated principal — NEVER from a request body/query.
  * tenant_id is strictly sanitised; traversal / odd chars are refused (fail-closed),
    never silently coerced, so a hostile or malformed id can't escape the tenants dir.
  * tokens + the codenexus identity stay at the SHARED gate-root (auth is global);
    only the project/config/exec layer is per-tenant.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

# codenexus ids are cuid/uuid-ish; allow a conservative, filesystem-safe charset.
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_TENANTS_DIRNAME = "tenants"


class TenantError(Exception):
    """Raised when a tenant_id is missing or fails strict sanitisation."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def sanitize_tenant_id(tenant_id: str) -> str:
    """Return a filesystem-safe tenant_id or raise ``TenantError`` (fail-closed).

    Rejects empty, over-long, traversal (``.``/``..``), path separators, and any char
    outside ``[A-Za-z0-9._-]``. NEVER coerces — a bad id is an error, not a fallback.
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise TenantError("no_tenant", "missing tenant_id (unidentified principal)")
    if tid in (".", ".."):
        raise TenantError("bad_tenant", "tenant_id is a path-traversal token")
    if not _TENANT_ID_RE.match(tid):
        raise TenantError("bad_tenant", f"tenant_id has illegal characters: {tid!r}")
    return tid


def tenants_base(gate_root: Path) -> Path:
    """The parent dir under which every per-tenant home lives."""
    return Path(gate_root) / _TENANTS_DIRNAME


def tenant_home(gate_root: Path, tenant_id: str, *, create: bool = True) -> Path:
    """Resolve (and by default lazily create) the per-tenant home directory.

    ``<gate_root>/tenants/<safe_tenant_id>/``. The returned path is verified to be
    strictly inside the tenants base (defense in depth against any sanitiser gap).
    """
    safe = sanitize_tenant_id(tenant_id)
    base = tenants_base(gate_root).resolve()
    home = (base / safe).resolve()
    # Containment check: home MUST be a direct child of base.
    if home.parent != base:
        raise TenantError("escape", f"tenant home escaped the tenants base: {home}")
    if create:
        home.mkdir(parents=True, exist_ok=True)
        (home / "projects").mkdir(parents=True, exist_ok=True)
    return home


def tenant_projects_dir(gate_root: Path, tenant_id: str, *, create: bool = True) -> Path:
    """Where this tenant's imported/cloned repos live (under their home)."""
    home = tenant_home(gate_root, tenant_id, create=create)
    d = home / "projects"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def tenant_global_config_db(gate_root: Path, tenant_id: str, *, create: bool = True) -> Path:
    """Per-ORG "global" config DB. Replaces the host-wide ~/.aidocs/config.sqlite3
    for a tenant request (wired via a contextvar in config_store). This is the org's
    install-wide scope — isolated from the gate DAEMON's own config and from peers.
    """
    home = tenant_home(gate_root, tenant_id, create=create)
    return home / "global-config.sqlite3"


class GateOrgSelectionStore:
    """Per-token SELECTED org (tenant), stored at the SHARED auth home — NOT a tenant
    home, because this records WHICH tenant a token acts as (a user may belong to many
    orgs). Set by the `org_select` tool ONLY after the gate verifies the user is a
    current member of the chosen org; read by the transport to resolve the request's
    effective tenant. Project/session selection is per-tenant (a separate store); org
    selection is the layer above it.
    """

    def db_path(self, home: Path) -> Path:
        from .identity_store import IdentityStore

        return IdentityStore().db_path(home)

    def init_db(self, home: Path) -> None:
        from .identity_store import IdentityStore

        IdentityStore().init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS gate_org_selection ("
                "token_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, updated_at TEXT NOT NULL)",
            )
            conn.commit()

    def get(self, home: Path, token_id: str) -> str:
        """The token's selected org id, or "" if none selected."""
        if not token_id:
            return ""
        self.init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            row = conn.execute(
                "SELECT org_id FROM gate_org_selection WHERE token_id=?",
                (str(token_id),),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def set(self, home: Path, token_id: str, org_id: str) -> None:
        self.init_db(home)
        ts = datetime.fromtimestamp(time.time(), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.execute(
                "INSERT INTO gate_org_selection (token_id, org_id, updated_at) "
                "VALUES (?,?,?) ON CONFLICT(token_id) DO UPDATE SET "
                "org_id=excluded.org_id, updated_at=excluded.updated_at",
                (str(token_id), str(org_id), ts),
            )
            conn.commit()

    def clear(self, home: Path, token_id: str) -> None:
        self.init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.execute("DELETE FROM gate_org_selection WHERE token_id=?", (str(token_id),))
            conn.commit()
