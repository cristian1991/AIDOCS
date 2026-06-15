from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config_schema import SETTINGS_CATALOG, available_config_edit_modes


class RuntimePresentationService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def dashboard_rbac(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """RBAC snapshot for the dashboard's Roles & Users page.

        Phase 5d (2026-05-02). Read-only view of identity_users +
        rbac_roles + rbac_permissions + pending escalations. Each
        section is a flat list optimized for table rendering.
        Mutations (create_user / approve_escalation / etc.) go
        through the existing MCP rbac_* tools — not exposed on the
        dashboard's first cut to keep the trust surface tight.
        """
        from .escalation_store import EscalationStore
        from .identity_store import IdentityStore
        from .rbac_store import RBACStore

        identity = IdentityStore()
        rbac = RBACStore()
        escalations = EscalationStore()

        # Users — identity_users rows.
        users_payload: list[dict[str, object]] = []
        try:
            for u in identity.list_users(project_root):
                # Resolve roles per user.

                users_payload.append(
                    {
                        "user_id": u.user_id,
                        "email": u.email,
                        "role": u.role,
                        "created_at": u.created_at,
                        "disabled": bool(u.disabled),
                    },
                )
        except Exception:
            pass

        # Roles — rbac_roles rows. PERF (2026-05-26): permission counts come
        # from ONE bulk GROUP BY query instead of N per-role round-trips that
        # opened a fresh sqlite connection each. Identical column data, no
        # truth change (still the same rbac_role_permissions rows).
        permission_counts: dict[str, int] = {}
        try:
            permission_counts = rbac.role_permission_counts(project_root)
        except Exception:
            permission_counts = {}
        roles_payload: list[dict[str, object]] = []
        try:
            for r in rbac.list_roles(project_root):
                permission_count = int(permission_counts.get(r.role_id, 0))
                roles_payload.append(
                    {
                        "role_id": r.role_id,
                        "name": r.name,
                        "description": r.description,
                        "is_system": bool(r.is_system),
                        "rank": r.rank,
                        "inherits_from_role_key": r.inherits_from_role_key,
                        "permission_count": permission_count,
                    },
                )
        except Exception:
            pass

        # Permissions — rbac_permissions rows (the catalog).
        perms_payload: list[dict[str, object]] = []
        try:
            for p in rbac.list_permissions(project_root):
                perms_payload.append(
                    {
                        "name": p.name,
                        "description": getattr(p, "description", "") or "",
                    },
                )
        except Exception:
            pass

        # Pending escalations — admins triage these.
        pending_payload: list[dict[str, object]] = []
        try:
            for req in escalations.list_pending(project_root) or []:
                pending_payload.append(
                    {
                        "request_id": req.request_id,
                        "requester_label": req.requester_label,
                        "requester_user_id": req.requester_user_id,
                        "session_id": req.session_id,
                        "task_id": req.task_id,
                        "gate_permission": req.gate_permission,
                        "gate_phrase": req.gate_phrase,
                        "sticky": bool(req.sticky),
                        "created_at": req.created_at,
                        "expires_at": req.expires_at,
                    },
                )
        except Exception:
            pass

        return {
            "users": users_payload,
            "roles": roles_payload,
            "permissions": perms_payload,
            "pending_escalations": pending_payload,
            "summary": {
                "user_count": len(users_payload),
                "active_user_count": sum(1 for u in users_payload if not u["disabled"]),
                "role_count": len(roles_payload),
                "permission_count": len(perms_payload),
                "pending_escalation_count": len(pending_payload),
            },
        }

    def dashboard_bash_policy(
        self,
        project_root: Path,
        session_id: str | None,
    ) -> dict[str, object]:
        """Resolve the full bash policy as a per-command tristate grid.

        Phase 5c (2026-05-02). For the dashboard's 3-state allow/deny/
        bubble UI. Returns:

            {
              "commands": {
                "<cmd>": {
                  "factory": "allow" | "deny" | "bubble",
                  "global": "allow" | "deny" | "bubble",
                  "project": "allow" | "deny" | "bubble",
                  "session": "allow" | "deny" | "bubble",
                  "effective": "allow" | "deny" | "bubble",
                  "patterns": ["pattern1", ...],  # effective patterns
                },
                ...
              },
              "default": "allow" | "block",
              "layers": ["factory", "global", "project", "session"],
            }

        Bubble = layer doesn't have an explicit allow/deny entry for
        this command; resolution falls through to lower layers.
        """
        from .config import _DEFAULT_CONFIG, _get_dotted
        from .config_resolver import (
            LAYER_FACTORY,
            LAYER_GLOBAL,
            LAYER_PROJECT,
            LAYER_SESSION,
            LayeredConfigResolver,
        )

        # Universe of commands: union across factory + every layer's
        # explicit allow/deny rows.
        commands: set[str] = set()
        factory_allow = _get_dotted(_DEFAULT_CONFIG, "bash.allow") or {}
        factory_deny = _get_dotted(_DEFAULT_CONFIG, "bash.deny") or {}
        if isinstance(factory_allow, dict):
            commands.update(factory_allow.keys())
        if isinstance(factory_deny, dict):
            commands.update(factory_deny.keys())

        resolver = LayeredConfigResolver()
        layers = (LAYER_FACTORY, LAYER_GLOBAL, LAYER_PROJECT, LAYER_SESSION)

        # Resolve full bash tree to discover any operator-added cmds.
        full = resolver.resolve("bash", project_root, session_id=session_id)
        resolved_value = full.value if isinstance(full.value, dict) else {}
        for k, v in (resolved_value.get("allow") or {}).items():
            commands.add(k)
        for k, v in (resolved_value.get("deny") or {}).items():
            commands.add(k)

        def _cmd_state(layer_value: Any, cmd: str) -> tuple[str, list[str] | None]:
            """Resolve allow/deny/bubble for one command at one layer.

            Per king: deny overpowers allow within a layer.
            """
            if not isinstance(layer_value, dict):
                return "bubble", None
            allow = layer_value.get("allow") or {}
            deny = layer_value.get("deny") or {}
            if cmd in deny:
                return "deny", deny.get(cmd)
            if cmd in allow:
                return "allow", allow.get(cmd)
            return "bubble", None

        # For each layer, resolve its own contribution by querying
        # JUST that layer (not the cascade).
        per_layer_values: dict[str, dict[str, Any]] = {}
        # Factory layer.
        per_layer_values[LAYER_FACTORY] = _get_dotted(_DEFAULT_CONFIG, "bash") or {}
        # DB layers — read only that layer's contribution.
        from .config_store import _global_db_path, _project_db_path

        for layer, (db, scope_filter, scope_key) in (
            (LAYER_GLOBAL, (_global_db_path(), "global", "")),
            (LAYER_PROJECT, (_project_db_path(project_root), "project", "")),
            (LAYER_SESSION, (_project_db_path(project_root), "session", session_id or "")),
        ):
            if layer == LAYER_SESSION and not session_id:
                per_layer_values[layer] = {}
                continue
            try:
                layer_value, _, _ = resolver._db_layer(
                    db,
                    "bash",
                    layer,
                    scope_filter=scope_filter,
                    scope_key=scope_key,
                )
                per_layer_values[layer] = layer_value if isinstance(layer_value, dict) else {}
            except Exception:
                per_layer_values[layer] = {}

        out_commands: dict[str, object] = {}
        for cmd in sorted(commands):
            entry: dict[str, object] = {}
            effective_state = "bubble"
            effective_patterns: list[str] | None = None
            for layer in layers:
                state, patterns = _cmd_state(per_layer_values[layer], cmd)
                entry[layer] = state
                # Effective = highest-priority layer with explicit
                # allow/deny. Within layer, deny already won via
                # _cmd_state's order.
                if state != "bubble":
                    effective_state = state
                    effective_patterns = patterns
            entry["effective"] = effective_state
            entry["patterns"] = effective_patterns
            out_commands[cmd] = entry

        # Default policy (the floor when no command matches).
        default_value = resolved_value.get("default") if isinstance(resolved_value, dict) else None
        return {
            "commands": out_commands,
            "default": default_value or "block",
            "layers": list(layers),
        }

    def dashboard_config_entries(
        self,
        project_root: Path,
        session_id: str | None,
    ) -> list[dict[str, object]]:
        """Enriched catalog entries for the dashboard.

        Phase 5 (2026-05-02): each entry now carries dashboard_only,
        is_t0 (from description tag), effective_layer (which scope
        contributed the resolved value), and origin (per-leaf
        provenance).

        Phase 5b polish (2026-05-03): batch sqlite I/O. Prior version
        called resolver.get_layer_value() once per (setting x scope)
        AND layered.resolve() once per setting - that was 7-8 sqlite
        queries x ~100 catalog entries = 700+ queries per refresh,
        the dominant cost in the 6-10s dashboard refresh. Now we
        pre-fetch each scope full settings dict in 3 SELECTs
        (global/project/session) and resolve from in-memory dicts.
        Sub-key merge for namespace roots (only "bash" today) still
        delegates to layered.resolve since that requires LIKE-prefix
        SQL the batch primitive does not cover.
        """
        from .config_resolver import (
            LAYER_FACTORY,
            LAYER_GLOBAL,
            LAYER_PROJECT,
            LAYER_SESSION,
            LayeredConfigResolver,
        )
        from .config_store import ConfigStore

        entries: list[dict[str, object]] = []
        layered = LayeredConfigResolver()
        store = ConfigStore()
        # Batch pre-fetch: one SELECT per scope, dict lookup thereafter.
        try:
            global_values = store.get_all(project_root, scope="global", scope_key="")
        except Exception:
            global_values = {}
        try:
            project_values = store.get_all(project_root, scope="project", scope_key="")
        except Exception:
            project_values = {}
        session_values: dict[str, object] = {}
        if session_id:
            try:
                session_values = store.get_all(
                    project_root,
                    scope="session",
                    scope_key=session_id,
                )
            except Exception:
                session_values = {}
        scope_dicts = {
            "global": global_values,
            "project": project_values,
            "session": session_values,
        }
        # dev_mode only makes sense for the AIDOCS source project itself
        is_aidocs_source = (
            (project_root / "mcp" / "server" / "aidocs_mcp").is_dir() if project_root else False
        )
        for setting_path, metadata in sorted(SETTINGS_CATALOG.items()):
            if setting_path.startswith("dev.") and not is_aidocs_source:
                continue
            section, _, key = setting_path.rpartition(".")
            # scope_values: O(1) dict lookup per scope (was 1 sqlite per).
            scope_values: dict[str, object] = {}
            for scope in metadata["allowed_scopes"]:
                if scope == "factory":
                    scope_values[scope] = metadata["default"]
                else:
                    scope_values[scope] = scope_dicts.get(scope, {}).get(setting_path)
            # Effective value: leaf-fast-path vs namespace-root fallback.
            # If any scope has a sub-key under setting_path, the entry is
            # a namespace root and needs the resolver-LIKE merge. Else
            # walk the cascade in pure Python.
            prefix = setting_path + "."
            has_descendants = any(
                any(k.startswith(prefix) for k in d) for d in scope_dicts.values()
            )
            resolved_value = None
            origin: dict[str, str] = {}
            effective_layer: str | None = None
            if has_descendants:
                # Namespace root with sub-key contributions - slow path.
                try:
                    resolved = layered.resolve(
                        setting_path,
                        project_root,
                        session_id=session_id,
                    )
                    resolved_value = resolved.value
                    origin = resolved.origin
                except Exception:
                    pass
                for cand in (LAYER_SESSION, LAYER_PROJECT, LAYER_GLOBAL, LAYER_FACTORY):
                    if cand in origin.values():
                        effective_layer = cand
                        break
            else:
                # Leaf setting - fast path. Walk session -> project ->
                # global -> factory; first non-None wins.
                for layer_name, lookup in (
                    (LAYER_SESSION, session_values),
                    (LAYER_PROJECT, project_values),
                    (LAYER_GLOBAL, global_values),
                ):
                    if setting_path in lookup:
                        resolved_value = lookup[setting_path]
                        origin = {setting_path: layer_name}
                        effective_layer = layer_name
                        break
                if resolved_value is None:
                    factory = metadata["default"]
                    if factory is not None:
                        resolved_value = factory
                        origin = {setting_path: LAYER_FACTORY}
                        effective_layer = LAYER_FACTORY
            description = metadata["description"]
            entries.append(
                {
                    "path": setting_path,
                    "section": section,
                    "key": key,
                    "type": metadata["type"],
                    "description": description,
                    "default": metadata["default"],
                    "allowed_values": metadata["allowed_values"],
                    "value_descriptions": metadata["value_descriptions"],
                    "allowed_scopes": metadata["allowed_scopes"],
                    "agent_editable_scopes": metadata["agent_editable_scopes"],
                    "security_sensitive": metadata["security_sensitive"],
                    "dashboard_only": metadata.get("dashboard_only", False),
                    "requires_restart": metadata["requires_restart"],
                    "is_t0": "[T0" in description,
                    # editable=True when the dashboard CAN write this
                    # setting. Agent-editable scopes are one path;
                    # dashboard_only is the other (the dashboard is the
                    # authorized writer of last resort, e.g. dev.kill_
                    # switch). Without this OR the king couldn't flip
                    # kill_switch off after enabling it as a deadlock-
                    # recovery measure (2026-05-03 fix).
                    "editable": (
                        "project" in metadata["agent_editable_scopes"]
                        or metadata.get("dashboard_only", False)
                    ),
                    "current_value": (
                        resolved_value if resolved_value is not None else metadata["default"]
                    ),
                    "scope_values": scope_values,
                    "effective_layer": effective_layer,
                    "origin": origin,
                },
            )
        return entries

    def dashboard_token_usage(
        self,
        execution_summary: dict[str, object],
        recent_execution: list[dict[str, object]],
        session_breakdown: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        capability_counts: dict[str, int] = {}
        action_counts = {
            str(key): int(value)
            for key, value in (execution_summary.get("by_action_kind") or {}).items()
            if value is not None
        }
        tool_counts = {
            str(key): int(value)
            for key, value in (execution_summary.get("by_tool_name") or {}).items()
            if value is not None
        }
        for event in recent_execution:
            capability_name = str(event.get("capability_name") or "unknown")
            capability_counts[capability_name] = capability_counts.get(capability_name, 0) + 1
        top_capabilities = [
            {"label": label, "count": count}
            for label, count in sorted(
                capability_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:8]
        ]
        top_actions = [
            {"label": label, "count": count}
            for label, count in sorted(
                action_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:8]
        ]
        # Enrich action data with token estimates when available
        tokens_by_action = execution_summary.get("tokens_by_action_kind") or {}
        if tokens_by_action:
            for item in top_actions:
                ak_tokens = tokens_by_action.get(item["label"], {})
                item["tokens"] = ak_tokens.get("tokens_in", 0) + ak_tokens.get("tokens_out", 0)
        # Tool name breakdown — enrich with token data
        tokens_by_tool = execution_summary.get("tokens_by_tool_name") or {}
        tool_breakdown = []
        for label, count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))[:12]:
            tool_tok = tokens_by_tool.get(label, {})
            tokens = tool_tok.get("tokens_in", 0) + tool_tok.get("tokens_out", 0)
            tool_breakdown.append({"label": label, "count": count, "tokens": tokens})
        # Intent-category aggregation (search/read/edit/prompt)
        _TOOL_CATEGORIES: dict[str, str] = {
            "ai_find": "search",
            "ai_search": "search",
            "ai_text_search": "search",
            "ai_investigate": "search",
            "ai_trace": "search",
            "ai_bundle": "search",
            # ai_slop is the READ-ONLY scanner (post-split; ai_deslop_apply is
            # the mutating tool) → search category. Previously duplicated as
            # "edit" too, and the last-wins dict literal silently mis-categorized
            # its tokens as edit.
            "schema_query": "search",
            "ai_slop": "search",
            "ai_get_lines": "read",
            "ai_get_symbol_snippet": "read",
            "ai_get_symbol_info": "read",
            "ai_get_dependencies": "read",
            "ai_get_modules": "read",
            "ai_get_module_files": "read",
            "ai_edit_lines": "edit",
            "ai_batch_edit": "edit",
            "ai_replace": "edit",
            "ai_str_replace": "edit",
            "ai_insert_lines": "edit",
            "ai_create_file": "edit",
        }
        # Aggregate tokens by intent category
        tokens_by_tool = execution_summary.get("tokens_by_tool_name") or {}
        category_tokens: dict[str, int] = {}
        category_calls: dict[str, int] = {}
        for tool_name, count in tool_counts.items():
            cat = _TOOL_CATEGORIES.get(tool_name, "other")
            category_calls[cat] = category_calls.get(cat, 0) + count
            tool_tok = tokens_by_tool.get(tool_name, {})
            category_tokens[cat] = (
                category_tokens.get(cat, 0)
                + tool_tok.get("tokens_in", 0)
                + tool_tok.get("tokens_out", 0)
            )
        intent_breakdown = [
            {"label": label, "count": category_calls.get(label, 0), "tokens": tokens}
            for label, tokens in sorted(
                category_tokens.items(),
                key=lambda item: (-item[1], item[0]),
            )
            if tokens > 0 or category_calls.get(label, 0) > 0
        ]
        token_estimates = execution_summary.get("token_estimates") or {}
        tokens_in = int(token_estimates.get("tokens_in", 0))
        tokens_out = int(token_estimates.get("tokens_out", 0))
        # TOKEN-TRUTH (2026-05-26): surface the bounded/exact scope from
        # query_execution_summary so the dashboard renders the breakdown
        # accurately. The aggregate "tokens_in / tokens_out" is either
        # session-exact (when a session is selected) or a recent-window
        # view across all sessions; the session_breakdown is always the
        # recent-window project-wide view. The "reason" string discloses
        # the recent-window cap when one is in effect so users never
        # mistake a bounded count for an all-time total.
        token_estimates_scope = str(
            execution_summary.get("token_estimates_scope") or "session_exact",
        )
        session_breakdown_scope = str(
            execution_summary.get("session_breakdown_scope") or "all_sessions_recent",
        )
        breakdown_event_limit = execution_summary.get("breakdown_event_limit")
        _bounded_suffix = ""
        if token_estimates_scope == "all_sessions_recent" and breakdown_event_limit:
            _bounded_suffix = (
                f" · Across all sessions, recent {int(breakdown_event_limit):,} "
                "token-bearing events (not all-time)."
            )
        return {
            "available": tokens_in > 0 or tokens_out > 0,
            "reason": (
                f"Estimated from MCP tool call sizes (~4 chars/token). "
                f"Tokens in: ~{tokens_in:,} · Tokens out: ~{tokens_out:,} · Total: ~{tokens_in + tokens_out:,}"
                + _bounded_suffix
                if tokens_in > 0 or tokens_out > 0
                else "No token data yet. Token estimates will appear after MCP tool calls are recorded."
            ),
            "token_estimates": {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tokens_in_calls": int(token_estimates.get("tokens_in_calls", 0)),
                "tokens_out_calls": int(token_estimates.get("tokens_out_calls", 0)),
                "total": tokens_in + tokens_out,
            },
            # Scope labels — see TOKEN-TRUTH note above. Consumers (CLI,
            # web dashboard, partial manifests) MUST surface these when
            # rendering token figures so a bounded view is never read
            # as an all-time total.
            "token_estimates_scope": token_estimates_scope,
            "session_breakdown_scope": session_breakdown_scope,
            "breakdown_event_limit": breakdown_event_limit,
            "proxy_series": {
                "top_capabilities": top_capabilities,
                "top_action_kinds": top_actions,
                "event_breakdown": tool_breakdown,
                "intent_breakdown": intent_breakdown,
            },
            "session_breakdown": session_breakdown or [],
            "recent_event_count": len(recent_execution),
        }

    def dashboard_snapshot(
        self,
        project_root: Path,
        session_id: str | None = None,
        event_limit: int = 200,
        *,
        with_timings: bool = False,
    ) -> dict[str, object]:
        import time as _time

        _timings: dict[str, float] = {}

        def _t(name: str, fn):
            _t0 = _time.perf_counter()
            out = fn()
            _timings[name] = round((_time.perf_counter() - _t0) * 1000, 3)
            return out

        runtime = self.runtime
        # Hot path: cheap (DB-only) freshness, NOT the full sha256 walk that
        # _code_freshness runs (~2.6s on the live tree). The bullet stays
        # truthful (known-stale flag / unparsed / poll-window) and points at
        # ai_index_status for a deep on-disk drift count. Bootstrap still uses
        # freshness_mode="deep".
        # PERF (2026-05-26): list_sessions runs FIRST so repo_summary can
        # consume the count via same-call reuse (session_count kwarg) and
        # skip its own duplicate directory walk. Net effect: one
        # list_sessions per refresh instead of two.
        managed_mode = _t("managed_mode", lambda: runtime.hub.managed_mode.get_mode(project_root))
        sessions = _t("list_sessions", lambda: runtime.hub.sessions.list_sessions(project_root))
        repo_summary = _t(
            "repo_summary",
            lambda: runtime.repo_summary(
                project_root,
                freshness_mode="cheap",
                session_count=len(sessions),
            ),
        )
        selected_session_id = session_id
        if not selected_session_id and managed_mode.get("active"):
            selected_session_id = str(managed_mode.get("session_id") or "").strip() or None
        if not selected_session_id and len(sessions) == 1:
            selected_session_id = sessions[0].session_id

        # Per-session ownership truth (batched, one query): which sessions have
        # a session-scoped owner grant in RBAC. Surfaced as owner_granted so the
        # list can badge "owned" — honest (a grant exists), never a fabricated
        # "degraded" for legacy sessions that simply predate the owner model.
        owned_session_ids: set[str] = set()
        try:
            import sqlite3 as _sql

            from .rbac_store import RBACStore as _RB

            _rdb = _RB().db_path(project_root)
            if _rdb.is_file():
                with _sql.connect(str(_rdb)) as _c:
                    _rows = _c.execute(
                        "SELECT DISTINCT scope_id FROM rbac_user_roles WHERE scope_type = 'session'",
                    ).fetchall()
                owned_session_ids = {str(r[0]) for r in _rows if r and r[0]}
        except Exception:
            owned_session_ids = set()

        session_cards = [
            {
                "session_id": item.session_id,
                "title": item.title,
                "status": item.status,
                "owner": item.owner,
                "goal": item.goal,
                "last_updated": item.last_updated,
                "selected": item.session_id == selected_session_id,
                "managed": item.session_id == str(managed_mode.get("session_id") or "").strip(),
                "owner_granted": item.session_id in owned_session_ids,
            }
            for item in sessions
        ]

        selected_session: dict[str, object] | None = None
        # Execution queries are scoped strictly to the session the user picked.
        # "All Sessions" (session_id=None) returns unfiltered events. Managed mode
        # is a separate concern and must not influence this query.
        exec_session_filter = session_id
        execution_summary = _t(
            "execution_summary",
            lambda: runtime.hub.execution.query_execution_summary(
                project_root,
                session_id=exec_session_filter,
            ),
        )
        recent_execution = _t(
            "recent_execution",
            lambda: runtime.hub.execution.query_last_execution(
                project_root,
                session_id=exec_session_filter,
                limit=event_limit,
            ),
        )
        if selected_session_id:
            session = _t(
                "session_read",
                lambda: runtime.hub.sessions.read_session(project_root, selected_session_id),
            )
            context = _t(
                "session_context_read",
                lambda: runtime.hub.sessions.read_context(project_root, selected_session_id),
            )
            plan = _t(
                "session_plan_read",
                lambda: runtime.hub.sessions.read_plan(project_root, selected_session_id),
            )
            handoff_steps = _t(
                "session_handoff_read",
                lambda: runtime.hub.sessions.read_handoff_steps(project_root, selected_session_id),
            )
            compliance = _t(
                "compliance",
                lambda: self.session_compliance_summary(
                    project_root,
                    selected_session_id,
                    session=session,
                    plan=plan,
                    handoff_steps=handoff_steps,
                    execution_summary=execution_summary,
                ),
            )
            conductor: dict[str, object] | None = None
            conductor_error: str | None = None
            if getattr(plan, "lanes", None):
                try:
                    conductor = _t(
                        "conductor",
                        lambda: runtime.plan_conductor_status(project_root, selected_session_id),
                    )
                except Exception as exc:
                    conductor_error = str(exc)
            session_overview = _t(
                "session_overview",
                lambda: self.build_session_overview(
                    session_id=selected_session_id,
                    session_sections=session.sections,
                    context_sections=context.sections,
                    handoff_steps=handoff_steps,
                    compliance=compliance,
                ),
            )
            plan_overview = _t(
                "plan_overview",
                lambda: self.build_plan_overview(
                    session_id=selected_session_id,
                    plan_path=str(plan.path),
                    plan_sections=plan.sections,
                    has_lanes=bool(getattr(plan, "lanes", None)),
                ),
            )
            selected_session = {
                "session": {
                    "session_id": session.session_id,
                    "path": str(session.path),
                    "sections": session.sections,
                },
                "context": {"path": str(context.path), "sections": context.sections},
                "overview": session_overview,
                "plan_overview": plan_overview,
                "compliance": compliance,
                "handoff_steps": handoff_steps,
                "conductor": conductor,
                "conductor_error": conductor_error,
            }

        effective_config = _t(
            "effective_config",
            lambda: runtime.effective_config(project_root, session_id=selected_session_id),
        )
        project_overview = _t(
            "project_overview",
            lambda: self.build_project_overview(
                project_root,
                repo_summary=repo_summary,
                selected_session_id=selected_session_id,
            ),
        )
        # SEC-005 (2026-04-23): surface degraded_state for the selected
        # session so the dashboard top bar + right-panel strip render
        # the red badge without a second MCP round-trip. Clean state
        # when no session is selected (nothing to be degraded about).
        degraded_state: dict[str, object] = {
            "degraded": False,
            "reason": "",
            "degraded_at": "",
            "last_failure_event_id": "",
        }
        if selected_session_id:
            try:
                degraded_state = runtime.hub.query_gate.get_degraded_state(
                    project_root,
                    selected_session_id,
                )
            except Exception:
                pass
        freezes = _t("freezes", lambda: self._dashboard_freezes(project_root))
        token_usage = _t(
            "token_usage",
            lambda: self.dashboard_token_usage(
                execution_summary,
                recent_execution,
                session_breakdown=execution_summary.get("session_breakdown"),
            ),
        )
        config_entries = _t(
            "config_entries",
            lambda: self.dashboard_config_entries(project_root, selected_session_id),
        )
        config_bash_policy = _t(
            "config_bash_policy",
            lambda: self.dashboard_bash_policy(project_root, selected_session_id),
        )
        config_rbac = _t("config_rbac", lambda: self.dashboard_rbac(project_root))
        _recent_partial = len(recent_execution) >= event_limit
        # Explicit partiality manifest for the first-paint scoping: which sections
        # are a lighter/cheaper view than the deepest available. All sections NOT
        # listed here are complete. Truth-preserving: nothing here is fabricated —
        # it tells the consumer where to ask for more.
        partial = {
            "repo_freshness_check": str(repo_summary.get("freshness_check") or "cheap"),
            "repo_freshness_note": (
                "dashboard uses the cheap poll-based index-freshness signal "
                "(known-stale flag / unparsed / poll-window); a deep on-disk "
                "sha256 drift count is available via ai_index_status"
            ),
            "execution_recent_partial": _recent_partial,
            "execution_recent_limit": event_limit,
        }
        snapshot: dict[str, object] = {
            "project": project_overview,
            "managed_mode": managed_mode,
            "sessions": session_cards,
            "selected_session_id": selected_session_id,
            "selected_session": selected_session,
            "degraded_state": degraded_state,
            # Canonical approval cards for every pending freeze/escalation —
            # the dashboard freeze surface renders these (compact rows +
            # verbose detail), never a parallel free-form format.
            "freezes": freezes,
            "execution": {
                "summary": execution_summary,
                "recent": recent_execution,
                # The recent feed is the most-recent N events, capped at
                # event_limit — it is a PARTIAL view, never the full history.
                # recent_partial=True ⇒ the cap was hit and older events exist;
                # request them with a larger event_limit.
                "recent_limit": event_limit,
                "recent_partial": _recent_partial,
            },
            "token_usage": token_usage,
            "config": {
                "project_config_path": str(project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"),
                "session_config_path": "",
                "effective": effective_config,
                "entries": config_entries,
                "bash_policy": config_bash_policy,
                "rbac": config_rbac,
                "available_edit_modes": available_config_edit_modes("release"),
            },
            "partial": partial,
        }
        if with_timings:
            # Additive, opt-in diagnostic field (default off → response byte-
            # identical for existing callers). Never affects any section's truth.
            snapshot["_section_timings_ms"] = dict(sorted(_timings.items(), key=lambda kv: -kv[1]))
        return snapshot

    def _dashboard_freezes(
        self,
        project_root: Path,
    ) -> list[dict[str, object]]:
        """Pending escalations rendered as canonical approval cards
        (compact row + verbose detail + structured fields). One source of
        truth shared with the live freeze prompt — no parallel format.
        """
        from .escalation_store import EscalationStore
        from .freeze_service import (
            _card_state,
            card_from_escalation,
            render_approval_card,
        )

        rows: list[dict[str, object]] = []
        try:
            pending = EscalationStore().list_pending(project_root)
        except Exception:
            return rows
        for req in pending:
            try:
                card = card_from_escalation(req)
                rows.append(
                    {
                        "request_id": card.request_id,
                        "compact": render_approval_card(card, "compact"),
                        "verbose": render_approval_card(card, "verbose"),
                        "approval_card": _card_state(
                            card,
                            "awaiting_self_approve",
                        )["approval_card"],
                    },
                )
            except Exception:
                continue
        return rows

    def session_compliance_summary(
        self,
        project_root: Path,
        session_id: str,
        *,
        session: object | None = None,
        plan: object | None = None,
        handoff_steps: list[dict[str, object]] | None = None,
        execution_summary: dict[str, object] | None = None,
    ) -> dict[str, object]:
        # PERF (2026-05-26): kw-only prefetch params let dashboard_snapshot reuse
        # the values it already computed in the same call instead of re-reading
        # session/plan/handoff files and re-running query_execution_summary.
        # Other callers (resume bundle, orchestrator, session tools, the
        # runtime_service wrapper) pass nothing and get identical behavior via
        # the lazy fallback fetches below — no truth change.
        runtime = self.runtime
        if session is None:
            session = runtime.hub.sessions.read_session(project_root, session_id)
        if plan is None:
            plan = runtime.hub.sessions.read_plan(project_root, session_id)
        if handoff_steps is None:
            handoff_steps = runtime.hub.sessions.read_handoff_steps(project_root, session_id)
        journal = runtime.hub.sessions.read_journal(project_root, session_id, last_n=20)
        if execution_summary is None:
            execution_summary = runtime.hub.execution.query_execution_summary(
                project_root,
                session_id=session_id,
            )

        status_values = runtime._clean_bullets(session.sections.get("Status", []))
        task_open = any(value == "active" for value in status_values)
        partial_goals = runtime._clean_bullets(plan.sections.get("Partial Goals", []))
        upcoming = runtime._clean_bullets(session.sections.get("Upcoming", []))
        actionable_steps = [
            step
            for step in handoff_steps
            if str(step.get("status")) in {"open", "reset", "failed", "stale"}
        ]

        latest_journal_ts = None
        if journal:
            try:
                latest_journal_ts = max(
                    datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M")
                    for entry in journal
                    if entry.get("timestamp")
                )
            except Exception:
                latest_journal_ts = None

        journal_coverage = runtime.hub.execution.session_journal_coverage_summary(
            project_root,
            session_id,
            latest_journal_at=latest_journal_ts,
        )
        latest_work_ts = None
        latest_work_text = str(journal_coverage.get("latest_meaningful_event_at") or "").strip()
        if latest_work_text:
            try:
                latest_work_ts = datetime.strptime(latest_work_text, "%Y-%m-%d %H:%M:%S")
            except Exception:
                latest_work_ts = None

        logging_debt = bool(journal_coverage.get("logging_debt"))
        summary = {
            "task_open": task_open,
            "logging_debt": logging_debt,
            "actionable_step_count": len(actionable_steps),
            "partial_goal_count": len(partial_goals),
            "upcoming_count": len(upcoming),
            "execution_events": int(execution_summary.get("total_events", 0)),
            "latest_work_event_at": latest_work_ts.strftime("%Y-%m-%d %H:%M:%S")
            if latest_work_ts
            else None,
            "latest_journal_at": latest_journal_ts.strftime("%Y-%m-%d %H:%M")
            if latest_journal_ts
            else None,
            "journal_coverage": journal_coverage,
            "warnings": [],
        }
        warnings: list[str] = []
        if task_open:
            warnings.append("task remains open")
        if logging_debt:
            warnings.append("work occurred after the latest journal entry")
        if actionable_steps:
            warnings.append(f"{len(actionable_steps)} actionable handoff steps remain")
        summary["warnings"] = warnings
        return summary

    def build_project_overview(
        self,
        project_root: Path,
        *,
        repo_summary: dict[str, object] | None,
        selected_session_id: str | None = None,
        stage: str | None = None,
        ready: bool | None = None,
    ) -> dict[str, object]:
        runtime = self.runtime
        summary = (
            repo_summary if isinstance(repo_summary, dict) else runtime.repo_summary(project_root)
        )
        return {
            "project_name": summary.get("project_name") or project_root.name,
            "project_root": summary.get("project_root") or str(project_root),
            "code_file_count": int(summary.get("code_files") or 0),
            "module_count": int(summary.get("modules") or 0),
            "schema_entity_count": int(summary.get("schema_entities") or 0),
            "session_count": int(summary.get("sessions") or 0),
            "selected_session_id": selected_session_id,
            "artifact_catalog": self.project_artifact_catalog(project_root),
            "stage": stage,
            "ready": ready,
        }

    def project_artifact_catalog(self, project_root: Path) -> dict[str, dict[str, object]]:
        runtime = self.runtime
        return {
            "skill_provider_registry": {
                "path": str(runtime.hub.skills.external_provider_registry_path(project_root)),
                "classification": "config",
                "legacy_paths": [
                    str(runtime.hub.skills.legacy_external_provider_registry_path(project_root)),
                ],
            },
            "aidocs_managed": {
                "path": str(runtime.hub.managed_mode.config_path(project_root)),
                "classification": "runtime_binding_state",
            },
            "workflow_actions": {
                "path": str(runtime.hub.workflow.config_path(project_root)),
                "classification": "compiled_runtime_artifact",
            },
        }

    def result_artifacts_root(self, project_root: Path, session_id: str | None = None) -> Path:
        if session_id:
            return self.runtime.hub.sessions.session_path(project_root, session_id) / "artifacts"
        return project_root / ".MEMORY" / ".runtime" / "artifacts"

    def write_result_artifact(
        self,
        project_root: Path,
        *,
        payload: object,
        artifact_name: str,
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Store a tool result artifact in SQLite instead of JSON files."""
        slug = re.sub(r"[^a-z0-9_-]+", "-", artifact_name.strip().lower()).strip("-")
        if not slug:
            slug = "result"
        artifact_id = f"{slug}-{uuid4().hex[:12]}"
        serialized = json.dumps(payload, indent=2, sort_keys=True, default=str)
        size_bytes = len(serialized.encode("utf-8"))

        # Store in SQLite
        try:
            import sqlite3

            db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """CREATE TABLE IF NOT EXISTS result_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    artifact_name TEXT NOT NULL,
                    session_id TEXT,
                    payload TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )""",
            )
            from datetime import datetime

            conn.execute(
                "INSERT INTO result_artifacts (artifact_id, artifact_name, session_id, payload, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    artifact_name,
                    session_id,
                    serialized,
                    size_bytes,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            # Fallback: write to file if SQLite fails
            artifacts_root = self.result_artifacts_root(project_root, session_id)
            target_dir = artifacts_root / "mcp-results"
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"{artifact_id}.json"
            path.write_text(serialized + "\n", encoding="utf-8")

        return {
            "artifact_id": artifact_id,
            "artifact_path": f".MEMORY/.index/aidocs.sqlite3#{artifact_id}",
            "artifact_kind": "json",
            "size_bytes": size_bytes,
            "session_id": session_id,
        }

    def read_result_artifact(
        self,
        project_root: Path,
        artifact_id: str,
    ) -> dict[str, object] | None:
        """Read a stored artifact from SQLite."""
        try:
            import sqlite3

            db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
            if not db_path.is_file():
                return None
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM result_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            conn.close()
            if row is None:
                return None
            return {
                "artifact_id": row["artifact_id"],
                "artifact_name": row["artifact_name"],
                "session_id": row["session_id"],
                "payload": json.loads(row["payload"]),
                "size_bytes": row["size_bytes"],
                "created_at": row["created_at"],
            }
        except Exception:
            return None

    def build_artifact_backed_result(
        self,
        project_root: Path,
        *,
        inline_summary: str,
        payload: object,
        artifact_name: str,
        session_id: str | None = None,
        structured_summary: dict[str, object] | None = None,
    ) -> dict[str, object]:
        artifact = self.write_result_artifact(
            project_root,
            payload=payload,
            artifact_name=artifact_name,
            session_id=session_id,
        )
        structured_content: dict[str, object] = {
            **(structured_summary or {}),
            "artifact": artifact,
        }
        return {
            "content": (
                f"{inline_summary}\nFull payload saved to artifact: `{artifact['artifact_path']}`."
            ),
            "structuredContent": structured_content,
        }

    def build_session_overview(
        self,
        *,
        session_id: str | None,
        session_sections: dict[str, list[str]] | None,
        context_sections: dict[str, list[str]] | None,
        handoff_steps: list[dict[str, object]] | None,
        compliance: dict[str, object] | None,
    ) -> dict[str, object]:
        runtime = self.runtime
        session_sections = session_sections if isinstance(session_sections, dict) else {}
        context_sections = context_sections if isinstance(context_sections, dict) else {}
        titles = runtime._clean_bullets(session_sections.get("Title", []))
        statuses = runtime._clean_bullets(session_sections.get("Status", []))
        goals = runtime._clean_bullets(session_sections.get("Goal", []))
        owners = runtime._clean_bullets(session_sections.get("Owner", []))
        relevant_files = runtime._clean_bullets(context_sections.get("Relevant Files", []))
        actionable_handoff_step_count = len(
            [
                step
                for step in (handoff_steps or [])
                if str(step.get("status") or "") in {"open", "reset", "failed", "stale"}
            ],
        )
        journal_coverage = (
            (compliance or {}).get("journal_coverage")
            if isinstance((compliance or {}).get("journal_coverage"), dict)
            else {}
        )
        return {
            "session_id": session_id,
            "title": titles[0] if titles else None,
            "status": statuses[0] if statuses else None,
            "goal": goals[0] if goals else None,
            "owner": owners[0] if owners else None,
            "relevant_file_count": len(relevant_files),
            "actionable_handoff_step_count": actionable_handoff_step_count,
            "logging_debt": bool((compliance or {}).get("logging_debt")),
            "meaningful_event_count_since_journal": int(
                journal_coverage.get("meaningful_event_count_since_journal") or 0,
            ),
            "latest_meaningful_event_at": journal_coverage.get("latest_meaningful_event_at"),
        }

    def build_skills_overview(
        self,
        *,
        session_id: str | None,
        selected_skills: dict[str, object] | None,
        active_skills: list[str] | None,
        imported_skill_state: dict[str, object] | None,
        skill_trigger_state: dict[str, object] | None,
    ) -> dict[str, object]:
        selected = [str(item) for item in (selected_skills or {}).get("selected_skills", [])]
        active = [str(item) for item in (active_skills or [])]
        override_modes: dict[str, str] = {}
        triggered = (
            (skill_trigger_state or {}).get("triggered")
            if isinstance(skill_trigger_state, dict)
            else []
        )
        runtime_owned_capabilities = [
            item
            for item in ((skill_trigger_state or {}).get("runtime_owned_capabilities") or [])
            if isinstance(item, dict)
        ]
        if isinstance(triggered, list):
            for item in triggered:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("runtime_owned_capability"), dict):
                    continue
                skill_id = str(item.get("skill_id") or "")
                override_mode = str(item.get("override_mode") or "").strip()
                if skill_id and override_mode:
                    override_modes[skill_id] = override_mode
        return {
            "session_id": session_id,
            "selected_skills": selected,
            "selected_skill_count": len(selected),
            "active_skills": active,
            "active_skill_count": len(active),
            "runtime_owned_capabilities": runtime_owned_capabilities,
            "runtime_owned_capability_count": len(runtime_owned_capabilities),
            "provider_state": (imported_skill_state or {}).get("provider_state"),
            "provider_states": (imported_skill_state or {}).get("provider_states") or {},
            "override_modes": override_modes,
        }

    def build_default_plan_overview(
        self,
        *,
        session_id: str,
        end_goal: str | None = None,
    ) -> dict[str, object]:
        return {
            "session_id": session_id,
            "plan_path": None,
            "progress": "0/0",
            "completed_count": 0,
            "incomplete_count": 0,
            "next_step": None,
            "purpose": None,
            "end_goal": end_goal,
            "has_lanes": False,
        }

    def build_plan_overview(
        self,
        *,
        session_id: str,
        plan_path: str | None,
        plan_sections: dict[str, list[str]] | None,
        has_lanes: bool,
    ) -> dict[str, object]:
        runtime = self.runtime
        sections = plan_sections if isinstance(plan_sections, dict) else {}
        completed: list[str] = []
        incomplete: list[str] = []
        for lines in sections.values():
            for line in lines:
                parsed = runtime._parse_plan_checkbox_line(line)
                if not parsed:
                    continue
                text = str(parsed["text"])
                if parsed["status"] == "completed":
                    completed.append(text)
                else:
                    incomplete.append(text)
        total = len(completed) + len(incomplete)
        progress = f"{len(completed)}/{total}" if total > 0 else "0/0"
        end_goals = runtime._clean_bullets(sections.get("End Goal", []))
        purposes = runtime._clean_bullets(sections.get("Purpose", []))
        return {
            "session_id": session_id,
            "plan_path": plan_path,
            "progress": progress,
            "completed_count": len(completed),
            "incomplete_count": len(incomplete),
            "next_step": incomplete[0] if incomplete else None,
            "purpose": purposes[0] if purposes else None,
            "end_goal": end_goals[0] if end_goals else None,
            "has_lanes": has_lanes,
        }
