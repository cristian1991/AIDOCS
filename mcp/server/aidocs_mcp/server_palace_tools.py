"""server_palace_tools — Phase 1 MCP tool registrar (4 tools).

Per RFC 003 §8.1: ai_palace_search, ai_palace_status,
ai_palace_diary_read, ai_palace_diary_write.

Adopt by copying to ``aidocs_mcp/server_palace_tools.py``.

Each tool handler:
  1. Builds a fresh HubContext via build_palace_context(...).
  2. Delegates to hub.palace.<method>(...).
  3. Returns the result (or surfaces a structured Refused).

Gate invocation order is enforced INSIDE hub.palace.<method> per
RFC 003 §6.2 / §6.4 — not by this registrar. Universal notification
injection (mcp_server.py:1059-1060) wraps these tools automatically
because the registrar is called AFTER install_universal_notification_injection.
"""

from __future__ import annotations

from typing import Any

import threading as _threading

from .palace_hub_extension import build_palace_context
from .tool_display import renders_as

# Palace read/write tools open ChromaDB, which lazily initializes an ONNX
# embedder. That init can be pathologically slow on a cold process, and a
# synchronous hang here wedges the whole MCP request — observed 2026-06-30, a
# 2-minute ai_palace_status hang closed the MCP connection outright. Bound every
# palace tool call so a wedged engine fails fast instead of taking the session
# down. The canonical sqlite memory is unaffected; the abandoned daemon never
# blocks process exit.
PALACE_READ_TIMEOUT_S = 8.0


def _palace_unavailable(label: str, *, timed_out: bool = False, error: str = "") -> dict:
    note = (
        f"palace '{label}' exceeded {PALACE_READ_TIMEOUT_S:g}s (likely a slow "
        "ChromaDB/embedder init) and was abandoned so the MCP connection stays "
        "alive; canonical sqlite memory is unaffected — retry once the embedder "
        "is warm."
        if timed_out
        else f"palace '{label}' failed: {error}"
    )
    return {
        "ok": False,
        "palace_unavailable": True,
        "timed_out": timed_out,
        "tool": label,
        "note": note,
    }


def _timeboxed_palace(fn, label: str):
    """Run a palace call bounded by PALACE_READ_TIMEOUT_S in a daemon thread.
    Returns (result, None) on success or (None, unavailable_dict) on timeout/error
    so a hung embedder can never wedge the MCP request."""
    box: dict = {}

    def _w() -> None:
        try:
            box["r"] = fn()
        except Exception as exc:  # noqa: BLE001 — surfaced as unavailable
            box["err"] = str(exc)

    t = _threading.Thread(target=_w, name=f"palace-{label}", daemon=True)
    t.start()
    t.join(PALACE_READ_TIMEOUT_S)
    if t.is_alive():
        return None, _palace_unavailable(label, timed_out=True)
    if "err" in box:
        return None, _palace_unavailable(label, error=box["err"])
    return box.get("r"), None


async def _run_palace(fn, label: str):
    """Cancel-safe async wrapper for a palace call. FastMCP runs SYNC tools
    INLINE on the event loop, so a sync palace tool's blocking timebox join would
    freeze the whole loop — and a client cancel/disconnect during a cold-embedder
    block could then take the MCP connection down. Offloading the (still
    daemon-thread-timeboxed) call to a worker thread keeps the loop responsive:
    the cancel/disconnect is serviced while the call blocks, and on cancel the
    abandoned daemon dies quietly. Preserves the preload + 8s timebox behavior.
    """
    import anyio

    return await anyio.to_thread.run_sync(lambda: _timeboxed_palace(fn, label))


def warm_palace_embedder(*, hub: Any, runtime: Any) -> None:
    """Fire a background daemon that warms the ChromaDB ONNX embedder via one
    trivial palace search, so the first operator palace call does not pay the
    cold-init cost — the multi-second wait that, cancelled mid-flight, took the
    MCP connection down (2026-06-30). Best-effort: never blocks startup, never
    raises; on failure the cold path simply happens on the next real call.
    """
    if getattr(hub, "palace", None) is None:
        return

    def _warm() -> None:
        try:
            ctx = build_palace_context(
                hub, runtime, tool_name="palace_embedder_warmup"
            )
            hub.palace.search(query="warmup", limit=1, hub_ctx=ctx)
        except Exception:
            pass

    _threading.Thread(
        target=_warm, name="palace-embedder-warmup", daemon=True
    ).start()


