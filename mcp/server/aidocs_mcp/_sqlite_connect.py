"""THE ONE CANONICAL SQLITE CONNECT (#755, empire-doctrine XXII).

WHY THIS FILE EXISTS
--------------------
Census 2026-08-04, over ``server/aidocs_mcp/**.py``::

    files calling sqlite3.connect               : 100
    of those that set PRAGMA synchronous        :   3
    running at sqlite's DEFAULT synchronous=FULL:  97

Ninety-seven stores — including the entire governed hot path (execution_index,
tool_call_log, identity, config, host_concurrency, the outer_gate_* RBAC family)
— opened sqlite with NO pragmas at all. Three did it right.

That is not ninety-seven bugs. It is ONE bug: there was no chokepoint, so every
correct decision had to be re-made at every call site, and a decision that must
be re-made is a decision that rots. #746 proved it — its journal_mode fix was
applied store-by-store and five stores were still on rollback journal afterwards.
Fixing the pragmas without fixing the chokepoint guarantees a third pass over the
same ground.

The reasoning below is not new. It is the reasoning already written at
``_sqlite_index_store_base.py`` and never made reachable from anywhere else;
this module is that decision hoisted to where the other stores can reach it.

WHAT EACH PRAGMA IS FOR — and none is cosmetic
----------------------------------------------
``journal_mode = WAL``
    The default rollback journal makes a READER block on any active WRITER.
    Persisted on the DB FILE, so it is issued ONCE per file per process; the
    memo below exists because re-issuing it per connection was measured as pure
    waste (~137 redundant connects per prompt, #489).

``synchronous``
    THE PERFORMANCE FIX, and the reason this module was written today. Measured
    on this disk, 20 separate commits (the governed hook path's real shape)::

        synchronous=FULL   : 83.4 ms / 80.8 ms
        synchronous=NORMAL : 10.3 ms /  7.8 ms      -> 8-10x

    In WAL mode NORMAL CANNOT CORRUPT THE DATABASE — that is not a trade being
    made here, and anyone reading this later should not "harden" it back on the
    belief that it is. What NORMAL trades is narrow and specific:

        process death, taskkill, pm2 restart, SIGKILL  -> NOTHING IS LOST
        kernel panic / power cut                       -> commits since the last
                                                          checkpoint may be lost

    Committed data lives in the WAL and the OS page cache; the cache outlives the
    process. Only the MACHINE going down loses anything.

    OPERATOR RULING, 2026-08-04, recorded so it is not silently re-litigated:
    "yes, acceptable (if only power loss and not on a forced stop by user)".
    That is precisely the guarantee above, which is why NORMAL is the default
    here — and precisely why the level is a PARAMETER and not a constant. An
    audit trail that is meant to be EVIDENCE may not have a hole in it after a
    power cut, so audit-bearing stores pass ``Durability.AUDIT`` and keep FULL.
    They are a small share of the ~20 commits per tool call, so the guarantee is
    cheap to keep exactly where it is worth keeping.

``busy_timeout``
    Bounds contention instead of Python's 5s default. A bounded wait that
    refuses is better than an unbounded one that hangs.

``foreign_keys = ON``
    NOT performance — CORRECTNESS, and the quiet find of this census. SQLite
    defaults foreign_keys OFF *per connection*, so all ninety-seven stores that
    never issued it have had every FK constraint they declared sitting INERT:
    cascade-deletes not cascading, reference checks not checking. This is law
    183074ae in its purest form — a constraint with no enforcement is not a
    constraint. It is applied unconditionally here and is never optional.

THE RULE
--------
New code MUST come through ``connect()``. ``tests/runtime/test_sqlite_connect_
chokepoint.py`` FAILS on any new ``sqlite3.connect(`` outside this module, with
an explicit allowlist for the stores not yet migrated. Without that test this
rots back exactly the way #746 did: absence is a bug, not a skip (doctrine XXXI).
"""

from __future__ import annotations

import sqlite3
from enum import Enum
from pathlib import Path

__all__ = ["Durability", "connect", "apply_pragmas", "connect_cache_clear"]


class Durability(str, Enum):
    """How much fsync a store's contents are worth.

    Deliberately two values, not a free-form string: the question "can this
    store afford to lose the last commits to a POWER CUT" has exactly two
    answers, and offering more invites a caller to invent a third.
    """

    #: Advisory, telemetry, reconstructible or ephemeral runtime state —
    #: execution_index, tool_call_log, hook events, config reads, concurrency.
    #: Loses nothing on process death; may lose recent commits on power loss.
    RUNTIME = "NORMAL"

    #: Evidence. agent_audit, empire_audit, and anything whose ABSENCE after a
    #: crash would itself be a finding. Pays the fsync on every commit.
    AUDIT = "FULL"


