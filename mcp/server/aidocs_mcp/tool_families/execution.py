"""Execution family — run/test tools.

For now this hosts ``ai_test``, the subagent-SAFE, language-agnostic test
runner (the replacement for raw ``ai_run`` in lane workers). ``ai_run`` and
its detached siblings stay LOCAL/conductor-only for now (they go through the
gate's special RUN_ALLOWLIST path); the fam-execution promotion of those is a
later step. ``ai_test`` is a normal tier-M action (it does not edit files and
runs only the resolved test argv — shell=False), so it routes through the
standard gate cascade like any mixed action.
"""

from __future__ import annotations

from ..tool_interface import BOTH, EDIT, M, _delegate, tool


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
