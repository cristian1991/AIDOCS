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

from dataclasses import dataclass

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
) -> OutputRedactionDecision | None:
    """Host-agnostic generic (bash/monitor) output-redaction decision.

    Returns a decision iff: the tool is redactable, the HOST can shape-preserving
    redact, the result exists, and the secret scan finds something. None
    otherwise. The adapter renders the envelope + emits audit. Never raises.
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

        redacted, count, categories = redact_tool_response(tool_response, redact=True)
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
