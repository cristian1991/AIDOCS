from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from .service_hub import AidocsServiceHub

logger = logging.getLogger("aidocs.runtime")

def _resolve_action_tokens_dir() -> Path:
    """Find action_tokens directory: project root first, then legacy MCP location."""
    candidates = [
        Path(__file__).resolve().parents[3] / "action_tokens",  # project root
        Path(__file__).resolve().parent / "action_tokens",       # legacy: inside MCP package
    ]
    env_path = os.environ.get("AIDOCS_PATH")
    if env_path:
        candidates.insert(1, Path(env_path) / "action_tokens")
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]  # fallback to project root even if missing

_ACTION_TOKENS_DIR = _resolve_action_tokens_dir()


def _load_action_tokens(directory: Path | None = None) -> list[tuple[str, tuple[str, ...]]]:
    """Load action token mappings from all YAML files in the action_tokens directory.

    Returns an ordered list of (action_kind, tokens) tuples suitable for
    first-match classification.  Files are simple ``key: [- value]`` YAML
    parsed without PyYAML to avoid an extra dependency.
    """
    root = directory or _ACTION_TOKENS_DIR
    if not root.is_dir():
        logger.warning("action_tokens directory not found: %s", root)
        return []

    # Filter by configured languages
    from .config import LANGUAGES_ENABLED
    enabled = LANGUAGES_ENABLED.lower().strip()
    if enabled != "all":
        enabled_set = {lang.strip() for lang in enabled.split(",") if lang.strip()}
    else:
        enabled_set = None  # load all

    merged: dict[str, list[str]] = {}
    for yaml_file in sorted(root.glob("*.yaml")):
        if enabled_set is not None and yaml_file.stem not in enabled_set:
            continue
        current_key: str | None = None
        try:
            for raw_line in yaml_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                key_match = re.match(r"^(\w[\w_]*):\s*$", line)
                if key_match:
                    current_key = key_match.group(1)
                    continue
                item_match = re.match(r"^\s+-\s+(.+)$", line)
                if item_match and current_key:
                    token = item_match.group(1).strip()
                    if token:
                        merged.setdefault(current_key, []).append(token)
        except Exception as exc:
            logger.warning("Failed to load action tokens from %s: %s", yaml_file, exc)

    # Deduplicate tokens per action_kind while preserving order
    result: list[tuple[str, tuple[str, ...]]] = []
    for action_kind, tokens in merged.items():
        seen: set[str] = set()
        unique: list[str] = []
        for token in tokens:
            if token not in seen:
                seen.add(token)
                unique.append(token)
        result.append((action_kind, tuple(unique)))
    return result


