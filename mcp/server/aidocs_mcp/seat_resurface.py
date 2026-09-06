"""Compaction re-surface for an OCCUPIED conductor seat (#225).

After compaction `agent_memory_epoch` rotates (PostCompact), so once-per-epoch
payloads re-fire — the SAME seam helper skills use (helper_skill_injector). This
re-surfaces, LEAN, what the seat needs to keep working after a compaction: a
POINTER to the role, the tiered law, and the operating checklist — loaded on
demand via ai_skill, never dumped — plus lean identifiers for resources
previously opened in this host conversation.
Those pointers never imply current authority; every sovereign read is re-gated.

Two leanness controls, both per the Empire's directive (2026-07-01):
  - soul content is NEVER dumped; only a pointer may persist after a successful
    granted read, and using that pointer still requires fresh authority;
  - a host-profile context-size filter selects the one-line form for small windows.

Fail-closed-quiet: any exception returns [] rather than breaking the prompt build.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Below this context window (tokens), emit only the one-line pointer.
_SMALL_WINDOW = 60000
_MARKER = "seat-resurface:head-conductor"


def _resource_pointer(encoded: str) -> str:
    """Render a typed pointer; bare IDs remain public-skill compatible."""
    raw = str(encoded or "").strip()
    if raw.startswith("soul:"):
        return f"ai_soul('{raw[5:]}')"
    if raw.startswith("skill:"):
        raw = raw[6:]
    return f"ai_skill('{raw}')"


def _full_block(granted: tuple[str, ...]) -> str:
    lines = [
        ("You hold the head-conductor seat. Reload your standing context on demand "
        "(this re-surfaces after each compaction):"),
        "- Role + duties: ai_skill('head-conductor')",
        ("- Law — cross-project: ai_skill('empire-doctrine'); "
        "project: ai_skill('aidocs-doctrine')"),
        ("- Field manual: scratch/co-co reports/v5.md "
        "(retired curriculum: archive/)"),
        "- Runbooks (the HOW): ai_skill('runbook-seat-succession')",
        ("- Your soul (opened only by your word, never dumped): "
        "ai_soul('head-conductor-soul')"),
    ]
    for granted_id in granted:
        lines.append(
            f"- Resurface pointer: {_resource_pointer(granted_id)}"
        )
    return (
        "<aidocs-seat-resurface>\n"
        + "\n".join(lines)
        + "\n</aidocs-seat-resurface>"
    )


def _lean_block(granted: tuple[str, ...]) -> str:
    granted_part = (
        "; resurface: "
        + ", ".join(_resource_pointer(item) for item in granted)
        if granted
        else ""
    )
    return (
        "<aidocs-seat-resurface>head-conductor seat — reload on demand: "
        "ai_skill('head-conductor'), ai_skill('empire-doctrine'), "
        "ai_skill('aidocs-doctrine'), v5.md, "
        "ai_soul('head-conductor-soul')"
        + granted_part
        + "</aidocs-seat-resurface>"
    )


def render_seat_resurface(
    granted_skill_ids: tuple[str, ...] = (),
    context_window: int = 0,
) -> str:
    """Return pure, deduplicated pointer text; lean for a small window."""
    granted = tuple(dict.fromkeys(g for g in granted_skill_ids if g))
    small = 0 < context_window < _SMALL_WINDOW
    return _lean_block(granted) if small else _full_block(granted)


def maybe_seat_resurface_blocks(
    project_root: Path,
    *,
    occupied_seat: bool,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    granted_skill_ids: tuple[str, ...] = (),
    context_window: int = 0,
) -> list[str]:
    """Return the host-bound seat pointer block once per memory epoch.

    Explicit arguments remain useful for tests and non-hook hosts. Missing
    grant pointers and context-window metadata are resolved from the canonical
    host-session store. The store remembers identifiers only; it never grants
    soul access.
    """
    if not occupied_seat:
        return []
    try:
        from .helper_skill_injector import _resolve_epoch
        from .host_session_context_store import HostSessionContextStore
        from .protected_file_registry_store import ProtectedFileRegistryStore

        resolved_host_session_id = str(host_session_id or "").strip()
        if not resolved_host_session_id:
            try:
                from .mcp_server_runtime_helpers import (
                    current_calling_host_session_id,
                )

                resolved_host_session_id = str(
                    current_calling_host_session_id() or ""
                ).strip()
            except Exception:
                resolved_host_session_id = ""

        granted_items = [item for item in granted_skill_ids if item]
        try:
            effective_window = int(context_window or 0)
        except (TypeError, ValueError):
            effective_window = 0
        if resolved_host_session_id:
            host_store = HostSessionContextStore()
            granted_items.extend(
                host_store.list_pointers(
                    project_root,
                    host_session_id=resolved_host_session_id,
                    host_kind=str(host_kind or "").strip(),
                )
            )
            if effective_window <= 0:
                profile = host_store.get_profile(
                    project_root,
                    host_session_id=resolved_host_session_id,
                    host_kind=str(host_kind or "").strip(),
                )
                if profile:
                    effective_window = int(profile.get("context_window") or 0)

        granted = tuple(
            dict.fromkeys(
                item
                for item in granted_items
                if item and item != "soul:head-conductor-soul"
            )
        )
        small = 0 < effective_window < _SMALL_WINDOW
        block = render_seat_resurface(granted, effective_window)
        # Shape + pointer set are part of the once-per-epoch marker. A newly
        # opened resource or a lean/full shape change re-emits immediately.
        pointer_signal = hashlib.sha256(
            "\0".join(granted).encode("utf-8")
        ).hexdigest()[:16]
        marker = (
            f"{_MARKER}:{'lean' if small else 'full'}:"
            f"{pointer_signal if granted else 'none'}"
        )
        banner_store = ProtectedFileRegistryStore()
        epoch = _resolve_epoch(
            project_root,
            host_kind=host_kind,
            host_session_id=resolved_host_session_id,
        )
        if epoch and banner_store.was_banner_shown(
            project_root,
            epoch_id=epoch,
            dnt_id=marker,
        ):
            return []
        if epoch:
            banner_store.mark_banner_shown(
                project_root,
                epoch_id=epoch,
                dnt_id=marker,
            )
        return [block]
    except Exception:
        return []
