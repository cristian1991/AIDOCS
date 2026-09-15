"""SQLite-backed configuration store — replaces TOML file chain for settings.

Two physical storage locations depending on scope:

- **Global scope** → `~/.aidocs/config.sqlite3`
  Install-wide settings that should apply to every AIDOCS project for this
  user/machine. Examples: `security.bash_allowed`, `conductor.backend`,
  `dev.dev_mode`, `index.enabled_languages`, `tools.max_timeout`.

- **Project/session scope** → `<project>/.MEMORY/.index/aidocs.sqlite3`
  Settings specific to one project, and session-overrides inside that project.

Features:
- Atomic reads/writes (SQLite transactions)
- Cross-DB effective resolution: session > project > global
- Hot reload (every read hits DB, no stale caches)
- One-shot auto-migration: moves any `scope='global'` rows that were
  accidentally written into a project DB (old bug) into the global DB.

Scope model:
- `global`  — install-wide, stored in `~/.aidocs/config.sqlite3`
- `project` — stored in project DB
- `session` — stored in project DB with `scope_key = session_id`

There is no `user` scope. (`user` and `global` were interchangeable in intent
but were both physically project-local in the old buggy implementation —
conceptually merged into `global` here.)

Table schema (same in both DBs):
    config_settings (
        setting_path TEXT,      -- e.g. "security.enforce"
        scope TEXT,             -- "global", "project", "session"
        scope_key TEXT,         -- session_id for session scope, "" otherwise
        value TEXT,             -- JSON-encoded value
        updated_at TEXT,        -- ISO timestamp
        PRIMARY KEY (setting_path, scope, scope_key)
    )
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._sqlite_connect import connect as _canonical_connect

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS config_settings (
    setting_path TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'project',
    scope_key TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (setting_path, scope, scope_key)
)
"""

_SCOPE_PRIORITY = {"global": 0, "project": 1, "session": 2}

_GLOBAL_SCOPES = frozenset({"global"})

# Hard-removed setting keys (no aliases, no silent migration).
# Reads and writes raise loudly with the replacement instructions.
# 2026-04-28: collapsed legacy security booleans into explicit
# enum policies (security.prompt_secret_policy +
# security.tool_output_secret_policy).
_REMOVED_SETTINGS: dict[str, str] = {
    "security.block_user_credentials": (
        "Removed 2026-04-28. Use security.prompt_secret_policy "
        "(values: 'block' | 'allow', default 'block'). "
        "Migration: True → 'block', False → 'allow'."
    ),
    "security.output_guard": (
        "Removed 2026-04-28. Use security.tool_output_secret_policy "
        "(values: 'redact' | 'report_only' | 'allow_raw', default "
        "'redact'). Migration: False → 'allow_raw', True → keep your "
        "old security.output_guard_redact value as the new policy "
        "('redact' or 'report_only')."
    ),
    "security.output_guard_redact": (
        "Removed 2026-04-28. Use security.tool_output_secret_policy "
        "(values: 'redact' | 'report_only' | 'allow_raw', default "
        "'redact'). Migration: True → 'redact', False → 'report_only'."
    ),
    "agent.host_mode": (
        "Removed 2026-04-28. Advisory mode retired; every supported "
        "host now has PreToolUse + UserPromptSubmit hooks so gates do "
        "the enforcement. No replacement needed — drop the key."
    ),
    # 2026-05-02: Phase 2 of settings rationalization. Stale cobwebs
    # from previous arcs that left rows in the global DB but no code
    # path consults them anymore. Reads now raise loudly so operators
    # see the migration message instead of mysterious silent fallbacks.
    "conductor.autowake_mode": (
        "Removed 2026-04-30 with the autowake/forced-work feature "
        "(commit 3320307f). Mechanism could not achieve its goal — "
        "agents could decline ScheduleWakeup and stall the session. "
        "No replacement; drop the key."
    ),
    "conductor.autowake_max_interval_seconds": (
        "Removed 2026-04-30 with autowake/forced-work feature "
        "(commit 3320307f). No replacement; drop the key."
    ),
    "conductor.forced_work_mode": (
        "Removed 2026-04-30 with autowake/forced-work feature "
        "(commit 3320307f). No replacement; drop the key."
    ),
    # 2026-08-27 (#559), operator ruling verbatim: "559. no, remove it, no
    # auto-bind." The dashboard checkbox wrote this key and NOTHING on the
    # backend ever read it. The tombstone matters more than the deletion: an
    # operator who ticked the box has a row in their config.sqlite3, and a
    # deleted-but-untombstoned key would leave that row in place forever while
    # reads fell silently through to a default.
    "dashboard.auto_bind_local_sessions": (
        "Removed 2026-08-27 by operator ruling on #559 (\"no auto-bind\"). The "
        "toggle was writable and had zero backend readers, and wiring it would "
        "have minted a SECOND operator-authority path — auto-approving "
        "host-session bindings from a stored flag — alongside the existing "
        "project_authority path (c), which already authenticates local sessions "
        "while a machine login is live. No replacement: approve each binding in "
        "the dashboard's Bindings panel. Drop the key."
    ),
    "dev.allow_config_edit": (
        "Renamed 2026-04-22 to security.allow_config_edit — it is a "
        "security gate (controls who mutates config), not a dev "
        "capability. Migration: copy the old value to the new key "
        "via the dashboard (security.allow_config_edit is T0 "
        "dashboard-only)."
    ),
    "gate.bash_allowed": (
        "Removed 2026-04-25. Superseded by the declarative [bash] "
        "table (bash.allow.<cmd> = [patterns], bash.deny.<cmd> = "
        "[patterns], bash.default = 'allow'|'block'). The legacy "
        "substring allowlist is no longer consulted. Drop the key."
    ),
    "gate.enforce": (
        "Removed. Use security.enforce instead — same boolean semantic, project-correct namespace."
    ),
    "tool_output.verbose": (
        "Removed. Use the granular tool_output.show_* family: "
        "tool_output.show_tool_name, tool_output.show_duration, "
        "tool_output.show_tokens. Drop the key."
    ),
}


