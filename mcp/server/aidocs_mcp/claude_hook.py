from __future__ import annotations

import json
import sys
from pathlib import Path

from .runtime_service import RuntimeService
from .service_hub import AidocsServiceHub


def _resolve_templates_root() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "build" / ".MEMORY" / ".aidocs" / "templates"


def _resolve_script_root() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "build" / "scripts"


class ClaudeHookHandler:
    def __init__(self) -> None:
        hub = AidocsServiceHub(templates_root=_resolve_templates_root(), script_root=_resolve_script_root())
        self.runtime = RuntimeService(hub)

    def handle(self, payload: dict[str, object]) -> dict[str, object] | None:
        event_name = str(payload.get("hook_event_name") or "").strip()
        if not event_name:
            return None

        project_root = self._resolve_project_root(payload)
        if project_root is None:
            return None

        if event_name == "UserPromptSubmit":
            return self._handle_user_prompt_submit(project_root, payload)
        if event_name == "PreToolUse":
            return self._handle_pre_tool_use(project_root)
        return None

    def _handle_user_prompt_submit(self, project_root: Path, payload: dict[str, object]) -> dict[str, object] | None:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return None

        if prompt.startswith("/aidocs"):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "AIDOCS entry command detected. Use the MCP bootstrap/orchestrator flow for this project, "
                        "report selected session and managed-mode state, and avoid broad repo reads before session routing completes."
                    ),
                }
            }

        result = self.runtime.aidocs_handle_prompt(
            project_root,
            user_request=prompt,
            action_kind="auto",
            include_code_bundle=False,
            include_tests=False,
        )
        mode = str(result.get("mode") or "")

        if mode == "requires_aidocs_entry":
            return {
                "decision": "block",
                "reason": "Run /aidocs first to activate AIDOCS-managed mode for this project.",
            }

        if mode == "blocked":
            route = result.get("route") if isinstance(result.get("route"), dict) else {}
            blocked_reason = str(route.get("blocked_reason") or "This prompt is blocked by AIDOCS runtime policy.")
            return {
                "decision": "block",
                "reason": blocked_reason,
            }

        additional_context = self._build_prompt_context(result)
        if not additional_context:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            }
        }

    def _handle_pre_tool_use(self, project_root: Path) -> dict[str, object] | None:
        managed = self.runtime.hub.managed_mode.get_mode(project_root)
        if not managed.get("active"):
            return None

        session_id = str(managed.get("session_id") or "").strip()
        workflow_summary = self._build_compiled_workflow_summary(self.runtime.hub.workflow.read_compiled(project_root))
        additional_context = (
            "AIDOCS-managed mode is active"
            + (f" for session `{session_id}`" if session_id else "")
            + ". Keep MCP-first behavior: prefer session-guided retrieval over ad-hoc repo scanning, "
            "and when the work is an edit task, maintain task lifecycle state with `task_begin` and `task_complete`."
        )
        if workflow_summary:
            additional_context += f" Compiled workflow actions: {workflow_summary}."
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": additional_context,
            }
        }

    def _resolve_project_root(self, payload: dict[str, object]) -> Path | None:
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            return None

        project_root = Path(cwd).resolve()
        memory_root = project_root / ".MEMORY"
        if not memory_root.is_dir():
            return None

        if not ((project_root / "AGENTS.md").is_file() or (project_root / "CLAUDE.md").is_file()):
            return None

        return project_root

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
        bootstrap = orchestration.get("bootstrap") if isinstance(orchestration.get("bootstrap"), dict) else {}
        sync = bootstrap.get("sync") if isinstance(bootstrap.get("sync"), dict) else {}
        workflow = sync.get("workflow") if isinstance(sync.get("workflow"), dict) else {}

        parts = [
            "AIDOCS-managed mode is active for this project.",
            f"Classified action: `{action_kind}`.",
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
        workflow_summary = self._build_compiled_workflow_summary(workflow)
        if workflow_summary:
            parts.append(f"Compiled workflow actions: {workflow_summary}.")
        parts.append("Avoid ad-hoc broad repo scanning when the MCP routing result already provides the path forward.")
        return " ".join(parts)

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
