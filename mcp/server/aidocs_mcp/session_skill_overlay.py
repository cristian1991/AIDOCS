"""Per-session skill overlay — Layer 3 workflow-extension slice 3.

The global skill registry ships a fixed set of active skills per project.
Some sessions benefit from different defaults: one session drives
strict-tdd work, another works through caveman debugging, a third
disables everything for a UI polish pass. Forcing the global set to
cover every scenario dilutes it.

This module adds per-session overlays: `enable(sid, skill)` /
`disable(sid, skill)` / `reset(sid)` produce a session-scoped preference
that the skill resolver merges on top of the global set. Missing
session → global set passes through untouched.

Kept as a standalone in-memory store so tests can exercise the merge
logic without reaching into session_store; a Phase 2 task will promote
the storage to sqlite alongside session_skills_set.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionSkillOverlay:
    """Per-session skill preferences.

    enabled: skills force-on for this session even if globally off.
    disabled: skills force-off for this session even if globally on.
    A skill CAN appear in both sets transiently — `resolve_active`
    treats `disabled` as the winner (operator intent: opt-out wins
    over opt-in to fail safe).
    """

    session_id: str
    enabled: set[str] = field(default_factory=set)
    disabled: set[str] = field(default_factory=set)


class SessionSkillOverlayRegistry:
    """In-memory store keyed by session_id.

    Designed to be trivially wrappable by a persistent store in Phase 2
    — every method is a pure read/write over the `_overlays` dict with
    no implicit persistence. Swap the dict for a sqlite-backed mapping
    and semantics stay identical.
    """

    def __init__(self) -> None:
        self._overlays: dict[str, SessionSkillOverlay] = {}

    def enable(self, session_id: str, skill: str) -> SessionSkillOverlay:
        """Force a skill ON for the session. Wins over global disable
        but loses to an explicit disable() on the same session.
        """
        sid = self._normalize_sid(session_id)
        name = self._normalize_skill(skill)
        overlay = self._overlays.setdefault(
            sid,
            SessionSkillOverlay(session_id=sid),
        )
        overlay.enabled.add(name)
        overlay.disabled.discard(name)
        return overlay

    def disable(self, session_id: str, skill: str) -> SessionSkillOverlay:
        """Force a skill OFF for the session. Always wins over enable;
        operator opt-out beats opt-in to fail safe.
        """
        sid = self._normalize_sid(session_id)
        name = self._normalize_skill(skill)
        overlay = self._overlays.setdefault(
            sid,
            SessionSkillOverlay(session_id=sid),
        )
        overlay.disabled.add(name)
        overlay.enabled.discard(name)
        return overlay

    def reset(self, session_id: str) -> bool:
        """Drop every overlay for the session; global defaults restored.
        Returns True iff the session had overlays to drop.
        """
        sid = self._normalize_sid(session_id)
        return self._overlays.pop(sid, None) is not None

    def get(self, session_id: str) -> SessionSkillOverlay | None:
        """Look up the overlay for a session. None iff no overlay set."""
        return self._overlays.get(self._normalize_sid(session_id))

    def resolve_active(
        self,
        session_id: str | None,
        global_skills: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    ) -> frozenset[str]:
        """Compose the effective active-skill set for a session.

        Precedence (highest wins):
          1. session disable → skill OFF
          2. session enable → skill ON
          3. global_skills → skill ON iff present

        session_id=None returns the global set unchanged (callers who
        don't track per-session context get the old behavior).
        """
        base = {self._normalize_skill(s) for s in global_skills}
        if session_id is None:
            return frozenset(base)
        overlay = self.get(session_id)
        if overlay is None:
            return frozenset(base)
        return frozenset(
            (base | overlay.enabled) - overlay.disabled,
        )

    def all_session_ids(self) -> tuple[str, ...]:
        """Sorted list of every session with an overlay. Diagnostic
        helper for dashboards that want to surface "which sessions have
        custom skill preferences?
        """
        return tuple(sorted(self._overlays))

    def clear(self) -> None:
        """Wipe every overlay. Test-only helper."""
        self._overlays.clear()

    # ── internals ──

    @staticmethod
    def _normalize_sid(session_id: str) -> str:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id must be a non-empty string")
        return sid

    @staticmethod
    def _normalize_skill(skill: str) -> str:
        name = str(skill or "").strip().lower()
        if not name:
            raise ValueError("skill name must be a non-empty string")
        return name


# Process-wide singleton. Callers that want per-test isolation use
# `global_registry().clear()`; production code treats it as a shared
# registry so enable/disable from any tool surface immediately affects
# every subsequent get_selected_skills call.
_GLOBAL_REGISTRY: SessionSkillOverlayRegistry | None = None


def global_registry() -> SessionSkillOverlayRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = SessionSkillOverlayRegistry()
    return _GLOBAL_REGISTRY
