"""CLI bridge from non-Python host adapters to AIDOCS core services.

JavaScript / shell / future-language host adapters cannot import the
Python service modules directly. This CLI shim lets them invoke the
host-agnostic services via subprocess + JSON.

Invocation:

    python -m aidocs_mcp.host_adapter_cli <event_kind>

reads JSON from stdin, writes JSON result to stdout. Event kinds:

    pretool         → ToolGate full composition (kill-switch →
                      managed-mode → audit → orchestrator).
                      Input:  {tool_name, tool_input, host_session_id,
                              project_root, payload, lane_id?}
                      Output: {verdict, reason, additional_context_blocks,
                              why}
    posttool        → LifecycleService.on_post_tool_use_audit +
                      on_tool_end_output_guard combined.
                      Input:  {tool_name, tool_input, tool_response,
                              host_session_id, project_root, payload,
                              agent_id?, lane_id?}
                      Output: {audit_events, output_guard_findings, why}
    prompt_mutate   → PromptMutator.mutate_prompt + a few extra
                      sub-pipelines OpenCode-style hosts typically run.
                      Input:  {prompt, payload, project_root,
                              host_session_id, managed_session_id?}
                      Output: {decision, rewritten_prompt,
                              additional_context_blocks, why}
    session_start   → LifecycleService.build_session_start_context.
                      Input:  {host_kind, host_session_id, project_root,
                              is_worker_proc}
                      Output: {context}
    compact         → LifecycleService.on_post_compact.
                      Input:  {host_kind, host_session_id, project_root}
                      Output: {side_effects, why}

Failure policy is event-kind-specific:

- ``pretool`` is a **security-relevant** event. Any failure (unknown
  event, invalid stdin, handler exception, serialization failure)
  returns ``{"verdict": "deny", "reason": "<error>", "error": "<error>"}``.
  Hosts that route through the CLI for pretool gating MUST treat
  ``verdict=="deny"`` as a hard refusal. CLI-unreachable on the host
  side (e.g. JS subprocess timeout) MUST also be treated as deny —
  not continue — by the calling adapter.

- Other event kinds (``posttool``, ``prompt_mutate``, ``session_start``,
  ``compact``) are informational/best-effort. Failures return
  ``{"verdict": "continue", "error": "<error>"}`` so the host can
  degrade gracefully.

This is the doctrine encoded in /goal: "fail closed where security
decisions are undecided."

This is the bridge that makes the cross-host parity contract REAL
for non-Python hosts. JS plugins that previously re-implemented
gates inline now subprocess one Python call per event.

Operator-intent support (audit 2026-05-20)
------------------------------------------
Natural-language operator-intent routes (decision-trace toggle, bash
allowlist add/remove/show — see ``operator_intent_resolver``) are
**Claude-hook-only**. They are invoked exclusively from
``claude_hook._handle_user_prompt_submit`` via
``OperatorIntentResolver.resolve_and_apply``.

This bridge's ``prompt_mutate`` / ``oc_chat_message`` events route
through ``PromptMutator.mutate_prompt``, which deliberately does NOT
invoke the operator-intent resolver. So for OpenCode and any other
host that drives prompts through this CLI, operator intent is
**UNSUPPORTED_EXPLICIT_NOOP**:

  - the prompt is processed normally (security/freeze/classification);
  - NO config or bash-policy mutation happens from natural language;
  - and — crucially — when the prompt LOOKS like an operator-intent
    request (per ``operator_intent_resolver.looks_like_operator_intent``)
    the bridge surfaces an explicit warning in additional_context /
    session_prompt_context and sets ``operator_intent_unsupported=True``.

"Unsupported" therefore means an explicit "not supported, nothing
changed" notice — NOT a silent noop. The warning never says applied /
success / done, so a conversational agent cannot fake success on a
host where the mutation did not (and cannot) happen.

Fail-visible: if the canonical classifier is unavailable or raises, a
tiny dependency-free shape fallback still surfaces a DISTINCT "detection
degraded" warning for obvious operator-intent prompts — so a broken
detector can never silently re-introduce the noop. The fallback only
produces a warning string; it never authorizes or mutates.

This is intentional, not drift. Operator intent is gated on a
host_binding OperatorContext that only the Claude hook path resolves;
re-implementing it in JS (parallel authority) or mutating config
directly from the bridge is forbidden.

Adapter classification (audit 2026-05-20, pinned by
tests/host/test_operator_intent_adapter_audit.py):

  Claude hook              SUPPORTED_CANONICAL
  OpenCode plugin (JS)     READ_SIDE_STATEFUL_ADAPTER_NO_OPERATOR_INTENT_MUTATION
  host_adapter_cli prompt  UNSUPPORTED_EXPLICIT_NOOP
  OpenAI agents adapter    UNSUPPORTED_SAFE_NOOP (no prompt hooks)
  MCP tools                EXPLICIT_RBAC_TOOL / NOT_NLP_OPERATOR_INTENT
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

# Shown when the canonical detector is UNAVAILABLE/BROKEN but the prompt
# still has an obvious operator-intent shape. Distinct from the normal
# unsupported note so operators (and tests) can tell "detector degraded"
# from "detector ran and host doesn't support intent". Self-contained —
# no resolver import — because the resolver may be exactly what's broken.
_OPERATOR_INTENT_DEGRADED_NOTE: str = (
    "Operator intent detection is degraded in this host. No config/bash-policy "
    "change was made. Use Claude-hook path or dashboard."
)


def _degraded_operator_intent_shape(prompt: str) -> bool:
    """Tiny, dependency-free fallback detector used ONLY when the canonical
    classifier is unavailable, and ONLY to decide whether to surface a
    visibility WARNING.

    It NEVER authorizes, NEVER mutates, NEVER decides permissions, and
    NEVER reaches the resolver. It matches obvious shapes of the
    currently-supported routes only:
      - decision trace + session
      - bash allowlist + session
      - add/remove/show + bash allowlist
    Conservative by design: a miss just means no warning (the bridge
    still mutates nothing); a hit only ever produces a warning string.
    """
    p = (prompt or "").lower()
    if not p:
        return False
    has_session = "session" in p
    decision_trace = "decision" in p and "trace" in p
    bash_allow = "bash" in p and ("allowlist" in p or "allow list" in p)
    verb = any(v in p for v in ("add", "remove", "show"))
    return (decision_trace and has_session) or (bash_allow and has_session) or (bash_allow and verb)


def _build_origin_context(
    event_kind: str,
    payload: dict,
    *,
    strict: bool = False,
) -> dict:
    """Build the prompt-origin context for the gate from a bridge payload.

    The canonical worker signal (AIDOCS_EXPERT_LANE_ID, set on lane-worker
    processes for both CC and OpenCode) is read here so a worker-spawned
    bridge process is recognized even when the payload omits markers.

    Principal attribution:
      - worker process (AIDOCS_EXPERT_LANE_ID) → 'subagent' (never human).
      - else the payload's explicit principal_type, if any.
      - else: ``strict=True`` (AUTHORITY-BEARING grant/mutation path) →
        'unknown' (fail closed — rule: do NOT default a missing bridge
        principal to human for authority). ``strict=False`` (unsupported-
        warning UX only) → 'human' (permissive; warning never mutates).
    """
    import os

    worker_lane = os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()
    payload_principal = str(payload.get("principal_type") or "").strip()
    if worker_lane:
        principal = "subagent"
    elif payload_principal:
        principal = payload_principal
    else:
        principal = "unknown" if strict else "human"
    return {
        "event_kind": event_kind,
        "principal_type": principal,
        "host_session_id": str(payload.get("host_session_id") or "").strip(),
        "project_root": str(payload.get("project_root") or "").strip(),
        "worker_lane_id": worker_lane,
        "is_worker": bool(payload.get("is_worker") or payload.get("is_worker_proc")),
        "source_surface": str(payload.get("source_surface") or ""),
        "delivery": str(payload.get("delivery") or ""),
    }


def _grant_eligible_for_payload(event_kind: str, payload: dict) -> bool:
    """Strict authority-bearing eligibility for a bridge prompt. Used to
    decide whether PromptMutator may run grant/mutation/confirmation
    pipelines. Missing principal → 'unknown' → ineligible (fail closed).
    """
    try:
        from .operator_intent_resolver import (
            is_authority_bearing_prompt_eligible,
        )
    except Exception:
        return False
    return bool(
        is_authority_bearing_prompt_eligible(
            _build_origin_context(event_kind, payload, strict=True),
        ),
    )


def _operator_intent_unsupported_note(prompt: str, context: dict) -> str:
    """Return the unsupported-operator-intent warning when ``prompt`` is a
    verified direct-human prompt that looks like an operator-intent
    request, else "".

    ORIGIN-BOUND: the prompt-origin gate runs FIRST. A worker / -p / -q /
    delegated / replayed / automation prompt is inert — we do NOT even run
    detection on it, even if it contains an exact operator phrase. Only an
    eligible direct-human submit proceeds to detection.

    Operator intent is Claude-hook-only (it needs a host_binding
    OperatorContext this bridge never resolves). For an eligible human
    prompt, rather than silently ignoring it — which lets a conversational
    agent fake success — non-Claude hosts surface a notice. The bridge
    NEVER mutates and NEVER calls resolve_and_apply.

    Fail-visible: for an eligible prompt the canonical classifier is
    primary, but if it is unavailable or raises, a tiny dependency-free
    fallback surfaces a DEGRADED warning instead of a silent noop.
    """
    if not prompt:
        return ""
    # Origin gate — must verify a direct-human submit before ANY detection.
    try:
        from .operator_intent_resolver import is_operator_intent_eligible_prompt
    except Exception:
        # Cannot verify origin → must not act (origin-bound law wins).
        return ""
    if not is_operator_intent_eligible_prompt(context):
        return ""
    # Eligible direct-human prompt: run detection (with degraded fallback).
    try:
        from .operator_intent_resolver import (
            OPERATOR_INTENT_UNSUPPORTED_NOTE,
            looks_like_operator_intent,
        )

        return (
            OPERATOR_INTENT_UNSUPPORTED_NOTE
            if looks_like_operator_intent(
                prompt,
            )
            else ""
        )
    except Exception:
        # Detector unavailable / raised — fall back to the shape detector
        # for WARNING VISIBILITY only (never authorization or mutation).
        if _degraded_operator_intent_shape(prompt):
            return _OPERATOR_INTENT_DEGRADED_NOTE
        return ""


def _result_to_dict(result: Any) -> dict:
    """Convert a ToolGateResult / PromptMutationResult / LifecycleResult
    into a JSON-serializable dict. Tuples become lists; everything
    else passes through dataclass asdict().
    """
    if is_dataclass(result):
        out = asdict(result)
    elif isinstance(result, dict):
        out = dict(result)
    else:
        out = {"value": str(result)}

    # Normalize tuples → lists for JSON
    def _norm(v: Any) -> Any:
        if isinstance(v, tuple):
            return [_norm(x) for x in v]
        if isinstance(v, list):
            return [_norm(x) for x in v]
        if isinstance(v, dict):
            return {k: _norm(x) for k, x in v.items()}
        return v

    return _norm(out)


def _runtime_for_cli(project_root: Path) -> Any:
    """Build a minimal runtime that exposes hub services to the
    host-agnostic services. The CLI invocation runs in a fresh
    process; runtime construction must be cheap.
    """
    from .runtime_service import RuntimeService
    from .service_hub import AidocsServiceHub

    templates_root = Path(__file__).parent / "data"
    hub = AidocsServiceHub(templates_root=templates_root)
    runtime = RuntimeService(hub=hub)
    return runtime


def _handle_pretool(payload: dict) -> dict:
    """Thin shim. The pretool pipeline composition lives in
    ToolGate.evaluate_tool; this handler only translates the CLI's
    JSON envelope into Python kwargs and back.
    """
    from .tool_gate_service import ToolGate

    project_root = Path(payload["project_root"])
    runtime = _runtime_for_cli(project_root)
    result = ToolGate(runtime).evaluate_tool(
        tool_name=str(payload.get("tool_name") or ""),
        tool_input=payload.get("tool_input") or {},
        host_session_id=str(payload.get("host_session_id") or ""),
        project_root=project_root,
        payload=payload.get("payload") or {},
        lane_id=payload.get("lane_id"),
    )

    # ShellPolicy shadow (Batch 1.6, observe-only). Side-effect-free:
    # consumes the already-computed live verdict, never re-runs the
    # cascade, never blocks. host_kind drives the capability matrix; an
    # unknown / unproven host fails closed to skipped_unguardable.
    try:
        from .shell_policy_shadow import run_pretool_shadow

        run_pretool_shadow(
            project_root=project_root,
            host=str(payload.get("host_kind") or "unknown"),
            tool_name=str(payload.get("tool_name") or ""),
            tool_input=payload.get("tool_input") or {},
            host_session_id=str(payload.get("host_session_id") or ""),
            live_verdict=str(result.verdict or ""),
            live_reason=str(result.reason or ""),
            live_why=tuple(result.why or ()),
        )
    except Exception:
        pass

    return _result_to_dict(result)


def _handle_posttool(payload: dict) -> dict:
    from .lifecycle_service import LifecycleService

    project_root = Path(payload["project_root"])
    runtime = _runtime_for_cli(project_root)
    lc = LifecycleService(runtime)

    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or ""
    host_session_id = str(payload.get("host_session_id") or "")
    inner_payload = payload.get("payload") or {}
    agent_id = str(payload.get("agent_id") or "")
    lane_id = payload.get("lane_id")

    # Output-guard scan
    result_text = (
        tool_response if isinstance(tool_response, str) else json.dumps(tool_response, default=str)
    )
    guard = lc.on_tool_end_output_guard(
        tool_name=tool_name,
        result_text=result_text,
        host_session_id=host_session_id,
        agent_id=agent_id,
        project_root=project_root,
    )

    # Host READ output secret policy (read-gate content layer). For hosts
    # that can replace output before context, returns redacted_text +
    # audits host_read_output_redacted; otherwise a forensic
    # host_read_output_guard_finding (status=degraded) — the PreToolUse
    # path block is the real defense there. host_kind drives capability.
    host_read_guard = None
    host_read_redacted_text = None
    if tool_name.strip().lower() == "read":
        host_read_guard = lc.on_host_read_output(
            tool_name=tool_name,
            path=str(_extract_read_path(tool_input)),
            result_text=result_text,
            host_session_id=host_session_id,
            host_kind=str(payload.get("host_kind") or ""),
            project_root=project_root,
            agent_id=agent_id,
        )
        host_read_redacted_text = host_read_guard.redacted_text

    # Universal post-tool audit
    audit = lc.on_post_tool_use_audit(
        tool_name=tool_name,
        tool_input=tool_input,
        tool_response=tool_response,
        host_session_id=host_session_id,
        project_root=project_root,
        payload=inner_payload,
        lane_id=lane_id,
    )

    # Edit-lifecycle follow-through nudge (2026-05-20). When a write/
    # edit tool just ran in a managed session, fold the task-lifecycle
    # nudge string into additional_context_blocks so hosts that
    # forward it (OpenCode session prompt context, CC additionalContext)
    # show the next-step hint without each host re-implementing the
    # decision tree. Pure derivation from runtime.host_state().
    nudge_blocks: list[str] = []
    try:
        normalized_tool = tool_name.lower()
        if normalized_tool in {
            "edit",
            "write",
            "multiedit",
            "ai_replace",
            "ai_edit_lines",
            "ai_str_replace",
            "ai_batch_edit",
            "ai_create_file",
            "ai_insert_lines",
        }:
            try:
                host_state = runtime.host_state(project_root)
            except Exception:
                host_state = {}
            lifecycle = (
                host_state.get("lifecycle_state", {}) if isinstance(host_state, dict) else {}
            )
            nudge = lc.build_followthrough_nudge(lifecycle)
            if nudge:
                nudge_blocks.append(nudge)
    except Exception:
        # Nudge derivation is best-effort; never fails the posttool path.
        pass

    out = {
        "audit_events": (
            _result_to_dict(audit).get("audit_events", [])
            + _result_to_dict(guard).get("audit_events", [])
            + (_result_to_dict(host_read_guard).get("audit_events", []) if host_read_guard else [])
        ),
        "output_guard_findings": _result_to_dict(guard).get("side_effects", []),
        "additional_context_blocks": nudge_blocks,
        "why": list(audit.why)
        + list(guard.why)
        + (list(host_read_guard.why) if host_read_guard else []),
    }
    # Only present when the host can pre-context redact AND a secret was
    # found — the host substitutes the tool result with this text.
    if host_read_redacted_text is not None:
        out["redacted_tool_response"] = host_read_redacted_text
    return out


def _extract_read_path(tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "filePath", "path", "target"):
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _handle_prompt_mutate(payload: dict) -> dict:
    from .prompt_mutator import PromptMutator

    project_root = Path(payload["project_root"])
    runtime = _runtime_for_cli(project_root)
    pm = PromptMutator(runtime)

    inner_payload = payload.get("payload") or {}
    if "prompt" not in inner_payload and payload.get("prompt"):
        inner_payload["prompt"] = payload["prompt"]
    if "session_id" not in inner_payload and payload.get("host_session_id"):
        inner_payload["session_id"] = payload["host_session_id"]

    # ORIGIN-BOUND LAW: authority-bearing pipelines (grants, per-turn
    # intent state, DNT/config-set grants, lane-exit, freeze/approval
    # consumption, intent-phrase dispatch) run ONLY for a verified
    # direct-human origin. Strict eligibility (no human default for a
    # missing principal — rule 7). Worker / -p / -q / delegated /
    # compaction / handoff / replay prompts run ALWAYS-SAFE steps only.
    grant_eligible = _grant_eligible_for_payload("prompt_mutate", payload)
    result = pm.mutate_prompt(
        inner_payload,
        project_root,
        grant_eligible=grant_eligible,
    )
    out = _result_to_dict(result)
    # Explicit unsupported-operator-intent warning — ORIGIN-GATED. A
    # worker/-p/-q/delegated prompt is inert even if it contains the
    # exact operator phrase.
    note = _operator_intent_unsupported_note(
        str(inner_payload.get("prompt") or ""),
        _build_origin_context("prompt_mutate", payload),
    )
    if note:
        blocks = out.get("additional_context_blocks")
        blocks = list(blocks) if isinstance(blocks, (list, tuple)) else []
        blocks.append(note)
        out["additional_context_blocks"] = blocks
        out["operator_intent_unsupported"] = True
    return out


def _handle_session_start(payload: dict) -> dict:
    from .lifecycle_service import LifecycleService

    project_root = Path(payload["project_root"])
    runtime = _runtime_for_cli(project_root)
    lc = LifecycleService(runtime)

    context = lc.build_session_start_context(
        host_kind=str(payload.get("host_kind") or "unknown"),
        host_session_id=str(payload.get("host_session_id") or ""),
        project_root=project_root,
        is_worker_proc=bool(payload.get("is_worker_proc")),
    )
    return {"context": context}


def _build_compaction_blocks(project_root: Path, session_id: str) -> list[str]:
    """Assemble the structured continuation-summary blocks the host
    should prepend to its compaction prompt. Mirror of the JS
    buildCompactionContext, now canonical-Python so every host (OC /
    CC / Codex) gets the same compaction context with identical
    output shape.

    Reads:
      - canonical memory_index (top 10 active durable-memory paths) — SQLite-only
        doctrine (2026-06): no .MEMORY/INDEX.md markdown read
      - ROADMAP_2_0_0.md / ROADMAP.md / mcp/ROADMAP.md (first match)
      - .MEMORY/sessions/<sid>/SESSION.md
      - .MEMORY/sessions/<sid>/plans/PLAN.md
      - .MEMORY/sessions/<sid>/<sid>.handoff.md
      - .MEMORY/sessions/<sid>/journal.md (last 8 backtick-bulleted)

    Best-effort: missing files / read errors are silently skipped.
    """

    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _take_first_nonempty(text: str, n: int) -> list[str]:
        out: list[str] = []
        for line in text.splitlines():
            if line.strip():
                out.append(line)
                if len(out) >= n:
                    break
        return out

    blocks: list[str] = []
    # SQLite-only doctrine (2026-06): durable memory is canonical in memory_index;
    # the legacy .MEMORY/INDEX.md is retired and NOT read. Summarize the canonical
    # active rows instead.
    try:
        from . import memory_sqlite_store as _msq

        entries = _msq.list_entries(project_root)[:10]
        if entries:
            blocks.append(
                "Memory index (canonical):\n"
                + "\n".join(f"- {e.path}" for e in entries),
            )
    except Exception:
        pass

    for candidate in (
        project_root / "ROADMAP_2_0_0.md",
        project_root / "ROADMAP.md",
        project_root / "mcp" / "ROADMAP.md",
    ):
        text = _read(candidate)
        if text:
            blocks.append(
                "Roadmap:\n" + "\n".join(f"- {ln}" for ln in _take_first_nonempty(text, 10)),
            )
            break

    if session_id:
        sess_dir = project_root / ".MEMORY" / "sessions" / session_id
        session_text = _read(sess_dir / "SESSION.md")
        if session_text:
            blocks.append(
                "Session:\n"
                + "\n".join(f"- {ln}" for ln in _take_first_nonempty(session_text, 12)),
            )
        plan_text = _read(sess_dir / "plans" / "PLAN.md")
        if plan_text:
            blocks.append(
                "Plan:\n" + "\n".join(f"- {ln}" for ln in _take_first_nonempty(plan_text, 12)),
            )
        handoff_text = _read(sess_dir / f"{session_id}.handoff.md")
        if handoff_text:
            blocks.append(
                "Handoff:\n"
                + "\n".join(f"- {ln}" for ln in _take_first_nonempty(handoff_text, 12)),
            )
        journal_text = _read(sess_dir / "journal.md")
        if journal_text:
            backtick_lines = [
                ln.strip() for ln in journal_text.splitlines() if ln.strip().startswith("- `")
            ][-8:]
            if backtick_lines:
                blocks.append("Recent journal:\n" + "\n".join(f"- {ln}" for ln in backtick_lines))
    return blocks


_COMPACTION_PREAMBLE: tuple[str, ...] = (
    "Ignore the default generic compaction style.",
    "Create a continuation summary that preserves AIDOCS structured state first.",
    "Read and preserve the important information from project memory, "
    "roadmap, session plan, handoff, and session journal.",
    "Prioritize current actionable work, blockers, what failed, and next "
    "steps over conversational filler.",
    "Do not duplicate long prose if the same information already exists in structured artifacts.",
    "Produce a concise but complete continuation summary for the next agent.",
)


def _handle_compact(payload: dict) -> dict:
    from .lifecycle_service import LifecycleService

    project_root = Path(payload["project_root"])
    runtime = _runtime_for_cli(project_root)
    lc = LifecycleService(runtime)

    host_session_id = str(payload.get("host_session_id") or "")
    # Side effects (token reset, epoch bump, grace stamp).
    result = lc.on_post_compact(
        host_kind=str(payload.get("host_kind") or "unknown"),
        host_session_id=host_session_id,
        project_root=project_root,
    )

    # Assemble the continuation prompt the host renders verbatim.
    # Resolve managed session id from the runtime so cross-host calls
    # produce the same blocks for the same session.
    managed_sid = ""
    try:
        managed = runtime.hub.managed_mode.get_mode(project_root)
        if managed.get("active"):
            managed_sid = str(managed.get("session_id") or "").strip()
    except Exception:
        managed_sid = ""
    target_sid = managed_sid or host_session_id

    blocks = _build_compaction_blocks(project_root, target_sid)
    prompt = "\n\n".join(list(_COMPACTION_PREAMBLE) + list(blocks))

    out = _result_to_dict(result)
    out["prompt"] = prompt
    out["compaction_blocks"] = blocks
    return out


def _handle_oc_chat_message(payload: dict) -> dict:
    """OpenCode chat.message event — Phase-3 thin-adapter migration.

    Computes the entire shape the JS chat.message hook needs to render:

      session_prompt_context — the composed system-prompt text (was
                               buildPromptContext in JS)
      session_classification — action_kind for messages.transform
                               directive injection (was classifyPromptAction)
      should_inject_startup  — whether the JS should fold session-start
                               blocks into the context this turn
      startup_blocks         — the structured session-start blocks
                               when should_inject_startup is true
      is_aidocs_command      — when True, the prompt was '/aidocs' or
                               the equivalent plain-text form; JS
                               should set the activeCommand marker
                               and use buildAidocsExecutionPrompt
                               (kept JS-side as a host-specific
                               command-routing concern, not law)

    The JS hook stops MAKING decisions and just RENDERS what the
    canonical service produces.
    """
    from .intent_guard import classify_action
    from .lifecycle_service import LifecycleService
    from .prompt_mutator import PromptMutator

    project_root = Path(payload["project_root"])
    runtime = _runtime_for_cli(project_root)
    host_session_id = str(payload.get("host_session_id") or "")
    prompt_text = str(payload.get("prompt") or "")
    active_command = str(payload.get("active_command") or "")
    startup_already_injected = bool(payload.get("startup_already_injected"))

    out: dict = {
        "session_prompt_context": "",
        "session_classification": "",
        "should_inject_startup": False,
        "startup_blocks": [],
        "is_aidocs_command": False,
    }

    # /aidocs command detection — same canonical condition the JS used.
    stripped = prompt_text.strip().lstrip("/").lower()
    if active_command == "aidocs" or stripped.startswith("aidocs"):
        out["is_aidocs_command"] = True
        return out

    # Run the canonical PromptMutator pipeline so security / freeze /
    # grant / intent-phrase decisions all fire through the same
    # service CC uses. ORIGIN-BOUND: authority-bearing pipelines run only
    # for a verified direct-human origin (strict eligibility — a missing
    # principal fails closed, never defaults to human).
    try:
        pm = PromptMutator(runtime)
        pm_result = pm.mutate_prompt(
            {
                "prompt": prompt_text,
                "session_id": host_session_id,
            },
            project_root,
            grant_eligible=_grant_eligible_for_payload(
                "oc_chat_message",
                payload,
            ),
        )
        ctx_blocks = list(pm_result.additional_context_blocks)
    except Exception:
        ctx_blocks = []

    # Pull host_state for managed-mode / lifecycle / imported skills.
    try:
        host_state = runtime.host_state(project_root)
    except Exception:
        host_state = {}

    managed = bool(host_state.get("managed", False)) if isinstance(host_state, dict) else False
    session_id_resolved = ""
    if isinstance(host_state, dict):
        session_id_resolved = str(host_state.get("session_id") or "")

    startup_state = ""
    if isinstance(host_state, dict):
        startup_state = str(host_state.get("startup_state") or "")

    should_inject_startup = bool(
        managed
        and startup_state == "ready"
        and session_id_resolved
        and not startup_already_injected,
    )
    out["should_inject_startup"] = should_inject_startup

    if should_inject_startup:
        try:
            lc = LifecycleService(runtime)
            ctx = lc.build_session_start_context(
                host_kind="opencode",
                host_session_id=host_session_id,
                project_root=project_root,
                is_worker_proc=False,
            )
            if ctx:
                out["startup_blocks"] = [ctx] if isinstance(ctx, str) else list(ctx)
        except Exception:
            out["startup_blocks"] = []

    # Compose the session prompt context the JS would have built
    # via buildPromptContext. PromptMutator-produced additional
    # context blocks come first (they carry the security/freeze
    # advisories), then a managed-mode summary block.
    composed: list[str] = []
    if ctx_blocks:
        composed.extend(ctx_blocks)
    if managed and session_id_resolved:
        composed.append(
            f"AIDOCS-managed mode is active for this project. "
            f"Bound session: `{session_id_resolved}`. "
            f"Stay in the bound AIDOCS session and call the canonical "
            f"AIDOCS tools for memory/task/session lifecycle.",
        )
    if should_inject_startup and out["startup_blocks"]:
        composed.append(
            "Session start context for this conversation:\n" + "\n\n".join(out["startup_blocks"]),
        )
    # Explicit unsupported-operator-intent warning — ORIGIN-GATED.
    _oi_note = _operator_intent_unsupported_note(
        prompt_text,
        _build_origin_context("oc_chat_message", payload),
    )
    if _oi_note:
        composed.append(_oi_note)
        out["operator_intent_unsupported"] = True

    out["session_prompt_context"] = "\n\n".join(composed)

    # Classification for downstream messages.transform directive
    # injection. classify_action is the canonical Python entry the
    # JS used to subprocess into.
    if managed:
        try:
            cls = classify_action(prompt_text)
            out["session_classification"] = (
                str(
                    cls.get("action_kind") or "",
                )
                if isinstance(cls, dict)
                else ""
            )
        except Exception:
            out["session_classification"] = ""

    return out


# Mirror of the JS plugin's ACTION_TOOL_DIRECTIVES table. The
# strings are canonical AIDOCS guidance for each action_kind; keeping
# them in Python lets the canonical service compose the full
# message-transform envelope without the JS needing to know the
# content. The JS plugin still exposes a JS copy for backwards-
# compat with existing tests, but the active rendering path goes
# through this Python table.
_ACTION_TOOL_DIRECTIVES: dict = {
    "edit": (
        "`task_begin` → `ai_get_lines` (read) → `ai_edit_lines` or "
        "`ai_batch_edit` (write) → `task_complete`. Do NOT mix edit "
        'methods. Before editing: `ai_get_symbol_info(kind="signature")` '
        'or `kind="constructor"` to confirm signatures. '
        'CSS: `ai_trace(class, mode="css_class")`. '
        'DB: `schema_query(entity, mode="entity")`.'
    ),
    "trace": (
        '`ai_find(query, mode="references")` → '
        '`ai_trace(query, mode="field_flow"|"css_class"|"api_to_ui")`. '
        'DB: `schema_query("Source→Target", mode="trace_path")`.'
    ),
    "understand": (
        "`session_resume_bundle` (project/session/skills/plan overview) → "
        "`action_surface_current_session_bundle` (likely next tools) → "
        '`ai_find(query, mode="symbols")` → `ai_get_symbol_snippet`. '
        'Precision: `ai_get_symbol_info(kind="signature"|'
        '"constructor"|"enum"|"api"|"properties")`. '
        'Broad: `ai_bundle(concept, mode="subsystem")`. '
        'DB: `schema_query(name, mode="entity")`.'
    ),
    "read_error": (
        '`ai_find(symbol, mode="symbols")` → '
        '`ai_find(symbol, mode="references")` → '
        "`ai_get_symbol_snippet`. "
        'DB: `schema_query(entity, mode="entity")`.'
    ),
    "investigate": (
        "`session_resume_bundle` (overview) → "
        "`action_surface_current_session_bundle` (common path) → "
        "`ai_investigate(concept)` for guided navigation. Or: "
        '`ai_bundle(concept, mode="subsystem")` → '
        '`ai_find(concept, mode="mutations"|"validation"|"policy")`.'
    ),
    "inspect": (
        "`session_resume_bundle` (overview) → "
        "`action_surface_current_session_bundle` (common path) → "
        '`ai_get_dependencies` / `ai_find(mode="references")` → '
        "`ai_get_modules`. Read only after narrowing."
    ),
}


def _get_action_directive(action_kind: str) -> str:
    return _ACTION_TOOL_DIRECTIVES.get(action_kind, "")


def _handle_oc_message_transform(payload: dict) -> dict:
    """OpenCode messages.transform — Phase-4 thin-adapter migration.

    Returns the parts the JS hook should APPEND to the last user
    message, and a flag indicating whether the entire last-message
    parts list should be REPLACED (the /aidocs command case).

      replace_parts        — when truthy, JS swaps last.parts entirely
                             with these. Empty otherwise.
      append_parts         — JS appends each entry to last.parts.

    Computed:
      - Plan-continuation injection: when the prompt is a short
        continuation phrase AND PLAN.md has unchecked steps, append
        the next-step nudge.
      - Action-directive injection: when classification matches a
        MESSAGE_DIRECTIVE_ACTIONS kind, append the directive.
      - /aidocs command: replace with buildAidocsExecutionPrompt
        (the actual prompt text is canonical; JS just renders).
    """
    project_root = Path(payload["project_root"])
    runtime = _runtime_for_cli(project_root)
    prompt_text = str(payload.get("prompt") or "")
    active_command = str(payload.get("active_command") or "")
    session_classification = str(payload.get("session_classification") or "")
    inject_directives = bool(payload.get("inject_directives", True))

    out: dict = {
        "replace_parts": [],
        "append_parts": [],
    }

    # /aidocs command — replace entirely.
    stripped = prompt_text.strip().lstrip("/").lower()
    if active_command == "aidocs" and stripped.startswith("aidocs"):
        # The JS still owns the literal prompt text for /aidocs
        # (buildAidocsExecutionPrompt assembles from .aidocs/
        # templates) — pure rendering. We just signal the replace.
        out["replace_parts"] = [{"type": "text", "marker": "aidocs_command"}]
        return out

    if not inject_directives:
        return out

    # Plan continuation: short user message + PLAN.md has unchecked steps.
    import re

    trimmed = re.sub(
        r"<tool-directive[^>]*>[\s\S]*?</tool-directive>",
        "",
        prompt_text,
    ).strip()
    is_continuation = len(trimmed) < 40 and re.match(
        r"^(ok|continue|next|go|yes|yep|yeah|sure|do it|"
        r"keep going|proceed|all of them|perfect|great|nice|good)",
        trimmed,
        re.IGNORECASE,
    )
    if is_continuation:
        try:
            host_state = runtime.host_state(project_root)
            sid = str(host_state.get("session_id") or "") if isinstance(host_state, dict) else ""
            managed = bool(host_state.get("managed")) if isinstance(host_state, dict) else False
            if managed and sid:
                plan_path = project_root / ".MEMORY" / "sessions" / sid / "plans" / "PLAN.md"
                if plan_path.is_file():
                    plan_text = plan_path.read_text(encoding="utf-8")
                    incomplete = [
                        re.sub(r"^\s*-\s*\[\s*\]\s*", "", ln).strip()
                        for ln in plan_text.splitlines()
                        if re.match(r"^\s*-\s*\[\s*\]", ln)
                    ]
                    incomplete = [s for s in incomplete if s]
                    if incomplete:
                        out["append_parts"].append(
                            {
                                "type": "text",
                                "text": (
                                    f"\n<plan-continuation>\n"
                                    f"Session plan has {len(incomplete)} "
                                    f"incomplete step(s). Next: "
                                    f"{incomplete[0]}\n"
                                    f"Continue implementing. Do not stop "
                                    f"to ask — the user confirmed.\n"
                                    f"</plan-continuation>"
                                ),
                            },
                        )
        except Exception:
            pass

    # Action directive injection.
    MESSAGE_DIRECTIVE_ACTIONS = {
        "edit",
        "write_memory",
        "task_begin",
        "task_update",
        "task_complete",
    }
    if session_classification and session_classification in MESSAGE_DIRECTIVE_ACTIONS:
        directive = _get_action_directive(session_classification)
        if directive:
            out["append_parts"].append(
                {
                    "type": "text",
                    "text": (
                        f'\n<tool-directive action="{session_classification}">\n'
                        f"{directive}\n</tool-directive>"
                    ),
                },
            )
    return out


# Typed event-kind constants. Host adapters MUST reference these
# rather than pass raw strings, so a typo can't silently downgrade
# a pretool gate to a continue-on-unknown response. The JS mirror
# is the EVENTS object in opencode_plugin.js.
EVENT_PRETOOL: str = "pretool"
EVENT_POSTTOOL: str = "posttool"
EVENT_PROMPT_MUTATE: str = "prompt_mutate"
EVENT_SESSION_START: str = "session_start"
EVENT_COMPACT: str = "compact"
EVENT_OC_CHAT_MESSAGE: str = "oc_chat_message"
EVENT_OC_MESSAGE_TRANSFORM: str = "oc_message_transform"

HANDLERS: dict = {
    EVENT_PRETOOL: _handle_pretool,
    EVENT_POSTTOOL: _handle_posttool,
    EVENT_PROMPT_MUTATE: _handle_prompt_mutate,
    EVENT_SESSION_START: _handle_session_start,
    EVENT_COMPACT: _handle_compact,
    EVENT_OC_CHAT_MESSAGE: _handle_oc_chat_message,
    EVENT_OC_MESSAGE_TRANSFORM: _handle_oc_message_transform,
}

# Security-relevant events fail closed: any error path returns
# verdict="deny". Other events fail open with verdict="continue".
# This mirrors the doctrine in /goal §"fail closed where security
# decisions are undecided" and the AIDOCS edit-gate convention
# (gate lookup error → refuse closed).
FAIL_CLOSED_EVENTS: frozenset[str] = frozenset({EVENT_PRETOOL})


def _failure_envelope(event_kind: str, reason: str, **extra: object) -> dict:
    """Build the right failure envelope for the event kind."""
    base: dict = {"reason": reason, "error": reason}
    base.update(extra)
    if event_kind in FAIL_CLOSED_EVENTS:
        base["verdict"] = "deny"
    else:
        base["verdict"] = "continue"
    return base


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not argv:
        # No event kind → unknown context; the safest default is to
        # NOT grant security clearance. continue is fine here because
        # there's no specific tool call to deny.
        print(json.dumps({"error": "no event_kind", "verdict": "continue"}))
        return 1
    event_kind = argv[0]
    if event_kind not in HANDLERS:
        print(
            json.dumps(
                _failure_envelope(
                    event_kind,
                    f"unknown event_kind {event_kind!r}",
                ),
            ),
        )
        return 1
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        print(
            json.dumps(
                _failure_envelope(
                    event_kind,
                    f"invalid stdin JSON: {exc}",
                ),
            ),
        )
        return 1

    try:
        result = HANDLERS[event_kind](payload)
    except Exception as exc:
        print(
            json.dumps(
                _failure_envelope(
                    event_kind,
                    f"{type(exc).__name__}: {exc}",
                    trace=traceback.format_exc(limit=3),
                ),
            ),
        )
        return 1

    try:
        print(json.dumps(result, default=str))
    except Exception as exc:
        print(
            json.dumps(
                _failure_envelope(
                    event_kind,
                    f"result serialization failed: {exc}",
                ),
            ),
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
