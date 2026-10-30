"""RFC-4 Phase G — operational migration helpers.

For projects that have:
  - drawers in the global palace (~/.mempalace/palace) with wing-scoping
    that should be moved into a per-project palace
  - Mempalace MCP entries in .mcp.json that need to migrate to AIDOCS-
    hosted palace tools (Phase F target shape)
  - shell hooks that don't propagate --palace correctly

This module provides:
  - dry_run_dental_migration() — survey what would move, no writes
  - generate_mcp_json_patch() — emit the new .mcp.json shape
  - generate_hook_patch() — emit the patched settings.local.json

These are reference implementations the operator can wire into a
proper migration command. NOT auto-run — destructive operations
require explicit operator approval.

Adopt by copying to ``aidocs_mcp/phase_g_migration.py`` or by
running standalone via ``python -m aidocs_integration.phase_g_migration``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MigrationSurvey:
    """What a migration would do, without writing anything."""

    project_root: Path
    has_global_palace_drawers: bool = False
    wing_in_global_palace: str = ""
    drawer_count_in_global: int = 0
    has_local_palace: bool = False
    local_palace_path: Path = field(default_factory=lambda: Path())
    mcp_json_path: Path = field(default_factory=lambda: Path())
    mcp_json_has_mempalace_entry: bool = False
    mcp_json_has_aidocs_entry: bool = False
    settings_local_path: Path = field(default_factory=lambda: Path())
    hooks_missing_palace_propagation: list = field(default_factory=list)


def dry_run_dental_migration(
    *,
    project_root: Path,
    global_palace_path: Path,
    wing_name: str,
) -> MigrationSurvey:
    """Survey what the migration would do for a project.

    Args:
        project_root: e.g. D:/Projects/Active/DentalClinic-WebApp
        global_palace_path: ~/.mempalace/palace
        wing_name: the wing inside the global palace to migrate (e.g.
            "DentalClinic-WebApp")

    """
    survey = MigrationSurvey(
        project_root=project_root,
        local_palace_path=project_root / ".MEMORY" / "palace",
        mcp_json_path=project_root / ".mcp.json",
        settings_local_path=project_root / ".claude" / "settings.local.json",
    )

    # Check global palace for drawers in this wing
    try:
        from mempalace.palace import get_collection

        col = get_collection(str(global_palace_path), create=False)
        if col is not None:
            result = col.get(
                where={"wing": wing_name},
                limit=10000,
                include=["metadatas"],
            )
            metas = (
                result.get("metadatas")
                if isinstance(result, dict)
                else getattr(result, "metadatas", [])
            )
            survey.drawer_count_in_global = len(metas or [])
            survey.has_global_palace_drawers = survey.drawer_count_in_global > 0
            survey.wing_in_global_palace = wing_name
    except Exception:
        pass

    # Check local palace
    survey.has_local_palace = survey.local_palace_path.is_dir()

    # Inspect .mcp.json
    if survey.mcp_json_path.is_file():
        try:
            config = json.loads(survey.mcp_json_path.read_text())
            servers = config.get("mcpServers", {})
            survey.mcp_json_has_mempalace_entry = "mempalace" in servers
            survey.mcp_json_has_aidocs_entry = "aidocs" in servers
        except (json.JSONDecodeError, OSError):
            pass

    # Inspect settings.local.json hooks
    if survey.settings_local_path.is_file():
        try:
            settings = json.loads(survey.settings_local_path.read_text())
            hooks = settings.get("hooks", {})
            for hook_kind, hook_list in hooks.items():
                for hook_entry in hook_list:
                    for h in hook_entry.get("hooks", []):
                        cmd = h.get("command", "")
                        if "mempalace" in cmd and "--palace" not in cmd:
                            survey.hooks_missing_palace_propagation.append(
                                f"{hook_kind}: {cmd[:80]}",
                            )
        except (json.JSONDecodeError, OSError):
            pass

    return survey


def generate_mcp_json_patch(
    *,
    current_config: dict,
    drop_mempalace_entry: bool = True,
    ensure_aidocs_entry: dict | None = None,
) -> dict:
    """Produce the Phase F target .mcp.json shape.

    AIDOCS-managed projects MUST have one aidocs entry; no separate
    mempalace entry. Per RFC-4 §4 Golden test #3.

    Args:
        current_config: parsed contents of existing .mcp.json
        drop_mempalace_entry: if True, remove the mempalace server entry
        ensure_aidocs_entry: if provided, install/override the aidocs
            entry with these args

    """
    new_config = json.loads(json.dumps(current_config))  # deep copy
    servers = new_config.setdefault("mcpServers", {})

    if drop_mempalace_entry and "mempalace" in servers:
        del servers["mempalace"]

    if ensure_aidocs_entry is not None:
        servers["aidocs"] = ensure_aidocs_entry

    return new_config


def generate_hook_patch(
    *,
    current_settings: dict,
    palace_path: Path,
) -> dict:
    """Produce a patched settings.local.json with --palace propagation
    on every mempalace-invoking hook command.
    """
    new_settings = json.loads(json.dumps(current_settings))
    hooks = new_settings.setdefault("hooks", {})

    for hook_kind, hook_list in hooks.items():
        for hook_entry in hook_list:
            for h in hook_entry.get("hooks", []):
                cmd = h.get("command", "")
                if "mempalace" in cmd and "--palace" not in cmd:
                    # Insert --palace argument after "mempalace"
                    # Replace "mempalace " or "mempalace.cli" or similar
                    h["command"] = _insert_palace_arg(cmd, palace_path)

    return new_settings


def _insert_palace_arg(cmd: str, palace_path: Path) -> str:
    """Insert ``--palace <path>`` into a hook command line.

    Heuristic: place it right after the first "mempalace" token. Handles
    both ``mempalace hook run...`` and ``python -m mempalace ...`` shapes.
    """
    palace_str = str(palace_path)
    if " hook " in cmd:
        return cmd.replace(" hook ", f' --palace "{palace_str}" hook ', 1)
    if "-m mempalace " in cmd:
        return cmd.replace("-m mempalace ", f'-m mempalace --palace "{palace_str}" ', 1)
    # Fallback: append
    return f'{cmd} --palace "{palace_str}"'


def survey_summary(survey: MigrationSurvey) -> str:
    """Human-readable summary of a MigrationSurvey."""
    lines = [
        f"Migration survey for {survey.project_root}:",
        (f"  Local palace at {survey.local_palace_path}: "
        f"{'EXISTS' if survey.has_local_palace else 'MISSING (will be created)'}"),
        (f"  Drawers in global palace under wing='{survey.wing_in_global_palace}': "
        f"{survey.drawer_count_in_global}"),
        f"  .mcp.json has mempalace entry (to remove): {survey.mcp_json_has_mempalace_entry}",
        f"  .mcp.json has aidocs entry: {survey.mcp_json_has_aidocs_entry}",
        f"  Hooks missing --palace propagation: {len(survey.hooks_missing_palace_propagation)}",
    ]
    if survey.hooks_missing_palace_propagation:
        lines.append("  Hooks needing patch:")
        for h in survey.hooks_missing_palace_propagation:
            lines.append(f"    - {h}")
    return "\n".join(lines)
