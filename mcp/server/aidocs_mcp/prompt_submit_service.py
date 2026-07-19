"""Transactional prompt-submit service shared by supported host adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence


TRANSACTION_STAGES = (
    "resolve_session_freeze",
    "resolve_chat_unfreeze",
    "soul_grants",
    "escalation_scrub",
    "sticky_lifecycle",
    "sticky_answer",
    "user_tool_grants",
    "per_turn_intent",
    "dnt_grants",
    "config_grants",
    "lane_exit_grant",
    "intent_dispatch",
    "route_classification",
    "auto_bind_session",
    "auto_task",
    "operator_intent",
)

# Stages that MUTATE authority/session/task state (grants, freeze flips,
# escalation consumption, session binding, task lifecycle). These are the
# only stages that REQUIRE the multi-store snapshot: no mutation may run
# without a protecting snapshot (fail-CLOSED). The remaining stages
# (intent_dispatch / route_classification / operator_intent) are
# READ/advisory consumers: when the snapshot infrastructure is unavailable
# they still run so the operator's prompt degrades to advisory context
# instead of being blocked on an infra hiccup (require_active_task
# doctrine: infra errors fail OPEN for read/advisory surfaces only).
MUTATION_STAGES = frozenset(
    {
        "resolve_session_freeze",
        "resolve_chat_unfreeze",
        "soul_grants",
        "escalation_scrub",
        "sticky_lifecycle",
        "sticky_answer",
        "user_tool_grants",
        "per_turn_intent",
        "dnt_grants",
        "config_grants",
        "lane_exit_grant",
        "auto_bind_session",
        "auto_task",
    }
)


class PromptSubmitMutationUnavailable(RuntimeError):
    """Control signal: the protecting snapshot could not be captured, so an
    authority-MUTATION stage must be skipped (fail-closed). The canonical
    core catches this per-stage and continues with the advisory stages; a
    core that does NOT catch it aborts, which still yields a degraded ALLOW
    with zero mutations executed (never a grant without a snapshot)."""


@dataclass(frozen=True)
class CapturedStoreState:
    """Unambiguous snapshot result; absence is state, not an empty payload."""

    name: str
    captured: bool
    existed: bool
    state: Any


@dataclass(frozen=True)
class PromptSubmitStoreParticipant:
    name: str
    capture: Callable[[], CapturedStoreState]
    restore: Callable[[CapturedStoreState], None]


@dataclass
class MultiStoreSnapshot:
    participants: tuple[PromptSubmitStoreParticipant, ...]
    states: tuple[CapturedStoreState, ...]

    @classmethod
    def capture(
        cls, participants: Iterable[PromptSubmitStoreParticipant]
    ) -> "MultiStoreSnapshot":
        bound = tuple(participants)
        states: list[CapturedStoreState] = []
        for participant in bound:
            state = participant.capture()
            if not state.captured:
                raise RuntimeError(f"{participant.name} snapshot was not captured")
            if state.name != participant.name:
                raise RuntimeError(
                    f"snapshot identity mismatch: {participant.name} != {state.name}"
                )
            states.append(state)
        return cls(bound, tuple(states))

    def restore_all(self) -> None:
        errors: list[str] = []
        for participant, state in reversed(tuple(zip(self.participants, self.states))):
            try:
                participant.restore(state)
            except Exception as exc:
                errors.append(f"{participant.name}: {exc}")
        if errors:
            raise RuntimeError("rollback failed: " + "; ".join(errors))


@dataclass
class PromptSubmitStateLedger:
    store_names: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    serialized: bool = False
    committed: bool = False
    rolled_back: bool = False
    failure: str = ""


@dataclass
class PromptSubmitResult:
    decision: str = "allow"
    reason: str = ""
    rewritten_prompt: str = ""
    additional_context: str = ""
    degraded: bool = False
    failed_stage: str = ""
    state_ledger: PromptSubmitStateLedger = field(default_factory=PromptSubmitStateLedger)
    host_integration: str = "native_hook"
    automatic_ups: bool = True
    # Audit attribution (doctrine XIII): which sub-pipelines fired and WHY
    # authority was withheld (e.g. grant_eligible_unset_failed_closed).
    why: tuple[str, ...] = ()

    def semantic_fingerprint(self) -> tuple[str, str, str, str]:
        return (
            self.decision,
            self.reason,
            self.rewritten_prompt,
            self.additional_context,
        )

    def to_claude_envelope(self) -> dict[str, Any] | None:
        if self.decision == "block":
            return {"decision": "block", "reason": self.reason}
        if not self.additional_context:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": self.additional_context,
            },
        }

    def to_adapter_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "rewritten_prompt": self.rewritten_prompt,
            "additional_context_blocks": (
                [self.additional_context] if self.additional_context else []
            ),
            "degraded": self.degraded,
            "failed_stage": self.failed_stage,
            "host_integration": self.host_integration,
            "automatic_ups": self.automatic_ups,
        }


class _InjectedStageFault(BaseException):
    """Escapes legacy best-effort Exception handlers inside transaction stages."""

    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.stage = stage
        self.cause = cause


class PromptSubmitTransactionLock:
    """Cross-process project lock held only across the authority transaction."""

    def __init__(self, project_root: Path, *, timeout_seconds: float = 10.0) -> None:
        # Project-wide scope intentionally covers the pre-bind case where no
        # canonical managed session exists yet. SQLite releases the writer lock
        # automatically when a process crashes or its connection closes.
        self.path = (
            Path(project_root)
            / ".aidocs"
            / "runtime"
            / "prompt-submit-lock.sqlite3"
        )
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self._conn: sqlite3.Connection | None = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    def acquire(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
            conn.execute("BEGIN IMMEDIATE")
        except Exception:
            conn.close()
            raise
        self._conn = conn

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute("ROLLBACK")
        finally:
            conn.close()


class _SubmitTransaction:
    """Snapshot + lock state machine for ONE evaluate_submit run.

    Extracted from evaluate_submit's nested closures (#413 tranche D):
    identical fields and transitions, held by one object so the stage
    hooks handed to the canonical core are small bound methods instead
    of closures over a dozen nonlocals. Not shared across runs.
    """

    def __init__(
        self,
        *,
        lock: PromptSubmitTransactionLock,
        participants: tuple[PromptSubmitStoreParticipant, ...],
        ledger: PromptSubmitStateLedger,
        fault_injector: Callable[[str, str], None] | None,
    ) -> None:
        self.lock = lock
        self.participants = participants
        self.ledger = ledger
        self.fault_injector = fault_injector
        self.snapshot: MultiStoreSnapshot | None = None
        self.stages: list[str] = []
        self.current_stage = ""
        self.committed = False
        self.snapshot_failure = ""
        self.snapshot_failed_stage = ""
        self.lock_acquired = False

    def acquire_lock(self) -> None:
        if self.lock_acquired:
            return
        self.current_stage = "transaction_lock"
        self.lock.acquire()
        self.lock_acquired = True
        self.ledger.serialized = True

    def release_lock(self) -> None:
        if not self.lock_acquired:
            return
        try:
            self.lock.release()
        finally:
            self.lock_acquired = False

    def capture(self) -> None:
        if self.snapshot is None:
            self.acquire_lock()
            self.current_stage = "snapshot_capture"
            self.snapshot = MultiStoreSnapshot.capture(self.participants)

    def try_capture(self) -> bool:
        """Attempt lock+snapshot; on INFRA failure record the degrade
        (once) and release the lock instead of raising. Mutation stages
        translate a False into a fail-closed skip; advisory stages and
        the transaction boundary proceed degraded (fail-open reads)."""
        if self.snapshot is not None:
            return True
        if self.snapshot_failure:
            return False
        try:
            self.capture()
        except Exception as exc:
            if self.current_stage != "transaction_lock":
                self.current_stage = "snapshot_capture"
            self.snapshot_failure = str(exc)
            self.snapshot_failed_stage = self.current_stage
            # No snapshot → nothing to serialize; do not hold the
            # project-wide lock across the advisory-only remainder.
            try:
                self.release_lock()
            except Exception:
                pass
            return False
        return True

    def phase_boundary(self, phase: str, position: str) -> None:
        if phase != "transaction":
            return
        if position == "before":
            # Degrades (returns False) instead of raising: snapshot
            # unavailability must not block the prompt — mutation stages
            # individually fail closed via stage_hook.
            self.try_capture()
        elif position == "after":
            if self.snapshot is not None:
                self.committed = True
                self.ledger.committed = True
            self.release_lock()

    def stage_hook(self, stage: str, phase: str) -> None:
        if stage not in TRANSACTION_STAGES:
            raise RuntimeError(f"unknown prompt-submit transaction stage: {stage}")
        if phase == "before" and not self.try_capture():
            if stage in MUTATION_STAGES:
                # FAIL-CLOSED: no authority mutation without a protecting
                # snapshot. The canonical core catches this per-stage and
                # skips ONLY the mutation stage.
                raise PromptSubmitMutationUnavailable(
                    f"prompt-submit snapshot unavailable; {stage} skipped "
                    f"fail-closed: {self.snapshot_failure}"
                )
            # Advisory stage: proceed degraded (fail-open read surface).
        self.current_stage = stage
        if stage not in self.stages:
            self.stages.append(stage)
        if self.fault_injector is not None:
            try:
                self.fault_injector(stage, phase)
            except Exception as exc:
                raise _InjectedStageFault(stage, exc) from exc


class PromptSubmitService:
    def __init__(
        self,
        runtime: Any,
        *,
        core_runner: Callable[..., Any] | None = None,
        participant_factory: Callable[..., Sequence[PromptSubmitStoreParticipant]] | None = None,
        lock_factory: Callable[..., Any] | None = None,
        lock_timeout_seconds: float = 10.0,
        max_context_chars: int = 12_000,
    ) -> None:
        self.runtime = runtime
        self.core_runner = core_runner
        self.participant_factory = participant_factory or self._default_participants
        self.lock_factory = lock_factory or self._default_lock
        self.lock_timeout_seconds = max(0.0, float(lock_timeout_seconds))
        self.max_context_chars = max(128, int(max_context_chars))

    def _default_participants(self, **kwargs: Any) -> Sequence[PromptSubmitStoreParticipant]:
        from .prompt_submit_store_participants import build_prompt_submit_participants

        return build_prompt_submit_participants(self.runtime, **kwargs)

    @staticmethod
    def _default_lock(
        *,
        project_root: Path,
        host_session_id: str,
        timeout_seconds: float,
    ) -> PromptSubmitTransactionLock:
        del host_session_id
        return PromptSubmitTransactionLock(
            project_root,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _core() -> Callable[..., Any]:
        from .hook_pipeline import _run_user_prompt_core

        return _run_user_prompt_core

    @staticmethod
    def _extract_result(raw: Any, prompt: str) -> tuple[str, str, str, str]:
        if raw is None:
            return "allow", "", prompt, ""
        if not isinstance(raw, Mapping):
            raise TypeError("prompt-submit core returned a non-mapping result")
        decision = str(raw.get("decision") or "allow")
        reason = str(raw.get("reason") or "")
        rewritten = str(raw.get("rewritten_prompt") or prompt)
        hook_output = raw.get("hookSpecificOutput")
        context = ""
        if isinstance(hook_output, Mapping):
            context = str(hook_output.get("additionalContext") or "")
        if not context:
            blocks = raw.get("additional_context_blocks")
            if isinstance(blocks, (list, tuple)):
                context = "\n\n".join(str(item) for item in blocks if item)
        return decision, reason, rewritten, context

    def _bound_context(self, context: str) -> str:
        if len(context) <= self.max_context_chars:
            return context
        marker = "\n\n[context bounded: middle omitted]\n\n"
        remaining = max(0, self.max_context_chars - len(marker))
        head = remaining // 2
        tail = remaining - head
        return context[:head] + marker + (context[-tail:] if tail else "")

    def _managed_session_id(self, project_root: Path, host_session_id: str) -> str:
        try:
            managed = self.runtime.hub.managed_mode.get_mode(
                project_root,
                host_session_id=host_session_id,
            )
            return str(managed.get("session_id") or "").strip()
        except Exception:
            return ""

    def _set_degraded_state(
        self,
        project_root: Path,
        session_id: str,
        *,
        reason: str,
        failure_event_id: str,
    ) -> None:
        """SEC-005: surface degraded_state on the session row so the
        dashboard top bar + right-panel strip render the red badge without
        a second query. Best-effort — never breaks the submit path."""
        if not session_id:
            return
        try:
            self.runtime.hub.query_gate.set_degraded_state(
                project_root,
                session_id,
                reason=reason[:160],
                failure_event_id=str(failure_event_id or ""),
            )
        except Exception:
            pass

    def _record_degraded_event(
        self,
        project_root: Path,
        *,
        audit_source: str,
        session_id: str,
        host_session_id: str,
        failed_stage: str,
        failure: str,
        ledger: PromptSubmitStateLedger,
    ) -> None:
        """Best-effort audit of a snapshot-unavailable degrade: mutation
        stages skipped fail-closed, advisory stages ran fail-open. Also
        flips SEC-005 degraded_state — the fail-open decision change never
        hides the degradation from the operator dashboard."""
        event_id = ""
        try:
            event_id = (
                self.runtime.hub.execution.record_event(
                    project_root,
                    event_kind="prompt_submit_transaction_degraded",
                    source_kind=audit_source or "prompt_submit_service",
                    session_id=session_id or None,
                    capability_name="UserPromptSubmit",
                    action_kind="transaction_degraded",
                    status="degraded",
                    payload={
                        "failed_stage": failed_stage,
                        "error": failure[:500],
                        "store_names": list(ledger.store_names),
                        "mutation_stages_skipped": True,
                    },
                )
                or ""
            )
        except Exception:
            event_id = ""
        self._set_degraded_state(
            project_root,
            self._managed_session_id(project_root, host_session_id),
            reason=f"prompt_submit_transaction_degraded[{failed_stage}]: {failure[:120]}",
            failure_event_id=str(event_id),
        )

    def evaluate_submit(
        self,
        project_root: Path,
        payload: Mapping[str, Any],
        *,
        host_kind: str,
        audit_source: str = "",
        grant_eligible: bool | None = None,
        fault_injector: Callable[[str, str], None] | None = None,
        explicit_hookless: bool = False,
    ) -> PromptSubmitResult:
        """Run the canonical UPS core under the snapshot transaction.

        Decomposed 2026-07-19 (#413 tranche D): the snapshot/lock state
        machine lives in ``_SubmitTransaction`` (was nested closures);
        the four exits keep their ORDER and semantics — success (with
        rollback-if-uncommitted), PromptSubmitMutationUnavailable →
        degrade-to-allow, post-commit failure → allow degraded,
        pre-commit failure → block + SEC-005 degraded state. BaseException
        still restores + releases and re-raises.
        """
        prompt_payload = dict(payload)
        prompt = str(prompt_payload.get("prompt") or "")
        host_session_id = str(prompt_payload.get("session_id") or "").strip()
        participants = tuple(
            self.participant_factory(
                project_root=project_root,
                payload=prompt_payload,
                host_kind=host_kind,
                host_session_id=host_session_id,
            )
        )
        ledger = PromptSubmitStateLedger(
            store_names=tuple(item.name for item in participants)
        )
        why_tags: list[str] = []
        txn = _SubmitTransaction(
            lock=self.lock_factory(
                project_root=project_root,
                host_session_id=host_session_id,
                timeout_seconds=self.lock_timeout_seconds,
            ),
            participants=participants,
            ledger=ledger,
            fault_injector=fault_injector,
        )
        audit_session_id = ""
        failure = ""
        try:
            raw, audit_session_id = self._run_submit_core(
                project_root,
                prompt_payload,
                host_kind=host_kind,
                host_session_id=host_session_id,
                audit_source=audit_source,
                grant_eligible=grant_eligible,
                txn=txn,
                why_tags=why_tags,
            )
            decision, reason, rewritten, context = self._extract_result(raw, prompt)
            if txn.snapshot is not None and not txn.committed:
                txn.snapshot.restore_all()
                ledger.rolled_back = True
            txn.release_lock()
            ledger.stages = tuple(txn.stages)
            if txn.snapshot_failure:
                # Snapshot infra was unavailable: mutation stages were
                # skipped fail-closed inside the stage hook and the
                # advisory stages ran degraded. The core's decision stands —
                # an EXPLICIT refusal (secret-block / preflight / route
                # block) still blocks; an internal infra error alone never
                # blocks the operator's prompt (no authority committed).
                ledger.failure = txn.snapshot_failure
                self._record_degraded_event(
                    project_root,
                    audit_source=audit_source,
                    session_id=audit_session_id,
                    host_session_id=host_session_id,
                    failed_stage=txn.snapshot_failed_stage,
                    failure=txn.snapshot_failure,
                    ledger=ledger,
                )
            return PromptSubmitResult(
                decision=decision,
                reason=reason,
                rewritten_prompt=rewritten,
                additional_context=self._bound_context(context),
                degraded=bool(txn.snapshot_failure),
                failed_stage=txn.snapshot_failed_stage if txn.snapshot_failure else "",
                state_ledger=ledger,
                host_integration=(
                    "explicit_hookless" if explicit_hookless else "native_hook"
                ),
                automatic_ups=not explicit_hookless,
                why=tuple(why_tags),
            )
        except PromptSubmitMutationUnavailable:
            return self._submit_mutation_unavailable_result(
                project_root,
                prompt=prompt,
                ledger=ledger,
                txn=txn,
                audit_session_id=audit_session_id,
                host_session_id=host_session_id,
                audit_source=audit_source,
                explicit_hookless=explicit_hookless,
                why_tags=why_tags,
            )
        except _InjectedStageFault as exc:
            txn.current_stage = exc.stage
            failure = str(exc.cause)
        except Exception as exc:
            txn.current_stage = txn.current_stage or (
                "snapshot_capture" if txn.snapshot is None else "transaction"
            )
            failure = str(exc)
        except BaseException:
            try:
                if txn.snapshot is not None and not txn.committed:
                    try:
                        txn.snapshot.restore_all()
                        ledger.rolled_back = True
                    except Exception as rollback_exc:
                        ledger.failure = f"rollback failed: {rollback_exc}"
            finally:
                txn.release_lock()
            raise
        if txn.committed:
            return self._submit_post_commit_degraded_result(
                project_root,
                prompt_payload=prompt_payload,
                prompt=prompt,
                ledger=ledger,
                txn=txn,
                failure=failure,
                audit_session_id=audit_session_id,
                audit_source=audit_source,
                explicit_hookless=explicit_hookless,
                why_tags=why_tags,
            )
        return self._submit_transaction_failed_result(
            project_root,
            prompt=prompt,
            ledger=ledger,
            txn=txn,
            failure=failure,
            audit_session_id=audit_session_id,
            host_session_id=host_session_id,
            audit_source=audit_source,
            explicit_hookless=explicit_hookless,
            why_tags=why_tags,
        )

    def _run_submit_core(
        self,
        project_root: Path,
        prompt_payload: dict[str, Any],
        *,
        host_kind: str,
        host_session_id: str,
        audit_source: str,
        grant_eligible: bool | None,
        txn: _SubmitTransaction,
        why_tags: list[str],
    ) -> tuple[Any, str]:
        """Run the canonical UPS core inside the request-identity bracket.

        Returns (raw core result, audit_session_id). Exceptions propagate
        to evaluate_submit's exit ladder; both brackets are always reset.
        (Extracted from evaluate_submit, #413 tranche D.)
        """
        runner = self.core_runner or self._core()
        from .mcp_server_runtime_helpers import (
            current_calling_aidocs_session_id,
            reset_request_host_session_id,
            set_request_host_session_id,
        )

        identity_token = set_request_host_session_id(
            host_session_id,
            host_kind=host_kind,
        )
        # #436: request-scoped get_mode memo — same bracket as the
        # request identity so the cache lives exactly as long as this
        # pipeline run and can never leak into another request.
        from .managed_mode_service import (
            begin_request_mode_memo,
            reset_request_mode_memo,
        )

        memo_token = begin_request_mode_memo()
        audit_session_id = ""
        try:
            call_kwargs = {
                "host_kind": host_kind,
                "audit_source": audit_source,
                "verified_grant_eligible": grant_eligible,
                "phase_boundary": txn.phase_boundary,
                "transaction_stage_hook": txn.stage_hook,
                "why_sink": why_tags.append,
            }
            if self.core_runner is not None:
                raw = runner(
                    runtime=self.runtime,
                    project_root=project_root,
                    payload=prompt_payload,
                    **call_kwargs,
                )
            else:
                raw = runner(
                    self.runtime,
                    project_root,
                    prompt_payload,
                    **call_kwargs,
                )
            try:
                from .agent_memory_epoch import derive_session_uuid

                managed = self.runtime.hub.managed_mode.get_mode(
                    project_root,
                    host_session_id=host_session_id,
                )
                work_session_id = str(
                    managed.get("session_id") or ""
                ).strip()
                audit_session_id = (
                    current_calling_aidocs_session_id(
                        project_root,
                        session_uuid=derive_session_uuid(
                            project_root,
                            work_session_id,
                        ),
                    )
                    if work_session_id
                    else ""
                )
            except Exception:
                audit_session_id = ""
        finally:
            reset_request_mode_memo(memo_token)
            reset_request_host_session_id(identity_token)
        return raw, audit_session_id

    def _submit_mutation_unavailable_result(
        self,
        project_root: Path,
        *,
        prompt: str,
        ledger: PromptSubmitStateLedger,
        txn: _SubmitTransaction,
        audit_session_id: str,
        host_session_id: str,
        audit_source: str,
        explicit_hookless: bool,
        why_tags: list[str],
    ) -> PromptSubmitResult:
        """A core that does not catch the per-stage fail-closed signal
        aborted on the FIRST mutation stage: zero mutations executed,
        nothing to roll back (there is no snapshot by definition).
        Degrade to ALLOW — same failure mode as the advisory path.
        Defensive: should a core raise this signal spuriously while a
        snapshot IS held, restore it — never leave partial state.
        (Extracted from evaluate_submit, #413 tranche D.)
        """
        if txn.snapshot is not None and not txn.committed:
            try:
                txn.snapshot.restore_all()
                ledger.rolled_back = True
            except Exception as rollback_exc:
                ledger.failure = f"rollback failed: {rollback_exc}"
        txn.release_lock()
        ledger.stages = tuple(txn.stages)
        ledger.failure = txn.snapshot_failure
        self._record_degraded_event(
            project_root,
            audit_source=audit_source,
            session_id=audit_session_id,
            host_session_id=host_session_id,
            failed_stage=txn.snapshot_failed_stage or "snapshot_capture",
            failure=txn.snapshot_failure,
            ledger=ledger,
        )
        return PromptSubmitResult(
            decision="allow",
            reason="",
            rewritten_prompt=prompt,
            degraded=True,
            failed_stage=txn.snapshot_failed_stage or "snapshot_capture",
            state_ledger=ledger,
            host_integration=(
                "explicit_hookless" if explicit_hookless else "native_hook"
            ),
            automatic_ups=not explicit_hookless,
            why=tuple(why_tags),
        )

    def _submit_post_commit_degraded_result(
        self,
        project_root: Path,
        *,
        prompt_payload: dict[str, Any],
        prompt: str,
        ledger: PromptSubmitStateLedger,
        txn: _SubmitTransaction,
        failure: str,
        audit_session_id: str,
        audit_source: str,
        explicit_hookless: bool,
        why_tags: list[str],
    ) -> PromptSubmitResult:
        """Failure AFTER the transaction committed: authority changes are
        already durable, so the prompt proceeds (allow) flagged degraded.
        (Extracted from evaluate_submit, #413 tranche D.)
        """
        txn.release_lock()
        ledger.stages = tuple(txn.stages)
        ledger.failure = failure
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="prompt_submit_post_commit_degraded",
                source_kind=audit_source or "prompt_submit_service",
                session_id=audit_session_id or None,
                capability_name="UserPromptSubmit",
                action_kind="post_commit",
                status="degraded",
                payload={"error": failure[:500]},
            )
        except Exception:
            pass
        return PromptSubmitResult(
            decision="allow",
            reason="",
            rewritten_prompt=str(prompt_payload.get("prompt") or prompt),
            degraded=True,
            failed_stage="post_commit",
            state_ledger=ledger,
            host_integration=(
                "explicit_hookless" if explicit_hookless else "native_hook"
            ),
            automatic_ups=not explicit_hookless,
            why=tuple(why_tags),
        )

    def _submit_transaction_failed_result(
        self,
        project_root: Path,
        *,
        prompt: str,
        ledger: PromptSubmitStateLedger,
        txn: _SubmitTransaction,
        failure: str,
        audit_session_id: str,
        host_session_id: str,
        audit_source: str,
        explicit_hookless: bool,
        why_tags: list[str],
    ) -> PromptSubmitResult:
        """Pre-commit stage failure: roll back, audit, flip SEC-005
        degraded state, and BLOCK — no authority change committed.
        (Extracted from evaluate_submit, #413 tranche D.)
        """
        rollback_failure = ""
        if txn.snapshot is not None and not txn.committed:
            try:
                txn.snapshot.restore_all()
                ledger.rolled_back = True
            except Exception as exc:
                rollback_failure = str(exc)
        try:
            txn.release_lock()
        except Exception as exc:
            rollback_failure += (
                ("; " if rollback_failure else "")
                + f"transaction lock release failed: {exc}"
            )
        ledger.stages = tuple(txn.stages)
        ledger.failure = failure + (
            f"; {rollback_failure}" if rollback_failure else ""
        )
        managed_session_id = self._managed_session_id(project_root, host_session_id)
        audit_session_id = audit_session_id or managed_session_id
        failure_event_id = ""
        try:
            failure_event_id = (
                self.runtime.hub.execution.record_event(
                    project_root,
                    event_kind="prompt_mutation_failed",
                    source_kind=audit_source or "prompt_submit_service",
                    session_id=audit_session_id or None,
                    capability_name="UserPromptSubmit",
                    action_kind="mutation_error",
                    status="rolled_back" if ledger.rolled_back else "failed",
                    payload={
                        "failed_stage": txn.current_stage,
                        "error": ledger.failure[:500],
                        "store_names": list(ledger.store_names),
                        "rolled_back": ledger.rolled_back,
                    },
                )
                or ""
            )
        except Exception:
            failure_event_id = ""
        # SEC-005: the transaction rollback must flip the session's visible
        # degraded_state (the pre-transaction SEC-002 block in hook_pipeline
        # did this; the service owns it now that stage failures re-raise).
        self._set_degraded_state(
            project_root,
            managed_session_id,
            reason=f"prompt_mutation_failed[{txn.current_stage}]: {ledger.failure[:120]}",
            failure_event_id=str(failure_event_id),
        )
        return PromptSubmitResult(
            decision="block",
            reason="prompt-submit transaction unavailable; no authority change committed",
            rewritten_prompt=prompt,
            degraded=True,
            failed_stage=txn.current_stage,
            state_ledger=ledger,
            host_integration=(
                "explicit_hookless" if explicit_hookless else "native_hook"
            ),
            automatic_ups=not explicit_hookless,
            why=tuple(why_tags),
        )
