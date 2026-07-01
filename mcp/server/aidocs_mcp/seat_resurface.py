"""Compaction re-surface for an OCCUPIED conductor seat (#225).

After compaction `agent_memory_epoch` rotates (PostCompact), so once-per-epoch
payloads re-fire — the SAME seam helper skills use (helper_skill_injector). This
re-surfaces, LEAN, what the seat needs to keep working after a compaction: a
POINTER to the role, the tiered law, and the operating checklist — loaded on
demand via ai_skill, never dumped — plus any skill/soul granted this session.

Two leanness controls, both per the Empire's directive (2026-07-01):
  - the soul itself is NEVER dumped (sovereign); only a pointer surfaces, and
    only when the seat already holds a grant;
  - a context-size filter drops to a single-line pointer for small-window agents.

Fail-closed-quiet: any exception returns [] rather than breaking the prompt build.
"""

from __future__ import annotations

from pathlib import Path

# Below this context window (tokens), emit only the one-line pointer.
_SMALL_WINDOW = 60000
_MARKER = "seat-resurface:head-conductor"


def _full_block(granted: tuple[str, ...]) -> str:
    lines = [
        "You hold the head-conductor seat. Reload your standing context on demand "
        "(this re-surfaces after each compaction):",
        "- Role + duties: ai_skill('head-conductor')",
        "- Law — cross-project: ai_skill('empire-doctrine'); project: ai_skill('king-doctrine')",
        "- Operating checklist: scratch/co-co reports/120%.md (+ v4.md)",
        "- Your soul (opened only by your word, never dumped): ai_soul('head-conductor-soul')",
    ]
    for gid in granted:
        lines.append(f"- Granted this session: ai_skill('{gid}')")
    return "<aidocs-seat-resurface>\n" + "\n".join(lines) + "\n</aidocs-seat-resurface>"


def _lean_block(granted: tuple[str, ...]) -> str:
    g = ("; granted: " + ", ".join(f"ai_skill('{x}')" for x in granted)) if granted else ""
    return (
        "<aidocs-seat-resurface>head-conductor seat — reload on demand: "
        "ai_skill('head-conductor'), ai_skill('empire-doctrine'), ai_skill('king-doctrine'), "
        "120%.md, ai_soul('head-conductor-soul')" + g + "</aidocs-seat-resurface>"
    )


def render_seat_resurface(granted_skill_ids: tuple[str, ...] = (), context_window: int = 0) -> str:
    """The block text (no dedup) — pure + testable. Lean when the window is small."""
    granted = tuple(dict.fromkeys(g for g in granted_skill_ids if g))  # dedup, keep order
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
    """One LEAN re-surface block for an occupied seat, once per epoch (re-fires
    after compaction rotates the epoch). Empty when the seat is not occupied."""
    if not occupied_seat:
        return []
    try:
        from .helper_skill_injector import _resolve_epoch
        from .protected_file_registry_store import ProtectedFileRegistryStore

        granted = tuple(dict.fromkeys(g for g in granted_skill_ids if g))
        small = 0 < context_window < _SMALL_WINDOW
        block = render_seat_resurface(granted, context_window)
        # Marker carries shape + grant-set so a size change or a NEW grant re-emits.
        marker = f"{_MARKER}:{'lean' if small else 'full'}:{'-'.join(granted) or 'none'}"
        store = ProtectedFileRegistryStore()
        epoch = _resolve_epoch(project_root, host_kind=host_kind, host_session_id=host_session_id)
        if epoch and store.was_banner_shown(project_root, cli_session_id=epoch, dnt_id=marker):
            return []
        if epoch:
            store.mark_banner_shown(project_root, cli_session_id=epoch, dnt_id=marker)
        return [block]
    except Exception:
        return []
