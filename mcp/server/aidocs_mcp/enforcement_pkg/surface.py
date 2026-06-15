"""Request shape + Surface enum.

The Request is what every adapter constructs. The Surface tells the
controller which pipeline variant to run (PreToolUse vs UPS vs
run_kill vs dashboard_action have different gate sets).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Surface(str, Enum):
    """Where the request came from. Picks the pipeline variant."""

    CLAUDE_PRETOOL = "claude_pretool"
    CLAUDE_USER_PROMPT_SUBMIT = "claude_user_prompt_submit"
    CLAUDE_SESSION_START = "claude_session_start"
    CLAUDE_POST_TOOL_USE = "claude_post_tool_use"
    MCP_TOOL = "mcp_tool"
    MCP_RUN_KILL = "mcp_run_kill"
    OPENCODE_PRETOOL = "opencode_pretool"
    CODEX_PRETOOL = "codex_pretool"
    OPENAI_AGENTS_PRETOOL = "openai_agents_pretool"
    DASHBOARD_ACTION = "dashboard_action"


@dataclass
class Actor:
    """Caller identity. None when the surface is anonymous (e.g.
    plain MCP tool from an unauthenticated host).
    """

    actor_id: str = ""
    role: str = ""  # rbac tier (admin / operator / lane_worker / ...)
    machine_id: str = ""
    has_signed_admin_token: bool = False  # for dashboard breakglass gate


@dataclass
class Request:
    """Canonical enforcement request.

    All adapters build one of these. Field order/grouping is stable
    so audit / parity-test fixtures can rely on it.
    """

    request_id: str  # adapter-generated ulid
    project_root: Path
    surface: Surface
    host_kind: str  # "claude_code" / "opencode" / etc.

    # Identity
    host_session_id: str = ""  # canonical
    cli_session_id: str = ""  # legacy alias accepted
    actor: Actor | None = None

    # What is being attempted
    tool_name: str = ""
    action_kind: str = ""  # e.g. "edit" / "shell" / "kill"
    operation_kind: str = ""  # "read" / "write" / "execute" / "admin"
    tool_input: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    target_paths: list[Path] = field(default_factory=list)

    # Optional context
    prompt: str = ""  # for UPS surface
    run_id: str = ""  # for run_kill surface
