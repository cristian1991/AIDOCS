"""Tool visibility sets for LOCAL tool registration -- a neutral public module.

Design decision (2026-10-29): every outer_gate*.py is private, and no public
module may import one. Local ``create_server`` registration and its
``list_tools`` filter need the hidden / delegate / list-hidden carve-outs and
the run-trio name set, so those -- and only those -- live here. They are plain
name sets and pure predicates: they decide what the LOCAL surface registers
and lists, never what any caller is allowed to do. Authority (scope, grants,
project binding, audit) stays where it is.

``outer_gate_catalog`` and ``outer_gate_executor`` re-export these names, so
the gate and the local surface still read ONE declaration.
"""

from __future__ import annotations

# The ONE carve-out invisible to BOTH local MCP and the gate: @server.tool
# functions that are folded-into-dispatcher helpers / never agent-facing. This is
# the single owner -- mcp_server imports it (no separate hand-maintained list), so
# local list_tools and the gate share the same hidden set. NOTE: this is NOT the
# same as the gate's CLASS_INTERNAL -- control/config/lane tools are internal to
# the REMOTE surface but remain fully visible+usable LOCALLY (local is
# full-trust); only HIDDEN_EVERYWHERE is hidden on both.
HIDDEN_EVERYWHERE = frozenset(
    {
        "ai_find_symbol_range",
        "ai_extract_block",
        "ai_extract_symbol",
        "ai_preview_extraction_deps",
        "ai_refactor_extract",
        "ai_suggest_extractions",
        # Empire 2026-06-20: never agent-facing on EITHER surface (local stdio or gate).
        # Host/desktop UI tools the agent cannot drive + operator-only authority an
        # agent must never self-serve (self-approving an escalation / granting itself
        # membership is the control-plane breach to prevent). The operator drives these
        # from the dashboard, not the agent tool surface. Complements the PROJECT_TOOLS
        # exclusion (_NOT_AGENT_ADVERTISED) so both surfaces drop them.
        "tauri_todo_list",
        "tauri_backlog_list",
        "approve_escalation",
        "deny_escalation",
        # War 0 (2026-07-13): freeze recovery + surfacing are operator dashboard
        # ops -- same doctrine as escalation approve/deny (an agent must never
        # see or self-serve the lockdown controls it may itself be under).
        "clear_freeze",
        "freeze_list",
        "escalation_list",
        "project_grant_member",
        "project_revoke_member",
        "project_acl_list",
    },
)

# Dashboard Tauri IPC (backlog #288, conductor increment): registered ONLY on
# the dashboard profile (`@_dash_tool` in mcp_server.py, dashboard_mode=True)
# so the dashboard can drive the conductor CLI subprocess via tools/call -- but
# they are NOT agent tools (not in tool_interface's ai_*(mode) catalog) and
# must never appear in ANY agent-facing tools/list, dashboard profile included.
# LIST-hidden only: putting them in HIDDEN_EVERYWHERE would make mcp_server's
# _taxonomy_tool SKIP their registration entirely and break the dashboard IPC.
# Mirrors outer_gate_manifest.REGISTRY_ONLY_TOOLS (Tier-L local_only);
# test_conductor_ipc_hidden pins the two sets against drift.
DASHBOARD_IPC_TOOLS = frozenset(
    {
        "conductor_start",
        "conductor_send",
        "conductor_stop",
        "conductor_output",
    },
)

# Memory-war unify (2026-07-16): registered @server.tool impls whose AGENT
# surface is GATE_ONLY. They stay REGISTERED (the gate's read executor +
# the dashboard tools/call route keep working) but drop off the local stdio
# tools/list -- on local, memory retrieval is automagic (UPS surfacer +
# ai_recall/ai_memory); a palace retrieval tool on the local list would be
# a second, contradicting surface for the same internal projection.
GATE_SERVED_ONLY_IMPLS = frozenset(
    {
        "ai_palace_search",
        "ai_palace_status",
    }
)

