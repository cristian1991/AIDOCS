from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Process-level "schema ensured" guard (UPS sqlite-open seal, 2026-06-02): keyed
# by resolved db path. Idempotent DDL only needs to run once per process; this
# removes the redundant init_db connection on every record_event/read. Never
# cached across processes (a fresh process re-ensures), so cross-process truth
# and audit-row durability are unchanged. A db is only marked ensured once its
# additive migrations verifiably reached a known-good state: ONLY a benign
# already-exists (the migration already ran) counts as success. A transient
# lock/busy AND every UNKNOWN failure leave it UNmarked so the next call retries
# (perf degrades to the pre-seal open count, never to a missing column).
# Identity-validated (2026-07-09): keyed on the db FILE identity, not just
# its path — a recycled path with a fresh db (pytest retention-reused tmp
# dirs; project re-init under the long-lived daemon) re-ensures instead of
# skipping into 'no such table'. See schema_memo.py for the full autopsy.
from .schema_memo import SchemaMemo

_SCHEMA_ENSURED = SchemaMemo()


def _is_benign_already_exists(exc: Exception) -> bool:
    """True only for the success-equivalent case: the additive step already ran.
    ``ALTER TABLE ADD COLUMN`` on an existing column raises 'duplicate column
    name'; a re-applied object raises 'already exists'. These mean the schema is
    AT the target state — safe to cache as ensured."""
    msg = str(exc).lower()
    return "duplicate column name" in msg or "already exists" in msg


def _is_transient_lock(exc: Exception) -> bool:
    """True for a retryable SQLite lock/busy error (a KNOWN-transient cause; not
    surfaced as an anomaly). Distinct from an unknown failure, which is both
    retryable AND worth surfacing."""
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg or "locked" in msg


def _classify_migration_error(exc: Exception, step: str) -> bool:
    """Classify an additive-migration/index error. Returns True when the step is
    a benign already-exists (cache may proceed). Returns False for lock/busy and
    for EVERY unknown failure (leave unmarked → retry stays possible); an unknown
    failure is surfaced honestly to stderr so a real schema bug is never hidden
    behind the perf cache."""
    if _is_benign_already_exists(exc):
        return True
    if not _is_transient_lock(exc):
        # Unknown, non-transient: retryable but anomalous — do not hide it.
        sys.stderr.write(
            f"[aidocs execution_index init_db] unexpected {step} migration error "
            f"(NOT caching schema-ensured; will retry): {type(exc).__name__}: {exc}\n",
        )
    return False


