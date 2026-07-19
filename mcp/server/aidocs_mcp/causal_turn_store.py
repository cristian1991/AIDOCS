"""Causal Turn Context persistence — frozen entities of #467 (WAR AU).

Implements the spec's remaining durable entities against the kingdom DB
(``.MEMORY/.index/aidocs.sqlite3`` — the SAME file ``execution_events``
lives in, so ``ExecutionIndexStore.record_event_on_connection`` can resolve
the active instruction on the caller's own connection):

* ``causal_turns``        — the TurnState machine rows (one per minted turn);
* ``instruction_events``  — immutable, per-turn hash-chained instruction
                            revisions (operator prompt + every edit /
                            interrupt / override as a first-class event);
* ``interrupt_events``    — interrupts as governance events, durably
                            recorded and causally linked to their
                            instruction event;
* ``turn_seals``          — the per-turn terminal seal with
                            ``event_merkle_root`` over the turn's audit
                            rows, verifiable via :meth:`verify_turn_seal`;
* ``orphan_resolutions``  — the recovery classification for durable
                            attempts (``tool_call_started``) that never got
                            an outcome row.

Ordering / fail-direction:
* ``open_turn`` is called (best-effort) from
  ``SessionQueryGateStore.rotate_current_turn_id`` — the causal-turn mint
  rides the existing one-write-per-operator-turn budget. Mint
  fail-direction (best-effort at UPS; the tool chokepoint's intent audit
  stays the fail-closed layer) is an OPEN Empire question carried from War
  AM — this store preserves the current behavior and decides nothing.
* ``recover_open_turns`` is the heavy pass — on-demand / background only
  (AQ law); it is NEVER wired into the per-event hot path.
* Instruction events and interrupts are append-only: there is no update
  API, and appending to a SEALED turn is refused (the sealed event range
  is committed).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from ._sqlite_index_store_base import SQLiteIndexStoreBase
from .causal_turn_contract import (
    INTERRUPT_INSTRUCTION_KINDS,
    TERMINAL_STATE_FOR_SEAL_REASON,
    IllegalTurnTransition,
    InstructionKind,
    OrphanResolution,
    SealReason,
    TurnState,
    assert_turn_transition,
    compute_event_merkle_root,
)

#: Attempt/outcome event kinds at the local tool chokepoint (mcp_server
#: wrapper). The ``tool_call_started`` row IS the spec's durable
#: ToolAttempt; the two result kinds are its outcome legs.
_ATTEMPT_KIND = "tool_call_started"
_OUTCOME_KINDS = ("tool_call_completed", "tool_call_failed")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CausalTurnStore(SQLiteIndexStoreBase):
    """Durable Causal Turn Context state in the kingdom index DB."""

    # ── schema ──

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS causal_turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open',
                    instruction_revision INTEGER NOT NULL DEFAULT 0,
                    current_instruction_id TEXT NOT NULL DEFAULT '',
                    superseded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_causal_turns_session
                    ON causal_turns(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_causal_turns_state
                    ON causal_turns(state);

                CREATE TABLE IF NOT EXISTS instruction_events (
                    instruction_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    instruction_revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    parent_instruction_id TEXT NOT NULL DEFAULT '',
                    supersedes_instruction_id TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL DEFAULT '',
                    actor_role TEXT NOT NULL DEFAULT '',
                    origin_channel TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT '',
                    protected_content_reference TEXT NOT NULL DEFAULT '',
                    previous_event_hash TEXT NOT NULL DEFAULT '',
                    row_hash TEXT NOT NULL DEFAULT '',
                    UNIQUE (turn_id, instruction_revision)
                );

                CREATE TABLE IF NOT EXISTS interrupt_events (
                    interrupt_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    instruction_id TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL DEFAULT '',
                    actor_role TEXT NOT NULL DEFAULT '',
                    reason_hash TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    affected_attempt_ids TEXT NOT NULL DEFAULT '[]',
                    cancellation_requested INTEGER NOT NULL DEFAULT 0,
                    cancellation_observed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS turn_seals (
                    turn_id TEXT PRIMARY KEY,
                    terminal_state TEXT NOT NULL,
                    first_event_sequence INTEGER NOT NULL DEFAULT -1,
                    last_event_sequence INTEGER NOT NULL DEFAULT -1,
                    event_merkle_root TEXT NOT NULL DEFAULT '',
                    instruction_revision INTEGER NOT NULL DEFAULT 0,
                    completed_attempts INTEGER NOT NULL DEFAULT 0,
                    open_attempts INTEGER NOT NULL DEFAULT 0,
                    indeterminate_attempts INTEGER NOT NULL DEFAULT 0,
                    seal_reason TEXT NOT NULL,
                    sealed_at TEXT NOT NULL,
                    seal_hash TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS orphan_resolutions (
                    attempt_event_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    resolution TEXT NOT NULL,
                    idempotent INTEGER NOT NULL DEFAULT 0,
                    retry_attempt_id TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '',
                    resolved_by TEXT NOT NULL DEFAULT '',
                    classified_at TEXT NOT NULL
                );
                """,
            )

    # ── turn lifecycle ──

    def open_turn(
        self,
        project_root: Path,
        session_id: str,
        turn_id: str,
        *,
        content_hash: str = "",
        actor_id: str = "",
        actor_role: str = "",
        origin_channel: str = "user_prompt_submit",
        protected_content_reference: str = "",
    ) -> dict[str, Any]:
        """Open a causal turn for a freshly minted server turn id.

        In ONE transaction: every prior un-sealed turn of the session that
        is still pre-terminal (open / executing / interrupt_pending) is
        marked ABANDONED with ``superseded_by`` = the new turn (a new
        accepted operator instruction ends the prior causal boundary; the
        recovery pass seals it later), then the new turn row is inserted in
        state OPEN and its revision-1 UserPrompt instruction event is
        appended. ``turn_id`` comes from the server mint in
        ``SessionQueryGateStore.rotate_current_turn_id`` — callers never
        invent one.
        """
        sid = str(session_id or "").strip()
        tid = str(turn_id or "").strip()
        if not sid or not tid:
            raise ValueError("open_turn requires a session_id and a minted turn_id")
        self.init_db(project_root)
        now = self._timestamp()
        resolved_actor = actor_id or self._resolve_actor(project_root)
        with self.session(project_root) as conn:
            for row in conn.execute(
                "SELECT turn_id, state FROM causal_turns "
                "WHERE session_id = ? AND state IN (?, ?, ?)",
                (
                    sid,
                    TurnState.OPEN.value,
                    TurnState.EXECUTING.value,
                    TurnState.INTERRUPT_PENDING.value,
                ),
            ).fetchall():
                assert_turn_transition(TurnState(row["state"]), TurnState.ABANDONED)
                conn.execute(
                    "UPDATE causal_turns SET state = ?, superseded_by = ?, "
                    "updated_at = ? WHERE turn_id = ?",
                    (TurnState.ABANDONED.value, tid, now, row["turn_id"]),
                )
            conn.execute(
                "INSERT OR IGNORE INTO causal_turns ("
                "turn_id, session_id, state, instruction_revision, "
                "current_instruction_id, superseded_by, created_at, updated_at"
                ") VALUES (?, ?, ?, 0, '', '', ?, ?)",
                (tid, sid, TurnState.OPEN.value, now, now),
            )
            instruction = self._append_instruction_on_connection(
                conn,
                turn_id=tid,
                kind=InstructionKind.USER_PROMPT,
                content_hash=content_hash,
                actor_id=resolved_actor,
                actor_role=actor_role,
                origin_channel=origin_channel,
                protected_content_reference=protected_content_reference,
                received_at=now,
            )
        return {"turn_id": tid, "session_id": sid, "state": TurnState.OPEN.value,
                "instruction": instruction}

    def get_turn(self, project_root: Path, turn_id: str) -> dict[str, Any] | None:
        self.init_db(project_root)
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT * FROM causal_turns WHERE turn_id = ?",
                (str(turn_id or "").strip(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def transition_turn(
        self,
        project_root: Path,
        turn_id: str,
        new_state: TurnState | str,
    ) -> dict[str, Any]:
        """Move a turn along the frozen machine; illegal edges raise."""
        self.init_db(project_root)
        target = TurnState(new_state)
        with self.session(project_root) as conn:
            return self._transition_on_connection(conn, turn_id, target)

    def _transition_on_connection(
        self,
        conn: sqlite3.Connection,
        turn_id: str,
        target: TurnState,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT state FROM causal_turns WHERE turn_id = ?",
            (str(turn_id or "").strip(),),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown turn_id {turn_id!r}")
        current = TurnState(row["state"])
        assert_turn_transition(current, target)
        conn.execute(
            "UPDATE causal_turns SET state = ?, updated_at = ? WHERE turn_id = ?",
            (target.value, self._timestamp(), turn_id),
        )
        return {"turn_id": turn_id, "from": current.value, "to": target.value}

    # ── instruction events (immutable, per-turn hash chain) ──

    @staticmethod
    def _instruction_row_hash(
        *,
        instruction_id: str,
        turn_id: str,
        instruction_revision: int,
        kind: str,
        parent_instruction_id: str,
        supersedes_instruction_id: str,
        actor_id: str,
        actor_role: str,
        origin_channel: str,
        received_at: str,
        content_hash: str,
        protected_content_reference: str,
        previous_event_hash: str,
    ) -> str:
        """Domain-separated content hash of one instruction event, chained
        to the turn's previous instruction event (``instr-v1|`` prefix)."""
        raw = (
            f"instr-v1|{instruction_id}|{turn_id}|{int(instruction_revision)}"
            f"|{kind}|{parent_instruction_id}|{supersedes_instruction_id}"
            f"|{actor_id}|{actor_role}|{origin_channel}|{received_at}"
            f"|{content_hash}|{protected_content_reference}|{previous_event_hash}"
        )
        return _sha256_hex(raw)

    def _append_instruction_on_connection(
        self,
        conn: sqlite3.Connection,
        *,
        turn_id: str,
        kind: InstructionKind,
        content_hash: str,
        actor_id: str,
        actor_role: str,
        origin_channel: str,
        protected_content_reference: str,
        received_at: str,
        parent_instruction_id: str = "",
    ) -> dict[str, Any]:
        turn = conn.execute(
            "SELECT state, instruction_revision, current_instruction_id "
            "FROM causal_turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if turn is None:
            raise ValueError(f"unknown turn_id {turn_id!r}")
        if TurnState(turn["state"]) is TurnState.SEALED:
            raise IllegalTurnTransition(
                f"turn {turn_id!r} is sealed; its event range is committed — "
                "a new instruction requires a new turn"
            )
        revision = int(turn["instruction_revision"]) + 1
        supersedes = str(turn["current_instruction_id"] or "")
        prev = conn.execute(
            "SELECT row_hash FROM instruction_events WHERE turn_id = ? "
            "ORDER BY instruction_revision DESC LIMIT 1",
            (turn_id,),
        ).fetchone()
        previous_event_hash = str(prev["row_hash"]) if prev is not None else ""
        instruction_id = f"instr-{uuid4().hex}"
        row_hash = self._instruction_row_hash(
            instruction_id=instruction_id,
            turn_id=turn_id,
            instruction_revision=revision,
            kind=InstructionKind(kind).value,
            parent_instruction_id=parent_instruction_id,
            supersedes_instruction_id=supersedes,
            actor_id=actor_id,
            actor_role=actor_role,
            origin_channel=origin_channel,
            received_at=received_at,
            content_hash=content_hash,
            protected_content_reference=protected_content_reference,
            previous_event_hash=previous_event_hash,
        )
        conn.execute(
            "INSERT INTO instruction_events ("
            "instruction_id, turn_id, instruction_revision, kind, "
            "parent_instruction_id, supersedes_instruction_id, actor_id, "
            "actor_role, origin_channel, received_at, content_hash, "
            "protected_content_reference, previous_event_hash, row_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                instruction_id,
                turn_id,
                revision,
                InstructionKind(kind).value,
                parent_instruction_id,
                supersedes,
                actor_id,
                actor_role,
                origin_channel,
                received_at,
                content_hash,
                protected_content_reference,
                previous_event_hash,
                row_hash,
            ),
        )
        conn.execute(
            "UPDATE causal_turns SET instruction_revision = ?, "
            "current_instruction_id = ?, updated_at = ? WHERE turn_id = ?",
            (revision, instruction_id, received_at, turn_id),
        )
        return {
            "instruction_id": instruction_id,
            "turn_id": turn_id,
            "instruction_revision": revision,
            "kind": InstructionKind(kind).value,
            "supersedes_instruction_id": supersedes,
        }

    def record_instruction(
        self,
        project_root: Path,
        turn_id: str,
        kind: InstructionKind | str,
        *,
        content: str | None = None,
        content_hash: str = "",
        actor_id: str = "",
        actor_role: str = "",
        origin_channel: str = "",
        parent_instruction_id: str = "",
        protected_content_reference: str = "",
    ) -> dict[str, Any]:
        """Append an immutable instruction event to an un-sealed turn.

        An instruction EDIT never mutates a prior event — it lands as a new
        revision superseding the turn's current instruction (spec
        Instruction Revision). ``content`` (if given) is hashed here and
        NEVER stored raw; identity stays provable while the body stays
        protectable.
        """
        resolved_kind = InstructionKind(kind)
        resolved_hash = content_hash or (_sha256_hex(content) if content else "")
        self.init_db(project_root)
        with self.session(project_root) as conn:
            return self._append_instruction_on_connection(
                conn,
                turn_id=str(turn_id or "").strip(),
                kind=resolved_kind,
                content_hash=resolved_hash,
                actor_id=actor_id or self._resolve_actor(project_root),
                actor_role=actor_role,
                origin_channel=origin_channel,
                protected_content_reference=protected_content_reference,
                received_at=self._timestamp(),
                parent_instruction_id=parent_instruction_id,
            )

    def list_instructions(self, project_root: Path, turn_id: str) -> list[dict[str, Any]]:
        self.init_db(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT * FROM instruction_events WHERE turn_id = ? "
                "ORDER BY instruction_revision ASC",
                (str(turn_id or "").strip(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def verify_instruction_chain(self, project_root: Path, turn_id: str) -> dict[str, Any]:
        """Recompute the per-turn instruction hash chain; report the first
        broken link. Tamper with any stored instruction field and the chain
        breaks at that revision."""
        rows = self.list_instructions(project_root, turn_id)
        expected_prev = ""
        for idx, row in enumerate(rows):
            if str(row["previous_event_hash"] or "") != expected_prev:
                return {"verified": False, "total": len(rows), "broken_at": idx,
                        "broken_instruction_id": row["instruction_id"]}
            recomputed = self._instruction_row_hash(
                instruction_id=str(row["instruction_id"]),
                turn_id=str(row["turn_id"]),
                instruction_revision=int(row["instruction_revision"]),
                kind=str(row["kind"]),
                parent_instruction_id=str(row["parent_instruction_id"] or ""),
                supersedes_instruction_id=str(row["supersedes_instruction_id"] or ""),
                actor_id=str(row["actor_id"] or ""),
                actor_role=str(row["actor_role"] or ""),
                origin_channel=str(row["origin_channel"] or ""),
                received_at=str(row["received_at"]),
                content_hash=str(row["content_hash"] or ""),
                protected_content_reference=str(row["protected_content_reference"] or ""),
                previous_event_hash=str(row["previous_event_hash"] or ""),
            )
            if recomputed != str(row["row_hash"] or ""):
                return {"verified": False, "total": len(rows), "broken_at": idx,
                        "broken_instruction_id": row["instruction_id"]}
            expected_prev = recomputed
        return {"verified": True, "total": len(rows), "broken_at": None}

    # ── interrupts as governance events ──

    def record_interrupt(
        self,
        project_root: Path,
        turn_id: str,
        *,
        kind: InstructionKind | str,
        actor_id: str = "",
        actor_role: str = "",
        reason: str = "",
        origin_channel: str = "",
        affected_attempt_ids: list[str] | tuple[str, ...] = (),
        cancellation_requested: bool = True,
    ) -> dict[str, Any]:
        """Durably record an interrupt BEFORE it is delivered to the agent.

        One transaction appends the interrupt's instruction event (advancing
        the turn's instruction revision — an interrupt is an instruction
        mutation, never a silent edit), moves the turn to InterruptPending,
        and inserts the interrupt row causally linked to that instruction.

        DUPLICATE DELIVERY is idempotent: the interrupt id is content-
        addressed over (turn, kind, actor, reason_hash); a re-delivered
        identical interrupt returns the existing record without advancing
        the revision or re-transitioning the turn.

        A co-conductor interrupt MUST identify its actor (invariant 5).
        """
        resolved_kind = InstructionKind(kind)
        if resolved_kind not in INTERRUPT_INSTRUCTION_KINDS:
            raise ValueError(
                f"{resolved_kind.value!r} is not an interrupt instruction kind"
            )
        actor = str(actor_id or "").strip()
        if resolved_kind is InstructionKind.CO_CONDUCTOR_INTERRUPT and not actor:
            raise ValueError(
                "a co-conductor interrupt must identify the co-conductor actor"
            )
        tid = str(turn_id or "").strip()
        reason_hash = _sha256_hex(reason) if reason else ""
        interrupt_id = "int-" + _sha256_hex(
            f"{tid}|{resolved_kind.value}|{actor}|{reason_hash}"
        )[:32]
        self.init_db(project_root)
        now = self._timestamp()
        with self.session(project_root) as conn:
            existing = conn.execute(
                "SELECT * FROM interrupt_events WHERE interrupt_id = ?",
                (interrupt_id,),
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "duplicate_delivery": True}
            instruction = self._append_instruction_on_connection(
                conn,
                turn_id=tid,
                kind=resolved_kind,
                content_hash=reason_hash,
                actor_id=actor,
                actor_role=actor_role,
                origin_channel=origin_channel,
                protected_content_reference="",
                received_at=now,
            )
            state_row = conn.execute(
                "SELECT state FROM causal_turns WHERE turn_id = ?", (tid,)
            ).fetchone()
            if TurnState(state_row["state"]) is not TurnState.INTERRUPT_PENDING:
                self._transition_on_connection(conn, tid, TurnState.INTERRUPT_PENDING)
            else:
                # Distinct second interrupt while draining: the self-loop
                # edge (contract) — recorded, no state change needed.
                assert_turn_transition(
                    TurnState.INTERRUPT_PENDING, TurnState.INTERRUPT_PENDING
                )
            conn.execute(
                "INSERT INTO interrupt_events ("
                "interrupt_id, turn_id, instruction_id, actor_id, actor_role, "
                "reason_hash, received_at, affected_attempt_ids, "
                "cancellation_requested, cancellation_observed"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    interrupt_id,
                    tid,
                    instruction["instruction_id"],
                    actor,
                    actor_role,
                    reason_hash,
                    now,
                    json.dumps([str(a) for a in affected_attempt_ids or []]),
                    1 if cancellation_requested else 0,
                ),
            )
        return {
            "interrupt_id": interrupt_id,
            "turn_id": tid,
            "instruction_id": instruction["instruction_id"],
            "instruction_revision": instruction["instruction_revision"],
            "actor_id": actor,
            "reason_hash": reason_hash,
            "duplicate_delivery": False,
        }

    def mark_cancellation_observed(self, project_root: Path, interrupt_id: str) -> None:
        """Record that cancellation of the affected attempts was OBSERVED
        (spec InterruptEvent.cancellation_observed) — the one legal
        post-hoc field on an interrupt record."""
        self.init_db(project_root)
        with self.session(project_root) as conn:
            conn.execute(
                "UPDATE interrupt_events SET cancellation_observed = 1 "
                "WHERE interrupt_id = ?",
                (str(interrupt_id or "").strip(),),
            )

    def list_interrupts(self, project_root: Path, turn_id: str) -> list[dict[str, Any]]:
        self.init_db(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT * FROM interrupt_events WHERE turn_id = ? "
                "ORDER BY received_at ASC, interrupt_id ASC",
                (str(turn_id or "").strip(),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── orphaned attempts + recovery classification ──

    def _attempt_rows(
        self,
        conn: sqlite3.Connection,
        *,
        turn_id: str = "",
        session_id: str = "",
    ) -> list[sqlite3.Row]:
        clauses = ["event_kind IN (?, ?, ?)"]
        params: list[Any] = [_ATTEMPT_KIND, *_OUTCOME_KINDS]
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        try:
            return conn.execute(
                "SELECT event_id, event_kind, capability_name, session_id, "
                "turn_id, chain_seq, observed_at FROM execution_events "
                f"WHERE {' AND '.join(clauses)} ORDER BY chain_seq ASC",
                params,
            ).fetchall()
        except Exception:
            return []

    @staticmethod
    def _pair_orphans(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Pair attempt rows with outcome rows per capability in chain
        order (LIFO within a capability — nested calls resolve innermost
        first). Leftover attempts are the orphans: durable intent with no
        recorded outcome."""
        open_by_cap: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            cap = str(row["capability_name"] or "")
            if row["event_kind"] == _ATTEMPT_KIND:
                open_by_cap.setdefault(cap, []).append(row)
            elif open_by_cap.get(cap):
                open_by_cap[cap].pop()
        orphans: list[dict[str, Any]] = []
        for stack in open_by_cap.values():
            for row in stack:
                orphans.append(
                    {
                        "attempt_event_id": str(row["event_id"]),
                        "capability_name": str(row["capability_name"] or ""),
                        "session_id": str(row["session_id"] or ""),
                        "turn_id": str(row["turn_id"] or ""),
                        "chain_seq": int(row["chain_seq"] or 0),
                        "observed_at": str(row["observed_at"] or ""),
                    }
                )
        orphans.sort(key=lambda o: o["chain_seq"])
        return orphans

    def list_orphan_attempts(
        self,
        project_root: Path,
        *,
        turn_id: str = "",
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        """Durable ``tool_call_started`` rows with no outcome row — the
        spec's ORPHANED ATTEMPTS, never deleted, never assumed failed.
        Each carries its existing resolution (if classified)."""
        self.init_db(project_root)
        with self.session(project_root) as conn:
            orphans = self._pair_orphans(
                self._attempt_rows(conn, turn_id=turn_id, session_id=session_id)
            )
            for orphan in orphans:
                row = conn.execute(
                    "SELECT resolution, resolved_by, classified_at "
                    "FROM orphan_resolutions WHERE attempt_event_id = ?",
                    (orphan["attempt_event_id"],),
                ).fetchone()
                orphan["resolution"] = str(row["resolution"]) if row else ""
        return orphans

    def resolve_orphan(
        self,
        project_root: Path,
        attempt_event_id: str,
        resolution: OrphanResolution | str,
        *,
        idempotent: bool = False,
        retry_attempt_id: str = "",
        evidence: str = "",
        resolved_by: str = "operator",
        turn_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Classify one orphaned attempt (spec OrphanResolution taxonomy).

        Non-negotiables enforced here:
        * SAFELY_RETRIED requires ``idempotent=True`` — a non-idempotent
          indeterminate attempt must NOT be retried (spec Crash Recovery);
        * SAFELY_RETRIED requires ``retry_attempt_id`` — retries reference
          the original attempt with explicit idempotency semantics
          (invariant 13);
        * a PROVEN classification is immutable; only INDETERMINATE may be
          upgraded later when real evidence arrives.
        """
        resolved = OrphanResolution(resolution)
        if resolved is OrphanResolution.SAFELY_RETRIED:
            if not idempotent:
                raise ValueError(
                    "safely_retried is only lawful for an idempotent attempt — "
                    "non-idempotent indeterminate attempts must not be retried"
                )
            if not str(retry_attempt_id or "").strip():
                raise ValueError(
                    "safely_retried must reference the retry attempt "
                    "(retry_attempt_id) — retries reference the original attempt"
                )
        aid = str(attempt_event_id or "").strip()
        if not aid:
            raise ValueError("attempt_event_id is required")
        self.init_db(project_root)
        with self.session(project_root) as conn:
            existing = conn.execute(
                "SELECT resolution FROM orphan_resolutions WHERE attempt_event_id = ?",
                (aid,),
            ).fetchone()
            if existing is not None:
                prior = OrphanResolution(existing["resolution"])
                if prior is resolved:
                    return {"attempt_event_id": aid, "resolution": resolved.value,
                            "changed": False}
                if prior is not OrphanResolution.INDETERMINATE:
                    raise ValueError(
                        f"attempt {aid!r} already classified {prior.value!r}; "
                        "a proven resolution is immutable"
                    )
            conn.execute(
                "INSERT INTO orphan_resolutions ("
                "attempt_event_id, turn_id, session_id, resolution, idempotent, "
                "retry_attempt_id, evidence, resolved_by, classified_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(attempt_event_id) DO UPDATE SET "
                "resolution = excluded.resolution, "
                "idempotent = excluded.idempotent, "
                "retry_attempt_id = excluded.retry_attempt_id, "
                "evidence = excluded.evidence, "
                "resolved_by = excluded.resolved_by, "
                "classified_at = excluded.classified_at",
                (
                    aid,
                    str(turn_id or ""),
                    str(session_id or ""),
                    resolved.value,
                    1 if idempotent else 0,
                    str(retry_attempt_id or ""),
                    str(evidence or "")[:2000],
                    str(resolved_by or ""),
                    self._timestamp(),
                ),
            )
        return {"attempt_event_id": aid, "resolution": resolved.value, "changed": True}

    # ── turn seal ──

    @staticmethod
    def _seal_hash(
        *,
        turn_id: str,
        terminal_state: str,
        first_event_sequence: int,
        last_event_sequence: int,
        event_merkle_root: str,
        instruction_revision: int,
        completed_attempts: int,
        open_attempts: int,
        indeterminate_attempts: int,
        seal_reason: str,
        sealed_at: str,
    ) -> str:
        raw = (
            f"seal-v1|{turn_id}|{terminal_state}|{int(first_event_sequence)}"
            f"|{int(last_event_sequence)}|{event_merkle_root}"
            f"|{int(instruction_revision)}|{int(completed_attempts)}"
            f"|{int(open_attempts)}|{int(indeterminate_attempts)}"
            f"|{seal_reason}|{sealed_at}"
        )
        return _sha256_hex(raw)

    def _turn_event_leaves(
        self, conn: sqlite3.Connection, turn_id: str
    ) -> tuple[list[str], int, int, list[sqlite3.Row]]:
        """Row hashes (chain_seq order) + seq bounds for a turn's committed
        events, EXCLUDING ``turn_sealed`` rows (a seal event cannot be part
        of the range it commits)."""
        from .execution_index_store import ExecutionIndexStore

        try:
            rows = conn.execute(
                "SELECT * FROM execution_events WHERE turn_id = ? "
                "AND event_kind != 'turn_sealed' ORDER BY chain_seq ASC",
                (turn_id,),
            ).fetchall()
        except Exception:
            rows = []
        leaves = [ExecutionIndexStore._row_hash_from_stored_row(r) for r in rows]
        first_seq = int(rows[0]["chain_seq"]) if rows else -1
        last_seq = int(rows[-1]["chain_seq"]) if rows else -1
        return leaves, first_seq, last_seq, rows

    def _attempt_counts(
        self, conn: sqlite3.Connection, turn_id: str
    ) -> tuple[int, int, int]:
        """(completed, open, indeterminate) attempt counts for the seal.
        A classified orphan counts as indeterminate (when so resolved) or
        completed (proven/safely-retried); an unclassified orphan stays
        OPEN — visible, per invariant 20."""
        rows = self._attempt_rows(conn, turn_id=turn_id)
        started = sum(1 for r in rows if r["event_kind"] == _ATTEMPT_KIND)
        orphans = self._pair_orphans(rows)
        completed = started - len(orphans)
        open_attempts = 0
        indeterminate = 0
        for orphan in orphans:
            row = conn.execute(
                "SELECT resolution FROM orphan_resolutions WHERE attempt_event_id = ?",
                (orphan["attempt_event_id"],),
            ).fetchone()
            if row is None:
                open_attempts += 1
            elif row["resolution"] == OrphanResolution.INDETERMINATE.value:
                indeterminate += 1
            else:
                completed += 1
        return completed, open_attempts, indeterminate

    def seal_turn(
        self,
        project_root: Path,
        turn_id: str,
        seal_reason: SealReason | str,
        *,
        audit_seal_event: bool = True,
    ) -> dict[str, Any]:
        """Produce the turn's terminal seal (spec TurnSeal).

        Server-generated; a Stop hook may REQUEST a seal but is never the
        only mechanism (recovery calls this too). Idempotent: sealing a
        sealed turn returns the existing seal. The turn is driven through
        legal transitions to the reason's terminal state, the event Merkle
        root is computed over the turn's committed audit rows, and the seal
        row lands in one transaction with the state flip. When
        ``audit_seal_event`` is true a ``turn_sealed`` execution event is
        then appended (best-effort) so the seal itself is linked into the
        hash-bound session audit chain — the #467 "seal linkage".
        """
        reason = SealReason(seal_reason)
        tid = str(turn_id or "").strip()
        self.init_db(project_root)
        now = self._timestamp()
        with self.session(project_root) as conn:
            turn = conn.execute(
                "SELECT * FROM causal_turns WHERE turn_id = ?", (tid,)
            ).fetchone()
            if turn is None:
                raise ValueError(f"unknown turn_id {tid!r}")
            if TurnState(turn["state"]) is TurnState.SEALED:
                existing = conn.execute(
                    "SELECT * FROM turn_seals WHERE turn_id = ?", (tid,)
                ).fetchone()
                if existing is not None:
                    return {**dict(existing), "already_sealed": True}
            terminal = TERMINAL_STATE_FOR_SEAL_REASON[reason]
            state = TurnState(turn["state"])
            if state is not terminal and state is not TurnState.SEALED:
                if (
                    terminal is TurnState.INTERRUPTED
                    and state is not TurnState.INTERRUPT_PENDING
                ):
                    self._transition_on_connection(conn, tid, TurnState.INTERRUPT_PENDING)
                self._transition_on_connection(conn, tid, terminal)
            leaves, first_seq, last_seq, _rows = self._turn_event_leaves(conn, tid)
            merkle_root = compute_event_merkle_root(leaves)
            completed, open_attempts, indeterminate = self._attempt_counts(conn, tid)
            seal_hash = self._seal_hash(
                turn_id=tid,
                terminal_state=terminal.value,
                first_event_sequence=first_seq,
                last_event_sequence=last_seq,
                event_merkle_root=merkle_root,
                instruction_revision=int(turn["instruction_revision"]),
                completed_attempts=completed,
                open_attempts=open_attempts,
                indeterminate_attempts=indeterminate,
                seal_reason=reason.value,
                sealed_at=now,
            )
            conn.execute(
                "INSERT INTO turn_seals ("
                "turn_id, terminal_state, first_event_sequence, "
                "last_event_sequence, event_merkle_root, instruction_revision, "
                "completed_attempts, open_attempts, indeterminate_attempts, "
                "seal_reason, sealed_at, seal_hash"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(turn_id) DO NOTHING",
                (
                    tid,
                    terminal.value,
                    first_seq,
                    last_seq,
                    merkle_root,
                    int(turn["instruction_revision"]),
                    completed,
                    open_attempts,
                    indeterminate,
                    reason.value,
                    now,
                    seal_hash,
                ),
            )
            self._transition_on_connection(conn, tid, TurnState.SEALED)
        seal = {
            "turn_id": tid,
            "terminal_state": terminal.value,
            "first_event_sequence": first_seq,
            "last_event_sequence": last_seq,
            "event_merkle_root": merkle_root,
            "instruction_revision": int(turn["instruction_revision"]),
            "completed_attempts": completed,
            "open_attempts": open_attempts,
            "indeterminate_attempts": indeterminate,
            "seal_reason": reason.value,
            "sealed_at": now,
            "seal_hash": seal_hash,
            # Invariant 20: not 120%-sealed while any attempt lacks a
            # classified outcome — reported honestly, never hidden.
            "fully_classified": open_attempts == 0,
            "already_sealed": False,
        }
        if audit_seal_event:
            # Seal linkage: the seal enters the session's hash-bound audit
            # chain as its own event row (payload_json is folded into every
            # row-hash version), so erasing/tampering the seal is chain-
            # visible. Best-effort: a mirror/audit failure never unsseals.
            try:
                from .execution_index_store import ExecutionIndexStore

                ExecutionIndexStore().record_event(
                    project_root,
                    event_kind="turn_sealed",
                    source_kind="causal_turn_store",
                    session_id=str(turn["session_id"] or "") or None,
                    action_kind="seal",
                    target_entity=tid,
                    status=terminal.value,
                    payload={
                        "sealed_turn_id": tid,
                        "event_merkle_root": merkle_root,
                        "seal_reason": reason.value,
                        "terminal_state": terminal.value,
                        "seal_hash": seal_hash,
                        "open_attempts": open_attempts,
                        "indeterminate_attempts": indeterminate,
                    },
                )
            except Exception:
                pass
        return seal

    def get_seal(self, project_root: Path, turn_id: str) -> dict[str, Any] | None:
        self.init_db(project_root)
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT * FROM turn_seals WHERE turn_id = ?",
                (str(turn_id or "").strip(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def verify_turn_seal(self, project_root: Path, turn_id: str) -> dict[str, Any]:
        """Recompute the sealed turn's event Merkle root and seal hash from
        the stored rows; any post-seal tamper (row edit, row deletion,
        seal-field rewrite) breaks verification."""
        tid = str(turn_id or "").strip()
        self.init_db(project_root)
        with self.session(project_root) as conn:
            seal = conn.execute(
                "SELECT * FROM turn_seals WHERE turn_id = ?", (tid,)
            ).fetchone()
            if seal is None:
                return {"verified": False, "reason": "no_seal"}
            leaves, _first, _last, _rows = self._turn_event_leaves(conn, tid)
        recomputed_root = compute_event_merkle_root(leaves)
        if recomputed_root != str(seal["event_merkle_root"] or ""):
            return {
                "verified": False,
                "reason": "event_merkle_root_mismatch",
                "stored_root": str(seal["event_merkle_root"] or ""),
                "recomputed_root": recomputed_root,
            }
        recomputed_seal_hash = self._seal_hash(
            turn_id=tid,
            terminal_state=str(seal["terminal_state"]),
            first_event_sequence=int(seal["first_event_sequence"]),
            last_event_sequence=int(seal["last_event_sequence"]),
            event_merkle_root=str(seal["event_merkle_root"] or ""),
            instruction_revision=int(seal["instruction_revision"]),
            completed_attempts=int(seal["completed_attempts"]),
            open_attempts=int(seal["open_attempts"]),
            indeterminate_attempts=int(seal["indeterminate_attempts"]),
            seal_reason=str(seal["seal_reason"]),
            sealed_at=str(seal["sealed_at"]),
        )
        if recomputed_seal_hash != str(seal["seal_hash"] or ""):
            return {"verified": False, "reason": "seal_hash_mismatch"}
        return {"verified": True, "event_count": len(leaves)}

    # ── recovery worker (heavy pass — on-demand/background, AQ law) ──

    def recover_open_turns(
        self,
        project_root: Path,
        *,
        session_id: str = "",
        seal_reason: SealReason | str = SealReason.SERVER_RECOVERY,
        include_current: bool = False,
    ) -> dict[str, Any]:
        """Seal every un-sealed, non-current turn and classify its orphans.

        The spec's recovery worker: seals abandoned turns even when the
        client disconnected, the agent died, the Stop hook never ran, or
        the server restarted between attempt and outcome. Each orphaned
        attempt without a resolution is classified INDETERMINATE (recovery
        may not invent proof, and non-idempotent attempts are NEVER
        auto-retried); a later operator/tool proof can upgrade it via
        :meth:`resolve_orphan`. The session's CURRENT turn is skipped
        unless ``include_current`` — recovery must not seal live work.

        Heavy pass: on-demand / background only, never the per-event path.
        """
        reason = SealReason(seal_reason)
        self.init_db(project_root)
        with self.session(project_root) as conn:
            sql = "SELECT turn_id, session_id FROM causal_turns WHERE state != ?"
            params: list[Any] = [TurnState.SEALED.value]
            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)
            candidates = [dict(r) for r in conn.execute(sql, params).fetchall()]
            current_by_session: dict[str, str] = {}
            for cand in candidates:
                sid = str(cand["session_id"])
                if sid not in current_by_session:
                    try:
                        row = conn.execute(
                            "SELECT current_turn_id FROM session_query_gate "
                            "WHERE session_id = ?",
                            (sid,),
                        ).fetchone()
                        current_by_session[sid] = (
                            str(row["current_turn_id"] or "") if row else ""
                        )
                    except Exception:
                        current_by_session[sid] = ""
        sealed: list[str] = []
        skipped_current: list[str] = []
        orphans_classified = 0
        for cand in candidates:
            tid = str(cand["turn_id"])
            sid = str(cand["session_id"])
            if not include_current and tid == current_by_session.get(sid, ""):
                skipped_current.append(tid)
                continue
            for orphan in self.list_orphan_attempts(project_root, turn_id=tid):
                if not orphan.get("resolution"):
                    self.resolve_orphan(
                        project_root,
                        orphan["attempt_event_id"],
                        OrphanResolution.INDETERMINATE,
                        evidence="recovery pass: durable attempt with no outcome row",
                        resolved_by="recovery_worker",
                        turn_id=tid,
                        session_id=sid,
                    )
                    orphans_classified += 1
            self.seal_turn(project_root, tid, reason)
            sealed.append(tid)
        return {
            "sealed_turns": sealed,
            "skipped_current": skipped_current,
            "orphans_classified": orphans_classified,
            "seal_reason": reason.value,
        }

    # ── helpers ──

    @staticmethod
    def _resolve_actor(project_root: Path) -> str:
        """Best-effort acting identity for instruction attribution — the
        same IdentityResolver seam the audit rows use. '' when unresolvable
        (identity stays provable via the audit chain's own attribution)."""
        try:
            from .identity_resolver import current_user

            return str(current_user(project_root)[0] or "")
        except Exception:
            return ""
