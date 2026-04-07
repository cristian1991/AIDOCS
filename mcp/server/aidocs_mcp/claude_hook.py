from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .config import render_interaction_text
from .runtime_service import RuntimeService
from .service_hub import AidocsServiceHub

logger = logging.getLogger("aidocs.claude_hook")


def _resolve_templates_root() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "core" / ".MEMORY" / ".aidocs" / "templates"


def _resolve_script_root() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "core" / "scripts"


class ClaudeHookHandler:
    """Claude Code adapter — translates CC hook events to AgentOrchestrator calls."""

    def __init__(self) -> None:
        hub = AidocsServiceHub(
            templates_root=_resolve_templates_root(), script_root=_resolve_script_root()
        )
        self.runtime = RuntimeService(hub)
        from .agent_orchestrator import AgentOrchestrator
        self.orchestrator = AgentOrchestrator(self.runtime)
        self._last_user_prompt: str = ""

    def handle(self, payload: dict[str, object]) -> dict[str, object] | None:
        event_name = str(payload.get("hook_event_name") or "").strip()
        if not event_name:
            return None

        # Allow /aidocs command even on non-AIDOCS projects (for bootstrapping)
        if event_name == "UserPromptSubmit":
            prompt = str(payload.get("prompt") or "").strip()
            if prompt.startswith("/aidocs"):
                project_root = self._resolve_project_root(payload)
                if project_root is not None:
                    self._record_hook_event(
                        project_root, event_name=event_name, payload=payload
                    )
                return self._handle_aidocs_command(payload)

        if event_name == "SessionStart":
            project_root = self._resolve_cwd_root(payload)
            if project_root is None:
                return None
            result = self._handle_session_start(project_root)
            if (project_root / ".MEMORY").is_dir():
                self._record_hook_event(
                    project_root, event_name=event_name, payload=payload
                )
            return result

        project_root = self._resolve_project_root(payload)
        if project_root is None:
            return None

        self._record_hook_event(project_root, event_name=event_name, payload=payload)

        if event_name == "UserPromptSubmit":
            return self._handle_user_prompt_submit(project_root, payload)
        if event_name == "PreToolUse":
            return self._handle_pre_tool_use(project_root, payload)
        if event_name == "PostCompact":
            return self._handle_post_compact(project_root)
        return None

    def _handle_aidocs_command(self, payload: dict[str, object]) -> dict[str, object]:
        """Handle /aidocs command — works on both initialized and uninitialized projects."""
        cwd = str(payload.get("cwd") or "").strip()
        project_root = Path(cwd).resolve() if cwd else None
        memory_exists = (
            project_root and (project_root / ".MEMORY").is_dir()
            if project_root
            else False
        )

        if memory_exists:
            context = (
                "AIDOCS entry command detected. Use the MCP bootstrap/orchestrator flow for this project, "
                "report selected session and managed-mode state, and avoid broad repo reads before session routing completes."
            )
        else:
            context = (
                "AIDOCS entry command detected on a project without AIDOCS structure. "
                "This project needs initialization. Call the `project_init` MCP tool with the project root path "
                "to create .MEMORY/, AGENTS.md/CLAUDE.md, and AIDOCS templates. "
                "After initialization, call `project_bootstrap_or_resume` to activate managed mode."
            )

        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }

    def _handle_session_start(self, project_root: Path) -> dict[str, object]:
        host_state = self.runtime.host_state(project_root)
        session_state = (
            host_state.get("session_state")
            if isinstance(host_state.get("session_state"), dict)
            else {}
        )
        session_id = str(session_state.get("session_id") or "").strip()
        current_state = str(session_state.get("state") or "ready")

        if current_state == "not_initialized":
            context = (
                "AIDOCS startup check: this project is not initialized yet. "
                "Run `/aidocs` to initialize and bootstrap AIDOCS before normal work."
            )
        elif current_state == "not_bootstrapped":
            context = (
                "AIDOCS startup check: project structure is incomplete or not fully bootstrapped. "
                "Run `/aidocs` to repair bootstrap state before normal work."
            )
        elif current_state == "no_session":
            context = (
                "AIDOCS startup check: the project is initialized, but no session exists yet. "
                "Use `/aidocs`; create a session before normal work."
            )
        elif current_state == "multiple_sessions":
            context = (
                "AIDOCS startup check: multiple plausible sessions exist. "
                "Ask the user which session to connect to before normal work, then use `/aidocs` as needed to bind managed mode."
            )
        elif current_state == "stale_indexes":
            target = (
                f" Session `{session_id}` is the current candidate."
                if session_id
                else ""
            )
            context = (
                "AIDOCS startup check: indexes are stale and should be re-synced before normal work."
                f"{target} Run `/aidocs` to refresh bootstrap/index state."
            )
        else:
            target = f" Continue with session `{session_id}`." if session_id else ""
            context = (
                "AIDOCS startup check: startup state is ready."
                f"{target} Stay in the bound AIDOCS session and continue its current conductor/plan flow; do not switch to generic worktree or standalone execution setup. Prefer indexed AIDOCS retrieval before broad repository reads."
            )
        skill_state = (
            host_state.get("skill_state")
            if isinstance(host_state.get("skill_state"), dict)
            else {}
        )
        session_snapshot = (
            skill_state.get("session_snapshot")
            if isinstance(skill_state.get("session_snapshot"), dict)
            else {}
        )
        active_skills = (
            session_snapshot.get("active_skills")
            if isinstance(session_snapshot.get("active_skills"), list)
            else []
        )
        if active_skills:
            context = (
                context
                + " Imported skills: "
                + ", ".join(f"`{item}`" for item in active_skills if str(item).strip())
                + "."
            )

        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }

    # Prompts this short are conversational — don't inject tool directives
    _CONVERSATIONAL_MAX_LEN = 60

    def _handle_user_prompt_submit(
        self, project_root: Path, payload: dict[str, object]
    ) -> dict[str, object] | None:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return None

        # Clear previous turn's user-intent tool grants on every new prompt
        managed = self.runtime.hub.managed_mode.get_mode(project_root)
        session_id = str(managed.get("session_id") or "").strip() if managed.get("active") else ""
        if session_id:
            self.runtime.hub.query_gate.set_user_intent_tools(project_root, session_id, [])

        # Short/conversational prompts — don't inject directives, let the agent respond naturally
        if len(prompt) < self._CONVERSATIONAL_MAX_LEN and not any(
            kw in prompt.lower()
            for kw in (
                "fix",
                "edit",
                "add",
                "create",
                "delete",
                "remove",
                "update",
                "change",
                "implement",
                "refactor",
                "debug",
                "trace",
                "find",
                "search",
                "investigate",
            )
        ):
            return None

        host_state = self.runtime.host_state(project_root, prompt_text=prompt)
        prompt_state = (
            host_state.get("prompt_state")
            if isinstance(host_state.get("prompt_state"), dict)
            else {}
        )
        action_kind = str(prompt_state.get("action_kind") or "understand")
        route = self.runtime.aidocs_route_prompt(
            project_root,
            user_request=prompt,
            action_kind=action_kind,
        )

        if not route.get("managed_mode"):
            return {
                "decision": "block",
                "reason": "Run /aidocs first to activate AIDOCS-managed mode for this project.",
            }

        if route.get("blocked_reason"):
            blocked_reason = str(
                route.get("blocked_reason")
                or "This prompt is blocked by AIDOCS runtime policy."
            )
            return {
                "decision": "block",
                "reason": blocked_reason,
            }

        additional_context = self._build_lightweight_prompt_context(
            action_kind=action_kind,
            route=route,
            project_root=project_root,
            host_state=host_state,
        )
        if not additional_context:
            return None

        self._record_classification_event(project_root, action_kind, prompt)

        # Grant user-intent raw tool access for this turn
        session_id = str(route.get("session_id") or "").strip()
        if session_id:
            self._grant_user_intent_tools(project_root, session_id, prompt)

        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            }
        }

    # ── User-intent raw tool grants ──
    # When the user explicitly requests a raw tool (e.g., "grep for X"),
    # grant that tool for this turn. Cleared on next UserPromptSubmit.

    _USER_INTENT_TOOL_KEYWORDS: dict[str, list[str]] = {
        "grep": ["grep", "grep for", "search for", "find in files", "rg "],
        "read": ["read the", "read file", "cat the", "show me the file", "open the file"],
        "glob": ["find files", "list files", "ls "],
        "edit": ["edit the", "sed ", "replace in"],
        "write": ["write to", "write the", "create file", "save to file"],
        "bash": ["run ", "execute ", "shell ", "terminal "],
    }

    def _grant_user_intent_tools(
        self, project_root: Path, session_id: str, prompt: str
    ) -> None:
        """Detect explicit user intent for raw tools and grant them for this turn."""
        text = prompt.strip().lower()
        granted: list[str] = []
        for tool, keywords in self._USER_INTENT_TOOL_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                granted.append(tool)
        # Store in query gate state — PreToolUse checks this
        self.runtime.hub.query_gate.set_user_intent_tools(
            project_root, session_id, granted
        )

    def _handle_post_compact(self, project_root: Path) -> dict[str, object] | None:
        """Reset token counters after context compaction — new context window."""
        try:
            from .execution_index_store import ExecutionIndexStore
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            sid = str(managed.get("session_id") or "").strip() if managed.get("active") else None
            deleted = ExecutionIndexStore().clear_token_usage(project_root, session_id=sid or None)
            context = f"Context compacted. Token counter reset ({deleted} records cleared)."
        except Exception as exc:
            context = f"Context compacted. Token reset failed: {exc}"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostCompact",
                "additionalContext": context,
            }
        }

    def _handle_pre_tool_use(
        self, project_root: Path, payload: dict[str, object]
    ) -> dict[str, object] | None:
        """CC adapter: translate PreToolUse to orchestrator.check_tool()."""
        tool_name = str(payload.get("tool_name") or "").strip()
        tool_input = (
            payload.get("tool_input")
            if isinstance(payload.get("tool_input"), dict)
            else {}
        )

        decision = self.orchestrator.check_tool(project_root, tool_name, tool_input)

        if not decision.allowed:
            return {
                "decision": "block",
                "reason": decision.reason,
            }

        if decision.advisory:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": decision.advisory,
                }
            }

        return None


    _MCP_ALTERNATIVES: dict[str, list[tuple[str, str]]] = {
        "grep": [
            ('code_find(query, mode="symbols")', "Find symbols by name, kind, or role"),
            ('code_find(query, mode="references")', "Find all usages of a symbol"),
            ('schema_query(query, mode="field")', "Find a DB field/column across all entities"),
            ('code_trace(query, mode="css_class")', "Find CSS rules matching class names"),
        ],
        "read": [
            ('code_bundle(path, mode="file")', "Understand file structure without reading the whole file"),
            ("code_get_symbol_snippet", "Read just one symbol's code at a known location"),
            ('code_find(query, mode="symbols")', "Locate the exact symbol before reading code"),
        ],
        "glob": [
            ("code_search", "Find files by path/summary keywords"),
            ('code_find(query, mode="partial_group")', "Find all partial class files for a C# type"),
        ],
    }

    # Source code extensions where MCP tools add value — based on code indexer language mapping
    _CODE_EXTENSIONS: set[str] = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".cs",
        ".cshtml",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".rb",
        ".php",
        ".ex",
        ".exs",
        ".swift",
        ".dart",
        ".lua",
        ".sh",
        ".bash",
        ".ps1",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".html",
        ".htm",
        ".vue",
        ".svelte",
        ".sql",
        ".prisma",
        ".resx",
    }

    # Config files that agents must NEVER modify (absolute boundary)
    _PROTECTED_CONFIG: set[str] = {"aidocs.toml", "aidocs-plugin.json"}

    # Paths that indicate AIDOCS infrastructure (protected unless dev_mode)
    _INFRASTRUCTURE_PATHS: tuple[str, ...] = (
        "aidocs_mcp/",
        "aidocs_mcp\\",
        "core/plugins/aidocs.js",
    )



    def _record_hook_event(
        self, project_root: Path, event_name: str, payload: dict[str, object]
    ) -> None:
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            session_id = str(managed.get("session_id") or "").strip() or None
            tool_name = str(payload.get("tool_name") or "").strip() or None
            prompt = str(payload.get("prompt") or "").strip() or None
            event_kind = event_name.lower()
            payload_summary = {
                key: value
                for key, value in payload.items()
                if key
                in {"hook_event_name", "tool_name", "tool_input", "prompt", "cwd"}
            }
            # Token estimation from hook payloads
            tokens_in = 0
            tokens_out = 0
            if prompt:
                tokens_in += max(1, len(prompt.encode("utf-8")) // 4)
            tool_input = payload.get("tool_input")
            if isinstance(tool_input, dict):
                try:
                    tokens_out += max(1, len(json.dumps(tool_input, default=str).encode("utf-8")) // 4)
                except Exception:
                    pass
            elif isinstance(tool_input, str):
                tokens_out += max(1, len(tool_input.encode("utf-8")) // 4)
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind=event_kind,
                source_kind="claude_hook",
                session_id=session_id,
                capability_name=tool_name,
                action_kind="hook_intercept",
                status="observed",
                payload={
                    **payload_summary,
                    "prompt_preview": prompt[:200] if prompt else None,
                    "tokens_in_estimate": tokens_in,
                    "tokens_out_estimate": tokens_out,
                },
            )
        except Exception as exc:
            logger.debug("Failed to record hook event: %s", exc)
            return None

    def _resolve_cwd_root(self, payload: dict[str, object]) -> Path | None:
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            return None
        return Path(cwd).resolve()

    def _resolve_project_root(self, payload: dict[str, object]) -> Path | None:
        project_root = self._resolve_cwd_root(payload)
        if project_root is None:
            return None

        memory_root = project_root / ".MEMORY"
        if not memory_root.is_dir():
            self._log_resolution_failure(project_root, "missing .MEMORY directory")
            return None

        if not (
            (project_root / "AGENTS.md").is_file()
            or (project_root / "CLAUDE.md").is_file()
        ):
            self._log_resolution_failure(
                project_root, "missing AGENTS.md and CLAUDE.md"
            )
            return None

        return project_root

    def _record_classification_event(
        self, project_root: Path, action_kind: str, prompt: str
    ) -> None:
        """Record the classified action_kind as an execution event for traceability."""
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            session_id = str(managed.get("session_id") or "").strip() or None
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="prompt_classified",
                source_kind="claude_hook",
                session_id=session_id,
                action_kind=action_kind,
                status="classified",
                payload={"prompt_preview": prompt[:200] if prompt else None},
            )
        except Exception as exc:
            logger.debug("Failed to record classification event: %s", exc)

    def _log_resolution_failure(self, project_root: Path, reason: str) -> None:
        """Log project root resolution failure for debugging."""
        logger.warning("Project resolution failed for %s: %s", project_root, reason)

    def _build_lightweight_prompt_context(
        self,
        action_kind: str,
        route: dict[str, object],
        project_root: Path,
        host_state: dict[str, object] | None = None,
    ) -> str:
        """Build context from classification + route only (no orchestration data)."""
        from .config import AGENT_HOST_MODE

        prompt_payload = host_state if isinstance(host_state, dict) else {}
        session_state = (
            prompt_payload.get("session_state")
            if isinstance(prompt_payload.get("session_state"), dict)
            else {}
        )
        session_id = str(
            session_state.get("session_id") or route.get("session_id") or ""
        ).strip()

        # ── Enforced mode (CC): minimal injection, gates do the work ──
        if AGENT_HOST_MODE == "enforced":
            return self._build_enforced_context(action_kind, session_id, route, prompt_payload)

        # ── Advisory mode (OC/other): verbose directives ──
        return self._build_advisory_context(action_kind, session_id, route, prompt_payload)

    def _build_enforced_context(
        self,
        action_kind: str,
        session_id: str,
        route: dict[str, object],
        prompt_payload: dict[str, object],
    ) -> str:
        """Minimal context for hosts with PreToolUse gate enforcement."""
        parts = [f"AIDOCS managed. Action: `{action_kind}`."]
        if session_id:
            parts.append(f"Session: `{session_id}`.")

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

        # Lifecycle nudge — still useful as a reminder
        lifecycle_state = (
            prompt_payload.get("lifecycle_state")
            if isinstance(prompt_payload.get("lifecycle_state"), dict)
            else {}
        )
        lifecycle_nudge = self._build_lifecycle_followthrough_nudge(lifecycle_state)
        if lifecycle_nudge:
            parts.append(lifecycle_nudge)

        return " ".join(parts)

    def _build_advisory_context(
        self,
        action_kind: str,
        session_id: str,
        route: dict[str, object],
        prompt_payload: dict[str, object],
    ) -> str:
        """Verbose context for hosts without PreToolUse gate enforcement."""
        # Check if interaction_text provides pre-built context
        interaction_text = (
            prompt_payload.get("interaction_text")
            if isinstance(prompt_payload.get("interaction_text"), dict)
            else {}
        )
        if str(interaction_text.get("prompt_context") or "").strip():
            parts = [str(interaction_text.get("prompt_context") or "").strip()]
            directive = str(interaction_text.get("action_directive") or "").strip()
            if directive:
                parts.append(directive)
            recommended = (
                route.get("recommended_mcp_flow")
                if isinstance(route.get("recommended_mcp_flow"), list)
                else []
            )
            recommended_text = ", ".join(
                str(item) for item in recommended if str(item).strip()
            )
            if recommended_text:
                parts.append(f"Recommended MCP flow: {recommended_text}.")
            lifecycle_state = (
                prompt_payload.get("lifecycle_state")
                if isinstance(prompt_payload.get("lifecycle_state"), dict)
                else {}
            )
            lifecycle_nudge = self._build_lifecycle_followthrough_nudge(lifecycle_state)
            if lifecycle_nudge:
                parts.append(lifecycle_nudge)
            return " ".join(part for part in parts if part)

        recommended = (
            route.get("recommended_mcp_flow")
            if isinstance(route.get("recommended_mcp_flow"), list)
            else []
        )
        recommended_text = ", ".join(
            str(item) for item in recommended if str(item).strip()
        )

        parts = [
            f"AIDOCS managed. Action: `{action_kind}`.",
        ]
        if session_id:
            parts.append(f"Session: `{session_id}`.")
            parts.append(
                "Stay in the bound AIDOCS session and continue its current conductor/plan flow; do not switch to generic worktree or standalone execution setup."
            )

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
        active_skills = (
            prompt_activation.get("active_skills")
            if isinstance(prompt_activation.get("active_skills"), list)
            else []
        )
        prompt_state = (
            prompt_payload.get("prompt_state")
            if isinstance(prompt_payload.get("prompt_state"), dict)
            else {}
        )
        if not active_skills:
            imported = (
                route.get("imported_skill_state")
                if isinstance(route.get("imported_skill_state"), dict)
                else None
            )
            active_skills = (
                imported.get("active_skills")
                if isinstance(imported, dict)
                and isinstance(imported.get("active_skills"), list)
                else []
            )
        if active_skills:
            parts.append(
                "Imported skills: "
                + ", ".join(f"`{item}`" for item in active_skills if str(item).strip())
                + "."
            )
        override_modes = (
            prompt_state.get("override_modes")
            if isinstance(prompt_state.get("override_modes"), dict)
            else {}
        )
        if override_modes:
            rendered_modes = ", ".join(
                f"`{skill_id}={mode}`"
                for skill_id, mode in override_modes.items()
                if str(skill_id).strip() and str(mode).strip()
            )
            if rendered_modes:
                parts.append(f"Imported skill modes: {rendered_modes}.")
        runtime_owned_capabilities = (
            prompt_state.get("runtime_owned_capabilities")
            if isinstance(prompt_state.get("runtime_owned_capabilities"), list)
            else []
        )
        if runtime_owned_capabilities:
            rendered_capabilities = ", ".join(
                f"`{item.get('capability_id')}`"
                for item in runtime_owned_capabilities
                if isinstance(item, dict)
                and str(item.get("capability_id") or "").strip()
            )
            if rendered_capabilities:
                parts.append(
                    "Runtime-owned workflow capabilities: "
                    + rendered_capabilities
                    + "."
                )
        helper_skill_guidance = (
            prompt_activation.get("helper_skill_guidance")
            if isinstance(prompt_activation.get("helper_skill_guidance"), list)
            else []
        )
        if not helper_skill_guidance:
            session_snapshot = (
                skill_state.get("session_snapshot")
                if isinstance(skill_state.get("session_snapshot"), dict)
                else {}
            )
            helper_skill_guidance = (
                session_snapshot.get("helper_skill_guidance")
                if isinstance(session_snapshot.get("helper_skill_guidance"), list)
                else []
            )
        rendered_helper_skills = []
        for item in helper_skill_guidance[:2]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            name = str(item.get("name") or item.get("skill_id") or "skill").strip()
            rendered_helper_skills.append(
                f'<aidocs-skill name="{name}">{content}</aidocs-skill>'
            )
        if rendered_helper_skills:
            parts.append(
                "Active AIDOCS helper skill guidance: "
                + " ".join(rendered_helper_skills)
            )

        action_directive = self._action_directive(action_kind)
        if action_directive:
            parts.append(action_directive)

        lifecycle_state = (
            prompt_payload.get("lifecycle_state")
            if isinstance(prompt_payload.get("lifecycle_state"), dict)
            else {}
        )
        lifecycle_nudge = self._build_lifecycle_followthrough_nudge(lifecycle_state)
        if lifecycle_nudge:
            parts.append(lifecycle_nudge)

        return " ".join(parts)

    def _build_prompt_context(self, result: dict[str, object]) -> str:
        classification = (
            result.get("classification")
            if isinstance(result.get("classification"), dict)
            else {}
        )
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        orchestration = (
            result.get("orchestration")
            if isinstance(result.get("orchestration"), dict)
            else {}
        )

        action_kind = str(classification.get("action_kind") or "understand")
        mode = str(result.get("mode") or "")
        session_id = str(
            route.get("session_id") or orchestration.get("selected_session_id") or ""
        ).strip()
        recommended = (
            route.get("recommended_mcp_flow")
            if isinstance(route.get("recommended_mcp_flow"), list)
            else []
        )
        recommended_text = ", ".join(
            str(item) for item in recommended if str(item).strip()
        )
        retrieval = (
            orchestration.get("retrieval")
            if isinstance(orchestration.get("retrieval"), dict)
            else {}
        )
        retrieval_mode = str(retrieval.get("mode") or "")
        # Prefer workflow from orchestration result (avoids re-reading)
        workflow = (
            orchestration.get("workflow")
            if isinstance(orchestration.get("workflow"), dict)
            else {}
        )
        if not workflow:
            # Fallback: try bootstrap sync path
            bootstrap = (
                orchestration.get("bootstrap")
                if isinstance(orchestration.get("bootstrap"), dict)
                else {}
            )
            sync = (
                bootstrap.get("sync") if isinstance(bootstrap.get("sync"), dict) else {}
            )
            workflow = (
                sync.get("workflow") if isinstance(sync.get("workflow"), dict) else {}
            )

        parts = [
            "AIDOCS-managed mode is active for this project.",
            f"AIDOCS suggests action kind: `{action_kind}` (advisory — use your judgment if the classification seems wrong).",
        ]
        if session_id:
            parts.append(f"Bound session: `{session_id}`.")
            parts.append(
                "Stay in the bound AIDOCS session and continue its current conductor/plan flow; do not switch to generic worktree or standalone execution setup."
            )
        if mode == "mcp_orchestrated":
            parts.append(
                "Route this turn through the AIDOCS MCP flow before broad repo inspection."
            )
        elif mode == "direct_inspection_allowed":
            parts.append(
                "Inspect the explicit target first, then return to MCP-first flow for broader work."
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
        lifecycle_state = (
            host_state.get("lifecycle_state")
            if isinstance(host_state.get("lifecycle_state"), dict)
            else {}
        )
        lifecycle_nudge = self._build_lifecycle_followthrough_nudge(lifecycle_state)
        if lifecycle_nudge:
            parts.append(lifecycle_nudge)
        parts.append(
            "Avoid ad-hoc broad repo scanning when the MCP routing result already provides the path forward."
        )
        return " ".join(parts)

    def _build_lifecycle_followthrough_nudge(
        self, lifecycle_state: dict[str, object]
    ) -> str:
        if not isinstance(lifecycle_state, dict):
            return ""
        if lifecycle_state.get("needs_task_complete"):
            return "Lifecycle follow-through: meaningful edit work happened since the last lifecycle tool call; use `aidocs_task_complete` if the task is done."
        if lifecycle_state.get("needs_task_update"):
            return "Lifecycle follow-through: meaningful work has accumulated since the last lifecycle tool call; use `aidocs_task_update` to record progress."
        return ""

    _TOOL_FIRST_PREAMBLE = "MANDATORY: Use AIDOCS MCP tools FIRST. Fall back to raw Read/Grep only if MCP returns empty."

    _ACTION_DIRECTIVES: dict[str, str] = {
        "write_memory": (
            "Use `aidocs_memory_capture` with `target_hint` (workflow/coding-standards/security/project-state/user-profile). "
            "Do NOT write memory files manually."
        ),
        "task_begin": "Use `aidocs_task_begin` to register the task before starting work.",
        "task_complete": "Use `aidocs_task_complete` to finalize the task.",
        "task_update": "Use `aidocs_task_update` to record progress on the current task.",
        "trace": (
            '`aidocs_code_find(query, mode="references")` → `aidocs_code_trace(query, mode="field_flow")` → '
            '`aidocs_code_trace(query, mode="css_class")`. '
            'DB: `aidocs_schema_query(query, mode="trace_path")`. API→UI: `aidocs_code_trace(query, mode="api_to_ui")`.'
        ),
        "understand": (
            '`aidocs_code_bundle(path, mode="file")` (structure) → `aidocs_code_find(query, mode="symbols")` (find symbol) → '
            "`aidocs_code_get_symbol_snippet` (read it). "
            "Precision: `aidocs_code_get_method_signature`, `aidocs_code_get_constructor_params`, `aidocs_code_get_enum_values`, `aidocs_code_get_service_api`. "
            'Broad: `aidocs_code_bundle(concept, mode="subsystem")`. DB: `aidocs_schema_query(name, mode="entity")`.'
        ),
        "code_bundle": (
            '`aidocs_code_bundle(path, mode="context", session_id=...)` (session-guided) or '
            '`aidocs_code_bundle(path, mode="file")` (single file).'
        ),
        "edit": (
            "`aidocs_task_begin` → `aidocs_code_get_lines` (read target) → `aidocs_code_edit_lines` (single edit) or `aidocs_code_batch_edit` (multiple edits in one call) → `aidocs_task_complete`. "
            "Do NOT mix edit methods — use `aidocs_code_edit_lines`/`aidocs_code_batch_edit` for all edits, not raw Edit or apply_patch. "
            "Before editing: `aidocs_code_get_method_signature` / `aidocs_code_get_constructor_params` / `aidocs_code_get_enum_values` to confirm exact signatures. "
            'CSS: `aidocs_code_trace(class, mode="css_class")`. DB: `aidocs_schema_query(entity, mode="entity")`.'
        ),
        "test_heavy": (
            "If test/support code matters, re-run retrieval with test-inclusive indexing where the tool supports it. "
            "Then prefer: `aidocs_code_get_service_api` → `aidocs_code_get_method_signatures` → `aidocs_code_get_constructor_params_batch` → `aidocs_code_get_enum_values` → `aidocs_code_get_entity_properties`. "
            "Do not guess property names, constructor params, enum members, or service surfaces when the precision chain can confirm them first."
        ),
        "inspect": (
            '`aidocs_code_bundle(path, mode="file")` → `aidocs_code_get_dependencies` / '
            '`aidocs_code_find(query, mode="references")` → `aidocs_code_get_modules` (project boundaries). Read only after narrowing.'
        ),
        "read_error": (
            '`aidocs_code_find(symbol, mode="symbols")` (find it) → `aidocs_code_find(symbol, mode="references")` (trace) → '
            '`aidocs_code_get_symbol_snippet` (read method). DB: add `aidocs_schema_query(entity, mode="entity")`.'
        ),
        "investigate": (
            "`aidocs_code_investigate(concept, depth=..., focus=...)` for a guided navigation plan. "
            'Or: `aidocs_code_bundle(concept, mode="subsystem")` → narrow with '
            '`aidocs_code_find(concept, mode="mutations")`, `aidocs_code_find(concept, mode="validation")`, or `aidocs_code_find(concept, mode="policy")`.'
        ),
    }

    def _action_directive(self, action_kind: str) -> str:
        directive = render_interaction_text(
            f"interaction.action_directives.{action_kind}"
        )
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

    def _build_compiled_workflow_summary(
        self, workflow: dict[str, object] | None
    ) -> str:
        if not isinstance(workflow, dict):
            return ""
        actions = (
            workflow.get("actions") if isinstance(workflow.get("actions"), list) else []
        )
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


def main() -> None:
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    response = ClaudeHookHandler().handle(payload)
    if response is not None:
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
