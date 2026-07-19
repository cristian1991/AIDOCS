"""Vulture FUTURE-DEBT surface — code intentionally not yet wired (#426).

Doctrine (king, 2026-07-17): TWO suppression surfaces feed the deploy
gate's vulture lane (Gate 1d):
  - mcp/vulture_allowlist.py   = FALSE POSITIVES ONLY. Vulture is WRONG:
      the symbol IS consumed (tests, dynamic dispatch, getattr). Hidden
      from deploy output.
  - mcp/vulture_future_debt.py = THIS FILE. Vulture is RIGHT that no
      production consumer exists — but the absence is INTENTIONAL and
      tracked. Every entry carries a `direction=` marker + evidence:
        direction=add    -> a consumer is coming (staged wiring, next slice)
        direction=remove -> the symbol is scheduled for deletion
      These entries ALWAYS SURFACE in the deploy report as non-blocking
      "future debt" lines (the Gate-1d future-debt ledger, appended to
      mcp/.deploy-reports/vulture.summary.txt) — never hidden, never
      blocking.
  Anything matching NEITHER surface = a bug (hard-fail, as always).

When the tracked consumer lands (direction=add) or the deletion ships
(direction=remove), the entry dies in the SAME commit. The ledger makes
rot visible on every deploy.

Usage: vulture mcp/server/aidocs_mcp mcp/vulture_allowlist.py mcp/vulture_future_debt.py

This file is consumed by vulture only; never imported by runtime code.
"""

# ── identity/auth war (session campaign 2026-07-16) ──
require_epoch  # noqa: F821 (direction=add — function @ agent_memory_epoch.py:307, id-tree fail-CLOSED epoch guard with NO production consumer YET; tests/runtime/test_identity_tree_prerequisites.py pins the guard. Tracked follow-up: flip the epoch consumers (dnt_banner/helper_skill/read_memory_surfacer/agent_audit) from fail-open current_epoch onto require_epoch — the public guard landed first, tests first.)
set_session  # noqa: F821 (direction=add — method @ outer_gate_tenancy.py:287, GateBindingStore.set_session updates only the bound session preserving project/org; pinned by tests/security/test_gate_binding_store.py::test_set_session_*. Part of the #283 one-binding store — kept for the session-select wiring, the production caller lands with that slice.)

# ── LSP guest-oracle door (doctrine XXXII, Slice 1, 2026-07-17) ──
evict_all_projects  # noqa: F821 (direction=add — function @ lsp/client.py:490, door-level teardown for the module server pool; exercised today only by tests/lsp/test_lsp_door_fail_open.py + test_lsp_integration_pyright.py fixtures. The production consumer is the Slice 2 drain path (semantic_ref materialization) — part of the evict-after-materialize lifecycle contract.)
