from __future__ import annotations

import sqlite3
import threading
import contextlib
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import apply_pragmas as _apply_pragmas
from ._sqlite_connect import connect_cache_clear as _chokepoint_cache_clear

# ── Request-scoped connection pool (#489) ───────────────────────────
#
# THREAD-LOCAL BY CONSTRUCTION. sqlite3 connections are bound to the thread that
# created them (check_same_thread), and the hook broker threads per connection —
# so a process-wide pool would hand thread B a handle it may not legally touch.
# Each thread gets its own depth counter and its own idle pool, and a scope
# opened on one thread is invisible to every other.
_scope_state = threading.local()

#: How long a store will wait for the single WAL writer before giving up (#748).
#: WAL admits exactly ONE writer, so this is not a retry hint — it is the ENTIRE
#: window, and the competitors are separate PROCESSES. This base shipped 2s while
#: the audit ledger that shares these files already allowed its own writes 10s
#: (execution_index_store._WRITE_BUSY_TIMEOUT_MS), so the authority stores were
#: guaranteed to lose a race against the ledger's prune: measured 2026-08-23 as
#: prompt-submit transactions failing "database is locked" on session_query_gate
#: and actor_task_state while retention ran. Both knobs move together — the pragma
#: is sqlite's wait, `timeout` is python's, and raising one alone does nothing.
_BUSY_TIMEOUT_MS = 10_000


class ProjectNotAdopted(RuntimeError):
    """Raised when a store is opened against a directory that is not an AIDOCS project.

    NOT an error condition to route around — it is the honest answer to a question
    the caller asked. A read that wants to know "is this managed?" should treat this
    as NO; a writer that genuinely means to adopt the directory must go through the
    explicit adopt path (`aidocs init` / `/aidocs`), which is the only thing allowed
    to create `.MEMORY`.
    """


def _require_adopted(project_root: Path) -> None:
    """`.MEMORY` must already exist. Opening a store never creates it.

    THE BUG (operator report 2026-07-28, reproduced + bisected): an empty folder plus
    a single Claude SessionStart gained `.MEMORY/.index/` holding aidocs.sqlite3 and
    aidocs_identity.sqlite3 — ~94 KB into a directory nobody adopted. Two creators,
    both fixed by this one rule: `connect()` below, and `agent_memory_epoch._init_db`.

    Adoption is a DECISION, not a side effect. `.MEMORY` is where that decision is
    recorded, so conjuring it turns "I looked at this folder" into "I own this
    folder". The sharpest consequence was that `get_mode()` — a READ — reached
    init_db and built the store that would answer it, so the question "is managed
    mode active?" could not return no without first making the answer yes-ish.

    Deliberately checks only the `.MEMORY` ROOT, not the
    `.MEMORY/.aidocs/index.aidocs` governance marker. Those are different questions:
    the marker decides whether the GATE governs (agent_orchestrator:1133-1145, which
    already refuses to trust a bare `.MEMORY/` for exactly that reason); this decides
    whether AIDOCS may WRITE here at all. A half-initialised project must still be
    writable — that is how `init` finishes its work and how `doctor` heals the
    "half-init needs a decision" state (cli.py:4195-4199) instead of being locked out
    of repairing it.
    """
    try:
        if (Path(project_root) / ".MEMORY").is_dir():
            return
    except OSError as exc:  # unreadable path: cannot claim it is adopted
        raise ProjectNotAdopted(f"cannot inspect {project_root}: {exc}") from exc
    # Callers that want the ANSWER rather than the exception: catch ProjectNotAdopted
    # and treat it as "not managed" (managed_mode_service._resolve_mode is the
    # reference implementation). Do not re-add a mkdir to make the exception go away.
    raise ProjectNotAdopted(
        f"{project_root} is not an AIDOCS project (no .MEMORY) — opening a store must "
        "not adopt it; run `/aidocs` or `aidocs init` to adopt it deliberately"
    )


from ._sqlite_connect_sampler import note_close as _note_close  # noqa: E402
from ._sqlite_connect_sampler import sample_connect as _sample_connect  # noqa: E402


