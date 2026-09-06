"""Session freeze store — UX/control state layer for #39 confirm pipeline.

When the judge classifies a tool call as confirmable_destructive, the
hook creates an escalation request (existing primitive in
`escalation_store`) AND a freeze row here. The freeze row owns the
"this session is frozen, no other tools, no continued work" UX
contract. The escalation row owns the approval/grant backend.

Two concepts, two layers, one bridge: every freeze row points at an
escalation request_id.

## Freeze kinds

  self_approve       — operator-rank verdict. Single-turn semantics
                       per #39: lasts from blocked tool call until
                       the next UserPromptSubmit. That UPS resolves
                       it exactly once (exact phrase mints grant /
                       cancel pattern records denial / anything else
                       silently clears).

  admin_escalation   — admin-rank verdict. Persistent across multiple
                       UPS until admin runs aidocs admin approve in a
                       separate session. Operator's prompts during
                       the wait do NOT auto-cancel.

Phase A (this commit) ships self_approve only. admin_escalation is a
recognized kind but the polling logic lives in claude_hook, not here.

## Single-row-per-session contract

At most ONE active freeze per session. Setting a new freeze on a
session that already has one REPLACES the prior — the rationale is
that a fresh confirmable verdict supersedes any unresolved older
freeze. This shouldn't happen in normal flow (the prior freeze
prevents new tool calls) but defends against race conditions where a
parallel hook event somehow slips through.

Co-located with escalation_store on the identity DB so freeze rows
and request rows live together for atomicity.
"""

from __future__ import annotations

import sqlite3

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect
from ._sqlite_connect import mark_schema_ensured as _mark_schema_ensured
from ._sqlite_connect import schema_already_ensured as _schema_already_ensured
import time
from dataclasses import dataclass
from pathlib import Path

from .agent_memory_epoch import (
    derive_agent_context_id,
    derive_aidocs_session_id,
    derive_session_uuid,
)

KIND_SELF_APPROVE = "self_approve"
KIND_ADMIN_ESCALATION = "admin_escalation"
# Repeated flat security violations (command_read_intent, sensitive reads,
# unknown-external, raw-shell T0, hostile preflight). Cleared ONLY by
# operator/admin (like admin_escalation) — never by a UPS self-approve
# phrase, because the offending actor must not be able to talk its own
# way out of a lockdown it triggered.
KIND_REPEATED_SECURITY_VIOLATION = "repeated_security_violation"
# Hostile OPERATOR prompt caught at UserPromptSubmit (preflight forbidden
# verdict). Freezes IMMEDIATELY on the first nasty attempt — the prompt is
# judged before the agent ever sees it, so there is no "agent gets two
# chances" ladder. Like the repeated-violation kind, it is admin/operator
# recovery only, never a UPS self_approve.
KIND_HOSTILE_OPERATOR_PROMPT = "hostile_operator_prompt"

VALID_KINDS: frozenset[str] = frozenset(
    {
        KIND_SELF_APPROVE,
        KIND_ADMIN_ESCALATION,
        KIND_REPEATED_SECURITY_VIOLATION,
        KIND_HOSTILE_OPERATOR_PROMPT,
    },
)

# ── SCOPE: WHO a freeze binds (backlog #588 D1) ──────────────────────
#
# Before this, a freeze bound whoever the KEY happened to catch. Every
# `get_active_freeze` branch carried `OR (session_id = ? AND
# agent_context_id = '')`, and `set_freeze(host_session_id="")` — its
# own default — wrote exactly such a row. So any writer that could not
# resolve the calling agent silently minted a row that stopped EVERY
# actor in the work session: one subagent's read-only probe froze the
# conductor and every sibling (measured twice in one night, #588 D1).
#
# Session-wide is a legitimate scope; it was never a legitimate
# ACCIDENT. It is now a DECLARED column, so "this binds everyone" is
# something a writer says, not something a missing id causes:
#
#   FREEZE_SCOPE_ACTOR   — binds ONLY the agent_context_id that earned
#       it. Sibling lanes and the conductor stay live. A row cannot be
#       minted at this scope without a resolvable actor (see
#       UnattributableFreeze below) — the key IS the enforcement.
#   FREEZE_SCOPE_SESSION — binds every actor in the work session.
#       Explicit, and reserved for verdicts that are genuinely not one
#       agent's fault (a hostile OPERATOR prompt) or for a security
#       lockdown whose actor could not be identified.
#
# A legacy row (freeze_scope='') keeps its old meaning exactly: with an
# empty agent_context_id it still binds the session, because that is
# what it was minted to do and un-binding it would LIFT a live lock.
FREEZE_SCOPE_ACTOR = "actor"
FREEZE_SCOPE_SESSION = "session"

#: Kinds whose default scope is the whole work session. A hostile
#: OPERATOR prompt is judged before any agent sees it, so there is no
#: offending agent to bind — every lane in the session is downstream of
#: the same poisoned prompt. (It already broadcast operator-wide via
#: the user_id clause; this states the intent instead of leaving it to
#: the key.) Everything else defaults to the actor.
_SESSION_SCOPED_KINDS: frozenset[str] = frozenset({KIND_HOSTILE_OPERATOR_PROMPT})


class UnattributableFreeze(ValueError):
    """An actor-scoped freeze was asked for, and there is no actor.

    #588 D1 fail-closed direction. The two wrong answers were both on
    the table and both rejected:

      * "freeze the session" — that IS the outage. An identity the
        resolver could not pin is precisely the case that produced
        five compounding session-wide locks.
      * "freeze nobody" — a refused destructive action would then
        latch nothing at all, and the ladder that counts repeat
        offences would never start.

    So the mint REFUSES and says why. The caller still hard-denies the
    tool call (``freeze_service.build_freeze_response`` converts this
    into ``FreezeMintError``; the gate then blocks with
    ``freeze_mint_failed``), so the dangerous action does NOT run — it
    simply leaves no lock behind for anyone, least of all a bystander.
    A caller that genuinely means "stop everyone" must ask for
    ``FREEZE_SCOPE_SESSION`` by name.
    """


