"""Permission catalog — Layer 9 C-2 companion to rbac_store.

Closed-set catalog of capability tags. Every permission a role can
hold is defined here, seeded into the rbac_permissions table at
init_db time. Operators compose dynamic roles from this catalog
rather than inventing free-text permission strings (which would
defeat audit).

Axes:
- `mcp_tool.<tool_name>`: permission to invoke a specific MCP tool.
- `gate.<gate_name>`: permission to UNLOCK a user-intent gate (e.g.
  `security.allow_raw_shell` means "this user can grant raw shell
  access via 'allow psql'-style phrases").
- `rbac.<action>`: meta-permissions over the RBAC system itself.
- `admin.<action>`: general admin actions (create users, view audit
  log, etc.).
- `dev.<action>`: developer-mode toggles that relax gates during
  local development.

Seed roles (registered by setup_default_roles) compose these
permissions into the canonical organization chart:
super_admin / admin / audit / dev / tester / observer.
"""

from __future__ import annotations

from .rbac_store import Permission, RBACStore

# ── Permission names ──

# Security-gate unlocks (core RBAC integration with user-intent gates).
# Namespace renamed 2026-04-22: `gate.*` → `security.*` so everything
# security-adjacent groups under the SECURITY section in the dashboard.
# The PERM_GATE_* constant names are kept as aliases to the new
# PERM_SECURITY_* names for back-compat with any external callers that
# imported them; new code should use the PERM_SECURITY_* names.
PERM_SECURITY_ALLOW_RAW_SHELL = "security.allow_raw_shell"
PERM_SECURITY_ALLOW_RAW_EDITS = "security.allow_raw_edits"
PERM_SECURITY_ALLOW_RAW_READS = "security.allow_raw_reads"
PERM_SECURITY_ALLOW_BASH_SUBCMD = "security.allow_bash_subcmd"
PERM_SECURITY_ALLOW_PROTECTED_EDIT = "security.allow_protected_edit"
PERM_SECURITY_ALLOW_LANE_EXIT = "security.allow_lane_exit"
PERM_SECURITY_ALLOW_TEST_RETRY = "security.allow_test_retry"
# Hard-protected data-file authority (#344). Holder — a HUMAN principal only,
# enforcement.py gates principal_type first — may edit non-sqlite
# hard-protected data files and bypass the optional read deny-list. Checked
# through project_authority (authenticated operator + this grant — every
# flavor, #404). Replaces the deleted ghost rbac.py.
PERM_SECURITY_HARD_PROTECTED = "security.hard_protected"
# Super-admin preflight failsafe (operator-requested 2026-07-16). Holder is NOT
# frozen when a UserPromptSubmit trips a FORBIDDEN pre-flight verdict: the prompt
# passes through and the agent gets a sanitized "confirm intent with the operator"
# advisory (rule_ids only, never the prompt) instead. DELIBERATELY absent from
# every seed role except super_admin (which holds the full catalog), so it is
# super_admin-exclusive by construction — a mere admin cannot self-grant an
# unfreezable-prompt path. Fail-closed: an unauthenticated / non-super_admin
# caller keeps the immediate freeze.
PERM_SECURITY_PREFLIGHT_FAILSAFE = "security.preflight_failsafe"
# Back-compat aliases — delete after 2 releases.
PERM_GATE_ALLOW_RAW_SHELL = PERM_SECURITY_ALLOW_RAW_SHELL
PERM_GATE_ALLOW_RAW_EDITS = PERM_SECURITY_ALLOW_RAW_EDITS
PERM_GATE_ALLOW_RAW_READS = PERM_SECURITY_ALLOW_RAW_READS
PERM_GATE_ALLOW_BASH_SUBCMD = PERM_SECURITY_ALLOW_BASH_SUBCMD
PERM_GATE_ALLOW_PROTECTED_EDIT = PERM_SECURITY_ALLOW_PROTECTED_EDIT
PERM_GATE_ALLOW_LANE_EXIT = PERM_SECURITY_ALLOW_LANE_EXIT
PERM_GATE_ALLOW_TEST_RETRY = PERM_SECURITY_ALLOW_TEST_RETRY

# RBAC meta
PERM_RBAC_MANAGE_ROLES = "rbac.manage_roles"
PERM_RBAC_MANAGE_USERS = "rbac.manage_users"
PERM_RBAC_VIEW_AUDIT_LOG = "rbac.view_audit_log"
PERM_RBAC_APPROVE_ESCALATIONS = "rbac.approve_escalations"
# Admin clear-freeze: break-glass to clear a self_approve session
# freeze without minting a grant. Distinct from approve_escalations
# (which DOES mint a grant). 2026-05-04.
PERM_ADMIN_CLEAR_FREEZE = "rbac.admin_clear_freeze"

