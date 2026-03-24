from __future__ import annotations

from pathlib import Path

from .service_hub import AidocsServiceHub


class RuntimeService:
    """High-level runtime orchestration over sessions, memory, and indexes."""

    def __init__(self, hub: AidocsServiceHub) -> None:
        self.hub = hub

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
            self.hub.code.sync_code_manifest(project_root, include_tests=include_tests)

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
                return {
                    "startup_files": startup_files,
                    "requires_session_selection": True,
                    "reason": "no_unique_active_session",
                    "sessions": session_summaries,
                }

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

        return response

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
            return {
                "stage": "setup_required",
                "ready": False,
                "next_step": "project_init",
                "reason": "missing AIDOCS project structure",
            }

        sync_result = {
            "memory": self.hub.index.sync_all(project_root),
            "code_manifest": {"code_files": self.hub.code.sync_code_manifest(project_root, include_tests=include_tests)},
            "schema": self.hub.schema.sync_schema(project_root),
            "workflow": self.hub.workflow.compile_project_rules(project_root),
        }

        legacy_state = self.hub.legacy.inspect_legacy(project_root)
        sessions = self.hub.sessions.list_sessions(project_root)
        if legacy_state.get("legacy_present") and len(sessions) == 0:
            proposal = self.hub.legacy.build_session_proposal(project_root, session_id=session_id)
            return {
                "stage": "migration_required",
                "ready": False,
                "initialized": True,
                "indexes_synced": True,
                "sync": sync_result,
                "legacy": legacy_state,
                "proposal": proposal,
                "next_step": "issue_stop_for_migration_choice",
            }

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

        return {
            "stage": "session_active" if not session_result.get("requires_session_selection") else "session_selection_required",
            "ready": not session_result.get("requires_session_selection"),
            "initialized": True,
            "indexes_synced": True,
            "sync": sync_result,
            "session": session_result,
        }

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
            result["next_step"] = bootstrap.get("next_step") or bootstrap.get("stage")
            return result

        selected = bootstrap["session"]["selected_session"]["session_id"]
        result["selected_session_id"] = selected
        result["managed_mode"] = self.hub.managed_mode.set_mode(project_root, session_id=selected, source="/aidocs")

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

        mapping = [
            ("project_update", ("update project", "fix drift", "sync project", "refresh aidocs", "run project update")),
            ("archive", ("archive", "patch notes", "changelog")),
            ("delete_session", ("delete session", "archive session", "remove session")),
            ("write_memory", ("remember", "persist this", "save this rule", "capture this")),
            ("edit", ("fix", "change", "edit", "update", "implement", "refactor", "rename", "add", "remove")),
            ("trace", ("trace", "where does", "why does", "find usage", "find references", "how does")),
            ("understand", ("understand", "explain", "inspect", "analyze", "analyse", "look into", "investigate")),
        ]

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
                "next_step": "/aidocs",
            }

        if route.get("blocked_reason"):
            return {
                "handled": False,
                "mode": "blocked",
                "classification": classified,
                "route": route,
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
                "orchestration": orchestration,
            }

        return {
            "handled": True,
            "mode": "preflight_only",
            "classification": classified,
            "route": route,
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
            self.hub.code.sync_code_manifest(project_root, include_tests=include_tests)
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
