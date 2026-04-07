from __future__ import annotations

from pathlib import Path
from typing import Any


class RuntimeResumeBundleService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub

    def session_resume_bundle(
        self,
        project_root: Path,
        session_id: str,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        journal_last_n: int = 10,
    ) -> dict[str, object]:
        if journal_last_n == 10:
            journal_last_n = int(
                self.runtime._config_resolver.get(
                    "presentation.resume_journal_last_n",
                    project_root=project_root,
                    session_id=session_id,
                )
                or 10
            )
        session = self.hub.sessions.read_session(project_root, session_id)
        context = self.hub.sessions.read_context(project_root, session_id)
        plan = self.hub.sessions.read_plan(project_root, session_id)
        handoff = self.hub.sessions.read_handoff(project_root, session_id)
        journal = self.hub.sessions.read_journal(
            project_root, session_id, last_n=journal_last_n
        )
        freshness = self.runtime._handoff_freshness(
            handoff.sections, project_root=project_root, session_id=session_id
        )
        handoff_steps = self.hub.sessions.read_handoff_steps(project_root, session_id)
        actionable_steps = [
            step
            for step in handoff_steps
            if step.get("status") in {"open", "reset", "failed", "stale"}
        ]
        recently_changed_steps = [
            step
            for step in handoff_steps
            if self.runtime._step_changed_recently(
                step, project_root=project_root, session_id=session_id
            )
        ]
        selected_skills = self.hub.skills.get_selected_skills(project_root, session_id)
        imported_skill_state = self.runtime._imported_skill_state(
            project_root, session_id, selected_state=selected_skills
        )
        skill_trigger_state = self.runtime._resolve_skill_trigger_state(
            project_root,
            session_id,
            intent="startup",
            workflow_state="session_resume_bundle",
        )
        compliance = self.runtime.session_compliance_summary(project_root, session_id)
        repo_summary = self.runtime.repo_summary(project_root)

        result: dict[str, object] = {
            "session": {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            },
            "context": {"path": str(context.path), "sections": context.sections},
            "plan": {"path": str(plan.path), "sections": plan.sections},
            "handoff": {"path": str(handoff.path), "sections": handoff.sections},
            "handoff_steps": handoff_steps,
            "actionable_handoff_steps": actionable_steps,
            "recently_changed_handoff_steps": recently_changed_steps,
            "handoff_freshness": freshness,
            "selected_skills": selected_skills,
            "imported_skill_state": imported_skill_state,
            "runtime_owned_capabilities": [
                item
                for item in (
                    skill_trigger_state.get("runtime_owned_capabilities") or []
                )
                if isinstance(item, dict)
            ],
            "skill_trigger_state": skill_trigger_state,
            "compliance": compliance,
            "journal": journal,
            "repo_summary": repo_summary,
            "project_overview": self.runtime._build_project_overview(
                project_root,
                repo_summary=repo_summary,
                selected_session_id=session.session_id,
            ),
            "session_overview": self.runtime._build_session_overview(
                session_id=session.session_id,
                session_sections=session.sections,
                context_sections=context.sections,
                handoff_steps=handoff_steps,
                compliance=compliance,
            ),
            "skills_overview": self.runtime._build_skills_overview(
                session_id=session.session_id,
                selected_skills=selected_skills,
                active_skills=list(skill_trigger_state.get("active_skills", [])),
                imported_skill_state=imported_skill_state,
                skill_trigger_state=skill_trigger_state,
            ),
            "plan_overview": self.runtime._build_plan_overview(
                session_id=session.session_id,
                plan_path=str(plan.path),
                plan_sections=plan.sections,
                has_lanes=bool(getattr(plan, "lanes", None)),
            ),
        }
        if include_code_bundle:
            result["code_bundle"] = self.runtime._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )
        compact = self.runtime.build_artifact_backed_result(
            project_root,
            inline_summary=(
                f"Resume bundle ready for session `{session_id}`. "
                f"Actionable handoff steps: {len(actionable_steps)}. "
                f"Journal entries returned: {len(journal)}."
            ),
            payload=result,
            artifact_name=f"session-resume-bundle-{session_id}",
            session_id=session_id,
            structured_summary={
                "session_id": session_id,
                "actionable_handoff_step_count": len(actionable_steps),
                "recently_changed_handoff_step_count": len(recently_changed_steps),
                "journal_entry_count": len(journal),
                "logging_debt": bool(compliance.get("logging_debt")),
                "has_code_bundle": bool(include_code_bundle),
            },
        )
        result.update(compact)
        return result
