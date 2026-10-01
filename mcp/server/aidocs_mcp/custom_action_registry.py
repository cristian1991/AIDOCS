"""Custom action-kind registry — Layer 3 workflow-extension slice.

AIDOCS ships a fixed set of workflow action kinds (git_commit, git_push,
etc). Operators who run custom pipelines (e.g. `bazel build`, `dbt run`,
`cargo audit`) have no declarative way to wire them into workflow.md rules
today — they'd have to fork workflow_action_service.py.

This module adds a runtime registry that composes with the built-in
WORKFLOW_ACTION_KINDS set. Keeps workflow_action_service.py untouched
(lane isolation + low blast radius). New action kinds are validated
on register and surfaced via expanded_action_kinds().

Storage is per-process in-memory; persistence is a Phase 2 concern
once the grammar lands.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_ACTION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


@dataclass(frozen=True)
class CustomActionSpec:
    """One registered custom action.

    name: snake_case identifier, 2-40 chars. Used as the action_kind
        in compiled rules. Must not collide with a built-in kind.
    command_template: shell command template with {placeholder} slots.
        Placeholders are resolved from the tool-call context at dispatch.
    required_context: tuple of context-key names the template references.
        Used at register time to validate and at dispatch to fail fast
        when context is missing.
    description: one-line human-readable intent; surfaced by the
        dashboard action-catalog view.
    """

    name: str
    command_template: str
    required_context: tuple[str, ...]
    description: str


class CustomActionRegistry:
    """Per-process in-memory store for custom action kinds."""

    # Names reserved for built-in actions (kept in lockstep with
    # WorkflowActionService.WORKFLOW_ACTION_KINDS). Registration
    # validates against this set to prevent accidental overrides.
    _BUILTIN_ACTION_KINDS: frozenset[str] = frozenset(
        {
            "git_commit",
            "git_push",
            "git_commit_and_push",
            "git_status_check",
            "github_workflow_check",
            "deploy_health_check",
            "local_command",
            "ssh_command",
        },
    )

    def __init__(self) -> None:
        self._actions: dict[str, CustomActionSpec] = {}

    def register(
        self,
        name: str,
        command_template: str,
        required_context: Iterable[str] = (),
        description: str = "",
    ) -> CustomActionSpec:
        """Register a custom action. Returns the stored spec.

        Raises ValueError on collision with a built-in or on invalid
        name/template/context shape. Re-registering a name is an error;
        call unregister() first to replace.
        """
        name = str(name or "").strip().lower()
        if not _ACTION_NAME_PATTERN.match(name):
            raise ValueError(
                f"invalid action name: {name!r}. Expected snake_case, "
                f"2-40 chars, starting with a letter.",
            )
        if name in self._BUILTIN_ACTION_KINDS:
            raise ValueError(
                f"action name {name!r} collides with a built-in kind. "
                f"Pick a different name; built-ins cannot be overridden.",
            )
        if name in self._actions:
            raise ValueError(
                f"action name {name!r} already registered. Call "
                f"unregister({name!r}) first to replace.",
            )
        template = str(command_template or "").strip()
        if not template:
            raise ValueError("command_template must not be empty")
        context = tuple(str(c).strip() for c in required_context or () if c and str(c).strip())
        # Every required_context key must actually appear in the template
        # so dispatch can't reference a key that nothing consumes.
        for key in context:
            placeholder = "{" + key + "}"
            if placeholder not in template:
                raise ValueError(
                    f"required_context key {key!r} is declared but "
                    f"the template does not reference {{{key}}}.",
                )
        spec = CustomActionSpec(
            name=name,
            command_template=template,
            required_context=context,
            description=str(description or "").strip()[:200],
        )
        self._actions[name] = spec
        return spec

    def unregister(self, name: str) -> bool:
        """Remove a registered action. Returns True if present, False
        if the name wasn't registered. Built-in names are never
        unregistered — attempts are silently ignored (no raise) so
        idempotent reset-scripts don't break on clean environments.
        """
        name = str(name or "").strip().lower()
        if name in self._BUILTIN_ACTION_KINDS:
            return False
        return self._actions.pop(name, None) is not None

    def get(self, name: str) -> CustomActionSpec | None:
        """Look up a registered action by name. None if missing."""
        return self._actions.get(str(name or "").strip().lower())

    def registered_names(self) -> tuple[str, ...]:
        """All custom action names currently registered (sorted)."""
        return tuple(sorted(self._actions))

    def expanded_action_kinds(self) -> frozenset[str]:
        """Built-in kinds + every registered custom kind.

        Consumers who previously referenced WORKFLOW_ACTION_KINDS switch
        to this call to accept both. Returns a fresh snapshot so callers
        can iterate without worrying about concurrent registration.
        """
        return frozenset(self._BUILTIN_ACTION_KINDS) | frozenset(self._actions)

    def clear(self) -> None:
        """Drop every registered custom action. Test-only helper."""
        self._actions.clear()
