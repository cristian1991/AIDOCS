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
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        workflow_summary = self._build_compiled_workflow_summary(self.runtime.hub.workflow.read_compiled(project_root))
        additional_context = (
            "AIDOCS-managed mode is active"
            + (f" for session `{session_id}`" if session_id else "")
            + ". Keep MCP-first behavior: prefer session-guided retrieval over ad-hoc repo scanning, "
            "and when the work is an edit task, maintain task lifecycle state with `task_begin` and `task_complete`."
        )
        if workflow_summary:
            additional_context += f" Compiled workflow actions: {workflow_summary}."

        # Nudge toward MCP alternatives when raw tools are used for code exploration
        mcp_nudge = self._suggest_mcp_alternative(tool_name, tool_input)
        if mcp_nudge:
            additional_context += f" {mcp_nudge}"

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

    _MCP_ALTERNATIVES: dict[str, list[tuple[str, str]]] = {
        "grep": [
            ("code_search_symbols", "Find symbols by name, kind, or role"),
            ("code_find_references", "Find all usages of a symbol across the codebase"),
            ("schema_find_field", "Find a DB field/column across all entities"),
            ("code_get_style_bundle", "Find CSS rules matching class names"),
        ],
        "read": [
            ("code_get_outline", "Understand file structure without reading the whole file"),
            ("code_get_symbol_snippet", "Read just one symbol's code at a known location"),
            ("code_get_file_bundle", "Get file outline + deps + schema hints in one call"),
        ],
        "glob": [
            ("code_search", "Find files by path/summary keywords"),
            ("code_find_partial_group", "Find all partial class files for a C# type"),
        ],
    }

    def _suggest_mcp_alternative(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Suggest an MCP tool when a raw tool is used for code exploration."""
        lower = tool_name.lower()
        alternatives = self._MCP_ALTERNATIVES.get(lower)
        if not alternatives:
            return ""

        # Build a contextual suggestion based on what the tool appears to be doing
        parts = ["WARNING: You are using a raw tool. AIDOCS MCP tools should be used first. Try these instead:"]

        if lower == "grep":
            pattern = str(tool_input.get("pattern", "")).strip()
            if pattern:
                # Detect common grep patterns that have MCP equivalents
                if any(kw in pattern.lower() for kw in ("class ", "interface ", "def ", "function ", "enum ")):
                    parts.append("`code_search_symbols` — search indexed outlines by name/kind/role.")
                elif any(kw in pattern.lower() for kw in (".css", "class=", "className")):
                    parts.append("`code_get_style_bundle` — find CSS rules matching class names.")
                elif "." in pattern and not pattern.startswith("."):
                    parts.append("`code_find_references` — find all usages of a symbol.")
                else:
                    parts.append("`code_search_symbols` for symbol search, `code_find_references` for usage tracing.")
            else:
                parts.append(", ".join(f"`{name}` ({desc})" for name, desc in alternatives[:2]))

        elif lower == "read":
            path = str(tool_input.get("file_path", "")).strip()
            offset = tool_input.get("offset")
            if path and not offset:
                # Reading a whole file — suggest outline first
                parts.append("`code_get_outline` to understand structure, `code_get_file_bundle` for full context.")
            elif path and offset:
                # Reading a specific section — suggest snippet
                parts.append("Use `code_find(mode=\"symbols\")` first if the exact symbol is not known, then `code_get_symbol_snippet` for the exact symbol.")
            else:
                parts.append("`code_find(mode=\"symbols\")` to locate the exact symbol first, then `code_get_symbol_snippet` to read only that symbol.")

        elif lower == "glob":
            parts.append("`code_search` for indexed file search by keywords.")

        else:
            parts.append(", ".join(f"`{name}` ({desc})" for name, desc in alternatives[:2]))

        return " ".join(parts)

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

    _TOOL_FIRST_PREAMBLE = (
        "MANDATORY: Use AIDOCS MCP tools FIRST. Fall back to raw Read/Grep only if MCP returns empty."
    )

    _ACTION_DIRECTIVES: dict[str, str] = {
        "write_memory": (
            "Use `memory_capture` with `target_hint` (workflow/coding-standards/security/project-state/user-profile). "
            "Do NOT write memory files manually."
        ),
        "task_begin": "Use `task_begin` to register the task before starting work.",
        "task_complete": "Use `task_complete` to finalize the task.",
        "task_update": "Use `task_update` to record progress on the current task.",
        "trace": (
            '`code_find(query, mode="references")` → `code_trace(query, mode="field_flow")` → '
            '`code_trace(query, mode="css_class")`. '
            'DB: `schema_query(query, mode="trace_path")`. API→UI: `code_trace(query, mode="api_to_ui")`.'
        ),
        "understand": (
            "`code_get_outline` (structure) → `code_find(query, mode=\"symbols\")` (find symbol) → "
            "`code_get_symbol_snippet` (read it). "
            "Precision: `code_get_method_signature`, `code_get_constructor_params`, `code_get_enum_values`, `code_get_service_api`. "
            'Broad: `code_bundle(concept, mode="subsystem")`. DB: `schema_query(name, mode="entity|properties|batch_entity")`.'
        ),
        "code_bundle": (
            '`code_bundle(path, mode="context", session_id=...)` (session-guided) or '
            '`code_bundle(path, mode="file")` (single file).'
        ),
        "edit": (
            '`task_begin` → `code_get_outline` → `code_find(query, mode="symbols")` → Edit → `task_complete`. '
            'CSS: add `code_trace(class, mode="css_class")`. API: add `code_trace(concept, mode="api_to_ui")`. '
            'Precision: add `code_get_method_signature` / `code_get_service_api` / `code_get_constructor_params` / `code_get_enum_values`. '
            'DB: add `schema_query(entity, mode="entity|properties|batch_entity")`.'
        ),
        "test_heavy": (
            'If test/support code matters, re-run retrieval with test-inclusive indexing where the tool supports it. '
            'Then prefer: `code_get_service_api` → `code_get_method_signatures` → `code_get_constructor_params_batch` → `code_get_enum_values` → `code_get_entity_properties`. '
            'Do not guess property names, constructor params, enum members, or service surfaces when the precision chain can confirm them first.'
        ),
        "inspect": (
            "`code_get_outline` → `code_get_dependencies` / `code_find_dependents` → "
            "`code_get_modules` (project boundaries). Read only after narrowing."
        ),
        "read_error": (
            '`code_find(symbol, mode="symbols")` (find it) → `code_find(symbol, mode="references")` (trace) → '
            '`code_get_symbol_snippet` (read method). DB: add `schema_query(entity, mode="entity")`.'
        ),
        "investigate": (
            "`code_investigate(concept, depth=..., focus=...)` for a guided navigation plan. "
            'Or: `code_bundle(concept, mode="subsystem")` → narrow with '
            '`code_find(concept, mode="mutations|validation|policy")`.'
        ),
    }

    def _action_directive(self, action_kind: str) -> str:
        directive = self._ACTION_DIRECTIVES.get(action_kind, "")
        if directive and action_kind not in ("write_memory", "task_begin", "task_complete", "task_update"):
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


def main() -> None:
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    response = ClaudeHookHandler().handle(payload)
    if response is not None:
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
