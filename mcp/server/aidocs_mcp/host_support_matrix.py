"""Canonical host support matrix — TWO orthogonal axes, never conflated.

The danger this module guards against is OVERCLAIMING: telling an operator a host
is "full" when AIDOCS has no wired adapter, or claiming a hook surface a host
doesn't expose. To stay honest we separate two questions that are easy to blur:

  AXIS 1 — HOST SURFACE CAPABILITY (what the host's hook contract EXPOSES):
      full-surface   — exposes UPS + pre-tool + post-tool + permission-equivalent
      partial-surface— exposes some but not the full enforcement set
      mcp-only       — no host hooks; only the MCP tools/call boundary
      startup-only   — only a session/agent-start context hook
      unknown        — not yet researched / no hook surface found
      legacy         — retired / migration-only

  AXIS 2 — AIDOCS ADAPTER STATUS (what AIDOCS has actually BUILT for it):
      wired        — adapter exists AND proof tests cross-check it (this file's
                     test asserts wired hosts have real adapter code).
      candidate    — host is capable; AIDOCS adapter is the next build (NOT wired).
      spike-needed — host might be capable; needs an investigation spike first.
      unsupported  — AIDOCS deliberately doesn't adapt it (or can't).
      legacy       — retired.

PUBLIC LABEL is DERIVED from both (see `derive_label`). Rules the test enforces:
  * "full" is published ONLY for full-surface + wired (adapter + proof tests).
  * "full_candidate" requires full-surface + candidate/spike — and the docs MUST
    visibly say "AIDOCS adapter pending / NOT WIRED" (HOSTS.md does).
  * mcp-only/unknown + candidate → "mcp_candidate" (PARTIAL/MCP CANDIDATE).
  * mcp-only + unsupported → "mcp_only" (MCP-ONLY UNTIL PROVEN).

The per-surface cells below carry CAPABILITY (does the host expose it) + a note
that records the AIDOCS wiring nuance for that surface. The output_redact
capability delegates to `host_capabilities` so the redaction axis can't drift.
"""

from __future__ import annotations

from .host_capabilities import (
    can_redact_tool_output_before_context,
    can_replace_posttool_with_feedback,
)

# ── per-surface CAPABILITY vocab (does the host EXPOSE a usable hook here) ──
SUP = "supported"
PARTIAL = "partial"
MCP_ONLY = "mcp-only"
NONE = "none"
UNKNOWN = "unknown"
LEGACY = "legacy"

# ── Axis 1: host surface capability (summary) ──
FULL_SURFACE = "full-surface"
PARTIAL_SURFACE = "partial-surface"
MCP_ONLY_SURFACE = "mcp-only"
STARTUP_ONLY = "startup-only"

# ── Axis 2: AIDOCS adapter status ──
WIRED = "wired"
CANDIDATE = "candidate"
SPIKE_NEEDED = "spike-needed"
UNSUPPORTED = "unsupported"

SURFACES: tuple[str, ...] = (
    "user_prompt_submit",
    "pre_tool",
    "permission_request",
    "post_tool",
    "stop",
    "output_redact",
)