class _ClosingConnection(sqlite3.Connection):
    """A connection whose ``with`` block CLOSES it, not merely commits it.

    THE BUG (operator handle dump 2026-08-04): two processes held ~2,400 live
    SQLite connections between them -- 1,026 triplets (db + -wal + -shm) on one
    aidocs.sqlite3 at 6.8 GB private in the MCP server, ~1,342 across six stores
    at 2.05 GB in the hook broker. Every one came from `with self.connect(root)
    as conn:`, because sqlite3's own context manager commits or rolls back the
    TRANSACTION and never touches the HANDLE.

    Refcounting is supposed to reap the handle when the frame dies, and does
    not: these connections sit in reference CYCLES, so only a gen-2 cyclic
    collection frees them and a busy long-lived server rarely runs one.
    MEASURED: 600 unclosed connects grew the process +1,802 OS handles (+3
    each) and held there until an explicit gc.collect(); 600 closed connects
    grew it by +0, and 3,000 grew it by +0 with no collection at all.

    Fixed at the ONE canonical connect (empire-doctrine XXII) rather than at the
    ~140 callsites, because the leak reaches this factory through receivers a
    callsite sweep cannot enumerate -- `store.connect`, `hub.code.connect`,
    `ex.connect`. A new caller written in the old shape is correct by default.

    Does NOT disturb the two paths that deliberately outlive a block:
    ``session()`` yields the raw connection and commits it by hand, and
    ``connection_scope()`` parks handles in its idle pool -- neither enters the
    connection as a context manager, so this __exit__ never runs for them.
    A bare ``connect()`` with no ``with`` likewise stays caller-owned.
    """

    # Set on handles that someone else owns -- see the BORROWED note below.
    _aidocs_borrowed = False

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        # Commit/rollback exactly as sqlite3 would, THEN close. Close lives in
        # `finally` so a commit that RAISES still releases the handle -- a
        # failing commit that also leaked would be the original bug with extra
        # steps.
        #
        # BORROWED HANDLES ARE NOT OURS TO CLOSE. `with conn:` means two
        # different things in this codebase and Python cannot tell them apart:
        #   OWNERSHIP  `with store.connect(root) as conn:`  -- acquire and use
        #   TRANSACTION `with conn:` on a handle a CALLER passed in
        # The second form is real (prompt_submit_store_snapshot.restore_scoped_rows
        # takes a caller's connection and opens a transaction on it). Closing
        # there yanked the handle out from under the caller mid-request; the
        # prompt-submit path then hit a closed connection and its fail-open
        # returned None, turning a REFUSAL into "carry on" -- caught by
        # tests/security/test_prompt_mutation_plan.py before it ever shipped.
        #
        # So the owner marks what it still owns: session() and the
        # connection_scope() pool set _aidocs_borrowed and close by hand.
        # Anything straight from connect() is unowned and closes here.
        if self._aidocs_borrowed:
            return bool(super().__exit__(exc_type, exc, tb))
        try:
            suppress = super().__exit__(exc_type, exc, tb)
        finally:
            _note_close(self)
            self.close()
        return bool(suppress)


