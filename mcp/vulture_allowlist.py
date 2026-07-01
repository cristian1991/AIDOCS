"""Vulture allowlist — INTENTIONAL dead-code references.

Doctrine: vulture flags every unreachable branch. Some branches are
          deliberately disabled (staged migrations, controller skeletons
          retained for trace shape) and should NOT be touched until the
          migration that owns them finishes. List those here so the
          gate stays high-signal — vulture's REAL findings (typos,
          indentation bugs like mcp_server.py:738 that we just fixed)
          shouldn't drown in known-intentional noise.
Why:      every entry needs a comment naming the OWNER ticket / lane.
          When a migration lands, its allowlist entries get deleted in
          the SAME commit. The list rots otherwise.
Usage:    vulture mcp/server/aidocs_mcp mcp/vulture_allowlist.py

This file is consumed by vulture only; never imported by runtime code.
"""

# ── enforcement_pkg controller migration (Lane 2 phase 1) ──
# Dead-skeleton fallback in controller.py — `enforce_via_legacy(request)`
# returns first; the Decision(...) block below is retained as a typed
# template for the next phase. Delete when controller is fully wired.
Decision  # noqa: F821


# ── agent_orchestrator dev-mode kill-switch trace (castle law 2026-05-04) ──
# `if False: _sec012_trace.add(...)` branch preserves trace shape during
# the migration that replaces this whole try/except. Delete when migration
# lands and the kill_switch path moves into the controller.
_sec012_trace  # noqa: F821
_kill_record  # noqa: F821
