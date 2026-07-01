from __future__ import annotations

from pathlib import Path
from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as


def _gate_cross_project(target: str, operation: str) -> dict[str, Any] | None:
    """Authorize a cross-project tool against the bound (conductor) project.

    Returns a refusal dict when the target project may not be reached, or
    None when allowed (same-project, or commissioned + approved relation +
    permission per project_authority). Plain project LISTING is exempt —
    only register / list-sessions / handoff / spawn carry this gate.
    """
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
        )
        from .project_authority import require_cross_project

        conductor_root = resolve_project_root()
        d = require_cross_project(
            conductor_root,
            Path(target),
            operation=operation,
            host_session_id=current_calling_host_session_id(),
        )
    except Exception as exc:  # fail closed
        return {
            "ok": False,
            "error": f"cross-project gate error: {exc}",
            "blocked_by": "gate_error",
        }
    if d.get("ok"):
        return None
    return {
        "ok": False,
        "error": d.get("reason", "cross-project refused"),
        "blocked_by": d.get("blocked_by"),
    }


def _gate_cross_project_session(
    target: str,
    target_session_id: str,
    operation: str,
) -> dict[str, Any] | None:
    """Authorize a cross-project SESSION write/bind: source authority +
    approved relation + TARGET project authority + TARGET session
    membership + TARGET session RBAC. Source authority alone never
    suffices. Returns a refusal dict or None when allowed.
    """
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
        )
        from .project_authority import require_cross_project_session

        conductor_root = resolve_project_root()
        d = require_cross_project_session(
            conductor_root,
            Path(target),
            target_session_id,
            operation=operation,
            host_session_id=current_calling_host_session_id(),
        )
    except Exception as exc:  # fail closed
        return {
            "ok": False,
            "error": f"cross-project gate error: {exc}",
            "blocked_by": "gate_error",
        }
    if d.get("ok"):
        return None
    return {
        "ok": False,
        "error": d.get("reason", "cross-project refused"),
        "blocked_by": d.get("blocked_by"),
        "stage": d.get("stage"),
    }


def _grant_value_matches(grant_value: object, explicit_value: object) -> bool:
    """Soft match between operator-grant phrase value and agent payload.

    "turn on <key>" → grant_value=True. Boolean schemas: agent passes
    True, exact match. Non-bool schemas: agent passes a schema-valid
    payload (e.g. ["*"] for bash.allow.<subcommand>). True semantically
    means "on" — accept the agent's truthy concrete payload as the
    schema's canonical "on" interpretation.

    "turn off <key>" → grant_value=False. Symmetric: accept agent's
    falsy/empty payload as the "off" interpretation.

    "set <key> to <val>" → grant_value=<val>. Strict equality required.

    "__toggle__" sentinel is resolved before this check fires.
    """
    if explicit_value == grant_value:
        return True
    if grant_value is True:
        return bool(explicit_value)
    if grant_value is False:
        return not bool(explicit_value)
    return False


