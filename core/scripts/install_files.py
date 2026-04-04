"""AIDOCS smart file installer — called by PS1/SH installers.

Usage: python install_files.py <project_root> <core_root> <target_dir> <file_category>

Categories:
  action_tokens  — copy action_tokens/*.yaml + *.toml to target_dir
  action_hooks   — copy action_hooks/*.toml to target_dir
  commands       — copy core/.commands/*.md to target_dir
  plugin         — copy core/plugins/aidocs.js + config to target_dir
"""

from __future__ import annotations

import sys
from pathlib import Path

from install_manifest import (
    FileAction,
    execute_file_install,
    load_manifest,
    plan_file_install,
    save_manifest,
)


def install_category(
    source_dir: Path,
    target_dir: Path,
    extensions: list[str],
    category: str,
) -> list[str]:
    """Install files from source_dir to target_dir using manifest-aware logic."""
    manifest = load_manifest()
    target_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    for ext in extensions:
        for source_file in sorted(source_dir.glob(f"*{ext}")):
            if not source_file.is_file():
                continue
            target_file = target_dir / source_file.name
            manifest_key = f"{category}/{source_file.name}"
            action = plan_file_install(source_file, target_file, manifest_key, manifest)
            line = execute_file_install(source_file, target_file, action, manifest)
            lines.append(line)

    save_manifest(manifest)
    return lines


def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: install_files.py <project_root> <core_root> <target_dir> <category>")
        sys.exit(1)

    project_root = Path(sys.argv[1])
    core_root = Path(sys.argv[2])
    target_dir = Path(sys.argv[3])
    category = sys.argv[4]

    if category == "action_tokens":
        source = project_root / "action_tokens"
        if not source.is_dir():
            source = project_root / "mcp" / "server" / "aidocs_mcp" / "action_tokens"
        lines = install_category(source, target_dir, [".yaml", ".toml"], "action_tokens")

    elif category == "action_hooks":
        source = project_root / "action_hooks"
        lines = install_category(source, target_dir, [".toml"], "action_hooks")

    elif category == "commands":
        source = core_root / ".commands"
        lines = install_category(source, target_dir, [".md"], "commands")

    elif category == "plugin":
        source = core_root / "plugins"
        lines = install_category(source, target_dir, [".js", ".json"], "plugin")

    else:
        print(f"Unknown category: {category}")
        sys.exit(1)

    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