# host → {surface: {status, note}}  — CAPABILITY only (host-exposed), with the
# AIDOCS wiring nuance recorded in the note.
_SURFACE: dict[str, dict[str, dict[str, str]]] = {
    "claude_code": {
        "user_prompt_submit": {"status": SUP, "note": "UserPromptSubmit → PromptMutator (wired)"},
        "pre_tool": {"status": SUP, "note": "PreToolUse → ToolGate.evaluate_tool (wired)"},
        "permission_request": {"status": SUP, "note": "PermissionRequest exists; AIDOCS rides PreTool permissionDecision (deny/ask), dedicated event not wired"},
        "post_tool": {"status": SUP, "note": "PostToolUse updatedToolOutput + audit (wired)"},
        "stop": {"status": SUP, "note": "Stop/SubagentStop → Stop-gate + freeze (wired)"},
    },
    "opencode": {
        "user_prompt_submit": {"status": PARTIAL, "note": "chat.message/system.transform/messages.transform run PromptMutator; operator-intent config mutation WITHHELD (no parallel JS authority)"},
        "pre_tool": {"status": SUP, "note": "tool.execute.before blocks reads + mutates args (wired via aidocs.js)"},
        "permission_request": {"status": SUP, "note": "permission.ask + asked/replied exist; AIDOCS rides tool.execute.before, dedicated handler not wired"},
        "post_tool": {"status": PARTIAL, "note": "tool.execute.after: no result-replacement field → no redact"},
        "stop": {"status": PARTIAL, "note": "session.idle/status/error event bus (audit-ish)"},
    },
    "codex_cli": {
        "user_prompt_submit": {"status": SUP, "note": "Codex UserPromptSubmit → shared claude_hook dispatch → PromptMutator"},
        "pre_tool": {"status": SUP, "note": "PreToolUse matches MCP tool names → shared enforcement"},
        "permission_request": {"status": SUP, "note": "PermissionRequest exists; AIDOCS rides PreTool permissionDecision"},
        "post_tool": {"status": PARTIAL, "note": "decision:block/continue:false feedback suppression, not shape-preserving"},
        "stop": {"status": UNKNOWN, "note": "Stop not yet confirmed in Codex hook set"},
    },
    "conductor_worker": {
        "user_prompt_submit": {"status": MCP_ONLY, "note": "worker prompt is conductor-issued; no AIDOCS UPS hook"},
        "pre_tool": {"status": MCP_ONLY, "note": "outer-gate tools/call + lane allowed-tools"},
        "permission_request": {"status": MCP_ONLY, "note": "approval via gate/lane policy"},
        "post_tool": {"status": MCP_ONLY, "note": "owned-tool output_guard only"},
        "stop": {"status": MCP_ONLY, "note": "terminal state via lane-agent store, not a host stop hook"},
    },
    "openai_agents": {
        "user_prompt_submit": {"status": NONE, "note": "SDK has no prompt hook"},
        "pre_tool": {"status": SUP, "note": "on_tool_start → AgentOrchestrator.check_tool (wired)"},
        "permission_request": {"status": NONE, "note": "no permission hook; on_tool_start gates instead"},
        "post_tool": {"status": PARTIAL, "note": "on_tool_end observer + owned-tool output_guard (host-independent)"},
        "stop": {"status": PARTIAL, "note": "on_agent_end observer (audit), no enforcement"},
    },
    "generic_mcp": {
        "user_prompt_submit": {"status": MCP_ONLY, "note": "no host prompt hook"},
        "pre_tool": {"status": MCP_ONLY, "note": "outer-gate authorizes at tools/call"},
        "permission_request": {"status": MCP_ONLY, "note": "approval via gate policy"},
        "post_tool": {"status": MCP_ONLY, "note": "owned-tool output_guard only"},
        "stop": {"status": NONE, "note": "no stop hook"},
    },
    # ── candidates (full-surface, adapter pending) ──
    "copilot": {
        "user_prompt_submit": {"status": SUP, "note": "userPromptSubmitted (blocking)"},
        "pre_tool": {"status": SUP, "note": "preToolUse (synchronous, can block)"},
        "permission_request": {"status": SUP, "note": "permission via hook decision flow"},
        "post_tool": {"status": SUP, "note": "postToolUse"},
        "stop": {"status": SUP, "note": "agentStop"},
    },
    "windsurf_cascade": {
        "user_prompt_submit": {"status": SUP, "note": "pre_user_prompt"},
        "pre_tool": {"status": SUP, "note": "pre_read/write/command/mcp (block via exit 2)"},
        "permission_request": {"status": SUP, "note": "indirect via block/allow pre-hooks"},
        "post_tool": {"status": SUP, "note": "post hooks + transcript hook"},
        "stop": {"status": UNKNOWN, "note": "no explicit stop hook confirmed"},
    },
    "kilo": {
        "user_prompt_submit": {"status": PARTIAL, "note": "OpenCode-style chat.message/transforms (same withholding policy once wired)"},
        "pre_tool": {"status": SUP, "note": "tool.execute.before"},
        "permission_request": {"status": SUP, "note": "permission.ask"},
        "post_tool": {"status": SUP, "note": "tool.execute.after"},
        "stop": {"status": UNKNOWN, "note": "OpenCode-family session events not yet confirmed"},
    },
    "cline_sdk": {
        "user_prompt_submit": {"status": SUP, "note": "plugin hooks advertised — needs SDK spike"},
        "pre_tool": {"status": SUP, "note": "custom tools/hooks via plugins — needs SDK spike"},
        "permission_request": {"status": PARTIAL, "note": "policy/approval layer; never trust Cline native safe/unsafe as law"},
        "post_tool": {"status": UNKNOWN, "note": "likely possible; needs exact SDK proof"},
        "stop": {"status": UNKNOWN, "note": "not confirmed"},
    },
    # ── partial-surface ──
    "cline_ide": {
        "user_prompt_submit": {"status": NONE, "note": "config-hooks docs point to the SDK"},
        "pre_tool": {"status": PARTIAL, "note": "approval policies, not adapter-hard hooks"},
        "permission_request": {"status": PARTIAL, "note": "approval/auto-approve; YOLO auto-approves — never law"},
        "post_tool": {"status": NONE, "note": "limited"},
        "stop": {"status": UNKNOWN, "note": "not confirmed"},
    },
    "zed_acp": {
        "user_prompt_submit": {"status": NONE, "note": "no native UserPromptSubmit found"},
        "pre_tool": {"status": PARTIAL, "note": "tool permissions (confirm/allow/deny)"},
        "permission_request": {"status": PARTIAL, "note": "confirm/allow/deny tool permissions"},
        "post_tool": {"status": UNKNOWN, "note": "not confirmed"},
        "stop": {"status": UNKNOWN, "note": "not confirmed"},
    },
    "gemini_cli": {
        "user_prompt_submit": {"status": UNKNOWN, "note": "no hook surface found in repo"},
        "pre_tool": {"status": MCP_ONLY, "note": "MCP tool gating only"},
        "permission_request": {"status": UNKNOWN, "note": "not found"},
        "post_tool": {"status": UNKNOWN, "note": "not found"},
        "stop": {"status": UNKNOWN, "note": "not found"},
    },
    "goose": {
        "user_prompt_submit": {"status": UNKNOWN, "note": "no first-class prompt hook found"},
        "pre_tool": {"status": MCP_ONLY, "note": "MCP/ACP tool gating only"},
        "permission_request": {"status": UNKNOWN, "note": "not found"},
        "post_tool": {"status": UNKNOWN, "note": "not found"},
        "stop": {"status": UNKNOWN, "note": "not found"},
    },
    # ── mcp-only / legacy ──
    "cursor": {
        "user_prompt_submit": {"status": MCP_ONLY, "note": "no public hook proof"},
        "pre_tool": {"status": MCP_ONLY, "note": "MCP tool gating; IDE permissions only"},
        "permission_request": {"status": UNKNOWN, "note": "IDE permissions only; no hook seam proven"},
        "post_tool": {"status": MCP_ONLY, "note": "no host post-tool hook"},
        "stop": {"status": NONE, "note": "no stop hook"},
    },
    "continue": {
        "user_prompt_submit": {"status": LEGACY, "note": "rules/MCP only; no prompt hook proven"},
        "pre_tool": {"status": MCP_ONLY, "note": "MCP tool gating only"},
        "permission_request": {"status": UNKNOWN, "note": "not found"},
        "post_tool": {"status": UNKNOWN, "note": "not found"},
        "stop": {"status": UNKNOWN, "note": "not found"},
    },
    "roo_code": {
        "user_prompt_submit": {"status": LEGACY, "note": "retired"},
        "pre_tool": {"status": LEGACY, "note": "retired"},
        "permission_request": {"status": LEGACY, "note": "retired"},
        "post_tool": {"status": LEGACY, "note": "retired"},
        "stop": {"status": LEGACY, "note": "retired"},
    },
}

