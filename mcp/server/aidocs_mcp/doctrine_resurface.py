"""Doctrine survives compaction (#316 property 1).

Problem: an agent reads a doctrine scroll via ai_skill, then the host
compacts the transcript — the scroll text is gone and NOTHING re-surfaces
it. The seat-resurface block (#225) covers only the occupied conductor
seat's fixed pointer set; arbitrary scrolls read this conversation were
silently lost.

Mechanism (rides the existing epoch machinery, no new tables):

- ``record_scroll_read`` is called from the ai_skill read path. It writes
  two markers into the epoch-keyed ``dnt_banners_shown`` store:
    * ``scrollread:<skill_id>`` keyed on the STABLE ``agent_context_id``
      (survives compaction — the cross-epoch read ledger);
    * ``doctrine-resurface:<skill_id>`` keyed on the CURRENT
      ``agent_memory_epoch`` (suppresses a redundant pointer in the very
      epoch the scroll was just read in — the text is still in context).
- ``maybe_doctrine_resurface_blocks`` runs on the UserPromptSubmit
  context build. After compaction rotates the epoch, the suppress marker
  no longer matches, so every scroll in the read ledger re-surfaces as a
  LEAN ai_skill POINTER (never the body), once per epoch.

Fail-quiet everywhere: no resolvable host session, or any store error,
means record is a no-op and surfacing returns [] — a memory nicety must
never break a prompt build or a skill read.
"""

from __future__ import annotations

from pathlib import Path

_READ_LEDGER_PREFIX = "scrollread:"
_RESURFACE_PREFIX = "doctrine-resurface:"


def render_doctrine_resurface(skill_ids: tuple[str, ...]) -> str:
    """Pure pointer block — never the scroll text."""
    ids = tuple(dict.fromkeys(s for s in skill_ids if s))
    if not ids:
        return ""
    pointers = ", ".join(f"ai_skill('{s}')" for s in ids)
    return (
        "<aidocs-doctrine-resurface>compaction shrank your context — "
        "doctrine you had read this conversation, reload on demand: "
        + pointers
        + "</aidocs-doctrine-resurface>"
    )


def _resolve_ids(
    project_root: Path,
    *,
    host_kind: str | None,
    host_session_id: str | None,
) -> tuple[str, str]:
    """(agent_context_id, current_epoch) — ("", "") when unresolvable."""
    sid = (host_session_id or "").strip()
    if not sid:
        try:
            from .mcp_server_runtime_helpers import current_calling_host_session_id

            sid = (current_calling_host_session_id() or "").strip()
        except Exception:
            sid = ""
    if not sid:
        return "", ""
    # #587-A: ask the ONE authority first. This used to jump straight to the
    # `_detect_host_kind` shim, i.e. the MCP server's own environment — a
    # resolution that skipped both the request stamp and (now) the durable
    # record, so doctrine dedup could key on a different identity than every
    # other surface for the same session.
    try:
        from .agent_memory_epoch import resolve_host_identity

        kind = resolve_host_identity(
            host_kind=host_kind,
            host_session_id=sid,
            project_root=project_root,
        )[0]
    except Exception:
        kind = (host_kind or "").strip()
    if not kind:
        # Retained rung (not a new one): the shim still answers for a host that
        # has never been stamped or recorded, and it is the seam the worker-lane
        # tests patch. It returns LEGACY_UNKNOWN_HOST_KIND rather than "", which
        # keeps this surface's fail-OPEN dedup byte-identical — tightening that
        # is #525's job, not this one's (see resolve_epoch's docstring).
        try:
            from .read_memory_surfacer import _detect_host_kind

            kind = _detect_host_kind()
        except Exception:
            from .agent_memory_epoch import LEGACY_UNKNOWN_HOST_KIND

            kind = LEGACY_UNKNOWN_HOST_KIND
    try:
        from .agent_memory_epoch import current_epoch, derive_agent_context_id

        ctx = derive_agent_context_id(
            host_kind=kind,
            project_root=project_root,
            host_session_id=sid,
        )
        epoch = current_epoch(project_root, host_kind=kind, host_session_id=sid)
        return ctx, epoch
    except Exception:
        return "", ""


def record_scroll_read(
    project_root: Path,
    skill_id: str,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> None:
    """Record that this agent context has read a scroll. Fail-quiet."""
    sid = (skill_id or "").strip()
    if not sid:
        return
    try:
        ctx, epoch = _resolve_ids(
            project_root, host_kind=host_kind, host_session_id=host_session_id
        )
        if not ctx:
            return
        from .protected_file_registry_store import ProtectedFileRegistryStore

        store = ProtectedFileRegistryStore()
        # Cross-epoch read ledger, keyed on the stable agent_context_id.
        store.mark_banner_shown(
            project_root, epoch_id=ctx, dnt_id=_READ_LEDGER_PREFIX + sid
        )
        # Suppress the pointer within the epoch the scroll was read in.
        if epoch:
            store.mark_banner_shown(
                project_root, epoch_id=epoch, dnt_id=_RESURFACE_PREFIX + sid
            )
    except Exception:
        pass


def maybe_doctrine_resurface_blocks(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> list[str]:
    """One lean pointer block for scrolls read in a PREVIOUS epoch,
    once per epoch (re-fires after every compaction). [] on any error.
    """
    try:
        ctx, epoch = _resolve_ids(
            project_root, host_kind=host_kind, host_session_id=host_session_id
        )
        if not ctx or not epoch:
            return []
        from .protected_file_registry_store import ProtectedFileRegistryStore

        store = ProtectedFileRegistryStore()
        read_ids = tuple(
            marker[len(_READ_LEDGER_PREFIX):]
            for marker in store.list_banners_shown(
                project_root, epoch_id=ctx, prefix=_READ_LEDGER_PREFIX
            )
            if marker[len(_READ_LEDGER_PREFIX):]
        )
        due = tuple(
            s
            for s in read_ids
            if not store.was_banner_shown(
                project_root, epoch_id=epoch, dnt_id=_RESURFACE_PREFIX + s
            )
        )
        if not due:
            return []
        block = render_doctrine_resurface(due)
        for s in due:
            store.mark_banner_shown(
                project_root, epoch_id=epoch, dnt_id=_RESURFACE_PREFIX + s
            )
        return [block] if block else []
    except Exception:
        return []
