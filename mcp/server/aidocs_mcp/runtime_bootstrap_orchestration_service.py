from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import _parse_bool


class RuntimeBootstrapOrchestrationService:
    def __init__(self, runtime: Any, logger: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub
        self._logger = logger

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
            result["report"] = self.runtime._build_bootstrap_report(result)
            return result

        repaired = None
        structure_gaps = self.runtime.project_structure_gaps(project_root)
        if structure_gaps:
            repaired = self.runtime.project_init(
                project_root, init_git=False, create_remote=False
            )

        # Ensure .mcp.json is present for Claude Code (idempotent)
        try:
            self.runtime.ensure_claude_mcp_config(project_root)
        except Exception as exc:
            logger.debug("Failed to ensure .mcp.json: %s", exc)

        # Import TOML config into SQLite on first bootstrap (idempotent, no overwrite)
        try:
            from .config_store import ConfigStore
            _config_store = ConfigStore()
            toml_path = project_root / "aidocs.toml"
            if toml_path.is_file():
                _config_store.import_from_toml(project_root, toml_path, scope="project", overwrite=False)
        except Exception as exc:
            logger.debug("Config import from TOML: %s", exc)

        sync_result = self.runtime._sync_bootstrap_indexes(
            project_root, include_tests=include_tests
        )

        legacy_state = self.hub.legacy.inspect_legacy(project_root)
        sessions = self.hub.sessions.list_sessions(project_root)
        if legacy_state.get("legacy_present") and len(sessions) == 0:
            proposal = self.hub.legacy.build_session_proposal(
                project_root, session_id=session_id
            )
            result = {
                "stage": "migration_required",
                "ready": False,
                "initialized": True,
                "indexes_synced": True,
                "repaired": repaired,
                "sync": sync_result,
                "legacy": legacy_state,
                "proposal": proposal,
                "next_step": "issue_stop_for_migration_choice",
            }
            result["report"] = self.runtime._build_bootstrap_report(result)
            return result

        session_result = self.runtime.session_start(
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
                session_result["code_bundle"] = self.runtime._refresh_session_code_bundle(
                    project_root,
                    session_id=selected_session_id,
                    include_tests=include_tests,
                    sync_indexes=False,
                )

        result = {
            "stage": "session_active"
            if not session_result.get("requires_session_selection")
            else "session_selection_required",
            "ready": not session_result.get("requires_session_selection"),
            "initialized": True,
            "indexes_synced": True,
            "repaired": repaired,
            "repo_summary": self.runtime.repo_summary(project_root),
            "sync": sync_result,
            "session": session_result,
        }
        selected = (
            session_result.get("selected_session")
            if isinstance(session_result.get("selected_session"), dict)
            else {}
        )
        selected_session_id = str(selected.get("session_id") or "").strip() or None
        result["project_overview"] = self.runtime._build_project_overview(
            project_root,
            repo_summary=result.get("repo_summary")
            if isinstance(result.get("repo_summary"), dict)
            else None,
            selected_session_id=selected_session_id,
            stage=str(result.get("stage") or "unknown"),
            ready=bool(result.get("ready")),
        )
        if selected_session_id:
            result["session_overview"] = session_result.get("session_overview")
            result["skills_overview"] = session_result.get("skills_overview")
            selected_sections = (
                selected.get("sections")
                if isinstance(selected.get("sections"), dict)
                else {}
            )
            goal_values = self.runtime._clean_bullets(selected_sections.get("Goal", []))
            result["plan_overview"] = self.runtime._build_default_plan_overview(
                session_id=selected_session_id,
                end_goal=goal_values[0] if goal_values else None,
            )

        # Without rules injection, AIDOCS operates in MCP-tool-only mode —
        # the agent can use indexed retrieval but does not follow any /.MEMORY/rules/ directives.
        effective_config = self.runtime.effective_config(
            project_root, session_id=selected_session_id
        )
        agent_config = (
            effective_config.get("agent")
            if isinstance(effective_config.get("agent"), dict)
            else {}
        )
        inject_rules = _parse_bool(
            agent_config.get("inject_rules_on_bootstrap"), default=True
        )
        if inject_rules:
            rules = self.runtime._load_project_rules(project_root)
            if rules:
                result["rules"] = rules

        # Emit resolved-config.json so host plugins (OC, Cursor) can read
        # the fully-merged config without re-implementing TOML resolution.
        try:
            self.runtime._config_resolver.write_resolved_config(
                project_root=project_root, session_id=selected_session_id
            )
        except Exception as exc:
            logger.debug("Failed to write resolved-config.json: %s", exc)

        result["report"] = self.runtime._build_bootstrap_report(result)
        return result
