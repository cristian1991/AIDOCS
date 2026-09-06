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

# S2 hook-core rip (2026-07-06): moved VERBATIM from
# ClaudeHookHandler._TOOL_FIRST_PREAMBLE / _ACTION_DIRECTIVES. claude_hook
# keeps class attrs aliasing these tables for back-compat.
TOOL_FIRST_PREAMBLE = (
    "AIDOCS indexed tools for code. Raw Read/Grep/Glob blocked. Widen query if empty."
)

ACTION_DIRECTIVES: dict[str, str] = {
    "write_memory": (
        "Use `memory_capture` with `target_hint` (workflow/coding-standards/security/project-state/user-profile). "
        "Do NOT write memory files manually."
    ),
    "task_begin": "Use `ai_task(mode='begin')` to register the task before starting work.",
    "task_complete": "Use `ai_task(mode='complete')` to finalize the task.",
    "task_update": "Use `ai_task(mode='update')` to record progress on the current task.",
    "trace": (
        'Function/method callers: `ai_find(query, mode="references")` or `ai_trace(query, mode="references")` (delegates to ai_find). '
        'Data/field lineage: `ai_trace(query, mode="field_flow")` — for DB and struct fields only; it will not return callers of a function. '
        'CSS rules: `ai_trace(query, mode="css_class")`. API↔UI: `ai_trace(query, mode="api_to_ui")`. '
        'DB trace: `ai_schema(query, mode="trace_path")`.'
    ),
    "understand": (
        '`ai_bundle(path, mode="file")` (structure) → `ai_find(query, mode="symbols")` (find symbol) → '
        "`ai_get_symbol_snippet` (read it). "
        "Precision: `ai_get_symbol_info(kind='signature')`, `ai_get_symbol_info(kind='constructor')`, `ai_get_symbol_info(kind='enum')`, `ai_get_symbol_info(kind='api')`. "
        'Broad: `ai_bundle(concept, mode="subsystem")`. DB: `ai_schema(name, mode="entity")`.'
    ),
    "ai_bundle": (
        '`ai_bundle(path, mode="context", session_id=...)` (session-guided) or '
        '`ai_bundle(path, mode="file")` (single file).'
    ),
    "edit": (
        "Flow: `ai_task(mode='begin')` → read with `ai_get_lines` or `ai_get_symbol_snippet` → write with ONE of: "
        "`ai_replace(mode='string')` (small exact-match edit), `ai_replace(mode='lines')` (line-range rewrite), or `ai_batch_edit` (multiple edits atomic, up to 20) → `ai_task(mode='complete')`. "
        "Do not use raw Edit or apply_patch for managed files. Do not chain two writers against overlapping regions in the same turn. "
        "Signature shortcuts before editing: `ai_get_symbol_info(kind='signature')`, `ai_get_symbol_info(kind='constructor')`, `ai_get_symbol_info(kind='enum')`. "
        'CSS: `ai_trace(class, mode="css_class")`. DB: `ai_schema(entity, mode="entity")`.'
    ),
    "test_heavy": (
        "If test/support code matters, re-run retrieval with test-inclusive indexing where the tool supports it. "
        "Then prefer: `ai_get_symbol_info(kind='api')` → `ai_get_symbol_info(kind='signature')s` → `ai_get_symbol_info(kind='constructors')` → `ai_get_symbol_info(kind='enum')` → `ai_get_symbol_info(kind='properties')`. "
        "Do not guess property names, constructor params, enum members, or service surfaces when the precision chain can confirm them first."
    ),
    "inspect": (
        '`ai_bundle(path, mode="file")` → `ai_get_dependencies` / '
        '`ai_find(query, mode="references")` → `ai_get_modules` (project boundaries). Read only after narrowing.'
    ),
    "read_error": (
        '`ai_find(symbol, mode="symbols")` (find it) → `ai_find(symbol, mode="references")` (trace) → '
        '`ai_get_symbol_snippet` (read method). DB: add `ai_schema(entity, mode="entity")`.'
    ),
    "investigate": (
        'Pick by target: known symbol name → `ai_find(name, mode="symbols")`; '
        "concept/type/class search → `ai_investigate(concept, depth=..., focus=...)` (symbol-ranked, favors types/classes/structs); "
        'architecture of a known file/module → `ai_bundle(path, mode="file"|"subsystem")`. '
        "These are alternatives, not a chain. Narrow hits with "
        '`ai_find(concept, mode="mutations"|"validation"|"policy"|"references")`.'
    ),
}