class ExecutionIndexStore:
    """Derived SQLite index for execution runs and event evidence."""

    _LIFECYCLE_TOOLS = {
        "task_begin",
        "task_update",
        "task_complete",
    }
    _EDIT_LIKE_TOOLS = {
        "edit",
        "write",
        "bash",
        "ai_edit_lines",
        "ai_batch_edit",
        "ai_create_file",
        "ai_str_replace",
    }
    _MEANINGFUL_MCP_PREFIXES = (
        "ai_",
        "code_",
        "schema_",
        "memory_",
        "session_",
    )

    def index_root(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index"

    def db_path(self, project_root: Path) -> Path:
        return self.index_root(project_root) / "aidocs.sqlite3"

    def connect(self, project_root: Path) -> sqlite3.Connection:
        db_path = self.db_path(project_root)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self, project_root: Path) -> None:
        # Schema is code-defined and idempotent (CREATE TABLE IF NOT EXISTS), so
        # re-running it on every record_event / list_events / summary within a
        # process is pure sqlite-open tax. Ensure it AT MOST ONCE per (process,
        # db) — the actual read/write each still opens its own connection, so
        # every audit row is preserved and cross-process truth is unaffected
        # (a fresh process re-ensures; nothing is cached across processes).
        db_path = self.db_path(project_root)
        if _SCHEMA_ENSURED.is_current(db_path):
            return
        ensured_ok = True  # cleared by any transient-lock failure below
        with self.connect(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_runs (
                    run_id TEXT PRIMARY KEY,
                    run_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    session_id TEXT,
                    procedure_id TEXT,
                    capability_name TEXT,
                    status TEXT NOT NULL,
                    ad_hoc INTEGER NOT NULL DEFAULT 1,
                    target_entity TEXT,
                    metadata_json TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS execution_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    event_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    session_id TEXT,
                    procedure_id TEXT,
                    capability_name TEXT,
                    action_kind TEXT,
                    target_entity TEXT,
                    status TEXT,
                    payload_json TEXT,
                    observed_at TEXT NOT NULL,
                    -- Audit hardening (2026-04-19):
                    --   task_id: stable id from task_begin (empty = no active task)
                    --   prev_hash: sha256 of the previous row's content-hash in
                    --              this session's chain. First row has ''.
                    --   chain_seq: monotonic per-session sequence number.
                    -- Together these form a Merkle-style audit chain that
                    -- detects retroactive tampering: edit/delete any row and
                    -- prev_hash on the NEXT row no longer matches.
                    task_id TEXT NOT NULL DEFAULT '',
                    prev_hash TEXT NOT NULL DEFAULT '',
                    chain_seq INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (run_id) REFERENCES execution_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS session_lane_agents (
                    worker_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    lane_id TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    state TEXT NOT NULL,
                    allowed_files TEXT,
                    pid INTEGER,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    metadata_json TEXT,
                    host_session_id TEXT NOT NULL DEFAULT '',
                    agent_context_id TEXT NOT NULL DEFAULT '',
                    aidocs_session_id TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_session_lane_agents_session
                    ON session_lane_agents(session_id);
                CREATE INDEX IF NOT EXISTS idx_session_lane_agents_state
                    ON session_lane_agents(state);
                """,
            )
            # Additive migration for pre-2026-04-19 databases. ADD COLUMN
            # IF NOT EXISTS isn't available on sqlite < 3.35, so we use
            # the standard try/except-existing pattern.
            for column_def in (
                "ALTER TABLE execution_events ADD COLUMN task_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE execution_events ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE execution_events ADD COLUMN chain_seq INTEGER NOT NULL DEFAULT 0",
                # RBAC audit attribution (2026-04-21). Every event row
                # knows who acted + in what role + at what scope.
                # Defaults preserve pre-migration rows as local-operator
                # acting as super_admin at global scope — honest for
                # solo-flavor installs, and re-attributable via later
                # migrations once login lands.
                "ALTER TABLE execution_events ADD COLUMN user_id TEXT NOT NULL DEFAULT 'operator'",
                "ALTER TABLE execution_events ADD COLUMN effective_role TEXT NOT NULL DEFAULT 'super_admin'",
                "ALTER TABLE execution_events ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'global'",
                "ALTER TABLE execution_events ADD COLUMN scope_id TEXT",
                "ALTER TABLE execution_events ADD COLUMN permission_name TEXT",
                # Principal kind: human operator / agent (MCP-driven) /
                # subagent (lane worker subprocess). Lets the dashboard
                # filter "what did the agent do vs what did the operator
                # do" even when they share user_id in Profile A.
                "ALTER TABLE execution_events ADD COLUMN principal_type TEXT NOT NULL DEFAULT 'human'",
                # Content-addressed payload/result hashes (2026-04-21).
                # in_hash: sha256 of the event's input payload (what
                #   was asked / observed).
                # out_hash: sha256 of the event's result payload when
                #   caller supplies one; empty string otherwise.
                # Included in _compute_row_hash so tampering with
                # either payload or stored result breaks the chain,
                # not just changing payload_json (which is already
                # covered). Defaults are empty strings — existing
                # rows get hashes only as new events land.
                "ALTER TABLE execution_events ADD COLUMN in_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE execution_events ADD COLUMN out_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE execution_events ADD COLUMN result_json TEXT",
                # Phoenix 2026-05-09: per-worker host_session_id, stamped
                # by the worker's host plugin/hook (opencode plugin
                # chat.message; claude_hook PreToolUse) on first session
                # event. Source of truth for §VIII deny-path resume — the
                # dispatcher fires `<host> --resume <id>` against this
                # value. Replaces the prior reliance on
                # query_gate.last_host_session_id (deprecated; that column
                # carried the conductor's stamp, not the worker's).
                "ALTER TABLE session_lane_agents ADD COLUMN host_session_id TEXT NOT NULL DEFAULT ''",
                # Canonical worker identities: derived once the host session is
                # stamped and persisted across every later lifecycle state.
                "ALTER TABLE session_lane_agents ADD COLUMN agent_context_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE session_lane_agents ADD COLUMN aidocs_session_id TEXT NOT NULL DEFAULT ''",
                # Audit attribution v2 (2026-06-28): version the row-hash formula
                # and bind the agent_memory_epoch into each event. Pre-v2 rows are
                # 'v1' (verified under the old formula); new rows are 'v2' and fold
                # user_id + agent_epoch into the Merkle hash + the event_id dedup
                # key, so rewriting WHO acted breaks the chain and two actors no
                # longer collapse into one row.
                "ALTER TABLE execution_events ADD COLUMN hash_version TEXT NOT NULL DEFAULT 'v1'",
                "ALTER TABLE execution_events ADD COLUMN agent_epoch TEXT NOT NULL DEFAULT ''",
                # Causal turn binding (#441, audit v4, 2026-07-18): the
                # server-minted operator-turn id active when the event was
                # recorded (session_query_gate.current_turn_id, rotated at
                # UserPromptSubmit for operator-authored prompts only).
                # Resolved server-side at insert time — record_event has NO
                # turn_id parameter, so a caller can never supply one.
                # Folded into the v4 row hash: rewriting WHICH instruction
                # caused a historical event breaks the Merkle chain.
                "ALTER TABLE execution_events ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''",
                # Causal instruction binding (#467, audit v5, 2026-07-18):
                # ONE bump folds every causal-turn field frozen by the spec
                # remainder — never incremental bumps (#467 hash law).
                #   instruction_id / instruction_revision: the turn's ACTIVE
                #     instruction (causal_turns.current_instruction_id /
                #     .instruction_revision) at insert time. Server-resolved —
                #     record_event has NO parameter for either, so a caller can
                #     never bind an event to a forged instruction.
                #   causal_edge: the derivation claim (spec CausalEdge
                #     taxonomy); values outside the frozen taxonomy are
                #     refused (stored as '').
                # The turn SEAL is linked via its own hash-bound `turn_sealed`
                # event row + the seal's event_merkle_root over these row
                # hashes (causal_turn_store) — a row cannot fold a seal that
                # does not exist yet.
                "ALTER TABLE execution_events ADD COLUMN instruction_id TEXT NOT NULL DEFAULT ''",
                (
                    "ALTER TABLE execution_events ADD COLUMN instruction_revision "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "ALTER TABLE execution_events ADD COLUMN causal_edge TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    with self.connect(project_root) as conn:
                        conn.execute(column_def)
                except Exception as exc:
                    # Only a benign already-exists is success. Lock/busy and any
                    # unknown failure leave the db unmarked so retry stays open.
                    if not _classify_migration_error(exc, "ALTER"):
                        ensured_ok = False
            # Index the task_id for cheap "show every event from task X".
            try:
                with self.connect(project_root) as conn:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_execution_events_task "
                        "ON execution_events(task_id)",
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_execution_events_session_seq "
                        "ON execution_events(session_id, chain_seq)",
                    )
                    # Recent-event feed across ALL sessions (query_last_execution,
                    # dashboard "All Sessions"): ORDER BY observed_at DESC,
                    # event_id DESC LIMIT N. event_id (unique TEXT PK) is the
                    # deterministic tiebreak, so this composite index serves the
                    # full order via a reverse index walk (no temp b-tree, no
                    # full SCAN) and grows O(limit) not O(table). EXPLAIN-proven
                    # 26ms -> 0.24ms; results identical above the tie boundary,
                    # ties now deterministic. Additive; no semantics change.
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_execution_events_observed_event "
                        "ON execution_events(observed_at, event_id)",
                    )
                    # SESSION-filtered recent-event feed (query_last_execution with
                    # session_id, dashboard "this session"): WHERE session_id=?
                    # ORDER BY observed_at DESC, event_id DESC. The (session_id,
                    # chain_seq) index serves the filter but NOT this order (chain_seq
                    # != observed_at) → a temp b-tree over the whole session's events
                    # (EXPLAIN-proven 26.9ms on a 2649-event session). This composite
                    # serves filter + order in one reverse walk → 1.1ms, identical
                    # results. (session_seq stays: it serves chain_seq-ordered reads.)
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_execution_events_session_observed "
                        "ON execution_events(session_id, observed_at, event_id)",
                    )
                    # Session-scoped recent runs (list_runs): execution_runs had
                    # NO session index, so WHERE session_id=? ORDER BY started_at
                    # DESC scanned every run. Serves the filter + most of the order.
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_execution_runs_session_started "
                        "ON execution_runs(session_id, started_at)",
                    )
            except Exception as exc:
                # CREATE INDEX IF NOT EXISTS won't raise already-exists; any
                # error here is lock/busy or unknown → leave unmarked, retry.
                if not _classify_migration_error(exc, "INDEX"):
                    ensured_ok = False
        # Only cache the "ensured" verdict when additive migrations reached a
        # known-good state. A transient lock OR any unknown failure leaves the db
        # UNmarked → the next init_db retries (idempotent), so a temporary lock
        # or anomaly can never permanently skip a column.
        if ensured_ok:
            _SCHEMA_ENSURED.mark(db_path)

    def record_run(
        self,
        project_root: Path,
        run_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        status: str = "started",
        ad_hoc: bool = True,
        target_entity: str | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        completed_at: str | None = None,
    ) -> str:
        self.init_db(project_root)
        started_at = self._timestamp()
        metadata_json = json.dumps(metadata or {}, sort_keys=True, default=str)
        # Content-addressed run_id keeps duplicate logical calls from
        # the wire-doubling bug on the same PRIMARY KEY — the ON
        # CONFLICT DO UPDATE below then harmlessly refreshes status /
        # completed_at on the existing row instead of creating a twin.
        run_id = run_id or self._compute_run_id(
            run_kind,
            capability_name,
            session_id,
            metadata_json,
            started_at,
        )
        with self.connect(project_root) as conn:
            conn.execute(
                """
                INSERT INTO execution_runs (
                    run_id, run_kind, source_kind, session_id, procedure_id, capability_name,
                    status, ad_hoc, target_entity, metadata_json, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    run_kind=excluded.run_kind,
                    source_kind=excluded.source_kind,
                    session_id=excluded.session_id,
                    procedure_id=excluded.procedure_id,
                    capability_name=excluded.capability_name,
                    status=excluded.status,
                    ad_hoc=excluded.ad_hoc,
                    target_entity=excluded.target_entity,
                    metadata_json=excluded.metadata_json,
                    completed_at=COALESCE(excluded.completed_at, execution_runs.completed_at)
                """,
                (
                    run_id,
                    run_kind,
                    source_kind,
                    session_id,
                    procedure_id,
                    capability_name,
                    status,
                    1 if ad_hoc else 0,
                    target_entity,
                    metadata_json,
                    started_at,
                    completed_at,
                ),
            )
        return run_id

    # Duplicate-write suppression via content-addressed primary key.
    # Some MCP clients (observed: VSCode Claude Code extension) dispatch
    # each tool-call twice on the JSON-RPC wire. The MCP-wrapper dedup
    # proved unreliable across task-context edge cases, so dedup lives
    # at the sqlite boundary instead: hash the content-defining fields
    # plus a coarse time bucket into a deterministic event_id /
    # run_id. Duplicate writes collide on PRIMARY KEY and
    # `ON CONFLICT DO NOTHING` drops them — zero probe queries, no lock
    # contention, terminal guarantee.
    #
    # Time bucket: two identical writes landing within the same bucket
    # collapse to one row; writes spaced further apart (legitimate
    # repeat invocations) land in different buckets and insert
    # independently. 2-second buckets tolerate the extension's observed
    # ~100ms duplicate gap without collapsing intentional repeats.
    _DEDUP_BUCKET_SECONDS = 2

    @staticmethod
    def _iso_to_epoch(ts: str) -> float:
        from datetime import datetime

        try:
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except Exception:
            return 0.0

    @classmethod
    def _compute_event_id(
        cls,
        event_kind: str,
        capability_name: str | None,
        session_id: str | None,
        payload_json: str,
        observed_at: str,
        user_id: str = "",
        agent_epoch: str = "",
    ) -> str:
        import hashlib

        bucket = int(cls._iso_to_epoch(observed_at) // cls._DEDUP_BUCKET_SECONDS)
        raw = (
            f"{event_kind}|{capability_name or ''}|{session_id or ''}"
            f"|{user_id or ''}|{agent_epoch or ''}|{payload_json}|{bucket}"
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"event-{digest}"

    @classmethod
    def _compute_event_id_prior_bucket(
        cls,
        event_kind: str,
        capability_name: str | None,
        session_id: str | None,
        payload_json: str,
        observed_at: str,
        user_id: str = "",
        agent_epoch: str = "",
    ) -> str:
        import hashlib

        bucket = int(cls._iso_to_epoch(observed_at) // cls._DEDUP_BUCKET_SECONDS) - 1
        raw = (
            f"{event_kind}|{capability_name or ''}|{session_id or ''}"
            f"|{user_id or ''}|{agent_epoch or ''}|{payload_json}|{bucket}"
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"event-{digest}"

    @classmethod
    def _compute_run_id(
        cls,
        run_kind: str,
        capability_name: str | None,
        session_id: str | None,
        metadata_json: str,
        started_at: str,
    ) -> str:
        import hashlib

        bucket = int(cls._iso_to_epoch(started_at) // cls._DEDUP_BUCKET_SECONDS)
        raw = f"{run_kind}|{capability_name or ''}|{session_id or ''}|{metadata_json}|{bucket}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"run-{digest}"

    def record_event(
        self,
        project_root: Path,
        event_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        action_kind: str | None = None,
        target_entity: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        observed_at: str | None = None,
        *,
        # RBAC attribution (2026-04-21). Kw-only so callers opt in
        # explicitly; missing values resolve via IdentityResolver at
        # insert time so legacy callers keep working.
        user_id: str | None = None,
        effective_role: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        permission_name: str | None = None,
        principal_type: str | None = None,
        # Content-addressed payload/result hashes (2026-04-21).
        # Pass `result` to record the tool's output alongside its
        # input; both halves are hashed and stored in in_hash /
        # out_hash. Existing callers that only pass `payload` get
        # in_hash computed from payload and out_hash = ''.
        result: dict[str, Any] | None = None,
        # #467 causal edge (spec CausalEdge taxonomy): the caller's
        # derivation CLAIM for this event. Validated against the frozen
        # taxonomy — anything else stores as ''. instruction_id /
        # instruction_revision are deliberately NOT parameters (server-
        # resolved from the active turn, like turn_id).
        causal_edge: str = "",
    ) -> str:
        """Open a connection, write the event, commit. Delegates the row
        construction to ``record_event_on_connection`` so callers that
        need the audit row to land in their OWN transaction (e.g.
        ConfigStore atomic config-write + audit) can share a connection.
        """
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            written_event_id = self.record_event_on_connection(
                conn,
                project_root,
                event_kind,
                source_kind,
                session_id=session_id,
                procedure_id=procedure_id,
                capability_name=capability_name,
                action_kind=action_kind,
                target_entity=target_entity,
                status=status,
                payload=payload,
                run_id=run_id,
                event_id=event_id,
                observed_at=observed_at,
                user_id=user_id,
                effective_role=effective_role,
                scope_type=scope_type,
                scope_id=scope_id,
                permission_name=permission_name,
                principal_type=principal_type,
                result=result,
                causal_edge=causal_edge,
            )
        # Empire mirror (backlog #140): AFTER the kingdom commit (exit of the
        # `with` block above), best-effort copy the committed row into the
        # empire ledger. The kingdom stays the fail-closed authority — a
        # mirror failure never raises past _mirror_event_to_empire. Rows
        # written by atomic callers via record_event_on_connection directly
        # are NOT mirrored here: their commit belongs to the caller, and
        # mirroring inside an open transaction could archive a row that later
        # rolls back (see empire_audit_store module docstring).
        self._mirror_event_to_empire(project_root, written_event_id)
        return written_event_id

    def _mirror_event_to_empire(self, project_root: Path, event_id: str) -> None:
        """Best-effort empire mirror of one COMMITTED event row."""
        try:
            from .empire_audit_store import EmpireAuditStore, mirror_enabled

            if not mirror_enabled():
                return
            with self.connect(project_root) as conn:
                row = conn.execute(
                    "SELECT * FROM execution_events WHERE event_id = ? LIMIT 1",
                    (event_id,),
                ).fetchone()
            if row is None:
                return
            EmpireAuditStore().record_event_mirror(project_root, dict(row))
        except Exception:
            # The archive path must never disturb the kingdom audit.
            pass

    def _ensure_events_schema_on_connection(self, conn) -> None:
        """Guarantee execution_runs + execution_events exist on the GIVEN
        connection (2026-07-09 flake fix). record_event_on_connection writes on a
        CALLER-supplied connection whose db may differ from the one init_db seeded
        (ConfigStore inits the project db but writes via _connect_for_scope; a
        recycled tmp path yields a fresh empty db) -> the test-caught 'no such
        table: execution_events'. Idempotent CREATE IF NOT EXISTS with the FULL
        current column set, so a fresh table needs no ALTERs. Never raises."""
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_runs (
                    run_id TEXT PRIMARY KEY,
                    run_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    session_id TEXT,
                    procedure_id TEXT,
                    capability_name TEXT,
                    status TEXT NOT NULL,
                    ad_hoc INTEGER NOT NULL DEFAULT 1,
                    target_entity TEXT,
                    metadata_json TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS execution_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    event_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    session_id TEXT,
                    procedure_id TEXT,
                    capability_name TEXT,
                    action_kind TEXT,
                    target_entity TEXT,
                    status TEXT,
                    payload_json TEXT,
                    observed_at TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    prev_hash TEXT NOT NULL DEFAULT '',
                    chain_seq INTEGER NOT NULL DEFAULT 0,
                    user_id TEXT NOT NULL DEFAULT 'operator',
                    effective_role TEXT NOT NULL DEFAULT 'super_admin',
                    scope_type TEXT NOT NULL DEFAULT 'global',
                    scope_id TEXT,
                    permission_name TEXT,
                    principal_type TEXT NOT NULL DEFAULT 'human',
                    in_hash TEXT NOT NULL DEFAULT '',
                    out_hash TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
                    hash_version TEXT NOT NULL DEFAULT 'v1',
                    agent_epoch TEXT NOT NULL DEFAULT '',
                    turn_id TEXT NOT NULL DEFAULT '',
                    instruction_id TEXT NOT NULL DEFAULT '',
                    instruction_revision INTEGER NOT NULL DEFAULT 0,
                    causal_edge TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (run_id) REFERENCES execution_runs(run_id)
                );
                """
            )
            # #441/#467: a db seeded by an OLDER _ensure_events_schema (table
            # already existed without the causal columns) still needs the
            # additive columns on THIS connection — same self-heal contract
            # as the CREATE above. Benign 'duplicate column' swallowed.
            for column_def in (
                "ALTER TABLE execution_events ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''",
                (
                    "ALTER TABLE execution_events ADD COLUMN instruction_id "
                    "TEXT NOT NULL DEFAULT ''"
                ),
                (
                    "ALTER TABLE execution_events ADD COLUMN instruction_revision "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                (
                    "ALTER TABLE execution_events ADD COLUMN causal_edge "
                    "TEXT NOT NULL DEFAULT ''"
                ),
            ):
                try:
                    conn.execute(column_def)
                except Exception:
                    pass
        except Exception:
            pass

    def record_event_on_connection(
        self,
        conn,
        project_root: Path,
        event_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        action_kind: str | None = None,
        target_entity: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        observed_at: str | None = None,
        *,
        user_id: str | None = None,
        effective_role: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        permission_name: str | None = None,
        principal_type: str | None = None,
        result: dict[str, Any] | None = None,
        causal_edge: str = "",
    ) -> str:
        """Write one execution_events row using the CALLER's connection
        (no commit — the caller owns the transaction). Same dedup,
        prev_hash/chain_seq chaining, task_id, attribution, and
        in_hash/out_hash behavior as record_event. The caller MUST have
        ensured the schema exists (init_db) and the connection MUST have
        ``row_factory = sqlite3.Row``. Raises on DB error so an atomic
        caller can roll back its mutation when the audit row fails.
        """
        # The "caller ensured schema" contract above was violated by shared-
        # connection callers on a recreated/other db -> "no such table". Self-heal
        # on THIS connection so the audit row can never be lost to a missing table.
        self._ensure_events_schema_on_connection(conn)
        payload_json = json.dumps(payload or {}, sort_keys=True, default=str)
        result_json = json.dumps(result, sort_keys=True, default=str) if result is not None else ""
        import hashlib as _hashlib_inout

        in_hash = _hashlib_inout.sha256(payload_json.encode("utf-8")).hexdigest()
        out_hash = (
            _hashlib_inout.sha256(result_json.encode("utf-8")).hexdigest() if result_json else ""
        )
        ts = observed_at or self._timestamp()
        # Resolve the acting identity + agent epoch BEFORE the event_id so
        # attribution is folded into BOTH the dedup key (A) and the v2 row
        # hash (B). user_id falls back to the IdentityResolver, then 'operator'.
        resolved_user_id = user_id
        if resolved_user_id is None:
            try:
                from .identity_resolver import current_user

                resolved_user_id = current_user(project_root)[0]
            except Exception:
                resolved_user_id = None
        if resolved_user_id is None:
            resolved_user_id = "operator"
        agent_epoch = self._resolve_audit_epoch(project_root)
        # Content-addressed event_id + bucket-boundary dedup (see
        # record_event docstring history). Uses the caller's connection so
        # the dedup read is consistent with the in-flight transaction.
        if event_id is None:
            event_id = self._compute_event_id(
                event_kind,
                capability_name,
                session_id,
                payload_json,
                ts,
                resolved_user_id,
                agent_epoch,
            )
            prior_id = self._compute_event_id_prior_bucket(
                event_kind,
                capability_name,
                session_id,
                payload_json,
                ts,
                resolved_user_id,
                agent_epoch,
            )
            if prior_id != event_id:
                try:
                    hit = conn.execute(
                        "SELECT 1 FROM execution_events WHERE event_id = ? LIMIT 1",
                        (prior_id,),
                    ).fetchone()
                    if hit is not None:
                        return prior_id
                except Exception:
                    pass

        # Merkle chain + task_id resolution on the caller's connection.
        resolved_task_id = ""
        resolved_turn_id = ""
        resolved_instruction_id = ""
        resolved_instruction_revision = 0
        prev_hash = ""
        next_seq = 0
        # #467: causal_edge is a CLAIM validated against the frozen spec
        # taxonomy — a malicious/typo'd edge never enters the hash-bound row.
        try:
            from .causal_turn_contract import CAUSAL_EDGE_VALUES

            resolved_causal_edge = (
                causal_edge if causal_edge in CAUSAL_EDGE_VALUES else ""
            )
        except Exception:
            resolved_causal_edge = ""
        if session_id:
            # #441 causal turn attribution: read the session's current
            # SERVER-minted turn id (session_query_gate.current_turn_id,
            # rotated at UserPromptSubmit for operator-authored prompts
            # only). Resolved here — never caller-supplied — so events
            # can't be bound to a forged turn. Separate SELECT from the
            # task read below so a legacy session_query_gate without the
            # column degrades to '' without disturbing task attribution.
            try:
                row = conn.execute(
                    "SELECT current_turn_id FROM session_query_gate "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is not None:
                    resolved_turn_id = str(row["current_turn_id"] or "")
            except Exception:
                resolved_turn_id = ""
            # #467: resolve the turn's ACTIVE instruction (id + revision)
            # from causal_turns — server-side only, never caller-supplied.
            # A turn id with no causal_turns row (legacy mint, foreign/
            # forged id) degrades to ''/0: no cross-project correlation
            # can be smuggled in through a fabricated instruction binding.
            if resolved_turn_id:
                try:
                    row = conn.execute(
                        "SELECT current_instruction_id, instruction_revision "
                        "FROM causal_turns WHERE turn_id = ?",
                        (resolved_turn_id,),
                    ).fetchone()
                    if row is not None:
                        resolved_instruction_id = str(
                            row["current_instruction_id"] or ""
                        )
                        resolved_instruction_revision = int(
                            row["instruction_revision"] or 0
                        )
                except Exception:
                    resolved_instruction_id = ""
                    resolved_instruction_revision = 0
            # #463: task attribution reads the SAME (actor, lane, worker)
            # triple the task stores key on — one shared seam, so an
            # event minted mid-task always lands on the caller's own
            # task_id, whichever slot family owns it. The explicit
            # `principal_type="subagent"` parameter keeps forcing the
            # worker branch for callers that stamp it directly.
            try:
                from .task_actor_identity import resolve_task_actor

                worker_actor_id, worker_lane_id, is_worker_task = resolve_task_actor(
                    project_root,
                )
            except Exception:
                worker_actor_id, worker_lane_id, is_worker_task = "", "", False
            if principal_type == "subagent" and not is_worker_task:
                is_worker_task = True
                try:
                    from .mcp_server_runtime_helpers import (
                        current_calling_agent_context_id,
                    )

                    worker_actor_id = current_calling_agent_context_id(
                        project_root,
                    ).strip()
                except Exception:
                    worker_actor_id = ""
            if is_worker_task:
                if worker_actor_id:
                    try:
                        row = conn.execute(
                            "SELECT task_id FROM actor_task_state "
                            "WHERE session_id = ? AND agent_context_id = ? AND lane_id = ? "
                            "AND status = 'active'",
                            (session_id, worker_actor_id, worker_lane_id),
                        ).fetchone()
                        if row is not None:
                            resolved_task_id = str(row["task_id"] or "")
                    except Exception:
                        resolved_task_id = ""
            else:
                try:
                    row = conn.execute(
                        "SELECT current_task_id FROM session_query_gate WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    if row is not None:
                        resolved_task_id = str(row["current_task_id"] or "")
                except Exception:
                    resolved_task_id = ""
            try:
                row = conn.execute(
                    "SELECT event_id, prev_hash, chain_seq, event_kind, "
                    "observed_at, payload_json, in_hash, out_hash, "
                    "hash_version, user_id, agent_epoch, "
                    # #440 v3 / #441 v4: the newly hash-bound columns must
                    # reach the recompute so the prior row's STORED
                    # hash_version picks its exact formula (v1/v2/v3/v4
                    # boundary handling).
                    "session_id, capability_name, action_kind, "
                    "target_entity, status, effective_role, scope_type, "
                    "scope_id, permission_name, principal_type, turn_id, "
                    # #467 v5: the causal-instruction columns reach the
                    # recompute so a stored v5 prior row hashes correctly.
                    "instruction_id, instruction_revision, causal_edge "
                    "FROM execution_events "
                    "WHERE session_id = ? ORDER BY chain_seq DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if row is not None:
                    prev_hash = self._row_hash_from_stored_row(row)
                    next_seq = int(row["chain_seq"]) + 1
            except Exception:
                pass

        # RBAC attribution resolution (defaults so the insert never fails).
        # resolved_user_id + agent_epoch were resolved ABOVE (folded into the
        # event_id dedup key + the v2 row hash); only role/scope remain here.
        resolved_role = effective_role
        resolved_scope_type = scope_type
        resolved_scope_id = scope_id
        resolved_permission = permission_name
        resolved_principal = principal_type
        if resolved_user_id is None or resolved_role is None or resolved_principal is None:
            try:
                from .identity_resolver import (
                    current_effective_role,
                    current_user,
                )

                uid, _email, ptype = current_user(project_root)
                if resolved_user_id is None:
                    resolved_user_id = uid
                if resolved_principal is None:
                    resolved_principal = ptype
                if resolved_role is None:
                    resolved_role = current_effective_role(
                        project_root,
                        resolved_user_id,
                    )
            except Exception:
                pass
        if resolved_user_id is None:
            resolved_user_id = "operator"
        if resolved_role is None:
            resolved_role = "super_admin"
        if resolved_scope_type is None:
            resolved_scope_type = "global"
        if resolved_principal is None:
            resolved_principal = "human"

        conn.execute(
            """
            INSERT INTO execution_events (
                event_id, run_id, event_kind, source_kind, session_id, procedure_id,
                capability_name, action_kind, target_entity, status, payload_json, observed_at,
                task_id, prev_hash, chain_seq,
                user_id, effective_role, scope_type, scope_id, permission_name, principal_type,
                in_hash, out_hash, result_json, hash_version, agent_epoch, turn_id,
                instruction_id, instruction_revision, causal_edge
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                event_id,
                run_id,
                event_kind,
                source_kind,
                session_id,
                procedure_id,
                capability_name,
                action_kind,
                target_entity,
                status,
                payload_json,
                ts,
                resolved_task_id,
                prev_hash,
                next_seq,
                resolved_user_id,
                resolved_role,
                resolved_scope_type,
                resolved_scope_id,
                resolved_permission,
                resolved_principal,
                in_hash,
                out_hash,
                result_json or None,
                # #467: new rows are v5 — the v4 set PLUS the causal
                # instruction binding (instruction_id, instruction_revision,
                # causal_edge), folded in ONE bump per the #467 hash law
                # (see _compute_row_hash's v5 branch). Historical
                # v1/v2/v3/v4 rows keep verifying under their stored formula.
                "v5",
                agent_epoch,
                resolved_turn_id,
                resolved_instruction_id,
                resolved_instruction_revision,
                resolved_causal_edge,
            ),
        )
        return event_id

    @staticmethod
    def _compute_row_hash(
        *,
        event_id: str,
        event_kind: str,
        observed_at: str,
        payload_json: str,
        prev_hash: str,
        in_hash: str = "",
        out_hash: str = "",
        hash_version: str = "v1",
        user_id: str = "",
        agent_epoch: str = "",
        session_id: str = "",
        capability_name: str = "",
        action_kind: str = "",
        target_entity: str = "",
        status: str = "",
        effective_role: str = "",
        scope_type: str = "",
        scope_id: str = "",
        permission_name: str = "",
        principal_type: str = "",
        turn_id: str = "",
        instruction_id: str = "",
        instruction_revision: str = "0",
        causal_edge: str = "",
    ) -> str:
        """Content hash of one event row, chained to prev_hash.

        Used when computing THIS row's prev_hash (by hashing the
        previous row) and when verifying the chain at audit time.
        Fields: anything stable + observable. Excludes chain_seq so
        we're hashing "what the event was" not "where it sits".
        in_hash + out_hash are folded in so tampering with a stored
        payload or result breaks the chain as well — an attacker
        would have to rewrite every downstream prev_hash to hide it.

        The v3 kwargs (session_id .. principal_type) are IGNORED by the
        v1/v2 branches, so callers may always pass the full row and the
        stored per-row hash_version picks the right formula — exactly
        how the v1→v2 boundary already verifies.
        """
        import hashlib

        if str(hash_version) == "v5":
            # v5 (#467): everything v4 binds PLUS the causal instruction
            # binding — instruction_id, instruction_revision and
            # causal_edge, folded in ONE version bump (the #467 hash law:
            # all new hash-bearing fields together, never incremental
            # bumps). A DB-writer can no longer re-attribute a historical
            # event to a different instruction revision or rewrite its
            # derivation claim without breaking the Merkle chain. The
            # leading "v5|" domain-separates the formulae, same as every
            # earlier boundary. (The turn SEAL is linked via the seal's
            # event_merkle_root over these row hashes + its own hash-bound
            # `turn_sealed` event row — a row cannot fold a future seal.)
            raw = (
                f"v5|{event_id}|{event_kind}|{observed_at}|{payload_json}"
                f"|{prev_hash}|{in_hash}|{out_hash}|{user_id or ''}|{agent_epoch or ''}"
                f"|{session_id or ''}|{capability_name or ''}|{action_kind or ''}"
                f"|{target_entity or ''}|{status or ''}|{effective_role or ''}"
                f"|{scope_type or ''}|{scope_id or ''}|{permission_name or ''}"
                f"|{principal_type or ''}|{turn_id or ''}|{instruction_id or ''}"
                f"|{instruction_revision or '0'}|{causal_edge or ''}"
            )
        elif str(hash_version) == "v4":
            # v4 (#441): everything v3 binds PLUS the causal turn_id — the
            # server-minted operator-turn id active when the event landed.
            # Binding it means a DB-writer cannot re-attribute a historical
            # tool call to a different operator instruction without
            # breaking the Merkle chain. The leading "v4|" domain-
            # separates the formulae, same as the earlier boundaries.
            raw = (
                f"v4|{event_id}|{event_kind}|{observed_at}|{payload_json}"
                f"|{prev_hash}|{in_hash}|{out_hash}|{user_id or ''}|{agent_epoch or ''}"
                f"|{session_id or ''}|{capability_name or ''}|{action_kind or ''}"
                f"|{target_entity or ''}|{status or ''}|{effective_role or ''}"
                f"|{scope_type or ''}|{scope_id or ''}|{permission_name or ''}"
                f"|{principal_type or ''}|{turn_id or ''}"
            )
        elif str(hash_version) == "v3":
            # v3 (#440): fold EVERY auditable column — the v2 fields PLUS
            # the RBAC/scope attribution (effective_role, scope_type,
            # scope_id, permission_name, principal_type) and the WHO-scope/
            # WHAT-command columns (session_id, capability_name,
            # action_kind, target_entity, status). Before v3 those were
            # STORED but NOT hash-bound, so a DB-writer could rewrite a
            # historical row's authority context ('observer'→'super_admin')
            # without breaking the Merkle chain. The leading "v3|"
            # domain-separates the formulae.
            raw = (
                f"v3|{event_id}|{event_kind}|{observed_at}|{payload_json}"
                f"|{prev_hash}|{in_hash}|{out_hash}|{user_id or ''}|{agent_epoch or ''}"
                f"|{session_id or ''}|{capability_name or ''}|{action_kind or ''}"
                f"|{target_entity or ''}|{status or ''}|{effective_role or ''}"
                f"|{scope_type or ''}|{scope_id or ''}|{permission_name or ''}"
                f"|{principal_type or ''}"
            )
        elif str(hash_version) == "v2":
            # v2 folds the acting identity (user_id) + agent_epoch into the
            # chained hash, so rewriting WHO acted breaks the chain too. The
            # leading "v2|" domain-separates the formulae.
            raw = (
                f"v2|{event_id}|{event_kind}|{observed_at}|{payload_json}"
                f"|{prev_hash}|{in_hash}|{out_hash}|{user_id or ''}|{agent_epoch or ''}"
            )
        else:
            # v1 (legacy) -- byte-identical to the original formula so existing
            # chains keep verifying across the version boundary.
            raw = (
                f"{event_id}|{event_kind}|{observed_at}|{payload_json}|{prev_hash}|{in_hash}|{out_hash}"
            )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def _row_hash_from_stored_row(cls, row) -> str:
        """_compute_row_hash over a SELECTed execution_events row, honoring
        the row's OWN stored hash_version (v1/v2/v3) — the single shared
        seam for prev_hash chaining and audit-time verification, so the
        v1→v2→v3 boundaries behave identically at both call sites. Columns
        a legacy row/SELECT lacks default to '' (ignored by older
        formulae)."""

        def _col(name: str, default: str = "") -> str:
            try:
                return str((row[name] if name in row.keys() else default) or default)
            except Exception:
                return default

        return cls._compute_row_hash(
            event_id=row["event_id"],
            event_kind=row["event_kind"],
            observed_at=row["observed_at"],
            payload_json=row["payload_json"],
            prev_hash=row["prev_hash"],
            in_hash=_col("in_hash"),
            out_hash=_col("out_hash"),
            hash_version=_col("hash_version", "v1"),
            user_id=_col("user_id"),
            agent_epoch=_col("agent_epoch"),
            session_id=_col("session_id"),
            capability_name=_col("capability_name"),
            action_kind=_col("action_kind"),
            target_entity=_col("target_entity"),
            status=_col("status"),
            effective_role=_col("effective_role"),
            scope_type=_col("scope_type"),
            scope_id=_col("scope_id"),
            permission_name=_col("permission_name"),
            principal_type=_col("principal_type"),
            turn_id=_col("turn_id"),
            instruction_id=_col("instruction_id"),
            # INTEGER column: 0 and NULL both normalize to "0" so the v5
            # fold is deterministic across sqlite type affinities.
            instruction_revision=_col("instruction_revision", "0"),
            causal_edge=_col("causal_edge"),
        )

    @staticmethod
    def _resolve_audit_epoch(project_root: Path) -> str:
        """Best-effort agent_memory_epoch for the CURRENT caller (project +
        host_kind + host_session_id). Empty string when no host session
        resolves -- attribution then records agent_epoch='' (graceful). The
        value is STORED on the row so the verifier recomputes from the column
        and never re-derives it.
        """
        try:
            from .mcp_server_runtime_helpers import current_calling_host_session_id

            sid = (current_calling_host_session_id() or "").strip()
            if not sid:
                return ""
            kind = ""
            try:
                from .read_memory_surfacer import _detect_host_kind

                kind = _detect_host_kind() or ""
            except Exception:
                kind = ""
            from .agent_memory_epoch import current_epoch

            return current_epoch(project_root, host_kind=kind, host_session_id=sid) or ""
        except Exception:
            return ""

    def verify_audit_chain(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, Any]:
        """Walk the chain for a session. Reports first broken link (if
        any) and totals. Tamper evidence: edit or delete any row and
        the next row's prev_hash won't match the recomputed hash.

        O(n) in the number of session events. Callers use this from
        the dashboard / audit reports; not hot-path.
        """
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT event_id, event_kind, observed_at, payload_json, "
                "prev_hash, chain_seq, in_hash, out_hash, "
                "hash_version, user_id, agent_epoch, "
                # #440 v3 / #441 v4 / #467 v5: verification recomputes from
                # the FULL stored column set; each row's hash_version picks
                # its formula.
                "session_id, capability_name, action_kind, "
                "target_entity, status, effective_role, scope_type, "
                "scope_id, permission_name, principal_type, turn_id, "
                "instruction_id, instruction_revision, causal_edge "
                "FROM execution_events WHERE session_id = ? "
                "ORDER BY chain_seq ASC",
                (session_id,),
            ).fetchall()
        total = len(rows)
        if total == 0:
            return {"verified": True, "total": 0, "broken_at": None}
        expected_prev = ""
        for idx, row in enumerate(rows):
            if str(row["prev_hash"] or "") != expected_prev:
                return {
                    "verified": False,
                    "total": total,
                    "broken_at": idx,
                    "broken_event_id": row["event_id"],
                    "broken_chain_seq": int(row["chain_seq"]),
                    "expected_prev_hash": expected_prev,
                    "stored_prev_hash": str(row["prev_hash"] or ""),
                }
            expected_prev = self._row_hash_from_stored_row(row)
        return {"verified": True, "total": total, "broken_at": None}

    # Tier-aware denial event names produced by AgentOrchestrator and
    # claude_hook. Anything ending in _block, _denied, or matching the
    # tier0_* prefix counts as a refusal we want to track. Keep in
    # lockstep with DENIAL_TIERS in agent_orchestrator.py.
    _DENIAL_EVENT_KINDS = (
        "edit_redirect_block",
        "raw_shell_block",
        "bash_policy_block",
        "raw_tool_block",
        "lane_tool_block",
        "test_retry_block",
        "tool_policy_block",
        "agent_brief_block",
        "managed_mode_block",
        "infrastructure_block",
        "foreground_long_running_block",
        "heuristic_judge_block",
    )

    def denial_tier_stats(
        self,
        project_root: Path,
        session_id: str | None = None,
        limit_per_tier: int = 1000,
    ) -> dict[str, Any]:
        """Aggregate gate-denial counts per tier with recent samples.

        Bonus telemetry 2026-04-19. Gap filled: operators have no
        empirical view of which tier fires most. Heuristic judge could
        be over-firing, bash_policy might never trigger because tier-0
        catches first — without this we're guessing.

        Returns per-tier counts + last-N event timestamps for trend
        sketching. The dashboard layer can chart the timestamps; the
        MCP layer just exposes the raw shape.
        """
        self.init_db(project_root)
        out: dict[str, Any] = {"by_tier": {}, "total_denials": 0}
        with self.connect(project_root) as conn:
            for kind in self._DENIAL_EVENT_KINDS:
                if session_id:
                    rows = conn.execute(
                        "SELECT observed_at FROM execution_events "
                        "WHERE event_kind = ? AND session_id = ? "
                        "ORDER BY observed_at DESC LIMIT ?",
                        (kind, session_id.strip(), int(limit_per_tier)),
                    ).fetchall()
                    count_row = conn.execute(
                        "SELECT COUNT(*) FROM execution_events "
                        "WHERE event_kind = ? AND session_id = ?",
                        (kind, session_id.strip()),
                    ).fetchone()
                else:
                    rows = conn.execute(
                        "SELECT observed_at FROM execution_events "
                        "WHERE event_kind = ? "
                        "ORDER BY observed_at DESC LIMIT ?",
                        (kind, int(limit_per_tier)),
                    ).fetchall()
                    count_row = conn.execute(
                        "SELECT COUNT(*) FROM execution_events WHERE event_kind = ?",
                        (kind,),
                    ).fetchone()
                count = int(count_row[0]) if count_row else 0
                if count == 0:
                    continue
                # Strip the _block suffix so dashboard labels read as
                # tier names ("bash_policy") not event_kind strings
                # ("bash_policy_block"). Audit consumers still get the
                # raw event_kinds via execution_status.
                tier_name = kind.removesuffix("_block")
                out["by_tier"][tier_name] = {
                    "count": count,
                    "recent_timestamps": [str(r[0]) for r in rows[:20]],
                }
                out["total_denials"] += count
        return out

    def execution_status(self, project_root: Path) -> dict[str, Any]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            run_count = conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0]
            run_kind_rows = conn.execute(
                "SELECT run_kind, COUNT(*) AS count FROM execution_runs GROUP BY run_kind ORDER BY count DESC, run_kind ASC",
            ).fetchall()
            event_kind_rows = conn.execute(
                "SELECT event_kind, COUNT(*) AS count FROM execution_events GROUP BY event_kind ORDER BY count DESC, event_kind ASC",
            ).fetchall()
            source_rows = conn.execute(
                "SELECT source_kind, COUNT(*) AS count FROM execution_events GROUP BY source_kind ORDER BY count DESC, source_kind ASC",
            ).fetchall()
        return {
            "db_path": str(self.db_path(project_root)),
            "execution_runs": int(run_count),
            "execution_events": int(event_count),
            "run_kinds": {row["run_kind"]: int(row["count"]) for row in run_kind_rows},
            "event_kinds": {row["event_kind"]: int(row["count"]) for row in event_kind_rows},
            "by_source": {row["source_kind"]: int(row["count"]) for row in source_rows},
        }

    def list_runs(
        self,
        project_root: Path,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = "SELECT run_id, run_kind, source_kind, session_id, procedure_id, capability_name, status, ad_hoc, target_entity, metadata_json, started_at, completed_at FROM execution_runs"
        params: list[Any] = []
        if session_id and session_id.strip():
            sql += " WHERE session_id = ?"
            params.append(session_id.strip())
        sql += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._run_row_to_dict(row) for row in rows]

    def list_events(
        self,
        project_root: Path,
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = (
            "SELECT event_id, run_id, event_kind, source_kind, session_id, procedure_id, capability_name, action_kind, target_entity, status, payload_json, observed_at "
            "FROM execution_events"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if session_id and session_id.strip():
            clauses.append("session_id = ?")
            params.append(session_id.strip())
        if query and query.strip():
            needle = f"%{query.strip()}%"
            clauses.append(
                "(event_kind LIKE ? OR COALESCE(capability_name, '') LIKE ? OR COALESCE(action_kind, '') LIKE ? OR COALESCE(payload_json, '') LIKE ?)",
            )
            params.extend([needle, needle, needle, needle])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observed_at DESC, event_id DESC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._event_row_to_dict(row) for row in rows]

    def session_lifecycle_activity_summary(
        self,
        project_root: Path,
        session_id: str,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT rowid, event_id, run_id, event_kind, source_kind, session_id, procedure_id, capability_name, action_kind, target_entity, status, payload_json, observed_at FROM execution_events WHERE session_id = ? ORDER BY observed_at DESC, rowid DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        events = [self._event_row_to_dict(row) for row in rows]
        recent_activity: list[dict[str, Any]] = []
        last_lifecycle_tool: str | None = None
        last_lifecycle_observed_at: str | None = None

        for event in events:
            capability_name = str(event.get("capability_name") or "").strip()
            if (
                event.get("event_kind") == "tool_call_completed"
                and capability_name in self._LIFECYCLE_TOOLS
            ):
                last_lifecycle_tool = capability_name
                last_lifecycle_observed_at = str(event.get("observed_at") or "").strip() or None
                break
            recent_activity.append(event)

        edit_like_count = sum(1 for event in recent_activity if self._is_edit_like_event(event))
        meaningful_work_count = sum(
            1 for event in recent_activity if self._is_meaningful_work_event(event)
        )

        needs_task_complete = edit_like_count >= 1
        needs_task_update = not needs_task_complete and meaningful_work_count >= 3

        return {
            "session_id": session_id,
            "last_lifecycle_tool": last_lifecycle_tool,
            "last_lifecycle_observed_at": last_lifecycle_observed_at,
            "edit_like_count": edit_like_count,
            "meaningful_work_count": meaningful_work_count,
            "needs_task_update": needs_task_update,
            "needs_task_complete": needs_task_complete,
            "recent_activity_count": len(recent_activity),
        }

    def session_journal_coverage_summary(
        self,
        project_root: Path,
        session_id: str,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Legacy compliance hook — obsolete post journal-deslop (2026-04-20).

        `logging_debt` compared "has a journal entry landed recently vs.
        meaningful event stream?" to nudge agents to call
        `session_journal_log`. With that tool deleted and the audit trail
        now living exclusively in execution_events (Merkle-chained,
        task_id-stamped, written automatically), there IS no journal to
        fall behind of. Always returns logging_debt=False + 0 count so
        downstream compliance panels show clean instead of perpetually
        flagging. Callers kept for back-compat with the dashboard shape;
        the whole path can be deleted in a follow-up pass.
        """
        return {
            "meaningful_event_count_since_journal": 0,
            "latest_meaningful_event_at": None,
            "logging_debt": False,
        }

    def _is_edit_like_event(self, event: dict[str, Any]) -> bool:
        capability_name = str(event.get("capability_name") or "").strip().lower()
        return capability_name in self._EDIT_LIKE_TOOLS

    def _is_meaningful_work_event(self, event: dict[str, Any]) -> bool:
        capability_name = str(event.get("capability_name") or "").strip().lower()
        event_kind = str(event.get("event_kind") or "").strip().lower()
        action_kind = str(event.get("action_kind") or "").strip().lower()
        if event_kind == "native_tool_use":
            return capability_name in self._EDIT_LIKE_TOOLS or capability_name not in {
                "read",
                "glob",
                "grep",
            }
        if event_kind == "tool_call_completed":
            if capability_name in self._LIFECYCLE_TOOLS:
                return False
            if not capability_name and action_kind in {
                "edit",
                "write_memory",
                "trace",
                "understand",
                "inspect",
                "investigate",
            }:
                return True
            return capability_name.startswith(self._MEANINGFUL_MCP_PREFIXES)
        return False

    def _run_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "run_kind": row["run_kind"],
            "source_kind": row["source_kind"],
            "session_id": row["session_id"],
            "procedure_id": row["procedure_id"],
            "capability_name": row["capability_name"],
            "status": row["status"],
            "ad_hoc": bool(row["ad_hoc"]),
            "target_entity": row["target_entity"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    def _event_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "event_kind": row["event_kind"],
            "source_kind": row["source_kind"],
            "session_id": row["session_id"],
            "procedure_id": row["procedure_id"],
            "capability_name": row["capability_name"],
            "action_kind": row["action_kind"],
            "target_entity": row["target_entity"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
            "observed_at": row["observed_at"],
        }

    def query_last_execution(
        self,
        project_root: Path,
        action_kind: str | None = None,
        capability_name: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Query: 'What actually ran last time?' — returns recent execution events matching filters."""
        self.init_db(project_root)
        clauses: list[str] = []
        params: list[Any] = []
        if action_kind and action_kind.strip():
            clauses.append("action_kind = ?")
            params.append(action_kind.strip())
        if capability_name and capability_name.strip():
            clauses.append("capability_name = ?")
            params.append(capability_name.strip())
        if session_id and session_id.strip():
            clauses.append("session_id = ?")
            params.append(session_id.strip())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # Deterministic total order: observed_at is second-resolution so ties are
        # frequent; event_id (TEXT PRIMARY KEY, unique) is the stable tiebreak so
        # the recent feed is reproducible AND index-orderable. Matches the
        # composite index idx_execution_events_observed_event so the no-filter
        # feed walks the index (no temp b-tree) instead of scanning the table.
        sql = (
            "SELECT event_id, run_id, event_kind, source_kind, session_id, procedure_id, "
            "capability_name, action_kind, target_entity, status, payload_json, observed_at "
            f"FROM execution_events{where} ORDER BY observed_at DESC, event_id DESC LIMIT ?"
        )
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._event_row_to_dict(row) for row in rows]

    def query_execution_summary(
        self,
        project_root: Path,
        session_id: str | None = None,
        *,
        _recent_limit: int = 2000,
    ) -> dict[str, Any]:
        """Query: 'What happened in this session?' — returns aggregate execution summary.

        ``_recent_limit`` (kw-only) bounds the project-wide token LIKE scan
        that drives session_breakdown. Production callers leave it at the
        default 2000; tests pass a smaller value to exercise the
        recent-window truncation path on a small fixture without needing
        to insert thousands of synthetic events.
        """
        self.init_db(project_root)
        where = ""
        params: list[Any] = []
        if session_id and session_id.strip():
            where = " WHERE session_id = ?"
            params.append(session_id.strip())
        with self.connect(project_root) as conn:
            # PERF (2026-05-26): one table scan replaces seven separate
            # COUNT / GROUP BY queries. Before, the same (session-filtered)
            # row set was scanned six times for GROUP BY action_kind /
            # event_kind / source_kind / capability_name + two ad-hoc /
            # procedure COUNTs + a total COUNT(*). Each query paid SQLite
            # prepare/execute overhead. After, a single SELECT pulls the
            # five columns and Python tallies every bucket in one pass.
            # Same WHERE clause, same row set, same totals — regression
            # tests in test_token_estimation cover the aggregation shape;
            # action / event / source / tool dicts are built sorted by
            # count DESC to match the prior ORDER BY count DESC SQL.
            agg_rows = conn.execute(
                f"SELECT action_kind, event_kind, source_kind, capability_name, procedure_id FROM execution_events{where}",
                params,
            ).fetchall()
            total_events = len(agg_rows)
            ak_c: dict[Any, int] = {}
            ek_c: dict[Any, int] = {}
            sk_c: dict[Any, int] = {}
            tool_c: dict[Any, int] = {}
            adhoc_count = 0
            procedure_count = 0
            for r in agg_rows:
                ak = r["action_kind"]
                ak_c[ak] = ak_c.get(ak, 0) + 1
                ek = r["event_kind"]
                ek_c[ek] = ek_c.get(ek, 0) + 1
                sk = r["source_kind"]
                sk_c[sk] = sk_c.get(sk, 0) + 1
                if ek == "tool_call_completed":
                    cn = r["capability_name"]
                    tool_c[cn] = tool_c.get(cn, 0) + 1
                if r["procedure_id"] is None:
                    adhoc_count += 1
                else:
                    procedure_count += 1
            action_kinds = [
                {"action_kind": k, "count": v}
                for k, v in sorted(ak_c.items(), key=lambda kv: -kv[1])
            ]
            event_kinds = [
                {"event_kind": k, "count": v}
                for k, v in sorted(ek_c.items(), key=lambda kv: -kv[1])
            ]
            sources = [
                {"source_kind": k, "count": v}
                for k, v in sorted(sk_c.items(), key=lambda kv: -kv[1])
            ]
            tool_names = [
                {"capability_name": k, "count": v}
                for k, v in sorted(tool_c.items(), key=lambda kv: -kv[1])
            ]
            # TOKEN-TRUTH (2026-05-26): two LIKE queries, two distinct truths.
            #
            # The previous single-bounded-scan approach silently truncated
            # the selected-session token totals when old events for that
            # session fell outside the global 2000-row recent window.
            # Per-session "tokens_in", "tokens_by_tool" etc. claim to be
            # session-exact and MUST be exact — anything else is a
            # truth violation labelled as a fact.
            #
            # After:
            #   (a) When session_id is set, a SESSION-SCOPED LIKE query
            #       is unbounded and returns the exact session totals.
            #       Uses idx_execution_events_session_observed (WHERE
            #       session_id=? AND LIKE ...) — index-served, cheap.
            #   (b) The PROJECT-WIDE LIKE remains bounded to the most
            #       recent N events; it drives session_breakdown only.
            #       The returned dict carries:
            #         token_estimates_scope  : "session_exact" | "all_sessions_recent"
            #         session_breakdown_scope: "all_sessions_recent"
            #         breakdown_event_limit  : N
            #       so dashboard token_usage and partial manifests can
            #       label the breakdown explicitly and never present a
            #       recent-window count as an all-time total.
            #
            # When session_id is None, "session totals" is the same as
            # "all sessions recent" by definition — the project-wide
            # scan IS the data, so the same bounded rows drive both
            # session_breakdown and the (necessarily bounded) aggregate
            # token_estimates; scope is labelled "all_sessions_recent".
            TOKEN_BREAKDOWN_RECENT_LIMIT = _recent_limit
            breakdown_rows = conn.execute(
                "SELECT action_kind, capability_name, session_id, payload_json "
                "FROM execution_events "
                "WHERE payload_json LIKE '%tokens_in_estimate%' "
                "ORDER BY observed_at DESC, event_id DESC LIMIT ?",
                (TOKEN_BREAKDOWN_RECENT_LIMIT,),
            ).fetchall()
            if where:
                # Session-scoped LIKE — index-served via
                # idx_execution_events_session_observed; UNBOUNDED so the
                # per-session token totals are exact regardless of how
                # many older events the session has accumulated.
                session_token_rows = conn.execute(
                    "SELECT action_kind, capability_name, session_id, payload_json "
                    "FROM execution_events "
                    "WHERE session_id = ? AND payload_json LIKE '%tokens_in_estimate%'",
                    (params[0],),
                ).fetchall()
                token_estimates_scope = "session_exact"
            else:
                # No session filter: the project-wide bounded rows are
                # the only data — aggregate token totals are necessarily
                # the recent-window view. Labelled accordingly so the
                # consumer never reads them as all-time totals.
                session_token_rows = breakdown_rows
                token_estimates_scope = "all_sessions_recent"
        tokens_in = 0
        tokens_out = 0
        tokens_in_calls = 0
        tokens_out_calls = 0
        tokens_by_action: dict[str, dict[str, int]] = {}
        tokens_by_tool: dict[str, dict[str, int]] = {}
        tokens_by_session: dict[str, dict[str, int]] = {}
        _json = __import__("json")
        # Pass 1 — session-exact totals from the (possibly session-scoped)
        # row set. When session_id is None this is the same row set as
        # pass 2 and the aggregates carry the "all_sessions_recent" label.
        for row in session_token_rows:
            try:
                payload = _json.loads(row["payload_json"]) if row["payload_json"] else {}
                tin = int(payload.get("tokens_in_estimate", 0))
                tout = int(payload.get("tokens_out_estimate", 0))
                tokens_in += tin
                tokens_out += tout
                if tin > 0:
                    tokens_in_calls += 1
                if tout > 0:
                    tokens_out_calls += 1
                ak = str(row["action_kind"] or "unknown")
                ab = tokens_by_action.setdefault(ak, {"tokens_in": 0, "tokens_out": 0, "count": 0})
                ab["tokens_in"] += tin
                ab["tokens_out"] += tout
                ab["count"] += 1
                tool = row["capability_name"]
                if tool:
                    tool = str(tool)
                    tb = tokens_by_tool.setdefault(
                        tool,
                        {"tokens_in": 0, "tokens_out": 0, "count": 0},
                    )
                    tb["tokens_in"] += tin
                    tb["tokens_out"] += tout
                    tb["count"] += 1
            except Exception:
                pass
        # Pass 2 — session_breakdown is always project-wide over the
        # bounded scan; explicitly recent-window, never all-time.
        for row in breakdown_rows:
            try:
                payload = _json.loads(row["payload_json"]) if row["payload_json"] else {}
                tin = int(payload.get("tokens_in_estimate", 0))
                tout = int(payload.get("tokens_out_estimate", 0))
                bucket_sid = str(row["session_id"] or "unbound")
                bk = tokens_by_session.setdefault(
                    bucket_sid,
                    {"tokens_in": 0, "tokens_out": 0, "count": 0},
                )
                bk["tokens_in"] += tin
                bk["tokens_out"] += tout
                bk["count"] += 1
            except Exception:
                pass
        return {
            "session_id": session_id,
            "total_events": int(total_events),
            # TOKEN-TRUTH (2026-05-26): explicit scope labels so the dashboard
            # token_usage and partial manifests can render a bounded breakdown
            # without mislabeling it as an all-time total.
            #
            #   token_estimates_scope ∈ {"session_exact", "all_sessions_recent"}
            #     - "session_exact"        → session_id was set; aggregate
            #                                token_estimates / tokens_by_action /
            #                                tokens_by_tool reflect every
            #                                token-bearing event for that session
            #                                (unbounded session-scoped query).
            #     - "all_sessions_recent"  → session_id was None; aggregates
            #                                are over the bounded recent window
            #                                (project-wide). NOT all-time.
            #   session_breakdown_scope = "all_sessions_recent" (always —
            #     the project-wide LIKE is bounded for query-plan safety).
            #   breakdown_event_limit = the LIMIT used for the project-wide
            #     scan; consumers can detect when a session's breakdown row
            #     may be truncated by comparing to the session's true count.
            "token_estimates_scope": token_estimates_scope,
            "session_breakdown_scope": "all_sessions_recent",
            "breakdown_event_limit": _recent_limit,
            "by_action_kind": {
                row["action_kind"]: int(row["count"]) for row in action_kinds if row["action_kind"]
            },
            "by_event_kind": {row["event_kind"]: int(row["count"]) for row in event_kinds},
            "by_source": {row["source_kind"]: int(row["count"]) for row in sources},
            "by_tool_name": {
                row["capability_name"]: int(row["count"])
                for row in tool_names
                if row["capability_name"]
            },
            "ad_hoc_events": int(adhoc_count),
            "procedure_linked_events": int(procedure_count),
            "token_estimates": {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tokens_in_calls": tokens_in_calls,
                "tokens_out_calls": tokens_out_calls,
                "total": tokens_in + tokens_out,
            },
            "tokens_by_action_kind": tokens_by_action,
            "tokens_by_tool_name": tokens_by_tool,
            "session_breakdown": [
                {
                    "session_id": sid,
                    "tokens_in": data["tokens_in"],
                    "tokens_out": data["tokens_out"],
                    "total": data["tokens_in"] + data["tokens_out"],
                    "events": data["count"],
                }
                for sid, data in sorted(
                    tokens_by_session.items(),
                    key=lambda x: -(x[1]["tokens_in"] + x[1]["tokens_out"]),
                )
            ],
        }


    def query_token_breakdown_by_session(
        self,
        project_root: Path,
    ) -> list[dict[str, object]]:
        """Return per-session token estimates for the project."""
        self.init_db(project_root)
        import json as _json

        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT session_id, payload_json FROM execution_events WHERE payload_json LIKE '%tokens_in_estimate%'",
            ).fetchall()
        sessions: dict[str, dict[str, int]] = {}
        for row in rows:
            sid = str(row["session_id"] or "unbound")
            if sid not in sessions:
                sessions[sid] = {"tokens_in": 0, "tokens_out": 0, "events": 0}
            sessions[sid]["events"] += 1
            try:
                payload = _json.loads(row["payload_json"]) if row["payload_json"] else {}
                sessions[sid]["tokens_in"] += int(payload.get("tokens_in_estimate", 0))
                sessions[sid]["tokens_out"] += int(payload.get("tokens_out_estimate", 0))
            except Exception:
                pass
        return [
            {
                "session_id": sid,
                "tokens_in": data["tokens_in"],
                "tokens_out": data["tokens_out"],
                "total": data["tokens_in"] + data["tokens_out"],
                "events": data["events"],
            }
            for sid, data in sorted(
                sessions.items(),
                key=lambda x: -(x[1]["tokens_in"] + x[1]["tokens_out"]),
            )
        ]

    def query_procedure_compliance(
        self,
        project_root: Path,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Query: 'Did execution follow the intended procedure?' — compares runs against procedures."""
        self.init_db(project_root)
        where = ""
        params: list[Any] = []
        if session_id and session_id.strip():
            where = " WHERE session_id = ?"
            params.append(session_id.strip())
        with self.connect(project_root) as conn:
            # Runs with procedures
            procedured = conn.execute(
                f"SELECT run_id, run_kind, procedure_id, capability_name, status, ad_hoc, started_at, completed_at "
                f"FROM execution_runs{where} {'AND' if where else 'WHERE'} procedure_id IS NOT NULL "
                f"ORDER BY started_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            # Ad-hoc runs (no procedure)
            adhoc = conn.execute(
                f"SELECT run_id, run_kind, capability_name, status, started_at, completed_at "
                f"FROM execution_runs{where} {'AND' if where else 'WHERE'} procedure_id IS NULL AND ad_hoc = 1 "
                f"ORDER BY started_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return {
            "session_id": session_id,
            "procedure_linked_runs": [
                {
                    "run_id": row["run_id"],
                    "run_kind": row["run_kind"],
                    "procedure_id": row["procedure_id"],
                    "capability_name": row["capability_name"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                }
                for row in procedured
            ],
            "ad_hoc_runs": [
                {
                    "run_id": row["run_id"],
                    "run_kind": row["run_kind"],
                    "capability_name": row["capability_name"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                }
                for row in adhoc
            ],
            "compliance_ratio": f"{len(procedured)}/{len(procedured) + len(adhoc)}"
            if (procedured or adhoc)
            else "no data",
        }

    # NOTE: a second, shadowed `prune_old_events(max_age_days, max_events)` used
    # to live here. Python kept only the later `prune_old_events(keep_days=…)`
    # definition below, so this one was dead (every caller hit the keep_days
    # version; max_events is handled by prune_to_max_size). Removed 2026-05-24 —
    # behavior-identical (the interpreter already discarded it).

    def _timestamp(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # ── Management operations ──

    def _ensure_identity_columns(self, project_root: Path) -> None:
        """Add host_id/agent_id columns if they don't exist (migration)."""
        with self.connect(project_root) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(execution_events)").fetchall()
            }
            if "host_id" not in columns:
                conn.execute("ALTER TABLE execution_events ADD COLUMN host_id TEXT")
            if "agent_id" not in columns:
                conn.execute("ALTER TABLE execution_events ADD COLUMN agent_id TEXT")

    def clear_token_usage(self, project_root: Path, *, session_id: str | None = None) -> int:
        # Dashboard token figures are summed from execution_events.payload_json
        # (keys tokens_in_estimate / tokens_out_estimate), not execution_runs.
        # Deleting only runs left the events rows untouched, so the dashboard
        # total never budged after pressing Clear Tokens.
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            if session_id:
                events_deleted = conn.execute(
                    "DELETE FROM execution_events "
                    "WHERE payload_json LIKE '%tokens_in_estimate%' AND session_id = ?",
                    (session_id,),
                ).rowcount
                runs_deleted = conn.execute(
                    "DELETE FROM execution_runs WHERE run_kind = 'mcp_tool_invocation' AND session_id = ?",
                    (session_id,),
                ).rowcount
            else:
                events_deleted = conn.execute(
                    "DELETE FROM execution_events WHERE payload_json LIKE '%tokens_in_estimate%'",
                ).rowcount
                runs_deleted = conn.execute(
                    "DELETE FROM execution_runs WHERE run_kind = 'mcp_tool_invocation'",
                ).rowcount
            return events_deleted + runs_deleted

    def clear_tool_calls(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
    ) -> dict[str, int]:
        """Clear tool call events. Scoped to session if provided."""
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            if session_id:
                events_deleted = conn.execute(
                    "DELETE FROM execution_events WHERE event_kind IN ('tool_call_started', 'tool_call_completed', 'tool_call_failed') AND session_id = ?",
                    (session_id,),
                ).rowcount
                runs_deleted = conn.execute(
                    "DELETE FROM execution_runs WHERE run_kind = 'mcp_tool_invocation' AND session_id = ?",
                    (session_id,),
                ).rowcount
            else:
                events_deleted = conn.execute(
                    "DELETE FROM execution_events WHERE event_kind IN ('tool_call_started', 'tool_call_completed', 'tool_call_failed')",
                ).rowcount
                runs_deleted = conn.execute(
                    "DELETE FROM execution_runs WHERE run_kind = 'mcp_tool_invocation'",
                ).rowcount
            return {"events_deleted": events_deleted, "runs_deleted": runs_deleted}

    def clear_all(self, project_root: Path, *, session_id: str | None = None) -> dict[str, int]:
        """Clear all execution data. Scoped to session if provided."""
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            if session_id:
                events = conn.execute(
                    "DELETE FROM execution_events WHERE session_id = ?",
                    (session_id,),
                ).rowcount
                runs = conn.execute(
                    "DELETE FROM execution_runs WHERE session_id = ?",
                    (session_id,),
                ).rowcount
            else:
                events = conn.execute("DELETE FROM execution_events").rowcount
                runs = conn.execute("DELETE FROM execution_runs").rowcount
            return {"events_deleted": events, "runs_deleted": runs}

    def prune_old_tool_calls(self, project_root: Path, *, keep_days: int = 7) -> dict[str, int]:
        """Delete old tool call events only (NOT token tracking runs). Used by auto-prune."""
        self.init_db(project_root)
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        with self.connect(project_root) as conn:
            events = conn.execute(
                "DELETE FROM execution_events WHERE observed_at < ? AND event_kind IN ('tool_call_started', 'tool_call_completed', 'tool_call_failed')",
                (cutoff,),
            ).rowcount
            return {"events_deleted": events}

    def prune_old_events(self, project_root: Path, *, keep_days: int = 7) -> dict[str, int]:
        """Delete ALL execution events older than keep_days (including token tracking)."""
        self.init_db(project_root)
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        with self.connect(project_root) as conn:
            events = conn.execute(
                "DELETE FROM execution_events WHERE observed_at < ?",
                (cutoff,),
            ).rowcount
            runs = conn.execute(
                "DELETE FROM execution_runs WHERE started_at < ?",
                (cutoff,),
            ).rowcount
            return {"events_deleted": events, "runs_deleted": runs}

    def prune_to_max_size(self, project_root: Path, *, max_events: int = 10000) -> int:
        """Keep only the most recent max_events tool call events, delete the rest.

        max_events <= 0 means UNLIMITED — no count-based pruning.
        """
        if max_events <= 0:
            return 0
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM execution_events WHERE event_kind IN ('tool_call_started', 'tool_call_completed', 'tool_call_failed')",
            ).fetchone()[0]
            if total <= max_events:
                return 0
            to_delete = total - max_events
            deleted = conn.execute(
                "DELETE FROM execution_events WHERE event_id IN (SELECT event_id FROM execution_events WHERE event_kind IN ('tool_call_started', 'tool_call_completed', 'tool_call_failed') ORDER BY observed_at ASC LIMIT ?)",
                (to_delete,),
            ).rowcount
            return deleted

    def auto_prune(self, project_root: Path) -> dict[str, object]:
        """Auto-prune tool call events only (not token tracking). Called on dashboard load."""
        from .config import get_setting

        def _int(key: str, default: int) -> int:
            v = get_setting(key, project_root=project_root, default=default)
            try:
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        # 0 = unlimited for both dimensions (read without the `or default`
        # footgun that turned a configured 0 back into the default).
        max_events = _int("execution.max_events", 10000)
        auto_prune_days = _int("execution.auto_prune_days", 7)
        pruned_by_size = (
            self.prune_to_max_size(project_root, max_events=max_events) if max_events > 0 else 0
        )
        pruned_by_age = (
            self.prune_old_tool_calls(project_root, keep_days=auto_prune_days)
            if auto_prune_days > 0
            else {"events_deleted": 0}
        )
        return {
            "pruned_by_size": pruned_by_size,
            "pruned_by_age": pruned_by_age,
            "max_events": max_events,
            "auto_prune_days": auto_prune_days,
        }

    def event_count(self, project_root: Path) -> dict[str, int]:
        """Get current event and run counts."""
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            events = conn.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
            return {"events": events, "runs": runs}

    def usage_by_host(self, project_root: Path) -> list[dict[str, object]]:
        """Get token/tool usage broken down by host_id."""
        self.init_db(project_root)
        self._ensure_identity_columns(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """SELECT
                    COALESCE(execution_events.host_id, 'unknown') as host,
                    COUNT(*) as event_count,
                    SUM(CASE WHEN json_extract(payload_json, '$.tokens_in_estimate') IS NOT NULL
                        THEN CAST(json_extract(payload_json, '$.tokens_in_estimate') AS INTEGER) ELSE 0 END) as tokens_in,
                    SUM(CASE WHEN json_extract(metadata_json, '$.tokens_in_estimate') IS NOT NULL
                        THEN CAST(json_extract(metadata_json, '$.tokens_in_estimate') AS INTEGER) ELSE 0 END) as tokens_in_runs
                FROM execution_events
                LEFT JOIN execution_runs ON execution_events.run_id = execution_runs.run_id
                GROUP BY execution_events.host_id
                ORDER BY event_count DESC""",
            ).fetchall()
            return [
                {"host_id": row[0], "event_count": row[1], "tokens_in": row[2] + row[3]}
                for row in rows
            ]

    def usage_by_agent(self, project_root: Path) -> list[dict[str, object]]:
        """Get token/tool usage broken down by agent_id."""
        self.init_db(project_root)
        self._ensure_identity_columns(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """SELECT
                    COALESCE(agent_id, 'main') as agent,
                    COUNT(*) as event_count,
                    SUM(CASE WHEN json_extract(payload_json, '$.tokens_in_estimate') IS NOT NULL
                        THEN CAST(json_extract(payload_json, '$.tokens_in_estimate') AS INTEGER) ELSE 0 END) as tokens_in
                FROM execution_events
                GROUP BY agent_id
                ORDER BY event_count DESC""",
            ).fetchall()
            return [
                {"agent_id": row[0], "event_count": row[1], "tokens_in": row[2]} for row in rows
            ]