class RemovedSettingError(ValueError):
    """Raised when reading or writing a hard-removed setting key.

    Loud failure by design: silent migration would carry the legacy
    semantics across versions and let old configs slip past the
    cleanup. Operators must explicitly re-set under the new key.
    """


class ConfigScopeError(ValueError):
    """Raised when a setting is written at a scope its catalog does not
    allow — e.g. a global-only/platform setting written at project or
    session scope. This is the mutation-boundary seal that keeps config
    PRECEDENCE (specificity) from becoming an OVERRIDE of platform law: a
    more-specific layer can never persist a value for a setting the
    catalog pins to a less-specific scope. Enforced for catalog members
    only; unknown/runtime keys (e.g. the `bash.*` policy tables) are
    unaffected. Fail-closed by raising rather than silently writing.
    """


_PROJECT_SCOPES = frozenset({"project", "session"})

# Track which project DBs have already had their legacy `scope='global'`
# rows migrated to the global DB. Per-process, reset on restart — safe to
# repeat the migration SQL because it is idempotent, but the cache avoids
# redundant work per ConfigStore() instance lifetime.
_MIGRATED_PROJECTS: set[str] = set()
_MIGRATION_LOCK = threading.Lock()

# One-shot per-process flag for the global-DB removed-key sweep. The
# global sweep is idempotent (DELETE WHERE setting_path IN (...)) so
# repeating it is safe; this just avoids redundant work per process.
_GLOBAL_SWEPT: bool = False
_GLOBAL_SWEEP_LOCK = threading.Lock()


def _invalidate_config_request_cache() -> None:
    """Drop the active request-scoped config layer-rows cache (config_resolver).
    Called from EVERY config_settings mutation path (set / delete / removed-key
    sweep / global migration) so a read-after-write in the same logical event
    re-reads the committed DB. Best-effort: never raises into a write path."""
    try:
        from .config_resolver import invalidate_request_config_scope

        invalidate_request_config_scope()
    except Exception:
        pass


# ── Per-request "global" config override (webmcp multi-tenancy) ───────────────
# In the outer-gate webmcp service one process serves many orgs (tenants). The
# "global" config scope is install-wide on a single-tenant box, but for a tenant
# REQUEST it must resolve to that ORG's private global DB — never the shared host
# config. The transport sets this contextvar to the tenant's global-config path for
# the duration of one request (reset in `finally`). A contextvar (NOT an env var or
# module global) is mandatory: requests are concurrent and an env mutation would
# bleed one tenant's global scope into another's. EVERY read of the global DB goes
# through `_global_db_path()`, so setting this covers the whole effective-config
# path (resolver + write + readback), not just one call site.
import contextvars as _contextvars

_TENANT_GLOBAL_DB: _contextvars.ContextVar[str] = _contextvars.ContextVar(
    "aidocs_tenant_global_db", default="",
)


def set_tenant_global_db(path: str | Path | None) -> object:
    """Bind the per-tenant global-config DB for the current context. Returns a token
    to pass to ``reset_tenant_global_db`` in a `finally`. Pass None/"" to clear."""
    return _TENANT_GLOBAL_DB.set(str(path or ""))


def reset_tenant_global_db(token: object) -> None:
    """Restore the previous per-tenant global-config binding (use in `finally`)."""
    try:
        _TENANT_GLOBAL_DB.reset(token)  # type: ignore[arg-type]
    except (ValueError, LookupError):
        pass