def register_palace_tools(*, server: Any, hub: Any, runtime: Any) -> None:
    """Register the four Phase 1 palace tools on the FastMCP server.

    Skipped automatically when hub.palace is None (mempalace not
    installed).
    """
    if hub.palace is None:
        return

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Palace Search",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("find", title="palace hits")
    async def ai_palace_search(
        query: str,
        wing: str = "",
        room: str = "",
        limit: int = 5,
        max_distance: float = 1.5,
    ) -> Any:
        """Hybrid BM25 + vector + closet-first search over the project
        palace. Returns metadata + snippets — NOT full verbatim content.
        Read results respect CURRENT policy (protected_files, privacy
        floor) per RFC 003 §6.4.

        Verbatim drawer content is intentionally not exposed at this
        tool surface. AIDOCS-internal consumers (capture flow,
        preflight) call hub.palace.* directly for full content with
        appropriate audit. The agent should treat search hits as
        EVIDENCE only — gates remain AIDOCS-canonical.
        """
        def _do():
            ctx = build_palace_context(hub, runtime, tool_name="ai_palace_search")
            return hub.palace.search(
                query=query,
                wing=wing or None,
                room=room or None,
                limit=limit,
                max_distance=max_distance,
                hub_ctx=ctx,
            )

        result, unavailable = await _run_palace(_do,"ai_palace_search")
        if unavailable is not None:
            return unavailable
        return _search_result_to_dict(result)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Palace Status",
        },
    )
    @renders_as("status", title="palace")
    async def ai_palace_status() -> Any:
        """Palace health: drawer counts, wing/room breakdown, KG
        counts, vector-disabled state, palace_disabled / kill_switch
        active flags. Lightweight health probe.
        """
        def _do():
            ctx = build_palace_context(hub, runtime, tool_name="ai_palace_status")
            return hub.palace.status(hub_ctx=ctx)

        status, unavailable = await _run_palace(_do,"ai_palace_status")
        if unavailable is not None:
            return unavailable
        # NOTE: memory-anchor-health probe was deliberately NOT grafted onto this
        # hot path — an earlier attempt blocked status >1min on a startup-busy DB.
        # memory_anchor_health() lives standalone, surfaced off the hot path.
        return _dataclass_to_dict(status)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Palace Diary Read",
        },
    )
    @renders_as("list", title="diary entries")
    async def ai_palace_diary_read(
        agent_name: str,
        last_n: int = 10,
        wing: str = "",
        read_across_agents: bool = False,
    ) -> Any:
        """Read recent diary entries for ``agent_name``.

        Per RFC 003 §17.1, diaries are agent-isolated by default;
        cross-agent reads require explicit ``read_across_agents=True``.
        """
        def _do():
            ctx = build_palace_context(hub, runtime, tool_name="ai_palace_diary_read")
            return hub.palace.diary_read(
                agent_name=agent_name,
                last_n=last_n,
                wing=wing or None,
                read_across_agents=read_across_agents,
                hub_ctx=ctx,
            )

        result, unavailable = await _run_palace(_do,"ai_palace_diary_read")
        if unavailable is not None:
            return unavailable
        if hasattr(result, "reason"):
            return _refused_to_dict(result)
        return result

    @server.tool(
        annotations={
            "destructiveHint": False,
            "openWorldHint": False,
            "title": "Palace Diary Write",
        },
    )
    @renders_as("status", title="diary filed")
    async def ai_palace_diary_write(
        agent_name: str,
        entry: str,
        topic: str = "",
        wing: str = "",
    ) -> Any:
        """Write an agent diary entry. AAAK-encoded entries encouraged
        but plain text accepted.

        Per RFC 003 §17.2, hidden-reasoning content (extended-thinking
        XML, model-private state) is rejected at this surface.
        """
        def _do():
            ctx = build_palace_context(hub, runtime, tool_name="ai_palace_diary_write")
            return hub.palace.diary_write(
                agent_name=agent_name,
                entry=entry,
                topic=topic or "general",
                wing=wing or None,
                hub_ctx=ctx,
            )

        result, unavailable = await _run_palace(_do,"ai_palace_diary_write")
        if unavailable is not None:
            return unavailable
        if hasattr(result, "reason"):
            return _refused_to_dict(result)
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Palace Maintenance",
        },
    )
    @renders_as("status", title="palace maintenance")
    def ai_palace_maintenance(
        mode: str,
        dry_run: bool = False,
        force: bool = False,
        operator_token: str = "",
    ) -> Any:
        """Guarded palace maintenance — AUTHENTICATED DASHBOARD ADMIN only.

        Modes:
          - ``backfill_legacy_memory_drawers`` — reconcile pre-deterministic
            random-id MemPalace drawers: mark legacy drawers deleted and
            re-ingest deterministic drawers for active memory rows.

        ``dry_run=True`` reports what WOULD change without writing.
        ``force=True`` proceeds even when the palace collection is absent
        (degraded — signals still written; nothing to look up / re-ingest).

        Authority (a config flag is policy, NOT identity):
          - dev.dev_mode authorizes the dev/debug path (no token), OR
          - an authenticated dashboard operator (operator_token, or a
            dashboard-bound host session) holding admin.palace_maintenance
            (or admin.manage_config) AND security.allow_palace_maintenance=true.
        An ordinary agent without operator auth is refused
        (blocked_by="operator_auth"); no NLP grant lifts this.

        The dashboard/Tauri action attaches the cached operator token.
        Returns {ok, scanned, retired_legacy, reingested, failed, lookup_lag,
        dry_run, authorized_via, user_id, role, source}.
        """
        from .mcp_server_runtime_helpers import resolve_project_root

        root = resolve_project_root()
        # Resolve the managed session so a SESSION-scoped
        # allow_palace_maintenance unlock is honored.
        session_id = None
        try:
            managed = hub.managed_mode.get_mode(root)
            if managed.get("active"):
                session_id = str(managed.get("session_id") or "").strip() or None
        except Exception:
            session_id = None
        return run_palace_maintenance(
            hub,
            runtime,
            root,
            mode=mode,
            dry_run=dry_run,
            force=force,
            session_id=session_id,
            operator_token=(operator_token or "").strip() or None,
        )


