"""Universal cancel-safety offload for SYNC tools (#204 item-2, war (c)).

FastMCP runs SYNC tool functions INLINE on the event loop, so any blocking
sync tool (sqlite scan, subprocess wait, cold import) freezes the whole loop
— and a client cancel/disconnect during the block could take the MCP
connection down. That bit us live on 2026-06-30 (cold palace embedder) and
was fixed for the 4 palace tools in e4a106e1. This module generalizes the
fix at the REGISTRATION seam: ``server.tool`` is patched so every sync tool
registers as an async wrapper that offloads the call to a worker thread via
``anyio.to_thread.run_sync``. The event loop stays responsive, a cancel is
serviced immediately (the abandoned worker finishes quietly), and anyio's
default thread limiter bounds parallelism. Async tools pass through
untouched; contextvars propagate into the worker (anyio semantics), so
request-scoped state keeps working.

Install BEFORE any ``@server.tool()`` registration. Composes with the
universal notification injector — both patch ``server.tool`` in sequence,
and a sync fn offloaded here simply reaches the injector as an async fn.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any


def install_universal_sync_offload(server: Any) -> None:
    """Monkey-patch ``server.tool`` so sync tools register cancel-safe.

    Idempotent (attribute-marker guarded). Best-effort: if FastMCP refuses
    the re-bind, tools keep their previous inline-sync behavior."""
    if getattr(server, "_aidocs_sync_offload_installed", False):
        return

    original_tool = server.tool

    def patched_tool(*args: Any, **kwargs: Any):
        inner_decorator = original_tool(*args, **kwargs)

        def wrap_with_offload(fn):
            if inspect.iscoroutinefunction(fn):
                return inner_decorator(fn)

            @functools.wraps(fn)
            async def offloaded(*a: Any, **kw: Any):
                import anyio

                return await anyio.to_thread.run_sync(functools.partial(fn, *a, **kw))

            return inner_decorator(offloaded)

        return wrap_with_offload

    try:
        server.tool = patched_tool  # type: ignore[assignment]
        server._aidocs_sync_offload_installed = True
    except Exception:
        # Fail-open to the previous behavior — the per-tool palace offload
        # (server_palace_tools._run_palace) still covers the proven-hot path.
        pass
