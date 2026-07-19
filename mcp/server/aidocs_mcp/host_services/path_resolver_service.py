"""Path resolution for host adapters.

Lifted from claude_hook.py 2026-05-27 (Phase 2 of the thinning campaign).
Pre-extraction these functions lived at module level in claude_hook.py
as helpers used by ClaudeHookHandler.__init__. The opencode plugin
has an equivalent ``resolveAidocsRuntimeSourceRoot()`` in JS that
should also collapse onto this surface in a future PR.

What's host-agnostic about path resolution:
  - The repo layout (mcp/server/aidocs_mcp/<file>.py → repo root is
    parents[3]) is the same regardless of which host is calling.
  - Templates live at <repo>/core/.MEMORY/.aidocs/templates/.
  - Scripts live at <repo>/core/scripts/.

What's NOT here (kept host-specific in the hook):
  - Reading host-specific env vars (AIDOCS_PROJECT_ROOT,
    AIDOCS_EXPERT_LANE_ID) — those belong with the adapter that
    speaks the host's envelope shape.
"""

from __future__ import annotations

from pathlib import Path


def resolve_templates_root() -> Path:
    """Resolve the AIDOCS templates directory.

    AIDOCS templates (prompt scaffolds, response shapes, session
    bootstrap markdown) all live under ``core/.MEMORY/.aidocs/templates/``
    relative to the repo root. The repo root is computed from this
    file's location: ``parents[3]`` of ``mcp/server/aidocs_mcp/
    host_services/path_resolver_service.py`` resolves to ``<repo>/mcp``
    → another ``parent`` lands at ``<repo>``.

    Wait — that's not quite right; the original location was
    ``mcp/server/aidocs_mcp/claude_hook.py`` whose ``parents[3]`` IS
    the repo root. This file lives one directory deeper, so it needs
    ``parents[4]``.
    """
    return Path(__file__).resolve().parents[4] / "core" / ".MEMORY" / ".aidocs" / "templates"


def resolve_script_root() -> Path:
    """Resolve the AIDOCS scripts directory at ``<repo>/core/scripts/``."""
    return Path(__file__).resolve().parents[4] / "core" / "scripts"