# ── Tenant global store derived from the PROJECT PATH (#497) ─────────────────
# The contextvar above is set in exactly ONE place — the transport's
# `_ogt_tenant_bind`, i.e. only for the duration of an authenticated request.
# Every NON-request execution path on the gate (the index sitter's reconcile →
# sync_code_files → get_setting, watchdogs, maintenance) therefore resolved the
# "global" scope to the gate DAEMON's own ~/.aidocs/config.sqlite3, NOT to the
# tenant whose project it was working on. That is the #497 defect: an operator
# flips a control-plane toggle in the dashboard (a request — correctly written
# to the tenant store), the UI confirms, and the indexer that actually applies
# the setting never sees it. Silent no-effect, which is worse than an error.
#
# The tenant is derivable with NO request identity and NO session id: a tenant
# project always lives at `<base>/tenants/<tid>/projects/...` (outer_gate_tenancy
# .tenant_projects_dir) and its global store is the sibling
# `<base>/tenants/<tid>/global-config.sqlite3` (tenant_global_config_db). This
# derivation is pure path containment — deliberately independent of the
# per-request host session id, which is regenerated on every call and is NOT a
# stable identity to key anything on.
#
# It only fires when that store ALREADY EXISTS on disk. A directory that merely
# looks like a tenant layout never hijacks resolution, and the derivation never
# CREATES a store it did not find — so nothing an operator has stored at machine
# scope moves, and no local install changes behaviour.
_TENANTS_DIRNAME = "tenants"
_TENANT_PROJECTS_DIRNAME = "projects"
_TENANT_GLOBAL_DB_FILENAME = "global-config.sqlite3"


def _tenant_global_db_for_project(project_root: Path | None) -> Path | None:
    """The per-ORG global store owning `project_root`, or None.

    Matches `<base>/tenants/<tid>/projects/<anything…>` and returns
    `<base>/tenants/<tid>/global-config.sqlite3` when that file exists.
    Never raises: a path that cannot be inspected is simply not a tenant path.
    """
    if project_root is None:
        return None
    try:
        parts = Path(project_root).resolve().parts
    except (OSError, ValueError):
        return None
    # Scan from the deepest match outward so a nested tenants/ dir inside a
    # tenant project cannot shadow the real one.
    for i in range(len(parts) - 3, -1, -1):
        if parts[i] != _TENANTS_DIRNAME or parts[i + 2] != _TENANT_PROJECTS_DIRNAME:
            continue
        candidate = Path(*parts[: i + 2]) / _TENANT_GLOBAL_DB_FILENAME
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            return None
    return None


def _global_db_path(project_root: Path | None = None) -> Path:
    """Path to the install-wide config DB. Created lazily on first access.

    Precedence (fail-safe, request-scoped first):
      1. the per-tenant contextvar (webmcp multi-tenant request) — an ORG's private
         global DB, set by the transport and reset in `finally`;
      2. the tenant store DERIVED from `project_root` (#497) — the same ORG store
         the request path uses, for the background/daemon paths that have no
         request to bind them. Existing tenant store only; never created here;
      3. `AIDOCS_GLOBAL_CONFIG_DB` env var — test isolation from the real home;
      4. `~/.aidocs/config.sqlite3` — the single-tenant / local default.

    A tenant project resolves the global scope from the TENANT store only — the
    gate daemon's host store is deliberately not layered underneath it, because
    that is precisely the isolation the request path already enforces.
    """
    import os

    tenant = _TENANT_GLOBAL_DB.get()
    if tenant:
        return Path(tenant)
    derived = _tenant_global_db_for_project(project_root)
    if derived is not None:
        return derived
    override = os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "config.sqlite3"


def _project_db_path(project_root: Path) -> Path:
    """Path to the per-project DB (shared with indexes, legacy)."""
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _is_security_sensitive(setting_path: str) -> bool:
    """True when the catalog marks the setting security_sensitive (so the
    audit redacts its value). Robust to dict- or attr-style catalog
    entries; never raises.
    """
    try:
        from .config_schema import SETTINGS_CATALOG

        meta = SETTINGS_CATALOG.get(setting_path)
        if not meta:
            return False
        if isinstance(meta, dict):
            return bool(meta.get("security_sensitive"))
        return bool(getattr(meta, "security_sensitive", False))
    except Exception:
        return False


# GLOBAL-scope config audit is cross-DB (config in ~/.aidocs, ledger in
# the project DB) so it CANNOT be transactionally atomic. When the
# best-effort audit fails we record a degraded marker here so the gap is
# loud, not silent. Tests inspect this; ops can surface it.
GLOBAL_AUDIT_DEGRADED: list[dict] = []


