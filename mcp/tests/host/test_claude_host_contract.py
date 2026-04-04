from pathlib import Path

import pytest

from aidocs_mcp.claude_hook import ClaudeHookHandler


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text("# Context\n", encoding="utf-8")


def _seed_project(project_root: Path) -> None:
    mem = project_root / ".MEMORY"
    (mem / ".aidocs").mkdir(parents=True, exist_ok=True)
    for name in [
        "index.aidocs",
        "global-instructions.aidocs",
        "coding-standards.aidocs",
        "memory-system.aidocs",
    ]:
        (mem / ".aidocs" / name).write_text(f"# {name}\n", encoding="utf-8")
    (mem / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")


def _make_handler(tmp_path: Path) -> tuple[ClaudeHookHandler, Path]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    handler = ClaudeHookHandler()
    handler.runtime.hub = handler.runtime.hub.__class__(templates_root=templates)
    handler.runtime = handler.runtime.__class__(handler.runtime.hub)
    project_root = tmp_path / "project"
    _seed_project(project_root)
    return handler, project_root


def test_claude_hook_consumes_runtime_host_state_contract_for_session_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handler, project_root = _make_handler(tmp_path)
    calls: list[tuple[Path, str | None, str | None]] = []

    def fake_host_state(
        root: Path, session_id: str | None = None, prompt_text: str | None = None
    ) -> dict[str, object]:
        calls.append((root, session_id, prompt_text))
        return {
            "session_state": {
                "managed": True,
                "session_id": "session-a",
                "state": "ready",
                "next_step": None,
                "index_status": "fresh",
                "plan_ready": True,
            },
            "skill_state": {
                "session_snapshot": {
                    "source": "cached_session",
                    "session_id": "session-a",
                    "selected_skills": ["superpowers_external/brainstorming"],
                    "active_skills": ["brainstorming"],
                    "provider_states": {"superpowers_external": "compatible"},
                    "provider_state": "compatible",
                    "triggered": [{"skill_id": "brainstorming"}],
                    "snapshot_path": None,
                    "mode_metadata": {},
                },
                "prompt_activation": {
                    "source": "no_prompt",
                    "session_id": "session-a",
                    "active_skills": [],
                    "triggered": [],
                    "mode_metadata": {},
                    "activation_succeeded": False,
                },
            },
            "prompt_state": {
                "source": "no_prompt",
                "prompt_text": None,
                "action_kind": None,
                "intent": None,
                "triggered_skills": [],
                "active_skills": [],
                "override_modes": {},
                "mode_metadata": {},
                "activation_succeeded": False,
            },
            "inspection_state": {
                "provider_states": {"superpowers_external": "compatible"},
                "provider_state": "compatible",
                "session_state_source": "session_start_state",
                "skill_state_sources": {
                    "session_snapshot": "cached_session",
                    "prompt_activation": "no_prompt",
                },
                "prompt_state_source": "no_prompt",
                "skill_snapshot_path": None,
            },
            "host_actions": {
                "inject_context": ["Use AIDOCS MCP tools first."],
                "recommended_mcp_flow": ["runtime_preflight", "plan_conductor_status"],
                "show_imported_skills": True,
            },
        }

    monkeypatch.setattr(handler.runtime, "host_state", fake_host_state)

    def unexpected_session_start_state(
        *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        raise AssertionError("session_start_state should not be used directly")

    monkeypatch.setattr(
        handler.runtime, "session_start_state", unexpected_session_start_state
    )

    result = handler.handle(
        {"hook_event_name": "SessionStart", "cwd": str(project_root)}
    )

    assert calls == [(project_root, None, None)]
    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert (
        "AIDOCS startup check: startup state is ready." in payload["additionalContext"]
    )
    assert "Continue with session `session-a`." in payload["additionalContext"]
    assert "Stay in the bound AIDOCS session" in payload["additionalContext"]
    assert "Imported skills: `brainstorming`." in payload["additionalContext"]


def test_claude_hook_consumes_runtime_host_state_contract_for_user_prompt_submit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handler, project_root = _make_handler(tmp_path)
    prompt = "brainstorm the homepage and tell me how the startup flow should work before I change code"
    calls: list[tuple[Path, str | None, str | None]] = []

    monkeypatch.setattr(
        handler.runtime,
        "route_prompt",
        lambda *_args, **_kwargs: {
            "managed_mode": True,
            "session_id": "session-a",
            "recommended_mcp_flow": ["runtime_preflight"],
            "imported_skill_state": {"active_skills": []},
        },
    )

    def fake_host_state(
        root: Path, session_id: str | None = None, prompt_text: str | None = None
    ) -> dict[str, object]:
        calls.append((root, session_id, prompt_text))
        return {
            "session_state": {
                "managed": True,
                "session_id": "session-a",
                "state": "ready",
                "next_step": None,
                "index_status": "fresh",
                "plan_ready": True,
            },
            "skill_state": {
                "session_snapshot": {
                    "source": "cached_session",
                    "session_id": "session-a",
                    "selected_skills": ["superpowers_external/brainstorming"],
                    "active_skills": ["superpowers_external/brainstorming"],
                    "provider_states": {"superpowers_external": "compatible"},
                    "provider_state": "compatible",
                    "triggered": [{"skill_id": "superpowers_external/brainstorming"}],
                    "snapshot_path": None,
                    "mode_metadata": {},
                },
                "prompt_activation": {
                    "source": "live_prompt",
                    "session_id": "session-a",
                    "active_skills": ["superpowers_external/brainstorming"],
                    "triggered": [{"skill_id": "superpowers_external/brainstorming"}],
                    "mode_metadata": {
                        "active_skill_modes": {
                            "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
                        }
                    },
                    "activation_succeeded": True,
                },
            },
            "prompt_state": {
                "source": "live_prompt",
                "prompt_text": prompt,
                "action_kind": "understand",
                "intent": "brainstorming",
                "triggered_skills": ["superpowers_external/brainstorming"],
                "active_skills": ["superpowers_external/brainstorming"],
                "override_modes": {
                    "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
                },
                "mode_metadata": {
                    "active_skill_modes": {
                        "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
                    }
                },
                "activation_succeeded": True,
            },
            "inspection_state": {
                "provider_states": {"superpowers_external": "compatible"},
                "provider_state": "compatible",
                "session_state_source": "session_start_state",
                "skill_state_sources": {
                    "session_snapshot": "cached_session",
                    "prompt_activation": "live_prompt",
                },
                "prompt_state_source": "live_prompt",
                "skill_snapshot_path": None,
            },
            "host_actions": {
                "inject_context": ["Use AIDOCS MCP tools first."],
                "recommended_mcp_flow": ["runtime_preflight", "orchestrate"],
                "show_imported_skills": True,
            },
        }

    monkeypatch.setattr(handler.runtime, "host_state", fake_host_state)

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": prompt,
        }
    )

    assert calls == [(project_root, None, prompt)]
    assert result is not None
    payload = result["hookSpecificOutput"]
    assert payload["hookEventName"] == "UserPromptSubmit"
    assert "Action: `understand`." in payload["additionalContext"]
    assert (
        "Imported skills: `superpowers_external/brainstorming`."
        in payload["additionalContext"]
    )
    assert (
        "Imported skill modes: `superpowers_external/brainstorming=provider_content_aidocs_runtime`."
        in payload["additionalContext"]
    )


