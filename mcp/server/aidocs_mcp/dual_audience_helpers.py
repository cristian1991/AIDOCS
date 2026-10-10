"""DEPRECATED — consolidated into `aidocs_mcp.dual_audience` (2026-04-25).

This module was created in error; the canonical shared-helpers module
`dual_audience.py` pre-existed with a richer API (adds `ok_sub` /
`fail_sub` for subagent-facing output). Consolidation folded the two
into `dual_audience.py` and kept `ok_edit` / `fail_edit` as backward-
compat aliases in the canonical module.

This shim re-exports the names so any third-party importer that still
references `aidocs_mcp.dual_audience_helpers` keeps working. New code
should import from `aidocs_mcp.dual_audience`.
"""

from __future__ import annotations

from .dual_audience import (  # noqa: F401 — backward-compat re-export
    fail_edit,
    fmt_tags,
    ok_edit,
)
