"""The ONE canonical scope of pre-context output redaction (#1136 F2/F5).

Two things every output-redaction consumer must agree on, held in one leaf
module (no imports) so no adapter or policy can drift onto a private copy:

  * ``REDACTABLE_OUTPUT_TOOLS`` — the normalized (host-prefix-stripped,
    lowercased) names of tools whose RESULT is model-visible and must be
    certified before it reaches context. ``hook_pipeline`` and
    ``host_services.output_redaction_policy`` both import THIS object; a parity
    test pins the identity. Widening it (Grep/Glob/WebFetch/MCP) is a separate,
    reviewed change.

  * ``OutputScanState`` — the explicit verdict that replaces the old ``None``
    collapse, where "nothing to redact" and "the scan crashed" were the same
    value and the crash therefore delivered RAW output:

      NOT_APPLICABLE  not in scope (tool / no output / host cannot replace)
      CLEAN           certified: deliver the original, byte-identical
      REDACTED        findings: deliver the shape-preserving redacted copy
      UNKNOWN         certification failed after eligibility was known:
                      WITHHOLD (unknown != clean)
"""

from __future__ import annotations

from enum import Enum

REDACTABLE_OUTPUT_TOOLS: frozenset[str] = frozenset({"read", "bash", "monitor", "run"})

#: Audit event for an uncertifiable scan whose output was withheld — distinct
#: from a redaction so the audit never logs a crash as a clean zero-count.
UNKNOWN_WITHHELD_EVENT = "output_scan_unknown_withheld"
#: Audit event for an uncertifiable scan the operator posture let through
#: (``security.require_output_redaction_for_run=false`` / non-redact policy).
UNKNOWN_PASSTHROUGH_EVENT = "output_scan_unknown_passthrough"


class OutputScanState(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    CLEAN = "clean"
    REDACTED = "redacted"
    UNKNOWN = "unknown"


def normalize_tool_name(name: object) -> str:
    """Strip MCP prefixes + lowercase, so 'mcp__aidocs__bash' -> 'bash'."""
    n = str(name or "").strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if n.startswith(prefix):
            return n[len(prefix) :]
    return n


def is_redactable_tool(name: object) -> bool:
    return normalize_tool_name(name) in REDACTABLE_OUTPUT_TOOLS
