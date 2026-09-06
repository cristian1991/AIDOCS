"""Canonical taxonomy — unified read surface across heterogeneous stores.

Per /goal 2026-05-19 ("Clean the AIDOCS throne database") the audit charter
demanded a canonical taxonomy:

    stable_key, kind, scope, source, status, version, revision,
    superseded_by, read_access, created_at, updated_at

Different tables in `aidocs.sqlite3` serve genuinely different purposes
and have evolved their own schemas. Rather than destructively
restructuring (high risk, breaks every store and every host adapter),
this module installs a sqlite VIEW that projects each canonical store's
rows into the unified taxonomy.

Architectural shape: classic star-schema pattern — keep the
source-of-truth tables intact, expose a single ``canonical_rows`` view
over them.

## The unified row shape

Every row in ``canonical_rows`` carries:

  stable_key    TEXT  — globally unique key across all stores. Prefixed
                       by the source table to prevent collisions:
                       ``memory:domains/bugs.md``, ``backlog:#42``,
                       ``capability:ai_find``, ``task:task-abc-123``,
                       ``grant:s1:grep``, ``session:2026-05-19-a``,
                       ``skill:bundled/brainstorming``.
  kind          TEXT  — the row's intrinsic kind. memory_files.kind
                       ("invariant", "preference", …), backlog =
                       "bug/todo", capability_definitions.capability_kind,
                       task_todos = "task", sticky_grants.tool, etc.
  scope         TEXT  — "project" | "session" | "global" | "lane"
                       (broadest where it applies).
  source        TEXT  — where the row originated: "markdown",
                       "sqlite-native", "introspected", "host-event",
                       "capture-tool", "operator-grant".
  status        TEXT  — "active" | "completed" | "removed" | "pending"
                       | "expired" | "superseded".
  version       TEXT  — checksum / updated_at / revision id — anything
                       that changes when the row's content changes.
  revision      INTEGER — monotonic per-stable_key revision. 1 for
                       single-revision stores; per-row counter for stores
                       that mutate in place.
  superseded_by TEXT  — stable_key of the row that replaces this one,
                       or NULL when the row is still current.
  read_access   TEXT  — "public" | "sovereign" | "audit" — who may
                       read this row. Sovereign rows (head-conductor.md,
                       co-conductor.md) are restricted-write doctrine.
  created_at    TEXT  — ISO-8601 timestamp or NULL when source has none.
  updated_at    TEXT  — same.

## Why a VIEW, not a table

- Zero data migration: no row is moved, copied, or rewritten.
- Zero behavior change for every existing reader/writer of every
  source table.
- Idempotent install: ``CREATE VIEW IF NOT EXISTS`` runs free on every
  connection; no migration state to track.
- Fresh DB and upgraded DB produce IDENTICAL canonical_rows output for
  the same underlying data, because the projection logic is the same.
  Pinned by test_canonical_taxonomy.test_fresh_and_upgraded_db_match.

## Stores projected

  memory_files            → kind=<file kind>,  scope='project',  source='markdown'
  project_backlog         → kind='bug/todo',   scope='project',  source='sqlite-native'
  task_todos              → kind='task',        scope='session',  source='sqlite-native'
  session_todos           → kind='session-todo',scope='session',  source='sqlite-native'
  capability_definitions  → kind=capability_kind, scope='global',  source='introspected'
  sticky_grants           → kind='raw-tool-grant', scope='session', source='operator-grant'
  sessions                → kind='session',     scope='project',  source='sqlite-native'
  skill_providers         → kind='skill-provider', scope='global', source='sqlite-native'
  session_skills          → kind='session-skill', scope='session', source='sqlite-native'

(``aidocs_managed_per_conductor`` is **excluded** — it's runtime
host-binding state, not doctrine. Its rows mutate on every
session_connect / reconnect cycle and surfacing them in the
canonical view would conflate "doctrine" with "current host
identity". See EXCLUDED_FROM_CANONICAL_VIEW below.)

Tables intentionally NOT in canonical_rows:

  - execution_events / execution_runs / edit_history / result_artifacts /
    run_duration_buckets — append-only audit; their rows aren't doctrine,
    they're forensics. Surfacing them in the canonical view would explode
    its row count and dilute its meaning.
  - code_files / code_edges / code_outlines / code_modules — code-index
    projections, rebuilt by ai_index_sync. Not part of the doctrine /
    role / soul / skill / backlog universe the audit covers.
  - memory_links / memory_routes / memory_route_keywords / memory_symbol_anchors
    — derived projections OVER memory_files. Including them would
    duplicate rows.
  - schema_entities / schema_fields / schema_relationships — empty in
    this project.
  - session_lane_agents / session_lane_mailbox / session_query_gate /
    plan_conductor_state / lane_completion_reviews — runtime state, not
    doctrine.
  - cross_turn_scrutiny_window / cross_turn_scrutiny_signals — #651's
    per-session sliding window of prompt SHAPES and the counters derived
    from it. Scratch for one session's advisory scrutiny signal; the next
    turns rebuild it. Not a claim about the project.
  - window_conversation_state — #876's `<claude.exe pid>:<creation
    filetime>` -> the conversation that window declared at SessionStart.
    Runtime host-binding state, the same class as
    aidocs_managed_per_conductor; rebuilt by the next SessionStart of each
    live window. Not a claim about the project.
  - config_settings / resolved_config / project_info / index_meta /
    workflow_actions — singletons and config snapshots.
  - sync_event_receipts — authority-receipts ledger for the durable
    memory-sync system (War 1 #376, ``sync_store.py::record_receipt``):
    one row per locally-committed canonical mutation event, consulted by
    ``split_by_authority`` to separate authoritative from incoming events
    at hydration. Mutation-authority bookkeeping, not doctrine rows.

The exclusions are listed in STORE_INVENTORY.md §1 with rationale; the
test_canonical_taxonomy.test_excluded_tables_are_documented test
forbids silently dropping a new canonical-shaped store on the floor.
"""

