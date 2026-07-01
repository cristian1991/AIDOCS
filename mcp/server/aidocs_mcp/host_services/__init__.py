"""Host-agnostic services extracted from claude_hook.py — Phase 2 of
the claude_hook thinning campaign (started 2026-05-27).

DOCTRINE — see ``.MEMORY/sessions/ubermega/AUDIT_claude_hook.md``:

  claude_hook.py was 3892 lines of logic ~88% of which was
  host-agnostic — the same decisions OpenCode, future Codex adapters,
  and OpenAI Agents adapters all need to make. Phases 1A + 1B
  collapsed the 8 wrapper shells around canonical_intent_registry.
  Phase 2 (this package) lifts the next layer up: small, named
  services with clear input/output that any host adapter can call.

  Each service module here:
    1. Is host-agnostic (no Claude/OpenCode specifics)
    2. Has a single clear responsibility
    3. Can be reused by host_adapter_cli + claude_hook + opencode plugin
    4. Has its own test file under mcp/tests/host_services/

  After Phase 2 lands every service from the audit catalog, Phase 3
  thins the event handlers in claude_hook to ~10-line CLI delegates.

Services in this package (seeded today, growing):

  path_resolver_service       — resolve templates/scripts/cwd/project roots
  output_redaction_policy     — decide whether + how to redact tool output

Services pending (from AUDIT_claude_hook.md §"Service catalog"):

  ToolActionClassifier        — _TOOL_ACTION_BUCKETS + classify
  AuditPayloadBuilder         — pretool/posttool audit row shaping
  LaneStateService            — current lane ID, lane state queries
  EnforcementPolicy           — bypass detection + bypass-log
  OperatorIntentService       — operator-intent note construction
  PromptContextBuilder        — light/enforced/standard prompt context
  ToolDiscoveryHint           — MCP alternatives hint
  ProtectedPathPolicy         — protected config + infrastructure paths
  GrantStateService           — sticky grant consume + grant intent
  AgentDispatchBriefService   — agent dispatch brief check
  SkillSuggestionService      — skill inference for prompt context
  AidocsSlashCommand          — /aidocs slash handler body
"""

from __future__ import annotations

from . import (
    output_redaction_policy,
    path_resolver_service,
    protected_path_policy,
    tool_discovery_hint,
)

__all__ = [
    "output_redaction_policy",
    "path_resolver_service",
    "protected_path_policy",
    "tool_discovery_hint",
]