def register_project_admin_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_sync: Any,
    resolve_related_root: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Config Setting",
        },
        meta={"anthropic/searchHint": True},
    )
    def config_get(
        key: str,
        session_id: str = "",
        default: Any = None,
    ) -> dict[str, Any]:
        """Read a setting from the AIDOCS SQLite config store with the full
        scope cascade (session > project > global > code default).

        Use this instead of reading aidocs.sqlite3 via ai_read_sqlite —
        it respects the scope cascade and understands dotted keys like
        `bash.deny` or `conductor.backend`.
        """
        from .config import get_setting

        root = resolve_project_root()
        value = get_setting(
            key,
            project_root=root,
            session_id=session_id or None,
            default=default,
        )
        return {"key": key, "value": value}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Set Config Setting",
        },
    )
    def config_set(
        key: str,
        value: Any = None,
        scope: str = "project",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Write a config setting. User-intent gated (2026-04-21).

        The agent CANNOT flip settings unilaterally — the current
        UserPromptSubmit prompt must carry a grant phrase naming `key`:

          * "turn on <key>"      / "enable <key>"
          * "turn off <key>"     / "disable <key>"
          * "flip <key>"         / "toggle <key>"
          * "set <key> to <val>" / "set <key>=<val>"

        Security-sensitive keys (flagged in config_schema) additionally
        require the grant phrase to include the word "explicitly" so a
        near-miss conversational reference can't flip security posture.

        On success, returns {ok, key, scope, new_value, previous_value}.
        On refusal, returns {ok: False, error, granted_keys} so the agent
        can surface the needed phrase to the operator.
        """
        from .config import get_setting
        from .config_schema import SETTINGS_CATALOG as _SETTINGS
        from .config_store import ConfigStore

        root = resolve_project_root()

        k = (key or "").strip()
        if not k:
            return {"ok": False, "error": "key is required"}

        # Tier-RBAC (2026-04-21): human permission check precedes
        # tier-0 dashboard gate. Even with the dashboard toggle on,
        # the acting human user needs admin.manage_config at project
        # scope. Refusal emits rbac_denied event automatically.
        _rbac = hub.require_permission(
            root,
            "admin.manage_config",
            scope_type="project",
            scope_id=str(root).replace("\\", "/"),
            tool_name="config_set",
            extra_payload={"key": k, "scope": scope},
        )
        if not _rbac["ok"]:
            return _rbac

        # Tier 0: global dashboard gate. `security.allow_config_edit` is the
        # operator-owned switch; when OFF (default), config_set is
        # categorically refused regardless of user intent. This keeps
        # dashboard posture authoritative — per-prompt grants are an
        # agent-permission mechanism under an already-enabled gate,
        # not a bypass.
        _allow = bool(
            get_setting(
                "security.allow_config_edit",
                project_root=root,
                default=False,
            ),
        )
        if not _allow:
            return {
                "ok": False,
                "error": (
                    "config_set refused: config edits are disabled "
                    "via the dashboard (`security.allow_config_edit=false`). "
                    "Flip the dashboard toggle to enable the config_set "
                    "tool, then the operator's per-prompt grant covers "
                    "individual writes."
                ),
                "blocked_by": "dashboard_gate",
            }

        # Grants live in sqlite only. Hook populates on UserPromptSubmit;
        # this tool reads on every call. No per-process cache, no
        # protected_file_runtime fallback — one source of truth.
        grants: dict[str, object] = {}
        try:
            managed_gr = hub.managed_mode.get_mode(root)
            sid_gr = str(managed_gr.get("session_id") or "").strip()
            if sid_gr:
                grants = hub.query_gate.get_config_grants(root, sid_gr)
        except Exception:
            grants = {}
        if k not in grants:
            return {
                "ok": False,
                "error": (
                    f"config_set refused: no user-intent grant for "
                    f"'{k}' in the current prompt. Ask the operator to "
                    f"type e.g. 'turn on {k}' or 'set {k} to <value>' "
                    f"before retrying."
                ),
                "granted_keys": sorted(grants.keys()),
            }

        # [DASHBOARD-ONLY SETTINGS] Any setting flagged
        # dashboard_only=True in the catalog is categorically refused
        # by the agent-facing config_set tool, regardless of flavor,
        # user-intent grants, or RBAC role. The dashboard UI writes
        # sqlite directly via its own trust surface (operator is
        # physically present, clicks a button with a confirm modal);
        # agents never reach that state. Prevents social engineering
        # — an agent can't talk an operator into typing a grant
        # phrase that unlocks its own guardrails.
        setting_meta = _SETTINGS.get(k)
        # [DEPRECATED / RESERVED KEYS] Hidden from normal editing — surface
        # the migration path instead of silently flipping a dead alias.
        _dep = setting_meta.get("deprecated") if setting_meta else ""
        if _dep:
            return {
                "ok": False,
                "error": (f"config_set refused: '{k}' is deprecated. {_dep}"),
                "blocked_by": "deprecated_setting",
                "setting": k,
                "migration": _dep,
            }
        # [SERVICE-MANAGED SETTINGS] Keys owned by a high-level service
        # (e.g. Governed Bash) are not editable through ANY settings
        # surface — not here, not the dashboard settings editor. They are
        # written ONLY by their owning service, which validates and applies
        # the whole interdependent group atomically (and rolls back on
        # partial failure). Flipping one key at a time would create a
        # half-enabled, unverified posture. Redirect to the owning service.
        _svc = setting_meta.get("service_managed") if setting_meta else ""
        if _svc == "governed_bash":
            return {
                "ok": False,
                "error": (
                    f"config_set refused: '{k}' is managed by Governed "
                    f"Bash and cannot be edited as an individual setting. "
                    f"Use the one-switch operator surface instead: "
                    f"`aidocs governed-bash-enable [--provider-path ...] "
                    f"[--hash-pin ...] [--require-os-signature]` to enable, "
                    f"or `aidocs governed-bash-disable` to turn it off. "
                    f"Status: `aidocs governed-bash-status`. These flip "
                    f"every interdependent flag atomically so the posture "
                    f"can never sit half-enabled."
                ),
                "blocked_by": "service_managed_setting",
                "setting": k,
                "service": "governed_bash",
            }
        if _svc:
            return {
                "ok": False,
                "error": (
                    f"config_set refused: '{k}' is managed by the "
                    f"'{_svc}' service and is not individually editable."
                ),
                "blocked_by": "service_managed_setting",
                "setting": k,
                "service": _svc,
            }
        if setting_meta and setting_meta.get("dashboard_only"):
            return {
                "ok": False,
                "error": (
                    f"config_set refused: '{k}' is dashboard-only. "
                    f"The agent-facing config_set tool categorically "
                    f"cannot write this setting — it controls "
                    f"AIDOCS guardrails that must be toggled only "
                    f"via the dashboard UI."
                ),
                "blocked_by": "dashboard_only_setting",
                "setting": k,
            }
        # [POSITIVE-PROOF NORMAL-WRITABLE GATE] Beyond the specific
        # refusals above, the agent surface persists a key ONLY when it is
        # positively proven normal-editable (catalog member; not
        # service-managed / deprecated / dashboard-only / hidden-owned).
        # Fail CLOSED: any validator/import uncertainty refuses the write
        # (a hidden-owned diagnostic flag, an unknown key, or a broken
        # operator_surface import can never slip through as a normal save).
        try:
            from .operator_surface import (
                editability as _editability,
            )
            from .operator_surface import (
                is_normal_editable as _is_normal_editable,
            )

            _normal_ok = _is_normal_editable(k)
        except Exception:
            _normal_ok = False  # fail closed on uncertainty
            _editability = None  # type: ignore[assignment]
        if not _normal_ok:
            _reason = "not_normal_editable"
            if _editability is not None:
                try:
                    _reason = str(_editability(k).get("reason") or _reason)
                except Exception:
                    pass
            return {
                "ok": False,
                "error": (
                    f"config_set refused: '{k}' is not normally editable "
                    f"({_reason}). Use the Advanced Raw expert edit or the "
                    f"owning profile action."
                ),
                "blocked_by": "not_normal_editable",
                "setting": k,
                "reason": _reason,
            }

        setting = _SETTINGS.get(k)
        is_sensitive = bool(setting and getattr(setting, "security_sensitive", False))
        # Note: grant phrases must match the full key (e.g.
        # "security.block_user_credentials", not just "block_user_
        # credentials"), which already makes near-miss triggers
        # unlikely. A future strict-variant could require
        # "explicitly turn on <key>" for security_sensitive settings
        # if operator-only semantics prove insufficient.

        # Resolve toggle sentinel against the current effective value.
        grant_value = grants[k]
        if grant_value == "__toggle__":
            current = get_setting(
                k,
                project_root=root,
                session_id=session_id or None,
                default=None,
            )
            grant_value = not bool(current)

        if value is not None and not _grant_value_matches(grant_value, value):
            return {
                "ok": False,
                "error": (
                    f"config_set refused: explicit value={value!r} does "
                    f"not match the granted intent={grant_value!r} in the "
                    f"current prompt. Pass value={grant_value!r} or ask "
                    f"the operator for a fresh grant. Note: 'turn on' "
                    f"accepts any truthy schema-valid payload; 'turn off' "
                    f"accepts any falsy/empty payload; 'set X to Y' "
                    f"requires exact equality."
                ),
            }

        effective_value = value if value is not None else grant_value

        # Validate scope + snapshot previous for auditability.
        scope_s = (scope or "project").strip().lower()
        if scope_s not in ("global", "project", "session"):
            return {"ok": False, "error": f"invalid scope {scope!r}"}
        if scope_s == "session" and not session_id:
            return {"ok": False, "error": "session scope requires session_id"}

        previous = get_setting(
            k,
            project_root=root,
            session_id=session_id or None,
            default=None,
        )

        try:
            ConfigStore().set(
                root,
                k,
                effective_value,
                scope=scope_s,
                scope_key=session_id if scope_s == "session" else "",
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"config_set write failed: {exc}"}

        # Audit trail
        try:
            managed = hub.managed_mode.get_mode(root)
            sid = str(managed.get("session_id") or "").strip() or None
            # Redact sensitive values in the audit record.
            audit_prev = "[REDACTED]" if is_sensitive else previous
            audit_next = "[REDACTED]" if is_sensitive else effective_value
            hub.execution.record_event(
                root,
                event_kind="config_set",
                source_kind="mcp_call",
                session_id=sid,
                capability_name="config_set",
                action_kind="config_write",
                target_entity=k,
                status="applied",
                payload={
                    "key": k,
                    "scope": scope_s,
                    "previous": audit_prev,
                    "new": audit_next,
                    "security_sensitive": is_sensitive,
                },
            )
        except Exception:
            pass

        return {
            "ok": True,
            "key": k,
            "scope": scope_s,
            "new_value": effective_value if not is_sensitive else "[REDACTED]",
            "previous_value": previous if not is_sensitive else "[REDACTED]",
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Procedure Index Status",
        },
    )
    def procedure_index_status() -> dict[str, Any]:
        """Return current procedure-definition index status for a project."""
        return hub.procedures.procedure_status(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Procedure Definitions",
        },
        meta={"anthropic/searchHint": True},
    )
    def procedure_definitions_get(query: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Return indexed procedure definitions, optionally filtered by query."""
        root = resolve_project_root()
        result = hub.procedures.find_procedures(root, query=query, limit=limit)
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} procedure definition(s).",
            payload=result,
            artifact_name="procedure-definitions",
            structured_summary={
                "count": len(result),
                "query": query,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Procedure Capability Link Status",
        },
    )
    def procedure_capability_link_status() -> dict[str, Any]:
        """Return current procedure-to-capability link status for a project."""
        return hub.procedure_links.link_status(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Procedure Capability Links",
        },
    )
    def procedure_capability_links_get(
        procedure_id: str | None = None,
        unresolved_only: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return indexed procedure-to-capability links, optionally filtered by procedure or unresolved status."""
        root = resolve_project_root()
        result = hub.procedure_links.list_links(
            root,
            procedure_id=procedure_id,
            unresolved_only=unresolved_only,
            limit=limit,
        )
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} procedure-capability link(s).",
            payload=result,
            artifact_name="procedure-capability-links",
            structured_summary={
                "count": len(result),
                "procedure_id": procedure_id,
                "unresolved_only": unresolved_only,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Index Status",
        },
    )
    def execution_index_status() -> dict[str, Any]:
        """Return current execution-evidence index status for a project."""
        return hub.execution.execution_status(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Denial Tier Stats",
        },
    )
    def denial_tier_stats(
        session_id: str | None = None,
        limit_per_tier: int = 1000,
    ) -> dict[str, Any]:
        """Aggregate gate-denial counts per tier with recent timestamps.

        Bonus telemetry surfaces: which gate fires most? Heuristic
        judge dominating means the bash policy may be too loose; tier-0
        dominating means agents are still reaching for raw tools.
        Returns by_tier=dict and total_denials. Optionally scoped to
        a specific session.
        """
        return hub.execution.denial_tier_stats(
            resolve_project_root(),
            session_id=session_id,
            limit_per_tier=int(limit_per_tier),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Execution Runs",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_runs_get(session_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Return indexed execution runs, optionally filtered by session."""
        root = resolve_project_root()
        result = hub.execution.list_runs(root, session_id=session_id, limit=limit)
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} execution run(s).",
            payload=result,
            artifact_name="execution-runs",
            session_id=session_id,
            structured_summary={
                "count": len(result),
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Execution Events",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_events_get(
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return indexed execution events, optionally filtered by query/session."""
        root = resolve_project_root()
        result = hub.execution.list_events(root, query=query, session_id=session_id, limit=limit)
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} execution event(s).",
            payload=result,
            artifact_name="execution-events",
            session_id=session_id,
            structured_summary={
                "count": len(result),
                "query": query,
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Record Execution Run",
        },
    )
    def execution_run_record(
        run_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        status: str = "started",
        ad_hoc: bool = True,
        target_entity: str | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        """Record or update an execution run for observed work."""
        resolved = hub.execution.record_run(
            resolve_project_root(),
            run_kind=run_kind,
            source_kind=source_kind,
            session_id=session_id,
            procedure_id=procedure_id,
            capability_name=capability_name,
            status=status,
            ad_hoc=ad_hoc,
            target_entity=target_entity,
            metadata=metadata,
            run_id=run_id,
            completed_at=completed_at,
        )
        return {"run_id": resolved}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Record Execution Event",
        },
    )
    def execution_event_record(
        event_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        action_kind: str | None = None,
        target_entity: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Record one execution event for observed work."""
        resolved = hub.execution.record_event(
            resolve_project_root(),
            event_kind=event_kind,
            source_kind=source_kind,
            session_id=session_id,
            procedure_id=procedure_id,
            capability_name=capability_name,
            action_kind=action_kind,
            target_entity=target_entity,
            status=status,
            payload=payload,
            run_id=run_id,
            event_id=event_id,
            observed_at=observed_at,
        )
        return {"event_id": resolved}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Query Last Execution",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_query_last(
        action_kind: str | None = None,
        capability_name: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Query: 'What actually ran last time?' — returns recent execution events matching filters."""
        root = resolve_project_root()
        result = hub.execution.query_last_execution(
            root,
            action_kind=action_kind,
            capability_name=capability_name,
            session_id=session_id,
            limit=limit,
        )
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} recent execution event(s).",
            payload=result,
            artifact_name="execution-query-last",
            session_id=session_id,
            structured_summary={
                "count": len(result),
                "action_kind": action_kind,
                "capability_name": capability_name,
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Summary",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_query_summary(session_id: str | None = None) -> dict[str, Any]:
        """Query: 'What happened in this session?' — returns aggregate execution summary with ad-hoc vs procedure-linked breakdown."""
        return hub.execution.query_execution_summary(resolve_project_root(), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Dashboard Snapshot",
        },
        meta={"anthropic/searchHint": True},
    )
    def dashboard_snapshot(session_id: str | None = None, event_limit: int = 12) -> dict[str, Any]:
        """Return an operator-friendly dashboard snapshot for sessions, conductor state, config, execution, and usage proxies."""
        root = resolve_project_root()
        payload = runtime.dashboard_snapshot(
            root,
            session_id=session_id,
            event_limit=event_limit,
        )
        session_count = len(payload.get("sessions") or [])
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=(
                f"Dashboard snapshot ready for {session_count} session(s). "
                f"Selected session: `{payload.get('selected_session_id') or 'none'}`."
            ),
            payload=payload,
            artifact_name="dashboard-snapshot",
            session_id=session_id,
            structured_summary={
                "session_id": payload.get("selected_session_id"),
                "session_count": session_count,
                "has_selected_session": payload.get("selected_session") is not None,
                "token_usage_available": bool((payload.get("token_usage") or {}).get("available")),
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Registry List",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_registry_list() -> dict[str, Any]:
        """List MCP-touched AIDOCS projects known to the global registry."""
        # `project_registry` was never bound in this scope (F821 / NameError on
        # every call). The global registry is ProjectRegistryService (no-arg).
        from .project_registry_service import ProjectRegistryService

        return {
            "ok": True,
            "projects": ProjectRegistryService().list_projects(),
        }

    # Cross-project tools — eager-loaded. Conductors and sub-agents reach
    # for these when handing off work between projects.
    # Spec: .MEMORY/sessions/2026-04-13-repo-and-dashboard-audit/plans/global-aidocs-tree-and-loader.md

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project List",
        },
    )
    @renders_as("list", title="projects")
    def project_list(include_session_counts: bool = False) -> Any:
        """List every AIDOCS-enabled project. Set include_session_counts=true to include per-project session counts."""
        from .cross_project_ops import project_list as _project_list

        return _project_list(include_session_counts=include_session_counts)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project List Sessions",
        },
    )
    @renders_as("list", title="project sessions")
    def project_list_sessions(project_root: str) -> Any:
        """List sessions for a named project. Works cross-project — target need not be the currently-bound one (requires an approved relation + permission)."""
        _refusal = _gate_cross_project(project_root, "project_list_sessions")
        if _refusal is not None:
            return _refusal
        from .cross_project_ops import project_list_sessions as _project_list_sessions

        return _project_list_sessions(project_root)

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "Related Project Register",
        },
    )
    def related_project_register(
        name: str,
        path: str,
        reason: str = "",
        notes: str = "",
    ) -> Any:
        """Register a related/cross-project for this conductor.

        Writes to .MEMORY/.index/related_projects.sqlite3. Legacy
        markdown entries auto-import on first read.

        Once registered, related_project_code_search /
        related_project_symbol_bundle / related_project_compare_concept /
        related_project_subsystem_bundle can query the target by name.
        Also lets agent_spawn_worker_async run lanes inside the target
        via target_project=<name>.

        - `reason` (audit-grade): WHY these two projects are linked.
          E.g. "legacy source for current business logic",
          "client-feedback coordination", "shared deployment pipeline".
          Audit logs surface this field to explain cross-project work.
        - `notes` (free-form): anything else — gotchas, versions,
          contact points.

        Path must be absolute, exist, and contain a .MEMORY/ directory
        (target must be AIDOCS-managed). The target must additionally be a
        COMMISSIONED project on the operator-approved relation allowlist
        (security.approved_external_roots) and the caller must hold
        permission (solo/dev passthrough, corpo RBAC) — registering an
        un-approved external path is refused.
        """
        _refusal = _gate_cross_project(path, "related_project_register")
        if _refusal is not None:
            return _refusal
        return hub.related.register(
            resolve_project_root(),
            name=name,
            path=path,
            reason=reason or None,
            notes=notes or None,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project List",
        },
    )
    def related_project_list() -> Any:
        """List all related projects registered for this conductor."""
        rows = hub.related.list_related_projects(resolve_project_root())
        return {"count": len(rows), "projects": rows}

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "Handoff Create",
        },
    )
    def handoff_create(
        target_project_root: str,
        target_session_id: str,
        purpose: list[str] | None = None,
        current_state: list[str] | None = None,
        what_was_done: list[str] | None = None,
        what_failed: list[str] | None = None,
        what_matters_now: list[str] | None = None,
        open_questions: list[str] | None = None,
        risks_and_blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        estimated_effort: list[str] | None = None,
        suggested_next_steps: list[str] | None = None,
        related_sessions: list[str] | None = None,
        related_project_links: list[str] | None = None,
        freshness: list[str] | None = None,
        content_path: str = "",
        append: bool = False,
    ) -> dict[str, Any]:
        """Write a handoff into another project's session without switching binding. At least one section field must be provided.

        Path-or-inline rule (2026-04-20): pass content_path pointing at
        a pre-authored handoff markdown file instead of splitting the
        content across 13 list fields inline. When content_path is
        provided and no inline sections are, the file is written
        verbatim to <target_project>/.MEMORY/sessions/<sid>/HANDOFF.md
        and the section index rebuilt. append=True adds to an existing
        file; append=False overwrites. content_path resolves under
        target_project_root unless absolute.

        Hybrid: providing BOTH content_path AND inline sections is
        refused with an actionable error — pick one source.

        Writing into another project is gated: the target must be a
        COMMISSIONED project on the approved-relation allowlist and the
        caller must hold permission (solo/dev passthrough, corpo RBAC).
        """
        _refusal = _gate_cross_project_session(
            target_project_root,
            target_session_id,
            "handoff_create",
        )
        if _refusal is not None:
            return _refusal
        # Inline-vs-path conflict check. Any non-empty inline section
        # list alongside content_path is ambiguous; refuse.
        inline_sections = {
            "purpose": purpose,
            "current_state": current_state,
            "what_was_done": what_was_done,
            "what_failed": what_failed,
            "what_matters_now": what_matters_now,
            "open_questions": open_questions,
            "risks_and_blockers": risks_and_blockers,
            "relevant_files": relevant_files,
            "estimated_effort": estimated_effort,
            "suggested_next_steps": suggested_next_steps,
            "related_sessions": related_sessions,
            "related_project_links": related_project_links,
            "freshness": freshness,
        }
        has_inline = any(isinstance(v, list) and len(v) > 0 for v in inline_sections.values())
        if content_path and has_inline:
            return {
                "ok": False,
                "error": (
                    "handoff_create got both content_path AND inline "
                    "section fields. Pick ONE source — inline lists "
                    "OR a pre-authored handoff markdown file."
                ),
            }
        if content_path:
            from pathlib import Path as _Path

            target_root = _Path(target_project_root)
            content_path_obj = _Path(content_path)
            if not content_path_obj.is_absolute():
                content_path_obj = target_root / content_path_obj
            # Containment: the resolved file MUST live under the target
            # project or an operator-approved read-governed root. An
            # arbitrary absolute content_path is refused — handoff content
            # must never be a vector to read any file on the host.
            from .project_authority import _approved_roots, _under

            try:
                _resolved = content_path_obj.resolve()
            except Exception:
                return {
                    "ok": False,
                    "blocked_by": "content_path_unresolvable",
                    "error": f"content_path '{content_path}' could not be resolved; refused.",
                }
            _allowed_roots = [target_root] + _approved_roots(resolve_project_root())
            if not any(_under(_resolved, r) for r in _allowed_roots):
                return {
                    "ok": False,
                    "blocked_by": "content_path_escape",
                    "error": (
                        f"content_path '{content_path}' resolves outside the "
                        "target project and approved_external_roots; refused. "
                        "Handoff content must come from a governed root."
                    ),
                }
            if not content_path_obj.is_file():
                return {
                    "ok": False,
                    "error": (
                        f"content_path '{content_path}' does not "
                        f"resolve under target_project_root "
                        f"'{target_project_root}'. Pass inline fields "
                        f"or fix the path."
                    ),
                }
            try:
                raw = content_path_obj.read_text(encoding="utf-8")
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"failed to read content_path: {exc}",
                }
            # Write to target session's HANDOFF.md directly.
            handoff_path = target_root / ".MEMORY" / "sessions" / target_session_id / "HANDOFF.md"
            try:
                handoff_path.parent.mkdir(parents=True, exist_ok=True)
                if append and handoff_path.is_file():
                    existing = handoff_path.read_text(encoding="utf-8")
                    to_write = existing.rstrip() + "\n\n" + raw
                else:
                    to_write = raw
                if not to_write.endswith("\n"):
                    to_write += "\n"
                handoff_path.write_text(to_write, encoding="utf-8")
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"failed to write handoff: {exc}",
                }
            return {
                "ok": True,
                "mode": "verbatim_content_path",
                "session_id": target_session_id,
                "appended": bool(append),
            }
        from .cross_project_ops import handoff_create as _handoff_create

        return _handoff_create(
            target_project_root=target_project_root,
            target_session_id=target_session_id,
            purpose=purpose,
            current_state=current_state,
            what_was_done=what_was_done,
            what_failed=what_failed,
            what_matters_now=what_matters_now,
            open_questions=open_questions,
            risks_and_blockers=risks_and_blockers,
            relevant_files=relevant_files,
            estimated_effort=estimated_effort,
            suggested_next_steps=suggested_next_steps,
            related_sessions=related_sessions,
            related_project_links=related_project_links,
            freshness=freshness,
            append=append,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Compliance",
        },
        meta={"anthropic/searchHint": True},
    )
    def execution_query_compliance(
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Query: 'Did execution follow the intended procedure?' — compares procedure-linked runs vs ad-hoc runs."""
        return hub.execution.query_procedure_compliance(
            resolve_project_root(),
            session_id=session_id,
            limit=limit,
        )

    # NOTE (2026-05-24): the `execution_prune` MCP tool used to be registered
    # here too, but it was ALWAYS shadowed — create_server registers
    # register_project_admin_tools (early) before the inline
    # mcp_server.py:execution_prune (late), and FastMCP keeps the last
    # registration, so this one never won. Removed to kill the duplicate-name
    # footgun (manifest duplicate-detection caught it). The canonical
    # `execution_prune` tool lives in mcp_server.py (keep_days/max_events, with
    # by_age + by_size + auto_prune); the underlying service
    # hub.execution.prune_old_events is what project-sync calls directly.

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Compare Action Surface",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_compare(
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Compare what should happen, what can happen, and what did happen for a query."""
        return hub.action_surface.compare(
            resolve_project_root(),
            query=query,
            session_id=session_id,
            limit=limit,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Assess Action Surface",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_assess(
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return an operator-facing assessment of the action surface for a query."""
        return hub.action_surface.assess(
            resolve_project_root(),
            query=query,
            session_id=session_id,
            limit=limit,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Action Surface Status Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_status_bundle(
        queries: list[str],
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return an operator-facing multi-query status bundle over action surfaces."""
        return hub.action_surface.status_bundle(
            resolve_project_root(),
            queries=queries,
            session_id=session_id,
            limit=limit,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Action Surface Session Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_session_bundle(
        session_id: str,
        limit: int = 20,
        max_queries: int = 12,
    ) -> dict[str, Any]:
        """Return a session-driven operator-facing action-surface status bundle."""
        return hub.action_surface.session_status_bundle(
            resolve_project_root(),
            session_id=session_id,
            limit=limit,
            max_queries=max_queries,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Current Session Action Surface",
        },
        meta={"anthropic/searchHint": True},
    )
    def action_surface_current_session_bundle(
        limit: int = 20,
        max_queries: int = 12,
    ) -> dict[str, Any]:
        """Return a session-driven operator-facing action-surface bundle for the current managed or sole active session."""
        return hub.action_surface.current_session_bundle(
            resolve_project_root(),
            limit=limit,
            max_queries=max_queries,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Compile Workflow Actions",
        },
    )
    def workflow_actions_compile() -> dict[str, Any]:
        """Compile human-readable workflow rules into the runtime workflow artifact."""
        project_root = resolve_project_root()
        # Clear satisfaction records — rules recompiled means requirements restart
        hub.workflow.clear_satisfaction(project_root)
        result = hub.workflow.compile_project_rules(project_root)
        try:
            from .config import reload_config_caches

            reload_config_caches()
        except Exception:
            pass
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Workflow Actions",
        },
        meta={"anthropic/searchHint": True},
    )
    def workflow_actions_get() -> dict[str, Any] | None:
        """Read the compiled runtime workflow artifact for a project if present."""
        return hub.workflow.read_compiled(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Agent Workflow Rules",
        },
    )
    def workflow_agent_rules() -> dict[str, Any]:
        """Get natural language workflow rules for the conductor to enforce on agents.

        These are human-written rules like 'After fixing a bug, write a test' that
        the conductor reads and enforces via guidance and lane control.
        """
        rules = hub.workflow.get_agent_workflow_rules(resolve_project_root())
        return {"rules": rules, "count": len(rules)}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Workflow Triggers",
        },
        meta={"anthropic/searchHint": True},
    )
    def workflow_triggers_for_action(action_kind: str) -> dict[str, Any]:
        """Find workflow triggers that would fire after an action_kind completes."""
        triggers = hub.workflow.triggers_for_action_kind(action_kind)
        pending: list[dict[str, Any]] = []
        for trigger in triggers:
            pending.extend(
                hub.workflow.pending_actions_for_trigger(resolve_project_root(), trigger),
            )
        return {
            "action_kind": action_kind,
            "triggers": triggers,
            "pending_actions": pending,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project Code Search",
        },
        meta={"anthropic/searchHint": True},
    )
    def related_project_code_search(name: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search code in a configured related project using the same generic code index."""
        root = resolve_project_root()
        related_root = resolve_related_root(root, name)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        return hub.code.search_code(related_root, query=query, limit=limit)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project Symbol Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def related_project_symbol_bundle(
        name: str,
        symbol: str,
        path: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Build a symbol bundle from a configured related project."""
        root = resolve_project_root()
        related_root = resolve_related_root(root, name)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        return hub.code.get_symbol_bundle(
            related_root,
            symbol=symbol,
            path=path,
            kind=kind,
            limit=limit,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project Subsystem Bundle",
        },
        meta={"anthropic/searchHint": True},
    )
    def related_project_subsystem_bundle(
        name: str,
        concept: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Build a broad subsystem bundle from a configured related project."""
        root = resolve_project_root()
        related_root = resolve_related_root(root, name)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        hub.schema.sync_schema(related_root)
        return hub.code.get_subsystem_bundle(related_root, concept=concept, limit=limit)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Related Project Compare Concept",
        },
        meta={"anthropic/searchHint": True},
    )
    def related_project_compare_concept(name: str, concept: str, limit: int = 20) -> dict[str, Any]:
        """Compare a concept between the current project and a configured related project."""
        root = resolve_project_root()
        related_root = resolve_related_root(root, name)
        hub.code.sync_code_manifest(root, include_tests=False)
        hub.schema.sync_schema(root)
        hub.code.sync_code_manifest(related_root, include_tests=False)
        hub.schema.sync_schema(related_root)
        return {
            "concept": concept,
            "current_project": str(root),
            "related_project": {
                "name": name,
                "path": str(related_root),
            },
            "current": hub.code.get_subsystem_bundle(root, concept=concept, limit=limit),
            "related": hub.code.get_subsystem_bundle(related_root, concept=concept, limit=limit),
        }