from __future__ import annotations

import hashlib
import sqlite3

# View DDL. Each branch of the UNION ALL projects one source table.
#
# CREATE VIEW IF NOT EXISTS is idempotent — running on a fresh DB
# creates the view; running on an upgraded DB is a no-op. Either way
# the projection logic is the same, so the canonical_rows surface is
# guaranteed identical for the same underlying data. This is the
# "fresh DB ≡ upgraded DB" contract the /goal demands.
CANONICAL_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS canonical_rows AS
-- ── memory_index: sqlite-canonical doctrine / rules / system / etc. ──
--   Phase-8 flip (2026-05-19): memory_index is now the durable
--   write target; .MEMORY/**/*.md are exports/seeds.
SELECT
    'memory:' || path                                  AS stable_key,
    kind                                                AS kind,
    'project'                                           AS scope,
    CASE source
        WHEN 'capture'        THEN 'capture-tool'
        WHEN 'migration'      THEN 'markdown'
        WHEN 'markdown_seed'  THEN 'markdown'
        WHEN 'sovereign'      THEN 'sovereign-edit'
        ELSE source
    END                                                 AS source,
    status                                              AS status,
    checksum                                            AS version,
    1                                                   AS revision,
    NULLIF(superseded_by, '')                           AS superseded_by,
    CASE
        WHEN source = 'sovereign' THEN 'sovereign'
        WHEN path LIKE 'skills/head-conductor.md' THEN 'sovereign'
        WHEN path LIKE 'skills/co-conductor.md'   THEN 'sovereign'
        ELSE 'public'
    END                                                 AS read_access,
    created_at                                          AS created_at,
    updated_at                                          AS updated_at,
    'memory_index'                                      AS source_table
FROM memory_index
WHERE status != 'removed'

UNION ALL

-- ── project_backlog: canonical bug/todo store ──
SELECT
    'backlog:#' || id                                  AS stable_key,
    'bug/todo'                                          AS kind,
    'project'                                           AS scope,
    'sqlite-native'                                     AS source,
    status                                              AS status,
    updated_at                                          AS version,
    1                                                   AS revision,
    NULL                                                AS superseded_by,
    'public'                                            AS read_access,
    created_at                                          AS created_at,
    updated_at                                          AS updated_at,
    'project_backlog'                                   AS source_table
