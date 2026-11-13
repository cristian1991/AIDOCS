"""Lifecycle family — read-only project listing tools promoted to web.

These were local-only (advertised via @server.tool but absent from the gate
registry). Declaring them here as surface=BOTH promotes them onto the web
surface too. Each is a thin delegate to the same local handler the stdio
agent uses.

NOTE (120% clause B): worker status/jobs are NOT here — they are conductor
verbs folded into ai_lane(action='status') / ai_worker(action='status'|'list')
as the single conductor surface (no standalone ai_status/ai_jobs aliases).
"""

from __future__ import annotations

from typing import Any

from ..tool_interface import BOTH, READ, R, _READ_ANN, _delegate, tool


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def project_list(include_session_counts: bool = False) -> Any:
    """List every AIDOCS-enabled project. Set include_session_counts=true to
    include per-project session counts.
    """
    return _delegate("project_list", include_session_counts=include_session_counts)


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def project_list_sessions(project_root: str) -> Any:
    """List sessions for a named project. Works cross-project — target need not
    be the currently-bound one (requires an approved relation + permission).
    """
    return _delegate("project_list_sessions", project_root=project_root)
