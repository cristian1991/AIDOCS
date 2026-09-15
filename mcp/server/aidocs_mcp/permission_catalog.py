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

import threading as _threading

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
# Empire-law authority (#601): intentionally NOT in the seeded admin role.
# Only super_admin receives the full catalog by default; a custom role may be
# granted this capability explicitly by an existing RBAC authority.
PERM_MEMORY_SET_GLOBAL_LAW = "memory.set_global_law"
PERM_MEMORY_RETIRE_GLOBAL_LAW = "memory.retire_global_law"
PERM_ADMIN_PALACE_MAINTENANCE = "admin.palace_maintenance"
# Daemon lifecycle authority (#623). Holder may STOP / RESTART / refresh the
# governance daemon. Before this existed, `aidocs service stop` resolved no
# principal at all and `request_stop()` was a bare file write — so stopping the
# thing that enforces every gate was the least-governed operation in the
# system, and the CLI was a convenience rather than a gate.
#
# NO USER IS EXEMPT, the operator included: his stop is authenticated and
# audited like anyone's. This permission is about proving WHO, never about
# removing the capability — the operator and the deploy hot-swap must keep
# being able to stop and start the daemon.
PERM_ADMIN_DAEMON_LIFECYCLE = "admin.daemon_lifecycle"

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
# Cross-project handoff consent (#500, operator ruling 2026-07-25).
# handoff_create is a CROSS-PROJECT tool: it moves work context OUT of one
# project and INTO another tree — an egress. This permission is what lets a
# project REFUSE to participate, and it is evaluated in BOTH projects' OWN
# RBAC stores: "if one project doesn't allow, no handoff". Deliberately NOT
# granted to the read-only tiers (observer / audit) — a read-only principal
# must never originate an egress of project context.
PERM_PROJECT_CREATE_HANDOFF = "project.create_handoff"

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

# Project backlog CRUD from the DASHBOARD (2026-07-30, operator charter
# "dashboard needs to be able to CRUD backlog items (RBAC)").
#
# The desktop bridge used to reach project_backlog_store with no principal, no
# permission check and no audit row. These three name the authority instead.
# Split three ways because the acts differ in blast radius, and a single
# `backlog.manage` would have forced the read-only tiers to choose between
# blind and destructive:
#   backlog.read   → see the inventory and item bodies (every dashboard tier).
#   backlog.write  → add + update. NOT task-gated: the API deliberately dropped
#                    the task requirement for add/update, and the dashboard
#                    must not re-impose what the API dropped.
#   backlog.remove → tombstone an item. Destructive, so admin tier only; still
#                    task-gated on the agent surface
#                    (_BACKLOG_TASK_GATED_MODES = {remove, merge, unmerge}).
# The AGENT surface (ai_backlog) is UNCHANGED and unnarrowed by these — it
# carries its own task gate.
PERM_BACKLOG_READ = "backlog.read"
PERM_BACKLOG_WRITE = "backlog.write"
PERM_BACKLOG_REMOVE = "backlog.remove"


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
    Permission(
        PERM_ADMIN_DAEMON_LIFECYCLE,
        "Stop / restart / refresh the AIDOCS governance daemon (#623). "
        "Every use is authenticated and audited — the operator included.",
    ),
    Permission(
        PERM_MEMORY_SET_GLOBAL_LAW,
        "Seal a captured kingdom-memory proposal as global empire law",
    ),
    Permission(
        PERM_MEMORY_RETIRE_GLOBAL_LAW,
        "Approve or deny retirement of active global empire law",
    ),
    Permission(PERM_DEV_ENABLE_DEV_MODE, "Enable developer-mode gate relaxations"),
    Permission(PERM_DEV_SKIP_TESTS, "Skip test-gate checks during active development"),
    Permission(PERM_DEV_SPAWN_WORKERS, "Spawn sub-agent worker lanes"),
    Permission(PERM_OBSERVE_SESSIONS, "Read-only access to session state"),
    Permission(PERM_OBSERVE_LOGS, "Read-only access to logs + journals"),
    Permission(PERM_OBSERVE_METRICS, "Read-only access to metrics + dashboards"),
    Permission(PERM_PROJECT_BOOTSTRAP, "Initialize AIDOCS on a new project (/aidocs bootstrap)"),
    Permission(PERM_PROJECT_UNREGISTER, "Remove a project from the AIDOCS registry"),
    Permission(
        PERM_PROJECT_CREATE_HANDOFF,
        "Participate in a CROSS-PROJECT handoff (as source or destination). "
        "Required in BOTH projects' own RBAC stores — if one side lacks it, "
        "no handoff.",
    ),
    Permission(PERM_FILES_PROTECT_ADD, "Write a DO NOT TOUCH sentinel header to a file"),
    Permission(
        PERM_FILES_PROTECT_REMOVE,
        "Strip a DO NOT TOUCH sentinel (own protections only; "
        "others escalate via security.allow_protected_edit)",
    ),
    Permission(PERM_BACKLOG_READ, "Read the project backlog: inventory + item bodies"),
    Permission(PERM_BACKLOG_WRITE, "Create and update project backlog items"),
    Permission(PERM_BACKLOG_REMOVE, "Tombstone (remove) a project backlog item"),
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
        # #623: an admin may stop/restart the daemon — the deploy hot-swap and
        # the operator both need it. What changed is that the act now names an
        # actor and lands in the audit ledger; the capability is unchanged.
        PERM_ADMIN_DAEMON_LIFECYCLE,
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
        # #500: cross-project handoff consent. Granted to admin only among the
        # seed roles (super_admin holds the whole catalog). Deliberately NOT
        # widened to dev/tester/session_owner/observer/audit: the cross-project
        # session gate already requires admin.manage_sessions, so a lesser role
        # could not originate a handoff anyway, and a permission granted where
        # it cannot be used is a standing escalation surface, not a feature.
        PERM_PROJECT_CREATE_HANDOFF,
        # Admin can remove ANY protection — operators can only remove
        # their own; cross-operator removes escalate to admin via
        # security.allow_protected_edit (already in this set) which the
        # remove flow reads as the override.
        PERM_FILES_PROTECT_ADD,
        PERM_FILES_PROTECT_REMOVE,
        # Backlog: admin holds the full ladder including the destructive
        # tombstone. Removal is admin-only among the seed roles.
        PERM_BACKLOG_READ,
        PERM_BACKLOG_WRITE,
        PERM_BACKLOG_REMOVE,
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
        # Read-only compliance tier: sees the backlog, never writes it.
        PERM_BACKLOG_READ,
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
        # Backlog: dev does the day-to-day triage — read + add/update. NOT
        # remove: a tombstone is destructive and stays on the admin tier.
        PERM_BACKLOG_READ,
        PERM_BACKLOG_WRITE,
    },
)