FROM project_backlog

UNION ALL

-- ── task_todos: task lifecycle rows ──
SELECT
    'task:' || task_id                                 AS stable_key,
    'task'                                              AS kind,
    'session'                                           AS scope,
    'sqlite-native'                                     AS source,
    COALESCE(status, 'active')                          AS status,
    COALESCE(updated_at, created_at)                    AS version,
    1                                                   AS revision,
    NULL                                                AS superseded_by,
    'public'                                            AS read_access,
    created_at                                          AS created_at,
    updated_at                                          AS updated_at,
    'task_todos'                                        AS source_table
FROM task_todos

UNION ALL

-- ── capability_definitions: introspected MCP tool registry ──
SELECT
    'capability:' || name                              AS stable_key,
    capability_kind                                     AS kind,
    'global'                                            AS scope,
    'introspected'                                      AS source,
    'active'                                            AS status,
    checksum                                            AS version,
    1                                                   AS revision,
    NULL                                                AS superseded_by,
    'public'                                            AS read_access,
    discovered_at                                       AS created_at,
    discovered_at                                       AS updated_at,
    'capability_definitions'                            AS source_table
FROM capability_definitions

UNION ALL

-- ── sticky_grants: operator-granted raw-tool access ──
SELECT
    'grant:' || grant_id                               AS stable_key,
    'raw-tool-grant'                                    AS kind,
    'session'                                           AS scope,
    'operator-grant'                                    AS source,
    CASE WHEN revoked_at IS NULL THEN 'active' ELSE 'expired' END AS status,
    registered_at                                       AS version,
    1                                                   AS revision,
    NULL                                                AS superseded_by,
    'audit'                                             AS read_access,
    registered_at                                       AS created_at,
    COALESCE(revoked_at, registered_at)                 AS updated_at,
    'sticky_grants'                                     AS source_table
FROM sticky_grants

UNION ALL

-- ── sessions: per-project session rows ──
SELECT
    'session:' || session_id                           AS stable_key,
    'session'                                           AS kind,
    'project'                                           AS scope,
    'sqlite-native'                                     AS source,
    COALESCE(status, 'active')                          AS status,
    last_updated                                        AS version,
    1                                                   AS revision,
    NULL                                                AS superseded_by,
    'public'                                            AS read_access,
    NULL                                                AS created_at,
    last_updated                                        AS updated_at,
    'sessions'                                          AS source_table
FROM sessions

UNION ALL

-- ── skill_providers: registered skill providers (JSON blob per row) ──
SELECT
    'skill-providers:' || id                           AS stable_key,
    'skill-provider'                                    AS kind,
    'global'                                            AS scope,
    'sqlite-native'                                     AS source,
    'active'                                            AS status,
    updated_at                                          AS version,
    1                                                   AS revision,
    NULL                                                AS superseded_by,
    'public'                                            AS read_access,
    updated_at                                          AS created_at,
    updated_at                                          AS updated_at,
    'skill_providers'                                   AS source_table
FROM skill_providers

UNION ALL

-- ── session_skills: per-session selected skills bundle ──
SELECT
    'session-skills:' || session_id                    AS stable_key,
    'session-skills'                                    AS kind,
    'session'                                           AS scope,
    'sqlite-native'                                     AS source,
    'active'                                            AS status,
    updated_at                                          AS version,
    1                                                   AS revision,
    NULL                                                AS superseded_by,
    'public'                                            AS read_access,
    updated_at                                          AS created_at,
    updated_at                                          AS updated_at,
    'session_skills'                                    AS source_table
FROM session_skills

UNION ALL

-- ── session_todos: ad-hoc per-session todo bundle (JSON) ──
SELECT
    'session-todos:' || session_id                     AS stable_key,
    'session-todos'                                     AS kind,
    'session'                                           AS scope,
    'sqlite-native'                                     AS source,
    'active'                                            AS status,
    updated_at                                          AS version,
    1                                                   AS revision,
    NULL                                                AS superseded_by,
    'public'                                            AS read_access,
    updated_at                                          AS created_at,
    updated_at                                          AS updated_at,
    'session_todos'                                     AS source_table