# Admin
PERM_ADMIN_MANAGE_CONFIG = "admin.manage_config"
PERM_ADMIN_MANAGE_SESSIONS = "admin.manage_sessions"
PERM_ADMIN_VIEW_DASHBOARD = "admin.view_dashboard"
PERM_ADMIN_EXPORT_DATA = "admin.export_data"
PERM_ADMIN_PALACE_MAINTENANCE = "admin.palace_maintenance"

# Dev
PERM_DEV_ENABLE_DEV_MODE = "dev.enable_dev_mode"
PERM_DEV_SKIP_TESTS = "dev.skip_tests"
PERM_DEV_SPAWN_WORKERS = "dev.spawn_workers"

# Read-only observation
PERM_OBSERVE_SESSIONS = "observe.sessions"
PERM_OBSERVE_LOGS = "observe.logs"
PERM_OBSERVE_METRICS = "observe.metrics"

# Project lifecycle (added 2026-04-21 for RBAC enforcement at
# project_init / project_unregister / archive_sessions sites).
PERM_PROJECT_BOOTSTRAP = "project.bootstrap"
PERM_PROJECT_UNREGISTER = "project.unregister"

# DO NOT TOUCH file protection (ai_protect MCP tool, 2026-04-24).
# files.protect.add       → operator+ (anyone with normal work auth
#                            can flag a file via verb+phrase grant).
# files.protect.remove    → operator who protected it OR anyone with
#                            security.allow_protected_edit OR admin+.
#                            Mismatch escalates via escalation_hook.
# No `.approve` permission — the existing rbac.approve_escalations
# and security.allow_protected_edit already cover admin drain.
PERM_FILES_PROTECT_ADD = "files.protect.add"
PERM_FILES_PROTECT_REMOVE = "files.protect.remove"


ALL_PERMISSIONS: tuple[Permission, ...] = (
    Permission(PERM_GATE_ALLOW_RAW_SHELL, "Unlock tier-0 raw shell via 'allow psql'-style phrases"),
    Permission(PERM_GATE_ALLOW_RAW_EDITS, "Unlock raw Edit tool (bypass AIDOCS index)"),
    Permission(PERM_GATE_ALLOW_RAW_READS, "Unlock raw Read/Grep/Glob for managed files"),
    Permission(
        PERM_GATE_ALLOW_BASH_SUBCMD,
        "Unlock a specific bash subcommand (psql, docker, etc.)",
    ),
    Permission(PERM_GATE_ALLOW_PROTECTED_EDIT, "Approve edits to DO-NOT-TOUCH protected files"),
    Permission(PERM_GATE_ALLOW_LANE_EXIT, "Approve a lane worker to exit isolation"),
    Permission(PERM_GATE_ALLOW_TEST_RETRY, "Approve skipping test-retry gate"),
    Permission(
        PERM_SECURITY_HARD_PROTECTED,
        "Hold hard-protected data-file authority (edit non-sqlite "
        "hard-protected data files; bypass the read deny-list)",
    ),
    Permission(
        PERM_SECURITY_PREFLIGHT_FAILSAFE,
        "Super-admin only: a forbidden pre-flight prompt verdict becomes a "
        "non-blocking 'confirm intent with the operator' advisory instead of "
        "an immediate session freeze.",
    ),
    Permission(PERM_RBAC_MANAGE_ROLES, "Create/delete roles and assign permissions"),
    Permission(PERM_RBAC_MANAGE_USERS, "Create/disable users and bind roles"),
    Permission(PERM_RBAC_VIEW_AUDIT_LOG, "Read RBAC audit log entries"),
    Permission(
        PERM_RBAC_APPROVE_ESCALATIONS,
        "Approve/deny escalation requests from the dashboard",
    ),
    Permission(
        PERM_ADMIN_CLEAR_FREEZE,
        "Break-glass: clear a session freeze without minting a "
        "grant. Operator retries the original action normally.",
    ),
    Permission(PERM_ADMIN_MANAGE_CONFIG, "Mutate runtime config (gates, policies)"),
    Permission(PERM_ADMIN_MANAGE_SESSIONS, "Create/archive/transfer sessions"),
    Permission(PERM_ADMIN_VIEW_DASHBOARD, "View the AIDOCS operator dashboard"),
    Permission(PERM_ADMIN_EXPORT_DATA, "Export sessions, journals, audit logs"),
    Permission(
        PERM_ADMIN_PALACE_MAINTENANCE,
        "Run guarded MemPalace maintenance (legacy-drawer backfill)",
    ),
    Permission(PERM_DEV_ENABLE_DEV_MODE, "Enable developer-mode gate relaxations"),
    Permission(PERM_DEV_SKIP_TESTS, "Skip test-gate checks during active development"),
    Permission(PERM_DEV_SPAWN_WORKERS, "Spawn sub-agent worker lanes"),
    Permission(PERM_OBSERVE_SESSIONS, "Read-only access to session state"),
    Permission(PERM_OBSERVE_LOGS, "Read-only access to logs + journals"),
    Permission(PERM_OBSERVE_METRICS, "Read-only access to metrics + dashboards"),
    Permission(PERM_PROJECT_BOOTSTRAP, "Initialize AIDOCS on a new project (/aidocs bootstrap)"),
    Permission(PERM_PROJECT_UNREGISTER, "Remove a project from the AIDOCS registry"),
    Permission(PERM_FILES_PROTECT_ADD, "Write a DO NOT TOUCH sentinel header to a file"),
    Permission(
        PERM_FILES_PROTECT_REMOVE,
        "Strip a DO NOT TOUCH sentinel (own protections only; "
        "others escalate via security.allow_protected_edit)",
    ),
)

