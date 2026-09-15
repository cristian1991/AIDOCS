"""Execution family — run/test tools.

This hosts ``ai_test``, the subagent-SAFE, language-agnostic test runner, and
the ``ai_run`` detached-shell trio.

ONE RUNNER PER SURFACE (operator ruling 2026-07-26). ``ai_run`` and its
siblings are GATE_ONLY.

The axis is HARNESS vs NO-HARNESS, not geography. A LOCAL agent means any agent
editing code inside its own harness — the operator keywords are ``local``,
``serveragent`` and ``remoteagent``; a remoteagent is still a harness agent. All
of them already hold a shell that the AIDOCS gate governs (PreToolUse ->
shell_enforcement / bash_policy / heuristic_judge), so routing them through
ai_run is a governance detour to reach a capability they already have.

A WebMCP caller has NO harness and NO shell, so the governed dispatcher is the
only way it can run anything at all. That is who ai_run exists for.

  harness agents (local | serveragent | remoteagent) -> governed raw bash
  WebMCP / no-shell callers                          -> ai_run

Having BOTH routes produced a DEADLOCK: policy refused the deploy via ai_run
("use the Bash tool") while the Bash allowlist refused `bash` ("requires
operator confirmation"), so each refusal pointed at the other and the deploy
became unreachable by any harness agent.

``ai_test`` stays BOTH: it is a normal tier-M action (it does not edit files
and runs only the resolved test argv — shell=False), so it routes through the
standard gate cascade like any mixed action, and a remote caller needs it just
as much as a local one.
"""

from __future__ import annotations

from ..run_tool_contracts import (
    AI_RUN_ANNOTATIONS,
    AI_RUN_DESCRIPTION,
    AI_RUN_KILL_ANNOTATIONS,
    AI_RUN_KILL_DESCRIPTION,
    AI_RUN_OUTPUT_ANNOTATIONS,
    AI_RUN_OUTPUT_DESCRIPTION,
    RunAction,
    RunScope,
)
from ..tool_interface import BOTH, EDIT, GATE_ONLY, M, RUN, _delegate, tool


@tool(
    surface=GATE_ONLY,
    cls=RUN,
    tier=M,
    scope="project_run",
    description=AI_RUN_DESCRIPTION,
    annotations=AI_RUN_ANNOTATIONS,
)
def ai_run(
    command: str = "",
    timeout_seconds: int = 600,
    foreground: bool = False,
    cwd: str = "",
    action: RunAction = "",
    run_id: str = "",
    tail_bytes: int = 4096,
    raw_output: bool = False,
    confirm_token: str = "",
    scope: RunScope = "session",
) -> dict:
    """Canonical detached-shell dispatcher contract."""
    return _delegate(
        "ai_run",
        command=command,
        timeout_seconds=timeout_seconds,
        foreground=foreground,
        cwd=cwd,
        action=action,
        run_id=run_id,
        tail_bytes=tail_bytes,
        raw_output=raw_output,
        confirm_token=confirm_token,
        scope=scope,
    )


@tool(
    surface=GATE_ONLY,
    cls=RUN,
    tier=M,
    scope="project_run",
    description=AI_RUN_OUTPUT_DESCRIPTION,
    annotations=AI_RUN_OUTPUT_ANNOTATIONS,
)
def ai_run_output(
    run_id: str,
    tail_bytes: int = 4096,
    wait_seconds: float = 0.0,
    raw_output: bool = False,
) -> dict:
    """Canonical governed run-output contract."""
    return _delegate(
        "ai_run_output",
        run_id=run_id,
        tail_bytes=tail_bytes,
        wait_seconds=wait_seconds,
        raw_output=raw_output,
    )


@tool(
    surface=GATE_ONLY,
    cls=RUN,
    tier=M,
    scope="project_run",
    description=AI_RUN_KILL_DESCRIPTION,
    annotations=AI_RUN_KILL_ANNOTATIONS,
)
def ai_run_kill(run_id: str) -> dict:
    """Canonical governed run-stop contract."""
    return _delegate("ai_run_kill", run_id=run_id)


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        # Running a test suite executes code + may emit artifacts; re-running is
        # not guaranteed to yield the same state, so NOT idempotent. Must match
        # _annotations() for the EDIT class (test_annotations_match_helper).
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def ai_test(
    framework: str = "",
    paths: str = "",
    name_filter: str = "",
    cwd: str = "",
    timeout_seconds: int = 900,
) -> dict:
    """Run the project's test suite — the subagent-SAFE test runner.

    Language-agnostic: auto-detects pytest / dotnet / cargo / go / npm (or pass
    `framework=` to override). Unlike ai_run it runs ONLY the resolved test
    command in argv form (shell=False) — no arbitrary shell, so a worker can
    verify its work without the raw shell that could write to / evade gate
    code. `paths` (whitespace-separated, framework-permitting) and
    `name_filter` scope the run; `cwd` picks a project subdir (AIDOCS auto-uses
    mcp/ when present).
    """
    return _delegate(
        "ai_test",
        framework=framework,
        paths=paths,
        name_filter=name_filter,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