def render_suggested_skills(candidates: list[Any]) -> str:
    """Pure pointer block for inferred skill suggestions (#620).

    Mirrors ``doctrine_resurface.render_doctrine_resurface`` — the id AND the
    call that dereferences it, never the skill body. A suggestion the agent
    cannot dereference is noise; before this, the rail emitted bare backticked
    vocab keys with no address.

    Candidates whose ``skill_id`` is empty did not resolve against the skill
    catalog. They are NOT rendered with a retrieval call (drop-on-doubt: never
    hand out an address that 404s) — they still surface by name so the
    detector's advisory signal is not silently lost.
    """
    resolved: list[tuple[str, str]] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for cand in candidates or ():
        name = str(getattr(cand, "skill_name", "") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        handle = str(getattr(cand, "skill_id", "") or "").strip()
        if handle:
            resolved.append((name, handle))
        else:
            unresolved.append(name)
    parts: list[str] = []
    if resolved:
        parts.append(
            "Suggested skills (read on demand): "
            + ", ".join(f"`{name}` -> ai_skill('{handle}')" for name, handle in resolved)
            + ".",
        )
    if unresolved:
        parts.append(
            "Suggested skills (no resolvable catalog id): "
            + ", ".join(f"`{name}`" for name in unresolved)
            + ".",
        )
    return " ".join(parts)


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
            # Doctrine re-surface (#316): scrolls this agent read via ai_skill
            # come back as LEAN pointers once per epoch after a compaction —
            # doctrine survives compaction by construction, not by luck.
            from .doctrine_resurface import maybe_doctrine_resurface_blocks

            for block in maybe_doctrine_resurface_blocks(
                project_root,
                host_kind=host_kind,
                host_session_id=host_session_id,
            ):
                parts.append(block)

            # Open-backlog surfacing (#419 War DD): counts + top items with
            # the tell-the-user instruction. Epoch-deduped via the session
            # ledger (key 'backlog_surface', shared with the notification
            # rail) — once per epoch/session, re-emitted when the backlog
            # changes or the epoch rotates. Fail-quiet inside; never for
            # lane workers.
            from .backlog_surfacer import context_backlog_block

            backlog_block = context_backlog_block(
                project_root,
                session_id or host_session_id,
                host_kind=host_kind,
                host_session_id=host_session_id,
            )
            if backlog_block:
                parts.append(backlog_block)

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
                suggested = self._suggested_skill_candidates(
                    prompt,
                    project_root,
                    already_active=set(active_skills or []),
                )
                # #620: a surfaced suggestion NAMES its retrieval call. A bare
                # vocab key is not an address the agent can dereference.
                suggestion_block = render_suggested_skills(suggested)
                if suggestion_block:
                    parts.append(suggestion_block)
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
        result = surfacer.surface_on_prompt(
            prompt=prompt,
            project_root=project_root,
            action_kind=action_kind,
            already_used_tools=used,
            host_kind=host_kind,
            host_session_id=host_session_id or "",
        )
        return list(result.advisory_lines)

    def _tools_used_in_session(self, project_root: Path) -> set[str]:
        """Tool names already used in the current managed session (native_tool_use
        rows). Unmanaged sessions return empty; exceptions suppressed."""
        try:
            from .managed_mode_service import resolve_managed_session

            session_id = resolve_managed_session(
                self.runtime.hub.managed_mode, project_root
            )
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
        """Skill NAMES the prompt suggests activating, beyond active_skills.
        NLP-FREE literal word-overlap against configured triggers. [] on error.

        Kept as the host-facing NAME seam (claude_hook delegates here). The
        emit path uses ``_suggested_skill_candidates`` instead, so what the
        agent actually sees carries a dereferenceable handle (#620).
        """
        return [
            cand.skill_name
            for cand in self._suggested_skill_candidates(
                prompt,
                project_root,
                already_active=already_active,
            )
        ]

    def _suggested_skill_candidates(
        self,
        prompt: str,
        project_root: Path,
        *,
        already_active: set[str],
    ) -> list[Any]:
        """SkillCandidates the prompt suggests, each carrying its RESOLVABLE
        catalog handle (#620).

        MATCHING IS UNTOUCHED: the same NLP-FREE literal detector, the same
        top_n, the same already-active filter — so the candidate SET is
        identical to before. This only ATTACHES identity, via the ONE identity
        module (``skill_resolution.resolve_suggested_skill_handle``) against
        the live catalog. The catalog's own SQL excludes sovereign-only rows
        (``WHERE read_access = 'public'``), so no soul can reach this rail and
        nothing is post-filtered here.

        A candidate whose name resolves to nothing keeps an EMPTY handle: it is
        never dropped (that would move the set) and never handed a fabricated
        address.
        """
        import dataclasses

        from .aidocs_nlp.consumers.skill_trigger import (
            detect_skill_triggers_literal,
            load_skill_trigger_tokens,
        )

        try:
            triggers = load_skill_trigger_tokens()
            if not triggers:
                return []
            hits = [
                hit
                for hit in detect_skill_triggers_literal(prompt, triggers, top_n=5)
                if hit.skill_name not in already_active
            ]
        except Exception:
            return []
        if not hits:
            return []
        # Identity attachment is BEST-EFFORT: a catalog hiccup leaves the
        # suggestions standing handle-less rather than breaking the rail.
        try:
            from .skill_resolution import resolve_suggested_skill_handle

            catalog = self.runtime.hub.skills.list_skills(
                project_root,
                include_body=False,
            )
        except Exception:
            return hits
        out: list[Any] = []
        for hit in hits:
            try:
                handle = resolve_suggested_skill_handle(hit.skill_name, catalog)
            except Exception:
                handle = ""
            out.append(dataclasses.replace(hit, skill_id=handle) if handle else hit)
        return out

    def _lifecycle_followthrough_nudge(self, lifecycle_state: dict[str, object]) -> str:
        from .lifecycle_service import LifecycleService

        return LifecycleService(self.runtime).build_followthrough_nudge(lifecycle_state)

    # ── S2 hook-core rip (2026-07-06): moved VERBATIM from ClaudeHookHandler ──
    # (_build_prompt_context / _action_directive / _build_compiled_workflow_summary
    # / _build_lifecycle_followthrough_nudge). claude_hook keeps thin delegates.

    _TOOL_FIRST_PREAMBLE = TOOL_FIRST_PREAMBLE
    _ACTION_DIRECTIVES: dict[str, str] = ACTION_DIRECTIVES

    def build_prompt_context(self, result: dict[str, object]) -> str:
        classification = (
            result.get("classification") if isinstance(result.get("classification"), dict) else {}
        )
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        orchestration = (
            result.get("orchestration") if isinstance(result.get("orchestration"), dict) else {}
        )

        action_kind = str(classification.get("action_kind") or "understand")
        mode = str(result.get("mode") or "")
        session_id = str(
            route.get("session_id") or orchestration.get("selected_session_id") or "",
        ).strip()
        recommended = (
            route.get("recommended_mcp_flow")
            if isinstance(route.get("recommended_mcp_flow"), list)
            else []
        )
        recommended_text = ", ".join(str(item) for item in recommended if str(item).strip())
        retrieval = (
            orchestration.get("retrieval")
            if isinstance(orchestration.get("retrieval"), dict)
            else {}
        )
        retrieval_mode = str(retrieval.get("mode") or "")
        # Prefer workflow from orchestration result (avoids re-reading)
        workflow = (
            orchestration.get("workflow") if isinstance(orchestration.get("workflow"), dict) else {}
        )
        if not workflow:
            # Fallback: try bootstrap sync path
            bootstrap = (
                orchestration.get("bootstrap")
                if isinstance(orchestration.get("bootstrap"), dict)
                else {}
            )
            sync = bootstrap.get("sync") if isinstance(bootstrap.get("sync"), dict) else {}
            workflow = sync.get("workflow") if isinstance(sync.get("workflow"), dict) else {}

        parts = [
            "AIDOCS-managed mode is active for this project.",
            f"AIDOCS suggests action kind: `{action_kind}` (advisory — use your judgment if the classification seems wrong).",
        ]
        if session_id:
            parts.append(f"Bound session: `{session_id}`.")
            parts.append(
                "Stay in the bound AIDOCS session and continue its current conductor/plan flow; do not switch to generic worktree or standalone execution setup.",
            )
        if mode == "mcp_orchestrated":
            parts.append(
                "Route this turn through the AIDOCS MCP flow before broad repo inspection.",
            )
        elif mode == "direct_inspection_allowed":
            parts.append(
                "Inspect the explicit target first, then return to MCP-first flow for broader work.",
            )
        if retrieval_mode:
            parts.append(f"Current retrieval mode: `{retrieval_mode}`.")
        if recommended_text:
            parts.append(f"Recommended MCP flow: {recommended_text}.")

        action_directive = self._action_directive(action_kind)
        if action_directive:
            parts.append(action_directive)

        # Domain-specific tool recommendations from __domain_hint_* tokens
        domain_hints = classification.get("domain_hints")
        if isinstance(domain_hints, list) and domain_hints:
            hint_parts = []
            for hint in domain_hints:
                if isinstance(hint, dict) and hint.get("recommended_tools"):
                    hint_parts.append(f"{hint['domain']}: {hint['recommended_tools']}")
            if hint_parts:
                parts.append(f"Domain-specific tools: {'; '.join(hint_parts)}.")

        workflow_summary = self._build_compiled_workflow_summary(workflow)
        if workflow_summary:
            parts.append(f"Compiled workflow actions: {workflow_summary}.")
        host_state = result.get("host_state") if isinstance(result.get("host_state"), dict) else {}
        lifecycle_state = (
            host_state.get("lifecycle_state")
            if isinstance(host_state.get("lifecycle_state"), dict)
            else {}
        )
        lifecycle_nudge = self._build_lifecycle_followthrough_nudge(lifecycle_state)
        if lifecycle_nudge:
            parts.append(lifecycle_nudge)
        parts.append(
            "Avoid ad-hoc broad repo scanning when the MCP routing result already provides the path forward.",
        )
        return " ".join(parts)

    def _build_lifecycle_followthrough_nudge(self, lifecycle_state: dict[str, object]) -> str:
        """Thin delegate to LifecycleService.build_followthrough_nudge
        (host-agnostic pure function).
        """
        from .lifecycle_service import LifecycleService

        return LifecycleService.build_followthrough_nudge(lifecycle_state)

    def _action_directive(self, action_kind: str) -> str:
        from .config import render_interaction_text

        directive = render_interaction_text(f"interaction.action_directives.{action_kind}")
        if not directive:
            directive = self._ACTION_DIRECTIVES.get(action_kind, "")
        if directive and action_kind not in (
            "write_memory",
            "task_begin",
            "task_complete",
            "task_update",
        ):
            return f"{self._TOOL_FIRST_PREAMBLE} {directive}"
        return directive

    def _build_compiled_workflow_summary(self, workflow: dict[str, object] | None) -> str:
        if not isinstance(workflow, dict):
            return ""
        actions = workflow.get("actions") if isinstance(workflow.get("actions"), list) else []
        if not actions:
            return ""
        rendered = []
        for action in actions[:3]:
            if not isinstance(action, dict):
                continue
            trigger = str(action.get("trigger") or "?")
            kind = str(action.get("kind") or "?")
            rendered.append(f"`{trigger} -> {kind}`")
        if not rendered:
            return ""
        if len(actions) > len(rendered):
            rendered.append(f"and {len(actions) - len(rendered)} more")
        return ", ".join(rendered)
