"""Canonical public contracts for the detached shell tool family.

Both local FastMCP registration and WebMCP Tool Interface declarations import
these constants. Execution remains in server_run_tools and RUN_ALLOWLIST.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

RunAction: TypeAlias = Literal["", "start", "output", "wait", "kill", "list"]
RunScope: TypeAlias = Literal["session", "all"]

AI_RUN_DESCRIPTION = """Unified governed shell dispatcher.

Start a detached command, read or wait for an existing run, stop a run after
its action-bound confirmation, or list active runs. Every action remains bound
to the selected project and caller identity. scope='all' is available only to
an authenticated org administrator; ordinary callers are session-scoped.
""".strip()

AI_RUN_OUTPUT_DESCRIPTION = """Read a governed run's bounded output or progress.

Ownership checks prevent cross-session and cross-actor reads. raw_output skips
presentation rendering but never the output security scan. wait_seconds is a
backwards-compatible ignored parameter; use ai_run(action='wait') for a bounded
server-side wait.
""".strip()

AI_RUN_KILL_DESCRIPTION = """Stop a governed run owned by the calling identity.

Cross-session and cross-actor run identifiers are refused. The unified
ai_run(action='kill') path provides the action-bound confirmation challenge;
this sibling remains a direct compatibility surface with the same ownership
law.
""".strip()

AI_RUN_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
    "title": "Code Run",
}

AI_RUN_OUTPUT_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
    "title": "Code Run Output",
}

AI_RUN_KILL_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    # Killing a running process is a state-changing termination, not a no-op:
    # the first call ends a live run; it is NOT idempotent (fixes fix/768 which
    # mistakenly marked it True). destructiveHint stays False by host doctrine —
    # AIDOCS ownership/confirmation law owns the real refusal, not a host card.
    "idempotentHint": False,
    "openWorldHint": False,
    "title": "Code Run Kill",
}
