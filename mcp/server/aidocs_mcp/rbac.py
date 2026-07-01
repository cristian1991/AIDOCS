"""Role-based access control — users, roles, and permissions.

Three built-in roles:
    admin    — full access (config, security settings, user management)
    operator — normal work (edit code, run tools, manage sessions)
    viewer   — read-only (view code, read sessions, no edits)

Permissions are checked at the MCP tool level. Each tool maps to one or more
required permissions. Users without the required permission get a clear error.

Storage: SQLite tables in .MEMORY/.index/aidocs.sqlite3

Designed for future multi-tenant use but works single-user too — when no users
exist, all operations are allowed (backward compatible).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# ── Permissions ──

PERMISSIONS = {
    # Code operations
    "code.read": "Read code files and indexes",
    "code.edit": "Edit code files",
    "code.create": "Create new files",
    "code.delete": "Delete files",
    # Session operations
    "session.read": "Read sessions, journals, handoffs",
    "session.create": "Create new sessions",
    "session.edit": "Update session state, context, handoff",
    "session.claim": "Claim/release sessions",
    # Config operations
    "config.read": "Read settings and config",
    "config.edit": "Change project settings",
    "config.security": "Change security settings (dev_mode, security.enforce)",
    # Hard-protected DATA files (sqlite/index/gate-state). Holder may declare a
    # file hard-protected AND unlock the non-sqlite ones for edit. Admin-only by
    # default; operator/viewer must escalate. sqlite stays config_set-only even
    # for the holder. See hard_protected_paths.py.
    "security.hard_protected": "Declare/unlock hard-protected data files",
    # Memory operations
    "memory.read": "Read memory files",
    "memory.write": "Write to memory (rules, domains, roadmaps)",
    # Conductor operations
    "conductor.view": "View conductor status and lanes",
    "conductor.control": "Start/stop/pause conductor, dispatch lanes",
    # Admin operations
    "admin.users": "Manage users and roles",
    "admin.audit": "View audit trail",
    # Tool operations
    "tools.bash": "Execute bash commands",
    "tools.agent": "Spawn sub-agents",
}

# ── Built-in roles ──

BUILTIN_ROLES: dict[str, dict[str, object]] = {
    "admin": {
        "description": "Full access — config, security, user management",
        "permissions": list(PERMISSIONS.keys()),
    },
    "operator": {
        "description": "Normal work — edit code, run tools, manage sessions",
        "permissions": [
            "code.read",
            "code.edit",
            "code.create",
            "session.read",
            "session.create",
            "session.edit",
            "session.claim",
            "config.read",
            "config.edit",
            "memory.read",
            "memory.write",
            "conductor.view",
            "conductor.control",
            "tools.bash",
            "tools.agent",
        ],
    },
    "viewer": {
        "description": "Read-only — view code, read sessions, no edits",
        "permissions": [
            "code.read",
            "session.read",
            "config.read",
            "memory.read",
            "conductor.view",
            "admin.audit",
        ],
    },
}

# ── Tool → permission mapping ──

TOOL_PERMISSIONS: dict[str, str] = {
    # Code read
    "ai_get_lines": "code.read",
    "ai_find": "code.read",
    "ai_trace": "code.read",
    "ai_bundle": "code.read",
    "ai_search": "code.read",
    "ai_text_search": "code.read",
    "ai_investigate": "code.read",
    "ai_get_symbol_snippet": "code.read",
    "ai_get_dependencies": "code.read",
    "ai_get_modules": "code.read",
    "ai_get_module_files": "code.read",
    # Code edit
    "ai_edit_lines": "code.edit",
    # King doctrine 2026-05-01: ai_replace(mode=…) is the unified entry.
    "ai_replace": "code.edit",
    "ai_batch_edit": "code.edit",
    "ai_anchor_replace": "code.edit",
    "ai_insert_lines": "code.edit",
    "ai_create_file": "code.create",
    # Sessions
    "ai_session": "session.edit",
    "ai_task": "session.edit",
    # Config
    "config_edit_policy_get": "config.read",
    # Memory
    "memory_read": "memory.read",
    "memory_search": "memory.read",
    "memory_capture": "memory.write",
    # Conductor
    "ai_plan_status": "conductor.view",
    "ai_plan_graph": "conductor.view",
    "execution_loop_next": "conductor.control",
    "ai_plan_dispatch": "conductor.control",
    # Tools
    "bash": "tools.bash",
}


@dataclass(slots=True)
class User:
    user_id: str
    username: str
    role: str
    created_at: str
    active: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "active": self.active,
        }


@dataclass(slots=True)
class PermissionCheck:
    allowed: bool
    user: User | None = None
    required_permission: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "user": self.user.to_dict() if self.user else None,
            "required_permission": self.required_permission,
            "reason": self.reason,
        }


_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS rbac_users (
    user_id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT 'operator',
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rbac_custom_roles (
    role_id TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    permissions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""


class RBACStore:
    """SQLite-backed RBAC with built-in roles and permission checks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"

    def _connect(self, project_root: Path) -> sqlite3.Connection:
        path = self._db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.executescript(_CREATE_TABLES)
        return conn

    # ── User management ──

    def create_user(
        self,
        project_root: Path,
        username: str,
        role: str = "operator",
    ) -> User:
        if role not in self._all_role_ids(project_root):
            raise ValueError(
                f"Unknown role: {role}. Available: {list(self._all_role_ids(project_root))}",
            )
        user_id = f"user-{uuid4()}"
        now = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._connect(project_root)
            try:
                conn.execute(
                    "INSERT INTO rbac_users (user_id, username, role, created_at, active) VALUES (?, ?, ?, ?, 1)",
                    (user_id, username, role, now),
                )
                conn.commit()
            finally:
                conn.close()
        return User(user_id=user_id, username=username, role=role, created_at=now, active=True)

    def get_user(self, project_root: Path, username: str) -> User | None:
        with self._lock:
            conn = self._connect(project_root)
            try:
                row = conn.execute(
                    "SELECT * FROM rbac_users WHERE username = ?",
                    (username,),
                ).fetchone()
                if row is None:
                    return None
                return User(
                    user_id=row["user_id"],
                    username=row["username"],
                    role=row["role"],
                    created_at=row["created_at"],
                    active=bool(row["active"]),
                )
            finally:
                conn.close()

    def list_users(self, project_root: Path) -> list[User]:
        with self._lock:
            conn = self._connect(project_root)
            try:
                rows = conn.execute("SELECT * FROM rbac_users ORDER BY username").fetchall()
                return [
                    User(
                        user_id=row["user_id"],
                        username=row["username"],
                        role=row["role"],
                        created_at=row["created_at"],
                        active=bool(row["active"]),
                    )
                    for row in rows
                ]
            finally:
                conn.close()

    def update_user_role(self, project_root: Path, username: str, role: str) -> bool:
        if role not in self._all_role_ids(project_root):
            raise ValueError(f"Unknown role: {role}")
        with self._lock:
            conn = self._connect(project_root)
            try:
                cursor = conn.execute(
                    "UPDATE rbac_users SET role = ? WHERE username = ?",
                    (role, username),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def deactivate_user(self, project_root: Path, username: str) -> bool:
        with self._lock:
            conn = self._connect(project_root)
            try:
                cursor = conn.execute(
                    "UPDATE rbac_users SET active = 0 WHERE username = ?",
                    (username,),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    # ── Role management ──

    def _all_role_ids(self, project_root: Path) -> set[str]:
        roles = set(BUILTIN_ROLES.keys())
        conn = self._connect(project_root)
        try:
            rows = conn.execute("SELECT role_id FROM rbac_custom_roles").fetchall()
            roles.update(row["role_id"] for row in rows)
        finally:
            conn.close()
        return roles

    def get_role_permissions(self, project_root: Path, role: str) -> list[str]:
        if role in BUILTIN_ROLES:
            return list(BUILTIN_ROLES[role]["permissions"])
        conn = self._connect(project_root)
        try:
            row = conn.execute(
                "SELECT permissions_json FROM rbac_custom_roles WHERE role_id = ?",
                (role,),
            ).fetchone()
            if row is None:
                return []
            return json.loads(row["permissions_json"])
        finally:
            conn.close()

    def create_custom_role(
        self,
        project_root: Path,
        role_id: str,
        description: str,
        permissions: list[str],
    ) -> dict[str, object]:
        if role_id in BUILTIN_ROLES:
            raise ValueError(f"Cannot override built-in role: {role_id}")
        invalid = [p for p in permissions if p not in PERMISSIONS]
        if invalid:
            raise ValueError(f"Unknown permissions: {invalid}")
        now = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._connect(project_root)
            try:
                conn.execute(
                    """INSERT INTO rbac_custom_roles (role_id, description, permissions_json, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT (role_id) DO UPDATE SET
                           description = excluded.description,
                           permissions_json = excluded.permissions_json""",
                    (role_id, description, json.dumps(permissions), now),
                )
                conn.commit()
            finally:
                conn.close()
        return {"role_id": role_id, "description": description, "permissions": permissions}

    def list_roles(self, project_root: Path) -> list[dict[str, object]]:
        roles: list[dict[str, object]] = []
        for role_id, meta in BUILTIN_ROLES.items():
            roles.append(
                {
                    "role_id": role_id,
                    "builtin": True,
                    "description": meta["description"],
                    "permission_count": len(meta["permissions"]),
                },
            )
        conn = self._connect(project_root)
        try:
            rows = conn.execute("SELECT * FROM rbac_custom_roles").fetchall()
            for row in rows:
                perms = json.loads(row["permissions_json"])
                roles.append(
                    {
                        "role_id": row["role_id"],
                        "builtin": False,
                        "description": row["description"],
                        "permission_count": len(perms),
                    },
                )
        finally:
            conn.close()
        return roles

    # ── Permission checking ──

    def check_permission(
        self,
        project_root: Path,
        username: str | None,
        permission: str,
    ) -> PermissionCheck:
        """Check if a user has a specific permission.

        When no users exist in the system, all operations are allowed
        (backward compatible single-user mode).
        """
        # No users = no RBAC enforcement (backward compat)
        users = self.list_users(project_root)
        if not users:
            return PermissionCheck(
                allowed=True,
                reason="No RBAC users configured — all operations allowed.",
            )

        if username is None:
            return PermissionCheck(
                allowed=False,
                required_permission=permission,
                reason="Authentication required — RBAC is active but no user identified.",
            )

        user = self.get_user(project_root, username)
        if user is None:
            return PermissionCheck(
                allowed=False,
                required_permission=permission,
                reason=f"User '{username}' not found.",
            )
        if not user.active:
            return PermissionCheck(
                allowed=False,
                user=user,
                required_permission=permission,
                reason=f"User '{username}' is deactivated.",
            )

        role_perms = self.get_role_permissions(project_root, user.role)
        if permission in role_perms:
            return PermissionCheck(allowed=True, user=user, required_permission=permission)

        return PermissionCheck(
            allowed=False,
            user=user,
            required_permission=permission,
            reason=f"User '{username}' (role: {user.role}) lacks permission '{permission}'.",
        )

    def check_tool_permission(
        self,
        project_root: Path,
        username: str | None,
        tool_name: str,
    ) -> PermissionCheck:
        """Check if a user can use a specific tool."""
        # Strip MCP prefix
        name = tool_name.strip().lower()
        for prefix in ("mcp__aidocs__",):
            name = name.removeprefix(prefix)

        permission = TOOL_PERMISSIONS.get(name)
        if permission is None:
            # Unknown tool — allow by default (fail open for unmapped tools)
            return PermissionCheck(
                allowed=True,
                reason=f"Tool '{name}' has no permission mapping — allowed by default.",
            )

        return self.check_permission(project_root, username, permission)
