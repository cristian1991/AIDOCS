"""AIDOCS install manifest — tracks installed file checksums for smart updates.

Three-way merge logic:
  - source_hash: checksum of the file in the AIDOCS distribution
  - installed_hash: checksum when we last installed it
  - current_hash: checksum of the file on disk right now

Decision matrix:
  source == installed == current → skip (nothing changed)
  source != installed, current == installed → update (AIDOCS changed, user didn't touch)
  source == installed, current != installed → skip (user customized, AIDOCS didn't change)
  source != installed, current != installed → conflict (both changed) → backup + warn, don't overwrite
  file missing on disk → install fresh
  file new in source → install fresh
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


MANIFEST_PATH = Path.home() / ".aidocs" / "manifest.json"


@dataclass
class FileAction:
    path: str
    action: str  # "install", "update", "skip_unchanged", "skip_user_modified", "conflict", "remove"
    reason: str


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def load_manifest() -> dict[str, str]:
    """Load the installed manifest: {relative_key: hash_at_install_time}."""
    if not MANIFEST_PATH.is_file():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_manifest(manifest: dict[str, str]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def plan_file_install(
    source: Path,
    target: Path,
    manifest_key: str,
    manifest: dict[str, str],
) -> FileAction:
    """Decide what to do with a single file."""
    source_hash = file_hash(source)
    if source_hash is None:
        return FileAction(manifest_key, "skip_unchanged", "source file missing")

    installed_hash = manifest.get(manifest_key)
    current_hash = file_hash(target)

    if current_hash is None:
        return FileAction(manifest_key, "install", "new file")

    if installed_hash is None:
        # First install with manifest — treat existing file as user-owned
        if source_hash == current_hash:
            return FileAction(manifest_key, "skip_unchanged", "already matches source")
        return FileAction(manifest_key, "conflict", "pre-manifest file exists, may be user-customized")

    if source_hash == installed_hash == current_hash:
        return FileAction(manifest_key, "skip_unchanged", "nothing changed")

    if source_hash != installed_hash and current_hash == installed_hash:
        return FileAction(manifest_key, "update", "AIDOCS updated, user hasn't modified")

    if source_hash == installed_hash and current_hash != installed_hash:
        return FileAction(manifest_key, "skip_user_modified", "user customized, AIDOCS unchanged")

    if source_hash != installed_hash and current_hash != installed_hash:
        return FileAction(manifest_key, "conflict", "both AIDOCS and user modified")

    return FileAction(manifest_key, "skip_unchanged", "no action needed")


def execute_file_install(
    source: Path,
    target: Path,
    action: FileAction,
    manifest: dict[str, str],
) -> str:
    """Execute a planned file action. Returns a human-readable status line."""
    if action.action == "install":
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy(source, target)
        manifest[action.path] = file_hash(source) or ""
        return f"  + {action.path} (installed)"

    if action.action == "update":
        backup = Path(str(target) + ".backup")
        if target.is_file():
            _copy(target, backup)
        _copy(source, target)
        manifest[action.path] = file_hash(source) or ""
        return f"  ~ {action.path} (updated, backup saved)"

    if action.action == "conflict":
        backup = Path(str(target) + ".backup")
        if target.is_file():
            _copy(target, backup)
        _copy(source, target)
        manifest[action.path] = file_hash(source) or ""
        return f"  ! {action.path} (CONFLICT — your version saved as .backup, review and merge manually)"

    if action.action == "skip_user_modified":
        return f"  = {action.path} (preserved — your customizations kept)"

    if action.action == "skip_unchanged":
        return f"  = {action.path} (unchanged)"

    return f"  ? {action.path} ({action.action}: {action.reason})"


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