FROM session_todos
;
"""


# Canonical column set — the contract the view exposes. Used by tests
# to assert fresh DB ≡ upgraded DB on the canonical surface.
CANONICAL_COLUMNS: tuple[str, ...] = (
    "stable_key",
    "kind",
    "scope",
    "source",
    "status",
    "version",
    "revision",
    "superseded_by",
    "read_access",
    "created_at",
    "updated_at",
    "source_table",
)

# Stores deliberately excluded from the canonical view. Update both
# this set AND the prose in this module's docstring whenever a new
# store appears that should NOT be canonicalized. test_canonical_taxonomy
# pins this list so silent omissions fail CI.
EXCLUDED_FROM_CANONICAL_VIEW: frozenset[str] = frozenset(
    {
        # Append-only audit
        "execution_events",
        "execution_runs",
        "edit_history",
        "result_artifacts",
        "run_duration_buckets",
        # Code-index projections
        "code_files",
        "code_edges",
        "code_outlines",
        "code_modules",
        "code_references",
        # Memory-derived projections (would duplicate memory_index rows).
        # ``memory_files`` is now itself a legacy mirror — the Phase-8 flip
        # (2026-05-19) moved the source-of-truth to memory_index, with
        # memory_files left in place as a read-only cache for sync_memory_files.
        "memory_files",
        "memory_links",
        "memory_routes",
        "memory_route_keywords",
        "memory_symbol_anchors",
        # #688: a rebuildable NLP projection of memory_route_keywords —
        # (keyword, language) -> lemmas, stamped with the model that made it.
        # Never doctrine: drop it and the next prompt regenerates it.
        "keyword_lemmas",
        # Schema-query (empty in this project; not doctrine)
        "schema_entities",
        "schema_fields",
        "schema_relationships",
        # Runtime state, not doctrine
        # #651 cross-turn scrutiny: a per-session sliding window of prompt
        # SHAPES (session_key, seq, shape) and the counters derived from it.
        # Both are scratch for one session's advisory scrutiny signal — drop
        # them and the next turns rebuild the window. Nothing here is a claim
        # about the project, which is what canonical_rows collects.
        "cross_turn_scrutiny_window",
        "cross_turn_scrutiny_signals",
        # 2026-08-23: the index reconciler's heartbeat — ONE upserted row
        # holding the last + previous reconcile moments. It replaced 30,202
        # per-occurrence `index_sitter_reconcile` audit events. Pure runtime
        # state, rebuilt by the next reconcile; never a claim about the
        # project, so never canonical.
        "index_reconcile_state",
        # #876 phase 1 (2026-08-23): `<claude.exe pid>:<creation filetime>` ->
        # the conversation that window most recently declared at SessionStart.
        # Runtime host-binding state — the same class as
        # `aidocs_managed_per_conductor`, whose rows also mutate on every
        # connect/reconnect cycle. Surfacing it in the canonical view would
        # conflate "doctrine" with "current host identity". Rebuilt by the next
        # SessionStart of each live window; never a claim about the project.
        "window_conversation_state",
        "session_lane_agents",
        "session_lane_mailbox",
        "session_query_gate",
        "session_host_skill_state",
        "plan_conductor_state",
        "lane_completion_reviews",
        # Singletons / config snapshots
        "config_settings",
        "resolved_config",
        "project_info",
        "index_meta",
        "workflow_actions",
        "aidocs_managed",
        "aidocs_managed_per_conductor",
        # Pending grants (subset of sticky_grants lifecycle, not authoritative)
        "sticky_grants_pending",
        # Vestigial empty tables
        "procedure_definitions",
        "procedure_capability_links",
        "palace_disable_state",
        "session_king_field",
        # Update-intent durability ledger (#219/#221) — a runtime queue staged
        # behind the durable-write flow, not a canonical memory source.
        "pending_durable_writes",
        "pending_dw_turns",
        # Control-plane authority registries — each is the SOLE authority for its
        # own domain (session existence / MCP server registry), with a paired
        # _meta marker table. They are not unified into the canonical row view.
        "session_membership",
        "session_membership_meta",
        "mcp_servers",
        "mcp_registry_meta",
        # Index/sitter runtime state — freshness + revision bookkeeping and
        # the sitter heartbeat. Operational telemetry, not doctrine rows.
        "freshness_snapshot",
        "index_revision",
        "sitter_heartbeat",
        # ai_task lifecycle store (runtime task state, not a canonical
        # doctrine source — task_todos is the surfaced projection).
        "tasks",
        # Enforcement notice rails (War M files->DB Phase A, #445 —
        # db14b1d9): freeze-strike between-block notices and the deploy
        # edit-window notice-surface cap ledger. Runtime enforcement
        # state with TTL-like lifecycles, not doctrine rows.
        "freeze_strike_notices",
        "deploy_notice_surfaces",
        # Authority-receipts ledger for the durable memory-sync system
        # (War 1 #376, sync_store.py::record_receipt) — one row per
        # locally-committed canonical mutation event; the receipt set is
        # the hydration authority boundary (split_by_authority). Mutation
        # bookkeeping, not doctrine rows.
        "sync_event_receipts",
        # #375 Phase 3 (memory-home flip): body-projection staging ledger +
        # landing receipts (memory_body_staging_store) and the one-shot
        # migration stamp table (memory_home_migrator). Durability/receipt
        # bookkeeping over memory_index bodies, not doctrine rows.
        "pending_palace_projections",
        "palace_projection_receipts",
        "memory_home_migrations",
        # War AZ #474 (session_response_ledger.py): per-session grounding
        # ledger for the response envelope — notification dedupe keys +
        # state hashes, the session's last-known task lifecycle snapshot,
        # and the per-session surfaced-file set. Runtime conversation
        # state of the tool surface itself, not doctrine rows.
        "session_response_ledger",
        "session_lifecycle_snapshot",
        "session_surfaced_files",
        # War R #475 (session_scaffold_grant_store.py): conductor-minted,
        # TTL-bounded, pattern-scoped grants letting a dispatched war's
        # task_begin scaffold its named session. Authorization bookkeeping
        # on the query-gate grant precedent, not doctrine rows.
        "session_scaffold_grants",
        # War R #475 (session_md_compaction.py): one-shot stamp rows for the
        # SESSION.md blank-line compaction sweep (migrator-pattern ledger).
        # Maintenance bookkeeping, not doctrine rows.
        "session_md_compactions",
        # War HH (tool_usage_report_store.py, Emperor charter 2026-07-19):
        # structured tool-feedback rows from superadmin/dev task_complete
        # tool_report payloads — harvested into backlog #469 as digest
        # annotations. Feedback telemetry, not doctrine rows.
        "tool_usage_reports",
        # #463 (task_actor_identity / actor task-state store), extended by
        # War JJ #483 (83520f87) to EVERY host-derived actor: per-actor
        # active-task slots. Runtime lifecycle state, not doctrine rows.
        "actor_task_state",
        # War AU #467 (causal_turn_store.py, 27b9c5a4): the causal-turn
        # contract's five tables — turn state machine, hash-chained
        # instruction events, interrupt governance events, orphan
        # resolutions, and merkle turn seals. Append-only audit provenance,
        # not doctrine rows.
        "causal_turns",
        "instruction_events",
        "interrupt_events",
        "orphan_resolutions",
        "turn_seals",
    },
)

# Stores INCLUDED in canonical view (mirror of the UNION branches above).
CANONICAL_SOURCE_TABLES: tuple[str, ...] = (
    # Phase-8 flip (2026-05-19): memory_index supplants memory_files
    # as the canonical doctrine/rules/system store.
    "memory_index",
    "project_backlog",
    "task_todos",
    "capability_definitions",
    "sticky_grants",
    "sessions",
    "skill_providers",
    "session_skills",
    "session_todos",
)


def _canonical_view_version() -> str:
    """Short hash of the current canonical_rows DDL. When the DDL
    changes — new source table, renamed column, additional projection
    branch — this hash changes too. Embedded as a comment line inside
    the view definition so an old DB's ``sqlite_master.sql`` carries
    its build-time version. Install path compares versions to decide
    DROP-and-rebuild vs no-op.

    Hash, not version string: keeps the contract self-describing.
    Anyone editing CANONICAL_VIEW_DDL automatically bumps the version;
    there's no separate "remember to bump VERSION" footgun.
    """
    return hashlib.sha256(CANONICAL_VIEW_DDL.encode("utf-8")).hexdigest()[:16]


def _versioned_view_ddl() -> str:
    """The DDL with a version marker as the first comment line. The
    marker lands inside the view body and survives in
    ``sqlite_master.sql``, making upgrades detectable.
    """
    version = _canonical_view_version()
    # Insert the marker as the SECOND line — first line is the
    # CREATE VIEW preamble. The marker is a SQL comment so it has
    # zero effect on query behavior but is preserved verbatim by
    # sqlite in the view's stored sql.
    lines = CANONICAL_VIEW_DDL.splitlines(keepends=True)
    # Find the CREATE VIEW line and insert the version comment right
    # after the "AS" so the marker is inside the view body.
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and " AS" in line.upper() and "CREATE VIEW" in line.upper():
            out.append(f"-- canonical_view_version: {version}\n")
            inserted = True
    if not inserted:
        # Fallback (DDL shape changed); prepend the marker.
        return f"-- canonical_view_version: {version}\n" + CANONICAL_VIEW_DDL
    return "".join(out)


def _existing_view_version(conn: sqlite3.Connection) -> str | None:
    """Return the version marker embedded in the live canonical_rows
    view's stored sql, or None when the view is absent / has no marker
    (i.e. was installed by a pre-versioning build).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='canonical_rows'",
    ).fetchone()
    if row is None or row[0] is None:
        return None
    stored = str(row[0])
    # Marker format: "-- canonical_view_version: <16-hex>"
    for line in stored.splitlines():
        s = line.strip()
        if s.startswith("-- canonical_view_version:"):
            return s.split(":", 1)[1].strip()
    return ""  # view exists but has no marker (legacy install)


