from __future__ import annotations

from pathlib import Path
from typing import Any


class RuntimePromptHandlingService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub

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
            classified = self.runtime.classify_prompt_action(
                user_request,
                explicit_targets=explicit_targets,
                project_root=project_root,
            )
            action_kind = str(classified["action_kind"])
        else:
            classified = {"action_kind": action_kind, "why": ["provided"]}

        route = self.runtime.aidocs_route_prompt(
            project_root,
            user_request=user_request,
            action_kind=action_kind,
            explicit_targets=explicit_targets,
        )

        if not route.get("managed_mode"):
            result = {
                "handled": False,
                "mode": "requires_aidocs_entry",
                "classification": classified,
                "route": route,
                "report": self.runtime._build_handle_prompt_report(
                    mode="requires_aidocs_entry",
                    classification=classified,
                    route=route,
                    next_step="/aidocs",
                ),
                "next_step": "/aidocs",
            }
            compact = self.runtime.build_artifact_backed_result(
                project_root,
                inline_summary="Prompt routing requires `/aidocs` entry before managed handling can continue.",
                payload=result,
                artifact_name="aidocs-handle-prompt-requires-entry",
                session_id=None,
                structured_summary={
                    "mode": result["mode"],
                    "handled": result["handled"],
                    "next_step": result["next_step"],
                },
            )
            result.update(compact)
            return result

        if route.get("blocked_reason"):
            result = {
                "handled": False,
                "mode": "blocked",
                "classification": classified,
                "route": route,
                "report": self.runtime._build_handle_prompt_report(
                    mode="blocked",
                    classification=classified,
                    route=route,
                    next_step=route.get("recommended_mcp_flow"),
                ),
                "next_step": route.get("recommended_mcp_flow"),
            }
            compact = self.runtime.build_artifact_backed_result(
                project_root,
                inline_summary=(
                    f"Prompt routing blocked for `{action_kind}`. "
                    f"Reason: {route.get('blocked_reason')}."
                ),
                payload=result,
                artifact_name=f"aidocs-handle-prompt-blocked-{action_kind}",
                session_id=str(route.get("session_id") or "") or None,
                structured_summary={
                    "mode": result["mode"],
                    "handled": result["handled"],
                    "blocked_reason": route.get("blocked_reason"),
                },
            )
            result.update(compact)
            return result

        session_id = route.get("session_id")
        if action_kind in {"inspect", "read_file", "read_error"} and route.get(
            "allowed_direct_inspection"
        ):
            result = {
                "handled": True,
                "mode": "direct_inspection_allowed",
                "classification": classified,
                "route": route,
                "selected_session_id": session_id,
                "report": self.runtime._build_handle_prompt_report(
                    mode="direct_inspection_allowed",
                    classification=classified,
                    route=route,
                    next_step="inspect_target_then_return_to_mcp_for_broader_work",
                ),
                "next_step": "inspect_target_then_return_to_mcp_for_broader_work",
            }
            compact = self.runtime.build_artifact_backed_result(
                project_root,
                inline_summary="Direct inspection is allowed for the requested prompt; broader work should return to MCP orchestration afterward.",
                payload=result,
                artifact_name=f"aidocs-handle-prompt-direct-inspection-{action_kind}",
                session_id=str(session_id) if session_id else None,
                structured_summary={
                    "mode": result["mode"],
                    "handled": result["handled"],
                    "next_step": result["next_step"],
                },
            )
            result.update(compact)
            return result

        if action_kind in {
            "understand",
            "trace",
            "code_bundle",
            "edit",
            "write_memory",
        }:
            orchestration = self.runtime.aidocs_orchestrate(
                project_root,
                user_request=user_request,
                action_kind=action_kind,
                session_id=str(session_id) if session_id else None,
                explicit_targets=explicit_targets,
                include_code_bundle=include_code_bundle,
                include_tests=include_tests,
            )
            result = {
                "handled": True,
                "mode": "mcp_orchestrated",
                "classification": classified,
                "route": route,
                "active_skills": list(orchestration.get("active_skills", [])),
                "operator_report": orchestration.get("operator_report"),
                "readiness_summary": orchestration.get("readiness_summary"),
                "report": self.runtime._build_handle_prompt_report(
                    mode="mcp_orchestrated",
                    classification=classified,
                    route=route,
                    operator_report=orchestration.get("operator_report")
                    if isinstance(orchestration.get("operator_report"), dict)
                    else None,
                ),
                "orchestration": orchestration,
            }
            compact = self.runtime.build_artifact_backed_result(
                project_root,
                inline_summary=(
                    f"Prompt handled through MCP orchestration for `{action_kind}`. "
                    f"Selected session: {session_id}."
                ),
                payload=result,
                artifact_name=f"aidocs-handle-prompt-orchestrated-{action_kind}",
                session_id=str(session_id) if session_id else None,
                structured_summary={
                    "mode": result["mode"],
                    "handled": result["handled"],
                    "selected_session_id": session_id,
                    "active_skill_count": len(result.get("active_skills", [])),
                },
            )
            result.update(compact)
            return result

        result = {
            "handled": True,
            "mode": "preflight_only",
            "classification": classified,
            "route": route,
            "report": self.runtime._build_handle_prompt_report(
                mode="preflight_only",
                classification=classified,
                route=route,
                next_step=route.get("recommended_mcp_flow"),
            ),
        }
        compact = self.runtime.build_artifact_backed_result(
            project_root,
            inline_summary=f"Prompt handled in preflight-only mode for `{action_kind}`.",
            payload=result,
            artifact_name=f"aidocs-handle-prompt-preflight-{action_kind}",
            session_id=str(session_id) if session_id else None,
            structured_summary={
                "mode": result["mode"],
                "handled": result["handled"],
            },
        )
        result.update(compact)
        return result