ALL_PERMISSION_NAMES: frozenset[str] = frozenset(p.name for p in ALL_PERMISSIONS)


# ── Seed role recipes ──

# super_admin: every permission. is_system so it can't be deleted.
_SUPER_ADMIN_PERMS = frozenset(p.name for p in ALL_PERMISSIONS)

_ADMIN_PERMS = frozenset(
    {
        PERM_RBAC_MANAGE_USERS,
        PERM_RBAC_VIEW_AUDIT_LOG,
        PERM_RBAC_APPROVE_ESCALATIONS,
        PERM_ADMIN_CLEAR_FREEZE,
        PERM_ADMIN_MANAGE_CONFIG,
        PERM_ADMIN_MANAGE_SESSIONS,
        PERM_ADMIN_VIEW_DASHBOARD,
        PERM_ADMIN_EXPORT_DATA,
        PERM_ADMIN_PALACE_MAINTENANCE,
        PERM_GATE_ALLOW_RAW_SHELL,
        PERM_GATE_ALLOW_RAW_EDITS,
        PERM_GATE_ALLOW_PROTECTED_EDIT,
        PERM_GATE_ALLOW_BASH_SUBCMD,
        PERM_GATE_ALLOW_LANE_EXIT,
        PERM_GATE_ALLOW_TEST_RETRY,
        PERM_SECURITY_HARD_PROTECTED,
        PERM_DEV_SPAWN_WORKERS,
        PERM_OBSERVE_SESSIONS,
        PERM_OBSERVE_LOGS,
        PERM_OBSERVE_METRICS,
        PERM_PROJECT_BOOTSTRAP,
        PERM_PROJECT_UNREGISTER,
        # Admin can remove ANY protection — operators can only remove
        # their own; cross-operator removes escalate to admin via
        # security.allow_protected_edit (already in this set) which the
        # remove flow reads as the override.
        PERM_FILES_PROTECT_ADD,
        PERM_FILES_PROTECT_REMOVE,
    },
)

_AUDIT_PERMS = frozenset(
    {
        PERM_RBAC_VIEW_AUDIT_LOG,
        PERM_ADMIN_VIEW_DASHBOARD,
        PERM_ADMIN_EXPORT_DATA,
        PERM_OBSERVE_SESSIONS,
        PERM_OBSERVE_LOGS,
        PERM_OBSERVE_METRICS,
    },
)

_DEV_PERMS = frozenset(
    {
        PERM_DEV_ENABLE_DEV_MODE,
        PERM_DEV_SPAWN_WORKERS,
        PERM_GATE_ALLOW_BASH_SUBCMD,
        PERM_GATE_ALLOW_LANE_EXIT,
        PERM_ADMIN_VIEW_DASHBOARD,
        PERM_OBSERVE_SESSIONS,
        PERM_OBSERVE_LOGS,
        PERM_PROJECT_BOOTSTRAP,
        # Dev: add/remove own protections; cross-operator removes still
        # need security.allow_protected_edit (admin-only).
        PERM_FILES_PROTECT_ADD,
        PERM_FILES_PROTECT_REMOVE,
    },
)

