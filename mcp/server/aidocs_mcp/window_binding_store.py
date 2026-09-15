"""``window -> conversation``, recorded at SessionStart (#876 phase 1).

WHAT THIS IS. One upserted row per HOST WINDOW saying which conversation that
window most recently declared, plus the one it displaced. Nothing more.

WHY IT EXISTS. Every identity channel AIDOCS had names a CONVERSATION, and a
conversation rotates: measured 2026-08-23, ``/resume`` rotated it, ``/clear``
rotated it again, and ``/mcp`` respawned the shim onto a third value, producing
four distinct host ids in one call while the WINDOW was unchanged throughout.
SessionStart is the one moment the host STATES the current conversation in a
payload it writes fresh every firing — so it is the one moment at which "which
conversation does this window hold" can be recorded from evidence instead of
inferred from a cache that has already gone stale.

STATE, NOT A LEDGER — modelled on ``index_reconcile_state`` (commit 80e8c3b01).
One row per window, upserted forever, O(1) storage. It is deliberately NOT an
append-only ledger and deliberately NOT in ``execution_events``: that table is a
hash-chained audit trail, upserting in place would break the chain it exists to
protect, and "which conversation does this window hold" is a CURRENT FACT, not a
decision worth forensics. The chain (#464) is what an append-only identity
history looks like, and #880 measured what it cost: a cap-16 FIFO that evicted
live windows, no format validation, and a synthetic test id seated permanently
in an authority structure.

PHASE 1 IS ADDITIVE AND THIS IS THE LOAD-BEARING SENTENCE: **nothing reads these
rows to make a decision.** No resolution path, no gate, no authority check. #880
turns this into a LEASE — one live conversation per window, unbound by a watcher
when the window dies — and that step carries the lockout risk #880 items 3 and 4
spell out. Writing the row first means that step gets to start from measurement.

NO ROW FROM AN UNRESOLVED WINDOW. Operator law 2026-08-23: "fallbacks can stamp
wrong data and we cannot tell from where. identity has no fallback." A row keyed
on an empty or synthesised window is WORSE than a missing row, because a later
reader cannot tell it apart from a real one. A blank key writes nothing; so does
a blank conversation, because a mapping with nothing on its right-hand side is
not a mapping.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase

# ONE DEFINITION OF WHAT A WINDOW KEY LOOKS LIKE, owned by the module that
# MINTS one. Imported rather than re-declared: a private copy here and the
# resolver's copy in `window_lease` would drift an edit at a time, and the
# drift only shows up as a key this writer accepted that the resolver refuses
# — a row that exists and can never be used.
from .window_key import WINDOW_KEY_SHAPE

#: One name, used by the schema, the taxonomy registration and the tests.
WINDOW_CONVERSATION_TABLE = "window_conversation_state"


def _window_process_is_alive(pid: int, created_filetime: int) -> bool | None:
    """Is the process that minted this window key still running? T/F/None.

    BOTH HALVES OR NOTHING (#880 item 1). A live pid alone is not the same
    window: Windows recycles pids, and a recycled pid whose creation time is
    unchecked would let a NEW process inherit a dead window's lease — its
    conversation, and since #892, its authority. That is the one-way door.

    ``None`` wherever the answer cannot be established — a non-win32 host, an
    unreadable creation time, a nonsense pair. The caller must not read it as
    death: this feeds a DELETE.

    `_pid_is_alive` is imported rather than re-implemented on purpose. It
    carries a documented Windows scar (2026-05-13): `os.kill(pid, 0)` there can
    leave a handle in a state that crashes the enclosing process's next stdio
    read, so it uses OpenProcess + GetExitCodeProcess. A second copy would be a
    second chance to get that wrong.
    """
    if pid <= 0 or created_filetime <= 0:
        return None

    from .aidocs_managed_store import _pid_is_alive

    try:
        if not _pid_is_alive(pid):
            return False  # the process is gone: positive proof
    except Exception:  # noqa: BLE001 -- a raise proves nothing
        return None

    if sys.platform != "win32":
        # The pid lives, but this host has no measured creation-time reader, so
        # "same process?" is unanswerable. UNPROVABLE, never death.
        return None

    from .window_key import _win32_creation_filetime

    try:
        current = _win32_creation_filetime(pid)
    except Exception:  # noqa: BLE001
        return None
    if not current:
        return None  # could not read it — not evidence of anything
    return True if int(current) == int(created_filetime) else False


class WindowBindingStore(SQLiteIndexStoreBase):
    """The window -> conversation state table. Written at SessionStart only."""

    _initialised: set[str] = set()

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                f"""
                -- #876 phase 1. ONE row per window, upserted forever.
                --
                -- window_key is `<claude.exe pid>:<its creation filetime>`.
                -- BOTH COMPONENTS, ALWAYS: Windows recycles pids, and a bare
                -- pid would let a NEW process inherit a DEAD window's row --
                -- and, once #880 makes this a lease, its conversation and its
                -- authority. The composite is derived in `window_key.py`; this
                -- table only stores it, and stores the two halves separately
                -- as well so a watcher can check liveness without re-parsing.
                --
                -- previous_host_session_id exists because #880's watcher must
                -- UNBIND what the window held before. It is updated only when
                -- the conversation actually CHANGES: SessionStart fires more
                -- than once per conversation (startup, resume, compact), and an
                -- unconditional overwrite would set previous == current on the
                -- second firing and destroy the only record of what was
                -- displaced. Same rule `index_reconcile_state` encodes for its
                -- unhealthy pair.
                --
                -- `source` is not decoration: operator law is that every value
                -- names where it came from, so a later reader never has to ask
                -- "we cannot tell from where".
                CREATE TABLE IF NOT EXISTS {WINDOW_CONVERSATION_TABLE} (
                    window_key TEXT PRIMARY KEY,
                    host_session_id TEXT NOT NULL DEFAULT '',
                    previous_host_session_id TEXT NOT NULL DEFAULT '',
                    host_kind TEXT NOT NULL DEFAULT '',
                    host_pid INTEGER NOT NULL DEFAULT 0,
                    host_created_filetime INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    bind_count INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',

                    -- ── THE WHO STAMP (operator ruling 2026-09-04) ──────────
                    --
                    -- WHO IS ESTABLISHED WHEN THE WINDOW COMES INTO EXISTENCE,
                    -- not discovered later by looking up whatever id a call
                    -- happens to carry. That lookup step IS the measured
                    -- defect: a conversation id rotated three times in one day
                    -- and each rotation orphaned a signed-in super_admin's
                    -- authority, because the binding was filed under a CALLER
                    -- fact and read as an IDENTITY key.
                    --
                    -- bound_via NAMES THE CREATION PATH that could name a user
                    -- ('machine_login', 'oauth_web', 'dashboard_command', ...)
                    -- so a later reader never has to ask "we cannot tell from
                    -- where". A creation with NO nameable principal stamps
                    -- NOTHING -- defaulting it to the machine login would
                    -- silently make every remote window the local operator.
                    --
                    -- WRITE ONCE. The upsert below sets these ONLY while they
                    -- are empty. A re-fired SessionStart (resume / clear /
                    -- compact) carries no principal, and an unconditional
                    -- overwrite would erase the stamp on the very rotation
                    -- this column exists to survive.
                    bound_user_id TEXT NOT NULL DEFAULT '',
                    bound_via TEXT NOT NULL DEFAULT '',
                    bound_at TEXT NOT NULL DEFAULT ''
                );

                -- #880 asks the reverse question ("which window holds this
                -- conversation") when it enforces one window per conversation.
                -- The index is schema, not a reader: nothing consults it yet.
                CREATE INDEX IF NOT EXISTS idx_window_conversation_session
                    ON {WINDOW_CONVERSATION_TABLE}(host_session_id);
                """,
            )
            # CREATE TABLE IF NOT EXISTS does nothing to a table that already
            # exists, so a box that ran an earlier build has the row shape from
            # that build. The WHO stamp is added here rather than by a rebuild:
            # the rows ARE the window identities, and dropping them would
            # unbind every live window on the box.
            self._add_missing_columns(conn)
        self._initialised.add(str(self.db_path(project_root)))

    #: Columns added after the table's first shipped shape, with their DDL.
    #: Every one is NOT NULL DEFAULT '' so an ALTER on a populated table
    #: cannot fail and cannot leave a NULL a reader must special-case.
    _ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
        ("bound_user_id", "TEXT NOT NULL DEFAULT ''"),
        ("bound_via", "TEXT NOT NULL DEFAULT ''"),
        ("bound_at", "TEXT NOT NULL DEFAULT ''"),
    )

    def _add_missing_columns(self, conn: Any) -> None:
        """Add any post-v1 column this DB lacks. Idempotent, never destructive."""
        try:
            have = {
                str(r[1])
                for r in conn.execute(
                    f"PRAGMA table_info({WINDOW_CONVERSATION_TABLE})",
                ).fetchall()
            }
        except Exception:  # noqa: BLE001 -- an unreadable schema is not a crash
            return
        for name, ddl in self._ADDED_COLUMNS:
            if name in have:
                continue
            try:
                conn.execute(
                    f"ALTER TABLE {WINDOW_CONVERSATION_TABLE} ADD COLUMN {name} {ddl}",
                )
            except Exception:  # noqa: BLE001 -- a racing migration already won
                pass

    def _run(self, project_root: Path, work: Callable[[Any], Any]) -> Any:
        """Run ``work(conn)`` with the table guaranteed to exist.

        The path memo is not trusted blindly, for the same reason the base class
        re-checks ``parent.is_dir()``: a db file can be recreated under a live
        process (pytest isolation does exactly this), leaving a memo that claims
        a table which is gone.
        """
        key = str(self.db_path(project_root))
        if key not in self._initialised:
            self.init_db(project_root)
        try:
            with self.session(project_root) as conn:
                return work(conn)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            self._initialised.discard(key)
            self.init_db(project_root)
            with self.session(project_root) as conn:
                return work(conn)

    def record_window_conversation(
        self,
        project_root: Path,
        *,
        window_key: str | None,
        host_session_id: str | None,
        host_kind: str = "",
        source: str = "session_start",
        bound_user_id: str = "",
        bound_via: str = "",
        is_alive=None,
    ) -> dict[str, Any] | None:
        """Upsert ``window -> conversation``. Returns None when it wrote nothing.

        REFUSES, rather than writing a placeholder, when either side is missing.
        A refusal LEAVES ANY EXISTING ROW UNTOUCHED — a blank overwrite would
        silently destroy a real binding, which is the destructive shape #880
        item 2 warns about arriving through a different door.
        """
        window = str(window_key or "").strip()
        conversation = str(host_session_id or "").strip()
        if not window or not conversation:
            return None

        # A KEY THAT IS NOT A KEY IS NOT STORED. #880 lists among the chain's
        # measured defects "append-only with NO FORMAT VALIDATION — which is
        # how auth-truth-614, a synthetic test id, is seated permanently in an
        # authority structure." The same omission was here: a non-blank check
        # and nothing else, which accepted and PERSISTED `auth-truth-614`,
        # `16716:None` (what the walk mints when the creation-time guard is
        # removed), `16716` (a BARE PID — the recycling hazard the composite
        # exists to remove) and `"  ..  "`.
        #
        # THE WRITE SIDE AS WELL AS THE READ SIDE, ON PURPOSE. `window_lease`
        # already refuses a malformed key when it RESOLVES one. That protects
        # the reader and not the TABLE: a seated junk row still occupies a
        # PRIMARY KEY, is still counted and migrated and reported by a
        # dashboard, and still cannot have its provenance explained by whoever
        # finds it later — which is the "we cannot tell from where" defect
        # under a different name.
        #
        # The refusal LEAVES ANY EXISTING ROW UNTOUCHED, exactly like the blank
        # check above it: nothing is written, so nothing is displaced.
        if not WINDOW_KEY_SHAPE.fullmatch(window):
            return None

        # The two halves, stored separately so a liveness check does not have to
        # re-parse the key. Unparseable halves are recorded as 0 rather than
        # guessed; the composite key remains the identity either way.
        pid_part, _, created_part = window.partition(":")
        try:
            host_pid = int(pid_part)
        except (TypeError, ValueError):
            host_pid = 0
        try:
            host_created = int(created_part)
        except (TypeError, ValueError):
            host_created = 0

        now = datetime.now(UTC).isoformat()

        # THE WHO STAMP, normalised. An empty principal is an HONEST EMPTY and
        # is written as one: the upsert's write-once CASE leaves any existing
        # stamp alone, so a re-fired SessionStart that knows nobody cannot
        # blank the identity established when this window was created.
        who = str(bound_user_id or "").strip()
        who_via = str(bound_via or "").strip()
        # A user id without a stated creation path is refused rather than
        # stored: "we cannot tell from where" is the exact defect this column
        # was added to end, and a stamp nobody can attribute is worse than none.
        if not who or not who_via:
            who, who_via = "", ""

        # ── INCUMBENT WINS (#919, operator ruling 2026-08-25) ──────────────
        #
        # A conversation already held by a PROVABLY LIVE other window is NOT
        # transferred. Nothing is written and nothing is displaced.
        #
        # WHAT HAPPENED. `claude --continue` respawned a copy of a running
        # conversation in a SECOND window. Its SessionStart claimed the same
        # conversation id, the one-conversation-one-window release cleared the
        # ORIGINAL window's claim, and that copy then closed and was reaped --
        # leaving the conversation held by NOBODY while the window still running
        # it was refused its own identity. Measured on the operator's box:
        # window 29520 alive (pid AND creation filetime confirmed), claim blank,
        # previous_host_session_id = the conversation it was still running.
        #
        # WHY THE INCUMBENT AND NOT THE NEWCOMER. Two live processes claiming one
        # identity is genuinely ambiguous and somebody must lose; the only
        # question is whether the rule is principled. "Whoever bound last" is not
        # -- the lease-unbind commit called that shape "#859's guess wearing a
        # new hat" while rejecting it as a tiebreak. First-holder-wins-while-live
        # is deterministic, and it favours the window that is actually doing the
        # work over a copy of it.
        #
        # THIS PRESERVES THE INVARIANT rather than weakening it. An earlier draft
        # of this fix merely stopped the incumbent being cleared, which left BOTH
        # windows holding the conversation. That is safe for #892's predicate
        # (`conversation_is_bound` is SELECT 1 ... LIMIT 1, so N>=1 answers True)
        # but it makes "one conversation, AT MOST one window" false, and #880
        # states that as the property the chain's retirement depends on.
        #
        # ONLY A POSITIVE True PROTECTS. A dead or unprovable holder still yields
        # to the newcomer, so the VPS gate -- where the daemon shares no pid
        # namespace and every verdict is None -- behaves exactly as before.
        #
        # RESIDUAL, stated rather than hidden: a refused newcomer gets no claim
        # until its NEXT SessionStart, and only after the incumbent dies and is
        # reaped. Bounded and recoverable, but not instant. Re-claiming a freed
        # conversation without waiting for SessionStart is follow-up work.
        checker = is_alive if is_alive is not None else _window_process_is_alive

        def _incumbent(conn: Any) -> Any:
            return conn.execute(
                f"SELECT window_key, host_pid, host_created_filetime "
                f"FROM {WINDOW_CONVERSATION_TABLE} "
                f"WHERE host_session_id = ? AND window_key <> ?",
                (conversation, window),
            ).fetchall()

        try:
            holders = self._run(project_root, _incumbent) or []
        except Exception:  # noqa: BLE001 -- an unreadable store proves nothing
            holders = []
        for holder in holders:
            try:
                verdict = checker(int(holder[1] or 0), int(holder[2] or 0))
            except Exception:  # noqa: BLE001 -- a raise proves nothing
                verdict = None
            if verdict is True:
                return None

        def _work(conn: Any) -> None:
            conn.execute(
                f"""
                INSERT INTO {WINDOW_CONVERSATION_TABLE} (
                    window_key, host_session_id, previous_host_session_id,
                    host_kind, host_pid, host_created_filetime,
                    first_seen_at, last_seen_at, bind_count, source,
                    bound_user_id, bound_via, bound_at
                ) VALUES (?, ?, '', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(window_key) DO UPDATE SET
                    -- Only a CHANGE displaces a conversation. A repeat firing
                    -- on the same one must not overwrite the real previous.
                    previous_host_session_id = CASE
                        WHEN {WINDOW_CONVERSATION_TABLE}.host_session_id
                             <> excluded.host_session_id
                        THEN {WINDOW_CONVERSATION_TABLE}.host_session_id
                        ELSE {WINDOW_CONVERSATION_TABLE}.previous_host_session_id
                        END,
                    host_session_id = excluded.host_session_id,
                    host_kind = excluded.host_kind,
                    host_pid = excluded.host_pid,
                    host_created_filetime = excluded.host_created_filetime,
                    -- first_seen_at is deliberately NOT in this list: it is the
                    -- moment this window was first seen, not the last.
                    last_seen_at = excluded.last_seen_at,
                    bind_count = {WINDOW_CONVERSATION_TABLE}.bind_count + 1,
                    source = excluded.source,
                    -- WRITE ONCE, AND ONLY FROM EMPTY. The stamp records WHO
                    -- this window was created by; a later firing is the SAME
                    -- window, so it may not re-answer the question. Two rules
                    -- in one CASE: an existing stamp is never overwritten (a
                    -- second principal cannot take the window), and an empty
                    -- excluded value never blanks a stamp (the rotation this
                    -- column exists to survive carries no principal at all).
                    bound_user_id = CASE
                        WHEN {WINDOW_CONVERSATION_TABLE}.bound_user_id = ''
                             AND excluded.bound_user_id <> ''
                        THEN excluded.bound_user_id
                        ELSE {WINDOW_CONVERSATION_TABLE}.bound_user_id
                        END,
                    bound_via = CASE
                        WHEN {WINDOW_CONVERSATION_TABLE}.bound_user_id = ''
                             AND excluded.bound_user_id <> ''
                        THEN excluded.bound_via
                        ELSE {WINDOW_CONVERSATION_TABLE}.bound_via
                        END,
                    bound_at = CASE
                        WHEN {WINDOW_CONVERSATION_TABLE}.bound_user_id = ''
                             AND excluded.bound_user_id <> ''
                        THEN excluded.bound_at
                        ELSE {WINDOW_CONVERSATION_TABLE}.bound_at
                        END
                """,
                (
                    window,
                    conversation,
                    str(host_kind or "").strip(),
                    host_pid,
                    host_created,
                    now,
                    now,
                    str(source or "").strip(),
                    who,
                    who_via,
                    now if who else "",
                ),
            )
            # ── ONE CONVERSATION, ONE WINDOW (#880) ───────────────────────
            #
            # SessionStart already unbinds per WINDOW (the previous_host_session_id
            # rule above). Nothing unbound per CONVERSATION, so a conversation
            # opened in a second window was held by BOTH — measured on the
            # operator's box 2026-08-23: bc8bd9e3 on 24324 and 6164 at once, and
            # 3d93740d on two windows since 17:25 with no way to resolve it.
            #
            # `idx_window_conversation_session` was created to ask exactly this
            # question, with the schema noting "nothing consults it yet".
            #
            # IN THE SAME TRANSACTION as the claim, deliberately: a crash between
            # the two would leave the duplicate this exists to prevent.
            #
            # RELEASE THE CONVERSATION, NOT THE WINDOW. The old row stays — that
            # window still exists, and deleting it would destroy the only record
            # of what it held. Its claim is cleared and remembered in
            # previous_host_session_id, which is the same displacement trace the
            # upsert writes.
            #
            # Reaching here means NO live incumbent was found above, so every
            # remaining holder is dead or unprovable and yields as it always did.
            conn.execute(
                f"""
                UPDATE {WINDOW_CONVERSATION_TABLE}
                   SET previous_host_session_id = host_session_id,
                       host_session_id = ''
                 WHERE host_session_id = ?
                   AND window_key <> ?
                """,
                (conversation, window),
            )

        self._run(project_root, _work)
        return {
            "window_key": window,
            "host_session_id": conversation,
            "recorded_at": now,
            "source": str(source or "").strip(),
        }

    def window_conversation(
        self, project_root: Path, window_key: str
    ) -> dict[str, Any]:
        """The row for one window, or ``{}`` when that window has none.

        A READ with no decision attached to it — diagnostics and tests only in
        phase 1. ``{}`` is the honest "this window has never declared a
        conversation here", never a substituted default row.
        """
        window = str(window_key or "").strip()
        if not window:
            return {}

        def _work(conn: Any) -> Any:
            return conn.execute(
                f"SELECT window_key, host_session_id, previous_host_session_id, "
                f"host_kind, host_pid, host_created_filetime, first_seen_at, "
                f"last_seen_at, bind_count, source, "
                f"bound_user_id, bound_via, bound_at "
                f"FROM {WINDOW_CONVERSATION_TABLE} WHERE window_key = ?",
                (window,),
            ).fetchone()

        try:
            row = self._run(project_root, _work)
        except Exception:  # noqa: BLE001 -- an unreadable store reads empty
            return {}
        return dict(row) if row else {}

    def window_operator(self, project_root: Path, window_key: str) -> tuple[str, str]:
        """``(bound_user_id, bound_via)`` for one window — the WHO STAMP.

        A PLAIN COLUMN READ, keyed on the window and nothing else. That is the
        whole point of the column: the measured defect (2026-09-04) was a
        RESOLUTION step — "find the operator for whatever id this call carries"
        — and a rotating id made it answer ``None`` for an authenticated
        super_admin. There is no id to rotate here.

        ``("", "")`` when the window has no stamp, and it is never filled in
        from the managed session, the conversation, or the machine login:
        SESSION MEMBERSHIP IS NOT IDENTITY (empire law promoted-cc6c4ac686ee),
        and a machine-login default would make every remote window the local
        operator.
        """
        row = self.window_conversation(project_root, window_key)
        if not row:
            return "", ""
        who = str(row.get("bound_user_id") or "").strip()
        via = str(row.get("bound_via") or "").strip()
        # Both halves or neither: an unattributable stamp is not a stamp.
        if not who or not via:
            return "", ""
        return who, via

    def conversation_windows(self, project_root: Path, host_session_id: str) -> list[str]:
        """Every window_key currently holding *host_session_id*, or ``[]``.

        THE REVERSE QUESTION, and the consumer `idx_window_conversation_session`
        was created for. Phase 1 shipped that index with the note "nothing
        consults it yet"; this is the reader.

        WHY ABSENCE FROM THIS TABLE IS EVIDENCE, and absence from the #464 chain
        never was. The chain is cap-16 with FIFO eviction, so a LIVE window's id
        can be pushed out of it by traffic alone — which is why #880 refuted
        chain membership as an authority predicate, and why grading an evicted
        window DEAD deleted live bindings (#892). This table evicts NOTHING: one
        row per window, written by SessionStart, and no pruner exists. So a
        conversation id that appears in NO row has never been a window here, and
        that is a fact rather than a guess.

        A LIST, not a bool: measured 2026-08-23, one conversation legitimately
        appears on two windows at once (open a window on an existing
        conversation and the prior holder is never unbound). Returning a single
        row would force a "whoever wrote last" tiebreak — the exact #859 guess
        this programme exists to delete. The caller sees the ambiguity instead.

        ``[]`` on an unreadable store is NOT the same claim as ``[]`` on a
        readable one; callers that need to tell those apart must not use this
        method — see `conversation_is_bound`.
        """
        sid = str(host_session_id or "").strip()
        if not sid:
            return []

        def _work(conn: Any) -> Any:
            return conn.execute(
                f"SELECT window_key FROM {WINDOW_CONVERSATION_TABLE} "
                f"WHERE host_session_id = ?",
                (sid,),
            ).fetchall()

        try:
            rows = self._run(project_root, _work)
        except Exception:  # noqa: BLE001 -- see conversation_is_bound for the honest form
            return []
        return [str(r[0]) for r in (rows or []) if r and r[0]]

    def has_any_conversation(self, project_root: Path) -> bool | None:
        """Does this store know ANY window at all? ``True``/``False``/``None``.

        THE COMPLETENESS PRECONDITION. `conversation_is_bound` returning False
        is a positive claim — "read the store, it does not carry this id" — and
        a caller may refuse or delete on it. That claim is only meaningful if
        the store has something to say: an EMPTY table cannot tell "not a
        window" from "nothing recorded yet".

        WRITTEN, DELETED, AND REINSTATED IN ONE SESSION — the history is the
        documentation. It was first added as a guard inside
        `window_lease.conductor_liveness_oracle`, and a mutation gate proved it
        could not change any outcome THERE: the classifier's per-session rule
        already made every row of an unattested store UNPROVABLE. Deleting it
        was right, and the reasoning attached to the deletion ("a check that
        cannot fail is not a check") was right too.

        It was wrong as a GENERAL claim. `managed_mode_service`'s writer guard
        has no per-session rule to lean on — it is a single lookup — so there an
        empty store refused the first-ever bind of every new session, which is
        precisely the availability failure the retired "an EMPTY chain refuses
        NOTHING" rule existed to prevent. Caught by a test, not by review.

        So it lives here with a real consumer and a test that fails without it,
        which is the standard its deletion was measured against.

        ``None`` when the store cannot be read — unreadable is not empty.
        """

        def _work(conn: Any) -> Any:
            return conn.execute(
                f"SELECT 1 FROM {WINDOW_CONVERSATION_TABLE} LIMIT 1",
            ).fetchone()

        try:
            return bool(self._run(project_root, _work))
        except Exception:  # noqa: BLE001 -- unreadable is UNPROVABLE, never empty
            return None

    def reap_dead_windows(self, project_root: Path, *, is_alive=None) -> dict[str, Any]:
        """Remove rows whose window is PROVABLY gone. Returns a report.

        The lease had no lifecycle: nothing ever deleted a row, and the
        operator's box carried rows stale for hours. `previous_host_session_id`
        exists in the schema because "#880's watcher must UNBIND what the window
        held before" — the field was built for this.

        IT MUST AGREE WITH `classify_conductor_bindings` ABOUT ABSENCE. #892 made
        "this id holds no lease" a licence to DELETE a conductor binding, which
        is sound only while absence means "not a LIVE window". So a row is
        removed ONLY on positive proof of death. Reaping on doubt would turn a
        merely-unreadable window into a phantom and cost it its binding — the
        same lockout, arriving from the other side.

        ``is_alive(pid, created_filetime) -> True | False | None`` is injected
        (default: the real check). BOTH HALVES, always: a recycled pid whose
        creation time is unchecked would let a NEW process inherit a dead
        window's lease, its conversation and its authority (#880 item 1, the
        one-way door).

        THE PID-NAMESPACE GUARD. If NOT ONE row can be confirmed alive, nothing
        is reaped. On the VPS gate the daemon shares no pid namespace with the
        windows, so its liveness answers are about unrelated processes; a reaper
        that believed them would delete every tenant's lease and look like it
        was working. "Wrong namespace" and "every window really is closed" are
        indistinguishable from here, so the safe reading is chosen. Same
        completeness shape as #892's per-session rule.
        """
        checker = is_alive if is_alive is not None else _window_process_is_alive

        def _read(conn: Any) -> Any:
            return conn.execute(
                f"SELECT window_key, host_pid, host_created_filetime, host_session_id "
                f"FROM {WINDOW_CONVERSATION_TABLE}",
            ).fetchall()

        try:
            rows = self._run(project_root, _read) or []
        except Exception:  # noqa: BLE001 -- an unreadable store reaps nothing
            return {"reaped": [], "skipped": "store_unreadable"}

        verdicts: list[tuple[str, str, bool | None]] = []
        any_alive = False
        for row in rows:
            key = str(row[0])
            try:
                verdict = checker(int(row[1] or 0), int(row[2] or 0))
            except Exception:  # noqa: BLE001 -- a raise proves nothing
                verdict = None
            if verdict is True:
                any_alive = True
            verdicts.append((key, str(row[3] or ""), verdict))

        if not any_alive:
            return {"reaped": [], "skipped": "no_window_confirmed_alive"}

        reaped: list[dict[str, Any]] = []
        for key, conversation, verdict in verdicts:
            if verdict is not False:
                continue

            def _delete(conn: Any, _k: str = key) -> None:
                conn.execute(
                    f"DELETE FROM {WINDOW_CONVERSATION_TABLE} WHERE window_key = ?",
                    (_k,),
                )

            try:
                self._run(project_root, _delete)
            except Exception:  # noqa: BLE001 -- a row we could not delete is not reaped
                continue
            reaped.append({"window_key": key, "host_session_id": conversation})
        return {"reaped": reaped, "skipped": ""}

    def heal_released_windows(self, project_root: Path, *, is_alive=None) -> dict[str, Any]:
        """Give a released claim back to a LIVE window when nothing else holds it.

        #919 residual, operator 2026-08-26: "released rows should heal on
        sessionstart".

        THE STATE THIS REPAIRS. A row with ``host_session_id = ''`` and a
        ``previous_host_session_id`` set is a window that HAD a conversation and
        lost it to the one-conversation-one-window release. A claim is written
        only by SessionStart, so such a window stays leaseless until IT fires
        SessionStart again -- which for a long-running session may be hours, or
        never. Measured on the operator's box: window 29520 sat alive and
        identity-less while the conversation it was still running was held by
        NOBODY, because the window that took it had already died and been reaped.

        WHY IT RUNS AFTER THE REAP, and the order is load-bearing: the reap is
        what DELETES the usurper's row, and that is what frees the conversation
        this heal can then hand back. Healing first would find the conversation
        still held and correctly decline.

        THREE CONDITIONS, ALL REQUIRED:
          * the row is genuinely released -- blank claim AND a remembered previous;
          * the window is PROVABLY ALIVE (a positive True, never None) -- the
            same asymmetry `reap_dead_windows` uses in the other direction, so
            an unreadable window is never handed authority on doubt;
          * NOTHING else holds that conversation -- checked against the live
            table AND against claims restored earlier in this same pass, so two
            released rows can never both be given the same conversation.

        The invariant therefore survives: exactly one holder before, exactly one
        after. This can only ever move a conversation from ZERO holders to one.

        WHAT IT WILL NOT DO. It never restores to a window that has since MOVED:
        a window that rotated its conversation (/clear, /resume) did so THROUGH
        SessionStart, which wrote the new id -- so its claim is not blank and
        this does not touch it. A blank claim is positive evidence that no
        SessionStart has run for that window since the release.
        """
        checker = is_alive if is_alive is not None else _window_process_is_alive

        def _read(conn: Any) -> Any:
            return conn.execute(
                f"SELECT window_key, host_session_id, previous_host_session_id, "
                f"host_pid, host_created_filetime FROM {WINDOW_CONVERSATION_TABLE}",
            ).fetchall()

        try:
            rows = self._run(project_root, _read) or []
        except Exception:  # noqa: BLE001 -- an unreadable store heals nothing
            return {"healed": [], "skipped": "store_unreadable"}

        held = {str(r[1] or "").strip() for r in rows if str(r[1] or "").strip()}
        healed: list[dict[str, Any]] = []
        for row in rows:
            window = str(row[0] or "").strip()
            claim = str(row[1] or "").strip()
            previous = str(row[2] or "").strip()
            if claim or not previous or not window:
                continue
            if previous in held:
                continue
            try:
                verdict = checker(int(row[3] or 0), int(row[4] or 0))
            except Exception:  # noqa: BLE001 -- a raise proves nothing
                verdict = None
            if verdict is not True:
                continue

            def _restore(conn: Any, _w: str = window, _c: str = previous) -> None:
                # `AND host_session_id = ''` is the concurrency guard: if
                # anything claimed this window between the read and now, the
                # restore must be a no-op rather than an overwrite.
                conn.execute(
                    f"""
                    UPDATE {WINDOW_CONVERSATION_TABLE}
                       SET host_session_id = ?,
                           previous_host_session_id = ''
                     WHERE window_key = ? AND host_session_id = ''
                    """,
                    (_c, _w),
                )

            try:
                self._run(project_root, _restore)
            except Exception:  # noqa: BLE001 -- a row we could not write is not healed
                continue
            held.add(previous)
            healed.append({"window_key": window, "host_session_id": previous})
        return {"healed": healed, "skipped": ""}

    def conversation_is_bound(
        self, project_root: Path, host_session_id: str
    ) -> bool | None:
        """Is *host_session_id* a window here? ``True`` / ``False`` / ``None``.

        THREE STATES, because two of them are answers and one is not:
          * ``True``  — at least one window holds it.
          * ``False`` — the store was READ and does not carry it. Positive
            evidence, for the no-eviction reason in `conversation_windows`.
          * ``None``  — the store could not be read. NOT "no", and callers must
            not collapse it into one: this feeds a classifier whose False
            bucket gets DELETED, so an unreadable store must never look like a
            denial (#892).
        """
        sid = str(host_session_id or "").strip()
        if not sid:
            return None

        def _work(conn: Any) -> Any:
            return conn.execute(
                f"SELECT 1 FROM {WINDOW_CONVERSATION_TABLE} "
                f"WHERE host_session_id = ? LIMIT 1",
                (sid,),
            ).fetchone()

        try:
            row = self._run(project_root, _work)
        except Exception:  # noqa: BLE001 -- unreadable is UNPROVABLE, never a denial
            return None
        return bool(row)


def reap_dead_windows_on_session_start(
    project_root: Path,
    *,
    is_alive=None,
) -> dict[str, Any]:
    """Reap provably-dead windows. Best-effort, NEVER raises.

    Called from SessionStart because the boot-time reap alone does not run: the
    daemon is long-lived (measured: ~18h uptime while this was being written),
    so "reap at boot" on a process that does not restart is a reap that does not
    happen. SessionStart fires on every startup, resume and compact.

    SESSIONSTART AVAILABILITY IS NOT NEGOTIABLE. `record_session_start_window`
    states the rule for its own write — "a diagnostic write that can refuse a
    session start would be a worse bug than any it records" — and it binds this
    just as hard: every failure degrades to "nothing reaped".
    """
    try:
        # NEVER ADOPT A FOLDER BY LOOKING AT IT. Opening the store CREATES its
        # tree, and this runs on EVERY SessionStart -- so starting a Claude
        # session in any unadopted directory would silently conjure `.MEMORY/`
        # there. Measured by test_no_adoption_by_side_effect: "SessionStart
        # adopted an unadopted folder by creating .MEMORY".
        #
        # A store that was never created holds no leases to reap, so "nothing
        # here" is the honest answer rather than a freshly minted empty table.
        # Same rule the seat reaper now follows, for the same reason.
        store = WindowBindingStore()
        if not store.db_path(project_root).exists():
            return {"reaped": [], "skipped": "no_window_store"}
        return store.reap_dead_windows(project_root, is_alive=is_alive)
    except Exception as exc:  # noqa: BLE001
        try:
            sys.stderr.write(
                f"[aidocs window] lease reap skipped: {type(exc).__name__}: {exc}\n",
            )
        except Exception:
            pass
        return {"reaped": [], "skipped": "reaper_failed"}


def heal_released_windows_on_session_start(
    project_root: Path,
    *,
    is_alive=None,
) -> dict[str, Any]:
    """Restore released claims to live windows. Best-effort, NEVER raises.

    Sibling of `reap_dead_windows_on_session_start` and bound by the same rule:
    SessionStart availability is not negotiable, so every failure here degrades
    to "nothing healed".

    MUST RUN AFTER THE REAP -- see `heal_released_windows`. The reap frees the
    conversations this restores.
    """
    try:
        # Same no-adoption-by-side-effect rule as the reaper: opening the store
        # CREATES its tree, and this runs on EVERY SessionStart, so a session
        # started in an unadopted directory must not conjure `.MEMORY/` there.
        store = WindowBindingStore()
        if not store.db_path(project_root).exists():
            return {"healed": [], "skipped": "no_window_store"}
        return store.heal_released_windows(project_root, is_alive=is_alive)
    except Exception as exc:  # noqa: BLE001
        try:
            sys.stderr.write(
                f"[aidocs window] lease heal skipped: {type(exc).__name__}: {exc}\n",
            )
        except Exception:
            pass
        return {"healed": [], "skipped": "heal_failed"}


def record_session_start_window(
    project_root: Path,
    payload: object,
    *,
    host_session_id: str,
    host_kind: str = "",
    bound_user_id: str = "",
    bound_via: str = "",
) -> dict[str, Any] | None:
    """SessionStart's one-line entry point. Best-effort, never raises.

    ``bound_user_id`` / ``bound_via`` are the WHO STAMP: the authenticated
    user this window is CREATED under, and the creation path that named them.
    Both or neither, and a creation that can name nobody stamps nothing —
    the window binds as unauthenticated and every consumer fails closed on it.

    Reads the window OFF THE PAYLOAD (``window_key.window_from_payload``) and
    never derives it: this runs in the broker's host — the watchdog — whose own
    ancestry is not this window's, and which may itself descend from some OTHER
    window's Bash.

    SessionStart availability is not negotiable. A diagnostic write that can
    refuse a session start would be a worse bug than any it records, so every
    failure here degrades to "no row", which is a state phase 1 already handles
    on every non-win32 box.
    """
    try:
        from .window_key import window_from_payload

        window, _reason = window_from_payload(payload)
        if not window:
            # The honest empty, and nothing else. No row is written from an
            # unresolved key — a later reader must never have to wonder whether
            # a row is real.
            return None
        return WindowBindingStore().record_window_conversation(
            project_root,
            window_key=window,
            host_session_id=host_session_id,
            host_kind=host_kind,
            source="session_start",
        )
    except Exception as exc:  # noqa: BLE001
        try:
            sys.stderr.write(
                f"[aidocs window] window->conversation not recorded: "
                f"{type(exc).__name__}: {exc}\n",
            )
        except Exception:
            pass
        return None