# journal_mode is persisted on the FILE, so it is established once per file per
# process. Module-level on purpose: this is a fact about the FILE and the
# FILESYSTEM, not about any store instance, and dozens of instances open the
# same db per prompt (#489).
_wal_established: set[str] = set()

#: #850 clause 2 — bound the WAL's DISK FOOTPRINT. SQLite's default
#: journal_size_limit is -1: a WAL file NEVER shrinks, it is only rewound.
#: Measured 2026-08-21: one long-writer/long-reader episode grew
#: aidocs.sqlite3-wal to 78MB and it would have stayed that size forever —
#: and a giant WAL is the seed of the next multi-minute checkpoint fold,
#: which is exactly the write-lock hold that starves the audit writer's
#: 40s budget and bricks the gate (#850).
#:
#: 16MB = 4x the ~4MB default wal_autocheckpoint ceiling (1000 pages), so
#: ordinary operation NEVER pays a truncate — the limit only acts at the
#: first checkpoint after a spike, giving the space back instead of keeping
#: it. It bounds the FILE, not the fold: a long reader or writer can still
#: let frames pile up; what it removes is the permanent oversized WAL that
#: makes every later fold monstrous. Per-CONNECTION and enforced by
#: whichever connection runs the reclaiming checkpoint — the reason it
#: lives here at the chokepoint and not in any one store.
_JOURNAL_SIZE_LIMIT_BYTES = 16 * 1024 * 1024


def connect_cache_clear() -> None:
    """Forget the per-process WAL memo (tests, or a caller that moved files).

    Correctness never depends on this: a dropped memo costs one extra pragma.
    """
    _wal_established.clear()