def _best_effort_global_audit(
    project_root: Path,
    *,
    event_kind: str,
    action_kind: str,
    capability_name: str,
    target_entity: str,
    payload: dict,
) -> None:
    """Emit a global-scope config audit best-effort. On failure, append a
    degraded marker AND attempt a secondary ``audit_emit_failed`` event
    so the missing ink is visible. NEVER raises (global writes already
    committed — non-atomic by design).
    """
    from .execution_index_store import ExecutionIndexStore

    try:
        ExecutionIndexStore().record_event(
            project_root,
            event_kind=event_kind,
            source_kind="config_store",
            capability_name=capability_name,
            action_kind=action_kind,
            target_entity=target_entity,
            status="applied",
            payload=payload,
        )
    except Exception as exc:
        marker = {
            "target_entity": target_entity,
            "action_kind": action_kind,
            "error": str(exc),
        }
        GLOBAL_AUDIT_DEGRADED.append(marker)
        try:
            ExecutionIndexStore().record_event(
                project_root,
                event_kind="audit_emit_failed",
                source_kind="config_store",
                capability_name=capability_name,
                action_kind=action_kind,
                target_entity=target_entity,
                status="degraded",
                payload={"original_event_kind": event_kind, "error": str(exc)},
            )
        except Exception:
            pass


