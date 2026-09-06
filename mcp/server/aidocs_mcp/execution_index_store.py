from __future__ import annotations

import json
import queue
import random
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect
from .audit_deletion_law import require_warrant

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

#: #885. The APPEND-ONLY replacement for the old token-usage DELETE. One row
#: per "reset my token counter", carrying the per-session chain_seq floor the
#: token queries read. Classified in ``execution_event_retention`` as DECISION:
#: an operator hid figures from a report, which is a thing someone may have to
#: justify, and it must not be evictable by a burst of chatter.
TOKEN_USAGE_RESET_EVENT_KIND = "token_usage_reset"


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


#: This writer's own waiting budget, deliberately larger than the shared
#: busy_timeout=2000 at the canonical connect. WAL admits exactly ONE writer, so
#: this IS the whole window, and the audit write is a single short INSERT on the
#: governed hook hot path: waiting longer costs almost nothing, while giving up
#: costs a refused tool call.
_WRITE_BUSY_TIMEOUT_MS = 10_000
#: BOUNDED on purpose. An unbounded retry turns a genuinely wedged database into
#: a hang, which is worse than an honest refusal.
_WRITE_RETRY_ATTEMPTS = 4
_WRITE_RETRY_BASE_SLEEP_S = 0.05


def _size_snapshot(db_path: Path | None) -> str:
    """Best-effort db/-wal size note for the exhaustion refusal (#850 clause 1).

    A stat takes NO sqlite locks, so it is safe on the very path where sqlite
    just refused us. The point is a SELF-IDENTIFYING outage: 2026-08-20 and
    2026-08-21 each cost a night of wrong theories because the refusal named
    neither size. Reading the numbers: a -wal far above the ~4MB default
    autocheckpoint ceiling (1000 pages) means WAL reclaim is being prevented —
    either a long-held reader snapshot (passive checkpoint cannot backfill
    past it) or a long writer / checkpoint fold (the very lock holder these
    retries lost to). Those have OPPOSITE fixes, and the sizes are the first
    fork in that diagnosis. Uncertain stats degrade to 'absent', never raise:
    the diagnostic must not be able to break the refusal it decorates.
    """
    if db_path is None:
        return ""

    def _fmt(p: Path) -> str:
        try:
            return f"{p.stat().st_size:,}B"
        except OSError:
            return "absent"

    wal = db_path.with_name(db_path.name + "-wal")
    return f" [db={_fmt(db_path)} wal={_fmt(wal)}]"


# ── Retention trigger (2026-08-23) ──────────────────────────────────────
# `auto_prune`'s docstring named its ONLY trigger: "Called on dashboard
# load." The dashboard had been broken for weeks, so `execution_events` was
# never pruned in 27 days and reached 235,307 rows / 701.7 MB, at which
# point the write lock saturated and governed tool calls executed with no
# result audit at all. Retention that depends on a UI being opened is not
# retention.
#
# The trigger is now the thing that CAUSES the growth: writing events. Two
# guards keep it off the hot path:
#   * COUNT — a pass is considered only every Nth write, so the ordinary
#     write pays one dict update;
#   * INTERVAL — and only if the last pass for this db was long enough ago,
#     so a burst cannot schedule a table scan per call. Pruning on EVERY
#     write would just be a different way to saturate the same lock.
# The pass itself runs on a background thread: retention must never sit in
# front of an audit write either.
#: How retention READS the kind column. The write boundary normalises from
#: 2026-08-23 on, but the ledger already holds 27 days of rows written
#: without it, and those are exactly the rows retention is about to judge.
#: Bare TRIM() strips spaces ONLY, so a kind ending in a newline would still
#: dodge both the registry IN-list and the prefix net; char() spells the
#: control characters out instead of embedding literal ones in SQL text.
_KIND_EXPR = "TRIM(event_kind, ' ' || char(9) || char(10) || char(13))"

_RETENTION_TRIGGER_EVERY = 500
_RETENTION_MIN_INTERVAL_S = 900.0
_RETENTION_QUEUE_MAX = 8

#: Rows deleted per prune statement (#748). WAL admits exactly ONE writer, and
#: this table shares its file -- so its writer lock -- with the gate's authority
#: tables (session_query_gate, actor_task_state, freeze_strike_notices), which
#: write two or three rows a day. A single DELETE over a whole backlog holds that
#: writer for its entire duration, and every prompt-submit transaction waiting on
#: those tables fails "database is locked" meanwhile. Batching does not make the
#: prune cheaper; it makes the lock RELEASABLE between batches. Sized to be large
#: enough that a routine pass is one or two statements, small enough that a
#: 27-day catch-up cannot monopolise the writer.
_PRUNE_CHUNK_ROWS = 5000

_retention_lock = threading.Lock()
_retention_counts: dict[str, int] = {}
_retention_last_run: dict[str, float] = {}
_retention_queue: queue.Queue[tuple[Any, Path]] | None = None
_retention_worker: threading.Thread | None = None


def _retention_loop(work: queue.Queue[tuple[Any, Path]]) -> None:
    while True:
        store, project_root = work.get()
        try:
            store.auto_prune(project_root)
        except Exception as exc:  # noqa: BLE001 — retention never breaks a write
            try:
                sys.stderr.write(
                    f"[aidocs retention] pass failed for {project_root}: "
                    f"{type(exc).__name__}: {exc}\n",
                )
            except Exception:
                pass
        finally:
            work.task_done()


