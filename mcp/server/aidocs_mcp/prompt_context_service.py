"""Shared, host-agnostic prompt-context builder.

Extracted from ``claude_hook`` (identity-spine audit, agents/2026-06-29): the
enforced-mode prompt context + tool-discovery hint + skill suggestions are host-
AGNOSTIC — they operate on route / prompt_payload / project_root, not on anything
Claude-Code-specific. The ONLY host inputs are ``host_kind`` + ``host_session_id``.

Thin host adapters (claude_hook, codex, opencode, openai_agents) construct this
with their runtime and call ``build_enforced_context(..., host_kind=<theirs>,
host_session_id=<theirs>)`` — no per-host copy of the surfacing logic. claude_hook
keeps 1-line delegate methods so its existing test surface (which calls e.g.
``_infer_skill_suggestions``) stays green.

Behavior-preserving move: bodies are copied verbatim from claude_hook, with the
hardcoded ``host_kind="claude_code"`` and the ``cli_session_id`` kwarg replaced by
the parameterized ``host_kind`` / ``host_session_id``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class PromptContextBuilder:
    """Host-agnostic UPS prompt-context assembly. One instance per call is fine
    (it only holds a runtime reference)."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def build_enforced_context(
        self,
        action_kind: str,
        session_id: str,
        route: dict[str, object],
        prompt_payload: dict[str, object],
        prompt: str = "",
        project_root: Path | None = None,
        *,
        host_kind: str = "",
        host_session_id: str = "",
    ) -> str:
        """Minimal context for hosts with PreToolUse gate enforcement.

        The static "AIDOCS-managed mode active" preamble was removed 2026-04-28
        (UserPromptSubmit IS the auto-bootstrap). Dynamic action + session label
        still fire — they vary per prompt and carry actual signal.
        """
        parts = [
            f"AIDOCS managed. Action: `{action_kind}`.",
        ]
        if session_id:
            parts.append(f"Session: `{session_id}`.")

        for line in self._tool_discovery_hint(
            prompt,
            project_root=project_root,
            action_kind=action_kind,
            host_kind=host_kind,
            host_session_id=host_session_id,
        ):
            parts.append(line)

        # Domain hints only — these are useful even with gates
        classification = (
            prompt_payload.get("prompt_state")
            if isinstance(prompt_payload.get("prompt_state"), dict)
            else {}
        )
        domain_hints = classification.get("domain_hints")
        if isinstance(domain_hints, list) and domain_hints:
            hint_parts = []
            for hint in domain_hints:
                if isinstance(hint, dict) and hint.get("recommended_tools"):
                    hint_parts.append(f"{hint['domain']}: {hint['recommended_tools']}")
            if hint_parts:
                parts.append(f"Domain tools: {'; '.join(hint_parts)}.")

        # Triggered skill guidance — inject full content when a skill matches the prompt
        skill_state = (
            prompt_payload.get("skill_state")
            if isinstance(prompt_payload.get("skill_state"), dict)
            else {}
        )
        prompt_activation = (
            skill_state.get("prompt_activation")
            if isinstance(skill_state.get("prompt_activation"), dict)
            else {}
        )
        session_snapshot = (
            skill_state.get("session_snapshot")
            if isinstance(skill_state.get("session_snapshot"), dict)
            else {}
        )
        helper_skill_guidance = (
            prompt_activation.get("helper_skill_guidance")
            if isinstance(prompt_activation.get("helper_skill_guidance"), list)
            else []
        )
        if not helper_skill_guidance:
            helper_skill_guidance = (
                session_snapshot.get("helper_skill_guidance")
                if isinstance(session_snapshot.get("helper_skill_guidance"), list)
                else []
            )
        # Once-per-epoch dedup (mirrors DNT banner contract). On compaction the
        # epoch rotates and the next prompt re-emits.
        if project_root is not None:
            from .helper_skill_injector import maybe_helper_skill_blocks

            for block in maybe_helper_skill_blocks(
                project_root,
                helper_skill_guidance,
                host_kind=host_kind,
                host_session_id=host_session_id,
            ):
                parts.append(block)

            # Seat re-surface (#225): the occupied conductor seat reloads its role
            # + tiered law as LEAN pointers once per epoch (re-fires after compaction).
            from .seat_resurface import maybe_seat_resurface_blocks

            for block in maybe_seat_resurface_blocks(
                project_root,
                occupied_seat=True,  # the managed UPS path is the seat-holder's prompt
                host_kind=host_kind,
                host_session_id=host_session_id,
            ):
                parts.append(block)

        # Active skill names
        active_skills = (
            prompt_activation.get("active_skills")
            if isinstance(prompt_activation.get("active_skills"), list)
            else (
                session_snapshot.get("active_skills")
                if isinstance(session_snapshot.get("active_skills"), list)
                else []
            )
        )
        if active_skills:
            parts.append(f"Active skills: {', '.join(f'`{s}`' for s in active_skills if s)}.")

        # NLP skill suggestion — infer additional skills from prompt content.
        try:
            if project_root is not None and prompt:
                suggested = self._infer_skill_suggestions(
                    prompt,
                    project_root,
                    already_active=set(active_skills or []),
                )
                if suggested:
                    parts.append(f"Suggested skills: {', '.join(f'`{s}`' for s in suggested)}.")
        except Exception:
            pass

        # Lifecycle nudge — still useful as a reminder
        lifecycle_state = (
            prompt_payload.get("lifecycle_state")
            if isinstance(prompt_payload.get("lifecycle_state"), dict)
            else {}
        )
        lifecycle_nudge = self._lifecycle_followthrough_nudge(lifecycle_state)
        if lifecycle_nudge:
            parts.append(lifecycle_nudge)

        return " ".join(parts)

    def _tool_discovery_hint(
        self,
        prompt: str,
        project_root: Path | None = None,
        action_kind: str | None = None,
        *,
        host_kind: str = "",
        host_session_id: str = "",
    ) -> list[str]:
        """Surface AIDOCS tools + project memory relevant to the prompt. Delegates
        the policy decision to the host-agnostic ReadMemorySurfacer."""
        if not prompt or not prompt.strip():
            return []
        from .read_memory_surfacer import ReadMemorySurfacer

        surfacer = ReadMemorySurfacer(self.runtime)
        used = self._tools_used_in_session(project_root) if project_root else set()
        already_surfaced = surfacer.sticky_surfaced_tools(project_root) if project_root else set()
        result = surfacer.surface_on_prompt(
            prompt=prompt,
            project_root=project_root,
            action_kind=action_kind,
            already_used_tools=used,
            already_surfaced_tools=already_surfaced,
            host_kind=host_kind,
            host_session_id=host_session_id or "",
        )
        return list(result.advisory_lines)

    def _tools_used_in_session(self, project_root: Path) -> set[str]:
        """Tool names already used in the current managed session (native_tool_use
        rows). Unmanaged sessions return empty; exceptions suppressed."""
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            if not managed.get("active"):
                return set()
            session_id = str(managed.get("session_id") or "")
            if not session_id:
                return set()
            events = (
                self.runtime.hub.execution.list_events(
                    project_root,
                    session_id=session_id,
                    limit=200,
                )
                or []
            )
            used: set[str] = set()
            for ev in events:
                if ev.get("event_kind") != "native_tool_use":
                    continue
                name = str(ev.get("capability_name") or "").strip()
                if name:
                    used.add(name)
            return used
        except Exception:
            return set()

    def _infer_skill_suggestions(
        self,
        prompt: str,
        project_root: Path,
        *,
        already_active: set[str],
    ) -> list[str]:
        """Skill names the prompt suggests activating, beyond active_skills.
        NLP-FREE literal word-overlap against configured triggers. [] on error."""
        from .aidocs_nlp.consumers.skill_trigger import (
            detect_skill_triggers_literal,
            load_skill_trigger_tokens,
        )

        try:
            triggers = load_skill_trigger_tokens()
            if not triggers:
                return []
            hits = detect_skill_triggers_literal(prompt, triggers, top_n=5)
        except Exception:
            return []
        out: list[str] = []
        for hit in hits:
            if hit.skill_name in already_active:
                continue
            out.append(hit.skill_name)
        return out

    def _lifecycle_followthrough_nudge(self, lifecycle_state: dict[str, object]) -> str:
        from .lifecycle_service import LifecycleService

        return LifecycleService(self.runtime).build_followthrough_nudge(lifecycle_state)