class RuntimeService:
    """High-level runtime orchestration over sessions, memory, and indexes."""

    def __init__(self, hub: AidocsServiceHub) -> None:
        self.hub = hub
        self._action_token_mapping: list[tuple[str, tuple[str, ...]]] | None = None

    def _get_action_tokens(self) -> list[tuple[str, tuple[str, ...]]]:
        if self._action_token_mapping is None:
            self._action_token_mapping = _load_action_tokens()
        return self._action_token_mapping

    def ensure_claude_mcp_config(self, project_root: Path) -> dict[str, object]:
        """Ensure the target project has a .mcp.json with the aidocs MCP server entry.

        Idempotent: if the entry already exists and points to a valid path, no change is made.
        Returns a dict describing what happened.
        """
        mcp_json_path = project_root / ".mcp.json"
        aidocs_source_root = Path(__file__).resolve().parents[3]
        # Prefer AIDOCS_PATH env var if set (installed by installer)
        env_aidocs_path = os.environ.get("AIDOCS_PATH")
        if env_aidocs_path and Path(env_aidocs_path).is_dir():
            aidocs_source_root = Path(env_aidocs_path)
        mcp_server_dir = aidocs_source_root / "mcp" / "server"

        # Resolve the python executable — prefer the one running this process
        python_bin = sys.executable or shutil.which("python") or shutil.which("python3") or "python"

        new_entry = {
            "type": "stdio",
            "command": python_bin,
            "args": ["-m", "aidocs_mcp.mcp_server"],
            "env": {
                "PYTHONPATH": str(mcp_server_dir),
            },
        }

        # Read existing .mcp.json if present
        existing: dict[str, object] = {}
        if mcp_json_path.is_file():
            try:
                existing = json.loads(mcp_json_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to parse existing .mcp.json: %s", exc)
                existing = {}

        servers = existing.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            existing["mcpServers"] = servers

        # Check if aidocs entry already exists and is correct
        current = servers.get("aidocs")
        if isinstance(current, dict):
            current_pythonpath = (current.get("env") or {}).get("PYTHONPATH", "")
            if current_pythonpath == str(mcp_server_dir):
                return {
                    "action": "no_change",
                    "path": str(mcp_json_path),
                    "reason": "aidocs MCP entry already present and correct",
                }

        servers["aidocs"] = new_entry
        existing["mcpServers"] = servers
        mcp_json_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        action = "updated" if current else "created"
        return {
            "action": action,
            "path": str(mcp_json_path),
            "entry": new_entry,
        }

    def session_start(
        self,
        project_root: Path,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        sync_indexes: bool = True,
        include_tests: bool = False,
    ) -> dict[str, object]:
        if sync_indexes:
            self.hub.index.sync_all(project_root)
            self.hub.code.sync_code_files(project_root, include_tests=include_tests)

        startup_files = [
            "/.MEMORY/.aidocs/index.aidocs",
            "/.MEMORY/.aidocs/global-instructions.aidocs",
            "/.MEMORY/.aidocs/coding-standards.aidocs",
            "/.MEMORY/.aidocs/memory-system.aidocs",
            "/.MEMORY/INDEX.md",
        ]

        sessions = self.hub.sessions.list_sessions(project_root)
        session_summaries = [
            {
                "session_id": item.session_id,
                "title": item.title,
                "status": item.status,
                "owner": item.owner,
                "goal": item.goal,
                "last_updated": item.last_updated,
            }
            for item in sessions
        ]

        if session_id is None:
            active = [item for item in sessions if item.status == "active"]
            if len(active) == 1:
                session_id = active[0].session_id
            else:
                response = {
                    "startup_files": startup_files,
                    "requires_session_selection": True,
                    "reason": "no_unique_active_session",
                    "sessions": session_summaries,
                }
                response["report"] = self._build_session_start_report(response)
                return response

        session = self.hub.sessions.read_session(project_root, session_id)
        context = self.hub.sessions.read_context(project_root, session_id)

        if sync_indexes:
            self.hub.code.sync_session_code(project_root, session_id=session_id, include_tests=include_tests)

        response: dict[str, object] = {
            "startup_files": startup_files,
            "requires_session_selection": False,
            "selected_session": {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            },
            "context": {
                "path": str(context.path),
                "sections": context.sections,
            },
            "sessions": session_summaries,
        }

        if include_code_bundle:
            response["code_bundle"] = self.hub.code.get_context_bundle(project_root, session_id=session_id)

        response["report"] = self._build_session_start_report(response)

        return response

    def _registered_tools_snapshot(self) -> list[object]:
        from .mcp_server import create_server

        server = create_server()
        components = getattr(getattr(server, "_local_provider", None), "_components", {})
        return [component for key, component in components.items() if str(key).startswith("tool:")]

    def _sync_bootstrap_indexes(self, project_root: Path, include_tests: bool) -> dict[str, object]:
        workflow = self.hub.workflow.compile_project_rules(project_root)
        capabilities = self.hub.capabilities.sync_capabilities(project_root, self._registered_tools_snapshot())
        procedures = self.hub.procedures.sync_procedures(project_root, self.hub.workflow.read_compiled(project_root))
        links = self.hub.procedure_links.sync_links(
            project_root,
            self.hub.procedures.find_procedures(project_root, query=None, limit=1000),
            self.hub.capabilities.find_capabilities(project_root, query=None, limit=1000),
        )
        return {
            "memory": self.hub.index.sync_all(project_root),
            "code_manifest": {"code_files": self.hub.code.sync_code_files(project_root, include_tests=include_tests), "modules": self.hub.code.sync_modules(project_root)},
            "schema": self.hub.schema.sync_schema(project_root),
            "workflow": workflow,
            "capabilities": {"capability_definitions": capabilities},
            "procedures": {"procedure_definitions": procedures},
            "procedure_capability_links": {"links": links},
            "execution": self.hub.execution.execution_status(project_root),
        }

    def _build_session_start_report(self, response: dict[str, object]) -> dict[str, object]:
        if response.get("requires_session_selection"):
            sessions = response.get("sessions") if isinstance(response.get("sessions"), list) else []
            return {
                "headline": "Session selection is required before continuing.",
                "bullets": [f"Active/available sessions: {len(sessions)}."],
                "next_step": "select_session",
            }

        selected = response.get("selected_session") if isinstance(response.get("selected_session"), dict) else {}
        session_id = selected.get("session_id")
        bullets = [f"Selected session: {session_id}."] if session_id else []
        if response.get("code_bundle"):
            bullets.append("Context code bundle is included.")
        else:
            bullets.append("Context code bundle is deferred by default.")
        return {
            "headline": "Session context is ready.",
            "bullets": bullets,
            "next_step": None,
        }

    def _build_bootstrap_report(self, result: dict[str, object]) -> dict[str, object]:
        stage = str(result.get("stage") or "unknown")
        if stage == "setup_required":
            return {
                "headline": "AIDOCS project setup is required.",
                "bullets": [str(result.get("reason") or "Missing AIDOCS project structure.")],
                "next_step": result.get("next_step"),
            }
        if stage == "migration_required":
            return {
                "headline": "Legacy migration choice is required before continuing.",
                "bullets": ["Legacy runtime files are present and no session has been migrated yet."],
                "next_step": result.get("next_step"),
            }

        session = result.get("session") if isinstance(result.get("session"), dict) else {}
        selected = session.get("selected_session") if isinstance(session.get("selected_session"), dict) else {}
        sync = result.get("sync") if isinstance(result.get("sync"), dict) else {}
        capabilities = sync.get("capabilities") if isinstance(sync.get("capabilities"), dict) else {}
        procedures = sync.get("procedures") if isinstance(sync.get("procedures"), dict) else {}
        links = sync.get("procedure_capability_links") if isinstance(sync.get("procedure_capability_links"), dict) else {}
        bullets = []
        if selected.get("session_id"):
            bullets.append(f"Selected session: {selected.get('session_id')}.")
        bullets.append(
            f"Action surfaces synced: capabilities={capabilities.get('capability_definitions')}, procedures={procedures.get('procedure_definitions')}, links={links.get('links')}."
        )
        return {
            "headline": "Project bootstrap is ready.",
            "bullets": bullets,
            "next_step": None,
        }

    def _build_readiness_summary(
        self,
        *,
        bootstrap: dict[str, object],
        selected_session_id: str | None,
        managed_mode: dict[str, object] | None,
        operator_summary: dict[str, object] | None,
    ) -> dict[str, object]:
        sync = bootstrap.get("sync") if isinstance(bootstrap.get("sync"), dict) else {}
        workflow = sync.get("workflow") if isinstance(sync.get("workflow"), dict) else {}
        capabilities = sync.get("capabilities") if isinstance(sync.get("capabilities"), dict) else {}
        procedures = sync.get("procedures") if isinstance(sync.get("procedures"), dict) else {}
        links = sync.get("procedure_capability_links") if isinstance(sync.get("procedure_capability_links"), dict) else {}
        execution = sync.get("execution") if isinstance(sync.get("execution"), dict) else {}
        memory = sync.get("memory") if isinstance(sync.get("memory"), dict) else {}
        code_manifest = sync.get("code_manifest") if isinstance(sync.get("code_manifest"), dict) else {}
        schema = sync.get("schema") if isinstance(sync.get("schema"), dict) else {}

        return {
            "ready": bool(bootstrap.get("ready")),
            "stage": bootstrap.get("stage"),
            "selected_session_id": selected_session_id,
            "managed_mode_active": bool((managed_mode or {}).get("active")),
            "managed_mode_session_id": (managed_mode or {}).get("session_id"),
            "operator_state": (operator_summary or {}).get("overall_state") or (operator_summary or {}).get("state"),
            "indexes": {
                "memory_files": memory.get("memory_files"),
                "code_files": code_manifest.get("code_files"),
                "schema_entities": schema.get("entities"),
                "workflow_actions": workflow.get("action_count"),
                "capability_definitions": capabilities.get("capability_definitions"),
                "procedure_definitions": procedures.get("procedure_definitions"),
                "procedure_capability_links": links.get("links"),
                "execution_runs": execution.get("execution_runs"),
                "execution_events": execution.get("execution_events"),
            },
        }

    def _build_operator_report(
        self,
        *,
        readiness_summary: dict[str, object],
        operator_summary: dict[str, object] | None,
        bootstrap: dict[str, object],
        action_kind: str | None = None,
        project_root: Path | None = None,
    ) -> dict[str, object]:
        ready = bool(readiness_summary.get("ready"))
        stage = str(readiness_summary.get("stage") or "unknown")
        operator_state = str(readiness_summary.get("operator_state") or "unknown")
        selected_session_id = str(readiness_summary.get("selected_session_id") or "").strip() or None
        indexes = readiness_summary.get("indexes") if isinstance(readiness_summary.get("indexes"), dict) else {}

        if not ready:
            next_step = bootstrap.get("next_step") or bootstrap.get("stage")
            return {
                "headline": f"AIDOCS is not ready: {stage}.",
                "bullets": [
                    f"Next step: {next_step}.",
                ],
                "next_step": next_step,
            }

        bullets = []
        if selected_session_id:
            bullets.append(f"Active session: {selected_session_id}.")
        bullets.append(f"Operator state: {operator_state}.")
        bullets.append(
            "Index coverage: "
            f"memory={indexes.get('memory_files')}, "
            f"code={indexes.get('code_files')}, "
            f"schema={indexes.get('schema_entities')}, "
            f"capabilities={indexes.get('capability_definitions')}, "
            f"procedures={indexes.get('procedure_definitions')}, "
            f"links={indexes.get('procedure_capability_links')}."
        )
        next_step = None
        if isinstance(operator_summary, dict):
            attention_items = operator_summary.get("attention_items")
            if isinstance(attention_items, list) and attention_items:
                first_attention = attention_items[0]
                if isinstance(first_attention, dict):
                    steps = list(first_attention.get("recommended_next_steps") or [])
                    next_step = steps[0] if steps else None
            if next_step is None:
                steps = list(operator_summary.get("recommended_next_steps") or [])
                next_step = steps[0] if steps else None
            if next_step is None and str(operator_summary.get("overall_state") or "") == "healthy":
                next_step = "No immediate gap detected; continue monitoring execution history and drift."

        # Surface pending workflow actions for the current action_kind
        pending_workflow = self._collect_pending_workflow(action_kind, project_root)
        if pending_workflow:
            bullets.append(f"Pending workflow actions after `{action_kind}`: {pending_workflow}.")

        return {
            "headline": f"AIDOCS is ready in stage `{stage}`.",
            "bullets": bullets,
            "next_step": next_step,
        }

    def _build_handle_prompt_report(
        self,
        *,
        mode: str,
        classification: dict[str, object],
        route: dict[str, object],
        next_step: object = None,
        operator_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        action_kind = str(classification.get("action_kind") or "unknown")
        if mode == "requires_aidocs_entry":
            return {
                "headline": "Enter `/aidocs` first to work in managed mode.",
                "bullets": [f"Requested action kind: {action_kind}."],
                "next_step": next_step,
            }
        if mode == "blocked":
            return {
                "headline": "The requested action is blocked by current policy or routing state.",
                "bullets": [
                    f"Requested action kind: {action_kind}.",
                    f"Blocked reason: {route.get('blocked_reason')}.",
                ],
                "next_step": next_step,
            }
        if mode == "direct_inspection_allowed":
            return {
                "headline": "Direct inspection is allowed for the requested target.",
                "bullets": [
                    f"Requested action kind: {action_kind}.",
                    "Inspect the target first, then return to MCP orchestration for broader work.",
                ],
                "next_step": next_step,
            }
        if mode == "mcp_orchestrated" and isinstance(operator_report, dict):
            return operator_report
        return {
            "headline": "Prompt was classified and routed successfully.",
            "bullets": [f"Requested action kind: {action_kind}."],
            "next_step": next_step,
        }

    def project_bootstrap_or_resume(
        self,
        project_root: Path,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        agents = project_root / "AGENTS.md"
        claude = project_root / "CLAUDE.md"
        memory_root = project_root / ".MEMORY"

        initialized = memory_root.is_dir() and (agents.is_file() or claude.is_file())
        if not initialized:
            result = {
                "stage": "setup_required",
                "ready": False,
                "next_step": "project_init",
                "reason": "missing AIDOCS project structure",
            }
            result["report"] = self._build_bootstrap_report(result)
            return result

        # Ensure .mcp.json is present for Claude Code (idempotent)
        try:
            self.ensure_claude_mcp_config(project_root)
        except Exception as exc:
            logger.debug("Failed to ensure .mcp.json: %s", exc)

        sync_result = self._sync_bootstrap_indexes(project_root, include_tests=include_tests)

        legacy_state = self.hub.legacy.inspect_legacy(project_root)
        sessions = self.hub.sessions.list_sessions(project_root)
        if legacy_state.get("legacy_present") and len(sessions) == 0:
            proposal = self.hub.legacy.build_session_proposal(project_root, session_id=session_id)
            result = {
                "stage": "migration_required",
                "ready": False,
                "initialized": True,
                "indexes_synced": True,
                "sync": sync_result,
                "legacy": legacy_state,
                "proposal": proposal,
                "next_step": "issue_stop_for_migration_choice",
            }
            result["report"] = self._build_bootstrap_report(result)
            return result

        session_result = self.session_start(
            project_root,
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            sync_indexes=False,
            include_tests=include_tests,
        )

        if include_code_bundle and not session_result.get("requires_session_selection"):
            selected = session_result.get("selected_session") or {}
            selected_session_id = selected.get("session_id")
            if isinstance(selected_session_id, str) and selected_session_id.strip():
                session_result["code_bundle"] = self._refresh_session_code_bundle(
                    project_root,
                    session_id=selected_session_id,
                    include_tests=include_tests,
                    sync_indexes=False,
                )

        result = {
            "stage": "session_active" if not session_result.get("requires_session_selection") else "session_selection_required",
            "ready": not session_result.get("requires_session_selection"),
            "initialized": True,
            "indexes_synced": True,
            "sync": sync_result,
            "session": session_result,
        }
        result["report"] = self._build_bootstrap_report(result)
        return result

    def aidocs_orchestrate(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str = "understand",
        session_id: str | None = None,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        explicit_targets = [item for item in (explicit_targets or []) if str(item).strip()]

        preflight = self.hub.policy.preflight_action(
            project_root,
            action_kind=action_kind,
            session_id=session_id,
            user_explicit_targets=explicit_targets,
        )

        bootstrap = self.project_bootstrap_or_resume(
            project_root,
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

        result: dict[str, object] = {
            "request": user_request,
            "action_kind": action_kind,
            "preflight": preflight,
            "bootstrap": bootstrap,
        }

        if not bootstrap.get("ready"):
            result["readiness_summary"] = self._build_readiness_summary(
                bootstrap=bootstrap,
                selected_session_id=None,
                managed_mode=None,
                operator_summary=None,
            )
            result["operator_report"] = self._build_operator_report(
                readiness_summary=result["readiness_summary"],
                operator_summary=None,
                bootstrap=bootstrap,
                action_kind=action_kind,
                project_root=project_root,
            )
            result["report"] = result["operator_report"]
            result["next_step"] = bootstrap.get("next_step") or bootstrap.get("stage")
            return result

        selected = bootstrap["session"]["selected_session"]["session_id"]
        result["selected_session_id"] = selected
        result["managed_mode"] = self.hub.managed_mode.set_mode(project_root, session_id=selected, source="/aidocs")
        result["operator_summary"] = self.hub.action_surface.current_session_bundle(project_root, limit=10, max_queries=12)
        result["readiness_summary"] = self._build_readiness_summary(
            bootstrap=bootstrap,
            selected_session_id=selected,
            managed_mode=result.get("managed_mode") if isinstance(result.get("managed_mode"), dict) else None,
            operator_summary=result.get("operator_summary") if isinstance(result.get("operator_summary"), dict) else None,
        )
        result["operator_report"] = self._build_operator_report(
            readiness_summary=result["readiness_summary"],
            operator_summary=result.get("operator_summary") if isinstance(result.get("operator_summary"), dict) else None,
            bootstrap=bootstrap,
            action_kind=action_kind,
            project_root=project_root,
        )
        result["report"] = result["operator_report"]

        if explicit_targets:
            if include_code_bundle:
                file_bundles = []
                for target in explicit_targets:
                    normalized = target.replace("\\", "/")
                    if not self.hub.code._is_indexed_file(project_root, normalized):
                        file_bundles.append({"path": normalized, "missing": True})
                        continue
                    file_bundles.append(self.hub.code.get_file_bundle(project_root, normalized))
                result["retrieval"] = {
                    "mode": "explicit_targets",
                    "targets": explicit_targets,
                    "bundles": file_bundles,
                }
            else:
                result["retrieval"] = {
                    "mode": "explicit_targets_deferred",
                    "targets": explicit_targets,
                    "reason": "bundle_omitted_by_default",
                }
        else:
            if include_code_bundle:
                result["retrieval"] = {
                    "mode": "session_bundle",
                    "bundle": self.hub.code.get_context_bundle(project_root, session_id=selected),
                }
            else:
                preview = self.hub.sessions.session_code_targets(project_root, selected)
                result["retrieval"] = {
                    "mode": "session_bundle_deferred",
                    "session_id": selected,
                    "session_target_count": len([item for item in preview if item and item.strip()]),
                    "memory_structure": self._memory_structure_summary(project_root),
                    "reason": "bundle_omitted_by_default",
                }

        # Include compiled workflow actions so the host doesn't need to re-read
        try:
            result["workflow"] = self.hub.workflow.read_compiled(project_root)
        except Exception as exc:
            logger.warning("Failed to read workflow for orchestration result: %s", exc)
            result["workflow"] = None

        return result

    def aidocs_route_prompt(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
    ) -> dict[str, object]:
        managed = self.hub.managed_mode.get_mode(project_root)
        explicit_targets = [item for item in (explicit_targets or []) if str(item).strip()]

        if not managed.get("active"):
            return {
                "managed_mode": False,
                "action_kind": action_kind,
                "allowed_direct_inspection": bool(explicit_targets),
                "requires_session": False,
                "requires_task_lifecycle": False,
                "recommended_mcp_flow": ["/aidocs"],
                "blocked_reason": None,
            }

        session_id = managed.get("session_id")
        preflight = self.hub.policy.preflight_action(
            project_root,
            action_kind=action_kind,
            session_id=str(session_id) if session_id else None,
            user_explicit_targets=explicit_targets,
        )

        requires_task_lifecycle = action_kind in {"edit", "write_memory", "task_begin", "task_update", "task_complete"}
        recommended = ["runtime_preflight"]
        if preflight.get("requires_session"):
            recommended.append("session_start")
        if requires_task_lifecycle:
            recommended.append("task_begin")
        if action_kind in {"understand", "trace", "edit", "code_bundle"}:
            recommended.append("aidocs_orchestrate")

        blocked_reason = None
        if managed.get("active") and preflight.get("allowed") is False:
            blocked_reason = str(preflight.get("reason"))

        return {
            "managed_mode": True,
            "action_kind": action_kind,
            "session_id": session_id,
            "allowed_direct_inspection": bool(explicit_targets) and action_kind in {"inspect", "read_file", "read_error"},
            "requires_session": bool(preflight.get("requires_session")),
            "requires_task_lifecycle": requires_task_lifecycle,
            "recommended_mcp_flow": recommended,
            "blocked_reason": blocked_reason,
            "preflight": preflight,
        }

    def classify_prompt_action(self, user_request: str, explicit_targets: list[str] | None = None) -> dict[str, object]:
        text = user_request.strip().lower()
        explicit_targets = [item for item in (explicit_targets or []) if str(item).strip()]

        if explicit_targets:
            if any(token in text for token in ("error", "stack trace", "traceback", "log", "logs", "why")):
                action_kind = "read_error"
            else:
                action_kind = "inspect"
            return {"action_kind": action_kind, "why": ["explicit_targets"]}

        mapping = self._get_action_tokens()
        for action_kind, tokens in mapping:
            if any(token in text for token in tokens):
                return {"action_kind": action_kind, "why": [f"matched:{action_kind}"]}

        return {"action_kind": "understand", "why": ["default:understand"]}

    def aidocs_handle_prompt(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        if not action_kind or action_kind == "auto":
            classified = self.classify_prompt_action(user_request, explicit_targets=explicit_targets)
            action_kind = str(classified["action_kind"])
        else:
            classified = {"action_kind": action_kind, "why": ["provided"]}

        route = self.aidocs_route_prompt(
            project_root,
            user_request=user_request,
            action_kind=action_kind,
            explicit_targets=explicit_targets,
        )

        if not route.get("managed_mode"):
            return {
                "handled": False,
                "mode": "requires_aidocs_entry",
                "classification": classified,
                "route": route,
                "report": self._build_handle_prompt_report(
                    mode="requires_aidocs_entry",
                    classification=classified,
                    route=route,
                    next_step="/aidocs",
                ),
                "next_step": "/aidocs",
            }

        if route.get("blocked_reason"):
            return {
                "handled": False,
                "mode": "blocked",
                "classification": classified,
                "route": route,
                "report": self._build_handle_prompt_report(
                    mode="blocked",
                    classification=classified,
                    route=route,
                    next_step=route.get("recommended_mcp_flow"),
                ),
                "next_step": route.get("recommended_mcp_flow"),
            }

        session_id = route.get("session_id")
        if action_kind in {"inspect", "read_file", "read_error"} and route.get("allowed_direct_inspection"):
            return {
                "handled": True,
                "mode": "direct_inspection_allowed",
                "classification": classified,
                "route": route,
                "selected_session_id": session_id,
                "report": self._build_handle_prompt_report(
                    mode="direct_inspection_allowed",
                    classification=classified,
                    route=route,
                    next_step="inspect_target_then_return_to_mcp_for_broader_work",
                ),
                "next_step": "inspect_target_then_return_to_mcp_for_broader_work",
            }

        if action_kind in {"understand", "trace", "code_bundle", "edit", "write_memory"}:
            orchestration = self.aidocs_orchestrate(
                project_root,
                user_request=user_request,
                action_kind=action_kind,
                session_id=str(session_id) if session_id else None,
                explicit_targets=explicit_targets,
                include_code_bundle=include_code_bundle,
                include_tests=include_tests,
            )
            return {
                "handled": True,
                "mode": "mcp_orchestrated",
                "classification": classified,
                "route": route,
                "operator_report": orchestration.get("operator_report"),
                "readiness_summary": orchestration.get("readiness_summary"),
                "report": self._build_handle_prompt_report(
                    mode="mcp_orchestrated",
                    classification=classified,
                    route=route,
                    operator_report=orchestration.get("operator_report") if isinstance(orchestration.get("operator_report"), dict) else None,
                ),
                "orchestration": orchestration,
            }

        return {
            "handled": True,
            "mode": "preflight_only",
            "classification": classified,
            "route": route,
            "report": self._build_handle_prompt_report(
                mode="preflight_only",
                classification=classified,
                route=route,
                next_step=route.get("recommended_mcp_flow"),
            ),
        }

    def task_begin(
        self,
        project_root: Path,
        session_id: str,
        goal: str | None = None,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = True,
        include_tests: bool = False,
    ) -> dict[str, object]:
        session_patch: dict[str, list[str]] = {"Status": ["- active"]}
        if goal is not None:
            session_patch["Goal"] = [f"- {goal}"]
        if state is not None:
            session_patch["State"] = self._as_bullets(state)
        if upcoming is not None:
            session_patch["Upcoming"] = self._as_bullets(upcoming)
        if blockers is not None:
            session_patch["Blockers"] = self._as_bullets(blockers)
        session = self.hub.sessions.update_session(project_root, session_id, session_patch)

        context_patch: dict[str, list[str]] = {}
        if relevant_files is not None:
            context_patch["Relevant Files"] = self._as_file_bullets(relevant_files)
        if relevant_commands is not None:
            context_patch["Relevant Commands"] = self._as_bullets(relevant_commands)
        if relevant_snippets is not None:
            context_patch["Relevant Snippets"] = self._as_code_block(relevant_snippets)
        if session_facts is not None:
            context_patch["Session Facts"] = self._as_bullets(session_facts)
        if constraints is not None:
            context_patch["Constraints"] = self._as_bullets(constraints)
        context = self.hub.sessions.update_context(project_root, session_id, context_patch) if context_patch else self.hub.sessions.read_context(project_root, session_id)

        result: dict[str, object] = {
            "session": {"session_id": session.session_id, "path": str(session.path), "sections": session.sections},
            "context": {"path": str(context.path), "sections": context.sections},
        }
        if include_code_bundle:
            result["code_bundle"] = self._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )
        return result

    def task_update(
        self,
        project_root: Path,
        session_id: str,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        return self.task_begin(
            project_root=project_root,
            session_id=session_id,
            goal=None,
            state=state,
            upcoming=upcoming,
            blockers=blockers,
            relevant_files=relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=relevant_snippets,
            session_facts=session_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    def task_complete(
        self,
        project_root: Path,
        session_id: str,
        result_summary: str,
        next_status: str = "done",
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        session = self.hub.sessions.read_session(project_root, session_id)
        existing_state = self._clean_bullets(session.sections.get("State", []))
        existing_state.append(result_summary)
        session_patch = {
            "Status": [f"- {next_status}"],
            "State": self._as_bullets(existing_state),
        }
        updated = self.hub.sessions.update_session(project_root, session_id, session_patch)

        # Auto-journal the task completion
        try:
            self.hub.sessions.write_journal_entry(
                project_root, session_id,
                action_kind="task_complete",
                intent=result_summary[:120],
                outcome=f"completed → {next_status}",
            )
        except Exception:
            pass  # journal is best-effort, never block task_complete

        result: dict[str, object] = {
            "session": {"session_id": updated.session_id, "path": str(updated.path), "sections": updated.sections}
        }
        if include_code_bundle:
            result["code_bundle"] = self._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )
        return result

    def _refresh_session_code_bundle(
        self,
        project_root: Path,
        session_id: str,
        include_tests: bool = False,
        sync_indexes: bool = False,
    ) -> dict[str, object]:
        if sync_indexes:
            self.hub.index.sync_all(project_root)
            self.hub.code.sync_code_files(project_root, include_tests=include_tests)
        self.hub.code.sync_session_code(project_root, session_id=session_id, include_tests=include_tests)
        return self.hub.code.get_context_bundle(project_root, session_id=session_id)

    def _as_bullets(self, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        return [f"- {item}" for item in cleaned] or ["-"]

    def _as_file_bullets(self, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        return [f"- `{item}`" for item in cleaned]

    def _as_code_block(self, values: list[str]) -> list[str]:
        cleaned = [item.rstrip() for item in values if item and item.strip()]
        if not cleaned:
            return []
        return ["```text", *cleaned, "```"]

    def _clean_bullets(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            stripped = item.strip()
            if not stripped or stripped == "-":
                continue
            if stripped.startswith("-"):
                stripped = stripped[1:].strip()
            if stripped:
                result.append(stripped)
        return result

    def _collect_pending_workflow(self, action_kind: str | None, project_root: Path | None) -> str:
        """Collect pending workflow actions for a given action_kind and format as a summary string."""
        if not action_kind or not project_root:
            return ""
        try:
            triggers = self.hub.workflow.triggers_for_action_kind(action_kind)
            if not triggers:
                return ""
            pending: list[dict[str, object]] = []
            for trigger in triggers:
                pending.extend(self.hub.workflow.pending_actions_for_trigger(project_root, trigger))
            if not pending:
                return ""
            # Record trigger evaluation event
            try:
                managed = self.hub.managed_mode.get_mode(project_root)
                session_id = str(managed.get("session_id") or "").strip() or None
                self.hub.execution.record_event(
                    project_root,
                    event_kind="workflow_trigger_evaluated",
                    source_kind="operator_report",
                    session_id=session_id,
                    action_kind=action_kind,
                    status="pending",
                    payload={
                        "triggers": triggers,
                        "pending_count": len(pending),
                        "pending_actions": [
                            {"trigger": a.get("trigger"), "kind": a.get("kind")}
                            for a in pending[:5]
                        ],
                    },
                )
            except Exception as exc:
                logger.debug("Failed to record workflow trigger evaluation event: %s", exc)
            parts = []
            for action in pending[:3]:
                trigger = action.get("trigger", "?")
                kind = action.get("kind", "?")
                parts.append(f"`{trigger} → {kind}`")
            if len(pending) > 3:
                parts.append(f"and {len(pending) - 3} more")
            return ", ".join(parts)
        except Exception as exc:
            logger.warning("Failed to collect pending workflow for action_kind=%s: %s", action_kind, exc)
            return ""

    def _memory_structure_summary(self, project_root: Path) -> dict[str, object]:
        root = project_root / ".MEMORY"
        sections: list[dict[str, object]] = []

        def add_file_section(name: str, relative_dir: str, legacy: bool = False) -> None:
            directory = root / relative_dir
            if not directory.exists():
                return
            files = sorted(path.name for path in directory.glob("*.md") if path.is_file())
            if not files and relative_dir != "config":
                return
            sections.append(
                {
                    "name": name,
                    "file_count": len(files),
                    "samples": files[:3],
                    "legacy": legacy,
                }
            )

        sessions = self.hub.sessions.list_sessions(project_root)
        archived_sessions_root = root / "archive" / "sessions"
        archived_sessions = 0
        if archived_sessions_root.exists():
            archived_sessions = sum(1 for path in archived_sessions_root.iterdir() if path.is_dir())
        sections.append(
            {
                "name": "sessions",
                "active_count": len(sessions),
                "archived_count": archived_sessions,
                "legacy": False,
            }
        )

        add_file_section("rules", "rules")
        add_file_section("domains", "domains")
        add_file_section("system", "system")
        add_file_section("config", "config")
        add_file_section("daily", "daily")
        add_file_section("archive", "archive")
        add_file_section("policy", "policy", legacy=True)
        add_file_section("architecture", "architecture", legacy=True)
        add_file_section("operations", "operations", legacy=True)
        add_file_section("decisions", "decisions", legacy=True)

        return {
            "router_files": ["/.MEMORY/.aidocs/index.aidocs", "/.MEMORY/INDEX.md"],
            "sections": sections,
        }