def _retention_channel() -> queue.Queue[tuple[Any, Path]]:
    global _retention_queue, _retention_worker
    if _retention_queue is None:
        _retention_queue = queue.Queue(maxsize=_RETENTION_QUEUE_MAX)
    if _retention_worker is None or not _retention_worker.is_alive():
        _retention_worker = threading.Thread(
            target=_retention_loop,
            args=(_retention_queue,),
            name="aidocs-retention",
            daemon=True,
        )
        _retention_worker.start()
    return _retention_queue


def _schedule_retention(store: Any, project_root: Path, db_path: Path) -> None:
    """Count this write and, on the Nth one past the interval floor, queue a
    retention pass. Never raises, never blocks."""
    try:
        key = str(db_path)
        now = time.monotonic()
        with _retention_lock:
            count = _retention_counts.get(key, 0) + 1
            _retention_counts[key] = count
            if _RETENTION_TRIGGER_EVERY <= 0 or count % _RETENTION_TRIGGER_EVERY:
                return
            last = _retention_last_run.get(key, 0.0)
            if last and (now - last) < _RETENTION_MIN_INTERVAL_S:
                return
            _retention_last_run[key] = now
            channel = _retention_channel()
        channel.put_nowait((store, project_root))
    except queue.Full:
        # Retention is already backed up; skipping a pass is correct.
        pass
    except Exception:  # noqa: BLE001 — an audit write must never fail on this
        pass


def flush_retention(timeout: float = 30.0) -> bool:
    """Wait (bounded) for queued retention passes. Tests only."""
    work = _retention_queue
    if work is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while work.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


class AuditWriteUnavailable(RuntimeError):
    """The audit row could not be written after bounded retries.

    DISTINGUISHABLE ON PURPOSE (#768). `intent_audit_or_refuse` refusing when the
    audit cannot be recorded is CORRECT and unchanged -- but the caller, and the
    operator reading the log, must be able to tell "the audit store was
    unreachable" from "your tool failed". A bare sqlite3.OperationalError reads
    as the latter and sent four different tests looking like flaky code.

    Never swallowed: an audit record is the one thing that must not vanish
    quietly, so exhaustion RAISES rather than returning.
    """


