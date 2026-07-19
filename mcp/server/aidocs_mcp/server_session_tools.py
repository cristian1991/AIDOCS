from __future__ import annotations

from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as


def register_session_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_sync: Any,
    annotate_skill_result: Any,
    session_summary_to_dict: Any,
    coerce_to_list: Any,
) -> None:
    # Internal helper. Tool surface removed 2026-05-12 — ai_session(mode='list').
    @renders_as("list", title="sessions")
    def session_list() -> Any:
        """List sessions from project-local /.MEMORY/sessions/."""
        summaries = hub.sessions.list_sessions(resolve_project_root())
        return [session_summary_to_dict(item) for item in summaries]

    # session_read MCP tool REMOVED 2026-05-02 (Empire directive — paved-road
    # entry: session_connect is the only entry, side-doors die). The
    # wrapper read SESSION.md without binding identity. Conductors call
    # conductor_mode_enter(verbose=True) for the body AFTER the bind.
    # hub.sessions.read_session() stays for internal callers.
    # Mirrors session_select (2026-04-28), session_start (2026-04-30).

    # session_select removed 2026-04-28 — its job (read summary) duplicated
    # what session_list already returns per-session, and the name was
    # actively misleading (sounded like "pick this session to work on"
    # but only read metadata; session_connect is the actual bind).

    # session_start RESTORED as a thin compat alias 2026-05-03 (Empire
    # diagnosis): Claude Code's bootstrap probe sequence calls
    # session_start unconditionally on /mcp reconnect (capability_
    # definitions_get → session_start). After the 2026-04-30 removal
    # it raised NotFoundError, which CC interpreted as "MCP unhealthy"
    # → managed-mode display flips off → operator has to re-bind.
    # Now it's a near-no-op that succeeds, and as a side effect
    # auto-activates managed mode if it isn't already (matches Empire's
    # rule: any user message with managed_mode inactive should
    # activate it). Body is intentionally trivial — no sync_indexes,
    # no hydrate, no heavy bootstrap. Heavy bootstrap stays under
    # project_bootstrap_or_resume.
    @server.tool(
        annotations={
            "destructiveHint": False,
            "openWorldHint": False,
            "title": "Session Start (Compat Alias)",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def session_start(
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Compat alias for Claude Code's bootstrap probe.

        If managed mode is already active, returns the current state
        (idempotent fast path). If inactive, auto-activates against
        the most recent active session — matches the Empire's rule that
        any user message in an unbound state should rebind.

        For full bootstrap (index sync, hydration), use
        project_bootstrap_or_resume. For lightweight conductor bind,
        use session_connect. This shim exists ONLY because Claude
        Code's hardcoded probe sequence calls `session_start` on
        every /mcp reconnect; without this alias the probe fails
        with NotFoundError and CC marks the MCP as unhealthy.
        """
        project_root = resolve_project_root()
        # #134: resolve THIS conductor's host session id so the bind is
        # PER-CONDUCTOR and the CC reconnect probe no longer restamps the ONE
        # shared project singleton on every /mcp reconnect (multi-conductor
        # ping-pong). get_mode with the host id resolves this conductor's own
        # binding, not "whoever prompted last".
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
            set_calling_conductor_host_session_id,
        )

        _hsid = (current_calling_host_session_id() or "").strip()
        managed = hub.managed_mode.get_mode(project_root, host_session_id=_hsid)
        if managed.get("active"):
            # Restamp bound_by_boot_token to the current process's
            # token (2026-05-03 fix). CC fires this probe on every
            # /mcp reconnect. Without restamping, the singleton's
            # token stays pointed at the previous (now-dead) MCP
            # process, leaking stale-PID tokens to the dashboard
            # and audit log. Restamp is cheap (one row UPSERT) and
            # idempotent.
            sid_active = str(managed.get("session_id") or "").strip()
            restamped = False
            if sid_active:
                # A restamp must NEVER keep a stale, non-member bind "active" —
                # that is the split-brain (active while require_session
                # refuses). Heal an unmigrated legacy bind once; if it still
                # isn't a member, report the stale bind instead of restamping.
                from .session_membership_store import SessionMembershipStore

                if not SessionMembershipStore().ensure_member_or_heal(project_root, sid_active):
                    return {
                        "ok": False,
                        "active": False,
                        "stale_bind": True,
                        "session_id": sid_active,
                        "source": "session_start_stale_bind",
                        "error": (
                            f"managed mode was bound to '{sid_active}' but it "
                            f"is not a member of this project — stale bind "
                            f"cleared from trust. Run session_connect with an "
                            f"existing session, or `aidocs "
                            f"migrate-control-authority` for legacy sessions."
                        ),
                    }
                try:
                    # #134: recover the host id from the query_gate stdio bridge
                    # if the contextvar was empty (mirrors session_connect), then
                    # write PER-CONDUCTOR and restamp the shared singleton ONLY
                    # when we have no identity. With identity, skip the singleton
                    # so two reconnecting conductors don't clobber each other.
                    _h = _hsid
                    if not _h and sid_active:
                        _h = (
                            hub.query_gate.get_last_host_session_id(project_root, sid_active) or ""
                        ).strip()
                        if _h:
                            set_calling_conductor_host_session_id(_h)
                    hub.managed_mode.set_mode(
                        project_root,
                        session_id=sid_active,
                        source="session_start_compat_alias_restamp",
                        host_session_id=_h,
                        restamp_singleton=(not _h),
                    )
                    restamped = True
                except Exception:
                    pass
            return {
                "ok": True,
                "active": True,
                "session_id": sid_active,
                "source": "session_start_compat_alias_already_active",
                "boot_token_restamped": restamped,
                "note": (
                    "Managed mode already active. session_start is a "
                    "compat alias for CC's bootstrap probe; for full "
                    "bootstrap use project_bootstrap_or_resume."
                ),
            }
        # Inactive — auto-activate. Pick provided session_id, else most
        # recent active session, else fall through with a clear error.
        sid = (session_id or "").strip()
        if not sid:
            try:
                # Bounded one-time heal: if the legacy-ingest marker is absent,
                # import on-disk legacy sessions into the registry so they are
                # visible to enumeration before we auto-pick. No-op once sealed.
                from .session_membership_store import SessionMembershipStore

                _ms = SessionMembershipStore()
                if not _ms.is_sealed(project_root):
                    _ms.migrate_legacy_once(project_root)
            except Exception:
                pass
            try:
                sessions = hub.sessions.list_sessions(project_root)
                active = [s for s in sessions if s.get("status") == "active"]
                if active:
                    sid = str(active[0].get("session_id") or "").strip()
            except Exception:
                pass
        if not sid:
            return {
                "ok": False,
                "active": False,
                "error": (
                    "No active session to auto-bind. Run /aidocs to "
                    "initialize, or session_connect with an explicit "
                    "session_id."
                ),
            }
        try:
            # #134: recover host id from the stdio bridge, then bind
            # per-conductor. SEED-IF-INACTIVE: the singleton is inactive on this
            # auto-activate path (first bind), so we DO restamp it — this keeps
            # `aidocs status` (a separate CLI process that reads the singleton)
            # correct even for a conductor whose only bind ever comes through
            # session_start. Seeding an inactive singleton cannot clobber another
            # conductor's active bind; only restamping an ALREADY-active one
            # (the reconnect path above) causes the ping-pong.
            _h = _hsid
            if not _h and sid:
                _h = (hub.query_gate.get_last_host_session_id(project_root, sid) or "").strip()
                if _h:
                    set_calling_conductor_host_session_id(_h)
            hub.managed_mode.set_mode(
                project_root,
                session_id=sid,
                source="session_start_auto_activate",
                host_session_id=_h,
                restamp_singleton=True,
            )
        except Exception as exc:
            return {
                "ok": False,
                "active": False,
                "error": f"auto-activate failed: {exc}",
            }
        return {
            "ok": True,
            "active": True,
            "session_id": sid,
            "source": "session_start_auto_activated",
            "note": (
                "Managed mode auto-activated for the most recent "
                "active session. Compat alias for CC's bootstrap probe."
            ),
        }

    # session_start ORIGINAL REMOVAL note (2026-04-30 operator decision —
    # tool surface trim, "120 tools, lean where unnecessary").
    # Reasons:
    #   - Functionally overlapped session_connect (both "begin work
    #     in a session"); confusion caused agents to call the heavy
    #     path and feel like a hang (sync_indexes=True default).
    #   - Bootstrap path uses runtime.session_start as an INTERNAL
    #     Python call from runtime_bootstrap_orchestration_service
    #     (with sync_indexes=False, hydrate=False) — that internal
    #     method stays. Only the agent-facing MCP tool is gone.
    #   - Agents now use:
    #       project_bootstrap_or_resume (eager, system-controlled
    #         first-prompt bootstrap; this DOES sync indexes via
    #         _sync_bootstrap_indexes — operator doctrine: bootstrap
    #         is system-decided, not agent-skippable)
    #       session_list (catalog of sessions)
    #       session_connect (eager, lightweight bind to selected
    #         session — operator/conductor identity-aware)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Start State",
        },
    )
    def session_start_state_get(session_id: str | None = None) -> dict[str, Any]:
        """Return lightweight startup readiness and imported skill state for a session."""
        return annotate_skill_result(
            runtime.session_start_state(resolve_project_root(), session_id=session_id),
            override_store=runtime._skill_overrides,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Bootstrap Project",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def project_bootstrap_or_resume(
        session_id: str | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run the mandatory project setup/index/session bootstrap flow."""
        return runtime.project_bootstrap_or_resume(
            resolve_project_root(),
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Orchestrate AIDOCS",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def aidocs_orchestrate(
        user_request: str,
        action_kind: str = "understand",
        session_id: str | None = None,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Run the AIDOCS bootstrap/session/retrieval flow as one high-level entrypoint."""
        return runtime.aidocs_orchestrate(
            resolve_project_root(),
            user_request=user_request,
            action_kind=action_kind,
            session_id=session_id,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    @server.tool(annotations={"readOnlyHint": True, "openWorldHint": False, "title": "Get Mode"})
    def aidocs_mode_get() -> dict[str, Any]:
        """Read the current runtime/session-binding AIDOCS-managed mode state."""
        return hub.managed_mode.get_mode(resolve_project_root())

    @server.tool(
        annotations={
            "destructiveHint": False,
            "openWorldHint": False,
            "title": "Bump Agent Memory Epoch",
        },
    )
    def bump_agent_memory_epoch(
        host_kind: str,
        host_session_id: str,
    ) -> dict[str, Any]:
        """Bump the compaction count for (host_kind, host_session_id).

        Host plugins call this on compaction events. The resulting
        epoch rotation invalidates DNT-banner-shown markers (and any
        future agent-memory gates keyed on epoch), so the agent sees
        fresh banners after compaction wipes its context.

        OpenCode wires this to its `session.compacted` event.
        Claude Code's compaction hook (when present) wires here too.
        Codex has no compaction hook today; epoch rotates only on
        host_session_id change for Codex.
        """
        if not host_kind or not host_session_id:
            return {
                "ok": False,
                "err": "host_kind and host_session_id required",
            }
        from .agent_memory_epoch import bump_compaction_count

        new_count = bump_compaction_count(
            resolve_project_root(),
            host_kind=host_kind.strip().lower(),
            host_session_id=host_session_id.strip(),
        )
        return {"ok": True, "compaction_count": new_count}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Set Mode",
        },
    )
    def aidocs_mode_set(session_id: str, source: str = "/aidocs") -> dict[str, Any]:
        """Set runtime/session-binding AIDOCS-managed mode for a selected session."""
        return hub.managed_mode.set_mode(
            resolve_project_root(),
            session_id=session_id,
            source=source,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Clear Mode",
        },
    )
    def aidocs_mode_clear() -> dict[str, Any]:
        """Clear the current runtime/session-binding AIDOCS-managed mode state."""
        # #438: clear_mode deletes ONLY the deprecated singleton row; the
        # real binding is per-conductor ROW EXISTENCE. A disable that
        # skips the unbind is a no-op against authority — so sever the
        # calling conductor's own binding first, then clear the singleton.
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        root = resolve_project_root()
        hsid = (current_calling_host_session_id() or "").strip()
        unbind = hub.managed_mode.unbind_current_conductor(root, hsid)
        state = hub.managed_mode.clear_mode(root)
        state["unbind"] = unbind
        return state

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Route Prompt",
        },
    )
    def aidocs_route_prompt(
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the deterministic MCP routing decision for a normal user prompt."""
        return annotate_skill_result(
            runtime.aidocs_route_prompt(
                resolve_project_root(),
                user_request=user_request,
                action_kind=action_kind,
                explicit_targets=explicit_targets,
            ),
            override_store=runtime._skill_overrides,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Classify Prompt",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def aidocs_classify_prompt(
        user_request: str,
        explicit_targets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Classify a normal prompt into a deterministic AIDOCS action kind."""
        return runtime.classify_prompt_action(user_request, explicit_targets=explicit_targets)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Handle Prompt",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def aidocs_handle_prompt(
        user_request: str,
        action_kind: str = "auto",
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Handle a normal user prompt through the MCP-first routing/orchestration flow."""
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        host_session_id = (current_calling_host_session_id() or "").strip()
        return runtime.aidocs_handle_prompt(
            resolve_project_root(),
            user_request=user_request,
            action_kind=action_kind,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
            host_session_id=host_session_id,
        )

    # Internal helper. Tool surface removed 2026-05-12 — ai_session(mode='create').
    def session_create(
        title: str,
        goal: str = "",
        session_id: str = "",
        owner: str = "",
        scope: str = "-",
        status: str = "active",
        predecessor_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new session. Only title is required — ID, owner, date auto-generated."""
        import re as _re
        from datetime import date as _date

        # Auto-generate session_id from date + slugified title
        if not session_id:
            slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
            session_id = f"{_date.today().isoformat()}-{slug}"

        # Auto-detect owner — the agent/host calling this tool
        if not owner:
            try:
                managed = hub.managed_mode.get_mode(resolve_project_root())
                # Use host identity from managed mode if available
                owner = str(managed.get("source") or "").strip() or "agent"
            except Exception:
                owner = "agent"

        session = hub.sessions.create_session(
            resolve_project_root(),
            session_id=session_id,
            title=title,
            owner=owner,
            goal=goal or title,
            scope=scope,
            status=status,
            predecessor_session_id=predecessor_session_id,
        )
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Prune Stale Claims",
        },
    )
    def session_prune_stale_claims(
        session_id: str,
        stale_after_minutes: int = 30,
    ) -> dict[str, Any]:
        """Remove stale advisory claims from a session."""
        session = hub.sessions.prune_stale_claims(
            resolve_project_root(),
            session_id,
            stale_after_minutes=stale_after_minutes,
        )
        return {
            "session_id": session.session_id,
            "path": str(session.path),
            "sections": session.sections,
        }

    # session_grant_raw_tools removed 2026-04-19: NLP parsing of the
    # user's current prompt in UserPromptSubmit is the single grant
    # source. The explicit MCP tool variant was a foot-gun — any
    # agent that learned it existed could have self-granted privileges
    # by calling it with the conductor's session_id.

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Grant Raw Tools To Lane",
        },
    )
    def ai_lane_grant(
        session_id: str,
        lane_id: str,
        tools: list[str],
        reason: str,
    ) -> dict[str, Any]:
        """Conductor-only: delegate raw-tool access (read, grep, bash, etc.) to a specific lane.

        Agents/lanes default to AIDOCS-only. When the user has authorized raw
        tools for a specific dispatched task, the conductor uses this tool to
        pass that authority down to the lane. The grant is lane-scoped and
        accumulates across calls (repeated grants add to the list).

        `reason` is required and journaled. Destructive-command guards (heuristic
        judge, bash denylist) still apply — this only lifts the raw-file-tool
        block, not the destructive-op block. Grantable tools:
        read, grep, glob, edit, write, patch, apply_patch, multiedit, bash.

        This tool is in AccessGate._CONDUCTOR_ONLY_TOOLS; a lane calling it
        is hard-blocked regardless of allowlist (no self-elevation).
        """
        return runtime.grant_raw_tools_for_lane(
            resolve_project_root(),
            session_id=session_id,
            lane_id=lane_id,
            tools=tools,
            reason=reason,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Send Prompt To Lane Worker",
        },
    )
    def ai_lane_send(
        worker_id: str,
        session_id: str,
        prompt: str,
    ) -> Any:
        """Conductor-only: write a prompt to a lane worker's mailbox.

        Delivery is host-agnostic: the pending prompt is injected into the
        worker's next-turn input by PromptMutator.worker_lane_intercept (the
        UPS pipeline every host calls), and when the worker has exited the
        conductor resumes its host session (lane_resume_dispatcher:
        `<host> --resume`/`-s`/`resume <session-id>`) so it acts on the new
        instruction. (Legacy ScheduleWakeup park-and-wake is retired — #103.)

        Args:
            worker_id: Target worker's id (from ai_status).
            session_id: The worker's session id (typically the caller's).
            prompt: The instruction to inject. Becomes the next thing
                the worker agent "hears" on its next turn.

        Returns:
            {"ok": True, "mailbox_id": N, "queue_depth": M}

        Write is audited via execution_events as lane_mailbox_write.
        Messages older than 15 minutes auto-expire.

        """
        from .lane_mailbox_store import LaneMailboxStore

        project_root = resolve_project_root()
        author_task_id = ""
        try:
            author_task_id = str(
                runtime.hub.query_gate.get_current_task_id(project_root, session_id) or "",
            )
        except Exception:
            pass
        store = LaneMailboxStore()
        mid = store.put(
            project_root,
            worker_id=worker_id,
            session_id=session_id,
            prompt=prompt,
            author_session_id=session_id,
            author_task_id=author_task_id or None,
        )
        # Queue depth for the caller's situational awareness.
        hist = store.list_for_worker(
            project_root,
            worker_id=worker_id,
            limit=50,
        )
        queue_depth = sum(1 for h in hist if h.get("status") == "pending")
        from . import dual_audience as _da

        return _da.ok_sub(
            tool_name="lane_send_prompt",
            structured={
                "mailbox_id": mid,
                "worker_id": worker_id,
                "queue_depth": queue_depth,
            },
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Lane Mailbox Peek",
        },
    )
    def ai_lane_inbox(
        worker_id: str,
        limit: int = 20,
    ) -> Any:
        """Read a worker's mailbox history (pending + consumed + expired).

        Diagnostic surface for dashboards and triage. WAR D (#452/#217):
        when the CALLER is the worker itself (env AIDOCS_EXPERT_ID matches
        worker_id), reading the inbox CONSUMES its pending prompts — this
        is the drain that clears the unread-message block. Conductor /
        dashboard reads stay non-consuming.
        """
        import os as _os_inbox

        from .lane_mailbox_store import LaneMailboxStore

        project_root = resolve_project_root()
        store = LaneMailboxStore()
        consumed = 0
        caller_worker = _os_inbox.environ.get("AIDOCS_EXPERT_ID", "").strip()
        if caller_worker and caller_worker == str(worker_id).strip():
            consumed = store.consume_pending(project_root, worker_id=worker_id)
        rows = store.list_for_worker(
            project_root,
            worker_id=worker_id,
            limit=int(limit),
        )
        out = {"worker_id": worker_id, "messages": rows, "count": len(rows)}
        if consumed:
            out["consumed_now"] = consumed
        return out

    # ── C.20 direct registry dispatch (tool-surface map §7.4, 2026-07-10) ──
    # The ai_lane consolidator dispatches these deprecation-window siblings by
    # literal legacy name via tool_interface._delegate; registering the same
    # closures here makes that dispatch in-process instead of a ~150ms
    # create_server round-trip. Idempotent — the latest create_server
    # invocation wins (register_impl doctrine). Zero behavior change: these
    # are the exact objects the @server.tool registrations above bind.
    # Coverage: test_c20_direct_dispatch (full legacy-sibling section).
    from . import tool_interface as _ti_c20

    _ti_c20.register_impl("ai_lane_grant", ai_lane_grant)
    _ti_c20.register_impl("ai_lane_send", ai_lane_send)
    _ti_c20.register_impl("ai_lane_inbox", ai_lane_inbox)

    # ── WAR D (#452/#218): additive scope grant onto the SAME gate
    # columns the spawn path stamps. Reached via the ai_lane
    # consolidator (action='grant_scope') — no standalone @server.tool.
    def ai_lane_grant_scope(
        session_id: str,
        lane_id: str,
        kind: str,
        items: list,
        reason: str = "",
    ) -> dict[str, Any]:
        """Conductor: additively widen a lane's gate columns (#218).

        kind='files' → lane_exact_paths (+ lane_scopes); kind='tools'
        → lane_extra_tools (extension, never replacement).
        """
        from .conductor_comms import lane_grant_scope

        return lane_grant_scope(
            resolve_project_root(),
            session_id=session_id,
            lane_id=lane_id,
            kind=kind,
            items=[str(i) for i in (items or [])],
            reason=reason,
        )

    _ti_c20.register_impl("ai_lane_grant_scope", ai_lane_grant_scope)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Session Handoff",
        },
    )
    def session_handoff_get(session_id: str) -> dict[str, Any]:
        """Read the structured collaboration handoff for a session."""
        handoff = hub.sessions.read_handoff(resolve_project_root(), session_id)
        return {
            "session_id": handoff.session_id,
            "path": str(handoff.path),
            "sections": handoff.sections,
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Session Handoff",
        },
    )
    def session_handoff_update(
        session_id: str,
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
        append: bool = False,
    ) -> dict[str, Any]:
        """Update the structured collaboration handoff for a session."""
        purpose = coerce_to_list(purpose)
        current_state = coerce_to_list(current_state)
        what_was_done = coerce_to_list(what_was_done)
        what_failed = coerce_to_list(what_failed)
        what_matters_now = coerce_to_list(what_matters_now)
        open_questions = coerce_to_list(open_questions)
        risks_and_blockers = coerce_to_list(risks_and_blockers)
        relevant_files = coerce_to_list(relevant_files)
        estimated_effort = coerce_to_list(estimated_effort)
        suggested_next_steps = coerce_to_list(suggested_next_steps)
        related_sessions = coerce_to_list(related_sessions)
        related_project_links = coerce_to_list(related_project_links)
        freshness = coerce_to_list(freshness)
        patch: dict[str, list[str]] = {}
        if purpose is not None:
            patch["Purpose"] = runtime._as_bullets(purpose)
        if current_state is not None:
            patch["Current State"] = runtime._as_bullets(current_state)
        if what_was_done is not None:
            patch["What Was Done"] = runtime._as_bullets(what_was_done)
        if what_failed is not None:
            patch["What Failed / Dead Ends"] = runtime._as_bullets(what_failed)
        if what_matters_now is not None:
            patch["What Matters Now"] = runtime._as_bullets(what_matters_now)
        if open_questions is not None:
            patch["Open Questions"] = runtime._as_bullets(open_questions)
        if risks_and_blockers is not None:
            patch["Risks and Blockers"] = runtime._as_bullets(risks_and_blockers)
        if relevant_files is not None:
            patch["Relevant Files"] = runtime._as_file_bullets(relevant_files)
        if estimated_effort is not None:
            patch["Estimated Effort"] = runtime._as_bullets(estimated_effort)
        if suggested_next_steps is not None:
            patch["Suggested Next Steps"] = runtime._as_bullets(suggested_next_steps)
        if related_sessions is not None:
            patch["Related Sessions"] = runtime._as_bullets(related_sessions)
        if related_project_links is not None:
            normalized = []
            for item in related_project_links:
                text = item.strip()
                if not text:
                    continue
                normalized.append(text)
            patch["Related Project Links"] = runtime._as_bullets(normalized)
        if freshness is not None:
            patch["Freshness"] = runtime._as_bullets(freshness)
        handoff = hub.sessions.update_handoff(
            resolve_project_root(),
            session_id,
            patch,
            append=append,
        )
        return {
            "session_id": handoff.session_id,
            "path": str(handoff.path),
            "sections": handoff.sections,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Handoff Steps",
        },
    )
    def session_handoff_steps_get(session_id: str) -> dict[str, Any]:
        """Read structured handoff steps for a session."""
        return {
            "session_id": session_id,
            "steps": hub.sessions.read_handoff_steps(resolve_project_root(), session_id),
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Normalize Handoff Steps",
        },
    )
    def session_handoff_steps_normalize(session_id: str) -> dict[str, Any]:
        """Normalize legacy/drifted handoff step markers into canonical step states."""
        return hub.sessions.normalize_handoff_steps(resolve_project_root(), session_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Handoff Step",
        },
    )
    def session_handoff_step_update(
        session_id: str,
        step_id: str | None = None,
        text: str | None = None,
        status: str = "open",
        append: bool = True,
    ) -> dict[str, Any]:
        """Create or update one structured handoff step."""
        handoff = hub.sessions.upsert_handoff_step(
            resolve_project_root(),
            session_id,
            step_id=step_id,
            text=text,
            status=status,
            append=append,
        )
        return {
            "session_id": handoff.session_id,
            "path": str(handoff.path),
            "sections": handoff.sections,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Compliance",
        },
        meta={"anthropic/searchHint": True},
    )
    def session_compliance_get(session_id: str) -> dict[str, Any]:
        """Return task/logging debt and actionable continuity state for a session."""
        return runtime.session_compliance_summary(resolve_project_root(), session_id)