_TESTER_PERMS = frozenset(
    {
        PERM_DEV_SKIP_TESTS,
        PERM_GATE_ALLOW_TEST_RETRY,
        PERM_ADMIN_VIEW_DASHBOARD,
        PERM_OBSERVE_SESSIONS,
        PERM_OBSERVE_LOGS,
    },
)

_OBSERVER_PERMS = frozenset(
    {
        PERM_OBSERVE_SESSIONS,
        PERM_OBSERVE_LOGS,
        PERM_OBSERVE_METRICS,
        PERM_ADMIN_VIEW_DASHBOARD,
    },
)

# Session-owner: the LEAST-PRIVILEGE role minted (session-scoped) for the
# authenticated creator of a session (2026-05-25 vocabulary seal). It carries
# ONLY admin.manage_sessions — exactly what require_session stage-2b checks —
# so the audit/dashboard name matches the authority granted. Previously the
# create flow reused the `admin` role scoped to the session, which over-implied
# (admin carries manage_config/manage_roles/raw_shell/… that a session owner
# never needs). Existing admin@session grants still authorize (admin ⊇
# manage_sessions); this only narrows NEW owner grants. NOT a role migration.
_SESSION_OWNER_PERMS = frozenset(
    {
        PERM_ADMIN_MANAGE_SESSIONS,
    },
)


SEED_ROLES: tuple[tuple[str, str, bool, frozenset[str], int], ...] = (
    # (name, description, is_system, perms, rank)
    # Rank is a numeric authority ladder. Lower = higher authority.
    # Seeded with gap=100 so operators can slot custom roles between
    # seed roles without renumbering. See rbac_store.Role for details.
    ("super_admin", "All permissions; the break-glass role.", True, _SUPER_ADMIN_PERMS, 0),
    ("admin", "Day-to-day administration: users, gates, escalations.", True, _ADMIN_PERMS, 100),
    ("dev", "Developer with gate relaxations for local work.", False, _DEV_PERMS, 200),
    ("audit", "Read-only compliance + audit log access.", True, _AUDIT_PERMS, 300),
    ("tester", "Test runner with retry-gate bypass.", False, _TESTER_PERMS, 400),
    # Scoped operator role: owns ONE session it created, nothing else.
    # Always granted session-scoped; never global/project. Rank 500 = weaker
    # than tester, stronger than observer (a read-only viewer).
    (
        "session_owner",
        "Owns a single session it created: manage that session only.",
        True,
        _SESSION_OWNER_PERMS,
        500,
    ),
    ("observer", "Read-only dashboard access.", True, _OBSERVER_PERMS, 600),
)


def seed_rbac(project_root, store: RBACStore | None = None) -> None:
    """Idempotent — safe to call on every server boot.

    1. Migrates legacy `gate.*` permission rows to `security.*`
       (2026-04-22 rename). No-op on fresh installs and on already-
       migrated DBs.
    2. Seeds the permission catalog into rbac_permissions.
    3. Creates any missing seed roles (with rank + inheritance) and
       sets their permission sets. Existing rows are updated to
       match the seed (system roles only — user-defined non-system
       roles are left alone).
    4. System roles keep their is_system + rank pinned to the seed
       values; operator-edited custom roles are never touched.
    """
    from pathlib import Path as _Path

    project_root = _Path(project_root)
    store = store or RBACStore()
    store.init_db(project_root, seed_permissions=ALL_PERMISSIONS)
    # Migration 2026-04-22: rewrite any lingering `gate.*` permission
    # strings to `security.*`. Touches the three tables that store
    # permission names by string value. Best-effort; if a table is
    # missing or the schema differs, we swallow and continue. New
    # installs start clean; existing installs roll forward on first
    # seed. Idempotent: re-running after migration is a no-op.
    try:
        import sqlite3 as _sqlite3

        _db = store.db_path(project_root)
        _rename_map = {
            "gate.allow_raw_shell": "security.allow_raw_shell",
            "gate.allow_raw_edits": "security.allow_raw_edits",
            "gate.allow_raw_reads": "security.allow_raw_reads",
            "gate.allow_bash_subcmd": "security.allow_bash_subcmd",
            "gate.allow_protected_edit": "security.allow_protected_edit",
            "gate.allow_lane_exit": "security.allow_lane_exit",
            "gate.allow_test_retry": "security.allow_test_retry",
        }
        with _sqlite3.connect(str(_db)) as _conn:
            for _tbl, _col in (
                ("rbac_permissions", "name"),
                ("rbac_role_permissions", "permission_name"),
                ("rbac_user_permission_overrides", "permission_name"),
                ("rbac_scoped_role_permissions", "permission_name"),
            ):
                for _old, _new in _rename_map.items():
                    try:
                        _conn.execute(
                            f"UPDATE OR IGNORE {_tbl} SET {_col} = ? WHERE {_col} = ?",
                            (_new, _old),
                        )
                        # If a UNIQUE constraint collision leaves an
                        # orphan old-prefix row, drop it (the new-prefix
                        # row is what we want).
                        _conn.execute(
                            f"DELETE FROM {_tbl} WHERE {_col} = ?",
                            (_old,),
                        )
                    except _sqlite3.OperationalError:
                        # Table or column missing on older schemas —
                        # skip quietly.
                        break
            _conn.commit()
    except Exception:
        pass
    for name, desc, is_system, perms, rank in SEED_ROLES:
        existing = store.get_role_by_name(project_root, name)
        if existing is None:
            role = store.create_role(
                project_root,
                name=name,
                description=desc,
                is_system=is_system,
                rank=rank,
                created_by_user_id="__seed__",
            )
        else:
            role = existing
            if not existing.is_system and is_system:
                # Operator intentionally created a non-system role with
                # the same name as a future seed. Don't clobber.
                continue
        if role.is_system:
            store.set_role_permissions(project_root, role.role_id, perms)


