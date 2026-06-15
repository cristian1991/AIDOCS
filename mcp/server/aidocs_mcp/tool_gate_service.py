"""Host-agnostic tool-gate service (PreToolUse pipeline).

Owns the PreToolUse gate contract: every host (Claude Code,
OpenCode, OpenAI Agents) translates its pre-tool event into a
``ToolGate.evaluate_tool(tool_name, tool_input, host_session_id,
project_root)`` call and renders the resulting ``ToolGateResult``
into its envelope shape.

This is an **incremental extraction**, same pattern as
``prompt_mutator.py`` and ``read_memory_surfacer.py``.

The completion bar (matches /goal item C): "host tool-gate as one
core service owning managed-mode checks, read gate, judge/orchestrator
checks, lane comms, advisory/context output, and palace/read-memory
hints. Hosts must become thin adapters only."

## Extraction status

Migrated to this service (host-agnostic):
  - kill_switch_bypass         — operator emergency-allow envelope
  - managed_mode_required      — refuse non-bootstrap tools when unbound
  - orchestrator_check         — AgentOrchestrator.check_tool dispatch
                                 with freeze data carried in result
  - conductor_comms            — lane state (paused/canceled) +
                                 pending conductor messages
  - record_pretool_audit       — universal pre-tool audit event
                                 (classify_tool_action + build_audit_payload)
  - reconnect_required         — refuse non-allowed tools while
                                 requires_reconnect flag is raised
  - agent_dispatch_brief       — refuse Task/Agent dispatches with
                                 research-shaped briefs (toggleable)
  - session_freeze_pretool     — refuse with frozen envelope while
                                 a session freeze is active
  - sticky_grant_pending_ask   — ask envelope when the about-to-run
                                 tool has a pending sticky grant
  - apply_lane_worker_bind     — Phoenix §VIII host_session_id stamp +
                                 lane-worker auto-bind from env
  - validate_edit_syntax       — wraps host_policy_service.validate_edit_syntax,
                                 fires in evaluate_tool for every host
                                 (was OpenCode-only pre-migration; CC
                                 + OA now also gated through the same
                                 service surface)
  - indexed_read_gate          — canonical normal-host Read law. Now
                                 delegates to AccessGate.host_read_decision
                                 (the SINGLE host-read policy, shared with
                                 the raw-tool gate and the script
                                 read-intent detector). Safe artifacts
                                 (images/PDFs/logs/csv/non-code) read
                                 freely; indexed source still needs
                                 discovery/grant; secrets/protected/
                                 traversal/unknown-external hard-block.
                                 (Was OpenCode JS-only via
                                 hasGrantedReadAccess, then an
                                 index-only gate; this goal collapsed the
                                 two contradictory laws into one.)

All sub-gates extracted. Remaining items are envelope translation
and SEC-001/002 transactional wrapper in the host adapters.

## Fail-open audit (sealed 2026-05-19)

Every exception path inside a sub-gate is classified per the
operator-doctrine rule "security-relevant gates fail closed on
undecidable state; advisory gates may continue with explicit
why-tags":

  Security-relevant (fail CLOSED → deny on lookup error):
    * indexed_read_gate
        - read_gate_managed_error, read_gate_no_session,
          read_gate_lookup_error
    * session_freeze_pretool
        - freeze_pretool_managed_error, freeze_pretool_no_session,
          freeze_pretool_lookup_error, freeze_pretool_build_error
    * reconnect_required
        - reconnect_managed_error, reconnect_no_session,
          reconnect_gate_lookup_error
    * managed_mode_required (pre-existing fail-closed)
        - managed_mode_no_identity, managed_mode_lookup_error,
          managed_mode_not_active
    * orchestrator_check (pre-existing fail-closed)
        - orchestrator_error, orchestrator_confirm_no_session,
          orchestrator_freeze_error

  Canonical host-read law (no cold-start carve-out — removed by the
  one-law goal 2026-05-20):
    * indexed_read_gate delegates to AccessGate.host_read_decision.
      A fresh session (no query_gate row) can still read SAFE
      ARTIFACTS (images/PDFs/logs/csv/non-code) but undiscovered
      indexed SOURCE blocks with the discovery message even at cold
      start — the old read_gate_cold_start blanket-continue (which
      also let undiscovered source through) was the contradictory
      behavior this goal removed.

  Advisory / quality gates (continue is correct; each tagged inline):
    * validate_edit_syntax.syntax_validator_error — quality, not
      privilege
    * sticky_grant_pending_ask.{managed,store}_error — degrades
      ASK UX only; judge already hard-refused dangerous grants
    * agent_dispatch_brief.evaluator_error — explicit design doctrine
    * record_pretool_audit.{managed,store}_error — write-only
      forensics, no privilege impact
    * conductor_comms_error — informational lane state
    * kill_switch_lookup_error — failure direction is "DON'T
      bypass", so downstream gates still enforce normally

Cross-host parity pinned by tests in test_evaluate_tool_fail_closed.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------


# Reconnect-allowed tools — single source of truth. Tools that may
# run while ``requires_reconnect=1`` is raised. Anything else gets
# the deny envelope with the connect instruction. Keep the
# allowlist tight: every read-only tool added here must make sense
# BEFORE a session has re-bound. The check is case-insensitive
# against the bare name (after mcp__aidocs__ / mcp__ prefix strip).
RECONNECT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "ai_session",
        "session_connect",
        "session_start",
        "session_list",
        "aidocs_orchestrate",
        "project_bootstrap_or_resume",
        "project_status",
        "project_check",
        "admin_clear_reconnect",
        "ToolSearch",
        "ai_find",
        "ai_investigate",
        "ai_get_lines",
        "ai_bundle",
        "ScheduleWakeup",
        "task_complete",
    },
)

# Phrases the operator may type in the current prompt to bypass the
# agent-dispatch brief gate. Reused from claude_hook's same constant.
AGENT_RESEARCH_OVERRIDE_PHRASES: tuple[str, ...] = (
    "delegate research",
    "let agents research",
    "let the agent research",
    "agent can research",
    "agents may research",
    "ok to research",
)


# Tool action buckets — closed taxonomy for audit ``action_kind``.
# Module-level so any host adapter can call ``classify_tool_action``
# without instantiating ToolGate. Hosts and the universal audit
# emitter MUST use the same bucket vocabulary so dashboard filters
# stay deterministic across hosts.
TOOL_ACTION_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "edit",
        (
            "edit",
            "write",
            "multiedit",
            "patch",
            "notebookedit",
            "ai_edit_lines",
            "ai_str_replace",
            "ai_batch_edit",
            "ai_insert_lines",
            "ai_create_file",
            "ai_slop",
        ),
    ),
    (
        "read",
        (
            "read",
            "glob",
            "grep",
            "search",
            "listdir",
            "ai_get_lines",
            "ai_get_symbol_snippet",
            "ai_read_raw",
            "ai_read_pdf",
            "ai_read_excel",
            "ai_read_docx",
            "ai_read_sqlite",
            "ai_read_jsonl",
            "ai_find",
            "ai_investigate",
            "ai_trace",
            "ai_bundle",
            "ai_text_search",
            "schema_query",
            "memory_read",
            "memory_search",
        ),
    ),
    (
        "run",
        (
            "bash",
            "ai_run",
            "ai_run_status",
            "ai_run_output",
            "ai_run_kill",
        ),
    ),
    (
        "agent",
        (
            "task",
            "agent",
            "ai_spawn",
            "lane_send_prompt",
            "conductor_ask",
            "conductor_answer",
        ),
    ),
    (
        "session",
        (
            "session_connect",
            "session_create",
            "task_begin",
            "task_update",
            "task_complete",
            "handoff_create",
            "plan_create_from_spec",
            "plan_dispatch_next",
            "plan_dispatch_report",
            "plan_conductor_status",
        ),
    ),
)


def classify_tool_action(tool_name: str) -> str:
    """Bucket a tool name into a coarse action_kind for audit.

    Strips common prefixes (``mcp__aidocs__`` / ``mcp__playwright__``
    / ``mcp__``) and matches case-insensitively against the curated
    buckets above. Unknown tools fall into ``'other'`` — keeps the
    taxonomy bounded while still recording the call.
    """
    name = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__playwright__", "mcp__"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    for bucket, names in TOOL_ACTION_BUCKETS:
        if name in names:
            return bucket
    return "other"


def build_audit_payload(
    *,
    tool_name: str,
    tool_input: Any,
    payload: dict,
    lane_id: str | None = None,
) -> dict:
    """Shared audit payload: tool_use_id, input hash+size, lane chain.

    Captures content-forensic fields without storing raw bytes:
      * ``input_hash``   — sha256 prefix of a stable JSON dump of
                           tool_input. Edit's old_string / Write's
                           content / Task's prompt leave a fingerprint
                           without being persisted.
      * ``input_bytes``  — UTF-8 byte length of that dump.
      * ``input_keys``   — sorted top-level keys (≤20), cheap schema tag.
      * ``parent_tool_use_id`` — from host payload if nested agent dispatch.
      * ``lane_id``      — current conductor lane when set.

    Best-effort throughout; any exception leaves the affected field
    out rather than raising.
    """
    import hashlib
    import json

    out: dict = {
        "tool_use_id": str(payload.get("tool_use_id") or ""),
        "tool_name": tool_name,
    }
    try:
        if isinstance(tool_input, dict):
            dump = json.dumps(
                tool_input,
                sort_keys=True,
                default=str,
                ensure_ascii=False,
            )
            raw = dump.encode("utf-8")
            out["input_hash"] = hashlib.sha256(raw).hexdigest()[:16]
            out["input_bytes"] = len(raw)
            out["input_keys"] = sorted(tool_input.keys())[:20]
    except Exception:
        pass
    parent = payload.get("parent_tool_use_id") or payload.get("parent_message_id")
    if isinstance(parent, str) and parent.strip():
        out["parent_tool_use_id"] = parent.strip()
    if lane_id:
        out["lane_id"] = lane_id
    return out


# Verdict vocabulary. Hosts MUST handle these:
#   "continue" — sub-gate did not decide; outer pipeline keeps going.
#   "allow"    — final allow (e.g. kill-switch bypass). Skip all
#                further gates. Tool proceeds.
#   "deny"     — final deny. Render host-specific block envelope.
#   "ask"      — operator confirmation required (sticky-grant pending,
#                judge needs_confirmation). Host renders ask envelope.
VERDICT_CONTINUE = "continue"
VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"
VERDICT_ASK = "ask"


@dataclass(frozen=True)
class GateHooks:
    """Optional host-specific rendering hooks for ``ToolGate
    .evaluate_tool``. Each callable is best-effort: failures are
    swallowed and never change the underlying gate verdict.

    The hooks let a host (Claude Code, OpenCode, OpenAI Agents)
    consume the canonical pipeline while still rendering its
    specific envelope shape. No host should fork the gate
    composition just because its envelope differs.

    Hook firing order during one ``evaluate_tool`` call:

      1. ``before_gate("kill_switch_bypass")``
      2. (gate runs)
      3. ``after_gate("kill_switch_bypass", result)``
      4. If result is terminal: ``on_allow``/``on_deny``/``on_ask``
      5. (next gate)
      ...
      8. For freeze envelopes from session_freeze_pretool or
         orchestrator_check: ``on_freeze(parsed_marker_fields)``
      9. For each ``additional_context_block`` accumulated during
         a continue cascade: ``on_context_block(block)`` (may
         transform; None drops the block).

    Hooks that need to return a host-specific envelope return it
    from ``on_deny`` / ``on_ask`` / ``on_freeze``; the value is
    surfaced as ``ToolGateResult.host_envelope``. Hosts then read
    that field instead of synthesizing their own envelope from
    the verdict/reason.

    All hooks default to None — callers that omit ``hooks`` get
    the canonical pipeline unchanged.
    """

    before_gate: Any | None = None
    """``(gate_name: str) -> None`` — pre-gate observation hook."""
    after_gate: Any | None = None
    """``(gate_name: str, result: ToolGateResult) -> None`` — post-gate
    observation hook. Cannot change the result."""
    on_allow: Any | None = None
    """``(result: ToolGateResult) -> Any | None`` — fired when a
    sub-gate returns allow (kill-switch bypass). Return value
    surfaces as host_envelope."""
    on_deny: Any | None = None
    """``(result: ToolGateResult) -> Any | None`` — fired when a
    sub-gate denies. Return value surfaces as host_envelope."""
    on_ask: Any | None = None
    """``(result: ToolGateResult) -> Any | None`` — fired when a
    sub-gate emits ask (sticky-grant-pending, freeze)."""
    on_freeze: Any | None = None
    """``(freeze_fields: dict) -> Any | None`` — fired when a gate
    returns the FREEZE marker (session_freeze_pretool or
    orchestrator_check with needs_confirmation). The marker is
    parsed into a dict {reason, blocked_by, freeze_state} so the
    host can render its envelope without re-parsing the marker
    string."""
    on_context_block: Any | None = None
    """``(block: str) -> str | None`` — fired for each
    additional_context_block accumulated during a continue cascade.
    Hook returns the (optionally transformed) block to include, or
    None to drop. Useful when CC wants to prepend conductor
    messages at advisory_parts position 0."""


def _sanitize_operator_reason(reason: str | None) -> str:
    """Strip Mock-object reprs and similar test-leak signatures from
    operator-facing deny reasons.

    Production code never produces a string containing
    ``<MagicMock ... id='...'>`` — only a test that has stubbed half
    the runtime hub does. But if such a string ever reached an
    operator (e.g. through a regression that swapped a real call
    for a Mock), the agent would see an unactionable repr instead
    of a refusal message. Belt-and-suspenders: detect the signature
    and substitute a generic but truthful message.

    Returns the sanitized string. Non-string / empty input → "".
    """
    if not reason or not isinstance(reason, str):
        return reason or ""
    # Cheap signature scan. The full Mock repr looks like:
    #   <MagicMock name='runtime.hub.x.get().get()' id='12345...'>
    # 'NonCallableMagicMock' and bare 'Mock id=' also appear.
    for needle in ("<MagicMock", "NonCallableMagicMock", "<Mock id=", "mock.get()", "mock.__"):
        if needle in reason:
            return (
                "AIDOCS gate refused (operator-facing reason was "
                "redacted because the upstream policy returned a "
                "non-serializable object — likely a test harness "
                "stub leaking into production rendering)."
            )
    return reason


def _parse_freeze_marker(block: str) -> dict:
    """Parse the ``FREEZE\\n<json>`` block emitted by
    session_freeze_pretool and orchestrator_check into a dict so
    on_freeze hooks don't have to re-parse it.

    JSON payload (current format): the reason is multi-line (it carries
    the "Type exactly:\\n   <phrase>" approval instructions) and
    freeze_state is a nested dict — neither survives a newline-delimited
    key=value round-trip, which silently truncated the operator-facing
    approval phrase. The legacy key=value parser is kept as a fallback
    for any marker that predates the JSON format.
    """
    text = block or ""
    if text.startswith("FREEZE\n"):
        payload = text[len("FREEZE\n") :]
        try:
            data = json.loads(payload)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # Legacy fallback: newline-delimited key=value (truncates multi-line
    # values — only reached for pre-JSON markers).
    fields: dict = {}
    for line in text.split("\n"):
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    return fields


@dataclass(frozen=True)
class ToolGateResult:
    """Host-agnostic gate verdict.

    Hosts render this into their envelope shape:
      - Claude Code (PreToolUse hook):
          deny  → hookSpecificOutput.permissionDecision="deny" + reason
          allow → return None (CC's "do not block" signal)
          ask   → hookSpecificOutput.permissionDecision="ask" + reason
      - OpenCode (tool.execute.before):
          deny  → throw new Error(reason)
          allow → no-op (let tool proceed)
          ask   → not yet supported; treat as deny with explanation
      - OpenAI Agents (on_tool_start):
          deny  → raise RuntimeError(reason)
          allow → return None
          ask   → raise RuntimeError("ask: " + reason)

    ``additional_context_blocks`` is informational text the host
    appends to its output (Claude Code: additionalContext in hook
    envelope; OpenCode: sessionPromptContext; OpenAI: system msg).
    These flow even on allow + continue.

    ``audit_events`` is a list of (event_kind, payload) the host or
    service writes to execution_events. Used for gate-bypass
    accounting (kill-switch fires an "enforcement_bypass" audit).

    ``why`` identifies which sub-gates produced the verdict — useful
    for tests and the operator-facing dashboard.
    """

    verdict: str = VERDICT_CONTINUE
    reason: str | None = None
    additional_context_blocks: tuple[str, ...] = ()
    audit_events: tuple[tuple[str, dict], ...] = ()
    why: tuple[str, ...] = ()
    host_envelope: Any = None
    """When ``evaluate_tool`` runs with ``GateHooks`` and a hook
    returned a host-specific envelope (e.g. CC's
    hookSpecificOutput dict, OpenCode's Error message), it lands
    here. Hosts that supplied hooks read this field directly;
    callers without hooks always see ``None``."""

    @classmethod
    def cont(
        cls,
        *,
        additional_context_blocks: tuple[str, ...] = (),
        audit_events: tuple[tuple[str, dict], ...] = (),
        why: tuple[str, ...] = (),
    ) -> ToolGateResult:
        """Sub-gate did not decide; pipeline continues. May still
        carry advisory context blocks (e.g. conductor messages) or
        audit events (e.g. pre-tool audit row that was written).
        """
        return cls(
            verdict=VERDICT_CONTINUE,
            additional_context_blocks=additional_context_blocks,
            audit_events=audit_events,
            why=why,
        )

    @classmethod
    def allow(
        cls,
        *,
        reason: str | None = None,
        audit_events: tuple[tuple[str, dict], ...] = (),
        additional_context_blocks: tuple[str, ...] = (),
        why: tuple[str, ...] = (),
    ) -> ToolGateResult:
        return cls(
            verdict=VERDICT_ALLOW,
            reason=reason,
            audit_events=audit_events,
            additional_context_blocks=additional_context_blocks,
            why=why,
        )

    @classmethod
    def deny(
        cls,
        *,
        reason: str,
        audit_events: tuple[tuple[str, dict], ...] = (),
        additional_context_blocks: tuple[str, ...] = (),
        why: tuple[str, ...] = (),
    ) -> ToolGateResult:
        return cls(
            verdict=VERDICT_DENY,
            reason=reason,
            audit_events=audit_events,
            additional_context_blocks=additional_context_blocks,
            why=why,
        )

    @classmethod
    def ask(
        cls,
        *,
        reason: str,
        additional_context_blocks: tuple[str, ...] = (),
        why: tuple[str, ...] = (),
    ) -> ToolGateResult:
        return cls(
            verdict=VERDICT_ASK,
            reason=reason,
            additional_context_blocks=additional_context_blocks,
            why=why,
        )

    @property
    def is_terminal(self) -> bool:
        """A terminal verdict short-circuits the pipeline. ``allow``,
        ``deny``, ``ask`` are terminal; ``continue`` is not.
        """
        return self.verdict != VERDICT_CONTINUE


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ToolGate:
    """Host-agnostic PreToolUse pipeline.

    Bound to a runtime for hub access (managed_mode, query_gate,
    enforcement). Stateless across calls.

    The canonical entry point is :meth:`evaluate_tool` — every host
    (Claude Code, OpenCode, OpenAI Agents, future Codex adapters)
    calls it for pre-tool gating and renders the result into its
    envelope shape. The individual sub-gates (``kill_switch_bypass``,
    ``managed_mode_required``, etc.) remain public so callers that
    need finer-grained control or testing can use them, but the
    composition itself lives here.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------
    # Canonical entry point: evaluate_tool
    # ------------------------------------------------------------------

    def evaluate_tool(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        host_session_id: str,
        project_root: Path,
        payload: dict | None = None,
        lane_id: str | None = None,
        is_aidocs_project: bool | None = None,
        surface_read_memory: bool = True,
        hooks: GateHooks | None = None,
    ) -> ToolGateResult:
        """Full pretool pipeline. Composes (in order):

           1. kill_switch_bypass — operator break-glass; short-circuits to allow
           2. managed_mode_required — refuse if unbound (with bootstrap-exempt)
           3. record_pretool_audit — universal native_tool_use event
           4. reconnect_required — refuse non-allowed tools while
              requires_reconnect flag is raised
           5. session_freeze_pretool — refuse while freeze active
           6. agent_dispatch_brief — refuse Task/Agent dispatches with
              research-shaped briefs (only fires when tool_name ∈
              {"task", "agent"})
           6b. validate_edit_syntax — refuse edits that would introduce
               parse errors (was OC JS subprocess pre-migration)
           6c. indexed_read_gate — refuse raw Read of un-discovered paths
               (was OC JS-only via hasGrantedReadAccess pre-migration)
           7. orchestrator_check — judge / orchestrator cascade (may emit ask)
           8. sticky_grant_pending_ask — surface pending sticky-grant confirms
           9. conductor_comms — lane state + pending messages (when lane bound)
          10. read-memory surfacing — x-ray goggles for file-reading tools

        A terminal verdict (allow/deny/ask) short-circuits subsequent
        gates. Continue results accumulate ``additional_context_blocks``
        + ``audit_events`` so callers see every sub-gate's output.

        Adopter status:

        - ``host_adapter_cli._handle_pretool`` (OpenCode JS plugin bridge)
            → one call here. Collapsed.
        - ``openai_agents_adapter.on_tool_start``
            → one call here. Collapsed.
        - ``claude_hook._handle_pre_tool_use``
            → STILL calls sub-gates individually. CC's PreToolUse path
              interleaves each gate with CC-specific envelope branches
              (deny / ask / freeze envelope shapes, conductor_comms
              advisory accumulation) that the canonical composition
              does not expose. Future migration: parameterize
              evaluate_tool with a callback-per-gate so CC can
              consume the composition while still rendering its
              host-specific envelope. Until then, CC is the documented
              exception, NOT a bug. The dedup invariant is pinned by
              ``test_canonical_entry_point_parity.TestNoDoubleFireFromCC``
              (CC must not call ``mutate_prompt`` either — that one
              IS a bug).

        No NEW host adapter should re-compose the sub-gates outside
        this method.

        ``is_aidocs_project`` — caller's pre-resolved marker check.
        If None, this method computes it via ``_has_marker``.
        ``payload`` — the inbound host event payload (passed to the
        audit row build).
        ``surface_read_memory`` — set False in tests / contexts that
        want to skip the goggles pass.

        ``hooks`` — optional ``GateHooks``. When provided, the host
        can render its specific envelope shape without forking the
        composition. See ``GateHooks`` doc for protocol + firing
        order. Hook callbacks are best-effort; any exception is
        swallowed and the canonical verdict still wins.
        """
        payload = payload or {}

        def _fire(name: str, callable_attr: str | None, *args) -> Any:
            """Call a hook by attribute name. Returns None if hooks
            is None, the attribute is None, or the call raised.
            """
            if hooks is None:
                return None
            cb = getattr(hooks, callable_attr, None) if callable_attr else None
            if cb is None:
                return None
            try:
                return cb(*args)
            except Exception:
                return None

        def _terminal(name: str, result: ToolGateResult) -> ToolGateResult:
            """Run after_gate + the matching on_allow/deny/ask hook
            and surface the host_envelope onto the result.
            """
            _fire(name, "after_gate", name, result)
            envelope = None
            if result.verdict == VERDICT_ALLOW:
                envelope = _fire(name, "on_allow", result)
            elif result.verdict == VERDICT_DENY:
                envelope = _fire(name, "on_deny", result)
            elif result.verdict == VERDICT_ASK:
                envelope = _fire(name, "on_ask", result)
            if envelope is not None:
                return ToolGateResult(
                    verdict=result.verdict,
                    reason=result.reason,
                    additional_context_blocks=result.additional_context_blocks,
                    audit_events=result.audit_events,
                    why=result.why,
                    host_envelope=envelope,
                )
            return result

        # 1. Kill-switch bypass — operator emergency-allow
        _fire("kill_switch_bypass", "before_gate", "kill_switch_bypass")
        ks = self.kill_switch_bypass(
            tool_name=tool_name,
            host_session_id=host_session_id,
            project_root=project_root,
        )
        if ks.verdict == VERDICT_ALLOW:
            return _terminal("kill_switch_bypass", ks)
        _fire("kill_switch_bypass", "after_gate", "kill_switch_bypass", ks)

        # 2. Managed-mode-required
        if is_aidocs_project is None:
            try:
                from .mcp_server_runtime_helpers import _has_marker

                is_aidocs_project = bool(_has_marker(project_root))
            except Exception:
                is_aidocs_project = False
        _fire("managed_mode_required", "before_gate", "managed_mode_required")
        mm = self.managed_mode_required(
            tool_name=tool_name,
            host_session_id=host_session_id,
            project_root=project_root,
            is_aidocs_project=is_aidocs_project,
        )
        if mm.verdict == VERDICT_DENY:
            return _terminal("managed_mode_required", mm)
        _fire("managed_mode_required", "after_gate", "managed_mode_required", mm)

        # 3. Universal pre-tool audit (side-effect; result is always continue)
        _fire("record_pretool_audit", "before_gate", "record_pretool_audit")
        audit = self.record_pretool_audit(
            tool_name=tool_name,
            tool_input=tool_input,
            host_session_id=host_session_id,
            project_root=project_root,
            payload=payload,
            lane_id=lane_id,
        )

        # Accumulator for non-terminal sub-gates so context + audit
        # rows survive through to the caller.
        _fire("record_pretool_audit", "after_gate", "record_pretool_audit", audit)
        ctx_blocks: list[str] = []
        audit_events: list[tuple[str, dict]] = list(audit.audit_events)
        why: list[str] = list(audit.why)

        # 4. Reconnect-required gate
        _fire("reconnect_required", "before_gate", "reconnect_required")
        rc = self.reconnect_required(
            tool_name=tool_name,
            host_session_id=host_session_id,
            project_root=project_root,
        )
        if rc.verdict == VERDICT_DENY:
            terminal = ToolGateResult.deny(
                reason=rc.reason or "reconnect required",
                additional_context_blocks=tuple(ctx_blocks),
                audit_events=tuple(audit_events),
                why=tuple(why) + rc.why,
            )
            envelope = _fire("reconnect_required", "on_deny", terminal)
            _fire("reconnect_required", "after_gate", "reconnect_required", rc)
            if envelope is not None:
                return ToolGateResult(
                    verdict=terminal.verdict,
                    reason=terminal.reason,
                    additional_context_blocks=terminal.additional_context_blocks,
                    audit_events=terminal.audit_events,
                    why=terminal.why,
                    host_envelope=envelope,
                )
            return terminal
        _fire("reconnect_required", "after_gate", "reconnect_required", rc)

        # 5. Session-freeze pre-tool guard
        _fire("session_freeze_pretool", "before_gate", "session_freeze_pretool")
        fz = self.session_freeze_pretool(project_root=project_root)
        if fz.verdict == VERDICT_DENY:
            terminal = ToolGateResult.deny(
                reason=fz.reason or "session frozen",
                additional_context_blocks=tuple(
                    list(ctx_blocks) + list(fz.additional_context_blocks),
                ),
                audit_events=tuple(audit_events),
                why=tuple(why) + fz.why,
            )
            # Freeze-specific hook: parse the marker into fields so
            # the host doesn't have to. Fall back to standard on_deny
            # if on_freeze isn't set or returns None.
            envelope = None
            if fz.additional_context_blocks:
                parsed = _parse_freeze_marker(fz.additional_context_blocks[0])
                envelope = _fire(
                    "session_freeze_pretool",
                    "on_freeze",
                    parsed,
                )
            if envelope is None:
                envelope = _fire("session_freeze_pretool", "on_deny", terminal)
            _fire("session_freeze_pretool", "after_gate", "session_freeze_pretool", fz)
            if envelope is not None:
                return ToolGateResult(
                    verdict=terminal.verdict,
                    reason=terminal.reason,
                    additional_context_blocks=terminal.additional_context_blocks,
                    audit_events=terminal.audit_events,
                    why=terminal.why,
                    host_envelope=envelope,
                )
            return terminal
        _fire("session_freeze_pretool", "after_gate", "session_freeze_pretool", fz)

        # 6. Agent-dispatch brief gate (only for task/agent tools)
        if tool_name.lower() in {"task", "agent"}:
            _fire("agent_dispatch_brief", "before_gate", "agent_dispatch_brief")
            ab = self.agent_dispatch_brief(
                tool_input=tool_input,
                project_root=project_root,
            )
            if ab.verdict == VERDICT_DENY:
                terminal = ToolGateResult.deny(
                    reason=ab.reason or "agent brief refused",
                    additional_context_blocks=tuple(ctx_blocks),
                    audit_events=tuple(audit_events),
                    why=tuple(why) + ab.why,
                )
                envelope = _fire(
                    "agent_dispatch_brief",
                    "on_deny",
                    terminal,
                )
                _fire("agent_dispatch_brief", "after_gate", "agent_dispatch_brief", ab)
                if envelope is not None:
                    return ToolGateResult(
                        verdict=terminal.verdict,
                        reason=terminal.reason,
                        additional_context_blocks=terminal.additional_context_blocks,
                        audit_events=terminal.audit_events,
                        why=terminal.why,
                        host_envelope=envelope,
                    )
                return terminal
            _fire("agent_dispatch_brief", "after_gate", "agent_dispatch_brief", ab)

        # 6b. Edit-syntax validation (was OC JS subprocess pre-migration)
        _fire("validate_edit_syntax", "before_gate", "validate_edit_syntax")
        vs = self.validate_edit_syntax(
            tool_name=tool_name,
            tool_input=tool_input,
            project_root=project_root,
        )
        if vs.verdict == VERDICT_DENY:
            terminal = ToolGateResult.deny(
                reason=vs.reason or "edit-syntax validation failed",
                additional_context_blocks=tuple(ctx_blocks),
                audit_events=tuple(audit_events),
                why=tuple(why) + vs.why,
            )
            envelope = _fire("validate_edit_syntax", "on_deny", terminal)
            _fire("validate_edit_syntax", "after_gate", "validate_edit_syntax", vs)
            if envelope is not None:
                return ToolGateResult(
                    verdict=terminal.verdict,
                    reason=terminal.reason,
                    additional_context_blocks=terminal.additional_context_blocks,
                    audit_events=terminal.audit_events,
                    why=terminal.why,
                    host_envelope=envelope,
                )
            return terminal
        _fire("validate_edit_syntax", "after_gate", "validate_edit_syntax", vs)

        # 6c. Indexed-read gate (was OC JS-only pre-migration)
        _fire("indexed_read_gate", "before_gate", "indexed_read_gate")
        rd = self.indexed_read_gate(
            tool_name=tool_name,
            tool_input=tool_input,
            host_session_id=host_session_id,
            project_root=project_root,
        )
        if rd.verdict == VERDICT_DENY:
            terminal = ToolGateResult.deny(
                reason=rd.reason or "indexed-read gate refused",
                additional_context_blocks=tuple(ctx_blocks),
                audit_events=tuple(audit_events),
                why=tuple(why) + rd.why,
            )
            envelope = _fire("indexed_read_gate", "on_deny", terminal)
            _fire("indexed_read_gate", "after_gate", "indexed_read_gate", rd)
            if envelope is not None:
                return ToolGateResult(
                    verdict=terminal.verdict,
                    reason=terminal.reason,
                    additional_context_blocks=terminal.additional_context_blocks,
                    audit_events=terminal.audit_events,
                    why=terminal.why,
                    host_envelope=envelope,
                )
            return terminal
        _fire("indexed_read_gate", "after_gate", "indexed_read_gate", rd)

        # 7. Orchestrator check
        _fire("orchestrator_check", "before_gate", "orchestrator_check")
        orch = self.orchestrator_check(
            tool_name=tool_name,
            tool_input=tool_input,
            project_root=project_root,
        )
        if orch.verdict in (VERDICT_DENY, VERDICT_ASK):
            terminal = ToolGateResult(
                verdict=orch.verdict,
                reason=orch.reason,
                additional_context_blocks=tuple(
                    list(ctx_blocks) + list(orch.additional_context_blocks),
                ),
                audit_events=tuple(audit_events),
                why=tuple(why) + orch.why,
            )
            envelope = None
            # Orchestrator's ASK verdict carries a FREEZE marker block
            # for needs_confirmation cases — same envelope route as
            # session_freeze_pretool.
            if orch.verdict == VERDICT_ASK and orch.additional_context_blocks:
                parsed = _parse_freeze_marker(orch.additional_context_blocks[0])
                if parsed.get("freeze_state") or parsed.get("blocked_by"):
                    envelope = _fire(
                        "orchestrator_check",
                        "on_freeze",
                        parsed,
                    )
            if envelope is None:
                hook_name = "on_ask" if orch.verdict == VERDICT_ASK else "on_deny"
                envelope = _fire("orchestrator_check", hook_name, terminal)
            _fire("orchestrator_check", "after_gate", "orchestrator_check", orch)
            if envelope is not None:
                return ToolGateResult(
                    verdict=terminal.verdict,
                    reason=terminal.reason,
                    additional_context_blocks=terminal.additional_context_blocks,
                    audit_events=terminal.audit_events,
                    why=terminal.why,
                    host_envelope=envelope,
                )
            return terminal
        _fire("orchestrator_check", "after_gate", "orchestrator_check", orch)
        why.extend(orch.why)
        ctx_blocks.extend(orch.additional_context_blocks)

        # 6. Sticky-grant pending confirmation
        _fire("sticky_grant_pending_ask", "before_gate", "sticky_grant_pending_ask")
        sg = self.sticky_grant_pending_ask(
            tool_name=tool_name,
            project_root=project_root,
        )
        if sg.verdict == VERDICT_ASK:
            terminal = ToolGateResult(
                verdict=VERDICT_ASK,
                reason=sg.reason,
                additional_context_blocks=tuple(ctx_blocks),
                audit_events=tuple(audit_events),
                why=tuple(why) + sg.why,
            )
            envelope = _fire("sticky_grant_pending_ask", "on_ask", terminal)
            _fire("sticky_grant_pending_ask", "after_gate", "sticky_grant_pending_ask", sg)
            if envelope is not None:
                return ToolGateResult(
                    verdict=terminal.verdict,
                    reason=terminal.reason,
                    additional_context_blocks=terminal.additional_context_blocks,
                    audit_events=terminal.audit_events,
                    why=terminal.why,
                    host_envelope=envelope,
                )
            return terminal
        _fire("sticky_grant_pending_ask", "after_gate", "sticky_grant_pending_ask", sg)

        # 7. Conductor comms (only when a lane is bound)
        if lane_id:
            _fire("conductor_comms", "before_gate", "conductor_comms")
            comms = self.conductor_comms(
                lane_id=lane_id,
                project_root=project_root,
            )
            if comms.verdict == VERDICT_DENY:
                terminal = ToolGateResult.deny(
                    reason=comms.reason or "conductor refused",
                    additional_context_blocks=tuple(ctx_blocks),
                    audit_events=tuple(audit_events),
                    why=tuple(why) + comms.why,
                )
                envelope = _fire("conductor_comms", "on_deny", terminal)
                _fire("conductor_comms", "after_gate", "conductor_comms", comms)
                if envelope is not None:
                    return ToolGateResult(
                        verdict=terminal.verdict,
                        reason=terminal.reason,
                        additional_context_blocks=terminal.additional_context_blocks,
                        audit_events=terminal.audit_events,
                        why=terminal.why,
                        host_envelope=envelope,
                    )
                return terminal
            _fire("conductor_comms", "after_gate", "conductor_comms", comms)
            ctx_blocks.extend(comms.additional_context_blocks)
            why.extend(comms.why)

        # 8. Read-memory surfacing (x-ray goggles) — pre-read advisories
        if surface_read_memory:
            try:
                from .read_memory_surfacer import ReadMemorySurfacer

                xray = ReadMemorySurfacer(self.runtime).surface_on_read(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    project_root=project_root,
                    host_session_id=host_session_id,
                )
                if xray.advisory_lines:
                    ctx_blocks.extend(xray.advisory_lines)
                    why.append("read_memory_xray")
            except Exception:
                # Goggles failure must never gate the tool
                pass

        # Apply per-block transformation hook (CC uses this to
        # prepend conductor messages, etc.)
        if hooks is not None and hooks.on_context_block is not None:
            transformed: list[str] = []
            for block in ctx_blocks:
                try:
                    out = hooks.on_context_block(block)
                except Exception:
                    out = block
                if out is None:
                    continue
                transformed.append(out)
            ctx_blocks = transformed

        return ToolGateResult.cont(
            additional_context_blocks=tuple(ctx_blocks),
            audit_events=tuple(audit_events),
            why=tuple(why),
        )

    # Bootstrap-exempt tools — must work even when managed mode is
    # inactive, so the session bind itself can happen. King directive
    # 2026-05-12 (#157): session_connect / session_list / session_create
    # were folded into ai_session(mode=...); aidocs_mode_get is the
    # legacy probe. Exempt list pinned here as the canonical source
    # of truth for every host adapter.
    BOOTSTRAP_EXEMPT: frozenset[str] = frozenset(
        {
            "ai_session",
            "aidocs_mode_get",
        },
    )

    @staticmethod
    def _normalize_tool_name(tool_name: str) -> str:
        """Strip MCP prefixes + lowercase for cross-host matching."""
        if not tool_name:
            return ""
        norm = tool_name.strip().lower()
        for prefix in ("mcp__aidocs__", "mcp__"):
            if norm.startswith(prefix):
                return norm[len(prefix) :]
        return norm

    # ------------------------------------------------------------------
    # Migrated sub-gate: managed-mode-required
    # ------------------------------------------------------------------

    def managed_mode_required(
        self,
        *,
        tool_name: str,
        host_session_id: str,
        project_root: Path,
        is_aidocs_project: bool,
    ) -> ToolGateResult:
        """In an AIDOCS-managed project, refuse non-bootstrap tool
        calls when this host session has not bound via session_connect.

        Per-call identity from ``host_session_id`` (Claude Code's
        ``payload.session_id``, OpenCode's ``hookCtx.sessionID``,
        OpenAI's ``ctx.session_id``). Never trusts global state.

        Bootstrap tools (``BOOTSTRAP_EXEMPT``) pass through so the
        bind itself can happen. Their identity check still applies:
        even bootstrap tools need a host_session_id to authorize
        against.

        ``is_aidocs_project`` is the caller's pre-resolved project
        marker check (``_has_marker(project_root)`` in
        ``mcp_server_runtime_helpers``). When False, the gate
        continues — non-AIDOCS projects don't require binding.

        Returns:
          - ``continue`` when project is not managed-mode-required, OR
            when the bound session is active (gate passes).
          - ``deny`` when host_session_id is missing entirely (no
            identity to bind under), OR when a non-bootstrap tool
            fires without an active managed_mode binding.

        """
        if not is_aidocs_project:
            return ToolGateResult.cont()

        normalized = self._normalize_tool_name(tool_name)

        # Identity check: every tool call (including bootstrap)
        # requires a host_session_id. Without one, there's no key
        # to authorize anything against — refuse closed.
        if not host_session_id:
            return ToolGateResult.deny(
                reason=(
                    "AIDOCS gate: PreToolUse payload missing "
                    "session_id. Cannot authorize against a "
                    "missing host identity. Refusing closed."
                ),
                why=("managed_mode_required_no_identity",),
            )

        # Bootstrap-exempt tools pass through. They handle their own
        # bind sequence (e.g. ai_session(mode='connect')).
        if normalized in self.BOOTSTRAP_EXEMPT:
            return ToolGateResult.cont(
                why=("managed_mode_bootstrap_exempt",),
            )

        # Conductor-scoped lookup using the per-call host_session_id,
        # not any module global. Empty managed row → not active →
        # refused.
        try:
            managed_check = self.runtime.hub.managed_mode.get_mode(
                project_root,
                cli_session_id=host_session_id,
            )
        except Exception:
            return ToolGateResult.deny(
                reason=("AIDOCS gate: managed-mode lookup error. Refusing closed."),
                why=("managed_mode_lookup_error",),
            )

        if managed_check.get("active"):
            return ToolGateResult.cont(why=("managed_mode_active",))

        sid_preview = host_session_id[:8] + "…" if len(host_session_id) > 8 else host_session_id
        return ToolGateResult.deny(
            reason=(
                f"AIDOCS managed mode is not active for this host "
                f"session ({sid_preview}). Call "
                f"`mcp__aidocs__ai_session(mode='connect',"
                f"session_id='<id>')` first. Use "
                f"`mcp__aidocs__ai_session(mode='list')` to see "
                f"available sessions."
            ),
            why=("managed_mode_not_active",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-gate: lane-worker host_session_id stamp + auto-bind
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Migrated sub-gate: edit-syntax validation
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct_edited_file(
        tool_input: Any,
        path: str,
        project_root: Path,
    ) -> str:
        """Build the FULL file content a fragment edit would produce, so
        syntax validation runs on a whole program — never on a bare
        fragment. Returns "" (→ skip validation) when the file can't be
        read or the edit can't be applied; the downstream file_ops writer
        validates the real result with full content regardless.
        """
        if not isinstance(tool_input, dict) or not path:
            return ""
        old_string = str(
            tool_input.get("old_string") or tool_input.get("old_str") or "",
        )
        # No single old/new pair (batch / line / anchor edits, or a bare
        # fragment with no anchor) → cannot safely reconstruct here. Skip;
        # file_ops validates the true full result downstream.
        if not old_string:
            return ""
        new_string = str(
            tool_input.get("new_string") or tool_input.get("new_str") or "",
        )
        try:
            p = Path(path)
            if not p.is_absolute():
                p = Path(project_root) / path
            text = p.read_text(encoding="utf-8")
        except Exception:
            return ""
        if old_string not in text:
            return ""  # old_string not found here → let file_ops decide
        # Apply exactly as str_replace would (replace the occurrence(s)).
        return text.replace(old_string, new_string)

    def validate_edit_syntax(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        project_root: Path,
    ) -> ToolGateResult:
        """Pre-edit syntax check via ``host_policy_service``. Currently
        OpenCode's JS plugin calls this via subprocess; CC's PreToolUse
        doesn't. After this migration, both can route through the
        same service surface — CC adopts it by adding a call in the
        PreToolUse pipeline, OC by calling this Python function
        instead of subprocess-ing into host_policy_service directly.

        Returns:
          - ``continue`` when tool is not an edit / no content / syntax valid
          - ``deny`` when validation surfaces a parse/import error.

        Best-effort: any internal exception falls through to continue
        (don't block edits on validator hiccups).

        """
        bare = self._normalize_tool_name(tool_name)
        # Only edit tools get syntax-checked
        if bare not in {
            "edit",
            "write",
            "notebookedit",
            "ai_replace",
            "ai_str_replace",
            "ai_insert_lines",
            "ai_edit_lines",
            "ai_batch_edit",
            "ai_create_file",
        }:
            return ToolGateResult.cont(why=("syntax_not_edit_tool",))

        path = ""
        content = ""
        if isinstance(tool_input, dict):
            path = str(
                tool_input.get("path") or tool_input.get("file_path") or "",
            )
            # CRITICAL: validate the FULL RESULTING FILE, never a bare edit
            # fragment. A fragment replace (old_string→new_string) is not a
            # standalone program, so parsing `new_string` alone falsely
            # rejects valid edits (the bug that forced single-token edits).
            #   * fragment replace (old_string present) → reconstruct the
            #     file by applying the replacement; SKIP if we can't
            #     (file_ops validates the real full result downstream).
            #   * full-file writers (content / new_content) → that IS the
            #     whole file → validate it directly.
            # A bare `new_string` (no old_string, no content) is NEVER
            # validated as a standalone file.
            _old = str(
                tool_input.get("old_string") or tool_input.get("old_str") or "",
            )
            if _old:
                # Real fragment replace → reconstruct + validate the WHOLE
                # file (or skip). NEVER validate `new_string` alone here.
                content = self._reconstruct_edited_file(
                    tool_input,
                    path,
                    project_root,
                )
            else:
                # No old_string → the payload IS the content to validate
                # (full-file write, or a host that ships content directly).
                content = str(
                    tool_input.get("content")
                    or tool_input.get("new_content")
                    or tool_input.get("new_string")
                    or "",
                )
        if not path or not content:
            return ToolGateResult.cont(why=("syntax_no_payload",))

        # Only validate file extensions that have parsers wired up.
        # Matches the legacy JS gate's validExts list verbatim so the
        # cross-host parity tests can pin "same extension set on every
        # adapter". host_policy_service then dispatches to tree-sitter
        # / stdlib parsers per suffix.
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in {
            "py",
            "js",
            "ts",
            "jsx",
            "tsx",
            "json",
            "toml",
            "yaml",
            "yml",
            "xml",
            "html",
        }:
            return ToolGateResult.cont(why=("syntax_unsupported_ext",))

        try:
            from .host_policy_service import validate_edit_syntax as _vs

            result = _vs(path, content)
        except Exception:
            # Validator boom: continue (don't trap edits on a broken
            # validator). Matches the JS plugin's try/except behavior
            # that only re-threw when the message itself was the
            # "Syntax error" string we produced.
            # CLASSIFICATION: advisory/quality. Syntax check protects
            # code quality, not privilege; a broken parser must not
            # block edits. Pinned by test_syntax_validator_error_continues.
            return ToolGateResult.cont(why=("syntax_validator_error",))

        if isinstance(result, dict) and not result.get("valid", True):
            return ToolGateResult.deny(
                reason=str(
                    result.get("error") or "Syntax error",
                ),
                why=("syntax_invalid", "edit_syntax"),
            )
        return ToolGateResult.cont(why=("syntax_ok",))

    # ------------------------------------------------------------------
    # Migrated sub-gate: indexed-read gate (was OpenCode JS-only)
    # ------------------------------------------------------------------

    def indexed_read_gate(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        host_session_id: str,
        project_root: Path,
    ) -> ToolGateResult:
        """Canonical normal-host ``Read`` gate (PreToolUse).

        This is the SINGLE host-read law. It delegates the allow/block
        decision to ``AccessGate.host_read_decision()`` — the exact same
        function the raw-tool gate (``AccessGate.check_raw_tool`` →
        ``orchestrator_check``) and the script read-intent detector use.
        There is no second, contradictory law:

          - safe artifacts (images, PDFs, logs, csv, non-code files) → allow
          - indexed source still requires discovery/grant
            (known_exact_paths / lane_exact_paths) → block otherwise
          - secrets / protected / global-bootstrap paths → hard block
          - parent-traversal (``..``) → block
          - unknown external → block unless an approved external root or a
            recorded session artifact
          - recorded/generated artifacts → allow even outside the project
            when non-sensitive

        Method name kept as ``indexed_read_gate`` so the GateHooks firing
        contract and the cross-host parity tests stay stable; the law it
        carries is now the canonical host-read law (the old
        index-only / cold-start carve-outs that let artifacts AND
        undiscovered source through were the contradictory gate this goal
        removed).

        Short-circuits to continue only for non-Read tools, no path,
        and unmanaged sessions. Undecidable managed/session state still
        fails closed.
        """
        bare = self._normalize_tool_name(tool_name)
        if bare != "read":
            return ToolGateResult.cont(why=("read_gate_not_read",))

        path = ""
        if isinstance(tool_input, dict):
            path = str(
                tool_input.get("file_path")
                or tool_input.get("filePath")
                or tool_input.get("path")
                or "",
            ).strip()
        if not path:
            return ToolGateResult.cont(why=("read_gate_no_path",))

        # SECURITY: managed-mode lookup undecidable → fail closed. The
        # managed_mode_required gate already passed for us to reach
        # this point, so a second-lookup hiccup here is rare and means
        # we cannot resolve the session this read should be checked
        # against. Refusing the read is safer than admitting it under
        # an unknown identity.
        try:
            managed = self.runtime.hub.managed_mode.get_mode(
                project_root,
                cli_session_id=host_session_id,
            )
        except Exception:
            return ToolGateResult.deny(
                reason=(
                    "AIDOCS indexed-read gate: managed-mode lookup error. Refusing read closed."
                ),
                why=("indexed_read_gate", "read_gate_managed_error"),
            )
        # Unmanaged sessions don't enforce host-read; managed_mode_required
        # already refused them by this point if the project IS managed.
        if not managed.get("active"):
            return ToolGateResult.cont(why=("read_gate_unmanaged",))
        sid = str(managed.get("session_id") or "").strip()
        if not sid:
            # SECURITY: managed_active but no session_id is a corrupt
            # row — refuse rather than continue.
            return ToolGateResult.deny(
                reason=(
                    "AIDOCS indexed-read gate: active managed row "
                    "missing session_id. Refusing read closed."
                ),
                why=("indexed_read_gate", "read_gate_no_session"),
            )

        # Resolve the target the host-read law will classify. Project-
        # internal absolute paths are made relative (so the indexed-source
        # / artifact / grant rules apply); paths outside the project are
        # passed through verbatim so the external-zone rules (approved
        # root / recorded artifact / unknown-external block) apply.
        normalized = path.replace("\\", "/").rstrip("/")
        try:
            root_norm = str(project_root).replace("\\", "/").rstrip("/")
        except Exception:
            root_norm = ""
        if root_norm and normalized.lower().startswith((root_norm + "/").lower()):
            target = normalized[len(root_norm) + 1 :]
        elif "/" not in normalized and ":" not in normalized and not normalized.startswith("~"):
            target = normalized  # bare relative path — project-local
        else:
            target = normalized  # external — host_read_decision zones it

        if not target:
            return ToolGateResult.cont(why=("read_gate_empty_relative",))

        # SECURITY: query_gate lookup is the authoritative source for
        # known/lane discovered paths + recorded session artifacts. If we
        # cannot read it, we cannot decide read access — fail closed per
        # /goal 2026-05-19.
        try:
            state = self.runtime.hub.query_gate.get(project_root, sid) or {}
        except Exception:
            return ToolGateResult.deny(
                reason=(
                    "AIDOCS indexed-read gate: query-gate lookup "
                    "error. Cannot decide read access; refusing closed."
                ),
                why=("indexed_read_gate", "read_gate_lookup_error"),
            )

        # Build the gate_state the canonical law reads: discovered/granted
        # paths + recorded artifacts (from the query_gate row) + approved
        # external roots (from effective config — same source SEC-004 uses).
        gate_state: dict[str, Any] = dict(state) if isinstance(state, dict) else {}
        # Bind the session-artifact recognizer so the Read tool can open THIS
        # session's own task/deploy output (project_root + the host session id
        # the hook stamped as last_cli_session_id). Without this the canonical
        # law sees no session ids and refuses the agent's own task log.
        gate_state["project_root"] = str(project_root)
        try:
            _host_sid = str(
                self.runtime.hub.query_gate.get_last_cli_session_id(project_root, sid) or "",
            ).strip()
        except Exception:
            _host_sid = ""
        gate_state["host_session_ids"] = [
            s
            for s in (
                str(host_session_id or "").strip(),  # authoritative host id (the path's UUID)
                str(sid or "").strip(),  # managed session id
                _host_sid,  # last_cli_session_id fallback
            )
            if s
        ]
        try:
            eff = self.runtime.effective_config(project_root)
            sec = (eff.get("security", {}) if isinstance(eff, dict) else {}) or {}
            roots = sec.get("approved_external_roots") or []
            if isinstance(roots, list):
                gate_state["approved_external_roots"] = [str(r) for r in roots if str(r).strip()]
        except Exception:
            pass

        from .access_gate import host_read_decision

        decision = host_read_decision(gate_state, target)
        if decision.allowed:
            return ToolGateResult.cont(
                why=("indexed_read_gate", decision.level or "host_read_allow"),
            )

        # Block. For undiscovered indexed source preserve the verbatim
        # operator-facing message (cross-host UX parity); for every other
        # block reason surface the canonical host-read reason.
        if decision.level == "indexed_file_gate":
            reason = (
                f'AIDOCS indexed-read gate: "{target}" has not been '
                f"discovered via ai_investigate, ai_find, ai_trace, or "
                f"ai_bundle. Use AIDOCS indexed tools (e.g. ai_get_lines "
                f"after discovery) first before raw Read."
            )
        else:
            reason = decision.reason or "Host read refused by AIDOCS read policy."
        return ToolGateResult.deny(
            reason=reason,
            why=("indexed_read_gate", decision.level or "host_read_block"),
        )

    def stamp_lane_worker_host_session_id(
        self,
        *,
        host_session_id: str,
        worker_id: str,
        worker_lane_id: str,
        project_root: Path,
    ) -> ToolGateResult:
        """Phoenix §VIII deny-path stamp (2026-05-09).

        When this process is a lane worker (worker_id + worker_lane_id
        set) AND the host provided a session_id, stamp host_session_id
        into ``session_lane_agents`` so the deny-path dispatcher can
        ``claude --resume <id>`` against the right session.

        Idempotent (set_host_session_id no-ops on unchanged value).
        Always returns ``continue`` — side-effect only.

        Called BEFORE kill-switch in claude_hook (preserves original
        Phoenix §VIII ordering).
        """
        if host_session_id and worker_id and worker_lane_id:
            try:
                from .session_lane_agents_store import SessionLaneAgentsStore

                SessionLaneAgentsStore().set_host_session_id(
                    project_root,
                    worker_id,
                    host_session_id,
                )
                return ToolGateResult.cont(why=("lane_worker_stamped",))
            except Exception:
                pass
        return ToolGateResult.cont(why=("lane_worker_no_stamp",))

    def auto_bind_lane_worker_managed_mode(
        self,
        *,
        worker_id: str,
        worker_session_id: str,
        worker_lane_id: str,
        project_root: Path,
    ) -> ToolGateResult:
        """Lane-worker auto-bind (2026-04-24). When this process is a
        spawned lane worker (all three worker_* set) AND managed_mode
        is currently inactive, auto-activate it from the worker's env.

        Without this the worker wastes 3+ tool calls discovering it
        must call session_connect first. The spawner already knows
        the session+lane+worker identity.

        Latches the sub-agent flag in protected_file_runtime so
        downstream gates know.

        Best-effort: any failure falls through silently. Called AFTER
        managed_mode_required in claude_hook (preserves the original
        ordering where bootstrap-exempt tools fall through to here).
        """
        if not (worker_id and worker_session_id and worker_lane_id):
            return ToolGateResult.cont(why=("lane_worker_not_worker",))
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            if managed.get("active"):
                return ToolGateResult.cont(
                    why=("lane_worker_already_active",),
                )
            self.runtime.hub.managed_mode.set_mode(
                project_root,
                session_id=worker_session_id,
                source="lane_worker_auto_bind",
            )
            try:
                from .protected_file_runtime import latch_sub_agent_call_on

                latch_sub_agent_call_on()
            except Exception:
                pass
            return ToolGateResult.cont(
                why=("lane_worker_auto_bound", worker_session_id),
            )
        except Exception:
            return ToolGateResult.cont(why=("lane_worker_bind_error",))

    # ------------------------------------------------------------------
    # Migrated sub-gate: session-freeze pre-tool guard
    # ------------------------------------------------------------------

    def session_freeze_pretool(
        self,
        *,
        project_root: Path,
    ) -> ToolGateResult:
        """Pre-tool freeze guard (#39). When a confirmable destructive
        verdict landed on a previous tool call, the session is frozen
        until the next UPS resolves it (self_approve) or admin
        decides (admin_escalation).

        While frozen, every tool returns the same deny envelope with
        the fingerprint phrase the operator must type. The envelope
        data (reason / blocked_by / freeze_state) lives in
        ``additional_context_blocks`` as a FREEZE\\n... marker so the
        host can render it into its shape.

        Returns:
          - ``continue`` when not managed / no session / no freeze /
            store read failed (don't inject stale freeze)
          - ``deny`` with the freeze envelope when an active freeze
            row exists

        """
        # SECURITY: managed-mode lookup undecidable → fail closed.
        # Whether the session is frozen depends on knowing the session
        # at all. Refuse rather than admit a tool that might run inside
        # a confirmable-destructive freeze.
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
        except Exception:
            return ToolGateResult.deny(
                reason=("AIDOCS freeze gate: managed-mode lookup error. Refusing closed."),
                why=("session_freeze_pretool", "freeze_pretool_managed_error"),
            )
        if not managed.get("active"):
            return ToolGateResult.cont(why=("freeze_pretool_unmanaged",))
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            # SECURITY: corrupt active row → refuse.
            return ToolGateResult.deny(
                reason=(
                    "AIDOCS freeze gate: active managed row missing session_id. Refusing closed."
                ),
                why=("session_freeze_pretool", "freeze_pretool_no_session"),
            )

        # SECURITY: freeze status undecidable when the store hiccups.
        # A frozen session that admits one more tool defeats the whole
        # confirmable-destructive freeze contract. Fail closed.
        try:
            from .freeze_service import (
                build_existing_freeze_response,
                get_existing_freeze,
            )

            freeze = get_existing_freeze(project_root, session_id)
        except Exception:
            return ToolGateResult.deny(
                reason=(
                    "AIDOCS freeze gate: freeze-state lookup error. "
                    "Refusing closed until status can be verified."
                ),
                why=("session_freeze_pretool", "freeze_pretool_lookup_error"),
            )

        if freeze is None:
            return ToolGateResult.cont(why=("freeze_pretool_no_freeze",))

        # SECURITY: we KNOW there's a freeze row but cannot render it.
        # Letting the tool through would be the worst possible outcome:
        # the operator never sees the confirm prompt and the tool runs
        # inside an active freeze. Fail closed with a bare-bones reason.
        try:
            env = build_existing_freeze_response(freeze, project_root)
        except Exception:
            return ToolGateResult.deny(
                reason=(
                    "session frozen — confirmation required (envelope "
                    "build failed; please refresh the session)"
                ),
                why=("session_freeze_pretool", "freeze_pretool_build_error"),
            )

        # FREEZE marker so host adapters can extract structured fields
        # without reaching into the service. JSON-encoded so the
        # multi-line reason (with the "Type exactly:" phrase) and the
        # freeze_state dict round-trip intact.
        marker = "FREEZE\n" + json.dumps(
            {
                "reason": env.get("permissionDecisionReason", ""),
                "blocked_by": env.get("blocked_by", "session_frozen"),
                "freeze_state": env.get("freeze_state", ""),
            },
        )
        return ToolGateResult.deny(
            reason=env.get("permissionDecisionReason", "")
            or "session frozen until operator confirms",
            additional_context_blocks=(marker,),
            why=(
                "session_freeze_pretool",
                env.get("blocked_by", "session_frozen"),
            ),
        )

    # ------------------------------------------------------------------
    # Migrated sub-gate: sticky-grant pending confirmation ask
    # ------------------------------------------------------------------

    def sticky_grant_pending_ask(
        self,
        *,
        tool_name: str,
        project_root: Path,
    ) -> ToolGateResult:
        """When the about-to-run tool has a pending sticky-grant
        confirmation (require_confirm grants that haven't resolved
        yet), surface ask instead of allowing silently.

        The registration judge has already hard-refused dangerous
        grants; this surface fires only for grants the operator
        must explicitly confirm.

        Returns:
          - ``continue`` when no managed session / no pending /
            no match for the running tool / store error
          - ``ask`` with the judge_reason when a matching pending
            grant exists

        """
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
        except Exception:
            # CLASSIFICATION: advisory. This gate surfaces ASK only —
            # losing it degrades operator UX but is not a privilege
            # bypass. The registration judge already hard-refused
            # genuinely dangerous grants upstream.
            return ToolGateResult.cont(why=("sticky_ask_managed_error",))
        sid = str(managed.get("session_id") or "").strip()
        if not sid:
            return ToolGateResult.cont(why=("sticky_ask_no_session",))

        try:
            from .sticky_grants_store import StickyGrantsStore

            pendings = StickyGrantsStore().list_pending(project_root, sid)
        except Exception:
            # CLASSIFICATION: advisory (see sticky_ask_managed_error).
            return ToolGateResult.cont(why=("sticky_ask_store_error",))
        if not pendings:
            return ToolGateResult.cont(why=("sticky_ask_no_pending",))

        bare = self._normalize_tool_name(tool_name)
        matching = [r for r in pendings if str(r.get("tool") or "").lower() == bare]
        if not matching:
            return ToolGateResult.cont(why=("sticky_ask_no_match",))

        reason = str(
            matching[0].get("judge_reason")
            or f"Sticky grant for `{bare}` pending operator confirmation.",
        )
        return ToolGateResult.ask(
            reason=reason,
            why=("sticky_grant_pending_ask", "sticky_grant_registration"),
        )

    # ------------------------------------------------------------------
    # Migrated sub-gate: reconnect required
    # ------------------------------------------------------------------

    def reconnect_required(
        self,
        *,
        tool_name: str,
        host_session_id: str,
        project_root: Path,
    ) -> ToolGateResult:
        """Fresh-CLI reconnect gate. When the host's per-process
        session_id changed mid-session, ``requires_reconnect=1`` is
        sticky until the agent calls a bootstrap-adjacent tool that
        clears it. Anything outside ``RECONNECT_ALLOWED_TOOLS`` is
        hard-refused while the flag is raised.

        Side effect: when the agent calls ``ai_session`` or
        ``session_connect`` (the bind path), the flag is cleared.

        Returns:
          - ``continue`` when not managed / flag not raised / tool
            in allowed list (with flag clear when bind path runs)
          - ``deny`` otherwise

        """
        # SECURITY: managed-mode lookup undecidable → fail closed.
        # Reconnect is sticky security state; missing the flag means
        # admitting a tool to a session that might be in fresh-CLI
        # state. Refuse closed.
        try:
            managed = self.runtime.hub.managed_mode.get_mode(
                project_root,
                cli_session_id=host_session_id,
            )
        except Exception:
            return ToolGateResult.deny(
                reason=("AIDOCS reconnect gate: managed-mode lookup error. Refusing closed."),
                why=("reconnect_required", "reconnect_managed_error"),
            )
        if not managed.get("active"):
            return ToolGateResult.cont(why=("reconnect_unmanaged",))
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            # SECURITY: active row with no session_id is corrupt — refuse.
            return ToolGateResult.deny(
                reason=(
                    "AIDOCS reconnect gate: active managed row missing session_id. Refusing closed."
                ),
                why=("reconnect_required", "reconnect_no_session"),
            )

        # SECURITY: requires_reconnect is the authoritative flag for
        # fresh-CLI detection. If we cannot read it, we cannot tell
        # whether the agent must re-bind first. Fail closed.
        try:
            needs = self.runtime.hub.query_gate.get_requires_reconnect(
                project_root,
                session_id,
            )
        except Exception:
            return ToolGateResult.deny(
                reason=("AIDOCS reconnect gate: requires_reconnect lookup error. Refusing closed."),
                why=("reconnect_required", "reconnect_gate_lookup_error"),
            )
        if not needs:
            return ToolGateResult.cont(why=("reconnect_not_required",))

        bare = self._normalize_tool_name(tool_name)

        if bare in RECONNECT_ALLOWED_TOOLS or bare.lower() in {
            t.lower() for t in RECONNECT_ALLOWED_TOOLS
        }:
            # Bind path clears the flag
            if bare.lower() in {"ai_session", "session_connect"}:
                try:
                    self.runtime.hub.query_gate.clear_requires_reconnect(
                        project_root,
                        session_id,
                    )
                except Exception:
                    pass
            return ToolGateResult.cont(why=("reconnect_allowed_tool",))

        return ToolGateResult.deny(
            reason=(
                "Fresh CLI — call `mcp__aidocs__ai_session"
                f"(mode='connect', session_id=\"{session_id}\")` or "
                "`/aidocs`. Known-path reads wiped; re-discover via "
                "ai_find / ai_investigate."
            ),
            why=("reconnect_required", "requires_reconnect"),
        )

    # ------------------------------------------------------------------
    # Migrated sub-gate: agent-dispatch brief
    # ------------------------------------------------------------------

    def agent_dispatch_brief(
        self,
        *,
        tool_input: Any,
        project_root: Path,
    ) -> ToolGateResult:
        """Refuse Task/Agent dispatches whose brief is research-shaped.

        Caller decides when to call this — typically only when
        ``tool_name in {"task", "agent"}``. The agent_brief_gate
        module owns the actual research-shape detection; this method
        wraps it with the operator-override check + config toggle.

        Returns ``continue`` when:
          - no brief in tool_input
          - ``security.delegate_research_allowed=true`` (operator opt-in)
          - brief passes the gate
          - operator override phrase present in current-turn grants

        Returns ``deny`` when the brief is research-shaped and no
        override applies.
        """
        brief = ""
        if isinstance(tool_input, dict):
            brief = str(tool_input.get("prompt") or "").strip()
        if not brief:
            return ToolGateResult.cont(why=("agent_brief_no_brief",))

        try:
            from .config import get_setting

            if bool(
                get_setting(
                    "security.delegate_research_allowed",
                    project_root=project_root,
                    default=False,
                ),
            ):
                return ToolGateResult.cont(why=("agent_brief_allowed_globally",))
        except Exception:
            pass  # Fall through to safe path

        # Check operator's current-turn override phrases
        override_present = False
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            if managed.get("active") and managed.get("session_id"):
                from .protected_file_runtime import (
                    get_protected_edit_grants,
                )

                grants_blob = " ".join(
                    get_protected_edit_grants() or [],
                )
                lower_blob = grants_blob.lower()
                override_present = any(
                    phrase in lower_blob for phrase in AGENT_RESEARCH_OVERRIDE_PHRASES
                )
        except Exception:
            override_present = False

        try:
            from .agent_brief_gate import evaluate_agent_brief

            decision = evaluate_agent_brief(brief, override_present)
        except Exception:
            # Evaluator boom: fail-open to avoid trapping operator
            # CLASSIFICATION: advisory by explicit design (#agent_brief
            # gate doctrine). Evaluator boom must not trap the operator
            # since the gate refuses based on prompt heuristics, not
            # privilege state. Failing closed here would brick every
            # legitimate Task dispatch on a parser regression.
            return ToolGateResult.cont(why=("agent_brief_evaluator_error",))

        if decision["allowed"]:
            return ToolGateResult.cont(why=("agent_brief_allowed",))

        return ToolGateResult.deny(
            reason=str(decision["reason"]),
            why=("agent_brief_blocked", "agent_brief"),
        )

    # ------------------------------------------------------------------
    # Migrated handler: universal PreToolUse audit
    # ------------------------------------------------------------------

    def record_pretool_audit(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        host_session_id: str,
        project_root: Path,
        payload: dict,
        lane_id: str | None = None,
    ) -> ToolGateResult:
        """Write one ``native_tool_use`` row to execution_events per
        tool-invocation attempt BEFORE gate decisions.

        Without this, denied attempts vanish from the audit trail
        because PostToolUse only fires on allowed calls. Captures
        target-entity preview (≤500 chars of first scalar string in
        tool_input) plus the build_audit_payload forensic fields.

        Resolves the managed session_id from host_session_id (per-
        conductor lookup) so concurrent conductors on the same
        project produce independently-attributed audit rows.

        ``lane_id`` is the caller's resolved current lane id (passed
        through into the audit payload via build_audit_payload).
        Caller resolves it because lane-id resolution differs per host.

        This is a write-only effect — returns ``continue`` with the
        audit_events tuple carrying the row data (for tests that
        want to assert exact audit shape without hitting the store).
        Best-effort: store hiccups never raise.
        """
        try:
            managed = self.runtime.hub.managed_mode.get_mode(
                project_root,
                cli_session_id=host_session_id,
            )
        except Exception:
            # CLASSIFICATION: advisory. The audit row is write-only
            # forensics — losing one means a denied call doesn't appear
            # on the dashboard, but no privilege is granted.
            return ToolGateResult.cont(why=("pretool_audit_managed_error",))

        if not managed.get("active"):
            return ToolGateResult.cont(why=("pretool_audit_unmanaged",))

        sid = str(managed.get("session_id") or "").strip()
        if not sid or not tool_name:
            return ToolGateResult.cont(why=("pretool_audit_skipped",))

        # Extract target preview from tool_input
        target = ""
        if isinstance(tool_input, dict):
            for key in (
                "file_path",
                "path",
                "command",
                "pattern",
                "session_id",
                "url",
                "prompt",
                "query",
            ):
                v = tool_input.get(key)
                if isinstance(v, str) and v.strip():
                    target = v.strip()[:500]
                    break

        action_kind = classify_tool_action(tool_name)
        audit_payload = build_audit_payload(
            tool_name=tool_name,
            tool_input=tool_input,
            payload=payload,
            lane_id=lane_id,
        )
        audit_payload["has_target"] = bool(target)

        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="native_tool_use",
                source_kind="pre_tool_use",
                session_id=sid,
                capability_name=tool_name,
                action_kind=action_kind,
                target_entity=target,
                status="attempted",
                payload=audit_payload,
            )
        except Exception:
            # CLASSIFICATION: advisory (see pretool_audit_managed_error).
            return ToolGateResult.cont(why=("pretool_audit_store_error",))

        return ToolGateResult.cont(
            audit_events=(
                (
                    "native_tool_use",
                    {
                        "source_kind": "pre_tool_use",
                        "session_id": sid,
                        "capability_name": tool_name,
                        "action_kind": action_kind,
                        "target_entity": target,
                        "status": "attempted",
                        "payload": audit_payload,
                    },
                ),
            ),
            why=("pretool_audit_written",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-gate: conductor_comms (lane state + messages)
    # ------------------------------------------------------------------

    def conductor_comms(
        self,
        *,
        lane_id: str,
        project_root: Path,
    ) -> ToolGateResult:
        """Check lane state and surface pending conductor messages.

        Three outcomes per ``conductor_comms.check_lane_and_messages``:
          - lane_state="paused"   → deny ("wait for conductor to resume")
          - lane_state="canceled" → deny ("stop working")
          - lane_state="active"   → cont with pending messages folded
                                    into additional_context_blocks
                                    (prefixed `>>> CONDUCTOR MESSAGE:`)

        OpenCode currently re-implements this via a python subprocess
        call to ``check_lane_and_messages``; after this migration it
        can call into ToolGate.conductor_comms directly.

        ``lane_id`` is the caller's resolved current lane id. When
        empty (no lane bound), this gate returns continue immediately.

        Best-effort: any exception returns continue (lane gate is
        informational from the wider PreToolUse perspective; if the
        comms store hiccups, don't block tools on it).
        """
        if not lane_id:
            return ToolGateResult.cont()

        try:
            from .conductor_comms import check_lane_and_messages

            comms = check_lane_and_messages(project_root, lane_id)
        except Exception:
            # CLASSIFICATION: advisory. Conductor comms surfaces lane
            # pause/cancel + pending messages — informational. Losing
            # it means the agent doesn't see a conductor message but
            # gains no privilege. Pinned by tests in
            # test_conductor_comms_integration.
            return ToolGateResult.cont(why=("conductor_comms_error",))

        state = str(comms.get("lane_state") or "")
        reason_text = str(comms.get("lane_reason") or "")

        if state == "paused":
            return ToolGateResult.deny(
                reason=(
                    f"Lane '{lane_id}' is paused: {reason_text}. Wait for conductor to resume."
                ),
                why=("conductor_comms_lane_paused",),
            )
        if state == "canceled":
            return ToolGateResult.deny(
                reason=(f"Lane '{lane_id}' has been canceled: {reason_text}. Stop working."),
                why=("conductor_comms_lane_canceled",),
            )

        # Active lane — surface pending messages
        pending = comms.get("pending_messages") or []
        if not pending:
            return ToolGateResult.cont(why=("conductor_comms_active",))

        blocks = tuple(f">>> CONDUCTOR MESSAGE: {msg.get('content', '')}" for msg in pending)
        return ToolGateResult.cont(
            additional_context_blocks=blocks,
            why=("conductor_comms_messages",),
        )

    # ------------------------------------------------------------------
    # Batch 2.0-A: structural-only gate for native shell tools
    # ------------------------------------------------------------------

    def structural_block_native_shell(
        self,
        *,
        tool_name: str,
        host_session_id: str,
        project_root: Path,
        is_aidocs_project: bool | None = None,
    ) -> ToolGateResult:
        """Run ONLY the structural blocks that sit ABOVE ShellPolicy and
        return the first terminal block, or cont() if none.

        Runs: managed_mode_required → reconnect_required →
        session_freeze_pretool.

        Deliberately does NOT run (Batch 2.0-A doctrine):
          * kill_switch_bypass — a debug / break-glass allow must NEVER let
            a host-native process run in 2.0-A; skipping it means a native
            shell call can't be allowed-to-execute via the bypass.
          * orchestrator_check — ShellPolicy/ShellEnforcement owns the
            native-shell verdict slice (single authority).
        Mints NOTHING — the single freeze mint lives in ShellEnforcement.
        """
        if is_aidocs_project is None:
            try:
                from .mcp_server_runtime_helpers import _has_marker

                is_aidocs_project = bool(_has_marker(project_root))
            except Exception:
                is_aidocs_project = False
        mm = self.managed_mode_required(
            tool_name=tool_name,
            host_session_id=host_session_id,
            project_root=project_root,
            is_aidocs_project=is_aidocs_project,
        )
        if mm.is_terminal:
            return mm
        rc = self.reconnect_required(
            tool_name=tool_name,
            host_session_id=host_session_id,
            project_root=project_root,
        )
        if rc.is_terminal:
            return rc
        fz = self.session_freeze_pretool(project_root=project_root)
        if fz.is_terminal:
            return fz
        return ToolGateResult.cont(why=("shell_native_2a_structural_pass",))

    # ------------------------------------------------------------------
    # Migrated sub-gate: AgentOrchestrator.check_tool dispatch
    # ------------------------------------------------------------------

    def orchestrator_check(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        project_root: Path,
    ) -> ToolGateResult:
        """Dispatch to AgentOrchestrator.check_tool and translate the
        ToolDecision into a host-agnostic ToolGateResult.

        OpenCode currently bypasses this entirely (re-implements its
        own gates inline); OpenAI Agents calls check_tool then raises.
        After this migration, every host gets the same gate composition:

          decision.allowed=True            → ToolGateResult.cont
          decision.allowed=False
            needs_confirmation=True        → ToolGateResult.ask (freeze)
            needs_confirmation=False       → ToolGateResult.deny

        When ``needs_confirmation`` is True, the service also creates
        the freeze envelope data via ``freeze_service.build_freeze_response``
        and carries it in ``additional_context_blocks`` so the host
        adapter can render it into its envelope shape. The freeze
        creation itself is host-agnostic (same primitive ai_run uses).

        Caller passes the resolved managed session_id when available
        so the freeze can be bound to it. When no session_id is
        resolvable, we degrade to plain deny — can't freeze without
        a session to attach to.
        """
        try:
            from .agent_orchestrator import AgentOrchestrator
        except Exception:
            return ToolGateResult.cont(why=("orchestrator_unavailable",))

        try:
            orch = AgentOrchestrator(self.runtime)
            decision = orch.check_tool(project_root, tool_name, tool_input)
        except Exception as exc:
            # Fail closed: gate lookup error → deny.
            return ToolGateResult.deny(
                reason=f"orchestrator gate error: {exc}",
                why=("orchestrator_error",),
            )

        if decision.allowed:
            return ToolGateResult.cont(why=("orchestrator_allow",))

        if not decision.needs_confirmation:
            return ToolGateResult.deny(
                reason=_sanitize_operator_reason(
                    decision.reason or "denied by orchestrator",
                ),
                why=("orchestrator_deny", decision.blocked_by or "denied"),
            )

        # needs_confirmation path: build the freeze envelope data
        managed_session_id = ""
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            managed_session_id = (
                str(managed.get("session_id") or "").strip() if managed.get("active") else ""
            )
        except Exception:
            managed_session_id = ""

        if not managed_session_id:
            # Can't freeze without a session — degrade to deny.
            return ToolGateResult.deny(
                reason=decision.reason or "confirmation required (no session)",
                why=("orchestrator_confirm_no_session",),
            )

        try:
            from .freeze_service import build_freeze_response

            env = build_freeze_response(
                project_root,
                managed_session_id,
                tool_name=tool_name,
                tool_input=tool_input,
                judge_summary=decision.reason or "",
                admin_tier=False,
            )
        except Exception:
            # Mint failed (FreezeMintError or otherwise). Hard-deny with a
            # truthful reason — NO hollow ask, NO FREEZE marker, NO "Type
            # exactly" prompt that would resolve against nothing.
            return ToolGateResult.deny(
                reason=(
                    "operator approval could not be created; action remains "
                    "blocked; retry or contact admin"
                ),
                why=("orchestrator_freeze_mint_failed",),
            )

        # Defensive: build_freeze_response now guarantees a freeze_state
        # with request_id on success, but never emit an ask without one.
        # A dict freeze_state must carry request_id; an absent/empty
        # freeze_state is hollow and denies.
        _fs = env.get("freeze_state")
        _hollow = (not _fs) or (isinstance(_fs, dict) and not _fs.get("request_id"))
        if _hollow:
            return ToolGateResult.deny(
                reason=(
                    "operator approval could not be created; action remains "
                    "blocked; retry or contact admin"
                ),
                why=("orchestrator_freeze_mint_failed",),
            )

        # Carry freeze envelope data as an additional_context_block so
        # hosts can render. The host will likely return its own
        # block-shaped envelope using these fields; the service
        # produces them once.
        freeze_marker = "FREEZE\n" + json.dumps(
            {
                "reason": env.get("permissionDecisionReason", ""),
                "blocked_by": env.get("blocked_by", "judge_confirm_required"),
                "freeze_state": env.get("freeze_state", ""),
            },
        )
        return ToolGateResult.ask(
            reason=env.get("permissionDecisionReason", "")
            or decision.reason
            or "confirmation required",
            additional_context_blocks=(freeze_marker,),
            why=(
                "orchestrator_confirm",
                env.get("blocked_by", "judge_confirm_required"),
            ),
        )

    # ------------------------------------------------------------------
    # Migrated sub-gate: kill-switch bypass
    # ------------------------------------------------------------------

    def kill_switch_bypass(
        self,
        *,
        tool_name: str,
        host_session_id: str,
        project_root: Path,
        log_bypass: bool = True,
    ) -> ToolGateResult:
        """The operator's break-glass escape hatch.

        When ``dev.kill_switch`` is True at session / project / global
        scope (session > project > global cascade), every PreToolUse
        gate short-circuits to ALLOW. Emits one audit event per bypass
        so compliance can reconstruct the exemption window.

        Per emperor-doctrine §VI: kill-switch is BYPASS not refuse-all.
        It exists for deadlock recovery + emergency work, never for
        normal operation. Fires BEFORE the reconnect gate.

        ``host_session_id`` is the per-call host UUID (Claude Code's
        payload.session_id, OpenCode's hookCtx.sessionID, etc.).
        Resolved to the AIDOCS managed session_id via per-conductor
        lookup so a switch flipped for THIS session bypasses while
        other sessions on the same project keep enforcing.

        Returns:
          - ``allow`` with ``why=("kill_switch_bypass",)`` when the
            switch is active; ``audit_events`` carries one
            ``enforcement_bypass`` row the host writes.
          - ``continue`` when the switch is not active.

        """
        aidocs_session_id: str | None = None
        if host_session_id:
            try:
                managed = self.runtime.hub.managed_mode.get_mode(
                    project_root,
                    cli_session_id=host_session_id,
                )
                aidocs_session_id = str(managed.get("session_id") or "").strip() or None
            except Exception:
                aidocs_session_id = None

        try:
            from .enforcement import is_kill_switch_active

            active = bool(
                is_kill_switch_active(
                    project_root,
                    session_id=aidocs_session_id,
                ),
            )
        except Exception:
            # Fail closed-continue: kill-switch lookup error → don't
            # bypass, let downstream gates evaluate normally.
            # CLASSIFICATION: fail-closed-by-continue. Kill-switch
            # lookup error means we DON'T grant the operator's
            # break-glass; downstream security gates evaluate normally.
            # This is the safe direction — the gate never makes tools
            # MORE allowed on error.
            return ToolGateResult.cont(why=("kill_switch_lookup_error",))

        if not active:
            return ToolGateResult.cont()

        audit: tuple[tuple[str, dict], ...] = ()
        if log_bypass:
            audit = (
                (
                    "enforcement_bypass",
                    {
                        "tool_name": tool_name,
                        "host_session_id": host_session_id,
                        "aidocs_session_id": aidocs_session_id,
                        "scope": "session" if aidocs_session_id else "project_or_global",
                    },
                ),
            )
        return ToolGateResult.allow(
            reason="kill_switch_active",
            audit_events=audit,
            why=("kill_switch_bypass",),
        )