# ---------------------------------------------------------------------------
# Maintenance core (module-level for testability) + authority gate
# ---------------------------------------------------------------------------


def run_palace_maintenance(
    hub: Any,
    runtime: Any,
    project_root,
    *,
    mode: str,
    dry_run: bool = False,
    force: bool = False,
    session_id: str | None = None,
    operator_token: str | None = None,
) -> dict:
    """Guarded palace-maintenance dispatcher. See ai_palace_maintenance.

    Returns a result dict; refusals carry ``ok=False`` + ``blocked_by``.
    """
    from . import memory_sqlite_store as _msq

    normalized = (mode or "").strip().lower()

    # 1. Authority gate — authenticated dashboard admin (or dev_mode).
    auth = _resolve_maintenance_authority(
        project_root,
        session_id=session_id,
        operator_token=operator_token,
    )

    def _audit_attempt() -> None:
        try:
            hub.execution.record_event(
                project_root,
                event_kind="attempted_palace_maintenance",
                source_kind="ai_palace_maintenance",
                session_id=session_id or None,
                capability_name="ai_palace_maintenance",
                action_kind="maintenance",
                target_entity=normalized[:200],
                status="refused",
                payload={
                    "mode": normalized,
                    "blocked_by": auth["blocked_by"],
                    "operator_authenticated": auth["operator_authenticated"],
                    "token_present": auth["token_present"],
                    "user_id": auth["user_id"],
                    "role": auth["role"],
                    "source": auth["source"],
                    "operator_source": auth["operator_source"],
                },
                user_id=auth["user_id"] or None,
                effective_role=auth["role"] or None,
                permission_name=PERM_ADMIN_PALACE_MAINTENANCE,
            )
        except Exception:
            pass

    if not auth["authorized"]:
        _audit_attempt()
        return {
            "ok": False,
            "blocked_by": auth["blocked_by"],
            "reason": auth["reason"],
            "operator_authenticated": auth["operator_authenticated"],
            "token_present": auth["token_present"],
        }

    via = auth["via"]

    # 2. Mode dispatch.
    if normalized != "backfill_legacy_memory_drawers":
        return {
            "ok": False,
            "blocked_by": "unknown_mode",
            "reason": (
                f"unknown maintenance mode {mode!r}; supported: backfill_legacy_memory_drawers"
            ),
        }

    # 3. Palace availability — refuse unless force when absent.
    try:
        palace_present = _msq.palace_collection_path(project_root).exists()
    except Exception:
        palace_present = False
    if not palace_present and not force:
        return {
            "ok": False,
            "blocked_by": "palace_unavailable",
            "reason": (
                "MemPalace collection not found. Re-run with force=true to "
                "proceed in degraded mode (stale signals still written; no "
                "legacy lookup / re-ingest possible)."
            ),
            "authorized_via": via,
        }

    # 4. Run. dry_run needs no hub_ctx (it never re-ingests).
    ctx = None
    if not dry_run:
        try:
            ctx = build_palace_context(
                hub,
                runtime,
                tool_name="ai_palace_maintenance",
            )
        except Exception:
            ctx = None
    stats = _msq.backfill_legacy_memory_drawers(
        project_root,
        hub.palace,
        hub_ctx=ctx,
        dry_run=bool(dry_run),
    )

    # 5. Audit (success carries the acting identity on the same row).
    try:
        hub.execution.record_event(
            project_root,
            event_kind="palace_maintenance_backfill",
            source_kind="ai_palace_maintenance",
            session_id=session_id or None,
            capability_name="ai_palace_maintenance",
            action_kind="maintenance",
            target_entity=normalized[:200],
            status="dry_run" if dry_run else "applied",
            payload={
                "mode": normalized,
                "authorized_via": via,
                "source": auth["source"],
                "operator_source": auth["operator_source"],
                "user_id": auth["user_id"],
                "role": auth["role"],
                "session_id": session_id or "",
                "force": bool(force),
                "palace_present": palace_present,
                **stats,
            },
            user_id=auth["user_id"] or None,
            effective_role=auth["role"] or None,
            permission_name=PERM_ADMIN_PALACE_MAINTENANCE,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "mode": normalized,
        "authorized_via": via,
        "source": auth["source"],
        "operator_source": auth["operator_source"],
        "user_id": auth["user_id"],
        "role": auth["role"],
        **stats,
    }


