"""RBAC store — Layer 9 corporate-ready deliverable C-2.

Dynamic roles + permission matrix layered on top of identity_store
(C-1). Operators define roles by composing from a permission
catalog (permission_catalog.py); the store persists role rows,
permission assignments, and user→role bindings. Enforcement is a
single chokepoint: `user_has_permission(user_id, permission_name)`.

Why dynamic roles (not closed enum)?
- Real orgs need super_admin / admin / audit / dev / tester plus
  project-specific roles ("release-manager", "oncall-escalator").
- A closed enum forces premature commitment and forks the code on
  every new deployment.
- Audit risk is bounded by (a) built-in "is_system=1" seed roles
  that can't be deleted, (b) permission catalog being closed-set,
  (c) audit-logged role/permission mutations.

Tables:
- rbac_roles: role_id, name (unique), description, is_system,
  created_at.
- rbac_permissions: permission_name (PK — the canonical catalog
  name), description. Catalog is seeded from
  permission_catalog.ALL_PERMISSIONS on init_db.
- rbac_role_permissions: role_id × permission_name.
- rbac_user_roles: user_id × role_id (many-to-many so a user can
  hold audit + dev simultaneously).
"""

from __future__ import annotations

import secrets
import sqlite3

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect
from ._sqlite_connect import mark_schema_ensured as _mark_schema_ensured
from ._sqlite_connect import schema_already_ensured as _schema_already_ensured
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

from . import store_migrations


@dataclass(frozen=True)
class Role:
    role_id: str
    name: str
    description: str
    is_system: bool
    created_at: str
    # Corpo additions (2026-04-21). Defaults preserve the pre-migration
    # shape: rank=500 (Operator tier), no inheritance, no authored-by.
    rank: int = 500
    inherits_from_role_key: str | None = None
    created_by_user_id: str = ""


@dataclass(frozen=True)
class Permission:
    """Closed-set capability tag. Names are dotted axes:
    `mcp_tool.<tool_name>`, `security.allow_raw_shell`, `rbac.manage_roles`.
    """

    name: str
    description: str


@dataclass(frozen=True)
class UserPermissions:
    """Flattened view: every permission a user currently holds,
    summed across every role they're bound to.
    """

    user_id: str
    roles: tuple[str, ...]
    permissions: frozenset[str] = field(default_factory=frozenset)


