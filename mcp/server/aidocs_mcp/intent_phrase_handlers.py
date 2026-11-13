"""Intent-phrase handlers — map detected intents to state-changing
service calls.

The detector (intent_phrase_detector.py) returns a list of intent
records; this module maps each intent name to a handler function that
calls into the runtime services (currently plan_session_*) and returns
a context string the hook injects into the agent's prompt.

Handlers are registered in the _INTENT_HANDLERS dict. Adding a new
intent = adding one entry here + the corresponding phrases in the
toml + (optionally) the service method to call. Unknown intents
return a no-op result so the detector and handler tables can evolve
independently.

Module-level (not class-bound) so the claude_hook can call without
constructing a full RuntimeService — same rationale as plan_session_*
in runtime_plan_authoring_service.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .runtime_plan_authoring_service import (
    plan_session_enter,
    plan_session_exit,
)
from .session_query_gate_store import SessionQueryGateStore


def _handle_enter_plan_mode(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
    intent: dict[str, Any],
) -> dict[str, Any]:
    scope = str(intent.get("scope", "")).strip()
    result = plan_session_enter(store, project_root, session_id, scope=scope)
    if result.get("active"):
        return {
            "intent": "enter_plan_mode",
            "ok": True,
            "context": (
                f"Plan mode active. Scope: {scope}. "
                f"Call ai_plan_template FIRST for the strict lane format "
                f"(`- Phase:` / `- Lane:` / `- Files:` bullets — NOT `### Lane` "
                f"headings, which are ignored and yield a refused 0-lane plan), "
                f"fill it in, then ai_plan(action='create'). Design docs in "
                f"plans/ go to other markdown files."
            ),
        }
    return {
        "intent": "enter_plan_mode",
        "ok": False,
        "context": (f"Plan mode entry refused: {result.get('error', 'unknown reason')}."),
    }


def _handle_enter_plan_mode_invalid(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
    intent: dict[str, Any],
) -> dict[str, Any]:
    reason = intent.get("reason", "missing_scope")
    if reason == "missing_scope":
        msg = (
            "Plan mode requires scope. Retry with `create a plan for <topic>` "
            "where <topic> is the noun phrase identifying what's being planned."
        )
    else:
        msg = f"Plan mode entry refused: {reason}."
    return {
        "intent": "enter_plan_mode_invalid",
        "ok": False,
        "context": msg,
    }


def _handle_exit_plan_mode(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
    intent: dict[str, Any],
) -> dict[str, Any]:
    result = plan_session_exit(store, project_root, session_id)
    msg = (
        "Plan mode exited."
        if result.get("was_active")
        else "Plan mode was already inactive; no change."
    )
    return {
        "intent": "exit_plan_mode",
        "ok": True,
        "context": msg,
    }


def _handle_force_validate_plan(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
    intent: dict[str, Any],
) -> dict[str, Any]:
    # Phase 6 (parallelism validator) lands the real implementation.
    # Until then, this is a stub so the dispatcher recognizes the
    # intent and the operator gets feedback that validation will run.
    return {
        "intent": "force_validate_plan",
        "ok": True,
        "context": (
            "Plan validation requested. Validator implementation pending — "
            "this intent is recognized but no checks run yet."
        ),
    }


_INTENT_HANDLERS: dict[
    str,
    Callable[[SessionQueryGateStore, Path, str, dict[str, Any]], dict[str, Any]],
] = {
    "enter_plan_mode": _handle_enter_plan_mode,
    "enter_plan_mode_invalid": _handle_enter_plan_mode_invalid,
    "exit_plan_mode": _handle_exit_plan_mode,
    "force_validate_plan": _handle_force_validate_plan,
}


def dispatch_intent(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
    intent: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a single intent record to its handler.

    Unknown intents return a no-op result rather than raising so the
    detector and handler tables can evolve independently — adding a
    phrase before the handler exists is non-fatal.
    """
    name = str(intent.get("intent", ""))
    handler = _INTENT_HANDLERS.get(name)
    if handler is None:
        return {
            "intent": name,
            "ok": False,
            "context": "",
        }
    return handler(store, project_root, session_id, intent)


def dispatch_intents(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
    intents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Dispatch multiple intents in detected order.

    The detector returns intents sorted by position in the prompt, so
    the operator's intent ordering is preserved. Last-write semantics
    apply for state changes (e.g., exit-then-enter ends in plan-mode-on).
    """
    return [dispatch_intent(store, project_root, session_id, intent) for intent in intents]