def install_canonical_view(conn: sqlite3.Connection) -> None:
    """Idempotently install or upgrade the ``canonical_rows`` view.

    Three states handled:

      1. View ABSENT → CREATE.
      2. View PRESENT and version marker matches current DDL → no-op.
      3. View PRESENT with stale or missing version marker → DROP +
         CREATE (the live view shows old column shape or old
         projection). Source-table data is untouched: DROP VIEW
         only removes the read surface, never the underlying rows.

    The version marker is a SHA-256 prefix of CANONICAL_VIEW_DDL
    embedded in the view body as a SQL comment. Sqlite preserves the
    comment in ``sqlite_master.sql`` so the marker survives across
    process restarts.

    If a source table is missing (cold DB before its store init has
    run), install silently aborts — callers should run init_db on the
    relevant stores first. The view becomes installable on the next
    connection once the schema completes.

    Safe to call on any connection. Never modifies source-table data.
    """
    # Verify every source table exists before creating the view; a
    # missing table would make ``SELECT FROM canonical_rows`` fail at
    # query time with a confusing error.
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {r[0] for r in cur}
    missing = [t for t in CANONICAL_SOURCE_TABLES if t not in existing]
    if missing:
        return

    current_version = _canonical_view_version()
    existing_version = _existing_view_version(conn)

    if existing_version == current_version:
        # View already at current version — no-op fast path.
        return

    if existing_version is not None:
        # View present but stale (different version OR no marker at all,
        # which means a pre-versioning install). DROP and rebuild.
        # The DROP is non-destructive: source tables are untouched.
        conn.execute("DROP VIEW IF EXISTS canonical_rows")

    conn.executescript(_versioned_view_ddl())


def drop_canonical_view(conn: sqlite3.Connection) -> None:
    """Drop the canonical view. Used by tests to verify the install
    path is genuinely idempotent (drop + reinstall produces the same
    view definition).
    """
    conn.execute("DROP VIEW IF EXISTS canonical_rows")
