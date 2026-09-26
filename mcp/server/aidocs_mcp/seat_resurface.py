"""Compaction re-surface for an OCCUPIED conductor seat (#225).

After compaction `agent_memory_epoch` rotates (PostCompact), so once-per-epoch
payloads re-fire — the SAME seam helper skills use (helper_skill_injector). This
re-surfaces what the seat needs to keep working after a compaction.

CONTRACT MOVED 2026-09-12 (operator ruling): the seat's ROLE BODY re-surfaces
once per compaction epoch — the body, not a pointer. A compacted conductor
that has lost its role is not conducting, and the role door is mode-gated, so
a pointer asked the seat to remember to go and find out who it is. The tiered
LAW and the sovereign SOUL stay POINTERS (the soul is never dumped), as do
lean identifiers for resources previously opened in this host conversation.

WHICH seat is resolved, never assumed (DRT-05): the caller passes the role the
durable seat map holds for this host session; an unmapped caller — a lane
worker, an unregistered agent — is not a seat and gets nothing.
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

# Below this context window (tokens), emit the one-line pointer form.
# The ROLE BODY is emitted at BOTH sizes: the operator prefers the role
# present over the prompt being short (ruling 2026-09-12), and the body is
# never truncated mid-sentence — leanness applies to the POINTERS only.
_SMALL_WINDOW = 60000
_MARKER = "seat-resurface"

SEAT_HEAD = "head-conductor"
SEAT_CO = "co-conductor"

# What the durable seat map (msg_role_map) calls each seat → the seat id.
# Anything else — a lane worker, an unregistered agent, an empty map — is NOT
# a seat and re-surfaces nothing (DRT-05: occupancy is resolved, never assumed).
_SEAT_ALIASES: dict[str, str] = {
    "conductor": SEAT_HEAD,
    "head-conductor": SEAT_HEAD,
    "headconductor": SEAT_HEAD,
    "co-conductor": SEAT_CO,
    "coconductor": SEAT_CO,
}


def resolve_seat(occupied_seat: object) -> str | None:
    """Map a caller's occupancy signal to a seat id, or None for no seat.

    ``True`` stays the head seat for non-hook callers and tests that state
    occupancy directly; a role string ('conductor' / 'co_conductor' from
    msg_role_map) resolves to its seat; everything else is not a seat.
    """
    if occupied_seat is True:
        return SEAT_HEAD
    if not occupied_seat or occupied_seat is False:
        return None
    key = str(occupied_seat).strip().lower().replace("_", "-")
    return _SEAT_ALIASES.get(key)


def seat_role_body(seat: str) -> str:
    """The seat's role BODY — ONE home, one copy.

    Registry row wins when an operator inscribed one (#479 operator-wins);
    otherwise the bundled role scroll. Same resolver the seat-entry payload
    uses, so a compaction never re-surfaces a different role than seat entry
    handed out.
    """
    from . import conductor_doctrine as _cd

    if seat == SEAT_CO:
        return _cd.co_conductor_role_text()
    return _cd.head_conductor_role_text()


def _resource_pointer(encoded: str) -> str:
    """Render a typed pointer; bare IDs remain public-skill compatible."""
    raw = str(encoded or "").strip()
    if raw.startswith("soul:"):
        return f"ai_soul('{raw[5:]}')"
    if raw.startswith("skill:"):
        raw = raw[6:]
    return f"ai_skill('{raw}')"


def _full_block(granted: tuple[str, ...], seat: str, role_text: str) -> str:
    lines = [
        (f"You hold the {seat} seat. Your ROLE, re-surfaced for this compaction "
        "epoch (the law and your soul stay pointers):"),
        "",
        role_text,
        "",
        "Reload the rest on demand:",
        # DRT-04: role scrolls are mode-gated — the public ai_skill door
        # refuses 'head-conductor' by design. The door that SERVES the role
        # is seat entry; it is a secondary pointer now that the body itself
        # re-surfaces (CONTRACT MOVED 2026-09-12, operator ruling).
        "- Role door (re-dumps the text above): ai_seat(mode='enter')",
        ("- Law — cross-project: ai_skill('empire-doctrine'); "
        "project: ai_skill('aidocs-doctrine')"),
        ("- Field manual: scratch/co-co reports/v5.md "
        "(retired curriculum: archive/)"),
        "- Runbooks (the HOW): ai_skill('runbook-seat-succession')",
        (f"- Your soul (opened only by your word, never dumped): "
        f"ai_soul('{seat}-soul')"),
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


def _lean_block(granted: tuple[str, ...], seat: str, role_text: str) -> str:
    granted_part = (
        "; resurface: "
        + ", ".join(_resource_pointer(item) for item in granted)
        if granted
        else ""
    )
    return (
        f"<aidocs-seat-resurface>{seat} seat — reload on demand: "
        "ai_seat(mode='enter') for the role door, ai_skill('empire-doctrine'), "
        f"ai_skill('aidocs-doctrine'), v5.md, ai_soul('{seat}-soul')"
        + granted_part
        + "\n"
        + role_text
        + "</aidocs-seat-resurface>"
    )


def render_seat_resurface(
    granted_skill_ids: tuple[str, ...] = (),
    context_window: int = 0,
    seat: str = SEAT_HEAD,
    role_text: str | None = None,
) -> str:
    """Return the seat block: the ROLE BODY plus deduplicated pointers.

    The pointer set goes lean for a small window; the body does not shrink.
    """
    granted = tuple(dict.fromkeys(g for g in granted_skill_ids if g))
    body = role_text if role_text is not None else seat_role_body(seat)
    small = 0 < context_window < _SMALL_WINDOW
    return (
        _lean_block(granted, seat, body)
        if small
        else _full_block(granted, seat, body)
    )


def maybe_seat_resurface_blocks(
    project_root: Path,
    *,
    occupied_seat: bool | str,
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
    seat = resolve_seat(occupied_seat)
    if seat is None:
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
                if item and item != f"soul:{seat}-soul"
            )
        )
        small = 0 < effective_window < _SMALL_WINDOW
        block = render_seat_resurface(granted, effective_window, seat=seat)
        # Shape + pointer set are part of the once-per-epoch marker. A newly
        # opened resource or a lean/full shape change re-emits immediately.
        # The SEAT is part of it too: head and co hold different roles, so one
        # must never suppress the other's re-surface within an epoch.
        pointer_signal = hashlib.sha256(
            "\0".join(granted).encode("utf-8")
        ).hexdigest()[:16]
        marker = (
            f"{_MARKER}:{seat}:{'lean' if small else 'full'}:"
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