# ── TTL (backlog #588 D3; King ruling "strikes — and therefore freezes —
#    must TTL") ────────────────────────────────────────────────────────
#
# Every kind now gets a DEFAULT duration. NULL is not a duration: an
# escalation freeze with expires_at IS NULL was invisible to the purge
# (`WHERE expires_at IS NOT NULL AND expires_at <= ?`), so it survived a
# full daemon restart and an unattended session stayed dead until a human
# came back and typed a phrase.
#
# What expiry costs, and why these numbers: TTL expiry lifts the LOCK; it
# does NOT grant the action. The escalation request stays pending in
# escalation_store, the refused command never ran, and an agent that
# retries it is re-judged and re-frozen. So the worst case of expiring
# "too early" is one repeated refusal; the worst case of never expiring is
# a dead session (measured twice in one night).
#
#   self_approve (5 min)   — the single-turn contract already resolves at
#       the next UserPromptSubmit; the TTL is only the safety net for a UPS
#       that never fires.
#   admin_escalation (4 h) — long enough that an operator at a meal, a
#       meeting or an errand still returns to a live pending approval;
#       short enough that a session left running overnight is working again
#       by morning instead of burning its whole run against a wall.
#   security kinds (24 h)  — an actor that tripped the repeated-violation
#       ladder or wrote a hostile prompt stays down through a full
#       away-day. Still bounded: "until a human returns" is not a duration
#       either.
TTL_SELF_APPROVE_SECONDS = 300
TTL_ADMIN_ESCALATION_SECONDS = 4 * 60 * 60
TTL_SECURITY_VIOLATION_SECONDS = 24 * 60 * 60

DEFAULT_TTL_SECONDS: dict[str, int] = {
    KIND_SELF_APPROVE: TTL_SELF_APPROVE_SECONDS,
    KIND_ADMIN_ESCALATION: TTL_ADMIN_ESCALATION_SECONDS,
    KIND_REPEATED_SECURITY_VIOLATION: TTL_SECURITY_VIOLATION_SECONDS,
    KIND_HOSTILE_OPERATOR_PROMPT: TTL_SECURITY_VIOLATION_SECONDS,
}

#: Why a freeze row left the table without an operator clearing it. Written
#: to session_freeze_expiry so "my freeze aged out" is DISTINGUISHABLE from
#: "I was never frozen" (no row at all) and from "an operator cleared it"
#: (clear_freeze deletes without recording an expiry).
EXPIRY_REASON_TTL = "ttl_expired"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@dataclass(frozen=True)
class ExpiredFreeze:
    """A freeze that aged out on its own — the observable record of D3.

    Carries only what the notice needs to SAY. The one-shot bookkeeping
    (``session_freeze_expiry.surfaced_at``) is deliberately NOT a field
    here: it is stamped and filtered entirely in SQL by
    ``take_expiry_notice`` / ``recent_expiry(unsurfaced_only=True)``, so
    the durable state has one home. A copy on this object could only be
    stale — every instance production builds comes from the
    ``unsurfaced_only`` query, i.e. it would always read "not yet
    surfaced" even on the row we just surfaced.
    """

    session_id: str
    agent_context_id: str
    request_id: str
    kind: str
    frozen_at: str
    expires_at: str
    expired_at: str
    reason: str = EXPIRY_REASON_TTL


@dataclass(frozen=True)
class SessionFreeze:
    session_id: str
    request_id: str
    fingerprint_phrase: str
    kind: str
    frozen_at: str
    expires_at: str | None  # NULL for admin_escalation; ISO ts for self_approve TTL
    host_session_id: str = ""  # raw host token, attribution + derivation input only
    user_id: str = ""  # logged-in operator, attribution only (NOT part of the key)
    agent_context_id: str = ""  # DERIVED per-agent key (identity-spine); '' = session-wide
    aidocs_session_id: str = ""  # DERIVED canonical security-scope id
    # #571: the three-rung verdict class (verdict_class.py). '' = legacy row;
    # verdict_class.freeze_is_security_class() reads '' as security-class.
    verdict_class: str = ""
    # #588 D1: WHO this row binds — FREEZE_SCOPE_ACTOR / FREEZE_SCOPE_SESSION,
    # or '' for a legacy row whose scope was implied by the key.
    freeze_scope: str = ""


