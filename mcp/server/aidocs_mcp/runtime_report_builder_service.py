from __future__ import annotations

from pathlib import Path
from typing import Any


class RuntimeReportBuilderService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _build_session_start_report(
        self, response: dict[str, object]
    ) -> dict[str, object]:
        if response.get("requires_session_selection"):
            sessions = (
                response.get("sessions")
                if isinstance(response.get("sessions"), list)
                else []
            )
            repo_summary = (
                response.get("repo_summary")
                if isinstance(response.get("repo_summary"), dict)
                else {}
            )
            extra = (
                repo_summary.get("bullets")
                if isinstance(repo_summary.get("bullets"), list)
                else []
            )
            return {
                "headline": "Session selection is required before continuing.",
                "bullets": [f"Active/available sessions: {len(sessions)}."]
                + [str(item) for item in extra[:3]],
                "next_step": "select_session",
            }

        selected = (
            response.get("selected_session")
            if isinstance(response.get("selected_session"), dict)
            else {}
        )
        session_id = selected.get("session_id")
        bullets = [f"Selected session: {session_id}."] if session_id else []
        if response.get("code_bundle"):
            bullets.append("Context code bundle is included.")
        else:
            bullets.append("Context code bundle is deferred by default.")
        repo_summary = (
            response.get("repo_summary")
            if isinstance(response.get("repo_summary"), dict)
            else {}
        )
        extra = (
            repo_summary.get("bullets")
            if isinstance(repo_summary.get("bullets"), list)
            else []
        )
        bullets.extend(str(item) for item in extra[:3])
        handoff = (
            response.get("handoff") if isinstance(response.get("handoff"), dict) else {}
        )
        handoff_sections = (
            handoff.get("sections") if isinstance(handoff.get("sections"), dict) else {}
        )
        handoff_now = (
            handoff_sections.get("What Matters Now")
            if isinstance(handoff_sections.get("What Matters Now"), list)
            else []
        )
        bullets.extend(
            str(item) for item in handoff_now[:2] if str(item).strip() != "-"
        )
        handoff_steps = (
            response.get("handoff_steps")
            if isinstance(response.get("handoff_steps"), list)
            else []
        )
        actionable_count = sum(
            1
            for step in handoff_steps
            if str(step.get("status")) in {"open", "reset", "failed", "stale"}
        )
        if actionable_count:
            bullets.append(f"Actionable handoff steps: {actionable_count}.")
        freshness = self.runtime._handoff_freshness(handoff_sections)
        if freshness.get("status") == "stale":
            bullets.append(
                f"Handoff freshness is stale ({freshness.get('age_hours')}h old)."
            )
        elif freshness.get("status") == "unknown":
            bullets.append("Handoff freshness is unknown.")
        compliance = (
            response.get("compliance")
            if isinstance(response.get("compliance"), dict)
            else {}
        )
        journal_coverage = (
            compliance.get("journal_coverage")
            if isinstance(compliance.get("journal_coverage"), dict)
            else {}
        )
        meaningful_event_count = int(
            journal_coverage.get("meaningful_event_count_since_journal") or 0
        )
        latest_meaningful_event_at = str(
            journal_coverage.get("latest_meaningful_event_at") or ""
        ).strip()
        if meaningful_event_count:
            if latest_meaningful_event_at:
                bullets.append(
                    f"Recent meaningful work since latest journal: {meaningful_event_count} event(s), latest at {latest_meaningful_event_at}."
                )
            else:
                bullets.append(
                    f"Recent meaningful work since latest journal: {meaningful_event_count} event(s)."
                )
        for warning in (
            compliance.get("warnings", [])[:3]
            if isinstance(compliance.get("warnings"), list)
            else []
        ):
            bullets.append(f"Compliance: {warning}.")
        return {
            "headline": "Session context is ready.",
            "bullets": bullets,
            "next_step": None,
        }

    def _build_bootstrap_report(self, result: dict[str, object]) -> dict[str, object]:
        stage = str(result.get("stage") or "unknown")
        repo_summary = (
            result.get("repo_summary")
            if isinstance(result.get("repo_summary"), dict)
            else {}
        )
        repo_bullets = (
            repo_summary.get("bullets")
            if isinstance(repo_summary.get("bullets"), list)
            else []
        )
        if stage == "setup_required":
            return {
                "headline": "AIDOCS project setup is required.",
                "bullets": [
                    str(result.get("reason") or "Missing AIDOCS project structure.")
                ],
                "next_step": result.get("next_step"),
            }
        if stage == "migration_required":
            return {
                "headline": "Legacy migration choice is required before continuing.",
                "bullets": [
                    "Legacy runtime files are present and no session has been migrated yet."
                ],
                "next_step": result.get("next_step"),
            }

        session = (
            result.get("session") if isinstance(result.get("session"), dict) else {}
        )
        selected = (
            session.get("selected_session")
            if isinstance(session.get("selected_session"), dict)
            else {}
        )
        sync = result.get("sync") if isinstance(result.get("sync"), dict) else {}
        capabilities = (
            sync.get("capabilities")
            if isinstance(sync.get("capabilities"), dict)
            else {}
        )
        procedures = (
            sync.get("procedures") if isinstance(sync.get("procedures"), dict) else {}
        )
        links = (
            sync.get("procedure_capability_links")
            if isinstance(sync.get("procedure_capability_links"), dict)
            else {}
        )
        bullets = []
        repaired = (
            result.get("repaired") if isinstance(result.get("repaired"), dict) else None
        )
        if repaired:
            created = (
                repaired.get("created")
                if isinstance(repaired.get("created"), list)
                else []
            )
            bullets.append(
                f"Repaired canonical AIDOCS structure ({len(created)} files created)."
            )
        if selected.get("session_id"):
            bullets.append(f"Selected session: {selected.get('session_id')}.")
        bullets.append(
            f"Action surfaces synced: capabilities={capabilities.get('capability_definitions')}, procedures={procedures.get('procedure_definitions')}, links={links.get('links')}."
        )
        bullets.extend(str(item) for item in repo_bullets[:4])
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
        workflow = (
            sync.get("workflow") if isinstance(sync.get("workflow"), dict) else {}
        )
        capabilities = (
            sync.get("capabilities")
            if isinstance(sync.get("capabilities"), dict)
            else {}
        )
        procedures = (
            sync.get("procedures") if isinstance(sync.get("procedures"), dict) else {}
        )
        links = (
            sync.get("procedure_capability_links")
            if isinstance(sync.get("procedure_capability_links"), dict)
            else {}
        )
        execution = (
            sync.get("execution") if isinstance(sync.get("execution"), dict) else {}
        )
        memory = sync.get("memory") if isinstance(sync.get("memory"), dict) else {}
        code_manifest = (
            sync.get("code_manifest")
            if isinstance(sync.get("code_manifest"), dict)
            else {}
        )
        schema = sync.get("schema") if isinstance(sync.get("schema"), dict) else {}

        return {
            "ready": bool(bootstrap.get("ready")),
            "stage": bootstrap.get("stage"),
            "selected_session_id": selected_session_id,
            "managed_mode_active": bool((managed_mode or {}).get("active")),
            "managed_mode_session_id": (managed_mode or {}).get("session_id"),
            "operator_state": (operator_summary or {}).get("overall_state")
            or (operator_summary or {}).get("state"),
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
        selected_session_id = (
            str(readiness_summary.get("selected_session_id") or "").strip() or None
        )
        indexes = (
            readiness_summary.get("indexes")
            if isinstance(readiness_summary.get("indexes"), dict)
            else {}
        )

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
            if (
                next_step is None
                and str(operator_summary.get("overall_state") or "") == "healthy"
            ):
                next_step = "No immediate gap detected; continue monitoring execution history and drift."

        # Surface pending workflow actions for the current action_kind
        pending_workflow = self.runtime._collect_pending_workflow(action_kind, project_root)
        if pending_workflow:
            bullets.append(
                f"Pending workflow actions after `{action_kind}`: {pending_workflow}."
            )

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