class ConfigStore:
    """SQLite-backed configuration with scoped resolution across two DBs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ── Path resolution ──────────────────────────────────────────────────

    def db_path(self, project_root: Path, scope: str = "project") -> Path:
        """Return the DB path that would serve this scope.

        For `global`, returns `~/.aidocs/config.sqlite3`.
        For `project`/`session`, returns the project DB.
        """
        if scope in _GLOBAL_SCOPES:
            return _global_db_path(project_root)
        return _project_db_path(project_root)


    # ── Connections ──────────────────────────────────────────────────────

    def _connect(self, db_path: Path) -> sqlite3.Connection:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # THE CANONICAL CONNECT (#755). This store is read on essentially
        # every governed tool call, and it is one of the three files the
        # 2026-08-04 census found setting ANY pragma -- it set journal_mode
        # and nothing else, so it ran at sqlite's default synchronous=FULL
        # and paid the 8-10x fsync tax (#754) on every config write, with
        # foreign_keys OFF and no busy_timeout throughout. The helper sets
        # all four, and memoises WAL per file instead of re-issuing it on
        # every open. row_factory=sqlite3.Row is its default, so the
        # by-name row access below is unchanged.
        conn = _canonical_connect(db_path)
        conn.execute(_CREATE_TABLE)
        return conn

    def _connect_for_scope(self, project_root: Path, scope: str) -> sqlite3.Connection:
        if scope in _GLOBAL_SCOPES:
            self._ensure_global_swept()
            return self._connect(_global_db_path(project_root))
        self._ensure_project_migrated(project_root)
        return self._connect(_project_db_path(project_root))

    # ── Migration ────────────────────────────────────────────────────────

    def _ensure_project_migrated(self, project_root: Path) -> None:
        """One-shot migration: move any `scope='global'` rows out of this
        project's DB into the install-wide global DB.

        Older versions of ConfigStore wrote global-scope settings into each
        project's DB, so existing projects carry ~dozens of mis-scoped rows.
        This method runs once per ConfigStore instance per project_root,
        is fully transactional, and is a no-op if nothing needs migrating.
        """
        project_key = str(project_root.resolve())
        with _MIGRATION_LOCK:
            if project_key in _MIGRATED_PROJECTS:
                return
            _MIGRATED_PROJECTS.add(project_key)

        project_db = _project_db_path(project_root)
        if not project_db.is_file():
            return

        try:
            project_conn = _canonical_connect(project_db)
            project_conn.execute(_CREATE_TABLE)
            # Phase 2 sweep — drop removed-key rows before any migration
            # work. Runs even when there are no scope='global' rows to
            # migrate. Sweep + commit before the migration's transaction
            # opens so the two transactions don't interleave.
            self._sweep_removed_in_db(project_conn)
            project_conn.commit()
            rows = project_conn.execute(
                "SELECT setting_path, scope_key, value, updated_at "
                "FROM config_settings WHERE scope = 'global'",
            ).fetchall()
            if not rows:
                project_conn.close()
                # No global-rows to migrate, but still run the
                # Phase 2 sweep below.
                rows = []
                migration_needed = False
            else:
                migration_needed = True

            if not migration_needed:
                # Skip the global-DB migration block; fall through
                # to the sweep at the end of the method.
                pass
            else:
                global_db = _global_db_path(project_root)
            global_db.parent.mkdir(parents=True, exist_ok=True)
            global_conn = _canonical_connect(global_db)
            global_conn.execute(_CREATE_TABLE)
            try:
                global_conn.execute("BEGIN")
                for row in rows:
                    # INSERT OR IGNORE — if the global DB already has a value
                    # for this setting (from another project that migrated
                    # earlier or from a direct global write), keep it. The
                    # "winner" of concurrent migrations is whichever project
                    # ran first; that is acceptable because the rows were
                    # semantically the same global value before the bug was
                    # fixed anyway.
                    global_conn.execute(
                        "INSERT OR IGNORE INTO config_settings "
                        "(setting_path, scope, scope_key, value, updated_at) "
                        "VALUES (?, 'global', ?, ?, ?)",
                        (
                            row["setting_path"],
                            row["scope_key"] or "",
                            row["value"],
                            row["updated_at"],
                        ),
                    )
                global_conn.commit()
            finally:
                global_conn.close()

            # Only after the global DB write has committed, clear the rows
            # from the project DB. Do this in a separate transaction so a
            # crash between the two cannot leave us with nothing in either.
            project_conn.execute("BEGIN")
            project_conn.execute("DELETE FROM config_settings WHERE scope = 'global'")
            project_conn.commit()
            project_conn.close()
            _invalidate_config_request_cache()
        except Exception:
            # Migration failure must never break the caller. The rows stay
            # in the project DB and will be retried on next ConfigStore()
            # instance — but remove this project from the cache so we retry.
            with _MIGRATION_LOCK:
                _MIGRATED_PROJECTS.discard(project_key)

    def _sweep_removed_in_db(self, conn: sqlite3.Connection) -> dict[str, int]:
        """Drop every row whose setting_path is in _REMOVED_SETTINGS.

        Returns a {setting_path: rows_deleted} map for callers that want
        to surface the result (e.g. an admin sweep tool). Caller owns
        the connection's commit/close lifecycle.

        Idempotent: re-running on a clean DB is a no-op. Cross-scope
        (drops global / project / session rows alike for any removed
        key — once a key is loud-removed, it has no lawful storage).
        """
        if not _REMOVED_SETTINGS:
            return {}
        deleted: dict[str, int] = {}
        for key in _REMOVED_SETTINGS:
            cur = conn.execute(
                "DELETE FROM config_settings WHERE setting_path = ?",
                (key,),
            )
            if cur.rowcount:
                deleted[key] = cur.rowcount
        if deleted:
            _invalidate_config_request_cache()
        return deleted

    def _ensure_global_swept(self) -> None:
        """One-shot: sweep _REMOVED_SETTINGS rows from the global DB.

        Called lazily on the first global-scope connection. Per-process
        idempotency via _GLOBAL_SWEPT — the underlying SQL is itself
        idempotent so re-running across processes is safe too.
        """
        global _GLOBAL_SWEPT
        with _GLOBAL_SWEEP_LOCK:
            if _GLOBAL_SWEPT:
                return
            _GLOBAL_SWEPT = True
        global_db = _global_db_path()
        if not global_db.is_file():
            return
        try:
            conn = _canonical_connect(global_db)
            conn.execute(_CREATE_TABLE)
            try:
                self._sweep_removed_in_db(conn)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            with _GLOBAL_SWEEP_LOCK:
                _GLOBAL_SWEPT = False

    # ── Get / set / delete ───────────────────────────────────────────────

    def get(
        self,
        project_root: Path,
        setting_path: str,
        *,
        scope: str = "project",
        scope_key: str = "",
    ) -> Any | None:
        """Get a single setting value. Returns None if not set.

        Raises RemovedSettingError when the key was hard-removed in a
        migration. Loud failure prevents legacy semantics carrying
        forward via silent fall-through to None.
        """
        if setting_path in _REMOVED_SETTINGS:
            raise RemovedSettingError(
                f"Setting `{setting_path}` was removed. {_REMOVED_SETTINGS[setting_path]}",
            )
        with self._lock:
            conn = self._connect_for_scope(project_root, scope)
            try:
                row = conn.execute(
                    "SELECT value FROM config_settings "
                    "WHERE setting_path = ? AND scope = ? AND scope_key = ?",
                    (setting_path, scope, scope_key),
                ).fetchone()
                if row is None:
                    return None
                return json.loads(row["value"])
            finally:
                conn.close()

    def set(
        self,
        project_root: Path,
        setting_path: str,
        value: Any,
        *,
        scope: str = "project",
        scope_key: str = "",
    ) -> None:
        """Set a setting value in the appropriate DB for its scope.

        Audit gap fill (2026-04-21): every write emits a
        config_write_internal event. The MCP config_set tool ALREADY
        emits config_set via its own path; this covers the "someone
        called ConfigStore directly from a script" case so the audit
        chain never has silent config mutations.

        Raises RemovedSettingError when the key was hard-removed in a
        migration. Same loud-failure rationale as get().
        """
        if setting_path in _REMOVED_SETTINGS:
            raise RemovedSettingError(
                f"Setting `{setting_path}` was removed. {_REMOVED_SETTINGS[setting_path]}",
            )
        # Explicit revision invalidation for the request-scoped config cache:
        # a write makes any cached layer rows stale, so drop them. The next read
        # in this event re-reads the committed DB (verdict-correct, fresh).
        _invalidate_config_request_cache()
        # Scope-correctness at the mutation boundary (2026-05-25): a catalog
        # setting may only be written at a scope its catalog permits. This
        # stops a more-specific layer (session/project) from persisting an
        # override of a setting the platform pins to a coarser scope — e.g.
        # `distribution.flavor` (global-only) can never be set per-session to
        # forge a local-admin flavor. Specificity remains the RESOLUTION rule;
        # this only forbids WRITING where the catalog says you may not.
        # Unknown/runtime keys (e.g. `bash.*` policy tables) are exempt — they
        # are not catalog entries. Fail-closed: refuse rather than write.
        # The seal targets ONE direction: a MORE-SPECIFIC layer overriding a
        # setting the platform pins to a coarser scope (e.g. a per-session row
        # disabling a global/project security law). It refuses a write whose
        # scope is strictly more specific than the setting's most-specific
        # allowed scope. A coarser/broadening write (e.g. a breakglass profile
        # setting a project-scoped guardrail install-wide at global) is NOT the
        # override threat and is permitted. Catalog members only; unknown /
        # runtime keys (`bash.*`) are exempt. Specificity: global<project<session.
        _SPEC = {"global": 0, "project": 1, "session": 2}
        try:
            from .config_schema import SETTINGS_CATALOG as _CATALOG

            _meta = _CATALOG.get(setting_path)
        except Exception:
            _meta = None
        if _meta is not None and scope in _SPEC:
            _allowed = _meta.get("allowed_scopes") or ["project"]
            _max_allowed = max((_SPEC[s] for s in _allowed if s in _SPEC), default=2)
            if _SPEC[scope] > _max_allowed:
                raise ConfigScopeError(
                    f"Setting `{setting_path}` cannot be written at scope "
                    f"'{scope}': it is more specific than the setting's "
                    f"allowed scopes {sorted(_allowed)}. (A more-specific "
                    f"layer may not override a setting the platform pins to a "
                    f"coarser scope.)",
                )
        now = datetime.now(UTC).isoformat()
        json_value = json.dumps(value)
        # Snapshot previous value so the audit row can carry before+after.
        try:
            previous = self.get(
                project_root,
                setting_path,
                scope=scope,
                scope_key=scope_key,
            )
        except Exception:
            previous = None
        sensitive = _is_security_sensitive(setting_path)
        audit_prev = "[REDACTED]" if sensitive else previous
        audit_next = "[REDACTED]" if sensitive else value
        payload = {
            "scope": scope,
            "scope_key": scope_key,
            "previous": audit_prev,
            "new": audit_next,
            "security_sensitive": sensitive,
        }
        insert_sql = (
            "INSERT INTO config_settings (setting_path, scope, scope_key, value, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (setting_path, scope, scope_key) "
            "DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        )
        insert_args = (setting_path, scope, scope_key, json_value, now)

        if scope in _GLOBAL_SCOPES:
            # GLOBAL scope: config row lives in ~/.aidocs/config.sqlite3 but
            # the audit ledger is the per-project execution DB — DIFFERENT
            # files, so there is NO shared transaction. Design choice (A):
            # write the config row, then BEST-EFFORT audit; on audit
            # failure surface a degraded marker (never silently drop ink).
            with self._lock:
                conn = self._connect_for_scope(project_root, scope)
                try:
                    conn.execute(insert_sql, insert_args)
                    conn.commit()
                finally:
                    conn.close()
            _best_effort_global_audit(
                project_root,
                event_kind="config_write_internal",
                action_kind="config_write",
                capability_name="ConfigStore.set",
                target_entity=setting_path,
                payload=payload,
            )
            return

        # PROJECT/SESSION scope: config_settings and execution_events share
        # the same project DB, so the config row AND its audit row commit
        # in ONE transaction. If the audit write fails, the config mutation
        # rolls back — no mutation without ledger ink.
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().init_db(project_root)
        with self._lock:
            conn = self._connect_for_scope(project_root, scope)
            try:
                conn.execute("COMMIT")  # clear any implicit tx from connect DDL
            except Exception:
                pass
            try:
                conn.execute(insert_sql, insert_args)
                ExecutionIndexStore().record_event_on_connection(
                    conn,
                    project_root,
                    event_kind="config_write_internal",
                    source_kind="config_store",
                    session_id=scope_key if scope == "session" else None,
                    capability_name="ConfigStore.set",
                    action_kind="config_write",
                    target_entity=setting_path,
                    status="applied",
                    payload=payload,
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def delete(
        self,
        project_root: Path,
        setting_path: str,
        *,
        scope: str = "project",
        scope_key: str = "",
    ) -> bool:
        """Delete a setting. Returns True if it existed.

        Audit (config_delete_internal) is emitted ONLY when a row was
        actually removed (rule A: no mutation → no audit). For PROJECT/
        SESSION scope the delete + audit commit in ONE transaction (audit
        failure rolls back the delete). For GLOBAL scope (cross-DB) the
        audit is best-effort with a degraded marker on failure.
        """
        # Explicit revision invalidation: an unset makes any cached layer rows
        # stale, so drop them (a read-after-delete in the same event must NOT
        # see the removed value). Same contract as set().
        _invalidate_config_request_cache()
        sensitive = _is_security_sensitive(setting_path)
        select_sql = (
            "SELECT value FROM config_settings "
            "WHERE setting_path = ? AND scope = ? AND scope_key = ?"
        )
        delete_sql = (
            "DELETE FROM config_settings WHERE setting_path = ? AND scope = ? AND scope_key = ?"
        )
        key_args = (setting_path, scope, scope_key)

        def _read_previous(conn):
            prev_row = conn.execute(select_sql, key_args).fetchone()
            if prev_row is None:
                return None
            try:
                return json.loads(prev_row["value"])
            except Exception:
                return prev_row["value"]

        def _audit_payload(previous):
            return {
                "scope": scope,
                "scope_key": scope_key,
                "previous": "[REDACTED]" if sensitive else previous,
                "security_sensitive": sensitive,
            }

        if scope in _GLOBAL_SCOPES:
            # GLOBAL: cross-DB, non-atomic (design A — best-effort audit).
            with self._lock:
                conn = self._connect_for_scope(project_root, scope)
                try:
                    previous = _read_previous(conn)
                    existed = conn.execute(delete_sql, key_args).rowcount > 0
                    conn.commit()
                finally:
                    conn.close()
            if existed:
                _best_effort_global_audit(
                    project_root,
                    event_kind="config_delete_internal",
                    action_kind="config_delete",
                    capability_name="ConfigStore.delete",
                    target_entity=setting_path,
                    payload=_audit_payload(previous),
                )
            return existed

        # PROJECT/SESSION: delete + audit atomic in the shared project DB.
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().init_db(project_root)
        with self._lock:
            conn = self._connect_for_scope(project_root, scope)
            try:
                conn.execute("COMMIT")  # clear implicit tx from connect DDL
            except Exception:
                pass
            try:
                previous = _read_previous(conn)
                existed = conn.execute(delete_sql, key_args).rowcount > 0
                if existed:
                    ExecutionIndexStore().record_event_on_connection(
                        conn,
                        project_root,
                        event_kind="config_delete_internal",
                        source_kind="config_store",
                        session_id=scope_key if scope == "session" else None,
                        capability_name="ConfigStore.delete",
                        action_kind="config_delete",
                        target_entity=setting_path,
                        status="applied",
                        payload=_audit_payload(previous),
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()
        return existed

    # ── Effective resolution across both DBs ─────────────────────────────

    def get_effective(
        self,
        project_root: Path,
        setting_path: str,
        *,
        session_id: str | None = None,
        default: Any = None,
    ) -> Any:
        """Get the effective value via the canonical resolver.

        Phase 4 (2026-05-02): delegates to ConfigResolver. The 5-layer
        cascade (factory > global > project > session) replaces the
        previous shallow-merge path that produced the wormhole bug —
        root-namespace queries clobbered factory siblings whenever any
        sub-key write existed.

        Pass session_id to include the session layer.
        Raises RemovedSettingError when the key was hard-removed.
        """
        if setting_path in _REMOVED_SETTINGS:
            raise RemovedSettingError(
                f"Setting `{setting_path}` was removed. {_REMOVED_SETTINGS[setting_path]}",
            )
        with self._lock:
            self._ensure_project_migrated(project_root)
            self._ensure_global_swept()

        from .config_resolver import LayeredConfigResolver

        resolved = LayeredConfigResolver().resolve(
            setting_path,
            project_root,
            session_id=session_id,
        )
        if resolved.value is None:
            return default
        return resolved.value


    # ── Bulk reads (used by dashboard) ───────────────────────────────────

    def get_all(
        self,
        project_root: Path,
        *,
        scope: str | None = None,
        scope_key: str = "",
    ) -> dict[str, Any]:
        """Get all settings, optionally filtered by scope.

        If scope is None, returns rows from both DBs, keyed by setting_path.
        When multiple scopes have the same path, the highest-priority scope
        wins (session > project > global), matching get_effective semantics.
        """
        with self._lock:
            self._ensure_project_migrated(project_root)

            def read(db_path: Path, where: str, params: tuple) -> list[sqlite3.Row]:
                if not db_path.is_file():
                    return []
                conn = self._connect(db_path)
                try:
                    return conn.execute(where, params).fetchall()
                finally:
                    conn.close()

            if scope is not None:
                where = "SELECT setting_path, value FROM config_settings WHERE scope = ? AND scope_key = ?"
                if scope in _GLOBAL_SCOPES:
                    rows = read(_global_db_path(), where, (scope, scope_key))
                else:
                    rows = read(_project_db_path(project_root), where, (scope, scope_key))
                return {row["setting_path"]: json.loads(row["value"]) for row in rows}

            # scope=None — merge both DBs with priority cascade
            combined: dict[str, tuple[int, Any]] = {}
            for db_path in (_global_db_path(), _project_db_path(project_root)):
                rows = read(
                    db_path,
                    "SELECT setting_path, scope, value FROM config_settings",
                    (),
                )
                for row in rows:
                    prio = _SCOPE_PRIORITY.get(row["scope"], -1)
                    key = row["setting_path"]
                    existing = combined.get(key)
                    if existing is None or prio > existing[0]:
                        combined[key] = (prio, json.loads(row["value"]))
            return {path: value for path, (_prio, value) in combined.items()}

    def get_all_with_metadata(
        self,
        project_root: Path,
    ) -> list[dict[str, Any]]:
        """Get all settings from both DBs with full metadata (scope, updated_at)."""
        with self._lock:
            self._ensure_project_migrated(project_root)
            result: list[dict[str, Any]] = []
            for db_path in (_global_db_path(), _project_db_path(project_root)):
                if not db_path.is_file():
                    continue
                conn = self._connect(db_path)
                try:
                    rows = conn.execute(
                        "SELECT setting_path, scope, scope_key, value, updated_at "
                        "FROM config_settings ORDER BY setting_path, scope",
                    ).fetchall()
                    for row in rows:
                        result.append(
                            {
                                "setting_path": row["setting_path"],
                                "scope": row["scope"],
                                "scope_key": row["scope_key"],
                                "value": json.loads(row["value"]),
                                "updated_at": row["updated_at"],
                            },
                        )
                finally:
                    conn.close()
            return result

    def effective_config(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
        defaults: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Build the full effective config dict, matching the old TOML format.

        Starts from defaults, then layers DB values across both physical
        stores by scope cascade (session > project > global).
        """
        from collections import defaultdict
        from copy import deepcopy

        result: dict[str, Any] = deepcopy(defaults) if defaults else {}

        with self._lock:
            self._ensure_project_migrated(project_root)
            by_path: dict[str, dict[str, Any]] = defaultdict(dict)

            # Project + session rows come from project DB
            project_db = _project_db_path(project_root)
            if project_db.is_file():
                pconn = self._connect(project_db)
                try:
                    rows = pconn.execute(
                        "SELECT setting_path, scope, scope_key, value FROM config_settings",
                    ).fetchall()
                    for row in rows:
                        scope = row["scope"]
                        scope_key = row["scope_key"]
                        if scope == "session":
                            if session_id and scope_key == session_id:
                                by_path[row["setting_path"]]["session"] = json.loads(row["value"])
                        elif scope == "project":
                            by_path[row["setting_path"]]["project"] = json.loads(row["value"])
                finally:
                    pconn.close()

            # Global rows come from the install-wide DB — or, for a tenant
            # project with no request bound, from that ORG's own global store
            # (#497), which is the same store the request path resolves.
            global_db = _global_db_path(project_root)
            if global_db.is_file():
                gconn = self._connect(global_db)
                try:
                    rows = gconn.execute(
                        "SELECT setting_path, value FROM config_settings "
                        "WHERE scope = 'global' AND scope_key = ''",
                    ).fetchall()
                    for row in rows:
                        by_path[row["setting_path"]]["global"] = json.loads(row["value"])
                finally:
                    gconn.close()

        for setting_path, scope_values in by_path.items():
            value = None
            for scope in ("session", "project", "global"):
                if scope in scope_values:
                    value = scope_values[scope]
                    break
            if value is None:
                continue
            # Write into nested dict: "security.enforce" → result["gate"]["enforce"]
            parts = setting_path.split(".")
            current = result
            for part in parts[:-1]:
                if part not in current or not isinstance(current[part], dict):
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value

        return result

    # ── TOML import (legacy migration) ───────────────────────────────────

    def import_from_toml(
        self,
        project_root: Path,
        toml_path: Path,
        *,
        scope: str = "project",
        scope_key: str = "",
        overwrite: bool = False,
    ) -> int:
        """Import settings from a TOML file into the DB (scope-routed).

        Args:
            project_root: Project root (used only for project/session scopes).
            toml_path: Path to the TOML file to import.
            scope: Scope to assign to imported settings.
            scope_key: Scope key (e.g. session_id).
            overwrite: If True, overwrite existing DB values. If False, skip existing.

        Returns:
            Number of settings imported.

        """
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return 0

        if not toml_path.is_file():
            return 0

        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except Exception:
            return 0

        count = 0
        flat = _flatten_dict(data)
        for setting_path, value in flat.items():
            # Skip non-setting keys (interaction.*, policies.*)
            if setting_path.startswith("interaction.") or setting_path.startswith("policies."):
                continue
            if not overwrite:
                existing = self.get(project_root, setting_path, scope=scope, scope_key=scope_key)
                if existing is not None:
                    continue
            self.set(project_root, setting_path, value, scope=scope, scope_key=scope_key)
            count += 1

        return count


def _flatten_dict(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dict: {"gate": {"enforce": True}} → {"security.enforce": True}."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            result.update(_flatten_dict(value, full_key))
        else:
            result[full_key] = value
    return result