class SessionFreezeStore:
    """Per-project session-freeze state. One active row per session."""

    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs_identity.sqlite3"

    def _read_conn(self, project_root: Path) -> sqlite3.Connection | None:
        """Open the store READ-ONLY, or return None when it does not exist.

        #553: read methods used to call init_db(), which mkdirs and creates the
        database — so merely LOOKING for a freeze materialised a store. The admin
        path scans EVERY registered project to find a freeze's owner
        (cli._resolve_admin_root), so one lookup littered every project and
        whatever cwd the fallback resolved to. That debris is then what the doctor
        reports as `half_init` — "AIDOCS debris without commission evidence" —
        i.e. the creation path bypassed the condition the check enforces.

        A read answers "what is there", and "nothing is there" is a valid answer.
        Only an explicit write (init_db / set_freeze / clear_freeze) may create.
        """
        path = self.db_path(project_root)
        if not path.is_file():
            return None
        try:
            # read_only=True is the canonical connect's `file:...?mode=ro`
            # (#755, 2026-08-18). It also sets row_factory=sqlite3.Row and
            # applies the pragmas a reader CAN take -- synchronous,
            # busy_timeout, foreign_keys -- which this call site had none of.
            conn = _canonical_connect(path, read_only=True)
        except sqlite3.Error:
            return None
        return conn

    def init_db(self, project_root: Path) -> None:
        path = self.db_path(project_root)
        # ONE schema creation per process per file (#756): this ran on EVERY
        # hook event, paying an open + write lock to learn there was nothing to
        # do. The memo re-verifies the file still exists, so a deleted DB is
        # rebuilt rather than assumed.
        if _schema_already_ensured(path, "session_freeze"):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # #746: this file is SHARED by five stores, so whichever creates it
        # decides the journal mode every later connection inherits (journal_mode
        # lives in the FILE HEADER). Every creator therefore goes through the one
        # canonical connect (#755) -- WAL by luck is not WAL by design.
        with _canonical_connect(path, durability=_Durability.RUNTIME) as conn:
            # Per-agent freeze isolation keyed on the CANONICAL DERIVED identity
            # (identity-spine 2026-06-29): the per-agent axis is
            # agent_context_id = derive_agent_context_id(host_kind, project,
            # host_session_id), so two agents sharing a raw host_session_id
            # string across different host_kinds do NOT collide. session_id is
            # the work-session scope; aidocs_session_id is stored as the
            # canonical security-scope id. host_session_id is attribution +
            # derivation input only, never a primary key.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_freeze (
                    session_id TEXT NOT NULL,
                    agent_context_id TEXT NOT NULL DEFAULT '',
                    aidocs_session_id TEXT NOT NULL DEFAULT '',
                    host_session_id TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL,
                    fingerprint_phrase TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    expires_at TEXT,
                    user_id TEXT NOT NULL DEFAULT '',
                    -- #571: the VERDICT CLASS (verdict_class.py three-rung
                    -- ladder). '' = legacy row minted before the column
                    -- existed; verdict_class.is_security_class treats '' as
                    -- security-class (fails closed), so a conductor can never
                    -- clear an unclassified freeze as "just workflow".
                    verdict_class TEXT NOT NULL DEFAULT '',
                    -- #588 D1: WHO this row binds. 'actor' = only the
                    -- agent_context_id that earned it; 'session' = every
                    -- actor in the work session, declared on purpose.
                    -- '' = legacy row minted before the column existed;
                    -- it keeps its original meaning (session-wide when
                    -- agent_context_id is '', actor-only otherwise), so
                    -- no live lock is lifted by this migration.
                    freeze_scope TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (session_id, agent_context_id)
                );
                CREATE INDEX IF NOT EXISTS idx_session_freeze_request
                    ON session_freeze(request_id);
                -- #588 D3: the observable record of a freeze that aged out
                -- on its own. Purely additive; nothing reads it to DECIDE,
                -- so an interrupted create leaves the freeze contract
                -- intact and only the notice is missing.
                CREATE TABLE IF NOT EXISTS session_freeze_expiry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    agent_context_id TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT '',
                    frozen_at TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT '',
                    expired_at TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    surfaced_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_session_freeze_expiry_scope
                    ON session_freeze_expiry(session_id, agent_context_id);
            """)
            # Migrate any older shape (legacy PK=session_id, or the composite
            # PK=(session_id, host_session_id) era) onto the derived-key schema
            # via recreate-copy-rename. Existing rows are all claude_code (the
            # only live host today), so backfill the derived ids with
            # host_kind='claude_code'. Rows with host_session_id='' derive to
            # agent_context_id='' (session-wide), preserving that fallback.
            # #243 crash-atomic migration + recovery. Two trigger states:
            #  (1) FRESH: session_freeze is still the old shape (no
            #      agent_context_id) and no _legacy table yet -> rename aside,
            #      create the new table, restore derived rows, drop _legacy.
            #  (2) RECOVERY: a pre-fix crash committed an EMPTY new-schema
            #      session_freeze and stranded the rows in session_freeze_legacy
            #      (the fail-open — active freezes vanished and the re-fire guard
            #      was defeated by the empty new table). Restore from _legacy.
            # BOTH run under ONE transaction (store_migrations.atomic_migration):
            # a kill rolls back to the intact pre-migration state, so the source
            # rows are NEVER gone before the destination is populated. Only
            # single-statement conn.execute inside the txn — executescript would
            # auto-commit and defeat the atomicity.
            from .store_migrations import atomic_migration, table_exists

            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_freeze)").fetchall()}
            if "agent_context_id" not in cols or table_exists(conn, "session_freeze_legacy"):
                with atomic_migration(conn):
                    if not table_exists(conn, "session_freeze_legacy"):
                        conn.execute(
                            "ALTER TABLE session_freeze RENAME TO session_freeze_legacy"
                        )
                        conn.execute(
                            """
                            CREATE TABLE session_freeze (
                                session_id TEXT NOT NULL,
                                agent_context_id TEXT NOT NULL DEFAULT '',
                                aidocs_session_id TEXT NOT NULL DEFAULT '',
                                host_session_id TEXT NOT NULL DEFAULT '',
                                request_id TEXT NOT NULL,
                                fingerprint_phrase TEXT NOT NULL,
                                kind TEXT NOT NULL,
                                frozen_at TEXT NOT NULL,
                                expires_at TEXT,
                                user_id TEXT NOT NULL DEFAULT '',
                                PRIMARY KEY (session_id, agent_context_id)
                            )
                            """
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_session_freeze_request "
                            "ON session_freeze(request_id)"
                        )
                    lcols = {
                        r[1]
                        for r in conn.execute(
                            "PRAGMA table_info(session_freeze_legacy)"
                        ).fetchall()
                    }
                    host_expr = "host_session_id" if "host_session_id" in lcols else "''"
                    user_expr = "user_id" if "user_id" in lcols else "''"
                    legacy = conn.execute(
                        f"SELECT session_id, {host_expr} AS hsid, request_id, "
                        f"fingerprint_phrase, kind, frozen_at, expires_at, "
                        f"{user_expr} AS uid FROM session_freeze_legacy",
                    ).fetchall()
                    for r in legacy:
                        s_id = str(r[0])
                        hsid = str(r[1] or "")
                        acid, asid = self._derive_keys(
                            project_root,
                            session_id=s_id,
                            host_session_id=hsid,
                            host_kind="claude_code",
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO session_freeze "
                            "(session_id, agent_context_id, aidocs_session_id, "
                            " host_session_id, request_id, fingerprint_phrase, kind, "
                            " frozen_at, expires_at, user_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (s_id, acid, asid, hsid, r[2], r[3], r[4], r[5], r[6], str(r[7] or "")),
                        )
                    conn.execute("DROP TABLE session_freeze_legacy")
            # #571 additive column. Purely additive (NOT NULL DEFAULT '') so no
            # table rebuild and no atomic_migration is needed — an interrupted
            # ALTER leaves the pre-#571 shape, which still reads correctly
            # because every reader tolerates a missing verdict_class and the
            # fail-closed reader treats absence as security-class.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_freeze)").fetchall()}
            if "verdict_class" not in cols:
                conn.execute(
                    "ALTER TABLE session_freeze "
                    "ADD COLUMN verdict_class TEXT NOT NULL DEFAULT ''"
                )
            # #588 D1 additive column, same reasoning as verdict_class above:
            # an interrupted ALTER leaves the pre-#588 shape, which every
            # reader below still handles — a row with no freeze_scope is read
            # as the legacy row it is, and legacy rows keep binding exactly
            # who they bound before.
            if "freeze_scope" not in cols:
                conn.execute(
                    "ALTER TABLE session_freeze ADD COLUMN freeze_scope TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()
        # Schema settled for this file in this process -- see the guard at the
        # top of this method.
        _mark_schema_ensured(path, "session_freeze")

    def _derive_keys(
        self,
        project_root: Path,
        *,
        session_id: str,
        host_session_id: str,
        host_kind: str,
        agent_id: str = "",
    ) -> tuple[str, str]:
        """(agent_context_id, aidocs_session_id) from the canonical stack.

        Empty host_session_id -> ('', '') = the legacy/session-wide bucket.
        agent_context_id is the per-agent axis (excludes the work session, so
        it is compaction- AND work-session-stable); aidocs_session_id is the
        per-agent-per-session security-scope id.

        ``agent_id`` — THE SUBAGENT AXIS (#879 B1). Without it a subagent
        crossed the strike threshold under its OWN scope key
        (``security_violation_service`` has derived with ``agent_id`` since
        2026-08-22) and then latched the resulting freeze under its PARENT's
        key, which matches every sibling and the conductor. The comment at
        ``security_violation_service._create_freeze`` promised the opposite of
        what this derivation did.

        THE BASE DERIVATION IS UNCHANGED and that is the whole migration
        strategy: with no ``agent_id`` this returns the v1 id BYTE-IDENTICALLY
        (the main thread, and every host that never sends one). A caller that
        HAS an agent_id never had a distinct key before, so no stored row is
        orphaned — v2 is a strict superset of v1 on this axis, which is why
        this store needs no dual-read. See the module note on
        ``UnattributableFreeze`` for what happens when nothing resolves.

        ``aidocs_session_id`` deliberately does NOT take the agent axis: it is
        a different id with its own version tag and its own readers, and
        rotating it here would be exactly the silent orphaning this avoids.
        """
        agent_context_id = derive_agent_context_id(
            host_kind=host_kind,
            project_root=project_root,
            host_session_id=host_session_id,
            agent_id=agent_id or None,
        )
        session_uuid = derive_session_uuid(project_root, session_id)
        aidocs_session_id = derive_aidocs_session_id(
            host_kind=host_kind,
            project_root=project_root,
            host_session_id=host_session_id,
            session_uuid=session_uuid,
        )
        return agent_context_id, aidocs_session_id

    @staticmethod
    def _reap_expired(conn: sqlite3.Connection, now_iso: str) -> int:
        """Move every freeze row past its expires_at into the expiry log.

        The DELETE and the expiry record are the same statement pair on
        the same connection, so a reaped freeze is always accounted for:
        the row never disappears without leaving the reason behind. This
        is what makes "my freeze aged out" observable rather than the
        session silently finding things working again (#588 D3).

        Best-effort on the RECORD only — if the expiry table is missing
        (a store created by an older build and not yet re-inited) the
        rows are still reaped, because a freeze that outlives its TTL is
        the defect this fixes.
        """
        conn.row_factory = sqlite3.Row
        due = conn.execute(
            "SELECT session_id, agent_context_id, request_id, kind, frozen_at, expires_at "
            "FROM session_freeze WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso,),
        ).fetchall()
        if not due:
            return 0
        for row in due:
            try:
                conn.execute(
                    "INSERT INTO session_freeze_expiry "
                    "(session_id, agent_context_id, request_id, kind, frozen_at, "
                    " expires_at, expired_at, reason, surfaced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')",
                    (
                        str(row["session_id"]),
                        str(row["agent_context_id"] or ""),
                        str(row["request_id"] or ""),
                        str(row["kind"] or ""),
                        str(row["frozen_at"] or ""),
                        str(row["expires_at"] or ""),
                        now_iso,
                        EXPIRY_REASON_TTL,
                    ),
                )
            except sqlite3.Error:
                pass
        cur = conn.execute(
            "DELETE FROM session_freeze WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso,),
        )
        conn.commit()
        return int(cur.rowcount or 0)

    def set_freeze(
        self,
        project_root: Path,
        *,
        session_id: str,
        request_id: str,
        fingerprint_phrase: str,
        kind: str = KIND_SELF_APPROVE,
        ttl_seconds: int | None = None,
        host_session_id: str = "",
        user_id: str = "",
        host_kind: str = "claude_code",
        verdict_class: str = "",
        scope: str | None = None,
        agent_id: str = "",
        now: float | None = None,
    ) -> SessionFreeze:
        """Replace any existing freeze on this session with a fresh one.

        ``scope`` policy (#588 D1) — WHO the row binds:
          - ``FREEZE_SCOPE_ACTOR`` -> binds only the resolved actor. If the
            actor cannot be resolved (no host_session_id, or a host_kind the
            id-tree refuses), this RAISES ``UnattributableFreeze`` rather
            than widening to the session. See that class for why neither
            "freeze the session" nor "freeze nobody" was acceptable.
          - ``FREEZE_SCOPE_SESSION`` -> binds every actor in the work
            session. A deliberate, named act.
          - omitted -> the kind's default (session for
            ``hostile_operator_prompt``, actor for everything else), then
            degraded to session ONLY when there is no actor to key on. That
            degradation preserves the pre-#588 behaviour for the callers
            that never passed a host identity, but it is now RECORDED as a
            session-scoped row instead of being an invisible property of an
            empty key.

        ttl_seconds policy (#588 D3, superseding the 2026-05-04 "no
        automatic TTL" doctrine per the King ruling that strikes — and
        therefore freezes — must TTL):
          - ttl_seconds omitted -> DEFAULT_TTL_SECONDS[kind]. Every kind
            has one; see the constants above for the durations and the
            argument for each.
          - ttl_seconds <= 0 -> no expiry. Kept so a caller can still
            mint a deliberately unbounded lock, but that is now an
            EXPLICIT act with a name, not the silent default.

        ``agent_id`` (#879 B1) is the SUBAGENT axis of the actor key. Omitted
        (or blank) it derives the v1 id byte-identically, so the conductor and
        every host that never sends one are untouched; supplied, the row binds
        the subagent that actually earned the lock instead of its parent —
        which is what made one lane agent's lockdown stop its siblings and the
        conductor.

        ``now`` is the injected clock (epoch seconds); tests pass it so
        expiry is provable without sleeping and without a live store.
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown freeze kind: {kind!r}")
        if not session_id.strip():
            raise ValueError("session_id required")
        if not request_id.strip():
            raise ValueError("request_id required")
        if not fingerprint_phrase.strip():
            raise ValueError("fingerprint_phrase required")
        self.init_db(project_root)
        now_ts = time.time() if now is None else float(now)
        frozen_at = _iso(now_ts)
        # #588 D3: an omitted TTL is not "no TTL" any more — it is the
        # kind's default. Only an explicit non-positive ttl_seconds mints
        # an unbounded lock.
        effective_ttl = DEFAULT_TTL_SECONDS.get(kind, 0) if ttl_seconds is None else ttl_seconds
        expires_at: str | None = None
        if effective_ttl and effective_ttl > 0:
            expires_at = _iso(now_ts + effective_ttl)
        acid, asid = self._derive_keys(
            project_root,
            session_id=session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
            agent_id=agent_id,
        )
        # #571: normalize the verdict class at the persistence boundary so the
        # stored value is always one of verdict_class.VALID_VERDICT_CLASSES.
        # A caller that passes nothing gets '' preserved — meaning "legacy /
        # unclassified", which the fail-closed reader treats as security-class.
        # A caller that passes a typo gets it coerced to the freeze rung rather
        # than silently stored as an unrecognised (and therefore unenforceable)
        # string.
        stored_class = ""
        if str(verdict_class or "").strip():
            from .verdict_class import normalize as _normalize_verdict_class

            stored_class = _normalize_verdict_class(verdict_class)
        # #588 D1: decide the SCOPE before the write, and refuse rather than
        # widen. `acid` is the actor key; an empty one means the id-tree
        # declined to name this caller (no host_session_id, or no host_kind).
        requested_scope = str(scope or "").strip().lower()
        if requested_scope == FREEZE_SCOPE_ACTOR and not acid:
            raise UnattributableFreeze(
                "actor-scoped freeze refused: the calling agent could not be "
                "resolved (host_session_id/host_kind missing), and a freeze "
                "that cannot name its owner must not be latched onto every "
                "actor in the session",
            )
        if requested_scope in (FREEZE_SCOPE_ACTOR, FREEZE_SCOPE_SESSION):
            stored_scope = requested_scope
        elif not acid or kind in _SESSION_SCOPED_KINDS:
            stored_scope = FREEZE_SCOPE_SESSION
        else:
            stored_scope = FREEZE_SCOPE_ACTOR
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_freeze "
                "(session_id, agent_context_id, aidocs_session_id, host_session_id, "
                " request_id, fingerprint_phrase, kind, frozen_at, expires_at, user_id, "
                " verdict_class, freeze_scope) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    acid,
                    asid,
                    host_session_id,
                    request_id,
                    fingerprint_phrase,
                    kind,
                    frozen_at,
                    expires_at,
                    user_id,
                    stored_class,
                    stored_scope,
                ),
            )
            conn.commit()
        return SessionFreeze(
            session_id=session_id,
            request_id=request_id,
            fingerprint_phrase=fingerprint_phrase,
            kind=kind,
            frozen_at=frozen_at,
            expires_at=expires_at,
            host_session_id=host_session_id,
            user_id=user_id,
            agent_context_id=acid,
            aidocs_session_id=asid,
            verdict_class=stored_class,
            freeze_scope=stored_scope,
        )

    def get_active_freeze(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
        operator_user_id: str = "",
        agent_id: str = "",
        now: float | None = None,
    ) -> SessionFreeze | None:
        """Return the active freeze for a session, or None if not frozen.

        Caller is responsible for honoring the kind:
          - self_approve: resolve on next UPS
          - admin_escalation: poll escalation_store for admin decision

        #588 D3: rows past their expires_at are REAPED here, before the
        lookup, and the reaping is recorded in session_freeze_expiry.
        The boot-time sweep alone was not enough — an unattended session
        never reboots, which is exactly the case that died.

        #879 B1 — ``agent_id`` is READ ON THE SAME AXIS IT IS WRITTEN. There
        is deliberately NO fallback to the parent key when the subagent key
        finds nothing: matching the parent's row is precisely the defect this
        fixes (one lane agent's lockdown stopping its siblings). It is also
        structurally incapable of recovering anything, because a subagent's
        pre-v2 rows were never keyed distinctly — they carry the parent key,
        which the PARENT still derives byte-identically and can still find,
        clear and expire. That is why this store needs no dual-read window.
        """
        self.init_db(project_root)
        acid, _ = self._derive_keys(
            project_root,
            session_id=session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
            agent_id=agent_id,
        )
        _cols = (
            "SELECT session_id, agent_context_id, aidocs_session_id, "
            "host_session_id, request_id, fingerprint_phrase, kind, "
            "frozen_at, expires_at, user_id, verdict_class, freeze_scope "
            "FROM session_freeze "
        )
        # #588 D1: what it takes for a row to bind an actor OTHER than its
        # owner. Was `agent_context_id = ''` — i.e. "the writer could not
        # name anyone, so it binds everyone". Now it is a DECLARED
        # session scope; the legacy clause survives only for rows minted
        # before the column existed, so this migration lifts no live lock.
        _session_bound = (
            "(session_id = ? AND (freeze_scope = ? "
            "OR (freeze_scope = '' AND agent_context_id = '')))"
        )
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            # #588 D3: reap first, then look. A row past its TTL must not
            # answer "frozen" on ANY of the three branches below.
            try:
                self._reap_expired(conn, _iso(time.time() if now is None else float(now)))
            except sqlite3.Error:
                # Reaping is the recovery path, not the security decision:
                # if it fails the freeze simply stays in force, which is
                # the fail-closed direction.
                pass
            if operator_user_id:
                # Operator-wide (security.freeze_all_sessions_on_malicious_intent):
                # frozen if THIS actor has a freeze OR the SAME authenticated
                # operator has a malicious-intent (hostile-operator-prompt) freeze
                # in ANY session. The actor's own row is preferred in the order
                # so local context is returned when present.
                # AGENT-scoped (operator 2026-07-15, agent-identity invariant):
                # a frozen agent stays frozen after switching work sessions, so
                # match on agent_context_id (the agent) — NOT session_id — when
                # the agent identity is known. A session-scoped row still binds
                # it; ANOTHER ACTOR's row no longer does (#588 D1).
                if acid:
                    row = conn.execute(
                        _cols
                        + "WHERE agent_context_id = ? "
                        "   OR " + _session_bound + " "
                        "   OR (user_id = ? AND user_id != '' AND kind = ?) "
                        "ORDER BY (agent_context_id = ?) DESC, frozen_at DESC LIMIT 1",
                        (
                            acid,
                            session_id,
                            FREEZE_SCOPE_SESSION,
                            operator_user_id,
                            KIND_HOSTILE_OPERATOR_PROMPT,
                            acid,
                        ),
                    ).fetchone()
                else:
                    row = conn.execute(
                        _cols
                        + "WHERE " + _session_bound + " "
                        "   OR (user_id = ? AND user_id != '' AND kind = ?) "
                        "ORDER BY (session_id = ?) DESC, frozen_at DESC LIMIT 1",
                        (
                            session_id,
                            FREEZE_SCOPE_SESSION,
                            operator_user_id,
                            KIND_HOSTILE_OPERATOR_PROMPT,
                            session_id,
                        ),
                    ).fetchone()
            elif acid:
                # AGENT-scoped: the agent's OWN freeze follows it across work
                # sessions (agent_context_id), and a DECLARED session-scoped
                # freeze still binds it inside its own session.
                row = conn.execute(
                    _cols + "WHERE agent_context_id = ? "
                    "   OR " + _session_bound + " "
                    "ORDER BY (agent_context_id = ?) DESC, frozen_at DESC LIMIT 1",
                    (acid, session_id, FREEZE_SCOPE_SESSION, acid),
                ).fetchone()
            else:
                # No resolvable actor. #588 D1 fail-closed direction: such a
                # caller is still bound by anything DECLARED session-wide (it
                # is inside that session), and can neither inherit nor escape
                # another actor's row — it simply is not that actor.
                row = conn.execute(
                    _cols + "WHERE " + _session_bound + " ORDER BY frozen_at DESC LIMIT 1",
                    (session_id, FREEZE_SCOPE_SESSION),
                ).fetchone()

        if row is None:
            return None
        # The row survived the reap above, so it is inside its TTL.
        return SessionFreeze(
            session_id=str(row["session_id"]),
            request_id=str(row["request_id"]),
            fingerprint_phrase=str(row["fingerprint_phrase"]),
            kind=str(row["kind"]),
            frozen_at=str(row["frozen_at"]),
            expires_at=(str(row["expires_at"]) if row["expires_at"] is not None else None),
            host_session_id=str(row["host_session_id"]),
            user_id=str(row["user_id"]),
            agent_context_id=str(row["agent_context_id"]),
            aidocs_session_id=str(row["aidocs_session_id"]),
            verdict_class=str(row["verdict_class"] or ""),
            freeze_scope=str(row["freeze_scope"] or ""),
        )

    def clear_freeze(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str | None = None,
        host_kind: str = "claude_code",
        agent_id: str = "",
    ) -> bool:
        """Drop the freeze row. Returns True if a row was deleted,
        False when no freeze was active.

        When ``host_session_id`` is given, deletes ONLY that agent's row
        (the composite PK), so clearing one co-session agent's freeze does
        NOT lift another agent's lock — the per-agent isolation the PK
        promises now holds on the clear path too. When None, clears every
        row for the session (admin whole-session clear / legacy callers).

        #879 B1: ``agent_id`` keys the delete on the SAME axis ``set_freeze``
        wrote, so a subagent clears its own lock and not its parent's.
        """
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            if host_session_id is None:
                cur = conn.execute(
                    "DELETE FROM session_freeze WHERE session_id = ?",
                    (session_id,),
                )
            else:
                acid, _ = self._derive_keys(
                    project_root,
                    session_id=session_id,
                    host_session_id=host_session_id,
                    host_kind=host_kind,
                    agent_id=agent_id,
                )
                cur = conn.execute(
                    "DELETE FROM session_freeze "
                    "WHERE session_id = ? AND agent_context_id = ?",
                    (session_id, acid),
                )
            conn.commit()
            return cur.rowcount > 0

    def clear_freeze_by_request(
        self,
        project_root: Path,
        request_id: str,
    ) -> int:
        """Drop any freeze row pointing at this escalation request_id.

        Returns the number of rows deleted. Admin tools that hold a
        request_id (not a session_id) use this.
        """
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            # If this request points at an operator-wide malicious-intent freeze,
            # clearing it lifts the operator across ALL their sessions (one clear,
            # all sessions) — security.freeze_all_sessions_on_malicious_intent.
            target = conn.execute(
                "SELECT user_id, kind FROM session_freeze WHERE request_id = ? LIMIT 1",
                (request_id,),
            ).fetchone()
            cur = conn.execute(
                "DELETE FROM session_freeze WHERE request_id = ?",
                (request_id,),
            )
            deleted = int(cur.rowcount or 0)
            if (
                target is not None
                and str(target["kind"]) == KIND_HOSTILE_OPERATOR_PROMPT
                and str(target["user_id"] or "")
            ):
                cur2 = conn.execute(
                    "DELETE FROM session_freeze WHERE user_id = ? AND user_id != '' AND kind = ?",
                    (str(target["user_id"]), KIND_HOSTILE_OPERATOR_PROMPT),
                )
                deleted += int(cur2.rowcount or 0)
            conn.commit()
            return deleted

    def sweep_expired(self, project_root: Path, now: float | None = None) -> int:
        """Delete every freeze row whose expires_at is in the past.

        Called at MCP boot (lane 1.5, 2026-05-04) so a lock with a
        passed TTL cannot survive a server restart. #588 D3: the boot
        sweep is now the SECOND line of defence — get_active_freeze
        reaps lazily, because the session that died was never rebooted.
        Returns the number of rows deleted. Best-effort — never raises.
        """
        self.init_db(project_root)
        try:
            now_iso = _iso(time.time() if now is None else float(now))
            with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
                return self._reap_expired(conn, now_iso)
        except Exception:
            return 0

    def recent_expiry(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
        *,
        agent_id: str = "",
        unsurfaced_only: bool = False,
    ) -> ExpiredFreeze | None:
        """Most recent TTL expiry for this actor's scope, or None.

        None means "no freeze of yours ever aged out" — which is how an
        agent tells an EXPIRY apart from having never been frozen and
        from an operator clear (neither writes an expiry row).
        Read-only: an absent store answers None, it does not create one.

        #879 B1: ``agent_id`` keys this on the SAME axis the freeze row it
        came from was written on — ``_reap_expired`` copies that row's
        ``agent_context_id`` verbatim — so a subagent asks after its OWN
        expired lock. Absent, an honest None: never another actor's notice.
        """
        conn = self._read_conn(project_root)
        if conn is None:
            return None
        try:
            acid, _ = self._derive_keys(
                project_root,
                session_id=session_id,
                host_session_id=host_session_id,
                host_kind=host_kind,
                agent_id=agent_id,
            )
            sql = (
                "SELECT session_id, agent_context_id, request_id, kind, frozen_at, "
                "expires_at, expired_at, reason FROM session_freeze_expiry "
                "WHERE session_id = ? AND agent_context_id IN (?, '') "
            )
            if unsurfaced_only:
                sql += "AND surfaced_at = '' "
            sql += "ORDER BY id DESC LIMIT 1"
            row = conn.execute(sql, (session_id, acid)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if row is None:
            return None
        return ExpiredFreeze(
            session_id=str(row["session_id"]),
            agent_context_id=str(row["agent_context_id"] or ""),
            request_id=str(row["request_id"] or ""),
            kind=str(row["kind"] or ""),
            frozen_at=str(row["frozen_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
            expired_at=str(row["expired_at"] or ""),
            reason=str(row["reason"] or ""),
        )

    def take_expiry_notice(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
        agent_id: str = "",
        now: float | None = None,
    ) -> ExpiredFreeze | None:
        """Pop the un-surfaced expiry notice for this actor, if any.

        Stamps the row's ``surfaced_at`` column so the agent is told ONCE
        that its freeze aged out, instead of on every subsequent tool
        call. The stamp lives in the DATABASE, not in process memory:
        the session this TTL work exists for is the unattended one that
        died and resumed, and an in-memory one-shot would re-fire the
        whole notice on every restart. The row itself is never deleted —
        ``recent_expiry`` keeps answering for audit and for tests.
        """
        pending = self.recent_expiry(
            project_root,
            session_id,
            host_session_id,
            host_kind,
            # #879 B1: same axis as the row. Forwarded rather than dropped so
            # this cannot quietly pop a DIFFERENT actor's notice than the one
            # `recent_expiry` was asked about.
            agent_id=agent_id,
            unsurfaced_only=True,
        )
        if pending is None:
            return None
        stamped = _iso(time.time() if now is None else float(now))
        try:
            with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
                conn.execute(
                    "UPDATE session_freeze_expiry SET surfaced_at = ? "
                    "WHERE session_id = ? AND agent_context_id = ? AND surfaced_at = ''",
                    (stamped, pending.session_id, pending.agent_context_id),
                )
                conn.commit()
        except sqlite3.Error:
            pass
        return pending

    @staticmethod
    def _is_past_ttl(expires_at: str | None, now: float | None = None) -> bool:
        """#588 D3: the same expiry question the reaper asks, for the
        READ-ONLY listers — they cannot delete, but they must not report
        an aged-out row as an active freeze either. A row the reaper
        would take is already dead; showing it is how a dashboard and a
        gate disagree about whether a session is frozen.
        """
        if not expires_at:
            return False
        return str(expires_at) <= _iso(time.time() if now is None else float(now))

    @staticmethod
    def _verdict_class_col(conn: sqlite3.Connection) -> str:
        """SELECT fragment for verdict_class, or a literal '' on a store that
        predates the #571 column.

        The READ-ONLY accessors (``_read_conn``) deliberately never migrate, so
        they can meet a pre-#571 table. Naming a missing column there would
        raise sqlite3.Error and the caller's ``except`` would report "no
        freeze" — turning a live lockdown invisible. Degrade to '' instead,
        which the fail-closed reader treats as security-class.
        """
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_freeze)").fetchall()}
        except sqlite3.Error:
            return "'' AS verdict_class"
        return "verdict_class" if "verdict_class" in cols else "'' AS verdict_class"

    @staticmethod
    def _freeze_scope_col(conn: sqlite3.Connection) -> str:
        """SELECT fragment for freeze_scope, or a literal '' on a store that
        predates the #588 D1 column — same degrade-don't-raise reasoning as
        ``_verdict_class_col``. '' reads as "legacy row", whose scope the
        readers infer from the key exactly as they did before.
        """
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_freeze)").fetchall()}
        except sqlite3.Error:
            return "'' AS freeze_scope"
        return "freeze_scope" if "freeze_scope" in cols else "'' AS freeze_scope"

    def list_active_freezes(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
    ) -> list[SessionFreeze]:
        """Enumerate active freeze rows. With session_id=None,
        returns every active freeze in the project; otherwise
        filters to that session (0 or 1 row in practice).

        Q2 doctrine 2026-05-04: NO automatic expiration sweep. A
        malicious-detected lockdown must NOT dissolve on a clock —
        only operator/admin action clears it.

        READ-ONLY (#553): an absent store means "no freezes", never a CREATED
        store. cli._resolve_admin_root calls this for EVERY registered project
        while hunting a freeze's owner, so creating here littered all of them
        (and whatever cwd the fallback resolved to) with .MEMORY/.index trees —
        which the doctor then reports as `half_init`.
        """
        rows: list[SessionFreeze] = []
        conn = self._read_conn(project_root)
        if conn is None:
            return rows
        try:
            if session_id is not None:
                cur = conn.execute(
                    "SELECT session_id, agent_context_id, aidocs_session_id,"
                    " host_session_id, request_id, fingerprint_phrase, kind,"
                    f" frozen_at, expires_at, user_id, {self._verdict_class_col(conn)},"
                    f" {self._freeze_scope_col(conn)}"
                    " FROM session_freeze WHERE session_id = ?",
                    (session_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT session_id, agent_context_id, aidocs_session_id,"
                    " host_session_id, request_id, fingerprint_phrase, kind,"
                    f" frozen_at, expires_at, user_id, {self._verdict_class_col(conn)},"
                    f" {self._freeze_scope_col(conn)}"
                    " FROM session_freeze",
                )
            for row in cur.fetchall():
                if self._is_past_ttl(row["expires_at"]):
                    continue  # #588 D3: dead row, not an active freeze.
                rows.append(
                    SessionFreeze(
                        session_id=str(row["session_id"]),
                        request_id=str(row["request_id"]),
                        fingerprint_phrase=str(row["fingerprint_phrase"]),
                        kind=str(row["kind"]),
                        frozen_at=str(row["frozen_at"]),
                        expires_at=(
                            str(row["expires_at"]) if row["expires_at"] is not None else None
                        ),
                        host_session_id=str(row["host_session_id"]),
                        user_id=str(row["user_id"]),
                        agent_context_id=str(row["agent_context_id"]),
                        aidocs_session_id=str(row["aidocs_session_id"]),
                        verdict_class=str(row["verdict_class"] or ""),
                        freeze_scope=str(row["freeze_scope"] or ""),
                    ),
                )
        except sqlite3.Error:
            # Present but schemaless/foreign DB → empty, and NOT repaired here.
            return []
        finally:
            conn.close()
        return rows

    def get_active_freeze_by_id(
        self,
        project_root: Path,
        freeze_id: str,
    ) -> SessionFreeze | None:
        """Resolve a freeze by its `request_id` (its stable id).
        Admin tools prefer this over session_id — unambiguous by
        construction.

        Q2 doctrine 2026-05-04: NO automatic expiration sweep on
        admin lookups. Operator/admin clears explicitly.
        """
        # READ-ONLY (#553): absent store → "no freeze", never a created store.
        conn = self._read_conn(project_root)
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT session_id, agent_context_id, aidocs_session_id, "
                "host_session_id, request_id, fingerprint_phrase, kind, "
                f"frozen_at, expires_at, user_id, {self._verdict_class_col(conn)}, "
                f"{self._freeze_scope_col(conn)} "
                "FROM session_freeze WHERE request_id = ?",
                (freeze_id,),
            ).fetchone()
        except sqlite3.Error:
            # DB present but schemaless/foreign — empty, and NOT repaired here.
            return None
        finally:
            conn.close()
        if row is None:
            return None
        if self._is_past_ttl(row["expires_at"]):
            # #588 D3: an admin lookup must agree with the gate about
            # whether this freeze is still in force.
            return None
        return SessionFreeze(
            session_id=str(row["session_id"]),
            request_id=str(row["request_id"]),
            fingerprint_phrase=str(row["fingerprint_phrase"]),
            kind=str(row["kind"]),
            frozen_at=str(row["frozen_at"]),
            expires_at=(str(row["expires_at"]) if row["expires_at"] is not None else None),
            host_session_id=str(row["host_session_id"]),
            user_id=str(row["user_id"]),
            agent_context_id=str(row["agent_context_id"]),
            aidocs_session_id=str(row["aidocs_session_id"]),
            verdict_class=str(row["verdict_class"] or ""),
            freeze_scope=str(row["freeze_scope"] or ""),
        )

    def snapshot_prompt_submit_state(
        self,
        project_root: Path,
        *,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
    ) -> dict[str, object]:
        """Capture this work-session/agent freeze scope; errors propagate."""
        from .prompt_submit_store_snapshot import capture_scoped_rows

        self.init_db(project_root)
        agent_context_id, _ = self._derive_keys(
            project_root,
            session_id=session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
        )
        scopes = {
            "session_freeze": (
                "session_id = ? AND agent_context_id IN (?, '')",
                (session_id, agent_context_id),
            )
        }
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            return capture_scoped_rows(conn, scopes)

    def restore_prompt_submit_state(
        self,
        project_root: Path,
        snapshot: dict[str, object],
        *,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
    ) -> None:
        """Restore exactly this freeze scope, deleting rows created in T."""
        from .prompt_submit_store_snapshot import restore_scoped_rows

        self.init_db(project_root)
        agent_context_id, _ = self._derive_keys(
            project_root,
            session_id=session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
        )
        scopes = {
            "session_freeze": (
                "session_id = ? AND agent_context_id IN (?, '')",
                (session_id, agent_context_id),
            )
        }
        # BORROWED HANDLE (#756) -- see EscalationStore.restore_prompt_submit_state
        # for the full reasoning. restore_scoped_rows opens `with conn:` on the
        # handle it is GIVEN; a ClosingConnection would close it there and the
        # prompt-submit fail-open would turn the resulting error into None,
        # degrading a REFUSAL into "carry on". This method owns the handle and
        # closes it itself.
        conn = _canonical_connect(self.db_path(project_root), row_factory=False)
        conn._aidocs_borrowed = True  # noqa: SLF001 -- our own subclass
        try:
            with conn:
                restore_scoped_rows(conn, scopes, snapshot)
        finally:
            conn.close()
