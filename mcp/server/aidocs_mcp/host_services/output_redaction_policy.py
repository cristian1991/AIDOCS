"""Decide whether a tool result should be redacted before reaching the
model, and produce the host-specific envelope that delivers the redacted
output. Lifted from claude_hook.py 2026-05-27 (Phase 2).

This service answers: "given a tool result, does it need redaction?"
The HOST adapter (claude_hook, opencode plugin) is still responsible
for wrapping the redacted result in the host's envelope shape — but
the policy decision (is this tool eligible, did the host_capabilities
declare pre-context redaction support, did the output_guard find
anything to redact) is host-agnostic and lives here.

Eligible tools — host-name-stripped, lowercased:
  read / Read              → use LifecycleService.on_host_read_output
                             (returns a structured replacement)
  bash / monitor / run     → use output_guard.redact_tool_response
                             (regex-scrub categorized secrets)

The Claude Code post-tool-use envelope shape (`hookSpecificOutput`
with `updatedToolOutput`) is built by the caller — this service hands
back the redacted *content*, not the envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..output_scan_scope import REDACTABLE_OUTPUT_TOOLS, OutputScanState
from ..output_scan_scope import is_redactable_tool as _scope_is_redactable
from ..output_scan_scope import normalize_tool_name as _scope_normalize

# Tools whose output is in scope for pre-context redaction. #1136 F5: this is
# the ONE canonical set (output_scan_scope), the same object hook_pipeline
# uses — never a private copy. Lowercased + host-prefix-stripped.
_REDACTABLE_OUTPUT_TOOLS: frozenset[str] = REDACTABLE_OUTPUT_TOOLS


@dataclass(slots=True)
class RedactionResult:
    """What the policy decided about a single tool result.

    redacted:          the redaction-applied tool response (same type/shape
                       as the original tool_response, with categorized
                       tokens replaced); None unless state is REDACTED
    redaction_count:   number of replacements made (0 if nothing matched)
    categories:        names of the secret categories that matched
                       (aws_access_key, openai_token, github_pat, …)
    mechanism:         "lifecycle_read" | "output_guard_regex" — which
                       backend produced the result; useful for the audit
                       row the host writes after applying the redaction
    state:             #1136 F2 — the explicit verdict. CLEAN and UNKNOWN
                       both carry ``redacted=None`` and MUST NOT be read
                       alike: UNKNOWN means the output could not be
                       certified and has to be withheld.
    """

    redacted: object | None
    redaction_count: int
    categories: tuple[str, ...]
    mechanism: str
    state: OutputScanState = OutputScanState.REDACTED


def is_redactable_tool(tool_name: str) -> bool:
    """True iff a tool with this name should have its output evaluated
    for redaction. Strips host prefixes (`mcp__aidocs__`, `mcp__`) and
    lowercases before checking (the canonical scope's rule).
    """
    return _scope_is_redactable(tool_name)


def normalize_tool_name(tool_name: str) -> str:
    """Return the host-prefix-stripped, lowercased tool name. Useful
    for callers that need the canonical name (e.g. to dispatch on).
    """
    return _scope_normalize(tool_name)


def _verdict(state: OutputScanState, mechanism: str) -> RedactionResult:
    return RedactionResult(
        redacted=None,
        redaction_count=0,
        categories=(),
        mechanism=mechanism,
        state=state,
    )


def evaluate_read_output(
    *,
    runtime,
    project_root: Path,
    path: str,
    text_view: str,
    host_session_id: str,
    host_kind: str = "claude_code",
    result_obj: object | None = None,
) -> RedactionResult:
    """Evaluate a Read tool's output against the lifecycle service.

    Always returns a verdict (#1136 F2 — no ``None`` collapse):
      CLEAN           certified, deliver the original
      REDACTED        ``redacted`` is the shape-preserving replacement
      NOT_APPLICABLE  the lifecycle says the host cannot replace output
      UNKNOWN         the lifecycle raised, reported a scan error, or claimed a
                      redaction without a replacement — the caller WITHHOLDS
    """
    try:
        from ..lifecycle_service import LifecycleService

        lc = LifecycleService(runtime).on_host_read_output(
            tool_name="Read",
            path=path,
            result_text=text_view,
            host_session_id=host_session_id,
            host_kind=host_kind,
            project_root=project_root,
            result_obj=result_obj,
        )
    except Exception:
        return _verdict(OutputScanState.UNKNOWN, "lifecycle_read")
    try:
        state = OutputScanState(lc.output_scan_state)
    except (TypeError, ValueError):
        # A verdict the lifecycle did not state is not a certification.
        return _verdict(OutputScanState.UNKNOWN, "lifecycle_read")
    if state is not OutputScanState.REDACTED:
        return _verdict(state, "lifecycle_read")
    if lc.redacted_response is None:
        return _verdict(OutputScanState.UNKNOWN, "lifecycle_read")
    count = 0
    categories: tuple[str, ...] = ()
    for kind, data in lc.audit_events:
        if kind == "host_read_output_redacted":
            count = int(data.get("redaction_count") or 0)
            categories = tuple(data.get("categories") or ())
    return RedactionResult(
        redacted=lc.redacted_response,
        redaction_count=count,
        categories=categories,
        mechanism="lifecycle_read",
        state=OutputScanState.REDACTED,
    )


def evaluate_command_output(tool_response: object) -> RedactionResult:
    """Evaluate a bash/monitor/run tool result via the regex output_guard.

    CLEAN when nothing matched, REDACTED with the scrubbed copy, UNKNOWN when
    the scan could not run (never collapsed into "nothing to redact").
    """
    try:
        from ..output_guard import redact_tool_response

        redacted, count, categories = redact_tool_response(
            tool_response,
            redact=True,
        )
        count = int(count or 0)
    except Exception:
        return _verdict(OutputScanState.UNKNOWN, "output_guard_regex")
    if count == 0:
        return _verdict(OutputScanState.CLEAN, "output_guard_regex")
    return RedactionResult(
        redacted=redacted,
        redaction_count=count,
        categories=tuple(categories),
        mechanism="output_guard_regex",
        state=OutputScanState.REDACTED,
    )
