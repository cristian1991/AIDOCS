"""Sync the DNT registry from on-disk sentinel-marked files.

Walks the project tree, parses any structured DNT header at file head,
upserts via ProtectedFileRegistryStore.record_dnt_header. Same semantics
as ai_protect(mode='sync') — extracted so ai_index_sync can call it
automatically. The on-disk sentinel and SQL registry must not diverge;
manual-only sync was the gap that left dental's ~30 sentinel-marked
files unregistered (witnessed 2026-05-07 by the Phoenix-scribe seat).

Per backlog #79 (DNT auto-surface): the read tools' DNT banner only
fires when find_family_by_path returns a row with non-empty dnt_id.
Auto-population on every ai_index_sync closes that gap structurally
so the on-disk truth and the SQL truth converge automatically.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .dnt_header_parser import parse_dnt_header
from .protected_file_registry_store import ProtectedFileRegistryStore

# Bounded extensions — only languages where a DNT header is plausible.
# Razor (.cshtml) and C# (.cs) are the canonical operator targets;
# .py/.ts/.js round it out.
_DNT_SCAN_EXTS = frozenset(
    {
        ".cs",
        ".cshtml",
        ".razor",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".html",
        ".scss",
        ".css",
        ".sql",
        ".md",
        ".yml",
        ".yaml",
        ".toml",
    },
)

# Skip well-known noise dirs to keep the walk cheap.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".MEMORY",
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "bin",
        "obj",
        "dist",
        "build",
        ".next",
        ".cache",
        "target",
    },
)

# Probe just the top of each file (parser scans first 200 lines
# internally, but cap the read at a few KB to avoid pulling huge
# files entirely into memory).
_PROBE_BYTES = 16384


def sync_dnt_registry(
    project_root: Path,
    *,
    registry: ProtectedFileRegistryStore | None = None,
) -> dict[str, Any]:
    """Walk the project tree, find DNT-headered files, upsert all rows.

    Returns counts dict: {ok, synced, skipped, errors, error_count}.
    Idempotent — re-running over an already-populated tree just
    re-upserts the same rows (INSERT OR REPLACE in record_dnt_header).
    Conservative on per-file errors: collects + caps at 20 surfaced,
    keeps walking the rest of the tree.
    """
    if registry is None:
        registry = ProtectedFileRegistryStore()

    synced = 0
    skipped = 0
    errors: list[dict[str, str]] = []

    for root_dir, dirs, files in os.walk(project_root):
        # Prune skip dirs in-place so os.walk doesn't recurse.
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            fpath = Path(root_dir) / fname
            if fpath.suffix.lower() not in _DNT_SCAN_EXTS:
                continue
            try:
                with fpath.open("rb") as fh:
                    head = fh.read(_PROBE_BYTES)
                try:
                    text = head.decode("utf-8")
                except UnicodeDecodeError:
                    text = head.decode("utf-8", errors="replace")
                header = parse_dnt_header(text)
            except OSError as exc:
                errors.append(
                    {
                        "path": str(fpath.relative_to(project_root)),
                        "error": f"read failed: {exc}",
                    },
                )
                continue
            except Exception as exc:
                errors.append(
                    {
                        "path": str(fpath.relative_to(project_root)),
                        "error": f"parse failed: {exc}",
                    },
                )
                continue
            if not header.is_present:
                skipped += 1
                continue
            rel_path = str(fpath.relative_to(project_root)).replace("\\", "/")
            # Determine role: master if no master line OR master line
            # points at this file. Otherwise satellite.
            master_path = (header.master or "").strip().replace("\\", "/")
            if not master_path or master_path == rel_path:
                dnt_role = "master"
            else:
                dnt_role = "satellite"
            try:
                registry.record_dnt_header(
                    project_root,
                    path=rel_path,
                    dnt_id=header.dnt_id,
                    dnt_role=dnt_role,
                    master=master_path,
                    pair_files=list(header.pair),
                    forbid_list=list(header.forbid),
                    allow_list=list(header.allow),
                    incidents=list(header.incident),
                    baseline=header.baseline,
                    cost=header.cost,
                    full_header_text=header.raw_header_text,
                    why="",
                )
                synced += 1
            except Exception as exc:
                errors.append(
                    {
                        "path": rel_path,
                        "error": f"upsert failed: {exc}",
                    },
                )

    return {
        "ok": len(errors) == 0,
        "synced": synced,
        "skipped": skipped,
        "errors": errors[:20],
        "error_count": len(errors),
    }
