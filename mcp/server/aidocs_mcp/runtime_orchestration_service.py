from __future__ import annotations

from pathlib import Path
from typing import Any


class RuntimeOrchestrationService:
    def __init__(self, runtime: Any, logger: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub
        self._logger = logger

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

        bootstrap = self.runtime.project_bootstrap_or_resume(
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
            result["readiness_summary"] = self.runtime._build_readiness_summary(
                bootstrap=bootstrap,
                selected_session_id=None,
                managed_mode=None,
                operator_summary=None,
            )
            result["operator_report"] = self.runtime._build_operator_report(
                readiness_summary=result["readiness_summary"],
                operator_summary=None,
                bootstrap=bootstrap,
                action_kind=action_kind,
                project_root=project_root,
            )
            result["report"] = result["operator_report"]
            result["next_step"] = bootstrap.get("next_step") or bootstrap.get("stage")
            compact = self.runtime.build_artifact_backed_result(
                project_root,
                inline_summary=(
                    f"AIDOCS orchestration not ready yet for `{action_kind}`. "
                    f"Next step: {result['next_step']}."
                ),
                payload=result,
                artifact_name=f"aidocs-orchestrate-{action_kind}",
                session_id=None,
                structured_summary={
                    "action_kind": action_kind,
                    "ready": False,
                    "next_step": result["next_step"],
                    "stage": result.get("stage"),
                },
            )
            result.update(compact)
            return result

        selected = bootstrap["session"]["selected_session"]["session_id"]
        result["selected_session_id"] = selected
        intent = self.runtime._infer_skill_trigger_intent(
            user_request,
            action_kind,
            project_root=project_root,
            session_id=session_id,
        )
        skill_trigger_state = self.runtime.skill_trigger_state(
            project_root,
            selected,
            intent=intent,
            workflow_state=action_kind,
        )
        # #816: orchestrate is the routing entry point a confused caller is told
        # to use ("call aidocs_orchestrate for a full state diagnostic"), so a
        # bind here that the gate cannot read sends the caller round the loop it
        # is already stuck in. Bind the CALLER, not just the singleton.
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        _hsid = (current_calling_host_session_id() or "").strip()
        result["managed_mode"] = self.hub.managed_mode.set_mode(
            project_root,
            session_id=selected,
            source="/aidocs",
            host_session_id=_hsid,
            restamp_singleton=(not _hsid),
            authenticate_host=True,
        )
        result["skill_trigger_state"] = skill_trigger_state
        result["active_skills"] = list(skill_trigger_state.get("active_skills", []))
        result["runtime_owned_capabilities"] = [
            item
            for item in (skill_trigger_state.get("runtime_owned_capabilities") or [])
            if isinstance(item, dict)
        ]
        result["operator_summary"] = self.hub.action_surface.current_session_bundle(
            project_root,
            limit=10,
            max_queries=12,
        )
        result["readiness_summary"] = self.runtime._build_readiness_summary(
            bootstrap=bootstrap,
            selected_session_id=selected,
            managed_mode=result.get("managed_mode")
            if isinstance(result.get("managed_mode"), dict)
            else None,
            operator_summary=result.get("operator_summary")
            if isinstance(result.get("operator_summary"), dict)
            else None,
        )
        result["operator_report"] = self.runtime._build_operator_report(
            readiness_summary=result["readiness_summary"],
            operator_summary=result.get("operator_summary")
            if isinstance(result.get("operator_summary"), dict)
            else None,
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
        elif include_code_bundle:
            result["retrieval"] = {
                "mode": "session_bundle",
                "bundle": self.hub.code.get_context_bundle(project_root, session_id=selected),
            }
        else:
            preview = self.hub.sessions.session_code_targets(project_root, selected)
            result["retrieval"] = {
                "mode": "session_bundle_deferred",
                "session_id": selected,
                "session_target_count": len(
                    [item for item in preview if item and item.strip()],
                ),
                "memory_structure": self.runtime._memory_structure_summary(project_root),
                "reason": "bundle_omitted_by_default",
            }

        # Include compiled workflow actions so the host doesn't need to re-read
        try:
            result["workflow"] = self.hub.workflow.read_compiled(project_root)
        except Exception as exc:
            self._logger.warning("Failed to read workflow for orchestration result: %s", exc)
            result["workflow"] = None

        compact = self.runtime.build_artifact_backed_result(
            project_root,
            inline_summary=(
                f"AIDOCS orchestration prepared for `{action_kind}` in session `{selected}`. "
                f"Retrieval mode: {(result.get('retrieval') or {}).get('mode') or 'none'!s}."
            ),
            payload=result,
            artifact_name=f"aidocs-orchestrate-{selected}-{action_kind}",
            session_id=str(selected),
            structured_summary={
                "action_kind": action_kind,
                "ready": True,
                "selected_session_id": selected,
                "retrieval_mode": str((result.get("retrieval") or {}).get("mode") or "none"),
                "active_skill_count": len(result.get("active_skills", [])),
            },
        )
        result.update(compact)
        return result