def bootstrap_local_superadmin(
    project_root,
    *,
    email: str | None = None,
    store: RBACStore | None = None,
) -> str:
    """Idempotent first-start bootstrap of the local operator identity.

    Behavior by distribution.flavor setting (config):
      * 'dev' or 'solo' (default) → auto-create a local user + assign
        super_admin at global scope. Runs on every MCP boot; silent if
        the user already exists.
      * 'corpo' → no-op. Login required, first-register gets super_admin
        via the login flow (dashboard, not this function).

    email defaults to AIDOCS_OPERATOR_EMAIL env var or 'operator@local'.
    Returns the user_id of the bootstrapped (or existing) super-admin.
    """
    import os as _os
    from pathlib import Path as _Path

    from .identity_store import IdentityStore

    project_root = _Path(project_root)

    # Flavor gate.
    flavor = "solo"
    try:
        from .config import get_setting

        flavor_raw = get_setting(
            "distribution.flavor",
            project_root=project_root,
            default="solo",
        )
        flavor = str(flavor_raw or "solo").strip().lower()
    except Exception:
        flavor = "solo"
    if flavor not in ("dev", "solo"):
        return ""

    store = store or RBACStore()
    seed_rbac(project_root, store=store)

    identity = IdentityStore()
    resolved_email = (
        email or _os.environ.get("AIDOCS_OPERATOR_EMAIL", "").strip() or "operator@local"
    ).lower()

    # Find-or-create the user. Password is a random placeholder in
    # flavors that skip login; corpo flavor never reaches this branch.
    existing = identity.get_user_by_email(project_root, resolved_email)
    if existing is None:
        import secrets as _sec

        # Placeholder password — corpo flavor never reaches this branch,
        # and dev/solo flavors don't use the password for auth (local
        # user is implicitly trusted). Still hashed at rest.
        placeholder_pw = _sec.token_urlsafe(24)
        user = identity.create_user(
            project_root,
            email=resolved_email,
            password=placeholder_pw,
            role="admin",  # legacy identity-store role tag (VALID_ROLES)
        )
        user_id = user.user_id
    else:
        user_id = existing.user_id

    # Assign super_admin at global scope. Idempotent via INSERT OR
    # IGNORE inside assign_role_to_user_scoped.
    super_admin_role = store.get_role_by_name(project_root, "super_admin")
    if super_admin_role is None:
        # Shouldn't happen — seed_rbac ran above — but fail loudly if it did.
        raise RuntimeError("super_admin role missing after seed_rbac; cannot bootstrap")
    store.assign_role_to_user_scoped(
        project_root,
        user_id,
        super_admin_role.role_id,
        scope_type="global",
        authored_by_user_id="__bootstrap__",
        authored_by_rank=0,
    )
    return user_id


def require_password_for_gate(gate_permission: str) -> bool:
    """Policy hook: which gate unlocks need password-gated approval?

    Defaults to True for every security.* permission so operators must
    opt OUT rather than opt IN for risky unlocks. Wiring can read
    the rbac.require_password config list to override per-gate.
    Back-compat: also matches legacy `gate.*` prefix for any row that
    predates the 2026-04-22 rename (the DB migration in seed_rbac
    handles the rewrite, but a stale cache may still surface old
    names for a moment).
    """
    return (
        gate_permission.startswith("security.") or gate_permission.startswith("gate.")  # legacy
    )
