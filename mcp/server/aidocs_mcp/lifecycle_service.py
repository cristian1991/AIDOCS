"""Host-agnostic lifecycle service (SessionStart / Stop / PostCompact / PostToolUse parity).

Owns the lifecycle event contract: every host (Claude Code,
OpenCode, OpenAI Agents) translates its lifecycle events into
``LifecycleService.<event>`` calls and writes the resulting
``LifecycleResult`` audit + side effects.

Same incremental extraction pattern as goggles, prompt_mutator,
tool_gate.

The completion bar (matches /goal item D): "lifecycle/compaction/
task parity as shared runtime services where not already shared.
Hosts must become thin adapters only."

## Extraction status

Migrated to this service (host-agnostic):
  - on_post_compact         — token reset + epoch bump + compaction grace stamp
  - on_post_tool_use_audit  — universal post-tool audit event with
                              status detection (completed/failed)
  - on_assistant_turn_end   — Stop / SubagentStop audit
                              (stop_reason, tool_use_count, message)
  - build_followthrough_nudge — task lifecycle nudge from runtime
                                lifecycle_state (needs_task_complete /
                                needs_task_update + open-task detection)
  - build_session_start_context — SessionStart context builder
                                  (startup_state branches + active
                                  skills + helper skill guidance,
                                  with worker-fence shortcut)
  - dispatch_todo_lifecycle     — TodoWrite-style diff → task_begin /
                                  task_update / task_complete dispatch
  - record_agent_handoff        — agent_handoff audit event
                                  (currently only fired by the OA
                                  adapter; now host-agnostic)
  - on_tool_end_output_guard    — secret scan on tool output with
                                  policy (allow_raw / redact / block).
                                  Currently only OA has this; CC's
                                  PostToolUse should adopt it.

These will move in subsequent commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleResult:
    """Host-agnostic lifecycle event outcome.

    Lifecycle events are typically side-effect-heavy (rotate epoch,
    clear counters, write audit rows) rather than return-shape-heavy.
    The result carries:
      - ``side_effects``: human-readable list of what happened
        (used by tests + dashboards)
      - ``audit_events``: list of (event_kind, payload) tuples
        emitted to execution_events
      - ``why``: which sub-handlers fired
    """

    side_effects: tuple[str, ...] = ()
    audit_events: tuple[tuple[str, dict], ...] = ()
    why: tuple[str, ...] = ()
    # Set ONLY when the host can replace tool output before model context
    # AND a redaction was applied. The host substitutes the tool result
    # with this text. None means "no replacement" — the host must NOT
    # claim redaction protection.
    redacted_text: str | None = None
    # Shape-preserving redacted copy of the ORIGINAL tool_response
    # (str / dict / list), for hosts that replace the result in place
    # (e.g. Claude Code updatedToolOutput, which requires the replacement
    # to match the tool's output shape). None when no redaction applied.
    redacted_response: object | None = None

    @classmethod
    def empty(cls) -> LifecycleResult:
        return cls()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LifecycleService:
    """Host-agnostic lifecycle event handlers.

    Bound to a runtime for hub access (managed_mode, query_gate,
    execution). Stateless across calls.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------
    # Migrated handler: agent_handoff audit event
    # ------------------------------------------------------------------

    def record_agent_handoff(
        self,
        *,
        from_agent_name: str,
        to_agent_name: str,
        host_session_id: str,
        agent_id: str,
        project_root: Path,
    ) -> LifecycleResult:
        """Audit an agent-to-agent handoff. Currently only the OpenAI
        Agents adapter fires this; making it host-agnostic enables
        any host that supports multi-agent flows (future Codex
        adapters, CC sub-agent dispatch) to record handoffs in the
        same audit shape.

        Best-effort; failure logged in why but never raises.
        """
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="agent_handoff",
                source_kind="agent_lifecycle",
                session_id=host_session_id,
                capability_name=f"{from_agent_name}→{to_agent_name}",
                action_kind="handoff",
                status="observed",
                payload={
                    "from_agent": from_agent_name,
                    "to_agent": to_agent_name,
                    "agent_id": agent_id,
                },
            )
        except Exception:
            return LifecycleResult(why=("handoff_audit_error",))
        return LifecycleResult(
            audit_events=(
                (
                    "agent_handoff",
                    {
                        "from_agent": from_agent_name,
                        "to_agent": to_agent_name,
                        "agent_id": agent_id,
                    },
                ),
            ),
            why=("handoff_audited",),
        )

    # ------------------------------------------------------------------
    # Migrated handler: tool-output guard scan
    # ------------------------------------------------------------------

    def on_tool_end_output_guard(
        self,
        *,
        tool_name: str,
        result_text: str,
        host_session_id: str,
        agent_id: str,
        project_root: Path,
    ) -> LifecycleResult:
        """Scan tool output for secret tokens. Three policy modes
        from ``security.tool_output_secret_policy``:
          - allow_raw  → no scan, no redact (operator opt-out)
          - redact     → scan + redact (default)
          - block      → scan; audit finding; return marker

        Currently the OpenAI Agents adapter fires this on every
        on_tool_end; CC's PostToolUse should adopt it (the audit
        identified this as missing). Host-agnostic version lives
        here so all adapters get the same scan.

        Returns ``side_effects`` with redaction count + ``audit_events``
        with the finding row when the guard fires.
        """
        try:
            from .config import get_setting

            policy = (
                str(
                    get_setting(
                        "security.tool_output_secret_policy",
                        project_root=project_root,
                        default="redact",
                    )
                    or "redact",
                )
                .strip()
                .lower()
            )
        except Exception:
            policy = "redact"
        if policy == "allow_raw" or not result_text:
            return LifecycleResult(why=("output_guard_skipped",))

        try:
            from .output_guard import scan_text

            guard = scan_text(result_text, redact=(policy == "redact"))
        except Exception as exc:
            # Doctrine 2026-05-29 (king re-seal — output-guard fail-
            # closed): a scan error means we CANNOT certify the
            # output is safe. The previous behavior returned a soft
            # `output_guard_scan_error` reason and let the caller
            # surface the unredacted text — that's the leak we're
            # closing. Now: emit a fail-closed marker that callers
            # honor identically to a positive finding, audit the
            # condition, and refuse to pass through the raw text.
            try:
                self.runtime.hub.execution.record_event(
                    project_root,
                    event_kind="output_guard_scan_failed_closed",
                    source_kind="post_tool_use",
                    session_id=host_session_id,
                    capability_name=tool_name,
                    action_kind="scan_error",
                    status="failed_closed",
                    payload={"tool_name": tool_name, "policy": policy, "error": repr(exc)[:200]},
                )
            except Exception:
                pass
            return LifecycleResult(
                side_effects=(
                    f"output_guard scan_error on {tool_name}: refusing to surface "
                    f"unredacted text (fail-closed)",
                ),
                why=("output_guard_scan_failed_closed", policy),
            )

        if getattr(guard, "clean", True):
            return LifecycleResult(why=("output_guard_clean",))

        # Finding detected
        finding_count = len(getattr(guard, "findings", []) or [])
        redaction_count = getattr(guard, "redaction_count", 0)
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="output_guard_finding",
                source_kind="post_tool_use",
                session_id=host_session_id,
                capability_name=tool_name,
                action_kind="flagged",
                status="flagged",
                payload={
                    "tool_name": tool_name,
                    "finding_count": finding_count,
                    "redaction_count": redaction_count,
                    "agent_id": agent_id,
                    "policy": policy,
                },
            )
        except Exception:
            pass

        return LifecycleResult(
            side_effects=(
                f"output_guard: {finding_count} findings, "
                f"{redaction_count} redactions on {tool_name}",
            ),
            audit_events=(
                (
                    "output_guard_finding",
                    {
                        "tool_name": tool_name,
                        "finding_count": finding_count,
                        "redaction_count": redaction_count,
                        "policy": policy,
                    },
                ),
            ),
            why=("output_guard_flagged", policy),
        )

    def on_host_read_output(
        self,
        *,
        tool_name: str,
        path: str,
        result_text: str,
        host_session_id: str,
        host_kind: str,
        project_root: Path,
        agent_id: str = "",
        result_obj: object | None = None,
    ) -> LifecycleResult:
        """Secret-scan NORMAL HOST Read output, reusing output_guard's
        detector/redactor (no second regex set).

        Behavior depends on the host's pre-context capability:
          - host CAN replace output before context
            (host_capabilities.can_redact_tool_output_before_context):
            redact secrets in place, return ``redacted_text`` for the host
            to substitute, and emit ``host_read_output_redacted``.
          - host CANNOT replace: do NOT claim protection. If secrets are
            found, emit ``host_read_output_guard_finding`` status=
            "degraded" (forensic, AFTER exposure) and return NO
            redacted_text. PreToolUse path-blocking is the real defense
            for these hosts; this is only a tripwire.

        Secret-shaped PATHS are already blocked at PreToolUse
        (AccessGate.host_read_decision), so this guards the case of a
        SAFE path whose CONTENT happens to contain a credential.
        """
        if not result_text:
            return LifecycleResult(why=("host_read_output_clean",))
        try:
            from .host_capabilities import (
                can_redact_tool_output_before_context,
            )
            from .output_guard import scan_text
        except Exception:
            return LifecycleResult(why=("host_read_output_scan_error",))

        can_redact = can_redact_tool_output_before_context(host_kind)
        try:
            guard = scan_text(result_text, redact=can_redact)
        except Exception:
            return LifecycleResult(why=("host_read_output_scan_error",))

        if getattr(guard, "clean", True):
            return LifecycleResult(why=("host_read_output_clean",))

        findings = list(getattr(guard, "findings", []) or [])
        categories = sorted({f.category for f in findings})
        redaction_count = int(getattr(guard, "redaction_count", 0) or 0)

        if can_redact and getattr(guard, "redacted_text", None) is not None:
            # Pre-context redaction is real: hand back the redacted text.
            # Also build a SHAPE-PRESERVING redacted copy of the original
            # response when one was supplied (hosts like Claude Code
            # require updatedToolOutput to match the tool's output shape).
            redacted_response: object | None = None
            if result_obj is not None:
                try:
                    from .output_guard import redact_tool_response

                    redacted_response, _rc, _cats = redact_tool_response(
                        result_obj,
                        redact=True,
                    )
                    if _rc == 0:
                        redacted_response = None
                except Exception:
                    redacted_response = None
            if redacted_response is None:
                # Fall back to the plain redacted text as the replacement.
                redacted_response = guard.redacted_text
            try:
                self.runtime.hub.execution.record_event(
                    project_root,
                    event_kind="host_read_output_redacted",
                    source_kind="host_read",
                    session_id=host_session_id,
                    capability_name=tool_name,
                    action_kind="redacted",
                    target_entity=path,
                    status="applied",
                    payload={
                        "tool_name": tool_name,
                        "path": path,
                        "redaction_count": redaction_count,
                        "categories": categories,
                        "host_kind": host_kind,
                        "agent_id": agent_id,
                    },
                )
            except Exception:
                pass
            return LifecycleResult(
                redacted_text=guard.redacted_text,
                redacted_response=redacted_response,
                side_effects=(
                    f"host_read_output_redacted: {redaction_count} redactions on {path}",
                ),
                audit_events=(
                    (
                        "host_read_output_redacted",
                        {
                            "tool_name": tool_name,
                            "path": path,
                            "redaction_count": redaction_count,
                            "categories": categories,
                        },
                    ),
                ),
                why=("host_read_output_redacted",),
            )

        # Host cannot pre-context redact: forensic-only, AFTER exposure.
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="host_read_output_guard_finding",
                source_kind="host_read",
                session_id=host_session_id,
                capability_name=tool_name,
                action_kind="flagged",
                target_entity=path,
                status="degraded",
                payload={
                    "tool_name": tool_name,
                    "path": path,
                    "finding_count": len(findings),
                    "categories": categories,
                    "host_kind": host_kind,
                    "agent_id": agent_id,
                    "note": "after exposure; forensic only — host cannot redact before context",
                },
            )
        except Exception:
            pass
        return LifecycleResult(
            side_effects=(
                f"host_read_output_guard_finding (degraded, forensic): "
                f"{len(findings)} findings on {path}",
            ),
            audit_events=(
                (
                    "host_read_output_guard_finding",
                    {
                        "tool_name": tool_name,
                        "path": path,
                        "finding_count": len(findings),
                        "categories": categories,
                        "status": "degraded",
                    },
                ),
            ),
            why=("host_read_output_guard_finding_degraded",),
        )

    # ------------------------------------------------------------------
    # Migrated handler: TodoWrite-style task lifecycle dispatch
    # ------------------------------------------------------------------

    def dispatch_todo_lifecycle(
        self,
        *,
        todos: list,
        managed_session_id: str,
        project_root: Path,
    ) -> LifecycleResult:
        """Fire task_begin / task_update / task_complete based on a
        TodoWrite-style todo list diff.

        Workflow:
          1. Pull previous todos snapshot from TodoStateStore.
          2. Compute transitions via ``diff_todos``.
          3. Persist the new snapshot.
          4. Dispatch:
             - all_completed + task_open    → task_complete
             - first_submission + goal_text → task_begin
             - started_now / completed_now  → task_update

        CC's TodoWrite tool is the original consumer; any host that
        exposes a todo-style API can call this with the same shape.

        Best-effort: TodoStateStore or diff_todos error returns
        empty result; runtime dispatch errors are swallowed per-op.
        """
        if not managed_session_id:
            return LifecycleResult(why=("todo_lifecycle_no_session",))
        if not isinstance(todos, list):
            return LifecycleResult(why=("todo_lifecycle_invalid_todos",))
        todos = [t for t in todos if isinstance(t, dict)]

        try:
            from .todo_state_store import TodoStateStore, diff_todos

            store = TodoStateStore()
            prev = store.get(project_root, managed_session_id)
            transitions = diff_todos(prev, todos)
            store.set(project_root, managed_session_id, todos)
        except Exception:
            return LifecycleResult(why=("todo_lifecycle_store_error",))

        # Decide begin vs update via current task state
        runtime = self.runtime
        try:
            current_task = runtime.task_status(
                project_root,
                session_id=managed_session_id,
            )
            task_open = bool(current_task and current_task.get("current_task"))
        except Exception:
            task_open = False

        first_ip = transitions.get("first_in_progress")
        goal_text = ""
        if isinstance(first_ip, dict):
            goal_text = str(first_ip.get("content") or "")

        partial_goals = [str(t.get("content") or "") for t in todos if t.get("content")]

        side_effects: list[str] = []
        why: list[str] = []

        if transitions.get("all_completed"):
            if task_open:
                try:
                    runtime.task_complete(
                        project_root,
                        session_id=managed_session_id,
                        result_summary=(
                            f"All {transitions.get('total', 0)} TodoWrite items completed."
                        ),
                    )
                    side_effects.append("task_complete dispatched")
                    why.append("todo_all_completed")
                except Exception:
                    why.append("todo_complete_dispatch_error")
            else:
                why.append("todo_all_completed_no_open_task")
            return LifecycleResult(
                side_effects=tuple(side_effects),
                why=tuple(why),
            )

        if transitions.get("first_submission") and goal_text and not task_open:
            try:
                runtime.task_begin(
                    project_root,
                    session_id=managed_session_id,
                    goal=goal_text,
                    partial_goals=partial_goals,
                )
                side_effects.append(f"task_begin dispatched (goal={goal_text!r})")
                why.append("todo_first_submission")
            except Exception:
                why.append("todo_begin_dispatch_error")
            return LifecycleResult(
                side_effects=tuple(side_effects),
                why=tuple(why),
            )

        if transitions.get("started_now") or transitions.get("completed_now"):
            state_bits = []
            if goal_text:
                state_bits.append(f"active: {goal_text}")
            for t in transitions.get("completed_now", []) or []:
                state_bits.append(f"done: {str(t.get('content', ''))[:120]}")
            try:
                runtime.task_update(
                    project_root,
                    session_id=managed_session_id,
                    state=state_bits or None,
                    partial_goals=partial_goals or None,
                )
                side_effects.append("task_update dispatched")
                why.append("todo_transition")
            except Exception:
                why.append("todo_update_dispatch_error")

        if not why:
            why = ["todo_no_dispatch"]
        return LifecycleResult(
            side_effects=tuple(side_effects),
            why=tuple(why),
        )

    # ------------------------------------------------------------------
    # Migrated handler: SessionStart context builder
    # ------------------------------------------------------------------

    # SessionStart startup-state → context-text table. Pure data; pinned
    # so dashboards + tests reference the canonical operator-facing
    # messages. Branch order matters (ordered/strict not_initialized
    # before stale_indexes).
    SESSION_START_CONTEXTS: dict[str, str] = {
        "not_initialized": (
            "AIDOCS startup check: this project is not initialized yet. "
            "Run `/aidocs` to initialize and bootstrap AIDOCS before normal work."
        ),
        "not_bootstrapped": (
            "AIDOCS startup check: project structure is incomplete or not "
            "fully bootstrapped. Run `/aidocs` to repair bootstrap state "
            "before normal work."
        ),
        "no_session": (
            "AIDOCS startup check: the project is initialized, but no "
            "session exists yet. Use `/aidocs`; create a session before "
            "normal work."
        ),
        "multiple_sessions": (
            "AIDOCS startup check: multiple plausible sessions exist. "
            "Ask the user which session to connect to before normal work, "
            "then use `/aidocs` as needed to bind managed mode."
        ),
    }

    def build_session_start_context(
        self,
        *,
        host_kind: str,
        host_session_id: str,
        project_root: Path,
        is_worker_proc: bool,
    ) -> str:
        """Build the SessionStart context string a host adapter
        injects on startup.

        Worker fence: if ``is_worker_proc=True``, returns empty —
        workers already know their lane + session from spawn env;
        bootstrap guidance is noise that costs a turn if they try to
        act on it.

        Otherwise consults ``runtime.host_state(project_root)`` for
        session_state + skill_state, picks the operator-facing
        message by startup state (closed-set vocabulary in
        ``SESSION_START_CONTEXTS`` plus the ready/stale_indexes
        cases), and folds active skills + helper skill guidance
        (once-per-epoch dedup) into the result.

        ``host_kind`` ("claude_code" / "opencode" / "openai_agents")
        feeds the helper-skill epoch dedup so each host gets the
        same once-per-epoch behavior.
        """
        if is_worker_proc:
            return ""

        try:
            host_state = self.runtime.host_state(project_root)
        except Exception:
            return ""

        session_state = (
            host_state.get("session_state")
            if isinstance(host_state.get("session_state"), dict)
            else {}
        )
        session_id = str(session_state.get("session_id") or "").strip()
        current_state = str(session_state.get("state") or "ready")

        # Pick the base context message
        if current_state in self.SESSION_START_CONTEXTS:
            context = self.SESSION_START_CONTEXTS[current_state]
        elif current_state == "stale_indexes":
            target = f" Session `{session_id}` is the current candidate." if session_id else ""
            context = (
                "AIDOCS startup check: indexes are stale and should be "
                "re-synced before normal work."
                f"{target} Run `/aidocs` to refresh bootstrap/index state."
            )
        else:
            # ready / unknown
            target = f" Continue with session `{session_id}`." if session_id else ""
            context = (
                "AIDOCS startup check: startup state is ready."
                f"{target} Stay in the bound AIDOCS session and continue "
                "its current conductor/plan flow; do not switch to "
                "generic worktree or standalone execution setup. Prefer "
                "indexed AIDOCS retrieval before broad repository reads."
            )

        # Active skills + helper skill guidance
        skill_state = (
            host_state.get("skill_state") if isinstance(host_state.get("skill_state"), dict) else {}
        )
        snap = (
            skill_state.get("session_snapshot")
            if isinstance(skill_state.get("session_snapshot"), dict)
            else {}
        )
        active_skills = (
            snap.get("active_skills") if isinstance(snap.get("active_skills"), list) else []
        )
        if active_skills:
            context = (
                context
                + " Imported skills: "
                + ", ".join(f"`{item}`" for item in active_skills if str(item).strip())
                + "."
            )

        helper_guidance = (
            snap.get("helper_skill_guidance")
            if isinstance(snap.get("helper_skill_guidance"), list)
            else []
        )
        try:
            from .helper_skill_injector import maybe_helper_skill_blocks

            rendered = maybe_helper_skill_blocks(
                project_root,
                helper_guidance,
                host_kind=host_kind,
                host_session_id=host_session_id,
            )
        except Exception:
            rendered = []
        if rendered:
            context = context + " Active AIDOCS helper skill guidance: " + " ".join(rendered)

        return context

    # ------------------------------------------------------------------
    # Migrated handler: lifecycle follow-through nudge
    # ------------------------------------------------------------------

    @staticmethod
    def build_followthrough_nudge(lifecycle_state: Any) -> str:
        """Return a one-line nudge string (or empty) based on the
        runtime's lifecycle_state.

        Pure function — no runtime state. Hosts call it with the
        lifecycle_state dict from ``runtime.host_state(...)`` and
        append the returned string to context if non-empty.

        Branches:
          - needs_task_complete + has_open_task → "call task complete"
          - needs_task_complete (no open task)  → "register task or ignore"
          - needs_task_update + has_open_task   → "record progress"
          - needs_task_update (no open task)    → "register task if multi-step"
          - otherwise                           → ""
        """
        if not isinstance(lifecycle_state, dict):
            return ""
        last_lifecycle_tool = lifecycle_state.get("last_lifecycle_tool")
        has_open_task = last_lifecycle_tool in {"task_begin", "task_update"}
        if lifecycle_state.get("needs_task_complete"):
            if has_open_task:
                return (
                    "Lifecycle follow-through: edit work has occurred "
                    "since the last ai_task(mode='begin') or "
                    "ai_task(mode='update'); call "
                    "`ai_task(mode='complete')` when the task is done."
                )
            return (
                "Lifecycle follow-through: edit work happened outside "
                "an open task. If this was a standalone fix, ignore; "
                "if it's part of a larger task, call "
                "`ai_task(mode='begin')` now to register it, then "
                "`ai_task(mode='complete')` when done."
            )
        if lifecycle_state.get("needs_task_update"):
            if has_open_task:
                return (
                    "Lifecycle follow-through: work has accumulated "
                    "since the last ai_task(mode='begin') or "
                    "ai_task(mode='update'); call "
                    "`ai_task(mode='update')` to record progress."
                )
            return (
                "Lifecycle follow-through: significant work has "
                "accumulated without an open task. If this is a "
                "multi-step effort, call `ai_task(mode='begin')` now "
                "to register it."
            )
        return ""

    # ------------------------------------------------------------------
    # Migrated handler: Stop / SubagentStop audit
    # ------------------------------------------------------------------

    def on_assistant_turn_end(
        self,
        *,
        event_name: str,
        project_root: Path,
        payload: dict,
    ) -> LifecycleResult:
        """Audit the end of an assistant turn. CC fires Stop when
        the assistant ends its turn and SubagentStop for spawned
        sub-agents. Both carry (stop_reason, tool_use_count, message).

        One ``assistant_turn_end`` event per fire — paired with
        ``user_prompt_received`` this gives turn-by-turn replay.
        Never blocks; never returns context.

        Content capture (the message text) is gated behind
        ``audit.capture_response_content`` (default off) for payload
        size + sensitivity policy.
        """
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
        except Exception:
            return LifecycleResult(why=("turn_end_managed_error",))
        if not managed.get("active"):
            return LifecycleResult(why=("turn_end_unmanaged",))

        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            return LifecycleResult(why=("turn_end_no_session",))

        try:
            from .config import get_setting

            capture_content = bool(
                get_setting(
                    "audit.capture_response_content",
                    project_root=project_root,
                    default=False,
                ),
            )
        except Exception:
            capture_content = False

        stop_reason = str(payload.get("stop_reason") or "").strip()
        tool_use_count_raw = payload.get("tool_use_count")
        try:
            tool_use_count = int(tool_use_count_raw) if tool_use_count_raw is not None else None
        except (TypeError, ValueError):
            tool_use_count = None
        message = str(payload.get("message") or "")

        audit_payload: dict = {
            "event": event_name,
            "stop_reason": stop_reason,
            "tool_use_count": tool_use_count,
            "transcript_path": str(payload.get("transcript_path") or ""),
            "cli_session_id": str(payload.get("session_id") or ""),
            "message_len": len(message),
        }
        if capture_content and message:
            audit_payload["message_text"] = message[:16000]

        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="assistant_turn_end",
                source_kind=event_name.lower(),
                session_id=session_id,
                capability_name=event_name,
                action_kind="turn_end",
                status=stop_reason or "completed",
                payload=audit_payload,
            )
        except Exception:
            return LifecycleResult(why=("turn_end_store_error",))

        return LifecycleResult(
            audit_events=(
                (
                    "assistant_turn_end",
                    {
                        "source_kind": event_name.lower(),
                        "session_id": session_id,
                        "capability_name": event_name,
                        "action_kind": "turn_end",
                        "status": stop_reason or "completed",
                        "payload": audit_payload,
                    },
                ),
            ),
            why=("turn_end_written",),
        )

    # ------------------------------------------------------------------
    # Migrated handler: PostToolUse universal audit
    # ------------------------------------------------------------------

    def on_post_tool_use_audit(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        tool_response: Any,
        host_session_id: str,
        project_root: Path,
        payload: dict,
        lane_id: str | None = None,
        surface_downstream: bool = True,
    ) -> LifecycleResult:
        """Write one ``native_tool_use`` row per tool COMPLETION.

        Pairs with ``ToolGate.record_pretool_audit`` so every
        invocation produces an attempted/completed pair (or
        attempted/<denied-not-written> when a gate refused).

        Status detection from tool_response:
          - dict with ``is_error`` or ``error``      → "failed"
          - string starting with "error"/"❌"/"failed" → "failed"
          - anything else                             → "completed"

        Best-effort: store errors swallowed. Returns a LifecycleResult
        with the audit_event row so tests can assert exact shape.
        """
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
        except Exception:
            return LifecycleResult(why=("posttool_audit_managed_error",))
        if not managed.get("active"):
            return LifecycleResult(why=("posttool_audit_unmanaged",))

        sid = str(managed.get("session_id") or "").strip()
        if not sid or not tool_name:
            return LifecycleResult(why=("posttool_audit_skipped",))

        target = ""
        if isinstance(tool_input, dict):
            for key in (
                "file_path",
                "path",
                "command",
                "pattern",
                "session_id",
                "url",
            ):
                v = tool_input.get(key)
                if isinstance(v, str) and v.strip():
                    target = v.strip()[:500]
                    break

        # Status detection
        status = "completed"
        if isinstance(tool_response, dict):
            if tool_response.get("is_error") or tool_response.get("error"):
                status = "failed"
        elif isinstance(tool_response, str):
            lead = tool_response.lstrip()[:32].lower()
            if lead.startswith(("error", "❌", "failed")):
                status = "failed"

        from .tool_gate_service import (
            build_audit_payload,
            classify_tool_action,
        )

        action_kind = classify_tool_action(tool_name)
        audit = build_audit_payload(
            tool_name=tool_name,
            tool_input=tool_input,
            payload=payload,
            lane_id=lane_id,
        )
        audit["has_target"] = bool(target)

        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="native_tool_use",
                source_kind="post_tool_use",
                session_id=sid,
                capability_name=tool_name,
                action_kind=action_kind,
                target_entity=target,
                status=status,
                payload=audit,
            )
        except Exception:
            return LifecycleResult(why=("posttool_audit_store_error",))

        # Post-edit downstream goggles. Fires only for EDIT_TOOLS;
        # any failure swallowed (audit/goggles must never block the
        # tool's success path). Caller can disable via
        # surface_downstream=False (e.g. tests).
        downstream_blocks: tuple[str, ...] = ()
        downstream_why: tuple[str, ...] = ()
        if surface_downstream:
            try:
                from .read_memory_surfacer import ReadMemorySurfacer

                xray = ReadMemorySurfacer(self.runtime).surface_on_edit(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    project_root=project_root,
                )
                if xray.advisory_lines:
                    downstream_blocks = xray.advisory_lines
                    downstream_why = (f"posttool_downstream_{xray.why}",)
            except Exception:
                pass

        return LifecycleResult(
            audit_events=(
                (
                    "native_tool_use",
                    {
                        "source_kind": "post_tool_use",
                        "session_id": sid,
                        "capability_name": tool_name,
                        "action_kind": action_kind,
                        "target_entity": target,
                        "status": status,
                        "payload": audit,
                    },
                ),
            ),
            side_effects=tuple(f"posttool_downstream: {line}" for line in downstream_blocks),
            why=("posttool_audit_written", status) + downstream_why,
        )

    # ------------------------------------------------------------------
    # Migrated handler: PostCompact
    # ------------------------------------------------------------------

    def on_post_compact(
        self,
        *,
        host_kind: str,
        host_session_id: str,
        project_root: Path,
    ) -> LifecycleResult:
        """Post-compaction housekeeping. Three side effects:

          1. Clear token-usage counters for the managed session.
             Compaction shrinks the visible transcript; the counter
             must reset so the next session's budget is accurate.
          2. Stamp compaction grace (force-wakeup deadline +120s)
             so a freshly-compacted agent isn't locked out before
             it can fire the next ScheduleWakeup.
          3. Rotate ``agent_memory_epoch`` so once-per-epoch dedup
             re-fires — the agent's working memory was just shrunk,
             so memory hints SHOULD re-surface even if they fired
             before compaction.

        ``host_kind`` identifies which host fired the compaction
        event ("claude_code", "opencode", "codex", "openai_agents").
        Used in the epoch bump audit so cross-host operator
        debugging can attribute compactions correctly.

        ``host_session_id`` is the host's per-process session UUID.
        Falls back to ``query_gate.last_cli_session_id`` when empty
        (Claude Code's hook used to omit this in some PostCompact
        payloads pre-2026-05-03).

        Best-effort throughout: each sub-effect runs in its own
        try/except so one failure doesn't suppress the others.
        """
        side_effects: list[str] = []
        audit_events: list[tuple[str, dict]] = []
        why: list[str] = []

        # Resolve managed session id
        managed_session_id: str | None = None
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            if managed.get("active"):
                managed_session_id = str(managed.get("session_id") or "").strip() or None
        except Exception:
            managed_session_id = None

        # 1. Token counter reset
        try:
            from .execution_index_store import ExecutionIndexStore

            deleted = ExecutionIndexStore().clear_token_usage(
                project_root,
                session_id=managed_session_id,
            )
            side_effects.append(
                f"token_usage cleared (deleted={deleted}, session={managed_session_id or '*all*'})",
            )
            why.append("token_usage_reset")
        except Exception:
            pass

        # 2. Compaction grace stamp
        if managed_session_id:
            try:
                self.runtime.hub.query_gate.stamp_compaction(
                    project_root,
                    managed_session_id,
                )
                side_effects.append(f"compaction grace stamped (sid={managed_session_id})")
                why.append("compaction_grace_stamped")
            except Exception:
                pass

        # 3. Resolve host_session_id with fallback
        if not host_session_id and managed_session_id:
            try:
                gate_row = self.runtime.hub.query_gate.get(
                    project_root,
                    managed_session_id,
                )
                host_session_id = str((gate_row or {}).get("last_cli_session_id") or "").strip()
            except Exception:
                host_session_id = ""

        # 4. Epoch bump
        if host_session_id:
            try:
                from .agent_memory_epoch import bump_compaction_count

                bump_compaction_count(
                    project_root,
                    host_kind=host_kind,
                    host_session_id=host_session_id,
                )
                side_effects.append(
                    f"agent_memory_epoch bumped (host={host_kind}, host_session={host_session_id})",
                )
                audit_events.append(
                    (
                        "compaction_epoch_bumped",
                        {
                            "host_kind": host_kind,
                            "host_session_id": host_session_id,
                            "managed_session_id": managed_session_id,
                        },
                    ),
                )
                why.append("epoch_bumped")
            except Exception:
                pass

        # 5. Reset the security-strike COUNTER on epoch change (king directive):
        # a compacted agent is effectively a fresh mind (new agent_memory_epoch),
        # so the freeze threshold starts over. The marker does NOT lift an active
        # freeze (that stays admin-clear-only) and does NOT erase the recorded
        # violation events (audit stays intact); only the count that drives the
        # next freeze resets.
        if managed_session_id:
            try:
                from .security_violation_service import SecurityViolationService

                if SecurityViolationService(self.runtime.hub).reset_strikes(
                    project_root,
                    managed_session_id,
                    host_session_id=host_session_id,
                    reason="compaction_epoch_change",
                ):
                    side_effects.append(f"security strikes reset (sid={managed_session_id})")
                    why.append("strikes_reset_on_compaction")
            except Exception:
                pass

        return LifecycleResult(
            side_effects=tuple(side_effects),
            audit_events=tuple(audit_events),
            why=tuple(why),
        )
