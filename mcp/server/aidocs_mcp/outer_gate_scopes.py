"""Public scope constants shared by the open tool taxonomy and the private WebMCP gate.

Kept public so the open taxonomy (outer_gate_edit / outer_gate_catalog) needs no import from
the private gate moat (outer_gate_token_store). The gate's token store re-exports
SCOPE_TIER_M_EDIT from here, so existing `from .outer_gate_token_store import SCOPE_TIER_M_EDIT`
callers in the moat are unaffected.
"""
from __future__ import annotations

# Tier-M (real source edit) scope. The full scope vocabulary lives in the private token
# store; only this one constant is needed by the public taxonomy, so only it lives here.
SCOPE_TIER_M_EDIT = "tier_m_edit"
