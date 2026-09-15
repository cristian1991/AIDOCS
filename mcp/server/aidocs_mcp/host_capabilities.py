"""Host capability registry.

Different hosts expose different hook contracts. Some can MUTATE a tool's
result before it enters the model's context window; others can only append
advisory context for the NEXT turn, and some can only REPLACE the result
with feedback text (not a shape-preserving redaction). Output-secret
REDACTION is only an honest claim where the host can replace the result in
place before the model reads it — claiming it elsewhere is dishonest.

Research findings encoded here (2026-05-20, current host docs):

  - Claude Code PostToolUse: supports
    ``hookSpecificOutput.updatedToolOutput``, which "replaces the tool's
    output with the provided value before it is sent to Claude". The
    replacement must match the tool's output shape. Side effects /
    telemetry already happened (this is CONTEXT redaction only, not
    side-effect prevention). → CAN pre-context redact = TRUE, via
    updatedToolOutput.

  - OpenCode ``tool.execute.after(input, output)``: current plugin docs
    show ``tool.execute.before`` can mutate args and block reads, and
    ``tool.execute.after`` exists, but NO documented stable
    result-replacement field. → CANNOT pre-context redact (fail closed).
    Path blocking is the real protection.

  - Codex CLI PostToolUse: ``decision:"block"`` or ``continue:false``
    replaces the original tool result with FEEDBACK / stop text for the
    documented supported tools (Bash, apply_patch, MCP tools).
    ``updatedMCPToolOutput`` and ``suppressOutput`` are parsed but NOT
    supported. → CANNOT do shape-preserving redaction; CAN replace with
    feedback (so a secret can be SUPPRESSED, but not a clean redacted
    copy returned). Tracked separately as
    ``can_replace_posttool_with_feedback``.

  - OpenAI Agents SDK: ``on_tool_end(..., result: str) -> None`` is an
    observer callback, not a mutator. → lifecycle hooks CANNOT redact.
    But an AIDOCS-OWNED ``FunctionTool`` returns its own output, so it
    redacts internally via ``output_guard`` before returning (tracked as
    ``aidocs_owned_tool_can_redact``, host-independent).

Default for pre-context redaction is FALSE (fail closed): a host gets
credit only when explicitly registered True.
"""

from __future__ import annotations

# host_kind -> can it replace/mutate a tool result with a SHAPE-PRESERVING
# redacted copy BEFORE it enters the model's context window? Only True
# hosts may "read then redact" output and claim protection.
_CAN_REDACT_BEFORE_CONTEXT: dict[str, bool] = {
    "claude_code": True,  # PostToolUse hookSpecificOutput.updatedToolOutput
    "claude": True,
    "cc": True,
    "opencode": False,  # no documented result-replacement field
    "openai_agents": False,  # on_tool_end is observer-only
    "openai": False,
    "host_adapter_cli": False,
}

# host_kind -> can it REPLACE a PostToolUse result with FEEDBACK / stop
# text (suppressing the original) even though it cannot return a
# shape-preserving redacted copy? Codex documents this via
# decision:"block" / continue:false for supported tools.
_CAN_REPLACE_WITH_FEEDBACK: dict[str, bool] = {
    "codex": True,
    "codex_cli": True,
    "claude_code": True,  # decision:"block" path also suppresses
    "claude": True,
    "cc": True,
}

# Mechanism string per host, for honest audit/telemetry.
_REDACTION_MECHANISM: dict[str, str] = {
    "claude_code": "posttooluse.updatedToolOutput",
    "claude": "posttooluse.updatedToolOutput",
    "cc": "posttooluse.updatedToolOutput",
    "codex": "posttooluse.feedback_replacement",
    "codex_cli": "posttooluse.feedback_replacement",
}


def can_redact_tool_output_before_context(host_kind: str | None) -> bool:
    """True iff ``host_kind`` can replace a tool result with a
    shape-preserving redacted copy before it reaches model context.
    Unknown / unset hosts fail closed (False).
    """
    if not host_kind:
        return False
    return _CAN_REDACT_BEFORE_CONTEXT.get(str(host_kind).strip().lower(), False)


def can_replace_posttool_with_feedback(host_kind: str | None) -> bool:
    """True iff ``host_kind`` can suppress/replace a PostToolUse result
    with feedback text (Codex decision:block / continue:false). This is
    NOT shape-preserving redaction — it withholds, it does not return a
    clean redacted copy. Unknown hosts fail closed (False).
    """
    if not host_kind:
        return False
    return _CAN_REPLACE_WITH_FEEDBACK.get(str(host_kind).strip().lower(), False)


def redaction_mechanism(host_kind: str | None) -> str:
    """Human/audit string describing HOW a host redacts, or '' if it
    cannot. Used in audit payloads so the ledger records the truth.
    """
    if not host_kind:
        return ""
    return _REDACTION_MECHANISM.get(str(host_kind).strip().lower(), "")


def register_host_capability(
    host_kind: str,
    *,
    can_redact_before_context: bool,
    can_replace_with_feedback: bool | None = None,
    mechanism: str | None = None,
) -> None:
    """Register/override a host's capabilities. Used by an adapter that
    genuinely supports result replacement (and by tests for synthetic
    hosts).
    """
    key = str(host_kind).strip().lower()
    _CAN_REDACT_BEFORE_CONTEXT[key] = bool(can_redact_before_context)
    if can_replace_with_feedback is not None:
        _CAN_REPLACE_WITH_FEEDBACK[key] = bool(can_replace_with_feedback)
    if mechanism is not None:
        _REDACTION_MECHANISM[key] = str(mechanism)
