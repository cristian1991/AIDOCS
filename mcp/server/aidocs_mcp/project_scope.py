"""Connection-scoped project resolution for the shared HTTP daemon (#280 Phase 2).

The shared daemon (127.0.0.1:8748) serves many project windows from ONE process.
A rootless MCP tool call carries no project identity, so resolution fell back to
process-global state (``_last_known_project_root`` / the daemon cwd / the managed
singleton) — whichever window bound first owned resolution for ALL windows. That
is the TIER-∞ cross-tenant leak (a hooks-off DentalClinic agent's run/ledger
write surfacing under AIDOCS).

This module lets each CONNECTION declare its project and scopes resolution to it
PER REQUEST via the EXISTING ``_target_project_root_override`` ContextVar — already
the top-priority seam in ``resolve_project_root`` / ``discover_project_root``. No
new precedence is invented: we feed the existing top seam per request instead of
leaving it empty (which lets the process-globals win).

Declaration channels (first hit wins):
  1. URL query   ``?root=<abs path>``            (registry emits the scoped URL)
  2. HTTP header ``X-AIDOCS-Project-Root: <path>`` (fallback)
  3. per-connection cache keyed by the MCP session_id — for clients that send the
     declaration only at connect, not on every POST.

SECURITY invariants (plan §Security):
  * A declared root is honored ONLY when it is a COMMISSIONED AIDOCS project
    (``is_aidocs_managed``). An arbitrary ``?root=`` can NOT bind resolution to a
    non-project path.
  * ADDITIVE: absent/invalid declaration → None → the middleware is a NO-OP and
    pre-#280 behavior is unchanged. Single-tenant HTTP and stdio (env-pinned) are
    untouched — the mechanism is DORMANT until the registry emits ``?root=`` URLs.
  * The per-request override is always reset in ``finally`` (via
    ``with_target_project_root``) so it can never leak into another request that
    reuses the same async task/worker.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

_HEADER = "x-aidocs-project-root"
_QUERY = "root"

# MCP session_id → validated absolute project-root string. Connections are few
# (one per host window), so this dict stays tiny; entries are self-correcting
# (re-validated on read) and simply go stale-harmless when a window closes.
_session_root_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def _validate(raw: str) -> Path | None:
    """A declared root is honored ONLY if it resolves to a COMMISSIONED AIDOCS
    project. Returns the normalized Path, or None (reject) on any miss/error."""
    if not raw or not str(raw).strip():
        return None
    try:
        from .mcp_server_runtime_helpers import (
            _normalize_root_path,
            is_aidocs_managed,
        )

        p = _normalize_root_path(str(raw).strip())
    except Exception:
        return None
    try:
        return p if is_aidocs_managed(p) else None
    except Exception:
        return None


def _request_declaration() -> str:
    """Raw declared-root string from the CURRENT HTTP request (query then
    header), or '' when there is no live HTTP request (stdio) or none declared."""
    try:
        from fastmcp.server.dependencies import get_http_request

        req = get_http_request()
    except Exception:
        return ""
    if req is None:
        return ""
    try:
        return str(req.query_params.get(_QUERY) or req.headers.get(_HEADER) or "")
    except Exception:
        return ""


def declared_root(session_id: str = "") -> Path | None:
    """Validated declared project root for the CURRENT request, or None.

    Reads the per-request declaration first (query/header); on a hit, caches it
    against ``session_id`` and returns it. On a miss, falls back to the
    per-connection cache (for clients that declared only at connect) and
    RE-VALIDATES it (a decommission mid-connection must not keep resolving).
    """
    sid = (session_id or "").strip()
    root = _validate(_request_declaration())
    if root is not None:
        if sid:
            with _cache_lock:
                _session_root_cache[sid] = str(root)
        return root
    if sid:
        with _cache_lock:
            cached = _session_root_cache.get(sid)
        if cached:
            return _validate(cached)
    return None


def forget_session(session_id: str) -> None:
    """Drop a connection's cached root (called on session close, best-effort)."""
    sid = (session_id or "").strip()
    if not sid:
        return
    with _cache_lock:
        _session_root_cache.pop(sid, None)


def _session_id_from_context(context: Any) -> str:
    try:
        fc = getattr(context, "fastmcp_context", None)
        return str(getattr(fc, "session_id", "") or "")
    except Exception:
        return ""


def make_project_scope_middleware() -> Any:
    """Build the FastMCP ``ProjectScopeMiddleware`` (constructed lazily so the
    import cost + fastmcp dependency only load on the HTTP-daemon path)."""
    from fastmcp.server.middleware import Middleware

    class ProjectScopeMiddleware(Middleware):
        """Per-request project scoping for the shared HTTP daemon (#280).

        Scopes TWO seams for the duration of one call, both reset in finally:
          * project root — from the request's validated ``?root=`` declaration
            (clause 1), fed into ``_target_project_root_override``;
          * host-session identity (clause 2) — the per-connection MCP session_id,
            fed into the request-scoped host_session_id ContextVar so identity-
            keyed state (managed session, ledger keying) isolates per connection.
            Populated when the request DECLARES a commissioned root or under
            multitenant_strict (the activated shared daemon), so stdio /
            single-tenant / undeclared pre-activation stay byte-identical.

        DORMANT unless the request declares a COMMISSIONED root — a call with no
        declaration is a transparent pass-through (pre-#280 behavior).
        """

        async def _scoped(self, context: Any, call_next: Any) -> Any:
            from . import mcp_server_runtime_helpers as _h

            sid = _session_id_from_context(context)
            root = declared_root(sid)
            root_ctx = _h.with_target_project_root(root) if root is not None else None
            host_token = None
            if sid and (root is not None or _h.multitenant_strict_enabled()):
                # Per-connection identity: the MCP session_id is stable across a
                # connection's requests, so identity-keyed state isolates by it.
                # A request that DECLARES a commissioned root must ALSO run
                # under its own request identity — never the process-global
                # conductor stamp from another window — otherwise a scoped
                # call could read or restamp the shared process identity and
                # bleed actors across tenants. Undeclared calls stamp only
                # under multitenant_strict (pre-#280 behavior preserved).
                host_token = _h.set_request_host_identity(
                    sid,
                    host_kind="generic_mcp",
                )
            try:
                if root_ctx is not None:
                    with root_ctx:
                        return await call_next(context)
                return await call_next(context)
            finally:
                if host_token is not None:
                    _h.reset_request_host_identity(host_token)

        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            return await self._scoped(context, call_next)

        async def on_list_tools(self, context: Any, call_next: Any) -> Any:
            # Scope tool-listing too: which tools surface can depend on the
            # project's managed state, so a shared daemon must list per tenant.
            return await self._scoped(context, call_next)

    return ProjectScopeMiddleware()
