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

# Tools whose output is in scope for pre-context redaction. Same set
# claude_hook._REDACTABLE_OUTPUT_TOOLS used to enumerate inline.
# Lowercased + host-prefix-stripped (mcp__aidocs__ai_run → run).
_REDACTABLE_OUTPUT_TOOLS: frozenset[str] = frozenset(
    {
        "read",
        "bash",
        "monitor",
        "run",
    },
)


@dataclass(slots=True)
class RedactionResult:
    """What the policy decided about a single tool result.

    redacted:          the redaction-applied tool response (same type/shape
                       as the original tool_response, with categorized
                       tokens replaced)
    redaction_count:   number of replacements made (0 if nothing matched)
    categories:        names of the secret categories that matched
                       (aws_access_key, openai_token, github_pat, …)
    mechanism:         "lifecycle_read" | "output_guard_regex" — which
                       backend produced the result; useful for the audit
                       row the host writes after applying the redaction
    """

    redacted: object | None
    redaction_count: int
    categories: tuple[str, ...]
    mechanism: str


def is_redactable_tool(tool_name: str) -> bool:
    """True iff a tool with this name should have its output evaluated
    for redaction. Strips host prefixes (`mcp__aidocs__`, `mcp__`) and
    lowercases before checking.
    """
    name = (tool_name or "").strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name in _REDACTABLE_OUTPUT_TOOLS


def normalize_tool_name(tool_name: str) -> str:
    """Return the host-prefix-stripped, lowercased tool name. Useful
    for callers that need the canonical name (e.g. to dispatch on).
    """
    name = (tool_name or "").strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def evaluate_read_output(
    *,
    runtime,
    project_root: Path,
    path: str,
    text_view: str,
    host_session_id: str,
    host_kind: str = "claude_code",
    result_obj: object | None = None,
) -> RedactionResult | None:
    """Evaluate a Read tool's output against the lifecycle service.

    Returns None when the lifecycle service decided nothing needed
    redaction (clean read) or when the call itself errored — the
    caller treats both as "no envelope to send."
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
        return None
    if lc.redacted_response is None:
        return None
    # LifecycleService doesn't currently expose count/categories on
    # its return shape — surface what we have, mark mechanism so audit
    # rows can be disambiguated from the regex-scrub path.
    return RedactionResult(
        redacted=lc.redacted_response,
        redaction_count=getattr(lc, "redaction_count", 0) or 0,
        categories=tuple(getattr(lc, "categories", ()) or ()),
        mechanism="lifecycle_read",
    )


def evaluate_command_output(tool_response: object) -> RedactionResult | None:
    """Evaluate a bash/monitor/run tool result via the regex output_guard.

    Returns None when redaction errored OR nothing matched (count==0).
    """
    try:
        from ..output_guard import redact_tool_response

        redacted, count, categories = redact_tool_response(
            tool_response,
            redact=True,
        )
    except Exception:
        return None
    if count == 0:
        return None
    return RedactionResult(
        redacted=redacted,
        redaction_count=count,
        categories=tuple(categories),
        mechanism="output_guard_regex",
    )
