"""Tool-surface waiver ledger — the ONE list of live-but-off-catalog tools.

Convergence doctrine (tool-surface map §5.2/§8; shape mirrors
test_tool_kind_dispatch.GATE_ONLY_MIGRATION_DOCTRINE): every agent-facing
tool is either declared in tool_interface.py via `@tool`, or carries a
reasoned entry HERE — "name": "reason citing the governing doctrine/parallel
structure + the convergence step that retires the entry." Nothing at runtime
consults this module: it grants or denies nothing and is not an allowlist on
any dispatch path. It is reviewed documentation of the status quo, enforced
from two sides:

* mcp/tests/security/test_tool_surface_reverse_parity.py — walks the two
  live surfaces backwards ({live} − {declared} ⊆ {waived}); fails on any
  live off-catalog tool with no entry here, and on any entry whose tool is
  no longer live or has since gained a `@tool` declaration;
* mcp/tests/security/test_tool_surface_waiver_ledger.py — guards this
  module's own content: substantive ≥80-char reasons, non-empty unique
  names, and no entry for a tool that tool_interface already declares.

Entries were seeded 2026-07-10 verbatim from the reverse-parity suite's
original inline ledgers. Removing or catalog-declaring a waived tool must
delete its entry in the same commit; registering a new live tool without
either a `@tool` declaration or an entry here fails loudly.
"""

from __future__ import annotations

__all__ = ["EXPLICIT_WAIVER_SET", "EXPLICIT_GATE_WAIVER_SET"]


# ── stdio ledger (full-profile local agent surface) ─────────────────────────
#
# Seeded 2026-07-10 from the live full-profile surface (114 registered,
# 81 declared, 35 off-catalog — tool-surface map §3.2/§3.3).

_DEPRECATION_WINDOW_REASON = (
    "Internal _delegate target of the ai_lane/ai_plan consolidators — the "
    "deprecation window CLOSED (SSOT-05 #386/#359, 2026-07-18): registered "
    "but agent-list-hidden via outer_gate_catalog.CONSOLIDATOR_DELEGATE_IMPLS "
    "(absence from stdio list pinned by test_consolidator_stdio_live; C.20 "
    "direct dispatch pinned by test_c20_direct_dispatch) — retires with C.20."
)

_DEPRECATION_WINDOW_SIBLINGS = (
    # 8 legacy ai_lane_* names
    "ai_lane_agents",
    "ai_lane_control",
    "ai_lane_exit",
    "ai_lane_grant",
    "ai_lane_inbox",
    "ai_lane_send",
    "ai_lane_state",
    "ai_lane_summary",
    # 13 legacy ai_plan_* names
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
)

_CONSOLIDATOR_DELEGATE_REASON = (
    "internal _delegate target of the ai_memory consolidator (memory-war "
    "unify 2026-07-16); registered but agent-list-hidden via "
    "outer_gate_catalog.CONSOLIDATOR_DELEGATE_IMPLS — retires with BACKLOG C.20"
)

_STDIO_INDIVIDUAL_WAIVERS: dict[str, str] = {
    # ai_backends / ai_models / ai_deslop_apply / planning_step_mark /
    # workflow_action_satisfy retired 2026-07-15 (SSOT-04 phase C):
    # declared LOCAL_ONLY in tool_interface.py.
    "ai_run": (
        "Permanent named waiver — NOT a registry @tool by Empire ruling "
        "2026-06-20 (tool_interface.py ai_run carve-out note): a registry "
        "stub cannot satisfy BOTH the run-annotation override (the gate "
        "forces destructiveHint=False) AND the 'remote shell needs its own "
        "seal' doctrine. The detached shell runner keeps its structurally "
        "distinct dispatch via RUN_ALLOWLIST (outer_gate_executor.py), a "
        "bare frozenset that deliberately never unions the registry (unlike "
        "READ_EXEC_ALLOWLIST / EDIT_ALLOWLIST) — map §3.3. No convergence "
        "step retires this entry."
    ),
    "ai_run_output": (
        "Permanent named waiver — same ai_run seal carve-out (Empire "
        "2026-06-20, tool_interface.py note): the run-annotation override "
        "(gate forces destructiveHint=False) plus the 'remote shell needs "
        "its own seal' doctrine keep the shell trio out of the registry; "
        "dispatched solely via RUN_ALLOWLIST, which never unions the "
        "registry — map §3.3. No convergence step retires this entry."
    ),
    "ai_run_kill": (
        "Permanent named waiver — same ai_run seal carve-out (Empire "
        "2026-06-20, tool_interface.py note): the run-annotation override "
        "(gate forces destructiveHint=False) plus the 'remote shell needs "
        "its own seal' doctrine keep the shell trio out of the registry; "
        "dispatched solely via RUN_ALLOWLIST, which never unions the "
        "registry — map §3.3. No convergence step retires this entry."
    ),
    "config_set": (
        "NOT a registry @tool by Empire ruling 2026-06-20 (tool_interface.py "
        "config_set note): the gate serves it as a PROJECT_TOOL_SPECS "
        "control-plane op; the local stdio host-config writer stays its own "
        "@server.tool (server_project_admin_tools.py)."
    ),
    # Memory-war unify (2026-07-16): the four legacy memory_* impls stay
    # REGISTERED as internal _delegate targets of the ai_memory consolidator
    # (tool_interface.ai_memory routes each mode to them) but their @tool
    # declarations were removed in the SSOT fold, and they are hidden from the
    # agent tools/list (outer_gate_catalog.CONSOLIDATOR_DELEGATE_IMPLS).
    # Convergence: BACKLOG C.20 (lift impl closures to module level) retires
    # the _delegate server-call bridge, dropping these registrations + waivers
    # together.
    "memory_capture": _CONSOLIDATOR_DELEGATE_REASON,
    "memory_read": _CONSOLIDATOR_DELEGATE_REASON,
    "memory_search": _CONSOLIDATOR_DELEGATE_REASON,
    "memory_promote": _CONSOLIDATOR_DELEGATE_REASON,
}