class RBACStore:
    """MACHINE-GLOBAL RBAC sqlite store (#488).

    Co-located with identity_store's database so role checks don't cross
    file boundaries — and global for the same reason identity is: an
    operator's ROLES are a property of the operator, not of whichever
    project happens to be selected. Rows that must stay narrow already
    carry scope_type/scope_id, so a project-scoped grant remains
    project-scoped inside the global home (see identity_db.py).
    """

    def db_path(self, project_root: Path) -> Path:
        from .identity_db import identity_db_path

        return identity_db_path(project_root)

    def init_db(
        self,
        project_root: Path,
        seed_permissions: Iterable[Permission] | None = None,
    ) -> None:
        path = self.db_path(project_root)
        # ONE schema creation per process per file (#756) -- see session_freeze
        #_store.init_db. Keyed with the seed set so a caller that DOES pass
        # seed_permissions still runs (seeding is not schema).
        if seed_permissions is None and _schema_already_ensured(path, "rbac"):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # #746: aidocs_identity.sqlite3 is SHARED, so whichever store creates it
        # decides the journal mode every later connection inherits (journal_mode
        # lives in the FILE HEADER). Every creator therefore goes through the one
        # canonical connect (#755) -- WAL by luck is not WAL by design. It also
        # turns foreign_keys ON, which SQLite defaults OFF per connection and
        # without which the rbac_role_permissions -> rbac_roles / rbac_permissions
        # FKs declared below are inert.
        with _canonical_connect(path, durability=_Durability.RUNTIME) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rbac_roles (
                    role_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    is_system INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rbac_permissions (
                    permission_name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS rbac_role_permissions (
                    role_id TEXT NOT NULL,
                    permission_name TEXT NOT NULL,
                    PRIMARY KEY (role_id, permission_name),
                    FOREIGN KEY (role_id) REFERENCES rbac_roles(role_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (permission_name)
                        REFERENCES rbac_permissions(permission_name)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS rbac_user_roles (
                    user_id TEXT NOT NULL,
                    role_id TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, role_id),
                    FOREIGN KEY (role_id) REFERENCES rbac_roles(role_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_rbac_user_roles_user
                    ON rbac_user_roles(user_id);
                CREATE INDEX IF NOT EXISTS idx_rbac_role_permissions_role
                    ON rbac_role_permissions(role_id);
            """)
            # Corpo-model additive migration (2026-04-21). Columns are
            # added one-at-a-time because sqlite ALTER TABLE doesn't
            # support "ADD COLUMN IF NOT EXISTS" — catch the resulting
            # "duplicate column" OperationalError per-column.
            _additive_columns: tuple[tuple[str, str], ...] = (
                # Numeric authority ladder. 0 = highest (super_admin).
                # Seeded in permission_catalog.SEED_ROLES. Lower rank =
                # more authority. Allows dynamic roles to slot between
                # seed roles (gap 100 between seeds).
                (
                    "rbac_roles",
                    "ALTER TABLE rbac_roles ADD COLUMN rank INTEGER NOT NULL DEFAULT 500",
                ),
                # Opt-in role inheritance. NULL = explicit-only role.
                # Resolution walks parent's grants before applying own.
                ("rbac_roles", "ALTER TABLE rbac_roles ADD COLUMN inherits_from_role_key TEXT"),
                # Authorship for audit + rank-gate on overrides.
                (
                    "rbac_roles",
                    "ALTER TABLE rbac_roles ADD COLUMN created_by_user_id TEXT NOT NULL DEFAULT ''",
                ),
                # Three-state grants: 1=granted, 0=denied, NULL=unspecified.
                # Default 1 preserves existing grant-only semantics for
                # pre-migration rows.
                (
                    "rbac_role_permissions",
                    "ALTER TABLE rbac_role_permissions ADD COLUMN is_granted INTEGER NOT NULL DEFAULT 1",
                ),
                # Authored-by on role-permission assignment (audit trail +
                # rank gate: a row's author must have rank <= target
                # role's rank for the grant to be valid at resolution).
                (
                    "rbac_role_permissions",
                    "ALTER TABLE rbac_role_permissions ADD COLUMN authored_by_user_id TEXT NOT NULL DEFAULT ''",
                ),
                (
                    "rbac_role_permissions",
                    "ALTER TABLE rbac_role_permissions ADD COLUMN authored_at TEXT NOT NULL DEFAULT ''",
                ),
                (
                    "rbac_role_permissions",
                    "ALTER TABLE rbac_role_permissions ADD COLUMN authored_by_rank INTEGER NOT NULL DEFAULT 0",
                ),
                (
                    "rbac_role_permissions",
                    "ALTER TABLE rbac_role_permissions ADD COLUMN expires_at TEXT",
                ),
                # User-role assignments carry scope. scope_type:
                # 'global' | 'project' | 'session'. scope_id is NULL for
                # global, the project_root string for project scope, the
                # session_id for session scope. Default 'global' + NULL
                # preserves pre-migration rows as platform-wide grants.
                (
                    "rbac_user_roles",
                    "ALTER TABLE rbac_user_roles ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'global'",
                ),
                ("rbac_user_roles", "ALTER TABLE rbac_user_roles ADD COLUMN scope_id TEXT"),
                (
                    "rbac_user_roles",
                    "ALTER TABLE rbac_user_roles ADD COLUMN authored_by_user_id TEXT NOT NULL DEFAULT ''",
                ),
                (
                    "rbac_user_roles",
                    "ALTER TABLE rbac_user_roles ADD COLUMN authored_by_rank INTEGER NOT NULL DEFAULT 0",
                ),
                ("rbac_user_roles", "ALTER TABLE rbac_user_roles ADD COLUMN expires_at TEXT"),
            )
            for _table, _ddl in _additive_columns:
                try:
                    conn.execute(_ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

            # #243 recovery: a pre-fix crash could have committed an EMPTY
            # new-shape rbac_user_roles while stranding rows in
            # rbac_user_roles_legacy_v1 — and the scope-shape check BELOW would
            # then see a correct new-shape table, so the migration never
            # re-runs and the role assignments are lost. Restore the stranded
            # rows and drop the legacy table, atomically, BEFORE that check.
            if store_migrations.table_exists(conn, "rbac_user_roles_legacy_v1"):
                # #817: carry each row's OWN scope back. The stranded table is
                # produced by the RENAME below, i.e. AFTER the additive ALTERs,
                # so it normally has scope columns; a pre-corpo stray without
                # them still lands as ('global', NULL). Hardcoding 'global'
                # unconditionally (the pre-#817 spelling) WIDENED every
                # project- and session-scoped grant into a machine-wide one.
                _legacy_cols = {
                    str(r[1])
                    for r in conn.execute("PRAGMA table_info(rbac_user_roles_legacy_v1)").fetchall()
                }
                _scope_sel = (
                    "COALESCE(scope_type, 'global'), scope_id"
                    if {"scope_type", "scope_id"} <= _legacy_cols
                    else "'global', NULL"
                )
                with store_migrations.atomic_migration(conn):
                    conn.execute(
                        "INSERT OR IGNORE INTO rbac_user_roles (user_id, role_id, "
                        "granted_at, scope_type, scope_id, authored_by_user_id, "
                        "authored_by_rank, expires_at) "
                        "SELECT user_id, role_id, granted_at, " + _scope_sel + ", "
                        "COALESCE(authored_by_user_id, ''), COALESCE(authored_by_rank, 0), "
                        "expires_at FROM rbac_user_roles_legacy_v1"
                    )
                    conn.execute("DROP TABLE rbac_user_roles_legacy_v1")

            # rbac_user_roles PK widening (2026-04-21). Legacy PK was
            # (user_id, role_id); corpo model needs scope on the key so
            # a user can hold role X at global AND at project P with
            # different expiries.
            #
            # #817 SECURITY FIX (2026-08-18): the shape is READ FROM THE
            # SCHEMA, never probed by inserting a throwaway row. The old probe
            # inserted role_id='__probe__' — a value with no rbac_roles parent
            # — and #746/#755 made every canonical connection run
            # `PRAGMA foreign_keys = ON`, so that INSERT began raising
            # IntegrityError("FOREIGN KEY constraint failed") UNCONDITIONALLY.
            # The `except sqlite3.IntegrityError` arm read that as "old-shape
            # PK" and re-ran the rename→create→copy migration on EVERY
            # init_db(seed_permissions=...) call — and the copy rewrote every
            # surviving row as ('global', NULL). Result: each seed_rbac()
            # silently PROMOTED every PROJECT- and SESSION-scoped grant to a
            # machine-wide GLOBAL grant, so a grant minted in the source
            # project authorized the target project too (the confused-deputy
            # leak pinned by tests/security/
            # test_cross_project_session_ownership.py). A schema read cannot
            # confuse a constraint violation with a schema version.
            _pk_cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(rbac_user_roles)").fetchall()
                if int(r[5] or 0) > 0
            }
            if {"scope_type", "scope_id"} - _pk_cols:
                # Old-shape PK. #243: the whole rename→create→copy→drop runs
                # under ONE transaction (store_migrations.atomic_migration) — a
                # kill rolls back to the intact legacy table instead of stranding
                # rows in rbac_user_roles_legacy_v1 with an empty new table.
                # Only single-statement conn.execute inside the block —
                # executescript would auto-commit and defeat the atomicity.
                # Historic '__probe__' rows (written by the pre-#817 probe on
                # connections that had foreign_keys OFF) are cleared first so
                # the migration never carries them forward.
                conn.execute("DELETE FROM rbac_user_roles WHERE user_id = '__probe__'")
                conn.commit()
                with store_migrations.atomic_migration(conn):
                    conn.execute("ALTER TABLE rbac_user_roles RENAME TO rbac_user_roles_legacy_v1")
                    conn.execute("""
                        CREATE TABLE rbac_user_roles (
                            user_id TEXT NOT NULL,
                            role_id TEXT NOT NULL,
                            granted_at TEXT NOT NULL,
                            scope_type TEXT NOT NULL DEFAULT 'global',
                            scope_id TEXT,
                            authored_by_user_id TEXT NOT NULL DEFAULT '',
                            authored_by_rank INTEGER NOT NULL DEFAULT 0,
                            expires_at TEXT,
                            PRIMARY KEY (user_id, role_id, scope_type, scope_id),
                            FOREIGN KEY (role_id) REFERENCES rbac_roles(role_id)
                                ON DELETE CASCADE
                        )
                    """)
                    # #817: carry the rows' OWN scope across. A genuinely
                    # pre-corpo table has just received scope_type/scope_id via
                    # the additive ALTERs above, so every row already reads
                    # ('global', NULL) and this is identical to the old
                    # hardcoded copy — but it can never WIDEN a narrow grant if
                    # this branch is ever reached with scoped rows present.
                    conn.execute("""
                        INSERT INTO rbac_user_roles (user_id, role_id, granted_at,
                            scope_type, scope_id, authored_by_user_id,
                            authored_by_rank, expires_at)
                        SELECT user_id, role_id, granted_at,
                            COALESCE(scope_type, 'global'), scope_id,
                            COALESCE(authored_by_user_id, ''),
                            COALESCE(authored_by_rank, 0),
                            expires_at
                        FROM rbac_user_roles_legacy_v1
                    """)
                    conn.execute("DROP TABLE rbac_user_roles_legacy_v1")
                    conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_rbac_user_roles_user
                            ON rbac_user_roles(user_id)
                    """)
                    conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_rbac_user_roles_scope
                            ON rbac_user_roles(scope_type, scope_id)
                    """)

            # Scoped role-permission overrides + user-permission
            # overrides. These are corpo-model adds — dental's model
            # calls them ScopedRolePermission + UserPermissionOverride.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rbac_scoped_role_permissions (
                    role_id TEXT NOT NULL,
                    permission_name TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT,
                    is_granted INTEGER NOT NULL,
                    authored_by_user_id TEXT NOT NULL DEFAULT '',
                    authored_at TEXT NOT NULL DEFAULT '',
                    authored_by_rank INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    PRIMARY KEY (role_id, permission_name, scope_type, scope_id)
                );
                CREATE INDEX IF NOT EXISTS idx_rbac_scoped_rp_role
                    ON rbac_scoped_role_permissions(role_id);
                CREATE INDEX IF NOT EXISTS idx_rbac_scoped_rp_scope
                    ON rbac_scoped_role_permissions(scope_type, scope_id);

                CREATE TABLE IF NOT EXISTS rbac_user_permission_overrides (
                    user_id TEXT NOT NULL,
                    permission_name TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT,
                    is_granted INTEGER NOT NULL,
                    authored_by_user_id TEXT NOT NULL DEFAULT '',
                    authored_at TEXT NOT NULL DEFAULT '',
                    authored_by_rank INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    PRIMARY KEY (user_id, permission_name, scope_type, scope_id)
                );
                CREATE INDEX IF NOT EXISTS idx_rbac_user_perm_overrides_user
                    ON rbac_user_permission_overrides(user_id);
                CREATE INDEX IF NOT EXISTS idx_rbac_user_perm_overrides_scope
                    ON rbac_user_permission_overrides(scope_type, scope_id);

                -- Version counters for cache invalidation. Writers bump
                -- the appropriate row; readers check the counter to
                -- decide if cached resolutions are still valid. One
                -- row per (user_id, scope_type, scope_id) or ('*', ...)
                -- for role-level changes. Self-invalidating on write.
                CREATE TABLE IF NOT EXISTS rbac_cache_version (
                    scope_key TEXT PRIMARY KEY,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
            """)
            if seed_permissions:
                conn.executemany(
                    "INSERT OR IGNORE INTO rbac_permissions "
                    "(permission_name, description) VALUES (?, ?)",
                    [(p.name, p.description) for p in seed_permissions],
                )
            conn.commit()
        # #488: adopt this project's pre-global authority rows once (empty
        # tables only). Runs after the schema exists so the import has
        # somewhere to land; no-op once the global home is populated.
        from .identity_db import adopt_legacy_project_identity

        adopt_legacy_project_identity(project_root)
        # Schema (and the one-shot adoption above) are settled for this file in
        # this process -- see the guard at the top of this method.
        _mark_schema_ensured(path, "rbac")

    # ── roles ──

    def create_role(
        self,
        project_root: Path,
        name: str,
        description: str = "",
        is_system: bool = False,
        *,
        rank: int = 500,
        inherits_from_role_key: str | None = None,
        created_by_user_id: str = "",
    ) -> Role:
        name = str(name or "").strip()
        if not name:
            raise ValueError("role name is required")
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"role name must be alphanumeric + _/-: {name!r}")
        # Circular-inheritance check. Cheap — depth rarely > 3 in
        # practice, and we walk the chain at write time so resolution
        # doesn't have to.
        if inherits_from_role_key:
            self._assert_no_inheritance_cycle(
                project_root,
                child_name=name,
                parent_name=inherits_from_role_key,
            )
        self.init_db(project_root)
        role_id = "r_" + secrets.token_hex(8)
        now = _iso_now()
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            try:
                conn.execute(
                    "INSERT INTO rbac_roles "
                    "(role_id, name, description, is_system, created_at, "
                    "rank, inherits_from_role_key, created_by_user_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        role_id,
                        name,
                        description,
                        1 if is_system else 0,
                        now,
                        int(rank),
                        inherits_from_role_key or None,
                        created_by_user_id or "",
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"role name exists: {name}") from exc
        self._bump_cache_version(project_root, "role:" + name)
        return Role(
            role_id=role_id,
            name=name,
            description=description,
            is_system=is_system,
            created_at=now,
            rank=int(rank),
            inherits_from_role_key=inherits_from_role_key or None,
            created_by_user_id=created_by_user_id or "",
        )

    def _assert_no_inheritance_cycle(
        self,
        project_root: Path,
        *,
        child_name: str,
        parent_name: str,
    ) -> None:
        """Walk the parent chain from parent_name upward; fail if we hit
        child_name anywhere. Prevents cycles like A→B→A.
        """
        self.init_db(project_root)
        visited: set[str] = {child_name}
        current = parent_name
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            while current:
                if current in visited:
                    raise ValueError(f"inheritance cycle: {child_name} → ... → {current}")
                visited.add(current)
                row = conn.execute(
                    "SELECT inherits_from_role_key FROM rbac_roles WHERE name = ?",
                    (current,),
                ).fetchone()
                if row is None:
                    # Parent doesn't exist yet — that's the caller's
                    # problem to validate; we only guard cycles here.
                    return
                current = row[0]

    def _bump_cache_version(
        self,
        project_root: Path,
        scope_key: str,
    ) -> None:
        """Increment (or insert) the cache version counter for
        `scope_key`. Writers call this to invalidate readers. Readers
        include the counter in their cache key — on mismatch, recompute.
        """
        self.init_db(project_root)
        now = _iso_now()
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.execute(
                "INSERT INTO rbac_cache_version (scope_key, version, updated_at) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(scope_key) DO UPDATE SET "
                "version = version + 1, updated_at = excluded.updated_at",
                (scope_key, now),
            )
            conn.commit()

    def _row_to_role(self, row: sqlite3.Row) -> Role:
        """Handle both pre-migration rows (missing rank/inherits/created_by
        columns) and post-migration rows. sqlite3.Row.keys() enumerates
        whatever the SELECT pulled.
        """
        keys = set(row.keys())
        return Role(
            role_id=row["role_id"],
            name=row["name"],
            description=row["description"],
            is_system=bool(row["is_system"]),
            created_at=row["created_at"],
            rank=int(row["rank"]) if "rank" in keys and row["rank"] is not None else 500,
            inherits_from_role_key=(
                row["inherits_from_role_key"] if "inherits_from_role_key" in keys else None
            ),
            created_by_user_id=(
                row["created_by_user_id"]
                if "created_by_user_id" in keys and row["created_by_user_id"]
                else ""
            ),
        )

    def get_role_by_name(
        self,
        project_root: Path,
        name: str,
    ) -> Role | None:
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT role_id, name, description, is_system, created_at, "
                "rank, inherits_from_role_key, created_by_user_id "
                "FROM rbac_roles WHERE name = ?",
                (str(name).strip(),),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_role(row)

    def list_roles(self, project_root: Path) -> list[Role]:
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT role_id, name, description, is_system, created_at, "
                "rank, inherits_from_role_key, created_by_user_id "
                "FROM rbac_roles ORDER BY rank ASC, name ASC",
            ).fetchall()
        return [self._row_to_role(r) for r in rows]

    def delete_role(self, project_root: Path, role_id: str) -> bool:
        """Drop a role + all its bindings. Refuses system roles — they
        are the audit spine and can't be removed without code change.
        """
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT is_system FROM rbac_roles WHERE role_id = ?",
                (role_id,),
            ).fetchone()
            if row is None:
                return False
            if row["is_system"]:
                raise ValueError(f"cannot delete system role: {role_id}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "DELETE FROM rbac_roles WHERE role_id = ?",
                (role_id,),
            )
            conn.commit()
        return True

    # ── permissions ──

    def set_role_permissions(
        self,
        project_root: Path,
        role_id: str,
        permissions: Iterable[str],
    ) -> None:
        """Replace the entire permission set for a role. Atomic."""
        self.init_db(project_root)
        perms = [str(p).strip() for p in permissions if str(p).strip()]
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            # Validate every permission exists in the catalog.
            if perms:
                placeholders = ",".join("?" for _ in perms)
                known = {
                    r[0]
                    for r in conn.execute(
                        f"SELECT permission_name FROM rbac_permissions "
                        f"WHERE permission_name IN ({placeholders})",
                        perms,
                    ).fetchall()
                }
                unknown = [p for p in perms if p not in known]
                if unknown:
                    raise ValueError(f"unknown permissions (not in catalog): {unknown}")
            conn.execute(
                "DELETE FROM rbac_role_permissions WHERE role_id = ?",
                (role_id,),
            )
            conn.executemany(
                "INSERT INTO rbac_role_permissions (role_id, permission_name) VALUES (?, ?)",
                [(role_id, p) for p in perms],
            )
            conn.commit()

    def get_role_permissions(
        self,
        project_root: Path,
        role_id: str,
    ) -> frozenset[str]:
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            rows = conn.execute(
                "SELECT permission_name FROM rbac_role_permissions WHERE role_id = ?",
                (role_id,),
            ).fetchall()
        return frozenset(r[0] for r in rows)

    def role_permission_counts(self, project_root: Path) -> dict[str, int]:
        """Bulk count of permissions per role — one query, one connection.

        Used by dashboard_rbac to avoid the per-role get_role_permissions
        round-trips (N+1) that opened N fresh sqlite connections just to
        compute permission_count for a table column. Returns a dict keyed by
        role_id; roles with no rbac_role_permissions rows are absent (caller
        uses dict.get(rid, 0)).
        """
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            rows = conn.execute(
                "SELECT role_id, COUNT(*) FROM rbac_role_permissions GROUP BY role_id",
            ).fetchall()
        return {str(r[0]): int(r[1] or 0) for r in rows}

    def list_permissions(self, project_root: Path) -> list[Permission]:
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT permission_name, description FROM rbac_permissions "
                "ORDER BY permission_name ASC",
            ).fetchall()
        return [Permission(name=r["permission_name"], description=r["description"]) for r in rows]

    # ── user bindings ──

    def assign_role_to_user(
        self,
        project_root: Path,
        user_id: str,
        role_id: str,
    ) -> bool:
        """Back-compat shim — assigns at global scope. New callers
        should use assign_role_to_user_scoped.
        """
        return self.assign_role_to_user_scoped(
            project_root,
            user_id,
            role_id,
            scope_type="global",
            scope_id=None,
        )

    def assign_role_to_user_scoped(
        self,
        project_root: Path,
        user_id: str,
        role_id: str,
        *,
        scope_type: str = "global",
        scope_id: str | None = None,
        authored_by_user_id: str = "",
        authored_by_rank: int = 0,
        expires_at: str | None = None,
    ) -> bool:
        """Grant a user a role at a specific scope.

        Idempotent — INSERT OR IGNORE on the (user_id, role_id,
        scope_type, scope_id) key. Returns True iff a new row was
        created. Rank-gate is enforced at write: refuses if the author
        lacks authority over the target role (authored_by_rank >
        target_role_rank).
        """
        if scope_type not in ("global", "project", "session"):
            raise ValueError(f"invalid scope_type: {scope_type!r} (must be global|project|session)")
        if scope_type != "global" and not scope_id:
            raise ValueError(f"scope_type={scope_type!r} requires a non-empty scope_id")
        self.init_db(project_root)

        # Rank gate: author must have stronger authority than target role.
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            target = conn.execute(
                "SELECT rank FROM rbac_roles WHERE role_id = ?",
                (role_id,),
            ).fetchone()
            if target is None:
                raise ValueError(f"role not found: {role_id}")
            target_rank = int(target["rank"] or 500)
            if authored_by_rank > target_rank and authored_by_user_id not in (
                "",
                "__seed__",
                "__bootstrap__",
            ):
                raise ValueError(
                    f"rank gate: author rank {authored_by_rank} cannot "
                    f"grant role at rank {target_rank} (stronger needed)",
                )

        now = _iso_now()
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO rbac_user_roles "
                "(user_id, role_id, granted_at, scope_type, scope_id, "
                "authored_by_user_id, authored_by_rank, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    role_id,
                    now,
                    scope_type,
                    scope_id,
                    authored_by_user_id,
                    int(authored_by_rank),
                    expires_at,
                ),
            )
            conn.commit()
        self._bump_cache_version(
            project_root,
            f"user:{user_id}:{scope_type}:{scope_id or ''}",
        )
        return cur.rowcount > 0

    def assign_role_to_user_by_name(
        self,
        project_root: Path,
        user_id: str,
        role_name: str,
        *,
        authored_by_user_id: str = "__bootstrap__",
    ) -> bool:
        """Assign the role whose NAME matches ``role_name`` to a user, at global
        scope, idempotently (#434 identity↔RBAC reconciliation).

        The Identity context (``identity_users.role``) and the RBAC context
        (``rbac_user_roles``) are separate bounded contexts that were never kept
        in sync: ``create_user`` wrote an identity role but never created the
        matching RBAC grant, so a freshly-provisioned operator authenticated fine
        yet held ZERO permissions (the role-less-owner lockout). Login reconciles
        the two through this method.

        Resolves the role by name; a name with no defined role is a no-op
        (returns False) rather than an error. Authored as ``__bootstrap__`` so
        the rank-gate permits the reconciliation (the same first-operator
        provisioning path already sanctioned in assign_role_to_user_scoped).
        Returns True iff a new grant row was created.
        """
        role = self.get_role_by_name(project_root, role_name)
        if role is None:
            return False
        # Idempotency guard: global grants carry a NULL scope_id, and SQLite
        # treats NULLs as DISTINCT in a UNIQUE index, so assign_role_to_user_
        # scoped's INSERT OR IGNORE would NOT dedup a repeated global grant.
        # Login reconciles on EVERY sign-in, so check-before-insert to avoid
        # accumulating duplicate rows.
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            already = conn.execute(
                "SELECT 1 FROM rbac_user_roles WHERE user_id = ? AND role_id = ? "
                "AND scope_type = 'global'",
                (user_id, role.role_id),
            ).fetchone()
        if already is not None:
            return False
        return self.assign_role_to_user_scoped(
            project_root,
            user_id,
            role.role_id,
            scope_type="global",
            authored_by_user_id=authored_by_user_id,
            authored_by_rank=0,
        )

    def revoke_role_from_user(
        self,
        project_root: Path,
        user_id: str,
        role_id: str,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> bool:
        """Revoke a role binding. When scope_type is None, revokes at
        EVERY scope for this (user_id, role_id) pair (legacy behavior).
        When scope_type is set, revokes only that specific scope.
        """
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            if scope_type is None:
                cur = conn.execute(
                    "DELETE FROM rbac_user_roles WHERE user_id = ? AND role_id = ?",
                    (user_id, role_id),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM rbac_user_roles "
                    "WHERE user_id = ? AND role_id = ? "
                    "AND scope_type = ? "
                    "AND (scope_id IS ? OR scope_id = ?)",
                    (user_id, role_id, scope_type, scope_id, scope_id),
                )
            conn.commit()
        self._bump_cache_version(
            project_root,
            f"user:{user_id}:*",
        )
        return cur.rowcount > 0

    # ── Scoped overrides (2026-04-21) ──

    def revoke_scope(
        self,
        project_root: Path,
        *,
        scope_type: str,
        scope_id: str,
    ) -> int:
        """Revoke EVERY role binding granted at one scope. Returns the count.

        Called when the scope itself ceases to exist — today, when a session is
        deleted. ``dashboard-create-session`` mints a session-scoped
        ``session_owner`` role for the creator, and
        ``dashboard-delete-session`` used to touch RBAC not at all, so the
        binding outlived the session it described.

        WHY THIS IS A SECURITY FIX AND NOT HOUSEKEEPING: session ids are
        OPERATOR-CHOSEN (``--session <id>``, names like "phoenix"), not generated,
        so an id can be reused. A surviving binding means recreating a session
        under a previously-used id silently hands ownership to whoever owned the
        deleted one — a grant nobody issued.

        Per-binding revocation via ``revoke_role_from_user`` rather than one bulk
        DELETE: each removal goes through the same audited path an explicit
        revocation uses, and the per-user cache-version bump fires for every
        affected user instead of once for a wildcard.

        Raises ValueError on an empty scope_id. A missing scope must NEVER be
        read as "every scope" — that would strip every binding in the project,
        and the difference between "this session" and "all sessions" cannot be
        left to a falsy default.
        """
        scope_type = str(scope_type or "").strip()
        scope_id = str(scope_id or "").strip()
        if not scope_type:
            raise ValueError("scope_type is required to revoke a scope")
        if not scope_id:
            raise ValueError(
                "scope_id is required to revoke a scope — refusing to treat an "
                "empty scope as a wildcard over every binding",
            )
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT user_id, role_id FROM rbac_user_roles "
                "WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            ).fetchall()
        revoked = 0
        for row in rows:
            if self.revoke_role_from_user(
                project_root,
                str(row["user_id"]),
                str(row["role_id"]),
                scope_type=scope_type,
                scope_id=scope_id,
            ):
                revoked += 1
        return revoked

    def set_scoped_role_permission(
        self,
        project_root: Path,
        role_id: str,
        permission_name: str,
        *,
        scope_type: str,
        scope_id: str | None,
        is_granted: bool,
        authored_by_user_id: str = "",
        authored_by_rank: int = 0,
        expires_at: str | None = None,
    ) -> None:
        """Write a scoped role-permission override. Upsert on
        (role_id, permission_name, scope_type, scope_id).
        """
        if scope_type not in ("global", "project", "session"):
            raise ValueError(f"invalid scope_type: {scope_type!r}")
        self.init_db(project_root)
        # Catalog-presence check mirrors set_role_permissions.
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            known = conn.execute(
                "SELECT 1 FROM rbac_permissions WHERE permission_name = ?",
                (permission_name,),
            ).fetchone()
            if known is None:
                raise ValueError(f"unknown permission (not in catalog): {permission_name}")
            conn.execute(
                "INSERT INTO rbac_scoped_role_permissions "
                "(role_id, permission_name, scope_type, scope_id, "
                "is_granted, authored_by_user_id, authored_at, "
                "authored_by_rank, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(role_id, permission_name, scope_type, scope_id) "
                "DO UPDATE SET "
                "is_granted = excluded.is_granted, "
                "authored_by_user_id = excluded.authored_by_user_id, "
                "authored_at = excluded.authored_at, "
                "authored_by_rank = excluded.authored_by_rank, "
                "expires_at = excluded.expires_at",
                (
                    role_id,
                    permission_name,
                    scope_type,
                    scope_id,
                    1 if is_granted else 0,
                    authored_by_user_id,
                    _iso_now(),
                    int(authored_by_rank),
                    expires_at,
                ),
            )
            conn.commit()
        self._bump_cache_version(project_root, f"role:{role_id}")

    def set_user_permission_override(
        self,
        project_root: Path,
        user_id: str,
        permission_name: str,
        *,
        scope_type: str,
        scope_id: str | None,
        is_granted: bool,
        authored_by_user_id: str = "",
        authored_by_rank: int = 0,
        expires_at: str | None = None,
    ) -> None:
        """Write a user-specific permission override. Beats role
        grants at the same scope. Rank gate enforced at resolution
        time so both write-time and read-time defenses exist.
        """
        if scope_type not in ("global", "project", "session"):
            raise ValueError(f"invalid scope_type: {scope_type!r}")
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            known = conn.execute(
                "SELECT 1 FROM rbac_permissions WHERE permission_name = ?",
                (permission_name,),
            ).fetchone()
            if known is None:
                raise ValueError(f"unknown permission (not in catalog): {permission_name}")
            conn.execute(
                "INSERT INTO rbac_user_permission_overrides "
                "(user_id, permission_name, scope_type, scope_id, "
                "is_granted, authored_by_user_id, authored_at, "
                "authored_by_rank, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, permission_name, scope_type, scope_id) "
                "DO UPDATE SET "
                "is_granted = excluded.is_granted, "
                "authored_by_user_id = excluded.authored_by_user_id, "
                "authored_at = excluded.authored_at, "
                "authored_by_rank = excluded.authored_by_rank, "
                "expires_at = excluded.expires_at",
                (
                    user_id,
                    permission_name,
                    scope_type,
                    scope_id,
                    1 if is_granted else 0,
                    authored_by_user_id,
                    _iso_now(),
                    int(authored_by_rank),
                    expires_at,
                ),
            )
            conn.commit()
        self._bump_cache_version(
            project_root,
            f"user:{user_id}:{scope_type}:{scope_id or ''}",
        )

    def get_user_permissions(
        self,
        project_root: Path,
        user_id: str,
    ) -> UserPermissions:
        """Flatten user → roles → permissions into one set. Single
        source of truth for every enforcement call.
        """
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            role_rows = conn.execute(
                "SELECT r.role_id, r.name FROM rbac_user_roles ur "
                "JOIN rbac_roles r ON r.role_id = ur.role_id "
                "WHERE ur.user_id = ? "
                "ORDER BY r.name ASC",
                (user_id,),
            ).fetchall()
            role_ids = [r["role_id"] for r in role_rows]
            role_names = tuple(r["name"] for r in role_rows)
            perms: set[str] = set()
            if role_ids:
                placeholders = ",".join("?" for _ in role_ids)
                perm_rows = conn.execute(
                    f"SELECT DISTINCT permission_name "
                    f"FROM rbac_role_permissions "
                    f"WHERE role_id IN ({placeholders})",
                    role_ids,
                ).fetchall()
                perms.update(r[0] for r in perm_rows)
        return UserPermissions(
            user_id=user_id,
            roles=role_names,
            permissions=frozenset(perms),
        )

    def user_has_permission(
        self,
        project_root: Path,
        user_id: str,
        permission: str,
    ) -> bool:
        """Back-compat shim — global-scope only. New callers should use
        has_permission(user_id, permission, scope_type, scope_id).
        """
        return self.has_permission(
            project_root,
            user_id,
            permission,
            scope_type="global",
            scope_id=None,
        )

    # ── Corpo-model permission resolution (2026-04-21) ──
    #
    # Three-state grants (granted / denied / unspecified). Scope chain
    # session > project > global with narrower-wins semantics. Role
    # inheritance via inherits_from_role_key (parent perms merged first,
    # child overrides). User-permission-overrides applied last and beat
    # role grants at the same scope. Expires_at filter drops stale rows.
    # Rank gate: overrides whose authored_by_rank >= effective user rank
    # are dropped at resolution time (defense-in-depth on top of
    # write-time check).
    #
    # Fail-loud on empty seed: if rbac_permissions table is empty AND
    # the permission is in the hardcoded catalog, raise rather than
    # silently granting defaults. A half-seeded prod db giving users
    # unexpected grants is the vulnerability class we're designing out.

    def has_permission(
        self,
        project_root: Path,
        user_id: str,
        permission: str,
        *,
        scope_type: str = "global",
        scope_id: str | None = None,
    ) -> bool:
        """Authoritative permission check with full corpo semantics.

        Returns True iff the user's effective permission set (after
        resolving roles, inheritance, scoped overrides, user overrides,
        expiry, and rank gate) includes `permission` at the requested
        scope.

        DEV_MODE bypass still honored (env var + config setting).
        """
        if _rbac_dev_mode_active(project_root):
            return True
        effective = self.effective_permissions(
            project_root,
            user_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        return permission in effective

    def _session_owning_project(self, project_root: Path, session_id: str) -> str | None:
        """The PROJECT SCOPE KEY of the project that owns ``session_id``,
        or None when ownership cannot be established (#518).

        A session belongs to exactly one project, and ``session_membership``
        is the canonical record of that — the very table
        ``project_authority.session_belongs`` reads, so ownership has ONE
        spelling in the codebase rather than two that can drift. A bare
        SESSION.md folder is not ownership; only a membership row is.

        Returns None — never a project key — on any failure, so a missing
        row, a locked store, or an import error makes the caller REFUSE
        instead of widening a project grant onto an unknown session.
        """
        sid = (session_id or "").strip()
        if not sid:
            return None
        try:
            from .project_authority import project_scope_key
            from .session_membership_store import SessionMembershipStore

            if not SessionMembershipStore().is_member(project_root, sid):
                return None
            return project_scope_key(project_root)
        except Exception:
            return None

    def effective_permissions(
        self,
        project_root: Path,
        user_id: str,
        *,
        scope_type: str = "global",
        scope_id: str | None = None,
    ) -> frozenset[str]:
        """Compute the user's effective permission set at a scope.

        Resolution order:
          1. Platform_admin short-circuit — returns the full catalog.
             (Checked via identity_store; skipped if store unavailable.)
          2. Collect user's role assignments at scope-chain (global +
             project + session in order), filtering expired rows.
          3. For each role: merge inherited parent roles' permissions
             (walking inherits_from_role_key chain), then apply the
             role's own rbac_role_permissions (is_granted=0 removes).
          4. Apply rbac_scoped_role_permissions in scope-chain order
             (global first, then project, then session). Narrower wins.
          5. Apply rbac_user_permission_overrides in same order. User
             overrides beat role grants at the same scope.
          6. Drop rows whose authored_by_rank >= effective_user_rank
             AND whose author isn't platform_admin (rank gate).
          7. Fail loud if rbac_permissions catalog is empty AND any
             role assignment exists — half-seeded state is a bug, not
             a silent-grant scenario.
        """
        self.init_db(project_root)

        # Scope chain — broader → narrower. Narrower wins at merge time.
        chain: list[tuple[str, str | None]] = [("global", None)]
        if scope_type == "project" and scope_id:
            chain.append(("project", scope_id))
        elif scope_type == "session" and scope_id:
            # Session scope implies a project scope too (the project
            # the session lives in). Caller passes scope_id=session_id;
            # we don't resolve the project here because sessions only
            # live under one project.
            chain.append(("session", scope_id))

        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row

            # Fail-loud catalog sanity.
            perm_count = conn.execute("SELECT COUNT(*) FROM rbac_permissions").fetchone()[0]

            # Step 1: role assignments in-scope + not expired.
            now = _iso_now()
            role_rows = conn.execute(
                "SELECT ur.role_id, ur.scope_type, ur.scope_id, "
                "r.name, r.rank, r.inherits_from_role_key "
                "FROM rbac_user_roles ur "
                "JOIN rbac_roles r ON r.role_id = ur.role_id "
                "WHERE ur.user_id = ? "
                "AND (ur.expires_at IS NULL OR ur.expires_at > ?)",
                (user_id, now),
            ).fetchall()

            if not role_rows:
                return frozenset()

            if perm_count == 0 and role_rows:
                raise RuntimeError(
                    "rbac_permissions catalog is empty but user role "
                    "assignments exist. Half-seeded database; refusing "
                    "to silently grant default permissions. Run "
                    "permission_catalog.seed_rbac(project_root) to repair.",
                )

            # Filter role assignments to those valid at the requested
            # scope. A role assigned at broader scope (global) applies
            # to every narrower lookup; a role assigned at narrower
            # scope (session) applies only to that scope.
            # #518 SECURITY FIX: a PROJECT-scoped grant widens into a
            # SESSION-scope lookup ONLY when it names the project that
            # OWNS that session. The pre-fix rule admitted ANY project
            # grant for ANY session question — `scope_id == assign_id or
            # scope_type == "session"` never compared the assignment's own
            # scope_id on the session arm — so a grant on project Q
            # authorized a session of project P and the ladder
            # (global > project > session) silently skipped a rung.
            #
            # Ownership is resolved ONCE per call and FAILS CLOSED: an
            # unresolvable session inherits nothing.
            owning_project: str | None = None
            if scope_type == "session" and scope_id:
                owning_project = self._session_owning_project(project_root, scope_id)

            def _assignment_applies(assign_scope: str, assign_id: str | None) -> bool:
                if assign_scope == "global":
                    return True
                if assign_scope == "project":
                    if scope_type == "project":
                        return bool(scope_id) and scope_id == assign_id
                    if scope_type == "session":
                        # scope_id for a session lookup IS the session_id,
                        # so the assignment is matched against the session's
                        # OWNING project, never against the session id.
                        return owning_project is not None and owning_project == assign_id
                    return False
                if assign_scope == "session":
                    return scope_type == "session" and scope_id == assign_id
                return False

            active_roles: list[sqlite3.Row] = [
                r for r in role_rows if _assignment_applies(r["scope_type"], r["scope_id"])
            ]
            if not active_roles:
                return frozenset()

            # Effective rank = min (best) rank across active roles.
            effective_rank = min(int(r["rank"] or 500) for r in active_roles)

            # Step 2: walk inheritance for each role, collect base perms.
            # Three-state tracking: +granted moves key into set,
            # -denied removes, unspecified does nothing.
            granted: set[str] = set()
            denied: set[str] = set()

            def _apply_role(role_id: str, role_name: str) -> None:
                """Merge a single role's rbac_role_permissions rows
                into the state machine, respecting is_granted tri-state
                + expires_at filter.
                """
                rp_rows = conn.execute(
                    "SELECT permission_name, is_granted, expires_at "
                    "FROM rbac_role_permissions "
                    "WHERE role_id = ?",
                    (role_id,),
                ).fetchall()
                for rp in rp_rows:
                    if rp["expires_at"] and rp["expires_at"] <= now:
                        continue
                    if rp["is_granted"] == 1:
                        granted.add(rp["permission_name"])
                        denied.discard(rp["permission_name"])
                    elif rp["is_granted"] == 0:
                        denied.add(rp["permission_name"])
                        granted.discard(rp["permission_name"])

            def _walk_inheritance(role_name: str) -> list[tuple[str, str]]:
                """Return (role_id, role_name) pairs from root parent
                down to this role, so parent perms are applied first.
                """
                order: list[tuple[str, str]] = []
                visited: set[str] = set()
                cur = role_name
                while cur and cur not in visited:
                    visited.add(cur)
                    row = conn.execute(
                        "SELECT role_id, inherits_from_role_key FROM rbac_roles WHERE name = ?",
                        (cur,),
                    ).fetchone()
                    if row is None:
                        break
                    order.append((row["role_id"], cur))
                    cur = row["inherits_from_role_key"]
                return list(reversed(order))  # root → leaf

            for r in active_roles:
                for rid, rname in _walk_inheritance(r["name"]):
                    _apply_role(rid, rname)

            # Step 3: scoped_role_permissions applied in chain order.
            for chain_scope, chain_id in chain:
                role_ids = [r["role_id"] for r in active_roles]
                if not role_ids:
                    continue
                placeholders = ",".join("?" for _ in role_ids)
                srp_rows = conn.execute(
                    f"SELECT permission_name, is_granted, authored_by_rank, "
                    f"expires_at FROM rbac_scoped_role_permissions "
                    f"WHERE role_id IN ({placeholders}) "
                    f"AND scope_type = ? "
                    f"AND (scope_id IS ? OR scope_id = ?) "
                    f"AND (expires_at IS NULL OR expires_at > ?)",
                    [*role_ids, chain_scope, chain_id, chain_id, now],
                ).fetchall()
                for srp in srp_rows:
                    # Rank gate: author must have had rank ≤ effective.
                    # 0 = system seed (always valid). Others: must be
                    # stricter than target user's current effective rank.
                    author_rank = int(srp["authored_by_rank"] or 0)
                    if author_rank > effective_rank:
                        continue
                    if srp["is_granted"] == 1:
                        granted.add(srp["permission_name"])
                        denied.discard(srp["permission_name"])
                    elif srp["is_granted"] == 0:
                        denied.add(srp["permission_name"])
                        granted.discard(srp["permission_name"])

            # Step 4: user_permission_overrides applied last.
            for chain_scope, chain_id in chain:
                upo_rows = conn.execute(
                    "SELECT permission_name, is_granted, authored_by_rank, "
                    "expires_at FROM rbac_user_permission_overrides "
                    "WHERE user_id = ? AND scope_type = ? "
                    "AND (scope_id IS ? OR scope_id = ?) "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (user_id, chain_scope, chain_id, chain_id, now),
                ).fetchall()
                for upo in upo_rows:
                    author_rank = int(upo["authored_by_rank"] or 0)
                    if author_rank > effective_rank:
                        continue
                    if upo["is_granted"] == 1:
                        granted.add(upo["permission_name"])
                        denied.discard(upo["permission_name"])
                    elif upo["is_granted"] == 0:
                        denied.add(upo["permission_name"])
                        granted.discard(upo["permission_name"])

            return frozenset(granted - denied)


def _rbac_dev_mode_active(project_root: Path) -> bool:
    """Two-source check: env var wins (fastest path), then config
    setting. Fail closed — any error returns False so a broken
    config can't silently disable RBAC.
    """
    import os

    env = os.environ.get("AIDOCS_RBAC_DEV_MODE", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    # #404: the `rbac.dev_mode_bypass` config escape is removed — only
    # the explicit env var above (deployment/test harness) is consulted.
    return False


def _iso_now() -> str:
    from datetime import datetime

    return datetime.fromtimestamp(
        time.time(),
        tz=UTC,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