# host → (surface_capability, adapter_status). The two honest axes.
_AXES: dict[str, tuple[str, str]] = {
    "claude_code": (FULL_SURFACE, WIRED),
    "opencode": (FULL_SURFACE, WIRED),
    "codex_cli": (FULL_SURFACE, WIRED),  # UPS/PreTool wired via shared dispatch + proof tests; redaction host-kind attribution pending
    "conductor_worker": (MCP_ONLY_SURFACE, WIRED),
    "openai_agents": (PARTIAL_SURFACE, WIRED),
    "generic_mcp": (MCP_ONLY_SURFACE, WIRED),
    "copilot": (FULL_SURFACE, CANDIDATE),
    "windsurf_cascade": (FULL_SURFACE, CANDIDATE),
    "kilo": (FULL_SURFACE, CANDIDATE),
    "cline_sdk": (FULL_SURFACE, SPIKE_NEEDED),
    "cline_ide": (PARTIAL_SURFACE, UNSUPPORTED),
    "zed_acp": (PARTIAL_SURFACE, UNSUPPORTED),
    "gemini_cli": (MCP_ONLY_SURFACE, CANDIDATE),
    "goose": (MCP_ONLY_SURFACE, CANDIDATE),
    "cursor": (MCP_ONLY_SURFACE, UNSUPPORTED),
    "continue": (LEGACY, UNSUPPORTED),
    "roo_code": (LEGACY, UNSUPPORTED),
}