def _write_with_retry(
    what: str, op: Callable[[], Any], *, db_path: Path | None = None
) -> Any:
    """Run a write, absorbing TRANSIENT lock/busy errors.

    `_is_transient_lock` has lived one function above since the migration path
    needed it; the ROW-write path never used it, and that gap is #768 -- the
    store was lock-aware when CREATING TABLES and lock-naive when WRITING ROWS.

    Anything that is not a transient lock re-raises IMMEDIATELY: a real schema
    or constraint bug must surface as itself, not as a slow retry loop.
    """
    last: Exception | None = None
    for attempt in range(_WRITE_RETRY_ATTEMPTS):
        try:
            return op()
        except sqlite3.OperationalError as exc:
            if not _is_transient_lock(exc):
                raise
            last = exc
            if attempt + 1 < _WRITE_RETRY_ATTEMPTS:
                # #776: deterministic growth alone CANNOT de-synchronise
                # anything -- N writers that start together and sleep the
                # same exact 50/100/200ms wake together and collide again.
                # Only a random component spreads them apart. Full jitter
                # (0..ceiling, not ceiling/2..ceiling) so a pair that happens
                # to draw small values on the same attempt can still land in
                # different windows, while the ceiling itself still grows
                # with `attempt` so the worst case is unchanged.
                ceiling = _WRITE_RETRY_BASE_SLEEP_S * (2**attempt)
                time.sleep(random.uniform(0, ceiling))
    raise AuditWriteUnavailable(
        f"audit write '{what}' could not acquire the execution index after "
        f"{_WRITE_RETRY_ATTEMPTS} attempts: {last}{_size_snapshot(db_path)}",
    ) from last


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
        return self._require_absolute(project_root) / ".MEMORY" / ".index"

    @staticmethod
    def _require_absolute(project_root: Path) -> Path:
        """Refuse to bind the audit ledger to a RELATIVE project root.

        db_path is ``project_root / ".MEMORY" / ".index" / ...`` and connect()
        does ``mkdir(parents=True)``, so a relative root silently binds the
        ledger to whatever cwd the process happens to hold AND materialises a
        project tree there. A bare NAME is the dangerous case:

            Path("restore")  ->  <cwd>/restore/.MEMORY/.index/aidocs.sqlite3

        which is how a CLI mode name reaching the root resolver in a path
        position produced 24 phantom project roots at the AIDOCS repo root,
        each holding only the store its own creation had provisioned, plus a
        shadow ledger under mcp/ that swallowed 872 real audit events the
        dashboard could never show because it reads the true project root.

        An audit ledger bound to an ambiguous root is worse than a loud
        failure: the ink lands somewhere nothing reads, so the record is lost
        exactly when it is needed. Fail closed and name the mistake.
        """
        resolved = Path(project_root)
        if not resolved.is_absolute():
            raise ValueError(
                "ExecutionIndexStore needs an ABSOLUTE project root; got "
                f"{str(project_root)!r}. A relative root binds the audit ledger "
                "to the current working directory and creates a phantom project "
                "tree there. Pass the resolved project root."
            )
        return resolved

    def db_path(self, project_root: Path) -> Path:
        return self.index_root(project_root) / "aidocs.sqlite3"

    def connect(self, project_root: Path) -> sqlite3.Connection:
        db_path = self.db_path(project_root)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # #755: through the ONE canonical connect. This store is on the governed
        # hook hot path — it was opening at sqlite's default synchronous=FULL,
        # paying an fsync per commit (measured 8-10x, #754), and with
        # foreign_keys defaulted OFF. RUNTIME durability: execution events are
        # telemetry, and the operator's 2026-08-04 ruling accepted losing recent
        # commits to a POWER CUT only — a process kill still loses nothing.
        # #768: this store gets a LARGER waiting budget than the shared 2000ms
        # default. WAL admits one writer, so busy_timeout is the entire window,
        # and under xdist (or two agents on one project) the competing writers
        # are PROCESSES. Both knobs are raised together: busy_timeout_ms is the
        # sqlite pragma, `timeout` is python's own connect-level wait, and
        # leaving the latter at 2.0s would cap the former.
        return _canonical_connect(
            db_path,
            durability=_Durability.RUNTIME,
            timeout=_WRITE_BUSY_TIMEOUT_MS / 1000,
            busy_timeout_ms=_WRITE_BUSY_TIMEOUT_MS,
        )

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

                -- Index-reconciler heartbeat (2026-08-23). ONE row, upserted
                -- forever. `index_sitter_reconcile` used to be written to
                -- execution_events per occurrence: 30,202 rows in 27 days,
                -- the second-largest kind in the table that saturated the
                -- write lock. It has no consumer anywhere in the tree and it
                -- records no decision -- an audit trail carries decisions,
                -- refusals and privileged acts, not a background loop's
                -- pulse.
                --
                -- It lives HERE and not in execution_events because that
                -- table is an append-only hash-chained ledger (prev_hash /
                -- chain_seq): upserting a row in place would break the very
                -- chain it exists to protect. This table is state, not
                -- evidence, so overwriting is correct.
                --
                -- last/previous give "when did the index reconcile, and when
                -- before that". The unhealthy pair is separate on purpose: a
                -- single overwritten row would let the next healthy poll
                -- erase the fact that the index had been broken, and the
                -- collapse would have COST truth instead of just space.
                CREATE TABLE IF NOT EXISTS index_reconcile_state (
                    scope TEXT PRIMARY KEY,
                    last_reconcile_at TEXT NOT NULL DEFAULT '',
                    previous_reconcile_at TEXT NOT NULL DEFAULT '',
                    reconcile_count INTEGER NOT NULL DEFAULT 0,
                    last_trigger TEXT NOT NULL DEFAULT '',
                    last_state TEXT NOT NULL DEFAULT '',
                    last_synced_rows INTEGER NOT NULL DEFAULT 0,
                    last_tracked_rows INTEGER NOT NULL DEFAULT 0,
                    last_unhealthy_at TEXT NOT NULL DEFAULT '',
                    last_unhealthy_state TEXT NOT NULL DEFAULT ''
                );
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
                # user_id defaults to local-operator at global scope — honest
                # for solo-flavor installs, and re-attributable via later
                # migrations once login lands. effective_role does NOT get
                # the matching "assume the top role" treatment (#631): a
                # pre-migration row that predates this column has no proven
                # role, and 'unknown' — the same sentinel
                # identity_resolver.UNKNOWN_ROLE uses for unresolved-role
                # attribution (#576 D1) — says so instead of manufacturing
                # super_admin authority nobody was shown to hold.
                # #936: '' (identity_resolver.UNATTRIBUTED_USER), never
                # 'operator'. NOTE this is a MIGRATION: sqlite applies the
                # default to rows that predate the column, so on an existing
                # ledger those rows were already stamped and this change does
                # NOT correct them retroactively.
                "ALTER TABLE execution_events ADD COLUMN user_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE execution_events ADD COLUMN effective_role TEXT NOT NULL DEFAULT 'unknown'",
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
                    # CAUSAL-TURN HOT PATH (#489). Every UserPromptSubmit rotates
                    # the operator turn, which seals the superseded one
                    # (rotate_current_turn_id -> close_superseded_turn ->
                    # seal_turn). That path reads execution_events by turn_id
                    # three times: _turn_event_leaves (Merkle leaves for the
                    # seal), _attempt_counts and list_orphan_attempts. turn_id
                    # was UNINDEXED, so each was a full scan plus a temp b-tree
                    # over an audit table that only grows.
                    #
                    # MEASURED on the operator's live db (92,883 rows / 222MB,
                    # 2026-07-30): _turn_event_leaves 805ms for 188 rows;
                    # cProfile of ONE UserPromptSubmit put 2.84s of a 3.6s
                    # evaluation inside these two helpers. That was the #489
                    # "warm hook broker did not answer (timed_out)" banner —
                    # compute, not queueing, and growing with history, which is
                    # why raising the client budget only ever bought time.
                    #
                    # chain_seq second so the index also supplies the
                    # ORDER BY chain_seq ASC these statements all carry (no temp
                    # b-tree). Additive; the rows returned do not change.
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_execution_events_turn_seq "
                        "ON execution_events(turn_id, chain_seq)",
                    )
                    # list_orphan_attempts() with NO turn/session filter reads
                    # every attempt row in project history (11,101 rows on the
                    # live db). event_kind was likewise unindexed. This keeps it
                    # a SEARCH; the query itself is still unbounded by design
                    # (invariant 20 — an orphan is never dropped), so the index
                    # bounds the read cost, not the result set.
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_execution_events_kind_seq "
                        "ON execution_events(event_kind, chain_seq)",
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
        # Resolve the acting identity + agent epoch BEFORE the run_id, exactly
        # as record_event_on_connection does for the event digest (#672). Two
        # different actors performing the same run_kind on the same
        # capability/session with the same metadata inside one bucket used to
        # compute the SAME run_id, and the ON CONFLICT DO UPDATE below then
        # merged the second actor's run onto the first actor's row. The actor
        # is part of run identity, same as it is part of event identity.
        # Only resolved when we actually derive an id — a caller-supplied
        # run_id (the started/completed pair) must not pay for identity.
        if not run_id:
            resolved_user_id: str | None = None
            try:
                from .identity_resolver import current_user

                resolved_user_id = current_user(project_root)[0]
            except Exception:
                resolved_user_id = None
            if resolved_user_id is None:
                # #936: an actor that did not resolve is UNATTRIBUTED, never
                # the person named 'operator'. This value folds into the run
                # id (the comment above), so the manufactured name also merged
                # every unattributed actor's runs into one — the identity-blind
                # dedup this block exists to prevent.
                from .identity_resolver import UNATTRIBUTED_USER

                resolved_user_id = UNATTRIBUTED_USER
            run_id = self._compute_run_id(
                run_kind,
                capability_name,
                session_id,
                metadata_json,
                started_at,
                resolved_user_id,
                self._resolve_audit_epoch(project_root),
            )
        # #768: the ROW write goes through the retry. A competing writer holding
        # the WAL lock for longer than ONE busy_timeout window used to propagate
        # SQLITE_BUSY all the way up to intent_audit_or_refuse, which then
        # refused a governed call that had nothing wrong with it.
        def _write() -> None:
            with self.connect(project_root) as conn:
                # #776: BEGIN IMMEDIATE takes the write lock UP FRONT, before
                # this connection has done anything else. Left to python's
                # default (isolation_level=""), the first statement below
                # would open an implicit DEFERRED transaction instead --
                # lazily taking a read snapshot and only then discovering,
                # mid-statement, that it needs to upgrade to a write. If a
                # competing writer commits in that gap, SQLite raises
                # SQLITE_BUSY_SNAPSHOT immediately and busy_timeout is never
                # consulted: waiting cannot un-stale a snapshot, only a fresh
                # transaction can. Acquiring the write lock first means this
                # transaction is never built on a snapshot that can go stale
                # underneath it -- any wait it needs is an ordinary
                # SQLITE_BUSY wait, which busy_timeout (and the retry above)
                # already cover.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO execution_runs (
                        run_id, run_kind, source_kind, session_id, procedure_id,
                        capability_name, status, ad_hoc, target_entity,
                        metadata_json, started_at, completed_at
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
                        completed_at=COALESCE(
                            excluded.completed_at, execution_runs.completed_at
                        )
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

        _write_with_retry("record_run", _write, db_path=self.db_path(project_root))
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
        user_id: str = "",
        agent_epoch: str = "",
    ) -> str:
        import hashlib

        bucket = int(cls._iso_to_epoch(started_at) // cls._DEDUP_BUCKET_SECONDS)
        raw = (
            f"{run_kind}|{capability_name or ''}|{session_id or ''}"
            f"|{user_id or ''}|{agent_epoch or ''}|{metadata_json}|{bucket}"
        )
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
        # #768: the same retry as record_run. record_event is the SIBLING row
        # writer and was equally exposed -- fixing only one would have left the
        # identical flake reachable through the other path. The empire mirror
        # below deliberately stays OUTSIDE the retry: it is best-effort by
        # design and must not be re-attempted just because the kingdom write
        # needed a second try.
        def _write() -> str:
            with self.connect(project_root) as conn:
                # #776: same remedy as record_run's sibling write -- take the
                # write lock UP FRONT so this fresh connection never lazily
                # opens a DEFERRED transaction, reads under that stale
                # snapshot, and then discovers mid-flight it needs to
                # upgrade to a write (SQLITE_BUSY_SNAPSHOT, un-waitable).
                # Safe here specifically because THIS connection was just
                # opened by `self.connect` above and owned by nobody else --
                # unlike `record_event_on_connection` itself, which callers
                # also invoke on a connection they already hold open in
                # their OWN transaction and must not be forced to begin.
                conn.execute("BEGIN IMMEDIATE")
                return self.record_event_on_connection(
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

        written_event_id = _write_with_retry(
            "record_event", _write, db_path=self.db_path(project_root)
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
        # DEFECT A (2026-08-23): retention is triggered by the WRITE, not by
        # a dashboard. Counted here, executed on a background thread, rate
        # limited — this call adds one dict update to the audit path.
        _schedule_retention(self, project_root, self.db_path(project_root))
        return written_event_id

    def _mirror_event_to_empire(self, project_root: Path, event_id: str) -> None:
        """Best-effort empire mirror of one COMMITTED event row.

        #754 part B: the row is captured here but its empire COMMIT is
        DEFERRED to a batched background flush. The kingdom row above is
        already durable and is untouched by this -- it remains the
        fail-closed authority, written synchronously, and nothing about the
        decision path changes.

        Deferring is safe here and nowhere else on this path because no
        production code READS empire_audit_events: EmpireAuditStore has
        exactly one non-test reference in the server package, and it is this
        write. See the DEFERRED MIRROR QUEUE note in empire_audit_store.

        The kingdom SELECT stays inline on purpose -- it is a non-fsync read,
        and materialising the row HERE means a queued mirror can never be
        lost to a later wipe/rotation nor misrouted by an empire re-home.
        """
        try:
            from .empire_audit_store import enqueue_event_mirror, mirror_enabled

            if not mirror_enabled():
                return
            with self.connect(project_root) as conn:
                row = conn.execute(
                    "SELECT * FROM execution_events WHERE event_id = ? LIMIT 1",
                    (event_id,),
                ).fetchone()
            if row is None:
                return
            enqueue_event_mirror(project_root, dict(row))
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
                    -- #936: '' (identity_resolver.UNATTRIBUTED_USER), never
                    -- 'operator' — that is a REAL user_id (what
                    -- bootstrap_local_superadmin mints), so it cannot mark an
                    -- actor who never resolved.
                    user_id TEXT NOT NULL DEFAULT '',
                    -- #631: 'unknown' (identity_resolver.UNKNOWN_ROLE), never
                    -- 'super_admin' — a row with no explicit role has no
                    -- proven authority and must not be recorded as the
                    -- highest one.
                    effective_role TEXT NOT NULL DEFAULT 'unknown',
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
        # NORMALISE THE KIND AT THE WRITE BOUNDARY (2026-08-23, conductor
        # mutant R10). A kind differing from its declared name only by
        # surrounding whitespace is not a different kind, and letting one
        # into the column is a silent forensic downgrade: retention matches
        # `event_kind` verbatim against the registry IN-list and against
        # substr(event_kind, 1, N), so ' rbac_denied' misses BOTH guards,
        # lands in the operational catch-all and is deleted at seven days.
        #
        # Reachable, not theoretical: hook_pipeline.record_hook_event strips
        # session_id, tool_name and prompt on three consecutive lines and
        # then writes `event_kind = event_name.lower()` with no strip.
        #
        # Done BEFORE _compute_event_id and the row hash so the dedup key
        # and the chain both bind the normalised value -- ' x' and 'x' must
        # not become two different audit identities either.
        event_kind = str(event_kind or "").strip()
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
        # hash (B). An unresolvable user_id is UNATTRIBUTED, never a person.
        resolved_user_id = user_id
        if resolved_user_id is None:
            try:
                from .identity_resolver import current_user

                resolved_user_id = current_user(project_root)[0]
            except Exception:
                resolved_user_id = None
        if resolved_user_id is None:
            # #936, and THE site that matters most of the three: this value is
            # folded into the event_id AND the v2 row hash above, so a
            # manufactured 'operator' did not merely label the row — it was
            # SEALED INTO the audit chain's own identity.
            from .identity_resolver import UNATTRIBUTED_USER

            resolved_user_id = UNATTRIBUTED_USER
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
            # #936: an unresolved actor is UNATTRIBUTED. Stamping 'operator'
            # here made the audit chain attribute an action to a NAMED HUMAN
            # nobody was shown to have taken it — the user_id twin of the role
            # defect cured five lines below, and the same law (183074ae).
            from .identity_resolver import UNATTRIBUTED_USER

            resolved_user_id = UNATTRIBUTED_USER
        if resolved_role is None:
            # #576 D1: an unresolved role is UNKNOWN. Stamping
            # 'super_admin' here made the audit chain assert authority
            # nobody was proven to hold (law 183074ae).
            resolved_role = "unknown"
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
        # Resolved by the ONE authority (#525/#539). This used to hand-roll the
        # ladder (current_calling_host_session_id + read_memory_surfacer's
        # env-sniffing _detect_host_kind), which is how the WRITE path here and
        # the READ path in agent_audit._host_kinds ended up disagreeing about the
        # same agent's host_kind.
        from .agent_memory_epoch import resolve_epoch

        return resolve_epoch(project_root) or ""

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
            # #885: the token counter resets by WATERMARK, not by DELETE. Rows
            # at or below their session's chain_seq floor were counted before
            # the operator last reset the counter and are excluded HERE, on the
            # read — they stay in the ledger.
            token_floors = self._token_chain_seq_floors(conn)
            breakdown_rows = [
                row
                for row in conn.execute(
                    "SELECT action_kind, capability_name, session_id, chain_seq, payload_json "
                    "FROM execution_events "
                    "WHERE payload_json LIKE '%tokens_in_estimate%' "
                    "ORDER BY observed_at DESC, event_id DESC LIMIT ?",
                    (TOKEN_BREAKDOWN_RECENT_LIMIT,),
                ).fetchall()
                if self._above_token_floor(row, token_floors)
            ]
            if where:
                # Session-scoped LIKE — index-served via
                # idx_execution_events_session_observed; UNBOUNDED so the
                # per-session token totals are exact regardless of how
                # many older events the session has accumulated.
                session_token_rows = [
                    row
                    for row in conn.execute(
                        "SELECT action_kind, capability_name, session_id, chain_seq, payload_json "
                        "FROM execution_events "
                        "WHERE session_id = ? AND payload_json LIKE '%tokens_in_estimate%'",
                        (params[0],),
                    ).fetchall()
                    if self._above_token_floor(row, token_floors)
                ]
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
            # #885: same watermark as query_execution_summary. Two readers of
            # the same numbers must apply the same floor, or the dashboard's
            # per-session breakdown contradicts its own total.
            token_floors = self._token_chain_seq_floors(conn)
            rows = [
                row
                for row in conn.execute(
                    "SELECT session_id, chain_seq, payload_json FROM execution_events "
                    "WHERE payload_json LIKE '%tokens_in_estimate%'",
                ).fetchall()
                if self._above_token_floor(row, token_floors)
            ]
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

    def reset_token_usage_counter(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
        reason: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        """Reset the DISPLAYED token counter by APPENDING a watermark.

        #885. This used to be ``clear_token_usage``, and it deleted::

            DELETE FROM execution_events WHERE payload_json LIKE '%tokens_in_estimate%'
            DELETE FROM execution_runs   WHERE run_kind = 'mcp_tool_invocation'

        against a table that is append-only and hash-chained. There is no
        correct deletion predicate here, and this is not a matter of writing a
        tighter one: the token estimate is stamped onto the tool call's OUTCOME
        payload (``mcp_server`` sets ``payload_summary["tokens_in_estimate"]``
        on completion) and never onto its ``tool_call_started`` attempt row. So
        ANY predicate selecting "rows carrying token numbers" removes outcomes
        and leaves attempts, and
        ``causal_turn_store.list_orphan_attempts`` — the #467 crash-recovery
        surface, whose whole contract is "durable intent with no recorded
        outcome, never deleted, never assumed failed" — then reads every
        completed call as a crash. 10,368 such false orphans were measured on
        2026-08-23 (42.3% of surviving attempts), alongside 8,266 started
        run_ids whose ``execution_runs`` row the second DELETE had removed.

        What the operator actually wants from "Clear Tokens" is a counter that
        reads zero, which is a DISPLAY concern. So the reset writes a
        ``token_usage_reset`` row recording a per-session ``chain_seq`` floor,
        and the token queries count only rows above their session's floor.
        Nothing is deleted, the chain stays intact, no orphan is fabricated, and
        work done after the reset is counted again.

        chain_seq — not ``observed_at`` — is the floor because chain_seq is the
        session's own monotonic sequence, while observed_at is truncated to
        whole seconds and cannot order two events inside the same second.
        """
        self.init_db(project_root)
        sid = str(session_id or "").strip() or None
        with self.connect(project_root) as conn:
            if sid:
                rows = conn.execute(
                    "SELECT session_id, MAX(chain_seq) AS seq FROM execution_events "
                    "WHERE session_id = ? GROUP BY session_id",
                    (sid,),
                ).fetchall()
            else:
                # Project-wide reset: floor every session that has rows TODAY.
                # A session created later is absent from the map, floors at 0,
                # and is counted in full — which is what "reset the counter now"
                # means.
                rows = conn.execute(
                    "SELECT session_id, MAX(chain_seq) AS seq FROM execution_events "
                    "GROUP BY session_id",
                ).fetchall()
            floors = {str(row["session_id"] or ""): int(row["seq"] or 0) for row in rows}
        event_id = self.record_event(
            project_root,
            event_kind=TOKEN_USAGE_RESET_EVENT_KIND,
            source_kind="token_usage_reset",
            session_id=sid,
            action_kind="reset_token_counter",
            target_entity="execution_token_usage",
            status="reset",
            payload={
                "scope": sid or "*all*",
                "reason": reason,
                "actor": actor,
                "chain_seq_floors": floors,
            },
        )
        return {
            "reset": True,
            "event_id": event_id,
            "scope": sid or "*all*",
            "sessions_floored": len(floors),
            # Stated explicitly so no caller and no dashboard can report this
            # as a deletion. It is not one.
            "events_deleted": 0,
            "runs_deleted": 0,
        }

    def _token_chain_seq_floors(self, conn: Any) -> dict[str, int]:
        """session_id -> the chain_seq below which token rows are not counted.

        Folded with ``max`` across every watermark, so the order the rows come
        back in does not matter and a later project-wide reset can only ever
        raise a session's floor.
        """
        floors: dict[str, int] = {}
        try:
            rows = conn.execute(
                "SELECT payload_json FROM execution_events WHERE event_kind = ?",
                (TOKEN_USAGE_RESET_EVENT_KIND,),
            ).fetchall()
        except Exception:
            # A read that cannot resolve the floor must not silently present
            # pre-reset totals as current, but it must also not lose the
            # ledger: report un-floored (the honest superset) rather than zero.
            return floors
        for row in rows:
            try:
                payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            except Exception:
                continue
            for sid, seq in (payload.get("chain_seq_floors") or {}).items():
                try:
                    value = int(seq or 0)
                except (TypeError, ValueError):
                    continue
                if value > floors.get(str(sid), 0):
                    floors[str(sid)] = value
        return floors

    @staticmethod
    def _above_token_floor(row: Any, floors: dict[str, int]) -> bool:
        if not floors:
            return True
        return int(row["chain_seq"] or 0) > floors.get(str(row["session_id"] or ""), 0)

    def clear_tool_calls(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
    ) -> dict[str, int]:
        """Clear tool call events. Scoped to session if provided.

        #885 chokepoint: this DELETES from the append-only hash-chained ledger,
        so it runs only under ``audit_deletion_law``. The check is first, before
        any statement executes, so a refusal is never a partial delete.
        """
        require_warrant("clear_tool_calls")
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
        """Clear all execution data. Scoped to session if provided.

        #885 chokepoint: see :meth:`clear_tool_calls`.
        """
        require_warrant("clear_all")
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

    def _retention_predicates(self) -> tuple[str, list[Any], str, list[Any]]:
        """SQL for 'this row is count-capped' and its parameters.

        Returns (capped_sql, capped_params, uncapped_sql, uncapped_params).
        A row is count-capped when it is neither DECISION nor FORENSIC --
        which deliberately INCLUDES kinds nobody classified. The 2026-08-23
        outage happened because an unmatched kind was retained forever; the
        catch-all is what makes the table bounded again, and the registry
        test is what forbids reaching it in the first place.
        """
        from .execution_event_retention import (
            RetentionClass,
            forensic_prefixes,
            kinds_in_class,
        )

        protected = [
            *kinds_in_class(RetentionClass.DECISION),
            *kinds_in_class(RetentionClass.FORENSIC),
        ]
        prefixes = forensic_prefixes()
        holes = ",".join("?" for _ in protected) or "NULL"
        kind = _KIND_EXPR
        # substr(...) rather than LIKE: every one of these prefixes contains
        # '_', which LIKE treats as a single-character wildcard.
        prefix_sql = f" AND substr({kind}, 1, ?) <> ?" * len(prefixes)
        prefix_params: list[Any] = []
        for prefix in prefixes:
            prefix_params.extend((len(prefix), prefix))
        capped = f"{kind} NOT IN ({holes}){prefix_sql}"
        capped_params: list[Any] = [*protected, *prefix_params]
        return capped, capped_params, f"NOT ({capped})", list(capped_params)

    def _delete_in_chunks(
        self,
        conn: Any,
        select_sql: str,
        params: tuple[Any, ...],
        *,
        limit: int | None = None,
    ) -> int:
        """Delete the rows ``select_sql`` picks, in batches, committing between.

        ``select_sql`` selects ``event_id`` and MUST end in ``LIMIT ?`` — the
        batch size is bound as its final parameter. ``limit``, when given, caps
        the total number removed across all batches.

        The commit between batches is the entire point (#748). It is what
        releases the single WAL writer, so a prompt-submit transaction waiting
        on the authority tables in this same file gets a turn instead of timing
        out behind one long DELETE.
        """
        removed = 0
        while True:
            batch = _PRUNE_CHUNK_ROWS
            if limit is not None:
                batch = min(batch, limit - removed)
                if batch <= 0:
                    break
            deleted = conn.execute(
                f"DELETE FROM execution_events WHERE event_id IN ({select_sql})",
                (*params, batch),
            ).rowcount
            conn.commit()
            removed += deleted
            # A short batch means the predicate is exhausted. Checking AFTER the
            # count is added is what stops an exact multiple losing its last one.
            if deleted < batch:
                break
        return removed

    def prune_by_retention_class(
        self,
        project_root: Path,
        *,
        mechanical_days: int = 1,
        operational_days: int = 7,
        decision_days: int = 90,
    ) -> dict[str, int]:
        """Age-prune every event kind under ITS OWN horizon.

        Replaces the hardcoded
        ``event_kind IN ('tool_call_started','tool_call_completed',
        'tool_call_failed')`` filter, which covered 17.4% of the rows in the
        table that saturated the write lock on 2026-08-23. FORENSIC rows are
        outside every horizon; a day count <= 0 disables that class's pass.
        """
        from datetime import timedelta

        from .execution_event_retention import RetentionClass, kinds_in_class

        self.init_db(project_root)
        now = datetime.now(UTC)
        capped_sql, capped_params, _, _ = self._retention_predicates()
        deleted = {"mechanical": 0, "operational": 0, "decision": 0, "forensic": 0}
        with self.connect(project_root) as conn:
            for label, kinds, days in (
                (
                    "mechanical",
                    kinds_in_class(RetentionClass.MECHANICAL),
                    mechanical_days,
                ),
                ("decision", kinds_in_class(RetentionClass.DECISION), decision_days),
            ):
                if days <= 0 or not kinds:
                    continue
                cutoff = (now - timedelta(days=days)).isoformat()
                holes = ",".join("?" for _ in kinds)
                deleted[label] = self._delete_in_chunks(
                    conn,
                    "SELECT event_id FROM execution_events WHERE observed_at < ? "
                    f"AND {_KIND_EXPR} IN ({holes}) LIMIT ?",
                    (cutoff, *kinds),
                )
            if operational_days > 0:
                # OPERATIONAL *and everything unclassified*: the catch-all
                # leg. MECHANICAL kinds are excluded because their own,
                # shorter pass above already ran.
                mech = kinds_in_class(RetentionClass.MECHANICAL)
                mech_holes = ",".join("?" for _ in mech) or "NULL"
                cutoff = (now - timedelta(days=operational_days)).isoformat()
                deleted["operational"] = self._delete_in_chunks(
                    conn,
                    "SELECT event_id FROM execution_events WHERE observed_at < ? "
                    f"AND {_KIND_EXPR} NOT IN ({mech_holes}) AND {capped_sql} LIMIT ?",
                    (cutoff, *mech, *capped_params),
                )
        return deleted

    # `prune_old_tool_calls` WAS HERE AND IS DELETED (2026-08-23). It was the
    # pre-registry age-pruner; the retention rewrite replaced it with
    # `prune_by_retention_class`, which `auto_prune` now calls directly. Nothing
    # called it afterwards -- vulture found it, and its own docstring still said
    # "Used by auto-prune", which had stopped being true.
    #
    # DELETED RATHER THAN ALLOWLISTED, deliberately: an allowlist entry for a
    # function with no consumer teaches the tool to stay quiet about exactly the
    # thing it exists to find. If a caller is needed again, call
    # `prune_by_retention_class` -- it honours each class's own horizon instead
    # of scaling three of them off one configured number.

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
        """Keep only the most recent ``max_events`` COUNT-CAPPED events.

        max_events <= 0 means UNLIMITED — no count-based pruning.

        Count-capped means MECHANICAL, OPERATIONAL, and anything nobody
        classified. DECISION and FORENSIC rows sit outside the cap in BOTH
        directions: it never deletes them, and they never consume its
        budget. The old filter counted only the three ``tool_call_*`` kinds,
        so 10 native_tool_use rows read as a total of 0 and nothing was ever
        trimmed.
        """
        if max_events <= 0:
            return 0
        self.init_db(project_root)
        capped_sql, capped_params, _, _ = self._retention_predicates()
        with self.connect(project_root) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM execution_events WHERE {capped_sql}",
                tuple(capped_params),
            ).fetchone()[0]
            if total <= max_events:
                return 0
            return self._delete_in_chunks(
                conn,
                "SELECT event_id FROM execution_events "
                f"WHERE {capped_sql} ORDER BY observed_at ASC LIMIT ?",
                tuple(capped_params),
                limit=total - max_events,
            )

    def auto_prune(self, project_root: Path) -> dict[str, object]:
        """Age + size retention across the WHOLE emitter set.

        TRIGGERS (2026-08-23): this used to be reachable from exactly one
        place — "called on dashboard load". The dashboard had been broken
        for weeks, so nothing pruned for 27 days and the ledger reached
        701.7 MB. It is now also scheduled by the thing that CAUSES the
        growth: ``record_event`` (see ``_schedule_retention``), on a
        background thread, rate-limited. The dashboard call stays; it is no
        longer the only way in.
        """
        from .config import get_setting
        from .execution_event_retention import DEFAULT_POLICY, RetentionClass

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
        # Per-class horizons. FORENSIC is deliberately absent: no setting can
        # turn retention into a shredder for security records.
        mechanical_days = _int(
            "execution.retention.mechanical_days",
            DEFAULT_POLICY[RetentionClass.MECHANICAL].keep_days,
        )
        decision_days = _int(
            "execution.retention.decision_days",
            DEFAULT_POLICY[RetentionClass.DECISION].keep_days,
        )
        pruned_by_size = (
            self.prune_to_max_size(project_root, max_events=max_events)
            if max_events > 0
            else 0
        )
        by_class = self.prune_by_retention_class(
            project_root,
            mechanical_days=mechanical_days,
            operational_days=auto_prune_days,
            decision_days=decision_days,
        )
        return {
            "pruned_by_size": pruned_by_size,
            "pruned_by_age": {"events_deleted": sum(by_class.values())},
            "pruned_by_class": by_class,
            "max_events": max_events,
            "auto_prune_days": auto_prune_days,
            "mechanical_days": mechanical_days,
            "decision_days": decision_days,
        }

    _RECONCILE_SCOPE = "project"

    def record_index_reconcile(
        self,
        project_root: Path,
        *,
        tracked: int,
        synced: int,
        state: str,
        trigger: str,
    ) -> None:
        """Upsert the ONE index-reconciler heartbeat row.

        Replaces the per-occurrence ``index_sitter_reconcile`` audit event
        (30,202 rows in 27 days, no consumer). Storage is O(1) and the
        observability question the rows actually answered -- "when did the
        index last reconcile, and when before that" -- is answered better,
        because it is now answered for EVERY reconcile including the no-op
        polls the old code had to suppress to survive its own volume.

        Best-effort: a heartbeat must never break a reconcile.
        """
        try:
            self.init_db(project_root)
            # Microsecond precision, unlike the second-granular audit
            # timestamp: this row's whole job is "when", and now that `last`
            # and `previous` are the only two moments kept, two reconciles
            # inside one second must stay distinguishable.
            now = datetime.now(UTC).isoformat()
            unhealthy = state not in ("ready", "empty")
            with self.connect(project_root) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO index_reconcile_state (
                        scope, last_reconcile_at, previous_reconcile_at,
                        reconcile_count, last_trigger, last_state,
                        last_synced_rows, last_tracked_rows,
                        last_unhealthy_at, last_unhealthy_state
                    ) VALUES (?, ?, '', 1, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope) DO UPDATE SET
                        previous_reconcile_at =
                            index_reconcile_state.last_reconcile_at,
                        last_reconcile_at = excluded.last_reconcile_at,
                        reconcile_count =
                            index_reconcile_state.reconcile_count + 1,
                        last_trigger = excluded.last_trigger,
                        last_state = excluded.last_state,
                        last_synced_rows = excluded.last_synced_rows,
                        last_tracked_rows = excluded.last_tracked_rows,
                        -- A healthy pass must not erase the fact that the
                        -- index HAD been broken.
                        last_unhealthy_at = CASE
                            WHEN excluded.last_unhealthy_at <> '' THEN
                                excluded.last_unhealthy_at
                            ELSE index_reconcile_state.last_unhealthy_at END,
                        last_unhealthy_state = CASE
                            WHEN excluded.last_unhealthy_at <> '' THEN
                                excluded.last_unhealthy_state
                            ELSE index_reconcile_state.last_unhealthy_state END
                    """,
                    (
                        self._RECONCILE_SCOPE,
                        now,
                        str(trigger or ""),
                        str(state or ""),
                        int(synced or 0),
                        int(tracked or 0),
                        now if unhealthy else "",
                        str(state or "") if unhealthy else "",
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — never breaks a reconcile
            try:
                sys.stderr.write(
                    f"[aidocs index-sitter] heartbeat not recorded: "
                    f"{type(exc).__name__}: {exc}\n",
                )
            except Exception:
                pass

    def index_reconcile_state(self, project_root: Path) -> dict[str, Any]:
        """Read the heartbeat. A project that never reconciled reads empty."""
        empty: dict[str, Any] = {
            "last_reconcile_at": "",
            "previous_reconcile_at": "",
            "reconcile_count": 0,
            "last_trigger": "",
            "last_state": "",
            "last_synced_rows": 0,
            "last_tracked_rows": 0,
            "last_unhealthy_at": "",
            "last_unhealthy_state": "",
        }
        try:
            self.init_db(project_root)
            with self.connect(project_root) as conn:
                row = conn.execute(
                    "SELECT last_reconcile_at, previous_reconcile_at, "
                    "reconcile_count, last_trigger, last_state, "
                    "last_synced_rows, last_tracked_rows, last_unhealthy_at, "
                    "last_unhealthy_state FROM index_reconcile_state "
                    "WHERE scope = ?",
                    (self._RECONCILE_SCOPE,),
                ).fetchone()
        except Exception:
            return empty
        if row is None:
            return empty
        return dict(zip(empty.keys(), row, strict=False))

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