def apply_pragmas(
    conn: sqlite3.Connection,
    *,
    durability: Durability = Durability.RUNTIME,
    busy_timeout_ms: int = 2000,
    db_key: str | None = None,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Apply the runtime pragma set to an already-open connection.

    Split out from ``connect`` so stores that must build their own connection
    (a custom factory, an in-memory db, an existing handle) still have ONE way
    to get the pragmas right. A store that cannot use ``connect`` is expected to
    call this — not to hand-roll the pragmas again.

    ``read_only`` — APPLY WHAT IS APPLICABLE, FAIL ON WHAT IS NOT (#755,
    2026-08-18). ``journal_mode`` is the one pragma in this set that WRITES: it
    is persisted in the database FILE HEADER, so a connection opened
    ``mode=ro``/``immutable=1`` cannot set it and SQLite says so. That is not a
    reason for such a store to keep opening sqlite raw — the OTHER three pragmas
    (synchronous, busy_timeout, foreign_keys) are per-CONNECTION and apply
    perfectly well to a reader. So journal_mode is skipped when the caller
    declares the connection read-only, and — independently — it now sits in its
    OWN try/except: before this it shared one with synchronous and busy_timeout,
    so ANY journal_mode failure silently skipped the other two. A guard that
    drops the pragmas it CAN set because one it cannot set failed is the same
    "reports green while doing nothing" shape the ratchet's own comment warns
    about.
    """
    if not read_only:
        try:
            # WAL is persisted on the file; the others are per-CONNECTION and
            # always run. Skipping a per-connection pragma is not a saving, it
            # is a bypass.
            if db_key is not None:
                if db_key not in _wal_established:
                    conn.execute("PRAGMA journal_mode = WAL")
                    _wal_established.add(db_key)
            else:
                conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            # A locked/again pragma must NEVER break the connection (fail-open
            # on PERFORMANCE only — see below for the one that may not fail
            # open). Drop the memo so the next connect retries establishing WAL
            # rather than assuming a failed attempt succeeded.
            if db_key is not None:
                _wal_established.discard(db_key)
    try:
        conn.execute(f"PRAGMA synchronous = {durability.value}")
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        # Performance class like the two above (fail-open): a WAL that is
        # merely unbounded is degraded, not incorrect.
        conn.execute(f"PRAGMA journal_size_limit = {_JOURNAL_SIZE_LIMIT_BYTES}")
    except sqlite3.DatabaseError:
        pass
    # DELIBERATELY OUTSIDE THE except: foreign_keys is correctness, not
    # performance. If it cannot be set, the caller gets the DatabaseError rather
    # than a connection that silently enforces no constraints. Faster refusal is
    # good; faster bypass is treason.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# (db_path, marker) -> the (st_dev, st_ino) of the file the claim was made
# about. Keyed by path, VALIDATED by identity: see schema_already_ensured.
_schema_ensured: dict[tuple[str, str], tuple[int, int]] = {}


def _file_identity(db_path: str | Path) -> tuple[int, int] | None:
    """(device, inode) — the identity of the file AT this path, or None.

    Identity, not existence: (st_dev, st_ino) survives every ordinary write to
    the database (which is the point -- the memo must stay hot) but CHANGES the
    moment a different file takes the path's place. os.stat gives a usable
    st_ino on Windows too (the NTFS file index), so this is not a POSIX-only
    guard.
    """
    try:
        st = Path(db_path).stat()
    except OSError:  # missing, or unreadable: re-run rather than assume
        return None
    return (st.st_dev, st.st_ino)


def schema_already_ensured(db_path: str | Path, marker: str = "") -> bool:
    """Has THIS process already created ``marker``'s schema in THIS file?

    MEASURED 2026-08-06: one ClaudeHookHandler.handle() ran init_db for EVERY
    store -- session_freeze, rbac, task_lifecycle, escalation, project_commission,
    host_operator_binding, empire_soul_gate, ... each ~one open per event. A
    schema creation is idempotent and FILE-scoped, so re-issuing `CREATE TABLE
    IF NOT EXISTS` on every hook event is pure waste: it costs an open, a
    transaction and a write lock to discover there was nothing to do. This is
    the same shape as the 44x adopt_legacy_project_identity call already fixed
    -- a WRITE-shaped setup step re-run as if it were a read.

    IDENTITY, NOT EXISTENCE (2026-08-12, after Gate 2b). This first shipped
    checking only `Path(db_path).is_file()`, to catch a DB deleted under a live
    process. That guard cannot survive this codebase's own layout: EVERY store
    rides SQLiteIndexStoreBase and they all share ONE file --
    ``<project>/.MEMORY/.index/aidocs.sqlite3``. So when that file is deleted
    (ai_delete, operator rm, tmp cleanup, a restore), the next store to touch
    the project RECREATES it carrying only ITS OWN tables. A file exists at the
    path again, `is_file()` says yes, and init_db returns early FOREVER for
    every other store's marker. Gate 2b 2026-08-12 measured the result: 39
    `no such table` failures in one run (24 aidocs_managed_per_conductor, 15
    aidocs_managed, plus project_commission and session_freeze).

    Correctness never depends on the memo -- a dropped entry costs one
    redundant CREATE TABLE IF NOT EXISTS, nothing more. So every uncertain case
    (missing file, unreadable stat, replaced file) answers False.
    """
    key = (str(Path(db_path)), marker)
    known = _schema_ensured.get(key)
    if known is None:
        return False
    if known == _file_identity(db_path):
        return True
    # A DIFFERENT file now holds this path -- the recorded schema went with the
    # old one.
    _schema_ensured.pop(key, None)
    return False


def mark_schema_ensured(db_path: str | Path, marker: str = "") -> None:
    """Record that ``marker``'s schema exists in the file NOW at ``db_path``."""
    identity = _file_identity(db_path)
    if identity is None:
        # No file to bind the claim to (schema creation raced a delete): record
        # nothing rather than a claim that can never be checked.
        return
    _schema_ensured[(str(Path(db_path)), marker)] = identity


def schema_memo_clear() -> None:
    """Forget every schema memo (tests, or a caller that moved files)."""
    _schema_ensured.clear()


class ClosingConnection(sqlite3.Connection):
    """A connection whose ``with`` block CLOSES it, not merely commits it.

    MEASURED 2026-08-06, one ClaudeHookHandler.handle() of a single prompt:
    ~78 connections opened, 21 explicitly closed. The other ~57 were left for
    the garbage collector -- and they sit in reference CYCLES, so only a gen-2
    collection frees them. A short probe looks fine because collections keep
    up; the hook broker under sustained load reached 4,897 OS handles and
    1.26 GB, because they do not.

    sqlite3's own ``with conn:`` commits or rolls back the TRANSACTION and
    never touches the HANDLE. That is the whole defect, and it is fixed HERE,
    at the one canonical connect (#755, empire-doctrine XXII), because the
    callers reach this factory through receivers no callsite sweep can
    enumerate.

    BORROWED HANDLES ARE NOT OURS TO CLOSE. ``with conn:`` means two different
    things in this codebase and Python cannot tell them apart:
        OWNERSHIP    ``with connect(path) as conn:``      -- acquire and use
        TRANSACTION  ``with conn:`` on a CALLER's handle  -- commit a unit
    The second form is real: prompt_submit_store_snapshot.restore_scoped_rows
    takes a caller's connection and opens a transaction on it. Closing there
    yanked the handle out from under the caller mid-request, and the
    prompt-submit fail-open turned the resulting error into None -- a REFUSAL
    silently degraded to "carry on", caught by
    tests/security/test_prompt_mutation_plan.py before it shipped.

    So owners mark what they own: a helper that manages the handle itself sets
    ``_aidocs_borrowed`` and closes by hand. Anything straight from connect()
    is unowned and closes at the end of its ``with``.
    """

    _aidocs_borrowed = False

    def close(self):  # noqa: ANN201
        # Report to the connect ledger BEFORE closing, so open/close pair up.
        # Without this the diagnostic undercounts closes and a genuine fix
        # reads as no change -- which is exactly what happened on the first
        # measurement after this class landed.
        from ._sqlite_connect_sampler import note_close  # noqa: PLC0415

        note_close(self)
        return super().close()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if self._aidocs_borrowed:
            return bool(super().__exit__(exc_type, exc, tb))
        # Commit/rollback exactly as sqlite3 would, THEN close. Close lives in
        # `finally` so a commit that RAISES still releases the handle -- a
        # failing commit that also leaked would be the original bug with extra
        # steps.
        try:
            suppress = super().__exit__(exc_type, exc, tb)
        finally:
            self.close()
        return bool(suppress)


def connect(
    db_path: str | Path,
    *,
    durability: Durability = Durability.RUNTIME,
    timeout: float = 2.0,
    busy_timeout_ms: int = 2000,
    row_factory: bool = True,
    uri: bool = False,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open ``db_path`` with the runtime pragma set applied.

    NOTE ON DIRECTORIES — this does NOT mkdir. ``_sqlite_index_store_base``
    deliberately refuses to materialise a tree because "opening a store is not
    an adoption" (operator bug report 2026-07-28: merely LOOKING at a directory
    was adopting it). That boundary belongs to the caller that knows whether it
    is entitled to create the path, so this helper never creates one.

    READ-ONLY OPENS (#755, 2026-08-18) — ``read_only=True`` is the reason three
    stores could not migrate. escalation_store, identity_store and
    session_freeze_store each kept ONE raw call,
    ``sqlite3.connect(f"file:{p}?mode=ro", uri=True)``, purely because this
    helper had no way to express a URI. Their handles already close correctly,
    so that was PRAGMA debt, not lifecycle debt — and pragma debt is exactly
    what this module exists to retire.

    Read-only is a FLAG, not a URI the caller hand-builds, for the same reason
    the pragmas are not: a string every call site re-assembles is a decision
    re-made at every call site. ``file:`` URI assembly has real edges (Windows
    separators, and ``?``/``#`` in a path, which SQLite parses as URI syntax);
    owning it here means owning them once.

    ``uri=True`` stays available as the primitive for a caller that genuinely
    needs a URI this helper does not model (``immutable=1``, ``cache=shared``,
    ``vfs=``). Then ``db_path`` is passed to SQLite verbatim, and a caller
    asking for a non-writable one must say ``read_only=True`` too — the helper
    does not parse a hand-written URI to guess what it means.
    """
    if uri:
        # A verbatim URI is not a filesystem path: Path() would mangle it, and
        # it is not this helper's to normalise.
        target = str(db_path)
        memo_key = target
    elif read_only:
        path = Path(db_path)
        # as_posix() keeps Windows separators out of the URI, where a backslash
        # is not a separator at all. The query separator is appended by US, so a
        # '?' or '#' already IN the path would be read as URI syntax: percent-
        # encode those two rather than hand SQLite a different file ('%' must go
        # first or it would double-encode the escapes we just wrote).
        raw = path.as_posix()
        for ch, esc in (("%", "%25"), ("?", "%3f"), ("#", "%23")):
            raw = raw.replace(ch, esc)
        target = f"file:{raw}?mode=ro"
        memo_key = str(path)
    else:
        path = Path(db_path)
        target = str(path)
        memo_key = target
    conn = sqlite3.connect(
        target, timeout=timeout, uri=uri or read_only, factory=ClosingConnection
    )
    if row_factory:
        conn.row_factory = sqlite3.Row
    return apply_pragmas(
        conn,
        durability=durability,
        busy_timeout_ms=busy_timeout_ms,
        db_key=memo_key,
        read_only=read_only,
    )