_ALIASES: dict[str, str] = {
    "claude": "claude_code", "cc": "claude_code",
    "codex": "codex_cli", "openai": "openai_agents", "mcp": "generic_mcp",
    "host_adapter_cli": "opencode",
    "kilocode": "kilo", "kilo_cli": "kilo",
    "github_copilot": "copilot", "copilot_cli": "copilot",
    "windsurf": "windsurf_cascade", "cascade": "windsurf_cascade",
    "cline": "cline_ide", "gemini": "gemini_cli", "zed": "zed_acp",
}


def _canon(host_kind: str | None) -> str:
    k = (host_kind or "").strip().lower()
    return _ALIASES.get(k, k)


def _output_redact_capability(host_key: str) -> dict[str, str]:
    if can_redact_tool_output_before_context(host_key):
        return {"status": SUP, "note": "shape-preserving redaction (host_capabilities)"}
    if can_replace_posttool_with_feedback(host_key):
        return {"status": PARTIAL, "note": "feedback suppression only (host_capabilities)"}
    return {"status": NONE, "note": "no pre-context redaction (host_capabilities, fail-closed)"}


def host_surface_status(host_kind: str | None, surface: str) -> dict[str, str]:
    """CAPABILITY of a host for a surface (does it expose a usable hook). Unknown
    host/surface fails closed to UNKNOWN."""
    key = _canon(host_kind)
    if surface == "output_redact":
        return _output_redact_capability(key) if key in _SURFACE else {"status": UNKNOWN, "note": "unclassified host"}
    cells = _SURFACE.get(key)
    if not cells:
        return {"status": UNKNOWN, "note": "unclassified host"}
    return cells.get(surface, {"status": UNKNOWN, "note": "unclassified surface"})


def host_axes(host_kind: str | None) -> dict[str, str]:
    """The two honest axes for a host: surface_capability + adapter_status."""
    cap, adapter = _AXES.get(_canon(host_kind), (UNKNOWN, UNSUPPORTED))
    return {"surface_capability": cap, "adapter_status": adapter}


def derive_label(surface_capability: str, adapter_status: str) -> str:
    """Public label DERIVED from the two axes. 'full' is reachable ONLY via
    full-surface + wired. Candidates are never 'full'."""
    if surface_capability == LEGACY or adapter_status == "legacy":
        return "legacy"
    if surface_capability == FULL_SURFACE:
        if adapter_status == WIRED:
            return "full"
        if adapter_status in (CANDIDATE, SPIKE_NEEDED):
            return "full_candidate"  # NOT WIRED — docs must say adapter pending
        return "partial"
    if surface_capability == PARTIAL_SURFACE:
        return "partial"
    if surface_capability == MCP_ONLY_SURFACE:
        if adapter_status == WIRED:
            return "mcp_only"
        if adapter_status in (CANDIDATE, SPIKE_NEEDED):
            return "mcp_candidate"  # PARTIAL/MCP CANDIDATE
        return "mcp_only"  # MCP-ONLY UNTIL PROVEN
    if surface_capability == STARTUP_ONLY:
        return "startup_only"
    return "unknown"


def host_label(host_kind: str | None) -> str:
    ax = host_axes(host_kind)
    return derive_label(ax["surface_capability"], ax["adapter_status"])


def host_matrix(host_kind: str | None) -> dict[str, dict[str, str]]:
    key = _canon(host_kind)
    if key not in _SURFACE:
        return {s: {"status": UNKNOWN, "note": "unclassified host"} for s in SURFACES}
    return {s: host_surface_status(key, s) for s in SURFACES}


def full_matrix() -> dict[str, dict]:
    return {
        host: {
            "axes": host_axes(host),
            "label": host_label(host),
            "surfaces": host_matrix(host),
        }
        for host in _SURFACE
    }


def known_hosts() -> tuple[str, ...]:
    return tuple(_SURFACE.keys())


def is_wired(host_kind: str | None) -> bool:
    """True iff AIDOCS has a wired adapter for this host (adapter_status=wired)."""
    return host_axes(host_kind)["adapter_status"] == WIRED


def supports_user_prompt_submit(host_kind: str | None) -> bool:
    """True ONLY when the host exposes a usable UPS hook AND AIDOCS has it WIRED.
    A capable-but-unwired candidate returns False — the guard against claiming a
    prompt authority that doesn't run."""
    return (
        host_surface_status(host_kind, "user_prompt_submit").get("status") == SUP
        and is_wired(host_kind)
    )
