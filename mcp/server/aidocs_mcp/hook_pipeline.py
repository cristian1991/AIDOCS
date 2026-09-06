"""hook_pipeline — the host-AGNOSTIC hook core.

Host adapters (`claude_hook`, and future `codex_hook` / OpenCode bridge) translate
their host's hook JSON into these calls and render host-specific response
envelopes. The CORE never knows host envelope shapes; it decides behavior purely
by ``host_kind`` (via ``host_capabilities``) + the normalized inputs. This is
where logic that was fused into ``claude_hook`` is being extracted, slice by
slice, so adapters stay thin and no host re-implements another's law.

Doctrine: an adapter passes ITS OWN host_kind (claude_hook -> "claude_code";
a codex_hook -> "codex") and RENDERS the returned decision into its envelope
(Claude `updatedToolOutput`; Codex `decision:block` feedback; ...). The core is
the single place the host-capability gate lives -- so a new host gets correct
behavior by calling the core, not by threading a host_kind variable through a
3,700-line adapter.

Extracted slices:
  1 (2026-06-14): OUTPUT-REDACTION decision -- the capability gate + secret scan
     that decides whether a tool result must be redacted before it reaches model
     context. Host-agnostic: depends only on host_kind (can this host
     shape-preserving-redact?) + the tool result. (The Codex bug lived here:
     claude_hook hard-gated on "claude_code", so a Codex session would falsely
     attempt Claude's updatedToolOutput. In the core, the gate is parametric.)

  Remaining slices (tracked): prompt pipeline (UPS -> PromptMutator), pre-tool
  enforcement (-> ToolGate), stop-gate + freeze stewardship, audit attribution.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .managed_mode_service import resolve_managed_session

logger = logging.getLogger("aidocs.hook_pipeline")

# Tools whose RESULT is model-visible and worth a pre-context secret scan.
REDACTABLE_OUTPUT_TOOLS: frozenset[str] = frozenset({"read", "bash", "monitor"})


@dataclass(slots=True)
class OutputRedactionDecision:
    """A host-agnostic redaction verdict. The adapter renders this into its own
    envelope (Claude updatedToolOutput / Codex feedback) and emits the audit."""

    redacted: object  # the shape-preserving redacted tool_response
    count: int  # number of secrets redacted
    categories: list[str]  # secret categories found (audit)
    mechanism: str  # the host's redaction mechanism string (audit truth)
    # #401 gap 3: True when the scan could NOT certify the output and the
    # payload was WITHHELD rather than redacted (unknown != clean). The
    # adapter renders the same envelope; only the audit reads differently.
    withheld: bool = False


def normalize_tool_name(name: object) -> str:
    """Strip MCP prefixes + lowercase, so 'mcp__aidocs__bash' -> 'bash'."""
    n = str(name or "").strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if n.startswith(prefix):
            return n[len(prefix) :]
    return n


def is_redactable_tool(tool_name: object) -> bool:
    return normalize_tool_name(tool_name) in REDACTABLE_OUTPUT_TOOLS


def host_can_redact_output(host_kind: str | None) -> bool:
    """The capability gate, parametric by host. Only a host that can replace a
    tool result with a SHAPE-PRESERVING redacted copy before context (e.g. Claude
    updatedToolOutput) returns True. Codex/OpenCode/etc. -> False (fail closed)."""
    from .host_capabilities import can_redact_tool_output_before_context

    return can_redact_tool_output_before_context(host_kind)


def decide_generic_output_redaction(
    host_kind: str | None,
    tool_name: object,
    tool_response: object,
    *,
    project_root: Path | None = None,
) -> OutputRedactionDecision | None:
    """Host-agnostic generic (bash/monitor) output-redaction decision.

    Returns a decision iff: the tool is redactable, the HOST can shape-preserving
    redact, the result exists, and the secret scan finds something. None
    otherwise. The adapter renders the envelope + emits audit. Never raises.

    FAIL CLOSED (#401 gap 3): the scan RAISING is not "nothing to redact".
    ai_run's own command output guard withholds uncertifiable output
    (``run_output_guard``, ``security.require_output_redaction_for_run``);
    since ai_run went GATE_ONLY the host Bash tool is a local agent's only
    shell, so the same floor applies here — an uncertifiable scan returns a
    SHAPE-PRESERVING withheld decision (``withheld=True``) instead of letting
    raw output reach model context. The operator opt-out and the
    non-redacting policies (report_only / allow_raw, which never claimed
    redaction) are honored identically to ai_run.
    """
    try:
        if not is_redactable_tool(tool_name):
            return None
        if tool_response is None:
            return None
        if not host_can_redact_output(host_kind):
            return None
        from .host_capabilities import redaction_mechanism
        from .output_guard import redact_tool_response

        try:
            redacted, count, categories = redact_tool_response(tool_response, redact=True)
        except Exception:
            return _withheld_output_decision(host_kind, tool_response, project_root)
        if not count:
            return None
        return OutputRedactionDecision(
            redacted=redacted,
            count=int(count),
            categories=list(categories or []),
            mechanism=redaction_mechanism(host_kind) or "",
        )
    except Exception:
        return None


def _fail_closed_output_posture(project_root: Path | None) -> bool:
    """True when an uncertifiable scan must WITHHOLD rather than pass through.

    Reads the SAME two knobs ai_run's guard reads, with the same defaults, so
    neither shell surface sees a weaker law. Config trouble defaults to the
    protective answer."""
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
    if policy != "redact":
        # report_only / allow_raw never claimed redaction, so they never
        # claim a withhold either.
        return False
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "security.require_output_redaction_for_run",
                project_root=project_root,
                default=True,
            ),
        )
    except Exception:
        return True


def _withheld_output_decision(
    host_kind: str | None,
    tool_response: object,
    project_root: Path | None,
) -> OutputRedactionDecision | None:
    """Build the shape-preserving withhold for an uncertifiable scan, or None
    when the operator posture keeps the historical degraded pass-through."""
    if not _fail_closed_output_posture(project_root):
        return None
    notice = (
        "[AIDOCS: command output guard could not certify this output; "
        "output withheld — unknown != clean "
        "(security.require_output_redaction_for_run=true)]"
    )
    try:
        from .tool_gate_service import false_positive_affordance

        notice += "\n" + false_positive_affordance(
            "hook_pipeline.generic_output_withheld",
            project_root=project_root,
        )
    except Exception:
        pass
    try:
        from .shell_receipt import withheld_replacement

        replaced = withheld_replacement(tool_response, notice)
    except Exception:
        # Even the withhold builder is unavailable — still never deliver the
        # unscanned bytes.
        replaced = notice
    try:
        from .host_capabilities import redaction_mechanism

        mechanism = redaction_mechanism(host_kind) or ""
    except Exception:
        mechanism = ""
    return OutputRedactionDecision(
        redacted=replaced,
        count=0,
        categories=[],
        mechanism=mechanism,
        withheld=True,
    )


# ══════════════════════════════════════════════════════════════════════════
# Slice 2 (2026-07-06, hook-core rip S2): host-agnostic decision cores moved
# VERBATIM from claude_hook.ClaudeHookHandler. The hook keeps thin delegate
# methods with identical signatures; envelope RENDERING (hookSpecificOutput)
# stays in the adapter — these cores return plain data.
# ══════════════════════════════════════════════════════════════════════════

# Tools that may run while requires_reconnect=1. Everything else
# must wait for session_connect to clear the flag. Keeps the
# allowlist tight — any read-only tool added here must make
# sense BEFORE a session has re-bound.
RECONNECT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        # Bind / bootstrap — the tools that clear the flag.
        "ai_session",
        "session_connect",
        # session_start: compat alias for CC's hardcoded probe
        # (restored 2026-05-03 as auto-activator).
        "session_start",
        "session_list",
        "aidocs_orchestrate",
        "project_bootstrap_or_resume",
        "project_status",
        "project_check",
        # Admin escape hatch (2026-04-22) — named tool that clears
        # both reconnect flags in one idempotent call. Exists so
        # operators never have to do manual sqlite surgery to
        # escape a future AIDOCS-internal deadlock. FastMCP
        # registers the function as `admin_clear_reconnect` (strips
        # leading `aidocs_`); allowlist uses that bare name.
        "admin_clear_reconnect",
        # Schema discovery — agent must be able to LOAD the schema for
        # session_connect before calling it. Without ToolSearch
        # here, we recreate a catch-22 (user reported 2026-04-21).
        "ToolSearch",
        # Minimum discovery surface so the agent can figure out WHAT
        # to reconnect to without being fully blind. These are
        # read-only and don't depend on sqlite continuity.
        "ai_find",
        "ai_investigate",
        "ai_get_lines",
        "ai_bundle",
        # Heartbeat + task lifecycle.
        "ScheduleWakeup",
        "task_complete",
    },
)

# Override phrases that lift the agent-brief research-block for the
# current operator turn. Closed list — not heuristic. Operator has
# to type the phrase exactly to opt into delegated research.
AGENT_RESEARCH_OVERRIDE_PHRASES: tuple[str, ...] = (
    "delegate research",
    "let agents research",
    "let the agent research",
    "agent can research",
    "agents may research",
    "ok to research",
)


def decide_reconnect(
    runtime,
    project_root: Path,
    tool_name: str,
    *,
    cli_session_id: str = "",
) -> dict[str, object] | None:
    """Fresh-CLI reconnect gate (2026-04-21).

    When Claude Code's per-process session_id changed mid-session
    (window reopen), requires_reconnect=1 is sticky until the agent
    calls session_connect or an equivalent session-bind tool.
    While the flag is raised, every tool outside
    RECONNECT_ALLOWED_TOOLS is hard-refused. Keeps agents from
    acting on inherited sqlite state (known paths, lane binding)
    with an empty in-memory context.

    cli_session_id (#58, canonical 2026-04-26): when provided,
    managed_mode resolution is per-conductor — the deny envelope
    returns the calling conductor's bound session, not whichever
    session another conductor most recently set on the singleton.

    Returns None to allow, or ``{reason, blocked_by, session_id}`` for
    the adapter to render as its deny envelope. The flag-clearing side
    effect (clear_requires_reconnect on ai_session/session_connect)
    lives HERE in the core.
    """
    try:
        session_id = resolve_managed_session(
            runtime.hub.managed_mode,
            project_root,
            host_session_id=cli_session_id,
        )
    except Exception:
        return None
    if not session_id:
        return None
    # Single reconnect trigger: query_gate.requires_reconnect.
    # Set by UserPromptSubmit's check_and_update_cli_session_id
    # when Claude Code's per-process session_id changes (fresh CLI
    # launch that inherited sqlite state with empty in-memory
    # context). The old boot-token path was removed 2026-04-23:
    # claude_hook runs as a separate subprocess per tool call so
    # its module-level token never matched the long-running MCP
    # server's stamp, causing an infinite PreToolUse lockout.
    try:
        needs = runtime.hub.query_gate.get_requires_reconnect(
            project_root,
            session_id,
        )
    except Exception:
        return None
    if not needs:
        return None

    # Normalize tool name (Claude Code prefixes mcp__aidocs__ tools).
    bare = tool_name.strip()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break
    # Tool names Claude Code registers for host tools keep case
    # ("Read", "Task", "ScheduleWakeup") — match case-insensitively
    # against the allowlist to cover both.
    if bare in RECONNECT_ALLOWED_TOOLS or bare.lower() in {
        t.lower() for t in RECONNECT_ALLOWED_TOOLS
    }:
        # Clear the flag when session_connect runs — that's
        # the contract the agent is re-binding via.
        # (session_start MCP tool removed 2026-04-30; only
        # session_connect remains as the bind path.)
        if bare.lower() in {"ai_session", "session_connect"}:
            try:
                runtime.hub.query_gate.clear_requires_reconnect(
                    project_root,
                    session_id,
                )
            except Exception:
                pass
        return None

    # Server resolves host_session_id from the gate row stamped
    # by the UPS hook; the agent only passes the human-readable
    # session name. (Fixed 2026-05-13 — was demanding an arg the
    # tool schema doesn't accept, deadlocking fresh CLIs.)
    return {
        "reason": (
            "Fresh CLI — call `mcp__aidocs__ai_session(mode='connect', "
            f'session_id="{session_id}")`. '
            "Known-path reads wiped; re-discover via ai_find / "
            "ai_investigate."
        ),
        "blocked_by": "requires_reconnect",
        "session_id": session_id,
    }


def decide_session_freeze(
    runtime,
    project_root: Path,
    *,
    tool_name: str = "",
    tool_input: object = None,
) -> dict[str, object] | None:
    """Session-freeze pre-tool guard (#39, 2026-04-25).

    When a confirmable destructive verdict landed on a previous
    tool call, the session is frozen until the next UPS resolves
    the freeze (self_approve) or the admin decides
    (admin_escalation, Phase B). While frozen, every tool returns
    the same deny envelope with the fingerprint phrase the
    operator must type.

    Single-row-per-session contract. Failure on store read =
    return None (let other gates run); never inject a stale
    freeze if the row can't be read.

    Returns None or ``{permissionDecisionReason, blocked_by,
    freeze_state}`` for the adapter to render as its deny envelope.

    #588 D4: the reachability/jurisdiction exemption is evaluated FIRST
    and shares one implementation with ToolGate.session_freeze_pretool,
    so the two copies of this guard cannot drift on which surfaces stay
    reachable inside a freeze.
    """
    from .tool_gate_service import freeze_gate_exemption

    if freeze_gate_exemption(
        tool_name=tool_name,
        tool_input=tool_input,
        project_root=project_root,
    ):
        return None
    try:
        session_id = resolve_managed_session(runtime.hub.managed_mode, project_root)
    except Exception:
        return None
    if not session_id:
        return None
    from .freeze_service import (
        build_existing_freeze_response,
        get_existing_freeze,
    )

    freeze = get_existing_freeze(project_root, session_id)
    if freeze is None:
        return None
    env = build_existing_freeze_response(freeze, project_root)
    return {
        "permissionDecisionReason": env["permissionDecisionReason"],
        "blocked_by": env.get("blocked_by", "session_frozen"),
        "freeze_state": env["freeze_state"],
    }


def current_lane_id(runtime, project_root: Path) -> str | None:
    """Get the current lane ID from gate state."""
    try:
        managed = runtime.hub.managed_mode.get_mode(project_root)
        sid = managed.get("session_id") if isinstance(managed, dict) else None
        if not sid:
            return None
        state = runtime.hub.query_gate.get(project_root, str(sid))
        return state.get("current_lane_id")
    except Exception:
        return None


def record_hook_event(
    runtime,
    project_root: Path,
    event_name: str,
    payload: dict[str, object],
    *,
    source: str = "claude_hook",
) -> None:
    try:
        managed = runtime.hub.managed_mode.get_mode(project_root)
        session_id = str(managed.get("session_id") or "").strip() or None
        tool_name = str(payload.get("tool_name") or "").strip() or None
        prompt = str(payload.get("prompt") or "").strip() or None
        event_kind = event_name.lower()
        payload_summary = {
            key: value
            for key, value in payload.items()
            if key in {"hook_event_name", "tool_name", "tool_input", "prompt", "cwd"}
        }
        # Token estimation from hook payloads
        tokens_in = 0
        tokens_out = 0
        if prompt:
            tokens_in += max(1, len(prompt.encode("utf-8")) // 4)
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, dict):
            try:
                tokens_out += max(
                    1,
                    len(json.dumps(tool_input, default=str).encode("utf-8")) // 4,
                )
            except Exception:
                pass
        elif isinstance(tool_input, str):
            tokens_out += max(1, len(tool_input.encode("utf-8")) // 4)
        from .tool_call_log import record as _log_record

        _log_record(
            runtime.hub,
            project_root,
            phase=event_kind,
            name=tool_name,
            payload={
                **payload_summary,
                "prompt_preview": prompt[:200] if prompt else None,
                "tokens_in_estimate": tokens_in,
                "tokens_out_estimate": tokens_out,
            },
            session_id=session_id,
            source=source,
            action_kind="hook_intercept",
            status="observed",
        )
    except Exception as exc:
        logger.debug("Failed to record hook event: %s", exc)
        return


def record_classification_event(
    runtime,
    project_root: Path,
    action_kind: str,
    prompt: str,
    *,
    source: str = "claude_hook",
) -> None:
    """Record the classified action_kind as an execution event for traceability."""
    try:
        managed = runtime.hub.managed_mode.get_mode(project_root)
        session_id = str(managed.get("session_id") or "").strip() or None
        from .tool_call_log import record as _log_record

        _log_record(
            runtime.hub,
            project_root,
            phase="prompt_classified",
            name=None,
            payload={"prompt_preview": prompt[:200] if prompt else None},
            session_id=session_id,
            source=source,
            action_kind=action_kind,
            status="classified",
        )
    except Exception as exc:
        logger.debug("Failed to record classification event: %s", exc)


def resolve_cwd_root(payload: dict[str, object]) -> Path | None:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    return Path(cwd).resolve()


def resolve_project_root(
    runtime,
    payload: dict[str, object],
    log_failure: Callable[[Path, str], None],
) -> Path | None:
    """Resolve project root from the hook payload's cwd.

    A project resolves when it is COMMISSIONED per
    ``project_commission`` — i.e. the install-wide registry/SQLite
    records it (authority), OR the on-disk
    ``.MEMORY/.aidocs/index.aidocs`` marker exists (back-compat
    fallback). Governance no longer hinges on the marker file alone:
    a project commissioned via the registry resolves even if the
    marker was never written / was deleted, and a legacy marker-only
    project still resolves.

    Adopted-but-uncommissioned projects (declare the aidocs MCP /
    carry an adoption record but have no infrastructure yet) do NOT
    resolve here — they are first commissioned by the UPS /
    SessionStart auto-repair in ``_dispatch_event``, after which this
    check succeeds.

    No walk-up — the hook's cwd is the user's terminal cwd, which IS
    the kingdom root. Walking up would incorrectly adopt a parent
    project from a non-project subdir; and the old loose check
    (`.MEMORY/` + AGENTS.md/CLAUDE.md) wrongly accepted subdirs like
    `mcp/`/`core/` that ship their own guidance files. (2026-05-03 /
    commission-state fix.)
    """
    cwd_root = resolve_cwd_root(payload)
    if cwd_root is None:
        return None
    try:
        from .project_commission import is_commissioned

        commissioned = is_commissioned(cwd_root)
    except Exception:
        # Fail back to the marker so a commission-store hiccup never
        # silently un-manages a project that has the on-disk marker.
        from .mcp_server_runtime_helpers import is_aidocs_managed

        commissioned = is_aidocs_managed(cwd_root)
    if not commissioned:
        # T0 (2026-07-12): cwd is not ITSELF commissioned, but it may be a
        # SUBDIR of a commissioned project (running from `mcp/` or `core/`
        # inside the repo root). Walk UP to the nearest commissioned ancestor
        # so the hook GATES raw tools from a subdir instead of failing OPEN —
        # returning None makes _dispatch_event emit no PreToolUse decision, so
        # Claude Code permits the raw Edit/Write/Read, bypassing the ENTIRE
        # edit-gate (read-evidence, DNT cite, turn-locks, indexed-read). The
        # old no-walk-up rule existed to stop the loose '.MEMORY + guidance
        # file' test from adopting a subdir; that reason is gone — is_commissioned
        # is now the precise registry/commission-stamp check, so binding to a
        # commissioned ANCESTOR is a deliberate operator state, never an
        # accidental adopt. Bounded: stop at the FIRST commissioned ancestor,
        # never walk above the user's home dir.
        _anc_commissioned = None
        try:
            _home = Path.home().resolve()
        except Exception:
            _home = None
        for _ancestor in cwd_root.parents:
            try:
                if is_commissioned(_ancestor):
                    _anc_commissioned = _ancestor
                    break
            except Exception:
                pass
            if _home is not None and _ancestor == _home:
                break
        if _anc_commissioned is not None:
            return _anc_commissioned
        log_failure(
            cwd_root,
            "project not commissioned (no registry record or "
            ".MEMORY/.aidocs/index.aidocs marker at cwd or any "
            "commissioned ancestor)",
        )
        return None
    return cwd_root


def operator_intent_note(outcome) -> str:
    """Render a one-line operator-facing acknowledgment for an
    operator-intent outcome. Never echoes the prompt or any secret —
    only the structured action/target/scope and the decision.
    """
    status = getattr(outcome, "status", "")
    target = getattr(outcome, "target", "")
    scope = getattr(outcome, "scope", "")
    action = getattr(outcome, "action", "")
    if status == "reported":
        value = getattr(outcome, "read_value", None)
        # Bash-allowlist report: distinguish session entries from
        # inherited ones, concisely.
        if target == "bash.allowlist" and isinstance(value, dict):
            session_cmds = value.get("session") or []
            inherited = value.get("inherited") or []
            head = ", ".join(session_cmds) if session_cmds else "(none)"
            note = f"📊 Bash allowlist for this session: {head}"
            if inherited:
                shown = ", ".join(inherited[:6])
                extra = len(inherited) - 6
                if extra > 0:
                    shown += f", +{extra} more"
                note += f" (plus inherited: {shown})"
            return note + "."
        provenance = getattr(outcome, "provenance", "") or "default"
        state = "ON" if value else "OFF"
        return f"📊 {target} is {state} (scope: {scope}, value from: {provenance})."
    if status == "noop":
        # Non-mutating: a remove of an entry not present at session
        # scope. Name the command so the operator knows nothing
        # changed (and why).
        if target == "bash.allowlist":
            value = getattr(outcome, "value", None) or {}
            cmd = value.get("command", "?") if isinstance(value, dict) else "?"
            return (
                f"ℹ️ '{cmd}' was not present in the session bash allowlist. No change was made."
            )
        return (
            f"ℹ️ Operator intent '{action} {target}' ({scope}): nothing "
            f"to change. No change was made."
        )
    if status == "applied":
        # Bash-allowlist route reports the exact base command.
        if target == "bash.allowlist":
            value = getattr(outcome, "value", None) or {}
            cmd = value.get("command", "?") if isinstance(value, dict) else "?"
            if action == "remove":
                return (
                    f"✅ Removed '{cmd}' from the session bash allowlist "
                    f"(scope: {scope}). Authenticated via host binding."
                )
            return (
                f"✅ Added base command '{cmd}' to the bash allowlist "
                f"(scope: {scope}). Authenticated via host binding."
            )
        return (
            f"✅ Operator intent applied: {action} {target} "
            f"(scope: {scope}). Authenticated via host binding."
        )
    if status == "needs_exact_confirm":
        if target == "bash.allowlist":
            value = getattr(outcome, "value", None) or {}
            cmd = value.get("command", "?") if isinstance(value, dict) else "?"
            if action == "remove":
                phrase = f"remove {cmd} from bash allowlist for this session"
                verb = f"remove '{cmd}' from"
            else:
                phrase = f"add {cmd} to bash allowlist for this session"
                verb = f"add '{cmd}' to"
            return (
                f"⚠️ To {verb} the bash allowlist ({scope}), type the "
                f'exact phrase: "{phrase}". No change was made.'
            )
        return (
            f"⚠️ Operator intent '{action} {target}' ({scope}) is a "
            f"dangerous route — type the exact confirmation phrase to "
            f"proceed. No change was made."
        )
    if status == "needs_confirmation":
        return (
            f"❓ Operator intent '{action} {target}' read with low "
            f"confidence — rephrase to confirm. No change was made."
        )
    if status == "refused":
        reason = getattr(outcome, "reason", "")
        return f"⛔ Operator intent '{action} {target}' refused ({reason}). No change was made."
    return ""


def on_post_compact(
    runtime,
    project_root: Path,
    payload: dict[str, object] | None = None,
    *,
    host_kind: str = "claude_code",
) -> dict[str, object] | None:
    """Reset token counters + rotate agent_memory_epoch after compaction.

    2026-05-03 fix (king directive): also rotates the agent_memory_epoch
    via bump_compaction_count so once-per-epoch memory (helper skills,
    DNT banners, etc.) re-injects on the next prompt. Prior version
    had a comment claiming it did this but never called the function.
    Truth in comments now matches truth in code.

    The session bind itself (managed_mode active + bound_by_boot_token)
    is already preserved across compaction — same MCP process, same
    host session UUID, no requires_reconnect trigger. The agent doesn't
    need a "you're bound" reminder; it just needs the epoch rotation
    to receive fresh memory cues.
    """
    # PostCompact side effects — delegated to LifecycleService
    # (host-agnostic). The same handler powers OpenCode's
    # experimental.session.compacting hook (via python subprocess)
    # and any future Codex adapter.
    from .lifecycle_service import LifecycleService

    host_session_id = ""
    if payload:
        host_session_id = str(payload.get("session_id") or "").strip()
    _lifecycle = LifecycleService(runtime).on_post_compact(
        host_kind=host_kind,
        host_session_id=host_session_id,
        project_root=project_root,
    )
    # #622/#954 — WRITE THE AUDIT ROWS. This result used to be assigned and
    # never read: side_effects, why AND audit_events were all discarded, so the
    # epoch bump's `compaction_epoch_bumped` row never reached the ledger and
    # neither did the failure rows added for #622. The telemetry existed and
    # had no consumer (law 183074ae).
    #
    # THE OLD COMMENT'S REASONING WAS SOUND AND ABOUT SOMETHING ELSE: "CC's
    # hook schema rejects hookSpecificOutput with hookEventName='PostCompact',
    # and the agent doesn't need to know about internal bookkeeping." Both true
    # — and neither is a reason to drop the LEDGER row. Not telling the AGENT
    # is not the same as not recording. The host contract below is unchanged.
    #
    # BEST-EFFORT, matching the emitter it drains: a ledger that refuses must
    # not turn compaction bookkeeping into a dead turn. Each row is written
    # independently so one bad payload cannot swallow the rest.
    for _kind, _payload in getattr(_lifecycle, "audit_events", ()) or ():
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                _kind,
                "lifecycle",
                status="observed",
                payload=dict(_payload or {}),
            )
        except Exception:  # noqa: BLE001 — never let bookkeeping kill the hook
            pass
    # CC's hook schema rejects hookSpecificOutput with
    # hookEventName='PostCompact', so the host response stays empty.
    return {}


# ══════════════════════════════════════════════════════════════════════════
# Slice 3 (2026-07-06, hook-core rip S3): PreToolUse decision pipeline moved
# from claude_hook._handle_pre_tool_use. The core runs the EXACT check order
# of the old adapter method (native-shell 2.0-A enforcement → lane-worker
# stamp/auto-bind → ToolGate.evaluate_tool [which internally composes
# kill-switch, reconnect, session-freeze, orchestrator, sticky-grant,
# conductor-comms gates] → ShellPolicy shadow) and returns plain data; the
# adapter renders hookSpecificOutput envelopes from the verdict.
#
# Payload-shape note: keys like ``tool_name`` / ``tool_input`` / ``session_id``
# / ``tool_use_id`` are the CANONICAL normalized hook-payload contract shared
# by every adapter (Claude Code emits them natively; other hosts normalize
# into this shape before calling the core).
# ══════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ToolUseDecision:
    """Host-agnostic PreToolUse verdict. The adapter renders this into its
    own envelope shapes (Claude hookSpecificOutput; Codex feedback; ...)."""

    verdict: str  # "allow" | "deny" | "ask" | "freeze"
    reason: str = ""
    blocked_by: str = ""
    # Extras: "host_envelope" (pre-rendered passthrough from a service, e.g.
    # shell_adapter 2.0-A), "ask_kind", "freeze_state",
    # "additional_context_blocks" (tuple->list of context lines on allow).
    meta: dict = field(default_factory=dict)


def decide_tool_use(
    runtime,
    project_root: Path,
    payload: dict[str, object],
    *,
    host_kind: str = "claude_code",
    cli_session_id: str = "",
    audit_source: str = "claude_hook",
) -> ToolUseDecision:
    """PreToolUse decision pipeline (moved VERBATIM from
    claude_hook._handle_pre_tool_use, S3 rip).

    Calls the canonical pretool pipeline once with DATA-producing
    GateHooks (the old CC hooks rendered hookSpecificOutput inline; the
    same blocked_by-extraction logic now yields plain dicts). No side
    effect moved or reordered: lane-worker stamp/auto-bind, kill-switch
    bypass audit, and the ShellPolicy shadow all run exactly where they
    did in the adapter.
    """
    from .mcp_server_runtime_helpers import is_aidocs_managed as _is_aidocs_project
    from .tool_gate_service import GateHooks, ToolGate

    tool_name = str(payload.get("tool_name") or "").strip()
    tool_input = (
        payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    )
    # #58 conductor identity: Claude Code stamps its per-process
    # UUID as payload.session_id on every hook fire.
    cli_session_id = cli_session_id or str(payload.get("session_id") or "").strip()

    # Lane-worker plumbing — env-keyed side effects, not gate
    # decisions. Run around the evaluate_tool call.
    import os as _os_lw

    _worker_id = _os_lw.environ.get("AIDOCS_EXPERT_ID", "").strip()
    _worker_session_id = _os_lw.environ.get(
        "AIDOCS_EXPERT_SESSION_ID",
        "",
    ).strip()
    _worker_lane_id = _os_lw.environ.get(
        "AIDOCS_EXPERT_LANE_ID",
        "",
    ).strip()

    gate = ToolGate(runtime)

    # Batch 2.0-A: when shell-enforcement is live, host-native shell
    # tools (Bash/PowerShell/cmd) are owned by ShellPolicy/
    # ShellEnforcement — structural gates first, then ShellEnforcement
    # as the single authority + single freeze minter. NO native process
    # runs (execute_native/allow → native-deny + ai_run). Returns the
    # envelope directly, bypassing the normal orchestrator slice (so no
    # double freeze mint and no kill-switch native-allow). Off by
    # default; non-native tools and flag-off are unaffected.
    #
    # FAIL CLOSED: for a flag-on host-native shell tool, ANY error
    # (including the adapter import itself) must DENY — never fall
    # through to the normal pipeline, which could let the host run the
    # process. Native detection uses shell_envelope directly so it
    # holds even if shell_adapter cannot be imported. (Monitor is a
    # read/status surface and is intentionally NOT enforced in 2.0-A.)
    if _native_shell_enforcement_active(tool_name, project_root):
        return _native_shell_enforced_decision(
            runtime=runtime,
            project_root=project_root,
            host_kind=host_kind,
            tool_name=tool_name,
            tool_input=tool_input,
            cli_session_id=cli_session_id,
            payload=payload,
        )

    # Phoenix §VIII deny-path stamp BEFORE the pipeline runs.
    gate.stamp_lane_worker_host_session_id(
        host_session_id=cli_session_id or "",
        worker_id=_worker_id,
        worker_lane_id=_worker_lane_id,
        project_root=project_root,
    )
    # Lane-worker auto-bind BEFORE evaluate_tool so a worker's
    # first non-bootstrap call passes managed_mode_required.
    # Idempotent on already-active managed_mode.
    gate.auto_bind_lane_worker_managed_mode(
        worker_id=_worker_id,
        worker_session_id=_worker_session_id,
        worker_lane_id=_worker_lane_id,
        project_root=project_root,
    )
    try:
        _lane_id_for_audit = current_lane_id(runtime, project_root)
    except Exception:
        _lane_id_for_audit = None

    # DATA-producing gate hooks. Each returns the plain decision dict
    # the old CC hooks used to render as hookSpecificOutput inline.
    #
    # Note: no on_context_block hook — evaluate_tool's
    # conductor_comms gate already produces ">>> CONDUCTOR MESSAGE:"
    # blocks in the right order, and x-ray goggles append the
    # 🧠 lines after. The result.additional_context_blocks tuple
    # is already in the order the adapter wants (comms first, x-ray after).
    core_hooks = GateHooks(
        on_allow=_core_on_allow,
        on_deny=_core_on_deny,
        on_ask=_core_on_ask,
        on_freeze=_core_on_freeze,
    )

    # ── Canonical pipeline (one call, all sub-gates composed) ──
    result = gate.evaluate_tool(
        tool_name=tool_name,
        tool_input=tool_input,
        host_session_id=cli_session_id or "",
        project_root=project_root,
        host_kind=host_kind,
        payload=payload,
        lane_id=_lane_id_for_audit,
        is_aidocs_project=_is_aidocs_project(project_root),
        hooks=core_hooks,
    )

    # ── ShellPolicy shadow (Batch 1.5, observe-only) ──
    _run_shell_policy_shadow(
        project_root=project_root,
        host_kind=host_kind,
        tool_name=tool_name,
        tool_input=tool_input,
        cli_session_id=cli_session_id,
        result=result,
    )

    # ── Map verdict into plain decision data (adapter renders) ──
    return _decision_from_gate_result(result)


def _native_shell_enforcement_active(tool_name: str, project_root: Path) -> bool:
    """Resolve whether Batch 2.0-A native-shell enforcement owns this tool.

    FAIL CLOSED: if detection/config lookup is indeterminate AND the tool
    NAME literally looks like a host-native shell, enforcement is on — a
    Bash/PowerShell/cmd call must never slip through on an indeterminate
    enforcement state. (Extracted verbatim from decide_tool_use, #413.)
    """
    _native_shell_enforced = False
    _native_detection_failed = False
    try:
        from .shell_envelope import (
            TRANSPORT_HOST_NATIVE,
            detect_provider_and_transport,
        )

        _, _transport = detect_provider_and_transport(tool_name)
        if _transport == TRANSPORT_HOST_NATIVE:
            from .config import get_setting

            _native_shell_enforced = bool(
                get_setting(
                    "tools.shell_enforcement_live",
                    project_root=project_root,
                    default=False,
                ),
            )
    except Exception:
        # Detection / config lookup itself threw — we cannot prove the
        # tool is non-native or that enforcement is off.
        _native_detection_failed = True

    # Last-resort literal fallback: if detection/config was
    # indeterminate AND the tool NAME literally looks like a host-native
    # shell, fail closed. Monitor is NOT in this set — it is a
    # read/status surface, excluded from 2.0-A.
    if _native_detection_failed:
        _bn = (tool_name or "").strip().lower()
        for _pre in ("mcp__aidocs__", "mcp__"):
            _bn = _bn.removeprefix(_pre)
        _bn = _bn.removesuffix(".exe")
        if _bn in (
            "bash",
            "sh",
            "zsh",
            "wsl",
            "powershell",
            "pwsh",
            "cmd",
        ):
            _native_shell_enforced = True
    return _native_shell_enforced


def _native_shell_enforced_decision(
    *,
    runtime,
    project_root: Path,
    host_kind: str,
    tool_name: str,
    tool_input: object,
    cli_session_id: str,
    payload: dict[str, object],
) -> ToolUseDecision:
    """Flag-on host-native shell call → ShellEnforcement decision.

    A flag-on native-shell call MUST be handled by the adapter. If it
    returned an envelope, use it; if it raised or returned None
    unexpectedly, fail closed — do not fall through. (Extracted
    verbatim from decide_tool_use, #413.)
    """
    try:
        from .shell_adapter import native_shell_2a_pretool

        _na_env = native_shell_2a_pretool(
            runtime=runtime,
            hub=runtime.hub,
            project_root=project_root,
            host=host_kind,
            tool_name=tool_name,
            tool_input=(tool_input if isinstance(tool_input, dict) else {}),
            host_session_id=cli_session_id or "",
            tool_use_id=str(payload.get("tool_use_id") or ""),
        )
    except Exception:
        _na_env = None
    if _na_env is not None:
        # Pre-rendered by shell_adapter for this host — passthrough.
        return ToolUseDecision(
            verdict="deny",
            meta={"host_envelope": _na_env},
        )
    return ToolUseDecision(
        verdict="deny",
        reason=(
            "native shell enforcement failed; action remains "
            "blocked. Use ai_run(command=...) instead."
        ),
        blocked_by="shell_enforcement_error",
    )


def _core_on_allow(result):
    del result  # #404: no bypass envelope remains; allow is just allow.


# blocked_by markers — the gates that emit a (marker, blocked_by)
# pair in their why tuple. evaluate_tool accumulates why
# across all gates, so we can't read why[1] verbatim; scan
# for the marker instead.
_BLOCKED_BY_AFTER_MARKER = (
    "orchestrator_deny",
    "agent_brief_blocked",
    "reconnect_required",
)
# Gates whose denials historically omitted blocked_by from
# hookSpecificOutput — preserve that exact shape.
_NO_BLOCKED_BY_PREFIX = (
    "managed_mode_required",
    "conductor_comms",
)


def _core_on_deny(result):
    why_list = list(result.why or ())
    # Find the deciding gate's marker in the accumulated tuple.
    # The deciding marker is the LAST one (gates short-circuit
    # on terminal; later why entries come from the gate that
    # produced the terminal verdict).
    decider = why_list[-1] if why_list else ""
    # Find blocked_by either by trailing position after a
    # known (marker, blocked_by) gate, or by looking at the
    # LAST why entry when the gate's convention is to put
    # blocked_by as the second element.
    blocked_by = ""
    for i, marker in enumerate(why_list):
        if marker in _BLOCKED_BY_AFTER_MARKER and i + 1 < len(why_list):
            blocked_by = str(why_list[i + 1])
            break
    # Some gates (managed_mode_required, conductor_comms)
    # historically omitted blocked_by entirely. Detect them
    # by their why-marker prefix.
    for prefix in _NO_BLOCKED_BY_PREFIX:
        if decider.startswith(prefix) or any(w.startswith(prefix) for w in why_list):
            blocked_by = ""
            break
    return {
        "verdict": "deny",
        "reason": result.reason or "",
        "blocked_by": blocked_by,
    }


def _core_on_ask(result):
    # Sticky-grant-pending uses ask_kind="sticky_grant_registration".
    # Orchestrator freeze ask is intercepted by on_freeze
    # (which fires first when a FREEZE marker is present),
    # so any ask reaching here is the sticky-grant case.
    return {
        "verdict": "ask",
        "reason": result.reason or "",
        "ask_kind": "sticky_grant_registration",
    }


def _core_on_freeze(fields: dict):
    # Both session_freeze_pretool and orchestrator_check's
    # needs_confirmation path produce FREEZE marker fields:
    # {reason, blocked_by, freeze_state}. The adapter's envelope
    # shape mirrors that exactly.
    out: dict[str, object] = {
        "verdict": "freeze",
        "reason": fields.get("reason", ""),
        "blocked_by": fields.get("blocked_by", "session_frozen"),
    }
    if fields.get("freeze_state"):
        out["freeze_state"] = fields["freeze_state"]
    return out


def _run_shell_policy_shadow(
    *,
    project_root: Path,
    host_kind: str,
    tool_name: str,
    tool_input: object,
    cli_session_id: str,
    result,
) -> None:
    """ShellPolicy shadow (Batch 1.5, observe-only).

    Side-effect-free: consumes the ALREADY-computed live verdict via
    a replay delegate; never re-runs the cascade, never blocks,
    never enables native execution. Default OFF. Best-effort.
    """
    try:
        from .shell_policy_shadow import run_pretool_shadow

        run_pretool_shadow(
            project_root=project_root,
            host=host_kind,
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else {},
            host_session_id=cli_session_id or "",
            live_verdict=str(result.verdict or ""),
            live_why=tuple(result.why or ()),
        )
    except Exception:
        pass


def _decision_from_gate_result(result) -> ToolUseDecision:
    """Map the canonical gate result into plain decision data (adapter
    renders). Branch order preserved verbatim from decide_tool_use (#413).
    """
    # 1. Terminal with a hook-produced decision dict → translate.
    he = result.host_envelope
    if he is not None:
        return _decision_from_host_envelope(he)

    # 2. Allow (kill-switch bypass audited in _core_on_allow).
    if result.verdict == "allow":
        return ToolUseDecision(verdict="allow")

    # 3. Deny / ask without a hook decision (defensive — should not
    # happen with core_hooks wired, but cover the case). The old
    # adapter rendered BOTH as a deny envelope; preserve that.
    if result.verdict in ("deny", "ask"):
        return ToolUseDecision(
            verdict="deny",
            reason=result.reason or "",
            blocked_by=str(result.why[1]) if len(result.why or ()) >= 2 else "",
        )

    # 4. Continue: surface additional_context_blocks (conductor
    # messages + x-ray goggles) for the adapter to render.
    if result.additional_context_blocks:
        return ToolUseDecision(
            verdict="allow",
            meta={
                "additional_context_blocks": list(result.additional_context_blocks),
            },
        )

    return ToolUseDecision(verdict="allow")


def _decision_from_host_envelope(he) -> ToolUseDecision:
    """Translate a hook-produced decision dict into a ToolUseDecision.

    Branch order preserved verbatim from decide_tool_use (#413).
    """
    if isinstance(he, dict) and he.get("verdict") == "freeze":
        meta: dict = {}
        if he.get("freeze_state"):
            meta["freeze_state"] = he["freeze_state"]
        return ToolUseDecision(
            verdict="freeze",
            reason=str(he.get("reason") or ""),
            blocked_by=str(he.get("blocked_by") or "session_frozen"),
            meta=meta,
        )
    if isinstance(he, dict) and he.get("verdict") == "ask":
        return ToolUseDecision(
            verdict="ask",
            reason=str(he.get("reason") or ""),
            meta={"ask_kind": str(he.get("ask_kind") or "")},
        )
    if isinstance(he, dict) and he.get("verdict") == "deny":
        return ToolUseDecision(
            verdict="deny",
            reason=str(he.get("reason") or ""),
            blocked_by=str(he.get("blocked_by") or ""),
        )
    # Foreign/pre-rendered envelope (defensive) — passthrough.
    return ToolUseDecision(verdict="deny", meta={"host_envelope": he})


def join_response_text(resp: object) -> str:
    """Flatten a tool_response (str / dict / list) into scan text."""
    if isinstance(resp, str):
        return resp
    parts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(resp)
    return "\n".join(parts)


# ── S4 (#251): the UserPromptSubmit pipeline, relocated VERBATIM from
# claude_hook (phase 4a: return shapes unchanged — the CC additionalContext /
# block dicts ARE the contract for now; UserPromptOutcome conversion is 4b).
# Only mechanical ref adaptations: self.runtime->runtime; 4 thin-delegate
# calls -> the module shims below. host_kind threads the host identity.


def resolve_session_freeze(runtime, project_root, prompt, *, host_session_id=""):
    from .prompt_mutator import PromptMutator

    PromptMutator(runtime).resolve_session_freeze(
        prompt=prompt, host_session_id=host_session_id, project_root=project_root,
    )


def lightweight_prompt_context(
    runtime, host_kind, action_kind, route, project_root,
    host_state=None, prompt="", cli_session_id="",
):
    """Mirror of the CC adapter's lightweight-context delegate, host_kind
    parameterized (bug #234-1 precedence preserved: route wins)."""
    from .prompt_context_service import PromptContextBuilder

    prompt_payload = host_state if isinstance(host_state, dict) else {}
    session_state = (
        prompt_payload.get("session_state")
        if isinstance(prompt_payload.get("session_state"), dict)
        else {}
    )
    session_id = str(
        route.get("session_id") or session_state.get("session_id") or ""
    ).strip()
    return PromptContextBuilder(runtime).build_enforced_context(
        action_kind, session_id, route, prompt_payload, prompt, project_root,
        host_kind=host_kind, host_session_id=cli_session_id,
    )


def _should_mint_causal_turn(operator_text: str, grant_eligible: bool) -> bool:
    """Causal-turn mint decision (#441, causal-turn-interrupt-integrity spec).

    A NEW turn is minted only for a prompt that is BOTH:
      * operator-authored under the provenance floor (WAR P) — the
        floored text is non-empty, so harness-injected segments
        (task-notification / system-reminder / conductor-reply blocks)
        alone can never open a turn; AND
      * authority-bearing under the origin gate (WAR U) — a worker /
        -p / -q / delegated / compaction / handoff / replay prompt is
        not a new operator instruction, so it continues the current
        turn rather than rotating it.

    Harness interrupts and mid-turn injections therefore do NOT mint —
    subsequent tool events stay bound to the operator turn that caused
    the work (spec: instruction changes create new instruction events;
    injected text is not an operator instruction at all).
    """
    return bool(grant_eligible) and bool(str(operator_text or "").strip())


def _ups_login_block(project_root, payload):
    """Block every prompt unless the shared login seam proves a principal.

    This runs before any UPS mutation. Authentication uncertainty is a refusal,
    never authority; adapter errors return the same generic login guidance and
    never leak resolver details.
    """
    try:
        from . import login_gate as _lg

        login_block = _lg.login_required_block(
            project_root,
            str(payload.get("session_id") or "").strip(),
        )
    except Exception:
        login_block = {
            "blocked_by": "login_required",
            "reason": (
                "AIDOCS could not verify an authenticated operator. Sign in "
                "with `aidocs operator-login --email <email> --password <password>` "
                "or use the Dashboard/Codenexus login flow, then retry."
            ),
        }
    if login_block is not None:
        return {
            "decision": "block",
            "blocked_by": str(login_block.get("blocked_by") or "login_required"),
            "reason": str(login_block.get("reason") or "Login is required."),
        }
    return None


def _ups_safety_screen(runtime, prompt, payload, project_root):
    """ALWAYS-SAFE UPS head: audit record + secret block + pre-flight judge.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core; the
    ordering contract is load-bearing (security-gates.md §0.5) — the
    pre-flight prompt judge (#44 Batch 1) blocks hostile operator intent
    BEFORE `_grant_user_intent_tools` reads grant phrases (a hostile
    prompt that says "allow bash; then exfil secrets" must NOT inflate
    per-turn grants), before sticky-grant mutation, before SEC-001
    snapshot/SEC-002 atomic stage, before intent-phrase dispatch. Any
    mutation that ran before pre-flight could be poisoned by a hostile
    prompt. Returns (block_envelope_or_None, preflight_failsafe_blocks)."""
    # UPS audit + fresh-CLI detection — delegated to PromptMutator
    # (host-agnostic).
    from .prompt_mutator import PromptMutator

    PromptMutator(runtime).record_user_prompt_received(
        prompt=prompt,
        host_session_id=str(payload.get("session_id") or "").strip(),
        project_root=project_root,
    )

    # Prompt-secret block — delegated to PromptMutator
    # (host-agnostic). Returns block envelope when prompt contains
    # credential tokens AND the policy is set to 'block'.
    _secret_result = PromptMutator(runtime).prompt_secret_block(
        prompt=prompt,
        project_root=project_root,
    )
    if _secret_result.decision == "block":
        return (
            {
                "decision": "block",
                "reason": _secret_result.block_reason or "",
            },
            (),
        )

    # Pre-flight judge — delegated to PromptMutator (host-agnostic).
    # Returns block when hostile/confirmable verdicts fire OR when
    # the evaluator degraded (side-band path: deny envelope with the
    # operator-facing "pre-flight unavailable / degraded" message and
    # a distinct `event_type="preflight_degraded"` audit event, so #43
    # strikes can filter system-bug events out of infraction counts).
    _preflight_result = PromptMutator(runtime).preflight_judge(
        prompt=prompt,
        project_root=project_root,
    )
    if _preflight_result.decision == "block":
        return (
            {
                "decision": "block",
                "reason": _preflight_result.block_reason or "",
            },
            (),
        )
    # SUPER-ADMIN FAILSAFE allow-path (2026-07-16): a forbidden verdict for a
    # super_admin does NOT block — preflight_judge returns decision="allow" with
    # a sanitized advisory (rule_ids only). Capture it here and surface it on the
    # final additionalContext of THIS turn so the agent confirms intent with the
    # operator before acting. The prompt is untouched and never echoed back.
    # Defensive read: the failsafe advisory is best-effort — a preflight
    # result without this attribute (partial stub / future return shape)
    # must never crash the prompt path into a fail-closed block.
    _preflight_failsafe_blocks: tuple[str, ...] = tuple(
        getattr(_preflight_result, "additional_context_blocks", None) or ()
    )
    return None, _preflight_failsafe_blocks


def _ups_worker_lane_leg(runtime, payload, project_root):
    """Worker lane mailbox + protocol injection — delegated to
    PromptMutator. CC resolves worker identity from env vars
    (AIDOCS_EXPERT_*); other hosts will surface this via their
    runtime mechanism. The service is identity-source-neutral.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core; a
    worker rewrite SHORT-CIRCUITS the pipeline (its envelope is returned
    before the origin gate, so a worker prompt can never reach any
    authority-bearing stage). Returns the envelope or None."""
    import os as _os_env_worker

    from .prompt_mutator import PromptMutator

    _worker_result = PromptMutator(runtime).worker_lane_intercept(
        project_root=project_root,
        worker_lane_id=str(
            payload.get("worker_lane_id")
            or _os_env_worker.environ.get("AIDOCS_EXPERT_LANE_ID", "")
        ).strip(),
        worker_session_id=str(
            payload.get("worker_session_id")
            or _os_env_worker.environ.get("AIDOCS_EXPERT_SESSION_ID", "")
        ).strip(),
        worker_id=str(
            payload.get("worker_id")
            or _os_env_worker.environ.get("AIDOCS_EXPERT_ID", "")
        ).strip(),
    )
    if _worker_result.rewritten_prompt is not None:
        # Worker turn → also dump the WORKER role (what the seat does)
        # for the AIDOCS-spawned subagent. Best-effort, never blocks.
        _worker_ctx = _worker_result.rewritten_prompt
        try:
            _wrole = runtime.hub.skills.read_role("worker")
            if _wrole and _wrole.get("content_text"):
                _worker_ctx = f"== WORKER ROLE ==\n{_wrole['content_text']}\n\n" + _worker_ctx
        except Exception:
            pass
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _worker_ctx,
            },
        }
    return None


def _ups_origin_gate(payload, project_root, verified_grant_eligible, _why):
    """ORIGIN GATE (origin-bound law) — built ONCE, before ANY
    authority-bearing pipeline. Prompt ORIGIN — not prompt shape —
    decides whether grant / mutation / confirmation logic may run. A
    worker / -p / -q / delegated / compaction / handoff / replay / tool
    prompt is INERT for all such consumption even if it contains an
    exact grant/operator phrase.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core;
    fail direction unchanged — an unevaluable gate yields
    _grant_eligible=False (fail closed) and the Doctrine XIII
    attribution marker. Returns (_grant_eligible, _origin_principal)."""
    import os as _os_origin_gate

    _origin_worker_lane_id = str(
        payload.get("worker_lane_id")
        or _os_origin_gate.environ.get("AIDOCS_EXPERT_LANE_ID", "")
    ).strip()
    try:
        from .identity_resolver import current_principal_type as _cpt

        _origin_principal = _cpt(project_root)
        if not str(_origin_principal or "").strip():
            _origin_principal = "unknown"
    except Exception:
        _origin_principal = "unknown"
    try:
        from .operator_intent_resolver import (
            is_authority_bearing_prompt_eligible as _is_grant_eligible,
        )

        _origin_ctx = {
            "event_kind": "UserPromptSubmit",
            "principal_type": _origin_principal,
            "host_session_id": str(payload.get("session_id") or "").strip(),
            "project_root": str(project_root),
            "worker_lane_id": _origin_worker_lane_id,
            "source_surface": str(payload.get("source_surface") or ""),
            "delivery": str(payload.get("delivery") or ""),
        }
        _grant_eligible = bool(_is_grant_eligible(_origin_ctx))
    except Exception:
        # Fail closed: if the gate can't be evaluated, treat the
        # prompt as ineligible for authority-bearing consumption.
        _grant_eligible = False
    if verified_grant_eligible is not None:
        _grant_eligible = bool(verified_grant_eligible) and not _origin_worker_lane_id
    elif not _grant_eligible:
        # Doctrine XIII attribution: the caller did NOT verify grant
        # eligibility and the in-core origin gate withheld authority —
        # record the fail-closed marker so audits can see WHY no
        # authority-bearing pipeline ran this turn.
        _why("grant_eligible_unset_failed_closed")
    return _grant_eligible, _origin_principal


def _ups_freeze_soul_stage(
    runtime,
    project_root,
    payload,
    _operator_text,
    _grant_eligible,
    _tx,
    transaction_stage_hook,
):
    """Origin-gated freeze/unfreeze/soul stage.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core; the
    whole stage runs ONLY under `if _grant_eligible:` (origin-bound law)
    and each sub-stage runs only when the service confirms the protecting
    snapshot (fail-closed skip via `_tx(...) is False`). Returns
    _chat_unfreeze_blocks."""
    _chat_unfreeze_blocks: tuple = ()
    if _grant_eligible:
        # Confirmation-freeze resolver (#39, 2026-04-25). Single-window
        # contract: exact phrase mints grant, cancel pattern records
        # denial, ANYTHING ELSE keeps the session frozen (a stray/garbage
        # prompt no longer lifts it). AUTHORITY-BEARING: gated on origin.
        if _tx("resolve_session_freeze", "before"):
            try:
                resolve_session_freeze(runtime,
                    project_root,
                    _operator_text,
                    host_session_id=str(payload.get("session_id") or "").strip(),
                )
                _tx("resolve_session_freeze", "after")
            except Exception:
                if transaction_stage_hook is not None:
                    raise
                # Best-effort. Failure here means the freeze sticks until
                # next UPS — annoying but safe (operator retries).
                pass
        # Operator chat-unfreeze: an EXPLICIT, MOTIVATED unfreeze
        # request (NLP "unfreeze the agent because …" OR the literal
        # clear-freeze command) by a permissioned operator lifts ANY
        # freeze (incl. a security lock). Origin-gated here so the
        # frozen AGENT cannot lift its own lock; perms checked inside.
        if _tx("resolve_chat_unfreeze", "before"):
            try:
                from .prompt_mutator import PromptMutator as _PM

                _uf = _PM(runtime).resolve_chat_unfreeze(
                    prompt=_operator_text,
                    host_session_id=str(payload.get("session_id") or "").strip(),
                    project_root=project_root,
                )
                _chat_unfreeze_blocks = tuple(getattr(_uf, "additional_context_blocks", ()) or ())
                _tx("resolve_chat_unfreeze", "after")
            except Exception:
                if transaction_stage_hook is not None:
                    raise
                _chat_unfreeze_blocks = ()

        # ── Sovereign soul gate (origin-gated, per-turn) ──────────
        # The Emperor's word opens the named soul for this turn by
        # minting a single-use ai_soul read/write grant. Soul content is
        # never injected into prompt context. This whole block is
        # _grant_eligible — only the king's own prompt can open a soul;
        # a worker / delegated / replayed prompt never can. Fail-closed.
        if _tx("soul_grants", "before"):
            try:
                from .empire_soul_gate import mint_turn_grants

                mint_turn_grants(
                    project_root,
                    _operator_text,
                    host_session_id=str(
                        payload.get("session_id") or ""
                    ).strip(),
                    managed_mode=runtime.hub.managed_mode,
                    record_event=runtime.hub.execution.record_event,
                )
                _tx("soul_grants", "after")
            except Exception:
                if transaction_stage_hook is not None:
                    raise
    return _chat_unfreeze_blocks


def _ups_escalation_scrub_stage(
    runtime,
    project_root,
    payload,
    prompt,
    _operator_text,
    _grant_eligible,
    _tx,
):
    """RBAC escalation scrub: detect `approve: <email> <password>` /
    `deny: <request_id>` lines and strip credentials from the prompt
    BEFORE the agent ever sees it. Approvals that authenticate also flip
    the pending escalation row to approved; the consume pass in the
    privilege stage picks it up. Runs before grant-detection /
    route-classification so the scrubbed prompt is what downstream logic
    operates on.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core.
    AUTHORITY-BEARING: gated on origin — a worker/delegated prompt cannot
    consume an approval even if it carries an `approve:`/`deny:` line.
    Returns (prompt, _operator_text, _escalation_side_effects); on a
    rewrite the payload's prompt is updated and the provenance floor is
    re-derived from the scrubbed text."""
    from .prompt_mutator import PromptMutator

    _escalation_side_effects: list[dict[str, object]] = []
    if _grant_eligible:
        if _tx("escalation_scrub", "before"):
            _scrub_result = PromptMutator(runtime).escalation_scrub(
                prompt=prompt,
                project_root=project_root,
            )
            _tx("escalation_scrub", "after")
            if _scrub_result.rewritten_prompt is not None:
                prompt = _scrub_result.rewritten_prompt
                payload["prompt"] = prompt
                _escalation_side_effects = list(_scrub_result.side_effects)
                # Provenance floor: re-derive the operator-authored text
                # from the scrubbed prompt (credential lines removed).
                try:
                    from .canonical_intent_registry import (
                        strip_non_operator_text as _snot_refresh,
                    )

                    _operator_text = _snot_refresh(prompt)
                except Exception:
                    _operator_text = ""
    return prompt, _operator_text, _escalation_side_effects


def _ups_sec002_rollback(runtime, project_root, session_id, _sec001_snapshot, _sec002_exc):
    """SEC-002 (2026-04-23) atomic mutation stage failure handling.
    On any exception escaping the inner per-site nets: restore the
    pre-mutation snapshot, emit prompt_mutation_failed, and flip the
    SEC-005 degraded_state badge. Carve-outs (audit, cli_session_id)
    already ran and are NOT rolled back.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core's
    except branch (goal is "no partial state + visible failure," not
    block the operator)."""
    if _sec001_snapshot:
        try:
            runtime.hub.query_gate.restore_privilege_state(
                project_root,
                session_id,
                dict(_sec001_snapshot),
            )
        except Exception:
            pass
    _sec002_event_id = ""
    try:
        _sec002_event_id = (
            runtime.hub.execution.record_event(
                project_root,
                event_kind="prompt_mutation_failed",
                source_kind="sec002_atomic_stage",
                session_id=session_id or None,
                capability_name="UserPromptSubmit",
                action_kind="mutation_error",
                status="rolled_back",
                payload={
                    "exception_type": type(_sec002_exc).__name__,
                    "exception_msg": str(_sec002_exc)[:200],
                },
            )
            or ""
        )
    except Exception:
        pass
    # SEC-005 (2026-04-23): surface degraded_state on the
    # session row so the dashboard top bar + right-panel
    # strip render the red badge without a second query.
    # Reason string is exception_type:msg to keep the UI
    # short; full payload stays in the audit event.
    if session_id:
        try:
            _reason = f"{type(_sec002_exc).__name__}: {str(_sec002_exc)[:140]}"
            runtime.hub.query_gate.set_degraded_state(
                project_root,
                session_id,
                reason=_reason,
                failure_event_id=str(_sec002_event_id),
            )
        except Exception:
            pass


def _ups_privilege_mutation_stage(
    runtime,
    project_root,
    session_id,
    _operator_text,
    _grant_eligible,
    _tx,
    transaction_stage_hook,
    _sec001_snapshot,
    _escalation_side_effects,
):
    """AUTHORITY-BEARING privilege-mutation stage. ORIGIN-BOUND: runs
    only for a bound session AND an authority-bearing origin. A worker /
    -p / -q / delegated / compaction / handoff / replay prompt skips the
    ENTIRE block — no sticky grants, no user-intent grants, no per-turn
    intent state, no DNT/config-set grants, no lane-exit, no approval
    peek — even if it contains an exact grant phrase.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core; the
    stage cascade order is load-bearing (sticky lifecycle → sticky
    answers → user-intent grants → approval PEEK → per-turn intent →
    DNT → config-set → lane-exit) and the whole block is one SEC-002
    try/except (rollback via _ups_sec002_rollback). Returns
    (_sticky_drop_blocks, _sec002_tripped)."""
    from .prompt_mutator import PromptMutator

    _sec002_tripped = False
    # #99 FIX1 (UX half): default so the sticky sink-drop surfacing is safe on
    # the skip path (worker/delegated/-p prompts never enter the grant block).
    _sticky_drop_blocks: tuple[str, ...] = ()
    if session_id and _grant_eligible:
        try:
            # Per-turn TTL: drop non-sticky user-intent grants from the
            # previous turn while preserving the sticky baseline. An
            # explicit "revoke sticky" phrase in this prompt clears the
            # sticky slice as well before the new-turn detection runs.
            # Sticky-grant lifecycle (revoke / clear_expired /
            # clear_turn_edited) — delegated to PromptMutator.
            if _tx("sticky_lifecycle", "before"):
                PromptMutator(runtime).apply_sticky_grant_lifecycle(
                    prompt=_operator_text,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                _tx("sticky_lifecycle", "after")
            # Resolve prior-turn pending sticky grants (Phase 3 of
            # backlog #15). Operator answered "yes" on an AskUserQuestion
            # → register the grant. "no" → drop. Any other reply or no
            # pending → clear_expired_pending sweeps stale rows so the
            # single-turn TTL holds.
            # Sticky-grant answer consumption + user-intent tool
            # grants — both delegated to PromptMutator.
            if _tx("sticky_answer", "before"):
                PromptMutator(runtime).consume_sticky_grant_answers(
                    prompt=_operator_text,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                _tx("sticky_answer", "after")
            if _tx("user_tool_grants", "before"):
                _uig_result = PromptMutator(runtime).apply_user_intent_tool_grants(
                    prompt=_operator_text,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                _tx("user_tool_grants", "after")
                # #99 FIX1 (UX half): surface a sticky sink-drop (raw shell/file tool
                # that cannot be sticky) to the operator in THIS turn's context.
                _sticky_drop_blocks = tuple(
                    getattr(_uig_result, "additional_context_blocks", ()) or (),
                )
            # SEC-001 hotfix (2026-04-23): PEEK not CONSUME. Pre-hotfix
            # this called consume_approvals_for_session which burned
            # the admin approval unconditionally on every prompt. Now
            # we read pending approvals WITHOUT consuming — the
            # matching-action-consume path (SEC-003 proper fix) will
            # land later. Until then this path only SURFACES the
            # pending approvals for audit/telemetry; actual grant
            # application happens at tool-call time when the action
            # matches the approval's capability.
            try:
                from .escalation_hook import (
                    peek_approved_for_session as _peek_escalations,
                )

                _approved = _peek_escalations(project_root, session_id)
                for _req in _approved:
                    _escalation_side_effects.append(
                        {
                            "kind": "escalation.pending",
                            "request_id": _req.request_id,
                            "gate_permission": _req.gate_permission,
                            "sticky": _req.sticky,
                        },
                    )
            except Exception:
                pass
            # Per-turn intent-state mutations — delegated to
            # PromptMutator.apply_per_turn_intent_state. Bundles 4
            # coupled state writes (bash subcommand grants, ask-state
            # plumbing, credential stash, destructive stash) that all
            # key on the managed session_id. Each sub-mutation is
            # independent inside the service so a single failure
            # doesn't suppress the others.
            if _tx("per_turn_intent", "before"):
                PromptMutator(runtime).apply_per_turn_intent_state(
                    prompt=_operator_text,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                _tx("per_turn_intent", "after")
            # DNT (DO-NOT-TOUCH) grants — delegated to PromptMutator
            # (host-agnostic). Writes both per-process module state
            # AND sqlite (#236 2026-05-12 cross-process truth).
            if _tx("dnt_grants", "before"):
                PromptMutator(runtime).apply_dnt_grants(
                    prompt=_operator_text,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                _tx("dnt_grants", "after")
            # Config-set grants — delegated to PromptMutator
            # (host-agnostic).
            if _tx("config_grants", "before"):
                PromptMutator(runtime).apply_config_set_grants(
                    prompt=_operator_text,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                _tx("config_grants", "after")

            # Conductor lane-exit escape hatch — delegated to PromptMutator.
            # Workers self-fenced via env; sticky auto-exit conditional
            # on no-live-worker check.
            import os as _os_lane_exit

            if _tx("lane_exit_grant", "before"):
                PromptMutator(runtime).apply_lane_exit_grant(
                    prompt=_operator_text,
                    managed_session_id=session_id,
                    project_root=project_root,
                    is_worker_proc=bool(
                        _os_lane_exit.environ.get(
                            "AIDOCS_EXPERT_LANE_ID",
                            "",
                        ).strip(),
                    ),
                )
                _tx("lane_exit_grant", "after")
        except Exception as _sec002_exc:
            if transaction_stage_hook is not None:
                raise
            _sec002_tripped = True
            _ups_sec002_rollback(
                runtime, project_root, session_id, _sec001_snapshot, _sec002_exc
            )
    return _sticky_drop_blocks, _sec002_tripped


def _ups_unbound_dnt_stage(
    runtime,
    project_root,
    session_id,
    _operator_text,
    _grant_eligible,
    _tx,
    transaction_stage_hook,
):
    """DNT grants for UNMANAGED projects (operator repro 2026-06-11,
    DentalApp): the privilege-mutation stage is gated on
    `session_id and _grant_eligible`, and session_id is only set
    for MANAGED sessions — so in an unmanaged project the literal
    "protect <path>" phrase never even reached the parser and
    ai_protect was refused by construction. DNT authority is the
    human's direct word about FILES, not session state: run the
    same deterministic literal parser with no session bound. The
    origin gate (_grant_eligible) still applies — worker/-p/
    compaction/replay prompts mint nothing. Grants land under the
    '__unbound__' sqlite key (see prompt_mutator) which the
    ai_protect read side always checks.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core."""
    from .prompt_mutator import PromptMutator

    if _grant_eligible and not session_id:
        if _tx("dnt_grants", "before"):
            try:
                PromptMutator(runtime).apply_dnt_grants(
                    prompt=_operator_text,
                    managed_session_id="",
                    project_root=project_root,
                )
                _tx("dnt_grants", "after")
            except Exception:
                if transaction_stage_hook is not None:
                    raise


def _ups_intent_dispatch_stage(
    runtime,
    project_root,
    session_id,
    _operator_text,
    _grant_eligible,
    _tx,
):
    """Closed-vocabulary intent-phrase detection — runs before route
    classification so state changes (plan_session_enter, etc.) are
    visible to downstream context-building. Dispatch results are
    appended to the additional_context block downstream so the agent
    sees the activation acknowledgment ("Plan mode active. Scope: ...")
    in the same turn that triggered it.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core.
    AUTHORITY-BEARING: gated on origin so a worker/delegated prompt
    cannot trigger state changes."""
    from .prompt_mutator import PromptMutator

    intent_dispatch_results: list[dict[str, object]] = []
    if _grant_eligible:
        _tx("intent_dispatch", "before")
        _intent_dispatch_result = PromptMutator(runtime).intent_phrase_dispatch(
            prompt=_operator_text,
            managed_session_id=session_id or "",
            project_root=project_root,
        )
        intent_dispatch_results = [
            {"context": block} for block in _intent_dispatch_result.additional_context_blocks
        ]
        _tx("intent_dispatch", "after")
    return intent_dispatch_results


def _sec001_restore_and_audit(
    runtime, project_root, session_id, _sec001_snapshot, reason_tag
):
    """SEC-001 HOTFIX (2026-04-23): restore privilege state before
    returning a block decision. Snapshot was taken before any mutation;
    restore writes it back verbatim. Called inline (not a closure) so
    the audit event emits once per actual rollback, not per
    block-branch. Extraction pin (#413 T2): verbatim (was a closure in
    _run_user_prompt_core, hoisted with explicit parameters)."""
    if not session_id or not _sec001_snapshot:
        return
    try:
        runtime.hub.query_gate.restore_privilege_state(
            project_root,
            session_id,
            _sec001_snapshot,
        )
        runtime.hub.execution.record_event(
            project_root,
            event_kind="prompt_mutation_rolled_back",
            source_kind="sec001_hotfix",
            session_id=session_id,
            capability_name="UserPromptSubmit",
            action_kind="rollback",
            status="rolled_back",
            payload={"reason_tag": reason_tag},
        )
    except Exception:
        # Never let restore itself break the block path.
        pass


def _ups_managed_resolution(
    runtime,
    project_root,
    payload,
    prompt,
    action_kind,
    route,
    session_id,
    _sec001_snapshot,
    _tx,
):
    """Managed-mode resolution + route-block gate.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core.
    Cascade: (1) unmanaged route → self-heal auto-bind (a MUTATION
    stage: fail-closed skip when no protecting snapshot) and re-route;
    (2) still unmanaged → bug #234-2: NEVER hard-block — restore the
    SEC-001 snapshot and NO-OP (`{}`) so the agent runs the prompt
    normally (managed enforcement applies only once a session is
    actually bound); (3) route blocked_reason → restore + block
    envelope. Returns (route, envelope_or_None) — a non-None envelope
    is returned to the host verbatim."""
    from .prompt_mutator import PromptMutator

    if not route.get("managed_mode"):
        # Self-heal auto-bind — delegated to PromptMutator.
        # When a session was auto-bound, we re-fetch the route
        # to pick up the new managed_mode state. If nothing was
        # bound (truly uninitialized project), fall through to
        # the block envelope.
        auto_bound = False
        _ab_result = None
        if _tx("auto_bind_session", "before"):
            # MUTATION stage (binds managed-mode/session state): skipped
            # fail-closed when no protecting snapshot is available.
            _ab_result = PromptMutator(runtime).auto_bind_session(
                project_root=project_root,
            )
            _tx("auto_bind_session", "after")
        if _ab_result is not None and _ab_result.why and _ab_result.why[0] == "auto_bind_session":
            route = runtime.aidocs_route_prompt(
                project_root,
                user_request=prompt,
                action_kind=action_kind,
                host_session_id=str(payload.get("session_id") or "").strip(),
            )

            auto_bound = bool(route.get("managed_mode"))
        if not auto_bound:
            _sec001_restore_and_audit(
                runtime, project_root, session_id, _sec001_snapshot,
                "managed_mode_inactive",
            )
            return route, {}

    if route.get("blocked_reason"):
        _sec001_restore_and_audit(
            runtime, project_root, session_id, _sec001_snapshot,
            "route_blocked_reason",
        )
        blocked_reason = str(
            route.get("blocked_reason") or "This prompt is blocked by AIDOCS runtime policy.",
        )
        return route, {
            "decision": "block",
            "reason": blocked_reason,
        }
    return route, None


def _ups_context_notices(
    runtime,
    project_root,
    session_id,
    additional_context,
    _preflight_failsafe_blocks,
    _chat_unfreeze_blocks,
    _soul_dump_blocks,
):
    """Post-route notice blocks appended to additionalContext.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core;
    append order preserved — (1) #198 one-shot preflight-block awareness
    note (sanitized rule_ids only, taken-then-cleared), (2) super-admin
    preflight failsafe advisory, (3) operator chat-unfreeze result,
    (4) soul dumps (origin-gated at build time). Each uses the same
    \\n\\n-separator convention."""
    # #198: un-blind the agent after a preflight-blocked OPERATOR prompt.
    # The operator saw the block; the agent saw nothing. Surface a one-shot,
    # SANITIZED awareness note (rule_ids only — never the hostile content)
    # on this next non-blocked turn, then clear it. Injection-safe: the note
    # is fixed-format and the rule_ids are sanitized on read.
    if session_id:
        try:
            from .preflight_block_notice_store import PreflightBlockNoticeStore

            _pf_store = PreflightBlockNoticeStore()
            _pf_notice = _pf_store.take(project_root, session_id)
            if _pf_notice:
                additional_context = (
                    (additional_context or "")
                    + ("\n\n" if additional_context else "")
                    + _pf_store.render_note(_pf_notice)
                )
        except Exception:
            pass

    # Surface the super-admin preflight failsafe advisory (sanitized, rule_ids
    # only) captured at the preflight step — the prompt passed through, so tell
    # the agent to confirm intent with the operator before acting on it.
    for _block in _preflight_failsafe_blocks:
        if _block:
            additional_context = (
                (additional_context or "")
                + ("\n\n" if additional_context else "")
                + str(_block)
            )

    # Surface the operator chat-unfreeze result (✅ cleared / 🛑 needs
    # perms-or-reason) computed in the origin-gated block above.
    for _block in _chat_unfreeze_blocks:
        if _block:
            additional_context = (
                (additional_context or "")
                + ("\n\n" if additional_context else "")
                + str(_block)
            )

    # Dump any soul(s) the Emperor's word opened this turn — sovereign
    # content injected into context (the read surface). Origin-gated
    # (built only when _grant_eligible); private to the seat.
    for _block in _soul_dump_blocks:
        if _block:
            additional_context = (
                (additional_context or "")
                + ("\n\n" if additional_context else "")
                + str(_block)
            )
    return additional_context


def _ups_auto_task_leg(
    runtime,
    project_root,
    session_id,
    prompt,
    additional_context,
    _grant_eligible,
    _tx,
    transaction_stage_hook,
):
    """Auto-task (friction removal): an imperative ("commit and push") or
    investigation question ("did I set the address?") opens a task in
    sqlite if none is active for this session, so the agent doesn't have
    to call task_begin by hand. Answerable prompts open nothing.
    SQL-only (no SESSION.md/PLAN.md) per the no-file-layer doctrine;
    best-effort — a store hiccup never blocks the prompt.

    ORIGIN-BOUND: task lifecycle is law-adjacent state, so only an
    authority-bearing OPERATOR prompt may auto-open a task. Worker /
    delegated / -p / -q / replayed prompts are inert here (same
    _grant_eligible gate the grant/mutation pipeline obeys) — a
    sub-agent must not mutate the session's task lifecycle.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core."""
    if _tx("auto_task", "before") and session_id and _grant_eligible:
        # MUTATION stage (task-lifecycle write): fail-closed skip when the
        # protecting snapshot is unavailable.
        try:
            from .prompt_intent_classifier import classify_prompt_intent

            _intent = classify_prompt_intent(prompt)
            if _intent in ("imperative", "investigation"):
                _kind = "investigation" if _intent == "investigation" else "work"
                _opened = runtime.auto_task_begin(
                    project_root,
                    session_id,
                    goal=prompt[:200],
                    kind=_kind,
                    origin_prompt=prompt,
                )
                if _opened is not None:
                    _task_note = (
                        f"Auto-started {_kind} task "
                        f"`{_opened['task_id']}` (goal: {_opened['goal']}). "
                        f"Use task_update / task_complete as you work; "
                        f"no need to call task_begin."
                    )
                    additional_context = (
                        (additional_context or "")
                        + ("\n\n" if additional_context else "")
                        + _task_note
                    )
        except Exception:
            if transaction_stage_hook is not None:
                raise
            # Auto-task is a convenience; never block a prompt on it.
            pass
        _tx("auto_task", "after")
    return additional_context


def _ups_operator_intent_leg(
    project_root,
    payload,
    prompt,
    _origin_principal,
    _grant_eligible,
    additional_context,
):
    """Operator-intent resolution (first vertical slice): maps an
    authenticated operator's natural-language control-plane request
    ("enable decision trace for this session") into a structured,
    host_binding-authenticated, RBAC-gated, audited mutation via the
    canonical config service. NLP authorizes nothing; host_binding
    proves WHO; the permission service decides IF; the canonical
    service performs the write; the resolver audits the seal. A
    non-human principal is refused inside resolve_and_apply before any
    identity is resolved — guardrails cannot be self-unlocked.

    HOST-AGNOSTIC PARITY (War 3): every native prompt host reaches this
    ONE canonical call site through PromptSubmitService — the host_kind
    is NOT a security boundary here (host_binding + RBAC inside
    resolve_and_apply are).

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core;
    reuses the single origin gate (_grant_eligible) — an ineligible
    prompt is inert (not even parsed). ADVISORY fail-open: an
    intent-resolution hiccup never blocks the operator's prompt."""
    if _grant_eligible:
        try:
            from .operator_intent_resolver import OperatorIntentResolver

            # WHO comes from the ATTESTED WINDOW, READ off the payload the
            # hook process stamped -- never derived here, because this may run
            # in the watchdog, whose ancestry is not this window's (#880).
            # session_id still travels as PROVENANCE for the audit trail and
            # for the no-window-derivation hosts where it is the only channel.
            from .window_key import window_from_payload

            _window, _ = window_from_payload(payload)

            _intent_outcome = OperatorIntentResolver().resolve_and_apply(
                prompt,
                project_root=project_root,
                host_session_id=str(
                    payload.get("session_id") or ""
                ).strip(),
                window_key=_window,
                principal_type=_origin_principal,
                confirm_phrase=prompt,
            )
            if _intent_outcome is not None:
                _note = operator_intent_note(_intent_outcome)
                if _note:
                    additional_context = (
                        (additional_context or "")
                        + ("\n\n" if additional_context else "")
                        + _note
                    )
        except Exception:
            # ADVISORY fail-open (require_active_task doctrine): operator-
            # intent resolution is a read/advisory consumer whose writes are
            # internally host_binding-authenticated, RBAC-gated and audited
            # (and are NOT part of the seven snapshot domains, so a service
            # rollback could never undo them anyway). An intent-resolution
            # hiccup therefore never blocks the operator's prompt — on any
            # host path, transactional or not.
            pass
    return additional_context


def _ups_dashboard_drain_tail(
    runtime,
    project_root,
    payload,
    prompt,
    _operator_text,
    _grant_eligible,
    _why,
    additional_context,
):
    """Informational/advisory tail: dashboard config advisory +
    notification drain.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core.
    dashboard_config_advisory is AUTHORITY-ADJACENT (runs a config-grant
    SHAPE detector on the prompt) and therefore ORIGIN-BOUND — no grant
    detector, even advisory-only, runs on an ineligible origin.
    notifications_drain is ALWAYS-SAFE informational. Separator shapes
    preserved: the advisory concatenates INLINE (no separator); the
    drain block uses \\n\\n."""
    try:
        from .prompt_mutator import PromptMutator

        pm = PromptMutator(runtime)
        if _grant_eligible:
            _adv = pm.dashboard_config_advisory({"prompt": _operator_text}, project_root)
        else:
            from .prompt_mutator import PromptMutationResult

            _adv = PromptMutationResult.empty()
        _drain = pm.notifications_drain(
            {"prompt": prompt, "session_id": str(payload.get("session_id") or "")},
            project_root,
        )
        for _tail_tag in tuple(_adv.why) + tuple(_drain.why):
            _why(_tail_tag)
        _tail_blocks = list(_adv.additional_context_blocks) + list(
            _drain.additional_context_blocks,
        )
        if _tail_blocks:
            # The dashboard advisory historically concatenated with
            # NO separator (space-prefixed inline); the drain block
            # used \n\n separators. Preserve both shapes.
            advisory_inline = (
                _adv.additional_context_blocks[0] if _adv.additional_context_blocks else ""
            )
            drain_block = "\n\n".join(
                _drain.additional_context_blocks,
            )
            if advisory_inline:
                additional_context = (additional_context or "") + advisory_inline
            if drain_block:
                additional_context = (
                    (additional_context or "")
                    + ("\n\n" if additional_context else "")
                    + drain_block
                )
    except Exception:
        pass
    return additional_context




def _format_investigate_guide_for_ups(
    concept: str,
    result: dict,
    *,
    max_chars: int = 4200,
) -> str:
    """Render investigate's aggregated fan as a bounded UPS navigation guide."""
    findings = result.get("findings") or []
    if not findings:
        return ""

    def _label(item: dict) -> str:
        value = (
            item.get("symbol")
            or item.get("path")
            or item.get("memory")
            or item.get("title")
            or item.get("content")
            or item.get("module")
            or item.get("entity")
            or item.get("field")
            or item.get("class")
        )
        text = str(value or "").strip()
        return text[:180] + ("…" if len(text) > 180 else "")

    lines = [f"🧭 Related project knowledge for `{concept}` (investigate guide):"]
    for finding in findings[:9]:
        if not isinstance(finding, dict):
            continue
        area = str(finding.get("area") or "unknown")
        count = int(finding.get("count") or 0)
        labels = [
            _label(item)
            for item in (finding.get("top") or [])[:2]
            if isinstance(item, dict) and _label(item)
        ]
        suffix = " — " + "; ".join(f"`{label}`" for label in labels) if labels else ""
        lines.append(f"- {area}: {count}{suffix}")
    tool_names = [
        str(item.get("tool") or "")
        for item in (result.get("next_tools") or [])[:4]
        if isinstance(item, dict) and item.get("tool")
    ]
    if tool_names:
        lines.append("- next tools: " + ", ".join(f"`{name}`" for name in tool_names))
    lines.append("Use the named lanes before broad exploration; this is a guide, not a data dump.")
    rendered = "\n".join(lines)
    return rendered[:max_chars]
def _ups_advise_rails(
    runtime,
    project_root,
    prompt,
    _operator_text,
    additional_context,
):
    """ADVISE-only context rails appended after the transaction commits.

    Extraction pin (#413 T2): verbatim from _run_user_prompt_core;
    order preserved — (1) #219/#221 PR-1 update-intent durability
    advise + #9 durable-content capture hint, (2) #348 loud NLP-liveness
    notice, (3) #448 target resolution + #853 aggregated investigate guide
    (do-means-know). Every rail is fail-quiet and never blocks."""
    # ── Update-intent durability advise (#219/#221 PR-1) ──────────────
    # Deterministic detector over the OPERATOR prompt only: on a
    # plan/spec/priority/decision change, persist a pending_durable_write
    # row and inject the self-repeating "record this durably" reminder.
    # ADVISE only — never blocks; failures never break the UPS.
    try:
        _ui_session = ""
        try:
            _ui_session = resolve_managed_session(
                runtime.hub.managed_mode, project_root
            )
        except Exception:
            _ui_session = ""
        if _ui_session:
            from .update_intent_hook import process_user_prompt as _ui_process

            _ui_blocks = _ui_process(project_root, _ui_session, prompt)
            if _ui_blocks:
                additional_context = (
                    (additional_context or "")
                    + ("\n\n" if additional_context else "")
                    + "\n\n".join(_ui_blocks)
                )
            # ── Durable-content capture hint (#9) ────────────────────
            # Sibling advise rail: classify the OPERATOR prompt for
            # declarative-durable content (rules/decisions/invariants)
            # and queue a one-shot 💾 "record as durable?" hint that the
            # notification injector surfaces on the next tool call.
            # ADVISE only; failures never break the UPS.
            try:
                from .durable_hint_store import observe_content as _dh_observe

                _dh_observe(project_root, _ui_session, prompt)
            except Exception:
                pass
    except Exception:
        pass

    # ── #348: NLP liveness is LOUD on the UPS response itself ─────
    # spaCy was silently dead on the operator box for weeks and nothing
    # surfaced it at the prompt boundary. If the NLP security surface is
    # degraded (or unverifiable), THIS response says so — never a silent
    # keyword-only fallback. Detectors themselves stay fail-SAFE (§X
    # DROP-on-doubt); the notice is informational and never blocks.
    try:
        from . import gate_health as _gh_nlp

        _nlp_notice = _gh_nlp.nlp_degraded_ups_notice(project_root)
        if _nlp_notice:
            additional_context = (
                (additional_context or "")
                + ("\n\n" if additional_context else "")
                + _nlp_notice
            )
    except Exception:
        pass

    # ── #448 Consumer B: intent code-target enrichment (do-means-know) ──
    # The intent layer consults the OWNED code index for file/symbol
    # mentions in the OPERATOR prompt: a prompt naming a real file or
    # symbol gets it resolved into the intent context, including the
    # named target's bounded reverse-dependency reachability summary
    # (Emperor 2026-07-18: the acting agent is HANDED its blast radius,
    # not left to discover it). Rides the existing additionalContext
    # rail; ADVISE only — fail-quiet, never blocks, LSP never inline.
    try:
        from .semantic_enrichment import (
            prompt_code_mention_block,
            resolve_prompt_code_mentions,
        )

        _prompt_text = _operator_text or prompt
        _resolution = resolve_prompt_code_mentions(project_root, _prompt_text)
        _sem_block = prompt_code_mention_block(
            project_root,
            _prompt_text,
            resolution=_resolution,
        )
        if _sem_block:
            additional_context = (
                (additional_context or "")
                + ("\n\n" if additional_context else "")
                + _sem_block
            )

        # War Q: a real resolved target triggers the SAME aggregated fan as
        # ai_investigate. Shallow + bounded keeps UPS a navigation guide; no
        # indexed mention means no probe fan and zero extra hot-path cost.
        _concept = str(_resolution.get("concept") or "").strip()
        if _concept:
            _palace = getattr(runtime.hub, "palace", None)
            _hub_ctx = None
            if _palace is not None:
                try:
                    from .palace_hub_extension import build_palace_context

                    _hub_ctx = build_palace_context(
                        runtime.hub,
                        runtime,
                        tool_name="ups.do_means_know",
                    )
                except Exception:
                    _hub_ctx = None
            _guide = runtime.hub.code.investigate(
                project_root,
                concept=_concept,
                limit=3,
                depth="shallow",
                focus="general",
                palace=_palace,
                hub_ctx=_hub_ctx,
                session_id=locals().get("_ui_session") or None,
            )
            _guide_block = _format_investigate_guide_for_ups(_concept, _guide)
            if _guide_block:
                additional_context = (
                    (additional_context or "")
                    + ("\n\n" if additional_context else "")
                    + _guide_block
                )
    except Exception:
        pass
    return additional_context


def _run_user_prompt_core(
    runtime,
    project_root,
    payload,
    *,
    host_kind="claude_code",
    audit_source="claude_hook",
    verified_grant_eligible=None,
    phase_boundary=None,
    transaction_stage_hook=None,
    why_sink=None,
):
    """Canonical D/T/P prompt-submit core invoked only by PromptSubmitService.

    Native hooks call the service automatically. Hookless WebMCP/OpenAI surfaces
    can invoke it only through an explicit router and never imply automatic UPS.
    """
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return None

    # CONNECTED login gate (#404) — blocks an unauthenticated prompt
    # before ANY UPS mutation. (Body: _ups_login_block.)
    _login_envelope = _ups_login_block(project_root, payload)
    if _login_envelope is not None:
        return _login_envelope

    def _why(tag: str) -> None:
        if why_sink is not None:
            try:
                why_sink(str(tag))
            except Exception:
                pass

    # ── IDENTITY HEAL (#135 self-heal, hoisted; #672 fallout) ──────
    # Bind this host to the project's session BEFORE anything session-keyed
    # runs. See PromptMutator.ensure_host_session_bound for the full #672/#135
    # rationale. PromptSubmitService already heals before it builds the
    # transaction participants; this call covers the other entry points into
    # this core (mid-flight UPS, hookless adapters) and is a no-op when the
    # host is already bound.
    #
    # WHY THIS IS SAFE ABOVE THE ALWAYS-SAFE HEAD, whose ordering is otherwise
    # load-bearing (security-gates.md §0.5): §0.5 exists so that no mutation
    # can be POISONED BY PROMPT CONTENT before the preflight judge screens it.
    # The heal never receives or reads the prompt — it decides purely from
    # managed-mode and session state — so a hostile prompt cannot steer which
    # session is bound. It is an identity bind, not a privilege mutation: it
    # writes no grant and no privilege column, so there is no SEC-001 snapshot
    # for it to run without (also why it needs no _tx guard, which is not yet
    # defined this early). A genuinely unmanaged project is untouched, so bug
    # #234-2's "never force managed mode" contract still holds.
    try:
        from .prompt_mutator import PromptMutator as _PromptMutatorBind

        _PromptMutatorBind(runtime).ensure_host_session_bound(
            project_root=project_root,
            host_session_id=str(payload.get("session_id") or "").strip(),
        )
    except Exception:
        pass

    # ALWAYS-SAFE head: UPS audit record → prompt-secret block →
    # pre-flight prompt judge (#44). Ordering is load-bearing — see
    # _ups_safety_screen's docstring for the poisoned-mutation rationale.
    _safety_envelope, _preflight_failsafe_blocks = _ups_safety_screen(
        runtime, prompt, payload, project_root
    )
    if _safety_envelope is not None:
        return _safety_envelope

    # Worker lane mailbox + protocol injection — a worker rewrite
    # short-circuits the pipeline before the origin gate.
    _worker_envelope = _ups_worker_lane_leg(runtime, payload, project_root)
    if _worker_envelope is not None:
        return _worker_envelope

    # ── ORIGIN GATE (origin-bound law) ────────────────────────
    # Built ONCE here, before ANY authority-bearing pipeline. ALWAYS-SAFE
    # steps already ran above (UPS audit, secret block, preflight,
    # worker-lane intercept). Everything below that grants, mutates,
    # confirms, or dispatches is gated on `_grant_eligible`.
    _grant_eligible, _origin_principal = _ups_origin_gate(
        payload, project_root, verified_grant_eligible, _why
    )

    # ── PROVENANCE FLOOR (WAR P Task 2, 2026-07-18) ───────────────
    # Intent/grant detectors evaluate ONLY operator-authored prompt
    # text. Harness-injected segments (task-notification, system-
    # reminder, SYSTEM NOTIFICATION, conductor-reply / tool-result
    # blocks) are stripped ONCE here — the single seam through which
    # UPS text reaches every authority-bearing detector below. Observed
    # FPs pinned by tests/host/test_delegation_intent_shape.py:
    # 'add bash.allowlist' minted from notification noise;
    # 'enable security.emit_decision_trace' minted from conductor-
    # reply/system text. Fail direction is DROP (§X drop-on-doubt): a
    # floor that cannot be computed mints NOTHING. The RAW prompt keeps
    # flowing to audit, secret-block, preflight, route classification
    # and context building — provenance never censors what the judge or
    # the agent sees, only what can MINT authority.
    try:
        from .canonical_intent_registry import strip_non_operator_text

        _operator_text = strip_non_operator_text(prompt)
    except Exception:
        _operator_text = ""  # cannot prove provenance => mint nothing

    from .prompt_submit_service import PromptSubmitMutationUnavailable

    def _tx(stage: str, position: str) -> bool:
        """Stage boundary. Returns False when the service signals that the
        protecting snapshot is unavailable — the MUTATION stage must then be
        skipped (fail-closed: no grant/flip/bind without a snapshot). Any
        other exception (injected faults, real stage errors) propagates so
        the service's rollback semantics stay intact."""
        if transaction_stage_hook is None:
            return True
        try:
            transaction_stage_hook(stage, position)
        except PromptSubmitMutationUnavailable:
            return False
        return True

    if phase_boundary is not None:
        phase_boundary("transaction", "before")

    # Confirmation-freeze resolver (#39) + operator chat-unfreeze +
    # sovereign soul gate. AUTHORITY-BEARING: the whole stage is
    # origin-gated inside the helper; every sub-stage MUTATES authority
    # state, so each runs only when the service confirms the protecting
    # snapshot (fail-closed skip via `_tx(...) is False`).
    _soul_dump_blocks: list = []
    _chat_unfreeze_blocks = _ups_freeze_soul_stage(
        runtime,
        project_root,
        payload,
        _operator_text,
        _grant_eligible,
        _tx,
        transaction_stage_hook,
    )

    # Trivial-prompt early-return REMOVED 2026-04-30 (operator
    # doctrine). The block previously here dropped short prompts
    # like "ok"/"yes"/"thanks"/"sure"/"👍" before any AIDOCS
    # pipeline ran — which left a security gap: an attacker could
    # fragment a malicious instruction across turns and use a
    # trivial reply as an unaudited cover turn (no grant detect,
    # no revoke detect, no destructive-intent stamp, no audit
    # emission). The original optimization (avoid context-build
    # cost on conversational filler) was added when AIDOCS managed
    # mode had bugs that made the agent unresponsive to short
    # prompts — those bugs were fixed long ago. Today's contract:
    # AIDOCS managed = pipeline runs on every prompt, period.
    #
    # Autowake fast-path also REMOVED 2026-04-30 along with the
    # rest of the force-wakeup feature — the heuristic detector
    # could not actually achieve its goal (agents could decline to
    # set ScheduleWakeup and stall the session waiting for an
    # autowake reset). See the autowake-removal commit for the
    # full rationale; reuse may revisit this via a stop-hook
    # architecture instead.

    # ── RBAC escalation scrub ─────────────────────────────────
    # AUTHORITY-BEARING (origin-gated inside the helper): strips
    # `approve:`/`deny:` credential lines and flips escalation rows
    # before grant-detection / route-classification; the provenance
    # floor is re-derived from the scrubbed prompt on a rewrite.
    prompt, _operator_text, _escalation_side_effects = _ups_escalation_scrub_stage(
        runtime,
        project_root,
        payload,
        prompt,
        _operator_text,
        _grant_eligible,
        _tx,
    )

    # Clear previous turn's user-intent tool grants on every new prompt,
    # then apply any grants implied by this prompt BEFORE running route
    # classification. Grants must not depend on downstream context
    # building, which can short-circuit and skip grant application.
    # Cache TTL: zero. The hook is a one-shot subprocess (`python -m
    # aidocs_mcp.claude_hook`) so every event spawns a fresh process
    # with no cross-invocation cache, and ManagedModeService.get_mode
    # reads sqlite on every call (no in-process memoization). A
    # set_mode write from the conductor is therefore visible on the
    # next hook invocation without any invalidation step. Pinned by
    # tests/host/test_claude_hook_managed_mode_cache.py.
    session_id = resolve_managed_session(
        runtime.hub.managed_mode,
        project_root,
        host_session_id=str(payload.get("session_id") or "").strip(),
    )

    # ── CAUSAL TURN MINT (#441, before SEC-001 snapshot) ──────────
    # Server-mints the causal turn id binding every subsequent tool
    # event to THIS operator instruction, until the next operator
    # turn. Gated on _should_mint_causal_turn: provenance-floored
    # operator text + origin-gate eligibility — injections, worker
    # prompts and harness interrupts continue the current turn.
    # Carve-out (like the UPS audit above): audit attribution, not
    # privilege state — current_turn_id is intentionally NOT in
    # _PRIVILEGE_COLUMNS, so a later route-validate block keeps the
    # minted turn and the block events attribute to it.
    if session_id and _should_mint_causal_turn(_operator_text, _grant_eligible):
        try:
            # #467: the accepted prompt becomes a first-class instruction
            # event — its sha256 travels with the mint so identity/integrity
            # are provable while the raw body never enters the causal store.
            import hashlib as _hashlib_turn

            runtime.hub.query_gate.rotate_current_turn_id(
                project_root,
                session_id,
                instruction_content_hash=(
                    _hashlib_turn.sha256(_operator_text.encode("utf-8")).hexdigest()
                    if _operator_text
                    else ""
                ),
                origin_channel="user_prompt_submit",
            )
        except Exception:
            # Best-effort: a failed mint leaves the previous turn id in
            # place (events keep attributing to the prior turn rather
            # than blocking the prompt). The intent-audit gate at the
            # tool chokepoint remains the fail-closed layer.
            pass

    # SEC-001 HOTFIX (2026-04-23): snapshot privilege state BEFORE
    # any mutation so we can restore it if route-validate blocks
    # the prompt. This is a temporary containment — the full fix
    # is the plan-before-apply refactor. Carve-outs
    # (user_prompt_received audit, check_and_update_cli_session_id)
    # already ran above and are NOT restored; they're defensive /
    # audit-chain signals that must fire on every prompt.
    _sec001_snapshot: dict[str, object] = {}
    if session_id:
        try:
            _sec001_snapshot = runtime.hub.query_gate.snapshot_privilege_state(
                project_root,
                session_id,
            )
        except Exception:
            _sec001_snapshot = {}

    # SEC-002 atomic privilege-mutation stage (2026-04-23), ORIGIN-BOUND
    # and session-bound — the whole grant cascade is one try/except with
    # snapshot rollback on failure. (Body: _ups_privilege_mutation_stage;
    # rollback: _ups_sec002_rollback.)
    _sticky_drop_blocks, _sec002_tripped = _ups_privilege_mutation_stage(
        runtime,
        project_root,
        session_id,
        _operator_text,
        _grant_eligible,
        _tx,
        transaction_stage_hook,
        _sec001_snapshot,
        _escalation_side_effects,
    )

    # DNT grants for UNMANAGED projects (operator repro 2026-06-11):
    # origin-gated literal parser with no session bound — grants land
    # under the '__unbound__' key. (Body: _ups_unbound_dnt_stage.)
    _ups_unbound_dnt_stage(
        runtime,
        project_root,
        session_id,
        _operator_text,
        _grant_eligible,
        _tx,
        transaction_stage_hook,
    )

    # Closed-vocabulary intent-phrase dispatch (origin-gated) — runs
    # before route classification so state changes are visible to
    # downstream context-building. (Body: _ups_intent_dispatch_stage.)
    intent_dispatch_results = _ups_intent_dispatch_stage(
        runtime,
        project_root,
        session_id,
        _operator_text,
        _grant_eligible,
        _tx,
    )

    # UPS consumes no freshness field from host_state (only session_id), so
    # skip the exact per-prompt SHA freshness walks: verify_index=False yields
    # an honest "unverified" index status. SessionStart and the
    # status/sync/diagnostic tools keep verify_index=True (exact). The former
    # request_config_scope() wrapper here existed solely to batch the per-UPS
    # config-read storm that the code-freshness walk produced; with the walk
    # skipped that storm is gone, so the wrapper is removed.
    host_state = runtime.host_state(
        project_root,
        prompt_text=prompt,
        verify_index=False,
        host_session_id=str(payload.get("session_id") or "").strip(),
    )
    prompt_state = (
        host_state.get("prompt_state")
        if isinstance(host_state.get("prompt_state"), dict)
        else {}
    )
    action_kind = str(prompt_state.get("action_kind") or "understand")
    _tx("route_classification", "before")
    route = runtime.aidocs_route_prompt(
        project_root,
        user_request=prompt,
        action_kind=action_kind,
        host_session_id=str(payload.get("session_id") or "").strip(),
    )
    _tx("route_classification", "after")

    # Managed-mode resolution: self-heal auto-bind, bug #234-2 no-op for
    # genuinely unmanaged projects, and the route-block gate — each block
    # path restores the SEC-001 snapshot first. (Body:
    # _ups_managed_resolution + _sec001_restore_and_audit.)
    route, _resolution_envelope = _ups_managed_resolution(
        runtime,
        project_root,
        payload,
        prompt,
        action_kind,
        route,
        session_id,
        _sec001_snapshot,
        _tx,
    )
    if _resolution_envelope is not None:
        return _resolution_envelope

    # Auto-task (friction removal; ORIGIN-BOUND task-lifecycle write).
    # (Body: _ups_auto_task_leg.)
    #
    # MOVED HERE 2026-08-03 (#746) from after the context build. It is the
    # LAST authority-MUTATION stage of the transaction, and every remaining
    # step (context build, notice blocks, operator_intent) is advisory and
    # touches none of the seven snapshotted stores. Running it here lets the
    # project-wide transaction lock close BEFORE the expensive advisory
    # surfacing instead of after it. Its note is returned separately so the
    # append ORDER of additional_context is unchanged (context -> notices ->
    # auto-task note -> operator_intent).
    _auto_task_note = _ups_auto_task_leg(
        runtime,
        project_root,
        session_id,
        prompt,
        "",
        _grant_eligible,
        _tx,
        transaction_stage_hook,
    )

    # ── TRANSACTION CLOSES HERE (#746) ────────────────────────────
    # WHY NOT AT THE END OF THE FUNCTION, WHERE IT USED TO BE. The lock is a
    # project-wide sqlite `BEGIN IMMEDIATE` mutex (PromptSubmitTransactionLock),
    # so its hold time is every OTHER conductor's wait time, and a waiter that
    # exceeds PROMPT_SUBMIT_LOCK_BUDGET_S (2.5s) gets SQLITE_BUSY -> "database
    # is locked" -> prompt_submit_transaction_degraded[transaction_lock]: the
    # operator loses the whole retrieval layer for that prompt.
    #
    # MEASURED on the live project, cold process (the hook is a one-shot
    # subprocess, so cold is the NORMAL case):
    #     host_state                 1063 ms  ) must stay serialized
    #     aidocs_route_prompt         180 ms  )
    #     lightweight_prompt_context 5265 ms  <- 81% of the critical section,
    #                                            reads only, mutates nothing
    # Holding 6.5s against a 2.5s wait budget guarantees that any overlapping
    # prompt degrades; the observed rate was 58 degrades / 573 UPS (10.1%) on
    # 2026-08-01. Closing here caps the hold at the ~1.2s serialized part.
    #
    # Everything below is READ/advisory: `operator_intent` is deliberately not
    # in MUTATION_STAGES, and the context build + notice blocks are not
    # transaction stages at all. Committing before them is also SAFER, not
    # just faster: an advisory failure can no longer roll back committed
    # authority (it takes the post-commit degraded exit instead).
    if phase_boundary is not None:
        phase_boundary("transaction", "after")

    # Trivial-prompt gate deleted 2026-04-24: it was suppressing the
    # NLP tool-surfacing hint on short prompts like "git pdf" even
    # when the prompt carried real tool intent. The hint is cheap
    # (frozenset lookup + optional fuzzy) and genuinely useful even
    # on 2-word prompts. If over-chatter re-emerges on pure
    # conversational noise, gate inside _build_lightweight_prompt_context
    # on tool-hint emptiness instead of on word count.
    additional_context = lightweight_prompt_context(runtime, host_kind, 
        action_kind=action_kind,
        route=route,
        project_root=project_root,
        host_state=host_state,
        prompt=prompt,
        cli_session_id=str(payload.get("session_id") or "").strip(),
    )

    # Strike-count visibility MOVED OFF UPS (operator directive 2026-07-15):
    # UPS additional_context must carry only genuine per-prompt UPS information,
    # not a strike note that re-fired on EVERY prompt while peak>0. Strikes now
    # surface on the NOTIFICATION rail (freeze_strike_notice_store, enqueued by
    # SecurityViolationService.record_and_escalate) — 5 displays then auto-drop,
    # plus the full strike trail in the blocked-tool error at freeze time.

    # Notice blocks: #198 one-shot preflight awareness note, super-admin
    # failsafe advisory, chat-unfreeze result, soul dumps — append order
    # preserved. (Body: _ups_context_notices.)
    additional_context = _ups_context_notices(
        runtime,
        project_root,
        session_id,
        additional_context,
        _preflight_failsafe_blocks,
        _chat_unfreeze_blocks,
        _soul_dump_blocks,
    )

    # Auto-task note, produced by the leg that now runs INSIDE the
    # transaction above (#746). Appended at the ORIGINAL position so the
    # rendered context is byte-identical to the pre-#746 order.
    if _auto_task_note:
        additional_context = (
            (additional_context or "")
            + ("\n\n" if additional_context else "")
            + _auto_task_note
        )

    # Operator-intent resolution (origin-gated, ADVISORY fail-open) —
    # runs AFTER the NLP grant/intent extraction above. (Body:
    # _ups_operator_intent_leg.) OUTSIDE the transaction since #746:
    # `operator_intent` is not a MUTATION_STAGE, so `_tx` here is a
    # no-op passthrough over the already-committed snapshot.
    _tx("operator_intent", "before")
    additional_context = _ups_operator_intent_leg(
        project_root,
        payload,
        prompt,
        _origin_principal,
        _grant_eligible,
        additional_context,
    )
    _tx("operator_intent", "after")

    # Intent-dispatch results piggy-back on the same context block
    # so the agent sees state-change acknowledgments ("Plan mode
    # active") in the same turn the operator's phrase fired. The
    # append happens AFTER lightweight_prompt_context so dispatch
    # outcomes appear after the standard managed-mode header.
    intent_context_parts = [
        str(r.get("context", "")).strip() for r in intent_dispatch_results if r.get("context")
    ]
    # #99 FIX1 (UX half): sticky sink-drop feedback rides the same context block
    # so the operator sees "sticky grant for `grep` was refused at the sink" in
    # the very turn the phrase fired.
    intent_context_parts.extend(b.strip() for b in _sticky_drop_blocks if b and b.strip())
    if intent_context_parts:
        additional_context = (additional_context or "") + " " + " ".join(intent_context_parts)

    # CC-only sub-pipelines composed by this canonical core (above)
    # Phase P begins after PromptSubmitService commits the authority
    # transaction. These two informational/advisory tails remain best-effort;
    # every supported host reaches them through this one canonical service path.
    # Hookless WebMCP/OpenAI callers reach the same path only by explicit routing;
    # no host-automatic UserPromptSubmit event is claimed.

    # Dashboard config advisory (ORIGIN-BOUND shape detector) +
    # notification drain (ALWAYS-SAFE). (Body: _ups_dashboard_drain_tail.)
    additional_context = _ups_dashboard_drain_tail(
        runtime,
        project_root,
        payload,
        prompt,
        _operator_text,
        _grant_eligible,
        _why,
        additional_context,
    )

    # ADVISE-only rails: update-intent durability (#219/#221) + durable
    # hint (#9) + loud NLP-liveness notice (#348) + intent code-target
    # enrichment (#448 Consumer B). (Body: _ups_advise_rails.)
    additional_context = _ups_advise_rails(
        runtime,
        project_root,
        prompt,
        _operator_text,
        additional_context,
    )

    if not additional_context:
        return None

    record_classification_event(runtime, project_root, action_kind, prompt)

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        },
    }

def run_user_prompt(
    runtime,
    project_root,
    payload,
    *,
    host_kind="claude_code",
    audit_source="claude_hook",
):
    """Single public caller: render the shared service result for Claude hooks."""
    from .prompt_submit_service import PromptSubmitService

    return PromptSubmitService(runtime).evaluate_submit(
        project_root,
        payload,
        host_kind=host_kind,
        audit_source=audit_source,
    ).to_claude_envelope()


def _post_universal_audit(runtime, project_root, payload, tool_name_raw):
    """Leg 1 — universal post-tool audit, delegated to LifecycleService
    (host-agnostic). Same audit shape every host writes; status detection
    (completed/failed) lives in the service.

    Extraction pin (#413 T2): verbatim from after_tool_use; runs FIRST,
    before any leg that can return an envelope."""
    try:
        _lane_id_post = current_lane_id(runtime, project_root)
    except Exception:
        _lane_id_post = None
    from .lifecycle_service import LifecycleService

    LifecycleService(runtime).on_post_tool_use_audit(
        tool_name=tool_name_raw,
        tool_input=payload.get("tool_input") or {},
        tool_response=payload.get("tool_response"),
        host_session_id=str(payload.get("session_id") or ""),
        project_root=project_root,
        payload=payload,
        lane_id=_lane_id_post,
        # Dedup (UPS/PostToolUse sqlite seal, 2026-06-02): this Claude path
        # runs its OWN surface_on_edit below (goggles leg) and returns that
        # envelope; the audit call's downstream goggles were computed and
        # DISCARDED here (return value unused). Skip the duplicate goggles —
        # the audit event itself still fires. Other hosts (CLI / openai
        # adapter) keep surface_downstream=True since they consume it.
        surface_downstream=False,
    )


def _post_update_intent_satisfier(runtime, project_root, payload, tool_name_raw):
    """Leg 2 — update-intent durability satisfier (#219/#221 PR-1).
    A SUCCESSFUL durable write (ai_backlog add|update, ai_task todo
    add|update, ai_plan_create/expand, memory_capture) satisfies the
    session's pending update-intent rows. Best-effort: never blocks the
    tool. Extraction pin (#413 T2): verbatim from after_tool_use."""
    try:
        _ui_session_post = resolve_managed_session(
            runtime.hub.managed_mode, project_root
        )
        if _ui_session_post:
            from .update_intent_hook import observe_tool_result as _ui_observe

            _ui_observe(
                project_root,
                _ui_session_post,
                tool_name_raw,
                payload.get("tool_input") or {},
                payload.get("tool_response"),
            )
    except Exception:
        pass


def _post_is_native_shell(tool_name_raw):
    """Native-shell transport detection for the PostToolUse receipt leg.

    Extraction pin (#413 T2): verbatim from after_tool_use. Primary path
    is shell_envelope.detect_provider_and_transport; when detection itself
    throws for a possibly-native tool, the literal basename fallback keeps
    the receipt/guard leg armed (a broken import must never exempt a
    native shell from output proof)."""
    try:
        from .shell_envelope import (
            TRANSPORT_HOST_NATIVE,
            detect_provider_and_transport,
        )

        _, _post_transport = detect_provider_and_transport(tool_name_raw)
        return _post_transport == TRANSPORT_HOST_NATIVE
    except Exception:
        # Detection threw for a possibly-native tool — literal fallback.
        _bn = (tool_name_raw or "").strip().lower()
        for _pre in ("mcp__aidocs__", "mcp__"):
            _bn = _bn.removeprefix(_pre)
        _bn = _bn.removesuffix(".exe")
        return _bn in (
            "bash",
            "sh",
            "zsh",
            "wsl",
            "powershell",
            "pwsh",
            "cmd",
        )


def _post_native_withhold_envelope(project_root, _wh_resp):
    """SHAPE-PRESERVING withhold (2026-07-11): Claude Code validates
    updatedToolOutput against the tool's output schema (Bash = an
    object). A bare-string withhold is REJECTED by the host — it
    warns ("PostToolUse:Bash hook warning") and falls back to the
    ORIGINAL raw output, defeating the fail-closed intent. Built
    INLINE (not via shell_receipt) because the failure being
    handled may BE the shell_receipt import.

    Extraction pin (#413 T2): verbatim from after_tool_use — the sole
    fail-closed case (guard unavailable, unknown != clean)."""
    _wh_notice = (
        "[AIDOCS: native receipt/output guard failed; output withheld "
        "— guard unavailable, unknown != clean]"
    )
    try:
        from .tool_gate_service import false_positive_affordance

        _wh_notice += "\n" + false_positive_affordance(
            "shell_receipt.native_output_withheld",
            project_root=project_root,
        )
    except Exception:
        pass

    def _wh_blank(node):
        if isinstance(node, str):
            return ""
        if isinstance(node, dict):
            return {k: _wh_blank(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_wh_blank(v) for v in node]
        return node

    if isinstance(_wh_resp, dict):
        _wh_out = {k: _wh_blank(v) for k, v in _wh_resp.items()}
        if isinstance(_wh_out.get("stdout"), str):
            _wh_out["stdout"] = _wh_notice
        else:
            for _wk, _wv in _wh_out.items():
                if isinstance(_wv, str):
                    _wh_out[_wk] = _wh_notice
                    break
            else:
                _wh_out["stdout"] = _wh_notice
    else:
        _wh_out = _wh_notice
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": _wh_out,
        },
    }


def _post_native_receipt_degraded(runtime, project_root, payload, tool_name_raw):
    """Receipt/guard crashed for a native shell output → fail closed:
    withhold the raw output, record the failure.

    Extraction pin (#413 T2): verbatim from after_tool_use's except
    branch; cascade preserved — (1) record shell_native_receipt_failed,
    (2) #372 (WAR U) REDACT-NOT-WITHHOLD: the receipt plumbing failed,
    but the OUTPUT GUARD may still be healthy — the failure being
    handled is usually shell_receipt's import/correlation, not the
    scanner. Blanking the whole payload made the agent blind to its
    own deploy/test logs (operator finding 2026-07-13). So: run the
    guard DIRECTLY as a fallback. Redaction proven → deliver the
    redacted output (secret spans masked, prose kept). (3) Only when
    the guard itself cannot run does the shape-preserving withhold
    fire (unknown != clean — that is the sole fail-closed case)."""
    try:
        runtime.hub.execution.record_event(
            project_root,
            event_kind="shell_native_receipt_failed",
            source_kind="post_tool_use",
            session_id=str(payload.get("session_id") or ""),
            capability_name=tool_name_raw,
            action_kind="receipt",
            status="degraded",
            payload={"tool_name": tool_name_raw, "host": "claude_code"},
        )
    except Exception:
        pass
    _wh_resp = payload.get("tool_response")
    try:
        from .output_guard import redact_tool_response

        _rd_out, _rd_count, _rd_cats = redact_tool_response(
            _wh_resp, redact=True
        )
        _rd_note = (
            "[AIDOCS: native receipt degraded; output guard fallback "
            f"applied ({int(_rd_count)} redaction(s): "
            f"{', '.join(sorted(_rd_cats)) or 'none'})]"
        )
        if isinstance(_rd_out, dict):
            for _rk, _rv in _rd_out.items():
                if isinstance(_rv, str):
                    _rd_out[_rk] = _rv + (
                        ("\n" + _rd_note) if _rk == "stdout" else ""
                    )
        elif isinstance(_rd_out, str):
            _rd_out = _rd_out + "\n" + _rd_note
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": _rd_out,
            },
        }
    except Exception:
        pass  # guard unavailable → fail-closed withhold below
    return _post_native_withhold_envelope(project_root, _wh_resp)


def _post_native_shell_leg(runtime, project_root, payload, tool_name_raw):
    """Leg 3 — Batch 2.0-B0.1: native pilot completion receipt + output
    proof. For host-native shell outputs the receipt/output-guard is NOT
    best-effort: if it raises (or its import fails), raw native output
    must NOT fall through — fail CLOSED by withholding the output.

    Extraction pin (#413 T2): verbatim from after_tool_use. Returns the
    receipt / guard-fallback / withhold envelope; None means the receipt
    yielded nothing and the cascade falls through to the redaction leg."""
    try:
        from .shell_receipt import native_post_receipt

        _receipt = native_post_receipt(
            project_root,
            runtime,
            host="claude_code",
            tool_name=tool_name_raw,
            tool_input=payload.get("tool_input") or {},
            tool_response=payload.get("tool_response"),
            host_session_id=str(payload.get("session_id") or ""),
            tool_use_id=str(payload.get("tool_use_id") or ""),
        )
    except Exception:
        return _post_native_receipt_degraded(
            runtime, project_root, payload, tool_name_raw
        )
    return _receipt


def _post_todo_bridge(runtime, project_root, payload, tool_name):
    """Leg 6 — TodoWrite bridge → task lifecycle dispatch, delegated to
    LifecycleService.dispatch_todo_lifecycle.

    Extraction pin (#413 T2): verbatim from after_tool_use; always
    returns None (the bridge never produces an envelope)."""
    if tool_name not in ("todowrite", "todoread"):
        return None
    if tool_name == "todoread":
        return None
    try:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        todos = tool_input.get("todos")
        if not isinstance(todos, list):
            return None
    except Exception:
        return None
    try:
        sid = resolve_managed_session(runtime.hub.managed_mode, project_root)
    except Exception:
        return None
    from .lifecycle_service import LifecycleService

    LifecycleService(runtime).dispatch_todo_lifecycle(
        todos=todos,
        managed_session_id=sid,
        project_root=project_root,
    )
    return None


def after_tool_use(
    runtime,
    project_root,
    payload,
    *,
    host_kind="claude_code",
    redact_output=None,
):
    """PostToolUse core (S5 rip, #251): universal audit + TodoWrite bridge.
    ``redact_output`` is the HOST seam — the adapter passes its
    shape-preserving redactor (CC: updatedToolOutput envelope); None
    disables redaction for hosts that cannot redact.

    Cascade order (#413 T2 pin — behavior byte-identical to the pre-split
    body; each leg's internals live in the _post_* helpers above):
      1. universal audit                (_post_universal_audit)
      2. update-intent satisfier        (_post_update_intent_satisfier)
      3. native-shell receipt/guard     (_post_native_shell_leg — first
         leg that can return an envelope; non-native tools skip entirely)
      4. host output secret-redaction   (redact_output host seam)
      5. post-edit downstream goggles   (ReadMemorySurfacer)
      6. TodoWrite bridge               (_post_todo_bridge → None)
    """
    tool_name_raw = str(payload.get("tool_name") or "").strip()

    _post_universal_audit(runtime, project_root, payload, tool_name_raw)
    _post_update_intent_satisfier(runtime, project_root, payload, tool_name_raw)

    if _post_is_native_shell(tool_name_raw):
        _native_env = _post_native_shell_leg(
            runtime, project_root, payload, tool_name_raw
        )
        if _native_env is not None:
            return _native_env

    # Host Read output secret-redaction (one-law goal 2026-05-20).
    # Claude Code's PostToolUse supports hookSpecificOutput.
    # updatedToolOutput, which REPLACES the tool result before it
    # enters model context. A SAFE read path whose bytes happen to
    # contain a credential is the case PreToolUse path-blocking can't
    # catch — so scan the Read response and, if a secret is found,
    # return a SHAPE-PRESERVING redacted updatedToolOutput. The secret
    # never appears in additionalContext (only the redacted output is
    # returned). Capability-gated: only fires because claude_code is
    # registered can_redact_tool_output_before_context=True.
    _read_redacted = redact_output and redact_output(project_root, payload)
    if _read_redacted is not None:
        return _read_redacted

    # Post-edit downstream goggles — delegated to ReadMemorySurfacer.
    # Returns hookSpecificOutput envelope ONLY when there are hints
    # to surface; otherwise falls through to the TodoWrite branch
    # (and ultimately None) preserving existing behavior.
    from .read_memory_surfacer import ReadMemorySurfacer

    tool_input_post = payload.get("tool_input") or {}
    _downstream = ReadMemorySurfacer(runtime).surface_on_edit(
        tool_name=tool_name_raw,
        tool_input=tool_input_post,
        project_root=project_root,
    )
    if _downstream.hint_count > 0:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n".join(_downstream.advisory_lines),
            },
        }

    return _post_todo_bridge(runtime, project_root, payload, tool_name_raw.lower())


def run_aidocs_command(runtime, payload, *, host_kind="claude_code"):
    """/aidocs command core (S6 rip, #251) — verbatim relocation."""
    cwd = str(payload.get("cwd") or "").strip()
    project_root = Path(cwd).resolve() if cwd else None

    # ── /aidocs is ADMIN-ONLY ──────────────────────────────────
    # Adoption/commissioning binds governance to a project — a
    # privilege act. EVERY flavor requires an authenticated
    # operator holding the manage-config grant (#404: no
    # local-admin passthrough). A non-admin user must not be able
    # to adopt a tree or activate managed mode (self-escalation /
    # confused-deputy).
    if project_root is not None:
        # FAIL-CLOSED: any error evaluating the admin gate refuses
        # /aidocs (no silent proceed on an unevaluable check).
        _adm_reason = ""
        try:
            from .project_authority import require_admin

            _adm = require_admin(
                project_root,
                operation="aidocs_command",
                host_session_id=str(payload.get("session_id") or "").strip(),
            )
            _adm_ok = bool(_adm.get("ok"))
            _adm_reason = str(_adm.get("reason") or "")
        except Exception as _exc:
            _adm_ok = False
            _adm_reason = f"authorization check failed: {_exc!r}"
        if not _adm_ok:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "`/aidocs` is admin-only. "
                        + _adm_reason
                        + " Adoption/commissioning and managed-mode "
                        "activation require an authenticated operator "
                        "with the admin grant; no change was made."
                    ),
                },
            }

    memory_exists = bool(project_root and (project_root / ".MEMORY").is_dir())
    from .mcp_server_runtime_helpers import is_aidocs_managed

    aidocs_marker = bool(project_root and is_aidocs_managed(project_root))

    # MCP injection. Idempotent — no-ops if entry already correct.
    # Surface failures inline so the operator can fix `.mcp.json`
    # by hand if the runtime call breaks.
    mcp_inject_note = ""
    if project_root is not None:
        try:
            mcp_result = runtime.ensure_claude_mcp_config(project_root)
            action = str((mcp_result or {}).get("action") or "")
            if action in ("created", "updated"):
                mcp_inject_note = (
                    f"AIDOCS MCP entry was {action} in `.mcp.json`. "
                    "Run `/mcp` in Claude Code (or restart) to load "
                    "the aidocs server before invoking `project_init` "
                    "/ `project_bootstrap_or_resume`. "
                )
        except Exception as exc:  # noqa: BLE001 — surface, not crash.
            mcp_inject_note = (
                f"Could not ensure `.mcp.json`: "
                f"{type(exc).__name__}: {exc}. "
                "Manual update may be required before bootstrap. "
            )

    # Record DELIBERATE first adoption — running /aidocs IS the
    # operator's intent. Skipped for the foreign-`.MEMORY` case
    # (memory present, no marker), which still requires explicit
    # confirmation below before AIDOCS touches the tree. Adoption is
    # SQLite-only (creates no files); the actual infrastructure is
    # created by bootstrap / UPS auto-repair afterwards.
    if project_root is not None and not (memory_exists and not aidocs_marker):
        try:
            from .project_commission import adopt

            adopt(project_root, source="aidocs_cmd")
        except Exception:
            pass

    if aidocs_marker:
        context = (
            mcp_inject_note
            + "AIDOCS entry command detected. Use the MCP bootstrap/orchestrator flow for this project, "
            "report selected session and managed-mode state, and avoid broad repo reads before session routing completes. "
            "If multiple candidate sessions exist, STOP and ask the user which to bind."
        )
    elif memory_exists:
        # Foreign `.MEMORY/` — another tool owns it. Do NOT auto-init.
        context = (
            mcp_inject_note + f"AIDOCS entry command detected on `{project_root}` — "
            "`.MEMORY/` exists but is NOT marked as AIDOCS-managed "
            "(no `.MEMORY/.aidocs/index.aidocs`). This usually means "
            "another tool owns the `.MEMORY/` directory (e.g. a "
            "memory-system project). STOP and ASK the operator "
            "before running `project_init` — auto-bootstrap would "
            "mix two memory systems in one tree. If the operator "
            "confirms AIDOCS should manage this project, proceed with "
            "`project_init` then `project_bootstrap_or_resume`. "
            "Otherwise leave the existing `.MEMORY/` alone."
        )
    elif project_root is None:
        context = (
            "AIDOCS entry command detected, but no project root was provided by the host. "
            "Ask the user for the project path before initializing."
        )
    else:
        context = (
            mcp_inject_note
            + f"AIDOCS entry command detected on `{project_root}` — this project has no `.MEMORY/` yet. "
            "Call the `project_init` MCP tool with this root to create .MEMORY/, AGENTS.md/CLAUDE.md, and AIDOCS templates. "
            "Then call `project_bootstrap_or_resume` to activate managed mode. "
            "Do not begin other work until initialization and bootstrap succeed."
        )

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        },
    }


