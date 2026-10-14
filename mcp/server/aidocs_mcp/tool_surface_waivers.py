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

# #768 / C.20 (2026-07-20): the lane/plan and memory consolidator
# delegate targets are direct implementation bindings, not FastMCP tools.
# Their former 25-name waiver cluster is intentionally empty; the canonical
# direct-only inventory lives in outer_gate_catalog.CONSOLIDATOR_DELEGATE_IMPLS.
_DEPRECATION_WINDOW_SIBLINGS: tuple[str, ...] = ()

# #768: every live stdio tool now has a Tool Interface declaration.
_STDIO_INDIVIDUAL_WAIVERS: dict[str, str] = {}

#: stdio waiver ledger: every tool registered on the full-profile stdio
#: server that carries no tool_interface `@tool` declaration. Consumed by
#: test_tool_surface_reverse_parity as STDIO_OFF_CATALOG_WAIVERS.
EXPLICIT_WAIVER_SET: dict[str, str] = dict(_STDIO_INDIVIDUAL_WAIVERS)


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

_GATE_SELECTOR_CLUSTER: tuple[str, ...] = ()

# #768: all gate-advertised tools now have Tool Interface declarations.
_GATE_INDIVIDUAL_WAIVERS: dict[str, str] = {}

#: gate waiver ledger: every tool the gate's tools/list emission advertises
#: (full-scope super_admin principal) that carries no tool_interface `@tool`
#: declaration. Consumed by test_tool_surface_reverse_parity as
#: GATE_OFF_CATALOG_WAIVERS.
EXPLICIT_GATE_WAIVER_SET: dict[str, str] = {
    **{n: _GATE_SELECTOR_REASON for n in _GATE_SELECTOR_CLUSTER},
    **_GATE_INDIVIDUAL_WAIVERS,
}