def test_claude_hook_consumes_prompt_level_override_metadata_from_runtime_host_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handler, project_root = _make_handler(tmp_path)
    prompt = "write the plan before editing the startup flow"

    monkeypatch.setattr(
        handler.runtime,
        "route_prompt",
        lambda *_args, **_kwargs: {
            "managed_mode": True,
            "session_id": "session-a",
            "recommended_mcp_flow": ["runtime_preflight", "orchestrate"],
            "imported_skill_state": {"active_skills": []},
        },
    )

    monkeypatch.setattr(
        handler.runtime,
        "host_state",
        lambda root, session_id=None, prompt_text=None: {
            "session_state": {
                "managed": True,
                "session_id": "session-a",
                "state": "ready",
                "next_step": None,
                "index_status": "fresh",
                "plan_ready": True,
            },
            "skill_state": {
                "session_snapshot": {
                    "source": "cached_session",
                    "session_id": "session-a",
                    "selected_skills": ["superpowers_external/brainstorming"],
                    "active_skills": ["superpowers_external/brainstorming"],
                    "provider_states": {"superpowers_external": "compatible"},
                    "provider_state": "compatible",
                    "triggered": [],
                    "snapshot_path": None,
                    "mode_metadata": {
                        "selected_skill_modes": {
                            "superpowers_external/brainstorming": "provider_content_aidocs_runtime"
                        }
                    },
                },
                "prompt_activation": {
                    "source": "live_prompt",
                    "session_id": "session-a",
                    "active_skills": [
                        "superpowers_external/brainstorming",
                    ],
                    "triggered": [
                        {"skill_id": "superpowers_external/brainstorming"},
                        {"skill_id": "writing-plans"},
                    ],
                    "runtime_owned_capabilities": [
                        {
                            "capability_id": "planning",
                            "source": "aidocs_runtime",
                            "reason": "planning orchestration stays AIDOCS-native",
                            "mode": "runtime_owned",
                            "selected_skill_id": "superpowers_external/writing-plans",
                            "provider": "superpowers_external",
                        }
                    ],
                    "mode_metadata": {
                        "active_skill_modes": {
                            "superpowers_external/brainstorming": "provider_content_aidocs_runtime",
                        },
                        "selected_skill_modes": {
                            "superpowers_external/writing-plans": "runtime_owned",
                        },
                    },
                    "activation_succeeded": True,
                },
            },
            "prompt_state": {
                "source": "live_prompt",
                "prompt_text": prompt_text,
                "action_kind": "edit",
                "intent": "planning",
                "triggered_skills": [
                    "superpowers_external/brainstorming",
                    "writing-plans",
                ],
                "active_skills": [
                    "superpowers_external/brainstorming",
                ],
                "runtime_owned_capabilities": [
                    {
                        "capability_id": "planning",
                        "source": "aidocs_runtime",
                        "reason": "planning orchestration stays AIDOCS-native",
                        "mode": "runtime_owned",
                        "selected_skill_id": "superpowers_external/writing-plans",
                        "provider": "superpowers_external",
                    }
                ],
                "override_modes": {
                    "superpowers_external/brainstorming": "provider_content_aidocs_runtime",
                },
                "mode_metadata": {
                    "active_skill_modes": {
                        "superpowers_external/brainstorming": "provider_content_aidocs_runtime",
                    },
                    "selected_skill_modes": {
                        "superpowers_external/writing-plans": "runtime_owned",
                    },
                },
                "activation_succeeded": True,
            },
            "inspection_state": {
                "provider_states": {"superpowers_external": "compatible"},
                "provider_state": "compatible",
                "session_state_source": "session_start_state",
                "skill_state_sources": {
                    "session_snapshot": "cached_session",
                    "prompt_activation": "live_prompt",
                },
                "prompt_state_source": "live_prompt",
                "skill_snapshot_path": None,
            },
            "host_actions": {
                "inject_context": ["Use AIDOCS MCP tools first."],
                "recommended_mcp_flow": ["runtime_preflight", "orchestrate"],
                "show_imported_skills": True,
            },
        },
    )

    result = handler.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(project_root),
            "prompt": prompt,
        }
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "Imported skills: `superpowers_external/brainstorming`." in context
    assert (
        "`superpowers_external/brainstorming=provider_content_aidocs_runtime`"
        in context
    )
    assert "Runtime-owned workflow capabilities: `planning`." in context