def on_session_start(
    runtime,
    project_root,
    payload=None,
    *,
    host_kind="claude_code",
):
    """SessionStart core plus host-session model/window capture (#620)."""
    import os as _os_env_ws

    from .lifecycle_service import LifecycleService

    payload_dict = payload if isinstance(payload, dict) else {}
    host_session_id = str(payload_dict.get("session_id") or "").strip()
    try:
        from .host_session_context_store import HostSessionContextStore

        HostSessionContextStore().record_profile(
            project_root,
            host_session_id=host_session_id,
            host_kind=host_kind,
            model_id=str(payload_dict.get("model") or "").strip(),
            context_window=(
                payload_dict.get("context_window")
                or payload_dict.get("model_context_window")
                or 0
            ),
        )
    except Exception:
        # Context sizing is a surfacing optimization, never a SessionStart
        # availability dependency.
        pass

    # #876 phase 1: record WINDOW -> CONVERSATION. SessionStart is the one
    # moment the HOST states the current conversation in a payload it writes
    # fresh every firing, which makes it the one moment this mapping can be
    # recorded from evidence rather than from a cache that has already gone
    # stale (`/resume` rotates the conversation without respawning the shim).
    #
    # THE WINDOW IS READ OFF THE PAYLOAD, NEVER DERIVED HERE, and that is not a
    # style choice. `claude_hook` asks the resident broker first, and the broker
    # is hosted by the WATCHDOG (hook_broker.py:11) -- so this function usually
    # executes in a process that is NOT in the calling window. Worse than
    # useless: `aidocs service start` is routinely run from a Claude Code
    # window's Bash, so an ancestry walk here could SUCCEED and name whichever
    # window started the daemon, for every SessionStart from every window. The
    # derivation happens once, in the hook process Claude Code spawned
    # (`window_key.stamp_payload_window`), and travels.
    #
    # ADDITIVE: nothing reads the row it writes. That is phase 2 (#880).
    try:
        from .window_binding_store import record_session_start_window

        # ── WHO, STAMPED AT CREATION (operator ruling 2026-09-04) ─────
        #
        # THE ONLY CREATION PATH THAT CAN NAME ITS USER TODAY. SessionStart
        # fires for a window the human started ON THIS BOX, so the machine
        # login IS this window's operator here. It is NOT a general default:
        # the gate's web flow and dashboard-command spawns never reach this
        # function (they synthesise an `ogh_`/lane session and fire no hook),
        # so they cannot pick up this stamp -- which is the point. Defaulting
        # a REMOTE window to the machine login would silently make every
        # remote caller the local operator.
        #
        # NOBODY SIGNED IN IS AN HONEST EMPTY. resolve_machine_login answers
        # "" when no live token exists, and the store writes no stamp for it:
        # the window binds as UNAUTHENTICATED and operator-intent refuses
        # naming the missing WHO, rather than borrowing an identity.
        #
        # THE STAMP IS ONCE, THE STANDING IS LIVE: this records WHICH user,
        # and every later resolution re-checks that the user still exists and
        # is still enabled, so a sign-out or a disable propagates at once.
        _who = ""
        try:
            from .operator_auth_service import OperatorAuthService

            _who = OperatorAuthService().resolve_machine_login(project_root) or ""
        except Exception:
            _who = ""

        record_session_start_window(
            project_root,
            payload_dict,
            host_session_id=host_session_id,
            host_kind=host_kind,
            bound_user_id=_who,
            bound_via="machine_login" if _who else "",
        )
        # #880 lease lifecycle: reap windows that are provably gone, HERE.
        #
        # A boot-only reap is a reap that does not happen. This daemon had been
        # up ~18 hours when the operator asked "windows might die mid-session,
        # and the daemon doesn't restart - so they are never pruned?" -- and he
        # was right.
        #
        # NOT COSMETIC: #892 made "this id holds a lease" the proof that a
        # conductor binding is LIVE, so a dead window whose row survives keeps
        # its binding alive forever. Two of those and `correlate_host_session`
        # refuses -- #599/#787's lockout returning through the door the lease
        # was meant to close.
        #
        # SessionStart is the right trigger: it fires on every startup, resume
        # and compact, it already writes this table, and it fires immediately
        # BEFORE the moment staleness bites, because the correlation a stale row
        # breaks happens when a window arrives.
        #
        # ── SEAT REAP FIRST, WINDOW REAP SECOND ───────────────────────────
        #
        # THE ORDER IS LOAD-BEARING AND IT IS THE OPPOSITE OF THE OBVIOUS ONE.
        # `msg_role_map` stores a CONVERSATION, not a window, so the seat
        # reaper's only route from a seat to a pid is the lease row that names
        # that conversation. Reap the windows first and every seat the seat
        # reaper would have graded provably-DEAD becomes UNPROVABLE instead --
        # and an unprovable seat is kept forever, by design. The window reap
        # would silently destroy the evidence the seat reap depends on.
        #
        # ── ...AND THE HEAL LAST (#919, operator 2026-08-26) ──────────────
        #
        # A window that LOST its conversation to the one-conversation-one-window
        # release gets it back here, if it is provably alive and nothing else
        # holds that conversation. It runs THIRD because the window reap above
        # is what deletes a dead usurper's row -- and that deletion is what frees
        # the conversation to give back. Healing before the reap would find it
        # still held and correctly decline, which is a no-op, not a bug, but it
        # would never repair the case that actually occurs.
        #
        # Measured on the operator's box 2026-08-25: a live window sat
        # identity-less while the conversation it was still running was held by
        # nobody, because `claude --continue` had spawned a copy that took the
        # claim and then closed.
        from .conductor_comms import reap_dead_seats_on_session_start
        from .window_binding_store import (
            heal_released_windows_on_session_start,
            reap_dead_windows_on_session_start,
        )

        reap_dead_seats_on_session_start(project_root)
        reap_dead_windows_on_session_start(project_root)
        heal_released_windows_on_session_start(project_root)
        # #1007: the CONDUCTOR's own xaacp_actors row, established where the
        # host announces the conversation. A subagent never fires SessionStart
        # (measured), so this can only ever name the window's main thread. No
        # managed binding yet ⇒ no row; the first XAACP call registers it.
        from .conductor_comms import xaacp_register_host_actor

        xaacp_register_host_actor(
            project_root,
            host_session_id=host_session_id,
            host_kind=host_kind,
            actor_kind="conductor",
            source="session_start",
        )
    except Exception:
        # Same posture as the profile capture above: a diagnostic write must
        # never be able to refuse a session start.
        pass

    context = LifecycleService(runtime).build_session_start_context(
        host_kind=host_kind,
        host_session_id=host_session_id,
        project_root=project_root,
        is_worker_proc=bool(
            _os_env_ws.environ.get(
                "AIDOCS_EXPERT_LANE_ID",
                "",
            ).strip(),
        ),
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
    }