# Memory-war unify (2026-07-16): the four legacy memory_* impls stay
# REGISTERED as internal _delegate targets of the ai_memory consolidator but
# are NOT part of any agent surface -- hidden from the agent (full-profile)
# tools/list; the gate never advertises/executes them directly (dropped from
# the registry + allowlists in the same campaign).
CONSOLIDATOR_DELEGATE_IMPLS = frozenset(
    {
        "memory_capture",
        "memory_read",
        "memory_search",
        "memory_promote",
        # SSOT-05 #386/#359 (2026-07-18): the lane/plan deprecation window
        # CLOSED. The 8 ai_lane_* + 13 ai_plan_* legacy siblings stay
        # REGISTERED as internal _delegate targets of the ai_lane / ai_plan
        # consolidators (C.20 direct dispatch + dashboard tools/call keep
        # working) but drop off the agent (full-profile) tools/list -- the
        # consolidators are the ONLY advertised surface for these concerns.
        # Gate posture is unchanged: these names were never gate-advertised
        # (CLASS_INTERNAL fallthrough; see DOCTRINE.md tool table).
        "ai_lane_agents",
        "ai_lane_control",
        "ai_lane_exit",
        "ai_lane_grant",
        "ai_lane_inbox",
        "ai_lane_send",
        "ai_lane_state",
        "ai_lane_summary",
        "ai_plan_create",
        "ai_plan_dispatch",
        "ai_plan_expand",
        "ai_plan_graph",
        "ai_plan_mark_ready",
        "ai_plan_overlap",
        "ai_plan_pause",
        "ai_plan_reopen",
        "ai_plan_report",
        "ai_plan_resume",
        "ai_plan_signal",
        "ai_plan_status",
        "ai_plan_template",
    }
)

# The run trio: the canonical detached runner + its output/kill. A name set,
# not a permission: local registration reads it for the expose_run gate (ONE
# RUNNER PER SURFACE), and the gate's executor reads it as its run allowlist.
RUN_ALLOWLIST = frozenset({"ai_run", "ai_run_output", "ai_run_kill"})


def _registry_hidden_surface(name: str) -> bool:
    """True iff the tool_interface registry declares `name` with
    surface=HIDDEN -- the SSOT expression of "registered but invisible on
    every agent surface" (#386/#288: internal plumbing must not leak into
    agent-facing tools/list). Lazy import: tool_interface is heavy."""
    from . import tool_interface as _ti

    spec = _ti.get(name)
    return spec is not None and spec.surface == _ti.HIDDEN


def agent_list_hidden(name: str) -> bool:
    """Hidden from the AGENT'S (full-profile) stdio tools/list specifically:
    everything local_hidden hides, plus gate-served-only impls, the
    consolidators' internal delegate targets, and any registry spec declared
    surface=HIDDEN (#288 visibility law). The gate's READ-EXECUTOR build
    (read_only profile) deliberately does NOT apply this -- it must list what
    the gate executes (e.g. ai_palace_search for the dashboard route)."""
    return (
        local_hidden(name)
        or name in GATE_SERVED_ONLY_IMPLS
        or name in CONSOLIDATOR_DELEGATE_IMPLS
        or _registry_hidden_surface(name)
    )


def local_hidden(name: str) -> bool:
    """True iff `name` must be hidden from the LOCAL agent's tool list too:
    the single invisible-to-both carve-out (HIDDEN_EVERYWHERE), plus the
    dashboard Tauri IPC set (DASHBOARD_IPC_TOOLS -- list-hidden even on the
    dashboard profile while STAYING registered so the dashboard's tools/call
    keeps working). Agent-surface-only hides (GATE_SERVED_ONLY_IMPLS +
    CONSOLIDATOR_DELEGATE_IMPLS) live in agent_list_hidden -- the gate's
    read-executor list must keep them. Local shows everything else it
    registers (full-trust); the gate applies its own remote classification.
    """
    return name in HIDDEN_EVERYWHERE or name in DASHBOARD_IPC_TOOLS
