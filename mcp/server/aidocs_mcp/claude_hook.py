from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

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
    def __init__(self) -> None:
        hub = AidocsServiceHub(templates_root=_resolve_templates_root(), script_root=_resolve_script_root())
        self.runtime = RuntimeService(hub)

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
                    self._record_hook_event(project_root, event_name=event_name, payload=payload)
                return self._handle_aidocs_command(payload)

        project_root = self._resolve_project_root(payload)
        if project_root is None:
            return None

        self._record_hook_event(project_root, event_name=event_name, payload=payload)

        if event_name == "UserPromptSubmit":
            return self._handle_user_prompt_submit(project_root, payload)
        if event_name == "PreToolUse":
            return self._handle_pre_tool_use(project_root, payload)
        return None

    def _handle_aidocs_command(self, payload: dict[str, object]) -> dict[str, object]:
        """Handle /aidocs command — works on both initialized and uninitialized projects."""
        cwd = str(payload.get("cwd") or "").strip()
        project_root = Path(cwd).resolve() if cwd else None
        memory_exists = project_root and (project_root / ".MEMORY").is_dir() if project_root else False

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

    def _handle_user_prompt_submit(self, project_root: Path, payload: dict[str, object]) -> dict[str, object] | None:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return None

        # Lightweight path: classify + route only (no orchestration/sync).
        classification = self.runtime.classify_prompt_action(prompt)
        action_kind = str(classification.get("action_kind") or "understand")
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
            blocked_reason = str(route.get("blocked_reason") or "This prompt is blocked by AIDOCS runtime policy.")
            return {
                "decision": "block",
                "reason": blocked_reason,
            }

        additional_context = self._build_lightweight_prompt_context(
            action_kind=action_kind,
            route=route,
            project_root=project_root,
        )
        if not additional_context:
            return None

        self._record_classification_event(project_root, action_kind, prompt)

        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            }
        }

    def _handle_pre_tool_use(self, project_root: Path, payload: dict[str, object]) -> dict[str, object] | None:
        managed = self.runtime.hub.managed_mode.get_mode(project_root)
        if not managed.get("active"):
            return None

        session_id = str(managed.get("session_id") or "").strip()
        tool_name = str(payload.get("tool_name") or "").strip()
        workflow_summary = self._build_compiled_workflow_summary(self.runtime.hub.workflow.read_compiled(project_root))
        additional_context = (
            "AIDOCS-managed mode is active"
            + (f" for session `{session_id}`" if session_id else "")
            + ". Keep MCP-first behavior: prefer session-guided retrieval over ad-hoc repo scanning, "
            "and when the work is an edit task, maintain task lifecycle state with `task_begin` and `task_complete`."
        )
        if workflow_summary:
            additional_context += f" Compiled workflow actions: {workflow_summary}."

        # Surface pending post-action workflow items for edit-type tools
        if tool_name.lower() in {"edit", "write", "bash"}:
            pending = self.runtime._collect_pending_workflow("edit", project_root)
            if pending:
                additional_context += f" When this edit task completes, these workflow actions are pending: {pending}."
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": additional_context,
            }
        }

    def _record_hook_event(self, project_root: Path, event_name: str, payload: dict[str, object]) -> None:
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            session_id = str(managed.get("session_id") or "").strip() or None
            tool_name = str(payload.get("tool_name") or "").strip() or None
            prompt = str(payload.get("prompt") or "").strip() or None
            event_kind = event_name.lower()
            payload_summary = {
                key: value
                for key, value in payload.items()
                if key in {"hook_event_name", "tool_name", "tool_input", "prompt", "cwd"}
            }
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
                },
            )
        except Exception as exc:
            logger.debug("Failed to record hook event: %s", exc)
            return None

    def _resolve_project_root(self, payload: dict[str, object]) -> Path | None:
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            return None

        project_root = Path(cwd).resolve()
        memory_root = project_root / ".MEMORY"
        if not memory_root.is_dir():
            self._log_resolution_failure(project_root, "missing .MEMORY directory")
            return None

        if not ((project_root / "AGENTS.md").is_file() or (project_root / "CLAUDE.md").is_file()):
            self._log_resolution_failure(project_root, "missing AGENTS.md and CLAUDE.md")
            return None

        return project_root

    def _record_classification_event(self, project_root: Path, action_kind: str, prompt: str) -> None:
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
    ) -> str:
        """Build context from classification + route only (no orchestration data)."""
        session_id = str(route.get("session_id") or "").strip()
        recommended = route.get("recommended_mcp_flow") if isinstance(route.get("recommended_mcp_flow"), list) else []
        recommended_text = ", ".join(str(item) for item in recommended if str(item).strip())

        parts = [
            "AIDOCS-managed mode is active for this project.",
            f"AIDOCS suggests action kind: `{action_kind}` (advisory — use your judgment if the classification seems wrong).",
        ]
        if session_id:
            parts.append(f"Bound session: `{session_id}`.")
        if route.get("allowed_direct_inspection"):
            parts.append("Inspect the explicit target first, then return to MCP-first flow for broader work.")
        else:
            parts.append("Route this turn through the AIDOCS MCP flow before broad repo inspection.")
        if recommended_text:
            parts.append(f"Recommended MCP flow: {recommended_text}.")

        action_directive = self._action_directive(action_kind)
        if action_directive:
            parts.append(action_directive)

        parts.append("Avoid ad-hoc broad repo scanning when the MCP routing result already provides the path forward.")
        return " ".join(parts)

    def _build_prompt_context(self, result: dict[str, object]) -> str:
        classification = result.get("classification") if isinstance(result.get("classification"), dict) else {}
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        orchestration = result.get("orchestration") if isinstance(result.get("orchestration"), dict) else {}

        action_kind = str(classification.get("action_kind") or "understand")
        mode = str(result.get("mode") or "")
        session_id = str(route.get("session_id") or orchestration.get("selected_session_id") or "").strip()
        recommended = route.get("recommended_mcp_flow") if isinstance(route.get("recommended_mcp_flow"), list) else []
        recommended_text = ", ".join(str(item) for item in recommended if str(item).strip())
        retrieval = orchestration.get("retrieval") if isinstance(orchestration.get("retrieval"), dict) else {}
        retrieval_mode = str(retrieval.get("mode") or "")
        # Prefer workflow from orchestration result (avoids re-reading)
        workflow = orchestration.get("workflow") if isinstance(orchestration.get("workflow"), dict) else {}
        if not workflow:
            # Fallback: try bootstrap sync path
            bootstrap = orchestration.get("bootstrap") if isinstance(orchestration.get("bootstrap"), dict) else {}
            sync = bootstrap.get("sync") if isinstance(bootstrap.get("sync"), dict) else {}
            workflow = sync.get("workflow") if isinstance(sync.get("workflow"), dict) else {}

        parts = [
            "AIDOCS-managed mode is active for this project.",
            f"AIDOCS suggests action kind: `{action_kind}` (advisory — use your judgment if the classification seems wrong).",
        ]
        if session_id:
            parts.append(f"Bound session: `{session_id}`.")
        if mode == "mcp_orchestrated":
            parts.append("Route this turn through the AIDOCS MCP flow before broad repo inspection.")
        elif mode == "direct_inspection_allowed":
            parts.append("Inspect the explicit target first, then return to MCP-first flow for broader work.")
        if retrieval_mode:
            parts.append(f"Current retrieval mode: `{retrieval_mode}`.")
        if recommended_text:
            parts.append(f"Recommended MCP flow: {recommended_text}.")

        action_directive = self._action_directive(action_kind)
        if action_directive:
            parts.append(action_directive)

        workflow_summary = self._build_compiled_workflow_summary(workflow)
        if workflow_summary:
            parts.append(f"Compiled workflow actions: {workflow_summary}.")
        parts.append("Avoid ad-hoc broad repo scanning when the MCP routing result already provides the path forward.")
        return " ".join(parts)

    _ACTION_DIRECTIVES: dict[str, str] = {
        "write_memory": (
            "IMPORTANT: Use the `memory_capture` MCP tool to persist this memory. "
            "Do NOT write memory files manually or use Claude auto-memory (~/.claude/projects/*/memory/). "
            "Always provide a `target_hint` parameter to route to the correct file: "
            "'workflow' for task/git/deploy rules, 'coding-standards' for code style, "
            "'communication' for response style, 'design' for UI/visual preferences, "
            "'security' for auth/credentials, 'project-state' for project decisions, "
            "'user-profile' for user info. This works regardless of the user's language."
        ),
        "task_begin": (
            "Use the `task_begin` MCP tool to register the task before starting work."
        ),
        "task_complete": (
            "Use the `task_complete` MCP tool to finalize the task."
        ),
        "task_update": (
            "Use the `task_update` MCP tool to record progress on the current task."
        ),
        "trace": (
            "Use MCP `code_trace_*` and `code_find_*` tools to trace references and data flow. "
            "Prefer indexed retrieval over manual grep."
        ),
        "understand": (
            "Use MCP retrieval tools (`code_get_*_bundle`, `schema_get_entity`, `memory_read`) "
            "for context-aware analysis before answering."
        ),
        "code_bundle": (
            "Use MCP `code_get_context_bundle` or `code_get_session_bundle` for session-guided code retrieval."
        ),
        "edit": (
            "This is an edit task. Use `task_begin` before starting work and `task_complete` when done. "
            "Prefer MCP retrieval for context before making changes."
        ),
        "inspect": (
            "Use MCP retrieval tools to inspect the target. Prefer indexed data over raw file reads."
        ),
        "read_error": (
            "Analyze the error. Use MCP tools to find related code, then explain the root cause."
        ),
    }

    def _action_directive(self, action_kind: str) -> str:
        return self._ACTION_DIRECTIVES.get(action_kind, "")

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


def main() -> None:
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    response = ClaudeHookHandler().handle(payload)
    if response is not None:
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
