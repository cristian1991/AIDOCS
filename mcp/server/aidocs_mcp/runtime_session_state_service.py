from __future__ import annotations

from pathlib import Path
from typing import Any


class RuntimeSessionStateService:
    def __init__(self, runtime: Any, logger: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub
        self._logger = logger

    def session_start_state(
        self,
        project_root: Path,
        session_id: str | None = None,
        *,
        host_session_id: str = "",
        verify_index: bool = True,
    ) -> dict[str, object]:
        agents = project_root / "AGENTS.md"
        claude = project_root / "CLAUDE.md"
        memory_root = project_root / ".MEMORY"
        initialized = memory_root.is_dir() and (agents.is_file() or claude.is_file())
        if not initialized:
            return {
                "state": "not_initialized",
                "next_step": "project_init",
                "session_id": None,
                "index_status": "missing",
            }

        structure_gaps = self.runtime.project_structure_gaps(project_root)
        if structure_gaps:
            return {
                "state": "not_bootstrapped",
                "next_step": "project_bootstrap_or_resume",
                "session_id": None,
                "index_status": "missing",
                "structure_gaps": structure_gaps,
            }

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
        if not sessions:
            return {
                "state": "no_session",
                "next_step": "create_session",
                "session_id": None,
                "index_status": "missing",
            }

        resolved_session_id = (
            session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        )
        if resolved_session_id is None:
            active = [item for item in sessions if item.status == "active"]
            if len(active) == 1:
                resolved_session_id = active[0].session_id
            elif len(sessions) == 1:
                resolved_session_id = sessions[0].session_id
            else:
                return {
                    "state": "multiple_sessions",
                    "next_step": "select_session",
                    "session_id": None,
                    "index_status": "unknown",
                    "sessions": session_summaries,
                }
        elif not any(item.session_id == resolved_session_id for item in sessions):
            return {
                "state": "session_not_found",
                "next_step": "select_session",
                "session_id": None,
                "requested_session_id": resolved_session_id,
                "index_status": "unknown",
                "sessions": session_summaries,
            }

        imported_skill_state = self.runtime._persist_host_skill_state(
            project_root,
            resolved_session_id,
            intent="startup",
            workflow_state="session_start",
        )

        if not verify_index:
            # UPS-only fast path: the per-prompt caller consumes no freshness
            # field (only session_id), so skip BOTH exact SHA walks
            # (_code_freshness + _memory_freshness). Report an HONEST "unverified"
            # status — never a false "ready", never "stale_indexes" — and keep the
            # normal resume next_step. Exact freshness stays available via
            # SessionStart, ai_index_status, sync, and diagnostics (verify_index
            # defaults to True everywhere else).
            result = {
                "state": "indexes_unverified",
                "next_step": "session_resume_bundle",
                "session_id": resolved_session_id,
                "index_status": "unverified",
            }
            if imported_skill_state.get("selected_skills") or imported_skill_state.get(
                "provider_states",
            ):
                result["imported_skill_state"] = imported_skill_state
                result["active_skills"] = list(imported_skill_state.get("active_skills", []))
                result["runtime_owned_capabilities"] = [
                    item
                    for item in (imported_skill_state.get("runtime_owned_capabilities") or [])
                    if isinstance(item, dict)
                ]
                result["provider_state"] = imported_skill_state.get("provider_state")
            return result

        index_status, index_details = self.runtime._index_freshness_status(project_root)
        if index_status != "ready":
            result = {
                "state": "stale_indexes",
                "next_step": "project_bootstrap_or_resume",
                "session_id": resolved_session_id,
                "index_status": index_status,
                **index_details,
            }
            if imported_skill_state.get("selected_skills") or imported_skill_state.get(
                "provider_states",
            ):
                result["imported_skill_state"] = imported_skill_state
                result["active_skills"] = list(imported_skill_state.get("active_skills", []))
                result["runtime_owned_capabilities"] = [
                    item
                    for item in (imported_skill_state.get("runtime_owned_capabilities") or [])
                    if isinstance(item, dict)
                ]
                result["provider_state"] = imported_skill_state.get("provider_state")
            return result

        result = {
            "state": "ready",
            "next_step": "session_resume_bundle",
            "session_id": resolved_session_id,
            "index_status": index_status,
            **index_details,
        }
        if imported_skill_state.get("selected_skills") or imported_skill_state.get(
            "provider_states",
        ):
            result["imported_skill_state"] = imported_skill_state
            result["active_skills"] = list(imported_skill_state.get("active_skills", []))
            result["runtime_owned_capabilities"] = [
                item
                for item in (imported_skill_state.get("runtime_owned_capabilities") or [])
                if isinstance(item, dict)
            ]
            result["provider_state"] = imported_skill_state.get("provider_state")
        return result

    def host_state(
        self,
        project_root: Path,
        session_id: str | None = None,
        prompt_text: str | None = None,
        *,
        verify_index: bool = True,
        host_session_id: str = "",
    ) -> dict[str, object]:
        managed_mode = self.hub.managed_mode.get_mode(
            project_root,
            host_session_id=host_session_id,
        )
        resolved_session_id = (
            session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        )
        if resolved_session_id is None:
            # #1027 authority door. This CHOOSES the session every read below
            # is keyed on, so `active` is not enough: the binding must name a
            # session that can actually be read.
            #
            # JUDGES THE STATE ALREADY FETCHED ABOVE — this function needs the
            # full `managed_mode` dict for its descriptive fields, and calling
            # the fetching door here would read it a SECOND time per host.
            # test_host_state_resolves_managed_session_by_conductor counts
            # those calls and caught exactly that.
            from .managed_mode_service import judge_managed_state

            managed_session_id, _why = judge_managed_state(managed_mode, project_root)
            if managed_session_id:
                resolved_session_id = managed_session_id

        session_snapshot = self.session_start_state(
            project_root, session_id=resolved_session_id, verify_index=verify_index,
        )
        if resolved_session_id is None:
            resolved_session_id = (
                str((session_snapshot or {}).get("session_id") or "").strip() or None
            )

        cached_skill_state: dict[str, object] = {
            "source": "skill_trigger_state",
            "session_id": resolved_session_id,
            "intent": None,
            "workflow_state": None,
            "selected_skills": [],
            "active_skills": [],
            "runtime_owned_capabilities": [],
            "provider_states": {},
            "provider_state": None,
            "triggered": [],
            "path": None,
        }
        if resolved_session_id:
            cached_skill_state = self.runtime._read_host_skill_state(
                project_root,
                resolved_session_id,
            )
            if not cached_skill_state.get("selected_skills") and not cached_skill_state.get(
                "provider_states",
            ):
                cached_skill_state = self.runtime._persist_host_skill_state(
                    project_root,
                    resolved_session_id,
                    intent="startup",
                    workflow_state="session_start",
                )

        cached_selected_skills = list(cached_skill_state.get("selected_skills", []))
        cached_active_skills = list(cached_skill_state.get("active_skills", []))
        cached_runtime_owned_capabilities = [
            item
            for item in (cached_skill_state.get("runtime_owned_capabilities") or [])
            if isinstance(item, dict)
        ]
        cached_helper_skill_guidance = [
            item
            for item in (cached_skill_state.get("helper_skill_guidance") or [])
            if isinstance(item, dict)
        ]
        cached_triggered = [
            item for item in (cached_skill_state.get("triggered") or []) if isinstance(item, dict)
        ]
        cached_mode_metadata = self.runtime._build_imported_skill_mode_metadata(
            selected_skills=cached_selected_skills,
            active_skills=cached_active_skills,
            triggered=cached_triggered,
            provider_states=cached_skill_state.get("provider_states")
            if isinstance(cached_skill_state.get("provider_states"), dict)
            else None,
        )

        prompt_text_value = prompt_text.strip() if isinstance(prompt_text, str) else ""
        prompt_action_kind = None
        prompt_intent = None
        live_prompt_skill_state: dict[str, object] | None = None
        if prompt_text_value:
            prompt_action_kind = str(
                self.runtime.classify_prompt_action(
                    prompt_text_value,
                    project_root=project_root,
                    session_id=resolved_session_id,
                ).get("action_kind")
                or "understand",
            )
            prompt_intent = self.runtime._infer_skill_trigger_intent(
                prompt_text_value,
                action_kind=prompt_action_kind,
                project_root=project_root,
                session_id=resolved_session_id,
            )
            if resolved_session_id:
                live_prompt_skill_state = self.runtime._resolve_skill_trigger_state(
                    project_root,
                    resolved_session_id,
                    intent=prompt_intent,
                    workflow_state=prompt_action_kind,
                )

        prompt_active_skills = list((live_prompt_skill_state or {}).get("active_skills", []))
        prompt_runtime_owned_capabilities = [
            item
            for item in ((live_prompt_skill_state or {}).get("runtime_owned_capabilities") or [])
            if isinstance(item, dict)
        ]
        prompt_helper_skill_guidance = [
            item
            for item in (
                ((live_prompt_skill_state or {}).get("imported_skill_state") or {}).get(
                    "helper_skill_guidance",
                )
                or []
            )
            if isinstance(item, dict)
        ]
        prompt_triggered = [
            item
            for item in ((live_prompt_skill_state or {}).get("triggered") or [])
            if isinstance(item, dict)
        ]
        prompt_mode_metadata = self.runtime._build_imported_skill_mode_metadata(
            selected_skills=cached_selected_skills,
            active_skills=prompt_active_skills,
            triggered=prompt_triggered,
            provider_states=(live_prompt_skill_state or {}).get("provider_states")
            if isinstance((live_prompt_skill_state or {}).get("provider_states"), dict)
            else None,
        )
        prompt_override_modes = dict((prompt_mode_metadata or {}).get("active_skill_modes") or {})
        activation_succeeded = bool(prompt_triggered)
        prompt_block: dict[str, object] | None = None
        prompt_enforcement: dict[str, object] | None = None
        if prompt_text_value:
            try:
                from .config import get_setting

                policy = (
                    str(
                        get_setting(
                            "security.prompt_secret_policy",
                            project_root=project_root,
                            default="block",
                        )
                        or "block",
                    )
                    .strip()
                    .lower()
                )
                if policy == "block":
                    from .heuristic_judge import extract_credential_tokens

                    hits = extract_credential_tokens(prompt_text_value)
                    if hits:
                        preview = ", ".join(
                            (t[:6] + "…" + t[-2:] if len(t) > 10 else "***") for t in hits[:3]
                        )
                        prompt_block = {
                            "decision": "block",
                            "reason": (
                                f"🛑 SECRET DETECTED — your message contains "
                                f"credential token(s) [{preview}] and the "
                                f"security.prompt_secret_policy='block' is "
                                f"active. The turn was refused; the agent "
                                f"never saw the prompt. Store the secret in "
                                f"an env var or a secret manager and refer to "
                                f"it by name instead. Dashboard toggle: set "
                                f"security.prompt_secret_policy='allow' if "
                                f"you intended to share this credential."
                            ),
                        }
                        prompt_enforcement = {
                            "stage": "prompt",
                            "decision": "block",
                            "reason": str(prompt_block["reason"]),
                            "user_message": str(prompt_block["reason"]),
                        }

            except Exception:
                pass

        active_managed_session = bool(managed_mode.get("active") and resolved_session_id)
        recommended_flow = ["runtime_preflight"]
        if active_managed_session:
            recommended_flow.append("ai_plan_status")
        elif (session_snapshot or {}).get("next_step") == "session_resume_bundle":
            # session_start MCP tool removed 2026-04-30 — recommend
            # ai_session(mode='connect'), the canonical bind path.
            recommended_flow.append("ai_session")
        if prompt_action_kind in {
            "edit",
            "write_memory",
            "task_begin",
            "task_update",
            "task_complete",
        }:
            recommended_flow.append("ai_task")
        if prompt_action_kind in {"understand", "trace", "edit", "ai_bundle"}:
            recommended_flow.append("orchestrate")

        host_actions = {
            "inject_context": [
                self.runtime._interaction_text(
                    "managed.use_mcp_first_short",
                    project_root=project_root,
                    session_id=resolved_session_id,
                ),
            ],
            "recommended_mcp_flow": recommended_flow,
            "show_imported_skills": bool(prompt_active_skills),
            "show_runtime_owned_capabilities": bool(prompt_runtime_owned_capabilities),
            "enforcement": prompt_enforcement,
            "prompt": {
                "continuation_hint": "",
                "block": prompt_block,
            },
        }
        lifecycle_state: dict[str, object] | None = None
        if resolved_session_id:
            try:
                lifecycle_state = self.hub.execution.session_lifecycle_activity_summary(
                    project_root,
                    resolved_session_id,
                )
            except Exception as exc:
                self._logger.debug("Failed to compute lifecycle state: %s", exc)
                lifecycle_state = None
        workflow_summary = self.runtime._summarize_workflow_actions(
            project_root,
            resolved_session_id,
        )
        if (
            prompt_text_value
            and resolved_session_id
            and len(prompt_text_value.strip()) < 40
            and prompt_text_value.strip().lower()
            in {
                "ok",
                "continue",
                "next",
                "go",
                "yes",
                "yep",
                "yeah",
                "sure",
                "do it",
                "keep going",
                "proceed",
                "all of them",
                "perfect",
                "great",
                "nice",
                "good",
            }
        ):
            try:
                plan_path = (
                    project_root
                    / ".MEMORY"
                    / "sessions"
                    / resolved_session_id
                    / "plans"
                    / "PLAN.md"
                )
                if plan_path.is_file():
                    lines = plan_path.read_text(encoding="utf-8").splitlines()
                    incomplete = [
                        line.replace("- [ ]", "", 1).strip()
                        for line in lines
                        if line.strip().startswith("- [ ]")
                    ]
                    if incomplete:
                        host_actions["prompt"]["continuation_hint"] = (
                            f"Session plan has {len(incomplete)} incomplete step(s). "
                            f"Next: {incomplete[0]}"
                        )
            except Exception:
                pass
        use_cached_skill_snapshot = not prompt_text_value
        effective_active_skills = (
            cached_active_skills if use_cached_skill_snapshot else prompt_active_skills
        )
        effective_runtime_owned_capabilities = (
            cached_runtime_owned_capabilities
            if use_cached_skill_snapshot
            else prompt_runtime_owned_capabilities
        )
        effective_helper_skill_guidance = (
            cached_helper_skill_guidance
            if use_cached_skill_snapshot
            else prompt_helper_skill_guidance
        )
        effective_override_modes = (
            dict((cached_mode_metadata or {}).get("active_skill_modes") or {})
            if use_cached_skill_snapshot
            else prompt_override_modes
        )
        interaction_text = self.runtime._build_host_interaction_text(
            project_root=project_root,
            session_id=resolved_session_id,
            startup_state=str((session_snapshot or {}).get("state") or ""),
            managed=bool(managed_mode.get("active")),
            prompt_action_kind=prompt_action_kind,
            active_skills=effective_active_skills,
            override_modes=effective_override_modes,
            runtime_owned_capabilities=effective_runtime_owned_capabilities,
            helper_skill_guidance=effective_helper_skill_guidance,
            workflow_summary=workflow_summary,
            lifecycle_state=lifecycle_state,
        )
        if active_managed_session:
            host_actions["inject_context"].append(
                self.runtime._interaction_text(
                    "managed.stay_in_session",
                    project_root=project_root,
                    session_id=resolved_session_id,
                ),
            )
        if prompt_active_skills:
            host_actions["inject_context"].append(
                self.runtime._interaction_text(
                    "managed.skills_active_for_prompt",
                    project_root=project_root,
                    session_id=resolved_session_id,
                    skills=", ".join(
                        str(item) for item in prompt_active_skills if str(item).strip()
                    ),
                ),
            )
        if prompt_runtime_owned_capabilities:
            host_actions["inject_context"].append(
                self.runtime._interaction_text(
                    "managed.workflow_authority_for_prompt",
                    project_root=project_root,
                    session_id=resolved_session_id,
                    capabilities=", ".join(
                        str(item.get("capability_id") or "").strip()
                        for item in prompt_runtime_owned_capabilities
                        if isinstance(item, dict) and str(item.get("capability_id") or "").strip()
                    ),
                ),
            )

        return {
            "session_state": {
                "managed": bool(managed_mode.get("active")),
                "session_id": resolved_session_id,
                "state": (session_snapshot or {}).get("state"),
                "next_step": (session_snapshot or {}).get("next_step"),
                "index_status": (session_snapshot or {}).get("index_status"),
                "plan_ready": (session_snapshot or {}).get("state") == "ready",
            },
            "skill_state": {
                "session_snapshot": {
                    "source": "cached_session",
                    "session_id": resolved_session_id,
                    "selected_skills": cached_selected_skills,
                    "active_skills": cached_active_skills,
                    "provider_states": dict(cached_skill_state.get("provider_states", {})),
                    "provider_state": cached_skill_state.get("provider_state"),
                    "triggered": cached_triggered,
                    "snapshot_path": cached_skill_state.get("path"),
                    "mode_metadata": cached_mode_metadata,
                    "runtime_owned_capabilities": cached_runtime_owned_capabilities,
                    "helper_skill_guidance": cached_helper_skill_guidance,
                },
                "prompt_activation": {
                    "source": "live_prompt" if prompt_text_value else "no_prompt",
                    "session_id": resolved_session_id,
                    "active_skills": prompt_active_skills,
                    "runtime_owned_capabilities": prompt_runtime_owned_capabilities,
                    "triggered": prompt_triggered,
                    "mode_metadata": prompt_mode_metadata,
                    "activation_succeeded": activation_succeeded,
                    "helper_skill_guidance": prompt_helper_skill_guidance,
                },
            },
            "prompt_state": {
                "source": "live_prompt" if prompt_text_value else "no_prompt",
                "prompt_text": prompt_text_value or None,
                "action_kind": prompt_action_kind,
                "intent": prompt_intent,
                "triggered_skills": [
                    item.get("skill_id") for item in prompt_triggered if item.get("skill_id")
                ],
                "active_skills": prompt_active_skills,
                "runtime_owned_capabilities": prompt_runtime_owned_capabilities,
                "override_modes": prompt_override_modes,
                "mode_metadata": prompt_mode_metadata,
                "activation_succeeded": activation_succeeded,
                "helper_skill_guidance": prompt_helper_skill_guidance,
            },
            "inspection_state": {
                "provider_states": dict(cached_skill_state.get("provider_states", {})),
                "provider_state": cached_skill_state.get("provider_state"),
                "session_state_source": "session_start_state",
                "skill_state_sources": {
                    "session_snapshot": "cached_session",
                    "prompt_activation": "live_prompt" if prompt_text_value else "no_prompt",
                },
                "prompt_state_source": "live_prompt" if prompt_text_value else "no_prompt",
                "skill_snapshot_path": cached_skill_state.get("path"),
            },
            "lifecycle_state": lifecycle_state,
            "host_actions": host_actions,
            "interaction_text": interaction_text,
        }
