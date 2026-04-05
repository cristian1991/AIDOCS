from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config_schema import SETTINGS_CATALOG, available_config_edit_modes


class RuntimePresentationService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def dashboard_config_entries(
        self, project_root: Path, session_id: str | None
    ) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        resolver = self.runtime._config_resolver
        for setting_path, metadata in sorted(SETTINGS_CATALOG.items()):
            section, _, key = setting_path.rpartition(".")
            scope_values: dict[str, object] = {}
            for scope in metadata["allowed_scopes"]:
                layer_scope = "user" if scope == "global" else scope
                raw = resolver.get_layer_value(
                    setting_path, layer_scope,
                    project_root=project_root, session_id=session_id,
                )
                scope_values[scope] = raw
            entries.append(
                {
                    "path": setting_path,
                    "section": section,
                    "key": key,
                    "type": metadata["type"],
                    "description": metadata["description"],
                    "default": metadata["default"],
                    "allowed_values": metadata["allowed_values"],
                    "value_descriptions": metadata["value_descriptions"],
                    "allowed_scopes": metadata["allowed_scopes"],
                    "agent_editable_scopes": metadata["agent_editable_scopes"],
                    "security_sensitive": metadata["security_sensitive"],
                    "requires_restart": metadata["requires_restart"],
                    "editable": "project" in metadata["agent_editable_scopes"],
                    "current_value": resolver.get(
                        setting_path,
                        project_root=project_root,
                        session_id=session_id,
                    ),
                    "scope_values": scope_values,
                }
            )
        return entries

    def dashboard_token_usage(
        self,
        execution_summary: dict[str, object],
        recent_execution: list[dict[str, object]],
        session_breakdown: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        capability_counts: dict[str, int] = {}
        action_counts = {
            str(key): int(value)
            for key, value in (execution_summary.get("by_action_kind") or {}).items()
            if value is not None
        }
        event_counts = {
            str(key): int(value)
            for key, value in (execution_summary.get("by_event_kind") or {}).items()
            if value is not None
        }
        for event in recent_execution:
            capability_name = str(event.get("capability_name") or "unknown")
            capability_counts[capability_name] = (
                capability_counts.get(capability_name, 0) + 1
            )
        top_capabilities = [
            {"label": label, "count": count}
            for label, count in sorted(
                capability_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:8]
        ]
        top_actions = [
            {"label": label, "count": count}
            for label, count in sorted(
                action_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:8]
        ]
        event_breakdown = [
            {"label": label, "count": count}
            for label, count in sorted(
                event_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:8]
        ]
        token_estimates = execution_summary.get("token_estimates") or {}
        tokens_in = int(token_estimates.get("tokens_in", 0))
        tokens_out = int(token_estimates.get("tokens_out", 0))
        return {
            "available": tokens_in > 0 or tokens_out > 0,
            "reason": (
                f"Estimated from MCP tool call sizes (~4 chars/token). "
                f"Tokens in: ~{tokens_in:,} · Tokens out: ~{tokens_out:,} · Total: ~{tokens_in + tokens_out:,}"
                if tokens_in > 0 or tokens_out > 0
                else "No token data yet. Token estimates will appear after MCP tool calls are recorded."
            ),
            "token_estimates": {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "total": tokens_in + tokens_out,
            },
            "proxy_series": {
                "top_capabilities": top_capabilities,
                "top_action_kinds": top_actions,
                "event_breakdown": event_breakdown,
            },
            "session_breakdown": session_breakdown or [],
            "recent_event_count": len(recent_execution),
        }

    def dashboard_snapshot(
        self,
        project_root: Path,
        session_id: str | None = None,
        event_limit: int = 12,
    ) -> dict[str, object]:
        runtime = self.runtime
        repo_summary = runtime.repo_summary(project_root)
        managed_mode = runtime.hub.managed_mode.get_mode(project_root)
        sessions = runtime.hub.sessions.list_sessions(project_root)
        selected_session_id = session_id
        if not selected_session_id and managed_mode.get("active"):
            selected_session_id = (
                str(managed_mode.get("session_id") or "").strip() or None
            )
        if not selected_session_id and len(sessions) == 1:
            selected_session_id = sessions[0].session_id

        session_cards = [
            {
                "session_id": item.session_id,
                "title": item.title,
                "status": item.status,
                "owner": item.owner,
                "goal": item.goal,
                "last_updated": item.last_updated,
                "selected": item.session_id == selected_session_id,
                "managed": item.session_id
                == str(managed_mode.get("session_id") or "").strip(),
            }
            for item in sessions
        ]

        selected_session: dict[str, object] | None = None
        execution_summary = runtime.hub.execution.query_execution_summary(
            project_root,
            session_id=selected_session_id,
        )
        recent_execution = runtime.hub.execution.query_last_execution(
            project_root,
            session_id=selected_session_id,
            limit=event_limit,
        )
        if selected_session_id:
            session = runtime.hub.sessions.read_session(
                project_root, selected_session_id
            )
            context = runtime.hub.sessions.read_context(
                project_root, selected_session_id
            )
            plan = runtime.hub.sessions.read_plan(project_root, selected_session_id)
            handoff_steps = runtime.hub.sessions.read_handoff_steps(
                project_root, selected_session_id
            )
            compliance = self.session_compliance_summary(
                project_root, selected_session_id
            )
            conductor: dict[str, object] | None = None
            conductor_error: str | None = None
            if getattr(plan, "lanes", None):
                try:
                    conductor = runtime.plan_conductor_status(
                        project_root, selected_session_id
                    )
                except Exception as exc:
                    conductor_error = str(exc)
            selected_session = {
                "session": {
                    "session_id": session.session_id,
                    "path": str(session.path),
                    "sections": session.sections,
                },
                "context": {"path": str(context.path), "sections": context.sections},
                "overview": self.build_session_overview(
                    session_id=selected_session_id,
                    session_sections=session.sections,
                    context_sections=context.sections,
                    handoff_steps=handoff_steps,
                    compliance=compliance,
                ),
                "plan_overview": self.build_plan_overview(
                    session_id=selected_session_id,
                    plan_path=str(plan.path),
                    plan_sections=plan.sections,
                    has_lanes=bool(getattr(plan, "lanes", None)),
                ),
                "compliance": compliance,
                "handoff_steps": handoff_steps,
                "conductor": conductor,
                "conductor_error": conductor_error,
            }

        effective_config = runtime.effective_config(
            project_root, session_id=selected_session_id
        )
        return {
            "project": self.build_project_overview(
                project_root,
                repo_summary=repo_summary,
                selected_session_id=selected_session_id,
            ),
            "repo_summary": repo_summary,
            "managed_mode": managed_mode,
            "sessions": session_cards,
            "selected_session_id": selected_session_id,
            "selected_session": selected_session,
            "execution": {
                "summary": execution_summary,
                "recent": recent_execution,
            },
            "token_usage": self.dashboard_token_usage(
                execution_summary, recent_execution,
                session_breakdown=runtime.hub.execution.query_token_breakdown_by_session(project_root),
            ),
            "config": {
                "project_config_path": str(
                    runtime._config_resolver.project_config_path(project_root) or ""
                ),
                "session_config_path": str(
                    runtime._config_resolver.session_config_path(
                        project_root, selected_session_id
                    )
                    or ""
                ),
                "effective": effective_config,
                "entries": self.dashboard_config_entries(
                    project_root, selected_session_id
                ),
                "available_edit_modes": available_config_edit_modes("release"),
            },
        }

    def session_compliance_summary(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        runtime = self.runtime
        session = runtime.hub.sessions.read_session(project_root, session_id)
        plan = runtime.hub.sessions.read_plan(project_root, session_id)
        handoff_steps = runtime.hub.sessions.read_handoff_steps(
            project_root, session_id
        )
        journal = runtime.hub.sessions.read_journal(project_root, session_id, last_n=20)
        execution_summary = runtime.hub.execution.query_execution_summary(
            project_root,
            session_id=session_id,
        )
        recent_events = runtime.hub.execution.query_last_execution(
            project_root,
            session_id=session_id,
            limit=20,
        )

        status_values = runtime._clean_bullets(session.sections.get("Status", []))
        task_open = any(value == "active" for value in status_values)
        partial_goals = runtime._clean_bullets(plan.sections.get("Partial Goals", []))
        upcoming = runtime._clean_bullets(session.sections.get("Upcoming", []))
        actionable_steps = [
            step
            for step in handoff_steps
            if str(step.get("status")) in {"open", "reset", "failed", "stale"}
        ]

        latest_journal_ts = None
        if journal:
            try:
                latest_journal_ts = max(
                    datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M")
                    for entry in journal
                    if entry.get("timestamp")
                )
            except Exception:
                latest_journal_ts = None

        journal_coverage = runtime.hub.execution.session_journal_coverage_summary(
            project_root,
            session_id,
            latest_journal_at=latest_journal_ts,
        )
        latest_work_ts = None
        latest_work_text = str(
            journal_coverage.get("latest_meaningful_event_at") or ""
        ).strip()
        if latest_work_text:
            try:
                latest_work_ts = datetime.strptime(
                    latest_work_text, "%Y-%m-%d %H:%M:%S"
                )
            except Exception:
                latest_work_ts = None

        logging_debt = bool(journal_coverage.get("logging_debt"))
        summary = {
            "task_open": task_open,
            "logging_debt": logging_debt,
            "actionable_step_count": len(actionable_steps),
            "partial_goal_count": len(partial_goals),
            "upcoming_count": len(upcoming),
            "execution_events": int(execution_summary.get("total_events", 0)),
            "latest_work_event_at": latest_work_ts.strftime("%Y-%m-%d %H:%M:%S")
            if latest_work_ts
            else None,
            "latest_journal_at": latest_journal_ts.strftime("%Y-%m-%d %H:%M")
            if latest_journal_ts
            else None,
            "journal_coverage": journal_coverage,
            "warnings": [],
        }
        warnings: list[str] = []
        if task_open:
            warnings.append("task remains open")
        if logging_debt:
            warnings.append("work occurred after the latest journal entry")
        if actionable_steps:
            warnings.append(f"{len(actionable_steps)} actionable handoff steps remain")
        summary["warnings"] = warnings
        return summary

    def build_project_overview(
        self,
        project_root: Path,
        *,
        repo_summary: dict[str, object] | None,
        selected_session_id: str | None = None,
        stage: str | None = None,
        ready: bool | None = None,
    ) -> dict[str, object]:
        runtime = self.runtime
        summary = (
            repo_summary
            if isinstance(repo_summary, dict)
            else runtime.repo_summary(project_root)
        )
        return {
            "project_name": summary.get("project_name") or project_root.name,
            "project_root": summary.get("project_root") or str(project_root),
            "code_file_count": int(summary.get("code_files") or 0),
            "module_count": int(summary.get("modules") or 0),
            "schema_entity_count": int(summary.get("schema_entities") or 0),
            "session_count": int(summary.get("sessions") or 0),
            "selected_session_id": selected_session_id,
            "artifact_catalog": self.project_artifact_catalog(project_root),
            "stage": stage,
            "ready": ready,
        }

    def project_artifact_catalog(
        self, project_root: Path
    ) -> dict[str, dict[str, object]]:
        runtime = self.runtime
        return {
            "skill_provider_registry": {
                "path": str(
                    runtime.hub.skills.external_provider_registry_path(project_root)
                ),
                "classification": "config",
                "legacy_paths": [
                    str(
                        runtime.hub.skills.legacy_external_provider_registry_path(
                            project_root
                        )
                    )
                ],
            },
            "aidocs_managed": {
                "path": str(runtime.hub.managed_mode.config_path(project_root)),
                "classification": "runtime_binding_state",
            },
            "workflow_actions": {
                "path": str(runtime.hub.workflow.config_path(project_root)),
                "classification": "compiled_runtime_artifact",
            },
        }

    def result_artifacts_root(
        self, project_root: Path, session_id: str | None = None
    ) -> Path:
        if session_id:
            return (
                self.runtime.hub.sessions.session_path(project_root, session_id)
                / "artifacts"
            )
        return project_root / ".MEMORY" / ".runtime" / "artifacts"

    def write_result_artifact(
        self,
        project_root: Path,
        *,
        payload: object,
        artifact_name: str,
        session_id: str | None = None,
    ) -> dict[str, object]:
        artifacts_root = self.result_artifacts_root(project_root, session_id)
        target_dir = artifacts_root / "mcp-results"
        target_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9_-]+", "-", artifact_name.strip().lower()).strip("-")
        if not slug:
            slug = "result"
        artifact_id = f"{slug}-{uuid4().hex[:12]}"
        path = target_dir / f"{artifact_id}.json"
        serialized = json.dumps(payload, indent=2, sort_keys=True, default=str)
        path.write_text(serialized + "\n", encoding="utf-8")
        try:
            relative_path = str(path.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            relative_path = str(path)
        return {
            "artifact_id": artifact_id,
            "artifact_path": relative_path,
            "artifact_kind": "json",
            "size_bytes": len(serialized.encode("utf-8")),
            "session_id": session_id,
        }

    def build_artifact_backed_result(
        self,
        project_root: Path,
        *,
        inline_summary: str,
        payload: object,
        artifact_name: str,
        session_id: str | None = None,
        structured_summary: dict[str, object] | None = None,
    ) -> dict[str, object]:
        artifact = self.write_result_artifact(
            project_root,
            payload=payload,
            artifact_name=artifact_name,
            session_id=session_id,
        )
        structured_content: dict[str, object] = {
            **(structured_summary or {}),
            "artifact": artifact,
        }
        return {
            "content": (
                f"{inline_summary}\n"
                f"Full payload saved to artifact: `{artifact['artifact_path']}`."
            ),
            "structuredContent": structured_content,
        }

    def build_session_overview(
        self,
        *,
        session_id: str | None,
        session_sections: dict[str, list[str]] | None,
        context_sections: dict[str, list[str]] | None,
        handoff_steps: list[dict[str, object]] | None,
        compliance: dict[str, object] | None,
    ) -> dict[str, object]:
        runtime = self.runtime
        session_sections = (
            session_sections if isinstance(session_sections, dict) else {}
        )
        context_sections = (
            context_sections if isinstance(context_sections, dict) else {}
        )
        titles = runtime._clean_bullets(session_sections.get("Title", []))
        statuses = runtime._clean_bullets(session_sections.get("Status", []))
        goals = runtime._clean_bullets(session_sections.get("Goal", []))
        owners = runtime._clean_bullets(session_sections.get("Owner", []))
        relevant_files = runtime._clean_bullets(
            context_sections.get("Relevant Files", [])
        )
        actionable_handoff_step_count = len(
            [
                step
                for step in (handoff_steps or [])
                if str(step.get("status") or "") in {"open", "reset", "failed", "stale"}
            ]
        )
        journal_coverage = (
            (compliance or {}).get("journal_coverage")
            if isinstance((compliance or {}).get("journal_coverage"), dict)
            else {}
        )
        return {
            "session_id": session_id,
            "title": titles[0] if titles else None,
            "status": statuses[0] if statuses else None,
            "goal": goals[0] if goals else None,
            "owner": owners[0] if owners else None,
            "relevant_file_count": len(relevant_files),
            "actionable_handoff_step_count": actionable_handoff_step_count,
            "logging_debt": bool((compliance or {}).get("logging_debt")),
            "meaningful_event_count_since_journal": int(
                journal_coverage.get("meaningful_event_count_since_journal") or 0
            ),
            "latest_meaningful_event_at": journal_coverage.get(
                "latest_meaningful_event_at"
            ),
        }

    def build_skills_overview(
        self,
        *,
        session_id: str | None,
        selected_skills: dict[str, object] | None,
        active_skills: list[str] | None,
        imported_skill_state: dict[str, object] | None,
        skill_trigger_state: dict[str, object] | None,
    ) -> dict[str, object]:
        selected = [
            str(item) for item in (selected_skills or {}).get("selected_skills", [])
        ]
        active = [str(item) for item in (active_skills or [])]
        override_modes: dict[str, str] = {}
        triggered = (
            (skill_trigger_state or {}).get("triggered")
            if isinstance(skill_trigger_state, dict)
            else []
        )
        runtime_owned_capabilities = [
            item
            for item in (
                (skill_trigger_state or {}).get("runtime_owned_capabilities") or []
            )
            if isinstance(item, dict)
        ]
        if isinstance(triggered, list):
            for item in triggered:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("runtime_owned_capability"), dict):
                    continue
                skill_id = str(item.get("skill_id") or "")
                override_mode = str(item.get("override_mode") or "").strip()
                if skill_id and override_mode:
                    override_modes[skill_id] = override_mode
        return {
            "session_id": session_id,
            "selected_skills": selected,
            "selected_skill_count": len(selected),
            "active_skills": active,
            "active_skill_count": len(active),
            "runtime_owned_capabilities": runtime_owned_capabilities,
            "runtime_owned_capability_count": len(runtime_owned_capabilities),
            "provider_state": (imported_skill_state or {}).get("provider_state"),
            "provider_states": (imported_skill_state or {}).get("provider_states")
            or {},
            "override_modes": override_modes,
        }

    def build_default_plan_overview(
        self,
        *,
        session_id: str,
        end_goal: str | None = None,
    ) -> dict[str, object]:
        return {
            "session_id": session_id,
            "plan_path": None,
            "progress": "0/0",
            "completed_count": 0,
            "incomplete_count": 0,
            "next_step": None,
            "purpose": None,
            "end_goal": end_goal,
            "has_lanes": False,
        }

    def build_plan_overview(
        self,
        *,
        session_id: str,
        plan_path: str | None,
        plan_sections: dict[str, list[str]] | None,
        has_lanes: bool,
    ) -> dict[str, object]:
        runtime = self.runtime
        sections = plan_sections if isinstance(plan_sections, dict) else {}
        completed: list[str] = []
        incomplete: list[str] = []
        for lines in sections.values():
            for line in lines:
                parsed = runtime._parse_plan_checkbox_line(line)
                if not parsed:
                    continue
                text = str(parsed["text"])
                if parsed["status"] == "completed":
                    completed.append(text)
                else:
                    incomplete.append(text)
        total = len(completed) + len(incomplete)
        progress = f"{len(completed)}/{total}" if total > 0 else "0/0"
        end_goals = runtime._clean_bullets(sections.get("End Goal", []))
        purposes = runtime._clean_bullets(sections.get("Purpose", []))
        return {
            "session_id": session_id,
            "plan_path": plan_path,
            "progress": progress,
            "completed_count": len(completed),
            "incomplete_count": len(incomplete),
            "next_step": incomplete[0] if incomplete else None,
            "purpose": purposes[0] if purposes else None,
            "end_goal": end_goals[0] if end_goals else None,
            "has_lanes": has_lanes,
        }