_TESTER_PERMS = frozenset(
    {
        PERM_DEV_SKIP_TESTS,
        PERM_GATE_ALLOW_TEST_RETRY,
        PERM_ADMIN_VIEW_DASHBOARD,
        PERM_OBSERVE_SESSIONS,
        PERM_OBSERVE_LOGS,
        # Tester reads the backlog to know what to verify; never writes it.
        PERM_BACKLOG_READ,
    },
)

_OBSERVER_PERMS = frozenset(
    {
        PERM_OBSERVE_SESSIONS,
        PERM_OBSERVE_LOGS,
        PERM_OBSERVE_METRICS,
        PERM_ADMIN_VIEW_DASHBOARD,
        # Read is read: an observer who can open the dashboard can see the
        # backlog. Withholding it would render as an empty list, which is the
        # exact confusion this permission split exists to prevent.
        PERM_BACKLOG_READ,
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

        # #755/#756: the ONE canonical connect. This site was
        # `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION
        # context manager, which commits and NEVER closes the handle --
        # and it set no pragma at all, so a migration over the RBAC
        # tables ran with foreign_keys OFF, i.e. with their declared
        # constraints inert.
        # DURABILITY: AUDIT. Every row touched here is a PERMISSION
        # GRANT, and this rewrites the permission NAME those grants key
        # on, then DELETEs the stale rows. A half-applied rename lost to
        # a power cut leaves grants pointing at a string nothing checks:
        # an authority quietly changing hands, which is exactly the case
        # Durability.AUDIT exists for. Runs once per seed, never hot.
        from ._sqlite_connect import Durability as _Durability
        from ._sqlite_connect import connect as _canonical_connect

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
        with _canonical_connect(
            _db, durability=_Durability.AUDIT, row_factory=False
        ) as _conn:
            for _tbl, _col in (
                # rbac_permissions' primary key is `permission_name`
                # (rbac_store.py:127), NOT `name`. It was listed as `name` here,
                # so every UPDATE against it raised OperationalError and was
                # swallowed by the legacy-schema escape hatch below -- which
                # exists to skip a MISSING column quietly, and therefore made a
                # permanently-wrong column name indistinguishable from an old
                # box. The seven gate.* -> security.* renames never applied to
                # the canonical permissions table on ANY schema, old or new.
                #
                # BOTH SPELLINGS ARE LISTED because the escape hatch makes that
                # safe and honest: whichever column actually exists is renamed,
                # and the other breaks quietly exactly as a legacy skip should.
                # Asserting a single spelling here is what caused the silence in
                # the first place.
                ("rbac_permissions", "permission_name"),
                ("rbac_permissions", "name"),  # pre-rename legacy schemas
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
            # #658: a role we just CREATED gets its declared recipe regardless of
            # is_system. `dev` and `tester` are the only seed roles declared
            # is_system=False, and before this they were born holding NOTHING
            # while _DEV_PERMS (12 grants) and _TESTER_PERMS (5) sat fully
            # specified beside them — two seat types that could do nothing at all.
            #
            # Applying it HERE and not below is the whole point. is_system=False
            # means "do not re-assert on later seeds", which is what lets an
            # operator customise `dev` and keep it across reboots. Flipping those
            # roles to is_system=True would have granted the recipe AND silently
            # reverted every operator customisation on every boot — a recurring
            # defect traded for a birth defect. A freshly created role has no
            # customisation to protect, so there is nothing to clobber yet.
            store.set_role_permissions(project_root, role.role_id, perms)
            continue

        role = existing
        if not existing.is_system and is_system:
            # Operator intentionally created a non-system role with
            # the same name as a future seed. Don't clobber.
            continue
        if role.is_system:
            store.set_role_permissions(project_root, role.role_id, perms)


# ── Catalog roll-forward (flavor-INDEPENDENT) ──
#
# #576. ``seed_rbac`` was always written to roll the permission catalog
# forward idempotently — but its only bootstrap caller,
# ``bootstrap_local_superadmin``, returns early on any flavor other than
# dev/solo. Creating a LOCAL OPERATOR IDENTITY is legitimately
# flavor-specific. Rolling the permission CATALOG forward is not: the
# catalog is reviewed source, identical on every install type, and the
# seed is idempotent. Sharing one gate froze the catalog on every
# ``corpo`` install, so ``memory.set_global_law`` (added long after that
# install was first seeded) never reached ``rbac_permissions`` and NO
# principal — not even a proven super_admin — could seal an empire law.
# Hand-inserting that one grant would have greened the symptom and left
# every FUTURE permission equally unreachable.
#
# The split below is deliberately NARROW. ``ensure_permission_catalog_current``
# is ROLL-FORWARD ONLY:
#   * a VIRGIN store (no roles) is left byte-identical. service_hub's
#     pre-RBAC bootstrap bypass and outer_gate_project_rbac's fail-closed
#     "RBAC stays EMPTY" posture both key on "no roles yet"; seeding a
#     virgin store here would silently change both.
#   * it grants nothing beyond the reviewed SEED_ROLES recipes, and only
#     to is_system roles — it can never widen a role past source.
#   * it is memoised per process per identity DB, so the steady-state
#     cost is a dict lookup.
_ROLLED_FORWARD: dict[str, int] = {}
_ROLL_LOCK = _threading.Lock()

# Last roll-forward receipt seen by bootstrap_local_superadmin, kept ONLY so a
# failure can name its own cause. On a corpo install the roll-forward is the
# single repair route and its caller must not raise, so without this a failure
# and an already-current catalog are indistinguishable from the outside.
_LAST_ROLLFORWARD_RECEIPT: dict[str, object] | None = None


def ensure_permission_catalog_current(
    project_root,
    *,
    store: RBACStore | None = None,
    force: bool = False,
) -> dict[str, object]:
    """Roll an ALREADY-SEEDED install's permission catalog forward.

    Flavor-independent and idempotent by construction. Returns a receipt:
    ``{"checked": bool, "rolled_forward": bool, "reason": str, "added": [...]}``.

    Staleness is measured against BOTH tables that matter — a permission
    row missing from ``rbac_permissions`` and a seed grant missing from
    ``rbac_role_permissions`` are each stale, because a registered
    permission that no role holds is still unreachable.

    Never raises: a store that cannot be read reports ``store_unavailable``
    and changes nothing. Callers treat this as best-effort repair, never
    as an authorization decision.
    """
    from pathlib import Path as _Path

    project_root = _Path(project_root)
    store = store or RBACStore()
    expected = len(ALL_PERMISSION_NAMES)
    try:
        db_key = str(store.db_path(project_root))
    except Exception:
        db_key = str(project_root)

    # MEMO VALIDATION (#576). The memo used to short-circuit on `expected`
    # alone — a constant derived from the SOURCE catalog, never from the DB.
    # Once set it therefore asserted "this store is current" forever, and
    # could not notice the store changing underneath it: a migration, a
    # manual repair, another process, or a test stripping rows. Measured
    # consequence on a corpo install, where this is the ONLY repair route:
    # the roll-forward returned reason='memoised' with checked=False, having
    # never run a query, while the permission it was supposed to restore was
    # genuinely absent. dev/solo hid it because their unconditional seeder
    # downstream repairs the same gap by another path.
    #
    # So the memo now records what it OBSERVED (the registered count) beside
    # what it EXPECTED, and a hit is confirmed with ONE cheap read before it
    # is trusted. The expensive part of the full path is the per-role grant
    # walk, and that is still skipped — this keeps the steady-state cost at a
    # single query instead of ~10, while making the memo unable to outlive
    # the state it describes. A cache that cannot be invalidated by reality
    # is not a cache, it is an assertion.
    memo_hit = False
    if not force:
        with _ROLL_LOCK:
            memo_hit = _ROLLED_FORWARD.get(db_key) == expected

    try:
        registered = {str(getattr(p, "name", p)) for p in store.list_permissions(project_root)}
    except Exception:
        return {"checked": False, "rolled_forward": False, "reason": "store_unavailable"}

    if memo_hit and not (ALL_PERMISSION_NAMES - registered):
        return {"checked": True, "rolled_forward": False, "reason": "memoised"}

    try:
        roles = list(store.list_roles(project_root))
    except Exception:
        return {"checked": False, "rolled_forward": False, "reason": "store_unavailable"}

    if not roles:
        # Virgin install. Not ours to seed — see the note above.
        return {"checked": True, "rolled_forward": False, "reason": "virgin_store"}

    # `registered` was already read above, for the memo confirmation.
    missing = sorted(ALL_PERMISSION_NAMES - registered)

    # A registered permission nobody holds is still unreachable, so the
    # seed GRANTS are part of "current" too.
    grant_gap: list[str] = []
    try:
        for role_name, _desc, is_system, perms, _rank in SEED_ROLES:
            if not is_system:
                continue
            role = store.get_role_by_name(project_root, role_name)
            if role is None or not role.is_system:
                continue
            held = {
                str(getattr(p, "name", p))
                for p in store.get_role_permissions(project_root, role.role_id)
            }
            grant_gap.extend(sorted(perms - held))
    except Exception:
        grant_gap = []

    if not missing and not grant_gap:
        with _ROLL_LOCK:
            _ROLLED_FORWARD[db_key] = expected
        return {"checked": True, "rolled_forward": False, "reason": "already_current"}

    seed_rbac(project_root, store=store)
    with _ROLL_LOCK:
        _ROLLED_FORWARD[db_key] = expected
    return {
        "checked": True,
        "rolled_forward": True,
        "reason": "rolled_forward",
        "added": missing,
        "regranted": sorted(set(grant_gap)),
    }


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

    The permission CATALOG roll-forward runs BEFORE the flavor gate and
    on every flavor (#576) — it is not part of what makes bootstrapping
    flavor-specific. See ensure_permission_catalog_current.

    email defaults to AIDOCS_OPERATOR_EMAIL env var or 'operator@local'.
    Returns the user_id of the bootstrapped (or existing) super-admin.
    """
    import os as _os
    from pathlib import Path as _Path

    from .identity_store import IdentityStore

    project_root = _Path(project_root)

    # #576: flavor-INDEPENDENT and ahead of the gate. On a corpo install
    # this is the only line of this function that runs, and it is the
    # line that keeps an existing catalog able to grow. Roll-forward
    # only: a virgin corpo store is left untouched, exactly as before —
    # corpo seeds through the login flow, not here.
    global _LAST_ROLLFORWARD_RECEIPT
    try:
        _LAST_ROLLFORWARD_RECEIPT = ensure_permission_catalog_current(
            project_root, store=store,
        )
    except Exception as exc:  # never raise — bootstrap must not be killed by this
        # ...but never SWALLOW either. On a corpo install this is the ONLY
        # repair route, so an erased exception here presents as "the permission
        # simply never appeared" — indistinguishable from a catalog that was
        # already current. That ambiguity cost a full VPS proof cycle to
        # diagnose. Recording the reason is not a log line for its own sake:
        # it is the difference between a refusal that names its cause and one
        # that cannot be told apart from success.
        _LAST_ROLLFORWARD_RECEIPT = {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    # Flavor gate — the LOCAL OPERATOR IDENTITY only.
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


