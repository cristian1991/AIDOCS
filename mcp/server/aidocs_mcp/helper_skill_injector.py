"""Helper skill injection — once-per-epoch dedup mirroring DNT banners.

Replaces the per-prompt re-injection that used to live inline at
claude_hook.py `_build_enforced_context` and the SessionStart handler.
The agent gets `<aidocs-skill>` content the first time a skill activates
in an epoch window, and silence on subsequent prompts within the same
window. On compaction, agent_memory_epoch rotates → the next prompt
re-emits — exactly the DNT banner contract.

Storage reuses `dnt_banners_shown` (epoch-keyed) with marker_id
``skill:<skill_id>`` — same table, same pattern, no new migration.
The `dnt_id` column is opaque text and the existing
`was_banner_shown`/`mark_banner_shown` helpers don't validate the
prefix; conflating skill markers with DNT markers is a deliberate
reuse, not a layering violation.

Failure modes fail-closed-quietly: any exception returns ``[]`` rather
than blowing up the prompt-context builder. Empty epoch (no resolvable
host_session_id) emits unconditionally — better noisy than silent on a
triggered skill.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from .protected_file_registry_store import ProtectedFileRegistryStore

_SKILL_MARKER_PREFIX = "skill:"


def _content_hash(content: str) -> str:
    """Short stable hash of skill content. Edits to a skill rotate this,
    so the marker key `skill:<id>:<hash>` becomes a different row in
    dnt_banners_shown and the dedup lookup misses → re-emit. Hot-reload
    for free, with no need to hook every skill-write path. 64 bits is
    plenty for a single project's skill set.
    """
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]


def _detect_host_kind() -> str:
    if os.environ.get("CLAUDE_CODE_VERSION", "").strip():
        return "claude_code"
    if os.environ.get("OPENCODE_VERSION", "").strip():
        return "opencode"
    return "unknown"


def _resolve_epoch(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> str:
    """Derive the current agent_memory_epoch.

    Caller may pass identity explicitly (claude_hook has it from the
    UPS payload) or fall back to the thread-local lookup the DNT
    injector uses. Returns "" when host_session_id is unresolvable —
    callers MUST emit unconditionally in that case.
    """
    sid = (host_session_id or "").strip()
    if not sid:
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        sid = (current_calling_host_session_id() or "").strip()
    if not sid:
        return ""
    kind = (host_kind or "").strip() or _detect_host_kind()
    from .agent_memory_epoch import current_epoch

    try:
        return current_epoch(
            project_root,
            host_kind=kind,
            host_session_id=sid,
        )
    except Exception:
        return ""


def _render_block(name: str, content: str) -> str:
    return f'<aidocs-skill name="{name}">\n{content}\n</aidocs-skill>'


def maybe_helper_skill_blocks(
    project_root: Path,
    guidance: list[dict[str, Any]] | None,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    limit: int = 2,
) -> list[str]:
    """Return rendered <aidocs-skill> blocks for skills not yet shown
    this epoch (capped at `limit`).

    Skills already shown in the current epoch are silently dropped — the
    agent retains the content via host context until compaction rotates
    the epoch. When epoch can't be resolved, blocks emit unconditionally.
    """
    if not guidance:
        return []
    try:
        store = ProtectedFileRegistryStore()
        epoch = _resolve_epoch(
            project_root,
            host_kind=host_kind,
            host_session_id=host_session_id,
        )
        blocks: list[str] = []
        for item in guidance:
            if len(blocks) >= max(0, limit):
                break
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            skill_id = str(item.get("skill_id") or item.get("name") or "").strip()
            if not skill_id:
                continue
            name = str(item.get("name") or skill_id).strip()
            marker = f"{_SKILL_MARKER_PREFIX}{skill_id}:{_content_hash(content)}"
            if epoch and store.was_banner_shown(
                project_root,
                epoch_id=epoch,
                dnt_id=marker,
            ):
                continue
            if epoch:
                store.mark_banner_shown(
                    project_root,
                    epoch_id=epoch,
                    dnt_id=marker,
                )
            blocks.append(_render_block(name, content))
        return blocks
    except Exception:
        return []