class SQLiteIndexStoreBase:
    # ── Per-process memos for work that is NOT per-connection (#489) ──
    #
    # MEASURED: one UserPromptSubmit opens aidocs.sqlite3 234 times and THIS
    # connect() is the top callsite at 137 opens (scripts/ups_sqlite_histogram.py).
    # Each call paid a mkdir syscall plus FOUR pragma round trips = ~137 mkdirs
    # and ~548 pragmas per prompt, for one canonical connect.
    #
    # Only FILE-scoped work is memoized. The WAL memo itself now lives at the
    # ONE canonical chokepoint (_sqlite_connect, doctrine XXII) — the copy
    # this class kept was a rival definition of the same decision. Everything
    # per-CONNECTION still runs on EVERY connection — synchronous,
    # busy_timeout, journal_size_limit, and above all foreign_keys, which
    # SQLite defaults OFF and which would make every FK constraint in the
    # kingdom inert if skipped. Faster refusal is good; faster bypass is
    # treason.
    #
    # Class-level on purpose: the mkdir fact is about the FILESYSTEM, not a
    # store instance, and dozens of store instances open the same db per
    # prompt.
    _dirs_ensured: set[str] = set()

    @classmethod
    def connect_cache_clear(cls) -> None:
        """Forget the per-process memos (tests, or a caller that moved files).

        Correctness never depends on this: a dropped memo costs one extra mkdir
        and one extra pragma, nothing more. The WAL memo lives at the
        chokepoint since the pragma block migrated onto apply_pragmas —
        clearing here clears it there, so callers keep ONE reset button.
        """
        _chokepoint_cache_clear()
        cls._dirs_ensured.clear()

    def index_root(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index"

    def db_path(self, project_root: Path) -> Path:
        return self.index_root(project_root) / "aidocs.sqlite3"

    def connect(self, project_root: Path) -> sqlite3.Connection:
        db_path = self.db_path(project_root)
        parent = db_path.parent
        parent_key = str(parent)
        # OPENING A STORE IS NOT AN ADOPTION (operator bug report 2026-07-28).
        # This used to mkdir(parents=True) unconditionally, so the ONE canonical
        # connect materialised the ENTIRE .MEMORY tree from nothing — and because
        # every store rides this base, any code that merely LOOKED at a directory
        # adopted it. Measured: an empty folder plus one Claude SessionStart gained
        # .MEMORY/.index/ with two sqlite files (~94 KB). Worse, get_mode() — a READ
        # asking "is managed mode active?" — reached init_db and built the store that
        # would answer it, so the question could never return "no".
        #
        # `.MEMORY` is the adoption boundary: creating it is ADOPTION and belongs to
        # an explicit adopt path (`aidocs init` / `/aidocs`). Filling IN an already
        # adopted project is ordinary work. So: refuse when the root is absent,
        # create `.index` when it is present. The rule lives HERE, once, instead of
        # in every caller — a caller that has to remember a rule eventually forgets.
        _require_adopted(project_root)
        # Re-verify with a cheap is_dir() instead of trusting the memo blindly:
        # pytest tmp-dir cleanup (and any operator rm) can delete a directory
        # under a live process, and a stale "already ensured" would hand sqlite a
        # path whose parent no longer exists.
        if parent_key not in self._dirs_ensured or not parent.is_dir():
            parent.mkdir(parents=True, exist_ok=True)
            self._dirs_ensured.add(parent_key)
        # WAL + bounded busy-wait (2026-07-17 perf fix). The default rollback
        # journal makes a READER block on any active WRITER, and Python's default
        # sqlite3 busy timeout is 5s — so a UPS hook that opens this DB MANY times
        # (get_mode x11, #436) compounded to a ~30s prompt-submit stall while this
        # session wrote heavily. WAL lets readers and the single writer proceed
        # concurrently; timeout=2.0 bounds any residual contention instead of the
        # 5s default. Pure performance — no behavior change; all consumers benefit
        # (this is the ONE canonical connect, empire-doctrine XXII).
        # factory=_ClosingConnection: a `with self.connect(...)` block CLOSES the
        # handle instead of merely committing it (P0 leak 2026-08-04 — see the
        # class docstring). session() and the connection_scope() pool are
        # unaffected: neither enters the connection as a context manager, so
        # __exit__ never fires on those paths and pooled reuse still works.
        conn = sqlite3.connect(
            str(db_path), timeout=_BUSY_TIMEOUT_MS / 1000, factory=_ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        # Doctrine XXII — the pragma set is applied by the ONE chokepoint
        # (_sqlite_connect.apply_pragmas), not a hand-rolled copy. The block
        # that lived here (WAL memo + synchronous + busy_timeout + FK) was a
        # RIVAL DEFINITION of apply_pragmas, kept in sync by hand; #850 made
        # the cost concrete when a pragma added at the chokepoint
        # (journal_size_limit, the WAL disk-footprint bound) would NOT have
        # reached this db — the 711MB index where the 78MB runaway WAL
        # actually happened. This base keeps building its own connection (its
        # _ClosingConnection factory carries the pool/session borrowed-handle
        # semantics, and the adoption-boundary refusal above is its own rule),
        # which is exactly the case apply_pragmas exists for. Semantics are
        # unchanged: WAL stays memoised per file per process (#489) via
        # db_key, the performance pragmas stay fail-open, and foreign_keys
        # stays fail-closed.
        _apply_pragmas(
            conn,
            durability=_Durability.RUNTIME,
            busy_timeout_ms=_BUSY_TIMEOUT_MS,
            db_key=str(db_path),
        )
        _sample_connect(type(self).__name__, db_path)
        return conn

    # ── Request-scoped connection reuse (#489) ─────────────────────

    @classmethod
    @contextmanager
    def connection_scope(cls) -> Iterator[None]:
        """Reuse index-DB connections for the duration of this block.

        MEASURED: a warm UserPromptSubmit opened this DB ~116 times through
        session(), and the first file-touching statement on a fresh connection
        costs ~1.2ms — roughly 140ms per prompt spent purely opening a file that
        was already open a moment earlier.

        The bigger reason is SCALE, not latency: hundreds of opens per prompt
        contend on the same file, and with many agents running concurrently
        readers and writers start queueing on WAL locks. Fewer, longer-lived
        handles per request cut lock churn, which is what keeps fan-out from
        degrading into a crawl.

        OPT-IN, and bounded. Outside a scope, session() behaves exactly as it
        always did (open, commit, close) — because an open handle holds a WAL
        lock that blocks other processes and pytest's tmp cleanup on Windows, so
        idle connections must never outlive a request. Nesting is reference
        counted; only the OUTERMOST exit closes.
        """
        depth = int(getattr(_scope_state, "depth", 0))
        if depth == 0:
            _scope_state.idle = {}
        _scope_state.depth = depth + 1
        try:
            yield
        finally:
            _scope_state.depth = int(getattr(_scope_state, "depth", 1)) - 1
            if _scope_state.depth <= 0:
                _scope_state.depth = 0
                for pooled in list(getattr(_scope_state, "idle", {}).values()):
                    for conn in pooled:
                        try:
                            conn.close()
                        except Exception:  # noqa: BLE001,S110 — close is best-effort
                            pass
                _scope_state.idle = {}

    @classmethod
    def _scope_pool_size(cls) -> int:
        """Idle pooled connections on THIS thread (tests/diagnostics)."""
        return sum(len(v) for v in getattr(_scope_state, "idle", {}).values())

    @contextmanager
    def session(self, project_root: Path) -> Iterator[sqlite3.Connection]:
        """Context manager that commits, and closes unless a scope owns it.

        Python's sqlite3 ``with conn:`` block commits or rolls back but
        never closes the handle. On Windows the open handle keeps a WAL
        lock that blocks other processes (and pytest's tmp-dir cleanup)
        until Python GCs the connection. Every store that uses this
        method is safe under parallel test runs.

        Inside a connection_scope() the handle is CHECKED OUT of a per-thread
        idle pool and returned on success instead of being closed. Checkout —
        not sharing — is what preserves transaction isolation: session() blocks
        NEST (init_db's session calls _ingest_all_legacy_json, which opens its
        own), and a shared handle would let the inner commit flush the outer
        block's half-written work. An overlapping block simply gets its own
        connection, exactly as before.

        A block that RAISES closes its connection and never returns it: a
        rolled-back handle must not be handed to the next caller.
        """
        scoped = int(getattr(_scope_state, "depth", 0)) > 0
        db_key = str(self.db_path(project_root))
        conn: sqlite3.Connection | None = None
        if scoped:
            pooled = getattr(_scope_state, "idle", {}).get(db_key) or []
            if pooled:
                conn = pooled.pop()
        if conn is None:
            conn = self.connect(project_root)
        # This block OWNS the handle and closes it below (or parks it in the
        # pool). Mark it borrowed so a nested `with conn:` transaction inside
        # the block commits without closing it out from under us.
        with contextlib.suppress(AttributeError):
            conn._aidocs_borrowed = True  # noqa: SLF001 — our own subclass
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001,S110 — close is best-effort
                    pass
            raise
        # Success: hand the connection back for reuse, or close it when no scope
        # owns it. Every block ends committed or closed, so a pooled connection
        # never carries another block's uncommitted work.
        if scoped and int(getattr(_scope_state, "depth", 0)) > 0:
            getattr(_scope_state, "idle", {}).setdefault(db_key, []).append(conn)
        else:
            conn.close()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _timestamp(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