#: stdio waiver ledger: every tool registered on the full-profile stdio
#: server that carries no tool_interface `@tool` declaration. Consumed by
#: test_tool_surface_reverse_parity as STDIO_OFF_CATALOG_WAIVERS.
EXPLICIT_WAIVER_SET: dict[str, str] = {
    **{n: _DEPRECATION_WINDOW_REASON for n in _DEPRECATION_WINDOW_SIBLINGS},
    **_STDIO_INDIVIDUAL_WAIVERS,
}


# ── WebMCP gate ledger (outer_gate_catalog.advertised) ──────────────────────
#
# Seeded 2026-07-10 from the live advertised() emission for a full-scope
# super_admin principal (97 advertised, 35 off-catalog). The gate's parallel
# structures are inventoried in map §4.2; convergence is map §8 step 6 (fold
# the allowlists/PROJECT_TOOL_SPECS/MCP_TIER_OVERRIDES into ToolSpec fields).

_GATE_SELECTOR_REASON = (
    "Gate control-plane/tenancy surface fed by PROJECT_TOOL_SPECS or a "
    "sibling parallel structure (outer_gate_catalog.py hand-authored specs; "
    "map §4.2) — advertised via the selector mechanism, not the registry; "
    "convergence tracked as map §8 step 6."
)

_GATE_SELECTOR_CLUSTER = (
    "config_set",
)

_GATE_INDIVIDUAL_WAIVERS: dict[str, str] = {
    "ai_str_replace": (
        "EDIT_ALLOWLIST hand-carry (outer_gate_edit.py); pinned as expected "
        "legacy remainder by test_tool_interface_registry — map §3.6/§4.3."
    ),
    "ai_run": (
        "RUN_ALLOWLIST (outer_gate_executor.py), special-cased at the top of "
        "OuterGate.execute() — permanent carve-out, NOT a registry @tool by "
        "Empire ruling 2026-06-20 (tool_interface.py ai_run note): a registry "
        "stub cannot satisfy BOTH the run-annotation override (gate forces "
        "destructiveHint=False) AND the 'remote shell needs its own seal' "
        "doctrine, so RUN_ALLOWLIST stays a bare frozenset with no registry "
        "union (unlike READ_EXEC_ALLOWLIST / EDIT_ALLOWLIST) — map "
        "§3.3/§4.3. No convergence step retires this entry."
    ),
    "ai_run_output": (
        "RUN_ALLOWLIST third dispatch mechanism — same permanent ai_run seal "
        "carve-out (Empire 2026-06-20, tool_interface.py note): the "
        "run-annotation override (gate forces destructiveHint=False) plus "
        "the 'remote shell needs its own seal' doctrine exclude the shell "
        "trio from the registry; RUN_ALLOWLIST never unions the registry — "
        "map §3.3/§4.3. No convergence step retires this entry."
    ),
    "ai_run_kill": (
        "RUN_ALLOWLIST third dispatch mechanism — same permanent ai_run seal "
        "carve-out (Empire 2026-06-20, tool_interface.py note): the "
        "run-annotation override (gate forces destructiveHint=False) plus "
        "the 'remote shell needs its own seal' doctrine exclude the shell "
        "trio from the registry; RUN_ALLOWLIST never unions the registry — "
        "map §3.3/§4.3. No convergence step retires this entry."
    ),
}

#: gate waiver ledger: every tool the gate's tools/list emission advertises
#: (full-scope super_admin principal) that carries no tool_interface `@tool`
#: declaration. Consumed by test_tool_surface_reverse_parity as
#: GATE_OFF_CATALOG_WAIVERS.
EXPLICIT_GATE_WAIVER_SET: dict[str, str] = {
    **{n: _GATE_SELECTOR_REASON for n in _GATE_SELECTOR_CLUSTER},
    **_GATE_INDIVIDUAL_WAIVERS,
}