PERM_ADMIN_PALACE_MAINTENANCE = "admin.palace_maintenance"


def _resolve_maintenance_authority(
    project_root,
    *,
    session_id: str | None = None,
    operator_token: str | None = None,
) -> dict:
    """Resolve who may run palace maintenance — IDENTITY + RBAC, not just a
    config flag. Returns a dict with the gate decision + audit fields:
    {authorized, via, blocked_by, reason, source, operator_authenticated,
     token_present, user_id, role}.

    Order:
      1. dev.dev_mode → authorized (dev/debug path; no token, no flag).
      2. Else require an AUTHENTICATED dashboard operator:
         - operator_token → OperatorAuthService.authenticate, OR
         - a dashboard-bound host session → resolve_bound_operator.
         No auth → blocked_by='operator_auth'.
      3. Require admin.palace_maintenance (or admin.manage_config) RBAC.
         Missing → blocked_by='missing_permission'.
      4. Option A policy: security.allow_palace_maintenance must be true
         (session-scoped). Off → blocked_by='maintenance_policy_disabled'.
    """
    from .config import get_setting

    base = {
        "authorized": False,
        "via": "",
        "blocked_by": "",
        "reason": "",
        "source": "",
        "operator_source": "",
        "operator_authenticated": False,
        "token_present": bool(operator_token),
        "user_id": "",
        "role": "",
    }

    # 1. Dev/debug path — DEV-flavour install, no token, no flag.
    try:
        from .enforcement import is_dev_flavor

        if bool(is_dev_flavor(project_root)):
            return {**base, "authorized": True, "via": "dev_mode", "source": "dev"}
    except Exception:
        pass

    # 2. Authenticate the operator (token first, then dashboard host binding).
    from .operator_auth_service import OperatorAuthService

    svc = OperatorAuthService()
    ctx = None
    if operator_token:
        try:
            ctx = svc.authenticate(operator_token, project_root, source="dashboard")
        except Exception:
            ctx = None
    if ctx is None:
        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            host_sid = current_calling_host_session_id()
            if host_sid:
                ctx = svc.resolve_operator_context_from_host_session(
                    host_sid,
                    project_root,
                )
        except Exception:
            ctx = None
    if ctx is None:
        return {
            **base,
            "blocked_by": "operator_auth",
            "reason": "authenticated dashboard admin required",
        }
    base = {
        **base,
        "operator_authenticated": True,
        "user_id": ctx.user_id,
        "role": ctx.role,
        "source": "dashboard_admin",
        # Preserve the REAL identity origin so audits can distinguish a
        # dashboard bearer token from a host-session binding.
        "operator_source": str(getattr(ctx, "source", "") or ""),
    }

    # 3. RBAC permission (dedicated, or reuse admin.manage_config).
    has_perm = False
    try:
        has_perm = svc.require_permission(
            ctx,
            PERM_ADMIN_PALACE_MAINTENANCE,
            project_root,
            scope_type="project",
            scope_id=str(project_root).replace("\\", "/"),
        ) or svc.require_permission(
            ctx,
            "admin.manage_config",
            project_root,
            scope_type="project",
            scope_id=str(project_root).replace("\\", "/"),
        )
    except Exception:
        has_perm = False
    if not has_perm:
        return {
            **base,
            "blocked_by": "missing_permission",
            "reason": ("operator lacks admin.palace_maintenance (or admin.manage_config)"),
        }

    # 4. Option A policy flag (session-scoped) — admin AND flag.
    try:
        flag = bool(
            get_setting(
                "security.allow_palace_maintenance",
                project_root=project_root,
                session_id=session_id or None,
                default=False,
            ),
        )
    except Exception:
        flag = False
    if not flag:
        return {
            **base,
            "blocked_by": "maintenance_policy_disabled",
            "reason": (
                "security.allow_palace_maintenance is off; an admin may "
                "enable it from the dashboard to allow maintenance"
            ),
        }

    return {**base, "authorized": True, "via": "operator_admin"}


# ---------------------------------------------------------------------------
# Result-shape helpers
# ---------------------------------------------------------------------------


def _search_result_to_dict(result) -> dict:
    return {
        "hits": [
            {
                "drawer_id": h.drawer_id,
                "score": h.score,
                "source_file": h.source_file,
                "unit_id": h.unit_id,
                "snippet": h.snippet,
                "why_matched": list(h.why_matched),
                "privacy_class": h.privacy_class,
                "wing": h.wing,
                "room": h.room,
                "staleness": h.staleness,
                "is_evidence_only": h.is_evidence_only,
            }
            for h in result.hits
        ],
        "total_candidates": result.total_candidates,
        "total_returned": result.total_returned,
        "filtered_by_policy": result.filtered_by_policy,
        "query_sanitized": result.query_sanitized,
        "prefer_authority": result.prefer_authority,
    }


def _dataclass_to_dict(obj) -> dict:
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj):
        return asdict(obj)
    return dict(obj.__dict__) if hasattr(obj, "__dict__") else {"value": obj}


def _refused_to_dict(refused) -> dict:
    return {
        "refused": True,
        "reason": refused.reason,
        "detail": refused.detail,
        "audit_event_id": refused.audit_event_id,
    }
