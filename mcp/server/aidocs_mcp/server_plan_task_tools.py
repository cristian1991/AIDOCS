from __future__ import annotations

from pathlib import Path
from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .mode_schema import modes
from .tool_display import renders_as

# One-line directive appended to task_begin so a fresh agent knows
# the lifecycle protocol without needing to re-read workflow rules.
# The agent writes task_update at each meaningful step and
# task_complete when done; this string is the only useful thing to
# echo back from task_begin.
_TASK_BEGIN_DIRECTIVE = "call task_update at each step; task_complete when done"


def _capture_lane_worker_task_complete(
    hub: Any,
    project_root: Path,
    session_id: str,
    *,
    result_summary: str,
    verification_evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Phoenix 2026-05-08: §VIII enforcement.

    Lane workers (AIDOCS_EXPERT_LANE_ID env set) don't self-declare
    done. Instead, task_complete CAPTURES into a pending review row
    + returns a farewell envelope. Worker exits cleanly. Conductor
    sees the 📋 surface, calls ai_review.
    Approve = work was good, no resume needed. Deny = AIDOCS spawns
    the host's --resume CLI with a bootstrap message containing the
    rationale.

    Conductors (no AIDOCS_EXPERT_LANE_ID env) skip this branch
    entirely — they self-decide their tasks per the original
    contract.

    Returns farewell envelope dict when capture happens. Returns
    None when caller is conductor (proceed to normal task_complete).
    """
    import os as _os

    lane_id = _os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()
    if not lane_id:
        return None  # conductor; proceed normally
    worker_id = _os.environ.get("AIDOCS_EXPERT_ID", "").strip()
    # Phoenix 2026-05-09 §VIII deny-path fix: read backend +
    # host_session_id from session_lane_agents in one shot.
    # host_session_id is stamped by the worker's host plugin/hook
    # (opencode plugin chat.message; claude_hook PreToolUse) on the
    # worker's first session event. Replaces the prior reliance on
    # query_gate.last_cli_session_id (deprecated) which carried the
    # CONDUCTOR's stamp, not the worker's — making the dispatcher
    # fire `<host> --resume <conductor's id>` against the wrong
    # target. host_session_id may be empty if the worker exits before
    # its first session event lands; the dispatcher handles empty
    # gracefully (returns dispatched=False with a clear error).
    backend = ""
    host_session_id = ""
    try:
        import sqlite3 as _sqlite

        from .execution_index_store import ExecutionIndexStore

        store_path = ExecutionIndexStore().db_path(project_root)
        with _sqlite.connect(str(store_path)) as conn:
            conn.row_factory = _sqlite.Row
            row = (
                conn.execute(
                    "SELECT backend, host_session_id FROM session_lane_agents "
                    "WHERE worker_id = ? LIMIT 1",
                    (worker_id,),
                ).fetchone()
                if worker_id
                else None
            )
            if row is not None:
                backend = str(row["backend"] or "").strip()
                host_session_id = str(row["host_session_id"] or "").strip()
    except Exception:
        pass
    # Extract evidence paths from verification_evidence dict.
    evidence_paths: list[str] = []
    if isinstance(verification_evidence, dict):
        files = verification_evidence.get("files") or verification_evidence.get("evidence_paths")
        if isinstance(files, list):
            evidence_paths = [str(p) for p in files if str(p).strip()]
    try:
        from . import lane_completion_review_store as _lcr

        review_id = _lcr.request_review(
            project_root,
            lane_id=lane_id,
            session_id=session_id,
            work_summary=result_summary or "",
            evidence_paths=evidence_paths,
            host_session_id=host_session_id,
            backend=backend,
            worker_id=worker_id,
        )
    except Exception as exc:
        # Capture failure must NOT silently let the worker self-declare.
        # Return a refusal envelope so the worker exits with a clear
        # error rather than escaping the §VIII gate.
        return {
            "ok": False,
            "blocked_by": "review_capture_failed",
            "lane_id": lane_id,
            "error": (
                f"Lane completion review capture failed: {exc!r}. "
                "task_complete refused so the §VIII gate isn't bypassed."
            ),
        }
    return {
        "ok": True,
        "captured_for_review": True,
        "review_id": review_id,
        "lane_id": lane_id,
        "next": (
            "Your task_complete was captured for conductor verification. "
            "This process should now exit cleanly. You will only be "
            "resumed if the conductor needs you to fix something — "
            "approval is silent (no resume needed). Don't poll, don't "
            "retry; trust the asynchronous review channel."
        ),
    }


def _check_session_bind_match(
    hub: Any,
    project_root: Path,
    session_id: str,
    tool_name: str,
) -> dict[str, Any] | None:
    """#71 (2026-04-27): refuse task lifecycle calls where the
    requested session_id doesn't match the conductor's bound session.

    Pre-fix bug: agent calls task_begin(session_id="A") while bound
    to session_id="B". Task lands under A; subsequent edit-gate checks
    use B; gate refuses with confusing "no active task" error even
    though task_status reports the task as active under A.

    Now: refuse task_begin/task_update/task_complete when session_id
    arg differs from the bound session, with a clear envelope pointing
    at session_connect as the fix.

    Returns:
        None when session_id matches the bind (caller proceeds).
        Refusal envelope dict when mismatch.

    """
    try:
        # Resolve via per-conductor mapping when we can get the calling
        # conductor's host_session_id. Falls back to singleton only if
        # identity is unavailable. Per empire's gate-invariants #50/#54
        # sub-clause: "explicit per-conductor mapping wins; the legacy
        # project-only singleton is a deprecated fallback." Pulling
        # host_session_id from query_gate.last_cli_session_id mirrors
        # claude_hook's pattern (line 2500-2505) — query_gate stamps
        # the most-recent CLI session per (project_root, session_id).
        host_sid = ""
        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            host_sid = current_calling_host_session_id() or ""
        except Exception:
            host_sid = ""
        if not host_sid:
            try:
                # Direct accessor — gate.get() omits last_cli_session_id.
                host_sid = hub.query_gate.get_last_cli_session_id(
                    project_root,
                    session_id,
                )
            except Exception:
                host_sid = ""
        managed = hub.managed_mode.get_mode(
            project_root,
            host_session_id=host_sid,
        )
        bound = str(managed.get("session_id") or "").strip()
    except Exception:
        # Defensive: managed_mode resolution failure shouldn't break
        # the task lifecycle entirely. Let it through.
        return None
    if not bound:
        # No active managed_mode — nothing to mismatch against.
        return None
    requested = (session_id or "").strip()
    if not requested or requested == bound:
        return None
    return {
        "ok": False,
        "blocked_by": "session_mismatch",
        "tool": tool_name,
        "error": (
            f"{tool_name}(session_id='{requested}') called while "
            f"conductor is bound to session_id='{bound}'. Tasks live "
            f"under the BOUND session, not the parameter. To work in "
            f"'{requested}': call session_connect(session_id="
            f"'{requested}') first. Otherwise drop the session_id "
            f"argument — it defaults to the bound session."
        ),
        "bound_session_id": bound,
        "requested_session_id": requested,
    }


def _resolve_list_or_path(
    *,
    inline: list[str] | None,
    path: str,
    project_root: Path,
    field_name: str,
) -> list[str] | dict[str, Any] | None:
    """Resolve a list[str] field from inline OR a newline-delimited file.

    Rules (per operator's path-or-ID API pattern, 2026-04-20):
      - inline list wins when both are provided — explicit agent
        intent beats disk.
      - path resolves under project_root unless absolute.
      - blank lines dropped; lines prefixed "- " unwrapped.
      - returns a dict with {ok:false, error:...} on missing file so
        the caller can pass it straight back to the agent.
      - returns None when both are empty (caller treats as "field
        not provided").
    """
    if inline is not None and len(inline) > 0:
        return list(inline)
    if not path:
        return None
    path_obj = Path(path)
    if not path_obj.is_absolute():
        path_obj = project_root / path_obj
    if not path_obj.is_file():
        return {
            "ok": False,
            "error": (
                f"{field_name}_path '{path}' does not resolve to a "
                f"readable file under {project_root}. Pass the list "
                f"inline as '{field_name}' or fix the path."
            ),
        }
    try:
        raw = path_obj.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "error": f"failed to read {field_name}_path '{path}': {exc}",
        }
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if stripped:
            out.append(stripped)
    return out


def _trim_lifecycle_result(
    result: Any,
    include_code_bundle: bool,
    *,
    tool: str,
) -> Any:
    """Dual-audience envelope for task_begin/task_update/task_complete.

    Agent view (structured):
      task_begin    → {"ok": true, "next": "<directive>"}
      task_update   → {"ok": true}
      task_complete → {"ok": true}   on success
                     {"ok": false, "error": <reason>}  on verification block
    Operator view (TextContent):
      One-line confirmation with session_id + tool so they can see
      progress in the transcript without decoding JSON.

    The raw runtime result carries 10-20 KB of session/plan/context/
    handoff section echoes — useless to the agent (it wrote that
    content) and the biggest single context sink in the lifecycle.

    Bypassed when:
      - include_code_bundle=True — caller explicitly asked for the
        full bundle (dashboard / tests).
      - result isn't a dict — passthrough.
    """
    if not isinstance(result, dict):
        return result
    if include_code_bundle:
        return result

    from . import dual_audience as _da

    session_id = str(result.get("session_id") or "")

    # Block path on task_complete: operator sees red chip with reason.
    if tool == "task_complete":
        if result.get("blocked") or result.get("verified") is False:
            verification = result.get("verification") or {}
            reason = (
                result.get("reason")
                or (verification.get("reason") if isinstance(verification, dict) else None)
                or "verification_blocked"
            )
            extra: dict[str, Any] = {"tool": "task_complete"}
            if isinstance(verification, dict) and verification.get("details"):
                extra["details"] = str(verification["details"])[:500]
            return _da.fail(
                tool_name="task_complete",
                error=f"task_complete blocked: {reason}",
                extra_structured=extra,
            )

    if tool == "task_begin":
        pretty = [f"▶ task_begin  session={session_id}" if session_id else "▶ task_begin"]
        return _da.ok(
            tool_name="task_begin",
            pretty_lines=pretty,
            structured={"next": _TASK_BEGIN_DIRECTIVE},
        )
    if tool == "task_update":
        pretty = [f"… task_update  session={session_id}" if session_id else "… task_update"]
        return _da.ok(tool_name="task_update", pretty_lines=pretty)
    # task_complete success
    pretty = [f"✔ task_complete  session={session_id}" if session_id else "✔ task_complete"]
    return _da.ok(tool_name="task_complete", pretty_lines=pretty)


def register_plan_task_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_sync: Any,
) -> None:
    # plan_connect MCP tool REMOVED 2026-05-02 (king directive — paved-road
    # entry: session_connect is the only entry, side-doors die). The
    # wrapper dumped a swamp (lanes, decisions, step_analysis,
    # plan_feedback, normalized_lines) without binding identity.
    # Conductors get plans-list-with-status from session_connect; per-plan
    # bodies via the upcoming ai_plan(mode="read", name=...) consolidation.
    # runtime.plan_connect() stays for internal callers.
    # Mirrors session_select (2026-04-28), session_start (2026-04-30),
    # session_read (2026-05-02).

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "ai_plan_create",
        },
    )
    @timed_sync
    def ai_plan_create(
        session_id: str,
        spec_text: str = "",
        spec_path: str = "",
        scope: str | None = None,
        constraints: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Create or replace the session plan from a deterministic spec format.

        Pass EITHER spec_text (inline string) OR spec_path (relative or
        absolute path to an already-written .md file). If spec_path is
        provided, the file is read and its contents used — agents don't
        need to re-type a spec they've already authored on disk.

        When spec_text starts with '# Plan' and carries lane-aware
        section markers (## Why / ## Lane graph / ## Out of scope /
        ## Sequence / ## Backlog inbox), the file is written verbatim
        — operator-authored lane-aware plans survive round-trips.
        """
        root = resolve_project_root()
        resolved_text = spec_text
        if spec_path and not resolved_text.strip():
            from pathlib import Path as _Path

            path_obj = _Path(spec_path)
            if not path_obj.is_absolute():
                path_obj = root / path_obj
            if not path_obj.is_file():
                return {
                    "ok": False,
                    "error": (
                        f"spec_path '{spec_path}' does not resolve to a "
                        f"readable file under {root}. Pass spec_text "
                        f"directly or fix the path."
                    ),
                }
            try:
                resolved_text = path_obj.read_text(encoding="utf-8")
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"failed to read spec_path '{spec_path}': {exc}",
                }
        if not resolved_text.strip():
            return {
                "ok": False,
                "error": (
                    "plan_create_from_spec requires spec_text (inline) "
                    "or spec_path (file reference). Both were empty."
                ),
            }
        return runtime.plan_create_from_spec(
            root,
            session_id=session_id,
            spec_text=resolved_text,
            scope=scope,
            constraints=constraints,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Validate Plan",
        },
    )
    @timed_sync
    def plan_validate(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Validate that the session plan is executable and has real verification steps."""
        return runtime.plan_validate(resolve_project_root(), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "ai_plan_graph",
        },
    )
    @timed_sync
    def ai_plan_graph(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Return the conductor lane graph for a lane-aware session plan."""
        return runtime.plan_conductor_graph(resolve_project_root(), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "ai_plan_status",
        },
    )
    @timed_sync
    def ai_plan_status(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Return the conductor graph plus runnable lane status for a lane-aware session plan."""
        return runtime.plan_conductor_status(resolve_project_root(), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Mode Select",
        },
    )
    @timed_sync
    def execution_mode_select(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Return the runtime-owned execution mode selection for a session plan."""
        return runtime.execution_mode_select(resolve_project_root(), session_id=session_id)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "ai_plan_dispatch",
        },
    )
    @timed_sync
    def ai_plan_dispatch(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Return the next delegated lane task packet for a session plan."""
        return runtime.plan_dispatch_next(resolve_project_root(), session_id=session_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "ai_plan_report",
        },
    )
    @timed_sync
    def ai_plan_report(
        session_id: str,
        packet_result: dict[str, Any] | None = None,
        packet_result_path: str = "",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Ingest one delegated lane result and update conductor state.

        Path-or-inline rule (2026-04-20): pass packet_result_path
        pointing at a JSON file the lane worker already wrote instead
        of re-inlining the full dict. Inline packet_result wins when
        both are provided. Paths resolve under project_root unless
        absolute.
        """
        root = resolve_project_root()
        if packet_result is None or not packet_result:
            if packet_result_path:
                import json as _json
                from pathlib import Path as _Path

                path_obj = _Path(packet_result_path)
                if not path_obj.is_absolute():
                    path_obj = root / path_obj
                if not path_obj.is_file():
                    return {
                        "ok": False,
                        "error": (
                            f"packet_result_path '{packet_result_path}' "
                            f"does not resolve to a readable file "
                            f"under {root}. Pass packet_result inline "
                            f"or fix the path."
                        ),
                    }
                try:
                    packet_result = _json.loads(path_obj.read_text(encoding="utf-8"))
                except (OSError, _json.JSONDecodeError) as exc:
                    return {
                        "ok": False,
                        "error": (
                            f"failed to load packet_result_path '{packet_result_path}': {exc}"
                        ),
                    }
            else:
                return {
                    "ok": False,
                    "error": (
                        "plan_dispatch_report requires packet_result "
                        "(inline dict) or packet_result_path (JSON "
                        "file). Both were empty."
                    ),
                }
        return runtime.plan_dispatch_report(
            root,
            session_id=session_id,
            packet_result=packet_result,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Execution Loop Next",
        },
    )
    @timed_sync
    def execution_loop_next(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Return the next execution-loop state for a session plan."""
        return runtime.execution_loop_next(resolve_project_root(), session_id=session_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "ai_plan_overlap",
        },
    )
    @timed_sync
    def ai_plan_overlap(
        session_id: str,
        paused_lane_id: str,
        conflicting_lane_id: str,
        file_path: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Pause a lane when another in-flight lane reports emergent file overlap."""
        return runtime.plan_conductor_report_inflight_overlap(
            resolve_project_root(),
            session_id=session_id,
            paused_lane_id=paused_lane_id,
            conflicting_lane_id=conflicting_lane_id,
            file_path=file_path,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "ai_plan_resume",
        },
    )
    @timed_sync
    def ai_plan_resume(session_id: str, lane_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Resume a paused lane after explicit user override or conflict resolution."""
        return runtime.plan_conductor_resume_lane(
            resolve_project_root(),
            session_id=session_id,
            lane_id=lane_id,
        )

    @server.tool(
        annotations={"destructiveHint": False, "openWorldHint": False, "title": "ai_plan_pause"},
    )
    def ai_plan_pause(session_id: str, lane_id: str, reason: str = "") -> dict[str, Any]:
        """Pause a running lane. The lane will not be dispatched until resumed."""
        return runtime._conductor_state.pause_lane(
            resolve_project_root(),
            session_id,
            lane_id,
            reason=reason,
        )

    @server.tool(
        annotations={"destructiveHint": False, "openWorldHint": False, "title": "ai_plan_expand"},
    )
    def ai_plan_expand(
        session_id: str,
        lane_id: str,
        file_path: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Add a file to a running lane's allowed files. Emits undeclared_file_needed signal."""
        return {
            "session_id": session_id,
            "lane_id": lane_id,
            "file_path": file_path,
            "lane_exact_paths": runtime._conductor_state.expand_lane_scope(
                resolve_project_root(),
                session_id,
                lane_id,
                file_path,
                reason=reason,
            ),
        }

    @server.tool(
        annotations={"destructiveHint": True, "openWorldHint": False, "title": "ai_plan_reopen"},
    )
    def ai_plan_reopen(session_id: str, lane_id: str, reason: str = "") -> dict[str, Any]:
        """Reopen a completed or implementation_done lane for rework."""
        from .types import LaneState

        new_state = runtime._conductor_state.transition_lane(
            resolve_project_root(),
            session_id,
            lane_id,
            LaneState.REOPENED,
        )
        return {
            "session_id": session_id,
            "lane_id": lane_id,
            "new_state": new_state.value,
            "reason": reason,
        }

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "Agent Backends"},
    )
    def ai_backends() -> list[dict[str, str]]:
        """List available worker backends (Claude SDK, OpenAI Codex, OpenCode) on this system."""
        return runtime._agent_expert.available_backends()

    @server.tool(
        annotations={"destructiveHint": True, "openWorldHint": False, "title": "Concurrency Reset"},
    )
    def ai_concurrency_reset(reason: str) -> dict[str, Any]:
        """Force-clear the machine-concurrency registry for this host.

        Use when ai_spawn refuses with `blocked_by=machine_concurrency`
        even though actual live workers are fewer than `live_count`.
        Phantom rows accumulate when MCP crashes before workers' finally
        clauses ran AND pid wasn't recorded (pre-fix legacy rows).
        Re-registration on next spawn restores correct state for any
        actually-live workers.

        Required: `reason` (>=8 chars after trim, journaled for audit).
        """
        from .host_concurrency_store import HostConcurrencyStore
        from .reason_validator import validate_reason

        check = validate_reason(reason)
        if check is not None:
            return check
        r = reason.strip()
        cleared = HostConcurrencyStore().reset()
        try:
            runtime.hub.execution.record_event(
                resolve_project_root(),
                event_kind="machine_concurrency_reset",
                source_kind="conductor_tool",
                capability_name="ai_concurrency_reset",
                action_kind="reset",
                status="success",
                payload={"reason": r, "cleared": cleared},
            )
        except Exception:
            pass
        return {"ok": True, "cleared": cleared, "reason": r}

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "List Backend Models"},
    )
    def ai_models(backend: str) -> dict[str, Any]:
        """List available model slugs for a worker backend.

        backend='opencode' → shells `opencode models` (live; reflects
            this install's enabled providers).
        backend='claude' → hardcoded known Anthropic IDs (claude CLI
            has no --models flag; aliases and full IDs returned).
        backend='codex' → hardcoded OpenAI model IDs (codex CLI has
            no --models flag).

        Returns {backend, models: [slug, ...], source}. On failure,
        models=[] and an `error` key carries the reason.
        """
        from .backend_models import list_backend_models

        return list_backend_models(backend)
        return {"backend": backend, "models": [], "error": "unreachable"}

    # NOTE: sync `agent_spawn_worker` was removed 2026-04-20. It blocked
    # the MCP event loop for the full worker lifetime (minutes) and
    # left conductor agents "Actualizing..." indefinitely. The async
    # variant below is the ONLY agent-facing spawn surface. The
    # service-level `AgentExpertService.spawn_worker` is kept for
    # `spawn_worker_async` to delegate into inside a thread — not
    # exposed as a tool.

    # @server.tool removed (120% clause B): folded into ai_lane(action='spawn').
    # register_impl target (end of module) so the consolidator reaches it.
    def ai_spawn(
        session_id: str,
        lane_id: str,
        backend: str = "claude",
        timeout: int = 600,
        target_project: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        """Spawn a worker agent without blocking the MCP event loop.

        Returns immediately with a worker_id. Poll ai_status(worker_id)
        until state=='done'. When the worker finishes, plan_dispatch_report is
        invoked automatically so conductor lane state stays in sync.

        target_project: name registered via related_project_register.
        When set, the dispatch + worker run against that project's root
        (its own .MEMORY/, its own session_id) instead of the
        conductor's project. The conductor MCP doesn't move; only the
        spawned subprocess's cwd does. Cross-project lane work.

        model: per-spawn model override. Takes precedence over the
        per-session task_routing entry and the conductor.<backend>_model
        config default. Leave empty to fall back to config → CLI default.
        Useful for targeted Haiku/cheap-model dispatch without a config
        write round-trip.
        """
        conductor_root = resolve_project_root()
        if target_project:
            target_root = hub.related.resolve_related_project_path(
                conductor_root,
                target_project,
            )
            if target_root is None:
                return {
                    "success": False,
                    "error": (
                        f"target_project '{target_project}' not registered "
                        f"(or path no longer exists). Call "
                        f"related_project_register(name=..., path=...) "
                        f"first, or related_project_list to see what's known."
                    ),
                }
            # Cross-project execution is a privilege boundary: the target
            # must be commissioned + on the approved-relation allowlist +
            # the caller permitted (solo/dev passthrough, corpo RBAC).
            try:
                from .mcp_server_runtime_helpers import (
                    current_calling_host_session_id,
                )
                from .project_authority import require_cross_project_session

                _xp = require_cross_project_session(
                    conductor_root,
                    target_root,
                    session_id,
                    operation="ai_spawn_cross_project",
                    host_session_id=current_calling_host_session_id(),
                )
            except Exception as _xp_err:
                return {"success": False, "error": f"cross-project gate error: {_xp_err}"}
            if not _xp.get("ok"):
                return {
                    "success": False,
                    "error": (
                        f"cross-project spawn into '{target_project}' refused: {_xp.get('reason')}"
                    ),
                    "blocked_by": _xp.get("blocked_by"),
                }
            project_root = target_root
        else:
            project_root = conductor_root
        dispatch = runtime.plan_dispatch_next(project_root, session_id=session_id, lane_id=lane_id)
        packet = dispatch.get("packet")
        if not packet:
            return {"success": False, "error": "No dispatch packet available", "dispatch": dispatch}
        if model:
            route = packet.get("route")
            if not isinstance(route, dict):
                route = {}
            route["model"] = model
            packet["route"] = route
        handle = runtime._agent_expert.spawn_worker_async(
            project_root,
            packet,
            backend=backend,
            timeout=timeout,
            session_id=session_id,
        )
        return {
            "success": True,
            "target_project": target_project or None,
            "project_root": str(project_root),
            "dispatch_state": dispatch.get("dispatch_state"),
            **handle,
        }

    # @server.tool removed (120% clause B): ai_status / ai_jobs / ai_kill are
    # folded into ai_lane(action='status'|'list'|'kill') as the single conductor
    # surface (no standalone aliases). The functions remain as register_impl
    # targets (see end of this module) so the consolidator's _delegate reaches
    # them via direct-dispatch.
    def ai_status(worker_id: str, verbose: bool = False) -> dict[str, Any]:
        """Poll status of a background worker. Slim by default — pass verbose=True
        for full result (files_changed list, commands_run, raw_output, etc.).
        """
        return runtime._agent_expert.get_worker_status(worker_id, verbose=verbose)

    def ai_jobs(verbose: bool = False) -> list[dict[str, Any]]:
        """List background worker jobs. Slim by default — pass verbose=True for full payloads."""
        return runtime._agent_expert.list_worker_jobs(verbose=verbose)

    def ai_kill(worker_id: str, reason: str) -> dict[str, Any]:
        """SIGTERM a running lane worker. Universal LCD pause primitive.

        Required: `reason` (>=8 chars after trim, journaled for audit).
        Works regardless of backend or auth mode — even subscription Claude
        workers that can't be paused via lane_state gating. Idempotent:
        killing an already-dead worker returns ok without error.
        """
        from .reason_validator import validate_reason

        reason_check = validate_reason(reason)
        if reason_check is not None:
            return reason_check
        r = reason.strip()
        project_root = resolve_project_root()
        jobs = runtime._agent_expert._jobs
        job = jobs.get(worker_id)
        from . import dual_audience as _da

        if job is None:
            return _da.fail_sub(
                tool_name="ai_kill",
                error=f"worker_id not in job table: {worker_id}",
            )
        lane_id = getattr(job, "lane_id", "") or ""
        already_done = bool(getattr(job, "done", False))
        proc = getattr(job, "process", None)
        killed = False
        kill_steps: list[str] = []
        # Phoenix 2026-05-10: previous version called proc.terminate()
        # only — on Windows TerminateProcess hits the parent and does
        # NOT propagate to children (opencode spawns Node + child
        # AIDOCS MCP); POSIX without process-group setup likewise
        # leaves children orphan. Witness w-705f7233ff66 (2026-05-10):
        # kill audit row at 17:53:09 reported state=killed but worker
        # tool calls continued at 17:53:23/29. Now: terminate → wait
        # 5s → escalate to tree-kill (Windows taskkill /F /T or POSIX
        # killpg SIGKILL) so the entire tree dies.
        if proc is not None and not already_done:
            import os as _os_kill
            import subprocess as _sub_kill
            import sys as _sys_kill

            pid = getattr(proc, "pid", None)
            try:
                proc.terminate()
                kill_steps.append("terminate_sent")
            except Exception as exc:
                kill_steps.append(f"terminate_failed:{exc}")
            try:
                proc.wait(timeout=5)
                kill_steps.append("exited_after_terminate")
                killed = True
            except Exception:
                kill_steps.append("escalating_tree_kill")
                if pid:
                    try:
                        if _sys_kill.platform == "win32":
                            _sub_kill.run(
                                ["taskkill", "/F", "/T", "/PID", str(pid)],
                                capture_output=True,
                                timeout=10,
                            )
                            kill_steps.append("taskkill_tree_sent")
                        else:
                            try:
                                import signal as _sig_kill

                                pgid = _os_kill.getpgid(pid)
                                _os_kill.killpg(pgid, _sig_kill.SIGKILL)
                                kill_steps.append("killpg_sigkill_sent")
                            except (ProcessLookupError, OSError):
                                try:
                                    import signal as _sig_kill2

                                    _os_kill.kill(pid, _sig_kill2.SIGKILL)
                                    kill_steps.append("kill_sigkill_sent")
                                except Exception:
                                    pass
                    except Exception as exc2:
                        kill_steps.append(f"tree_kill_failed:{exc2}")
                try:
                    proc.wait(timeout=5)
                    kill_steps.append("exited_after_tree_kill")
                    killed = True
                except Exception:
                    kill_steps.append("still_alive_post_tree_kill")
                    killed = False

        # Update session_lane_agents state + record audit event.
        try:
            import sqlite3 as _sql

            from .execution_index_store import ExecutionIndexStore

            store = ExecutionIndexStore()
            store.init_db(project_root)
            import time as _t

            now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
            with _sql.connect(str(store.db_path(project_root))) as conn:
                # Find the hex registry worker_id that matches this in-memory
                # job via (session_id, lane_id, state='running' latest).
                conn.row_factory = _sql.Row
                if lane_id:
                    row = conn.execute(
                        "SELECT worker_id, session_id FROM session_lane_agents "
                        "WHERE lane_id = ? AND state = 'running' "
                        "ORDER BY started_at DESC LIMIT 1",
                        (lane_id,),
                    ).fetchone()
                    if row is not None:
                        conn.execute(
                            "UPDATE session_lane_agents SET state = 'killed', "
                            "completed_at = ? WHERE worker_id = ?",
                            (now, row["worker_id"]),
                        )
                        conn.commit()
        except Exception:
            # Audit failure shouldn't fail the kill result.
            pass
        try:
            runtime.hub.execution.record_event(
                project_root,
                event_kind="worker_killed",
                source_kind="agent_worker",
                capability_name="ai_kill",
                action_kind="kill",
                target_entity=worker_id,
                status="killed" if killed else "already_done",
                payload={
                    "reason": r,
                    "lane_id": lane_id,
                    "kill_steps": kill_steps,
                },
            )
        except Exception:
            pass
        return _da.ok_sub(
            tool_name="agent_worker_kill",
            structured={
                "worker_id": worker_id,
                "lane_id": lane_id,
                "state": "killed" if killed else "already_done",
                "reason": r,
                "kill_steps": kill_steps,
            },
        )

    # @server.tool removed (120% clause B): folded into ai_lane(action='resume').
    def ai_resume(
        worker_id: str,
        prompt: str = "continue",
        model: str = "",
    ) -> dict[str, Any]:
        """Re-spawn an opencode worker resuming the same opencode session.

        Looks up `host_session_id` in `session_lane_agents` for the given
        prior `worker_id` (typically a recently killed one) and calls
        `resume_opencode_worker` so the LLM picks up its prior in-context
        history via `opencode run --session <host_session_id>`.
        """
        project_root = resolve_project_root()
        from . import dual_audience as _da

        try:
            import sqlite3 as _sql

            from .execution_index_store import ExecutionIndexStore

            store = ExecutionIndexStore()
            store.init_db(project_root)
            with _sql.connect(str(store.db_path(project_root))) as conn:
                conn.row_factory = _sql.Row
                row = conn.execute(
                    "SELECT session_id, lane_id, host_session_id, backend "
                    "FROM session_lane_agents WHERE worker_id = ? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (worker_id,),
                ).fetchone()
        except Exception as exc:
            return _da.fail_sub(
                tool_name="agent_worker_resume",
                error=f"lookup_failed: {exc}",
            )
        if row is None:
            return _da.fail_sub(
                tool_name="agent_worker_resume",
                error=f"worker_id not found in session_lane_agents: {worker_id}",
            )
        host_session_id = (row["host_session_id"] or "").strip()
        if not host_session_id:
            return _da.fail_sub(
                tool_name="agent_worker_resume",
                error=f"no host_session_id captured for {worker_id} — cannot resume",
            )
        backend = (row["backend"] or "").strip().lower()
        if backend and backend != "opencode":
            return _da.fail_sub(
                tool_name="agent_worker_resume",
                error=f"resume only supported for opencode backend, got: {backend}",
            )
        result = runtime._agent_worker.resume_opencode_worker(
            project_root,
            prior_worker_id=worker_id,
            host_session_id=host_session_id,
            session_id=str(row["session_id"] or ""),
            lane_id=str(row["lane_id"] or ""),
            prompt=prompt or "continue",
            model=model or "",
        )
        return _da.ok_sub(
            tool_name="agent_worker_resume",
            structured={
                "prior_worker_id": worker_id,
                "new_worker_id": getattr(result, "worker_id", "") or "",
                "host_session_id": host_session_id,
                "lane_id": str(row["lane_id"] or ""),
                "success": bool(getattr(result, "success", False)),
                "error": getattr(result, "error", "") or "",
            },
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "ai_plan_mark_ready",
        },
    )
    @timed_sync
    def ai_plan_mark_ready(
        session_id: str,
        lane_id: str,
        ready: bool = True,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Mark a contract lane ready so compatible dependent lanes can run."""
        return runtime.plan_conductor_mark_contract_ready(
            resolve_project_root(),
            session_id=session_id,
            lane_id=lane_id,
            ready=ready,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "ai_plan_signal",
        },
    )
    @timed_sync
    def ai_plan_signal(
        session_id: str,
        lane_id: str,
        signal_kind: str,
        target_lane_id: str,
        detail: str = "",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Record a structured signal from one lane about another lane."""
        return runtime.plan_conductor_record_lane_signal(
            resolve_project_root(),
            session_id=session_id,
            lane_id=lane_id,
            signal_kind=signal_kind,
            target_lane_id=target_lane_id,
            detail=detail,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Plan Preflight",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def plan_preflight(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Analyze a session plan before implementation."""
        return runtime.plan_preflight(resolve_project_root(), session_id=session_id)

    @modes(
        begin={
            "required": ["session_id"],
            "optional": [
                "goal",
                "state",
                "upcoming",
                "partial_goals",
                "end_goal",
                "blockers",
                "relevant_files",
                "relevant_commands",
                "relevant_snippets",
                "relevant_snippets_path",
                "session_facts",
                "session_facts_path",
                "constraints",
                "include_code_bundle",
                "include_tests",
            ],
            "desc": "register task start before non-trivial work (goal recommended)",
        },
        update={
            "required": ["session_id"],
            "optional": [
                "state",
                "upcoming",
                "partial_goals",
                "end_goal",
                "blockers",
                "relevant_files",
                "relevant_commands",
                "relevant_snippets",
                "session_facts",
                "constraints",
                "include_code_bundle",
                "include_tests",
                "summary_only",
            ],
            "desc": "record progress on the open task at each step",
        },
        complete={
            "required": ["session_id", "result_summary"],
            "optional": [
                "next_status",
                "verification_evidence",
                "include_code_bundle",
                "include_tests",
            ],
            "desc": "close the task with result_summary + verification evidence",
        },
        status={"required": ["session_id"], "optional": [], "desc": "peek at the open task"},
    )
    @server.tool(
        annotations={"destructiveHint": True, "openWorldHint": False, "title": "ai_task"},
        meta={"anthropic/alwaysLoad": True},
    )
    def ai_task(
        mode: str,
        session_id: str,
        goal: str | None = None,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        relevant_snippets_path: str = "",
        session_facts: list[str] | None = None,
        session_facts_path: str = "",
        constraints: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        summary_only: bool = True,
        result_summary: str = "",
        next_status: str = "done",
        verification_evidence: dict[str, Any] | None = None,
    ) -> Any:
        """Unified task-lifecycle tool — one tool, four modes (king directive 2026-05-12).

        mode='begin'    — register a new task. Required: session_id. Most context
                         params (goal, state, upcoming, relevant_files, ...) optional.
        mode='update'   — record progress on the active task. Required: session_id.
        mode='complete' — close the active task with a result summary.
                         Required: session_id, result_summary.
        mode='status'   — read-only quick peek at the active task.
                         Required: session_id.

        Per-mode required-sets enforced by @modes; runtime branches by `mode`.
        Lifecycle gates (session-bind match, lane-worker §VIII capture for complete)
        run inside the per-mode branches via the helper functions.
        """
        m = (mode or "").strip().lower()
        if m == "begin":
            return task_begin(
                session_id=session_id,
                goal=goal,
                state=state,
                upcoming=upcoming,
                partial_goals=partial_goals,
                end_goal=end_goal,
                blockers=blockers,
                relevant_files=relevant_files,
                relevant_commands=relevant_commands,
                relevant_snippets=relevant_snippets,
                relevant_snippets_path=relevant_snippets_path,
                session_facts=session_facts,
                session_facts_path=session_facts_path,
                constraints=constraints,
                include_code_bundle=include_code_bundle,
                include_tests=include_tests,
            )
        if m == "update":
            return task_update(
                session_id=session_id,
                state=state,
                upcoming=upcoming,
                partial_goals=partial_goals,
                end_goal=end_goal,
                blockers=blockers,
                relevant_files=relevant_files,
                relevant_commands=relevant_commands,
                relevant_snippets=relevant_snippets,
                session_facts=session_facts,
                constraints=constraints,
                include_code_bundle=include_code_bundle,
                include_tests=include_tests,
                summary_only=summary_only,
            )
        if m == "complete":
            return task_complete(
                session_id=session_id,
                result_summary=result_summary,
                next_status=next_status,
                verification_evidence=verification_evidence,
                include_code_bundle=include_code_bundle,
                include_tests=include_tests,
            )
        if m == "status":
            return task_status(session_id=session_id)
        return {"error": f"unknown mode: {mode!r} (valid: begin|update|complete|status)"}

    # Internal helper. Tool surface removed 2026-05-12 — ai_task(mode='begin').
    def task_begin(
        session_id: str,
        goal: str | None = None,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        relevant_snippets_path: str = "",
        session_facts: list[str] | None = None,
        session_facts_path: str = "",
        constraints: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> Any:
        """Begin work in a selected session and update session/context state.

        Path-or-inline rule (2026-04-20): pass relevant_snippets_path
        / session_facts_path pointing at a newline-delimited file
        instead of retyping long content inline. Blank lines are
        dropped, lines prefixed with "- " are unwrapped. If both the
        inline list AND the path are provided, the inline list wins
        (explicit agent intent beats disk). Paths resolve under
        project_root unless absolute.

        Session-bind check (#71, 2026-04-27): if session_id differs
        from the conductor's currently-bound session, refuse with a
        clear envelope. Task lifecycle keys to the bound session, not
        the parameter; mismatch causes downstream "no active task"
        errors that mislead diagnostic.
        """
        root = resolve_project_root()
        bind_err = _check_session_bind_match(
            hub,
            root,
            session_id,
            "task_begin",
        )
        if bind_err is not None:
            return bind_err
        resolved_snippets = _resolve_list_or_path(
            inline=relevant_snippets,
            path=relevant_snippets_path,
            project_root=root,
            field_name="relevant_snippets",
        )
        if isinstance(resolved_snippets, dict):
            return resolved_snippets  # error payload
        resolved_facts = _resolve_list_or_path(
            inline=session_facts,
            path=session_facts_path,
            project_root=root,
            field_name="session_facts",
        )
        if isinstance(resolved_facts, dict):
            return resolved_facts  # error payload
        result = runtime.task_begin(
            root,
            session_id=session_id,
            goal=goal,
            state=state,
            upcoming=upcoming,
            partial_goals=partial_goals,
            end_goal=end_goal,
            blockers=blockers,
            relevant_files=relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=resolved_snippets,
            session_facts=resolved_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )
        return _trim_lifecycle_result(result, include_code_bundle, tool="task_begin")

    # Internal helper. Tool surface removed 2026-05-12 — ai_task(mode='update').
    def task_update(
        session_id: str,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        summary_only: bool = True,
    ) -> Any:
        """Update an active task session and optional context state.

        Session-bind check (#71, 2026-04-27): refuse mismatched session.
        """
        root = resolve_project_root()
        bind_err = _check_session_bind_match(
            hub,
            root,
            session_id,
            "task_update",
        )
        if bind_err is not None:
            return bind_err
        result = runtime.task_update(
            root,
            session_id=session_id,
            state=state,
            upcoming=upcoming,
            partial_goals=partial_goals,
            end_goal=end_goal,
            blockers=blockers,
            relevant_files=relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=relevant_snippets,
            session_facts=session_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
            summary_only=summary_only,
        )
        return _trim_lifecycle_result(result, include_code_bundle, tool="task_update")

    # Internal helper. Tool surface removed 2026-05-12 — ai_task(mode='complete').
    def task_complete(
        session_id: str,
        result_summary: str,
        next_status: str = "done",
        verification_evidence: dict[str, Any] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> Any:
        """Complete task work in a session and update session state.

        Session-bind check (#71, 2026-04-27): refuse mismatched session.
        """
        root = resolve_project_root()
        bind_err = _check_session_bind_match(
            hub,
            root,
            session_id,
            "task_complete",
        )
        if bind_err is not None:
            return bind_err
        # Phoenix 2026-05-08: §VIII enforcement via task_complete
        # capture. Lane workers (AIDOCS_EXPERT_LANE_ID env set) don't
        # complete directly — they capture into a pending review row,
        # exit cleanly, resumed by the conductor only on deny.
        # Conductors (no env) skip this branch entirely.
        capture = _capture_lane_worker_task_complete(
            hub,
            root,
            session_id,
            result_summary=result_summary,
            verification_evidence=verification_evidence,
        )
        if capture is not None:
            return capture
        result = runtime.task_complete(
            root,
            session_id=session_id,
            result_summary=result_summary,
            next_status=next_status,
            verification_evidence=verification_evidence,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )
        return _trim_lifecycle_result(result, include_code_bundle, tool="task_complete")

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Lane Workers Status Summary",
        },
    )
    @timed_sync
    def ai_lane_summary(
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate lane-worker states across sessions.

        Returns by_state (spawned/running/done/crashed counts),
        by_lane breakdown, and the longest-running worker. Optional
        session_id scope.
        """
        return runtime.lane_workers_status_summary(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Recent Commits Touching File",
        },
    )
    @timed_sync
    def recent_commits_touching_file(
        file_path: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Git history for a single file.

        Returns newest-first list of {sha, date, author, subject}.
        Path must be relative to project root and inside the tree.
        """
        return runtime.recent_commits_touching_file(
            resolve_project_root(),
            file_path=file_path,
            limit=int(limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Progress Dashboard",
        },
    )
    @timed_sync
    def project_progress_dashboard() -> dict[str, Any]:
        """One-call conductor overview: roadmap + audit + backlog +
        workers + freshness. Includes a rolled-up headline for
        status-bar rendering and a full drill-down for deeper reviews.
        """
        return runtime.project_progress_dashboard(resolve_project_root())

    # list_protected_files: folded into ai_protect(mode="list") at
    # server_code_edit_tools.py so agents have a single surface for
    # the DO-NOT-TOUCH lifecycle (add/remove/list/add_batch). The
    # runtime method runtime.list_protected_files is still available
    # for internal callers / dashboard.

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Roadmap Layer Progress",
        },
    )
    @timed_sync
    def roadmap_layer_progress(
        roadmap_path: str = ".MEMORY/roadmaps/roadmap.md",
    ) -> dict[str, Any]:
        """Parse roadmap.md and surface per-layer checkbox completion.

        Expected shape: `# Layer N ...` headers with `- [ ]` / `- [x]`
        bullets under each. Returns per-layer {checked, unchecked,
        percent} plus overall rollup.
        """
        return runtime.roadmap_layer_progress(
            resolve_project_root(),
            roadmap_path=roadmap_path,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Untouched Code Files",
        },
    )
    @timed_sync
    def untouched_code_files(limit: int = 100) -> dict[str, Any]:
        """Indexed source files with no edit_history entries.

        Cold twin of files_touched_heatmap — surfaces files nobody has
        edited through AIDOCS. Sorted by line_count desc so the most-
        impactful dead-code candidates surface first.
        """
        return runtime.untouched_code_files(
            resolve_project_root(),
            limit=int(limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Handoff Completeness",
        },
    )
    @timed_sync
    def session_handoff_completeness(session_id: str) -> dict[str, Any]:
        """Score a session's HANDOFF.md shape (0-100).

        Expected sections: Current State, What Was Done, What's Next,
        Key Decisions, Open Questions, Freshness. A section counts as
        filled if any line has content beyond the "-" placeholder.
        """
        return runtime.session_handoff_completeness(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Dependency Freshness",
        },
    )
    @timed_sync
    def dependency_freshness() -> dict[str, Any]:
        """Age of dependency lockfiles + package manifests.

        Walks known manifests (requirements.txt, pyproject.toml,
        package.json, Cargo.lock, go.sum, etc) at project root and
        first-level subdirs. Banded fresh (<30d) / aging (30-180d) /
        stale (>180d). Sorted oldest first.
        """
        return runtime.dependency_freshness(resolve_project_root())

    # rule_orphan_finder (markdown cross-link scan) was REMOVED 2026-05-21
    # under the no-file-layer doctrine; replaced by the SQL/routing-graph
    # diagnostic below.
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Memory Routing Orphans",
        },
    )
    @timed_sync
    def memory_routing_orphans() -> dict[str, Any]:
        """Rule/standards/system/domains memory entries that NOTHING can
        reach — no inbound memory_links AND no memory_routes entry (the
        topic/action router won't surface them). Likely dead/forgotten.

        SQL/routing-graph diagnostic over the sqlite memory index — no
        .MEMORY/*.md scan (replaces the retired rule_orphan_finder).
        """
        return runtime.hub.index.routing_orphans(resolve_project_root())

    # ── Workflow definitions (Stage 5c edit surface) ─────────────────
    # The canonical SOURCE for workflow rules/actions lives in the
    # workflow_definitions sqlite table (replacing the markdown files).
    # WRITES are operator-gated (host-binding RBAC admin.manage_config, or
    # a dev-flavor local super-admin); the agent cannot self-edit the
    # control plane. The compile-time heuristic judge still rejects
    # destructive action templates regardless. list/read is open.
    def _workflow_admin_authorized(
        project_root,
        operator_token: str = "",
    ) -> tuple[bool, str]:
        from .enforcement import is_dev_flavor
        from .operator_auth_service import OperatorAuthService
        from .permission_catalog import PERM_ADMIN_MANAGE_CONFIG

        if is_dev_flavor(project_root):
            return True, "local-operator-dev"
        svc = OperatorAuthService()
        ctx = None
        if operator_token:
            try:
                ctx = svc.authenticate(
                    operator_token,
                    project_root,
                    source="dashboard",
                )
            except Exception:
                ctx = None
        if ctx is None:
            try:
                from .mcp_server_runtime_helpers import (
                    current_calling_host_session_id,
                )

                hsid = current_calling_host_session_id() or ""
                if hsid:
                    ctx = svc.resolve_operator_context_from_host_session(
                        hsid,
                        project_root,
                    )
            except Exception:
                ctx = None
        if ctx is None:
            return False, "no_operator"
        if not svc.require_permission(
            ctx,
            PERM_ADMIN_MANAGE_CONFIG,
            project_root,
        ):
            return False, "missing_permission"
        return True, (getattr(ctx, "email", "") or ctx.user_id or "operator")

    def _workflow_recompile(project_root) -> dict[str, Any]:
        # Recompile failure must be surfaced, not hidden as success: on error
        # we return recompile_ok=False + recompile_error so callers can expose
        # the degraded state rather than reporting a clean recompile.
        try:
            c = runtime.hub.workflow.compile_project_rules(project_root)
            return {
                "recompile_ok": True,
                "recompiled_action_count": c.get("action_count", 0),
                "definitions_source": c.get("definitions_source"),
                "unsupported_count": c.get("unsupported_count", 0),
            }
        except Exception as exc:
            return {
                "recompile_ok": False,
                "recompile_error": str(exc),
                "recompiled_action_count": 0,
            }

    def _workflow_mutation_service():
        from .workflow_definition_mutation_service import (
            WorkflowDefinitionMutationService,
        )

        return WorkflowDefinitionMutationService()

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Workflow Definitions List",
        },
    )
    @timed_sync
    def workflow_definition_list(kind: str = "") -> dict[str, Any]:
        """List active workflow rule/action definitions (the SQL source the
        compiler reads). Read-only — no auth required.
        """
        from . import workflow_definitions_store as wd

        root = resolve_project_root()
        defs = wd.list_active(root, kind or None)
        return {"ok": True, "definitions": defs, "count": len(defs)}

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "Workflow Definition Add",
        },
    )
    @timed_sync
    def workflow_definition_add(
        kind: str,
        body: str,
        operator_token: str = "",
    ) -> dict[str, Any]:
        """Add a workflow rule/action definition (operator-gated). Triggers
        a recompile; the heuristic judge still vets compiled actions.
        """
        root = resolve_project_root()
        ok, who = _workflow_admin_authorized(root, operator_token)
        if not ok:
            return {"ok": False, "blocked_by": "operator_auth", "reason": who}
        return _workflow_mutation_service().add(
            root,
            kind=kind,
            body=body,
            approver=who,
            recompile_fn=lambda: _workflow_recompile(root),
        )

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "Workflow Definition Update",
        },
    )
    @timed_sync
    def workflow_definition_update(
        def_id: int,
        body: str = "",
        status: str = "",
        operator_token: str = "",
    ) -> dict[str, Any]:
        """Update a workflow definition's body and/or status (operator-
        gated). Triggers a recompile.
        """
        root = resolve_project_root()
        ok, who = _workflow_admin_authorized(root, operator_token)
        if not ok:
            return {"ok": False, "blocked_by": "operator_auth", "reason": who}
        return _workflow_mutation_service().update(
            root,
            def_id=int(def_id),
            body=body or None,
            status=status or None,
            approver=who,
            recompile_fn=lambda: _workflow_recompile(root),
        )

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "Workflow Definition Remove",
        },
    )
    @timed_sync
    def workflow_definition_remove(
        def_id: int,
        reason: str = "",
        operator_token: str = "",
    ) -> dict[str, Any]:
        """Retire a workflow definition (tombstone; operator-gated).
        Triggers a recompile.
        """
        root = resolve_project_root()
        ok, who = _workflow_admin_authorized(root, operator_token)
        if not ok:
            return {"ok": False, "blocked_by": "operator_auth", "reason": who}
        return _workflow_mutation_service().remove(
            root,
            def_id=int(def_id),
            reason=reason,
            approver=who,
            recompile_fn=lambda: _workflow_recompile(root),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Config Validation Report",
        },
    )
    @timed_sync
    def config_validation_report() -> dict[str, Any]:
        """Sanity-check every effective_config leaf vs default.

        Flags type mismatches (str where bool expected), negative
        timeouts, suspiciously-large second counts (>86400). Skips
        action_hooks/languages/interaction noise. Per-issue
        {path, current, expected_type, issue}.
        """
        return runtime.config_validation_report(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Plan Step Drift",
        },
    )
    @timed_sync
    def plan_step_drift(session_id: str) -> dict[str, Any]:
        """Unchecked PLAN items that never matched a journal intent.

        Surfaces steps that were skipped or forgotten — the PLAN
        said "do X" but no task_lifecycle entry for X ever fired.
        Useful for session postmortems.
        """
        return runtime.plan_step_drift(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Memory Doc Word Count",
        },
    )
    @timed_sync
    def memory_doc_word_count(memory_root: str = ".MEMORY") -> dict[str, Any]:
        """Per-doc word count with hygiene bands.

        sparse (<30 words — placeholder), healthy (30-500), bloated
        (>500 — split candidate). Sorted longest first so review
        focuses on the obvious offenders.
        """
        return runtime.memory_doc_word_count(
            resolve_project_root(),
            memory_root=memory_root,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "File Age Histogram",
        },
    )
    @timed_sync
    def file_age_histogram() -> dict[str, Any]:
        """Distribution of indexed-file mtimes by age bucket.

        Answers "how much of this codebase is old?" — 6 recency
        bands (24h / 7d / 30d / 90d / 1yr / older) with count +
        fraction per bucket. Sparkline-friendly.
        """
        return runtime.file_age_histogram(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Compare",
        },
    )
    @timed_sync
    def session_compare(session_a: str, session_b: str) -> dict[str, Any]:
        """Diff two session journals: action_kind counts + unique-per-side.

        "Did the re-run match the original?" verification surface —
        returns {by_kind: [...], only_in_a, only_in_b, a_total, b_total}.
        """
        return runtime.session_compare(
            resolve_project_root(),
            session_a=session_a,
            session_b=session_b,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Most Denied Commands",
        },
    )
    @timed_sync
    def most_denied_commands(limit: int = 20) -> dict[str, Any]:
        """Top shell commands by gate-block count.

        Aggregates payload_json.command from raw_shell_block,
        bash_policy_block, heuristic_judge_block, test_retry_block
        events. Helps operators see patterns ("agents keep trying
        find -delete") and decide whether to relax or train around.
        """
        return runtime.most_denied_commands(
            resolve_project_root(),
            limit=int(limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Inactive Session Nudge",
        },
    )
    @timed_sync
    def inactive_session_nudge(stale_after_days: int = 7) -> dict[str, Any]:
        """Non-terminal sessions with no journal activity for N days.

        Complement to list_archive_candidates (DONE sessions). Finds
        ACTIVE sessions that have gone quiet — the "stuck / forgotten"
        bucket that needs a nudge. Sorted oldest-activity first.
        """
        return runtime.inactive_session_nudge(
            resolve_project_root(),
            stale_after_days=int(stale_after_days),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Edit Session Overlap",
        },
    )
    @timed_sync
    def edit_session_overlap(window_hours: int = 24) -> dict[str, Any]:
        """Files edited by multiple sessions within `window_hours`.

        Conflict-risk surface — parallel sessions touching the same
        file risk merge conflicts or stale reads. Sorted by severity
        (distinct sessions desc, then edit count desc).
        """
        return runtime.edit_session_overlap(
            resolve_project_root(),
            window_hours=int(window_hours),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Recent Errors Scan",
        },
    )
    @timed_sync
    def recent_errors_scan(limit: int = 50) -> dict[str, Any]:
        """Execution events with status=error (or error*/failed).

        Forensic surface for "what's been erroring today?" — newest
        first, capped by limit.
        """
        return runtime.recent_errors_scan(
            resolve_project_root(),
            limit=int(limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Owner Summary",
        },
    )
    @timed_sync
    def session_owner_summary() -> dict[str, Any]:
        """Aggregate sessions per owner.

        Returns {owners: [{owner, total, active, done, blocked}, ...]}
        sorted by total desc. Useful for multi-operator projects to
        see who's working on what.
        """
        return runtime.session_owner_summary(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Tool Use Leaderboard",
        },
    )
    @timed_sync
    def tool_use_leaderboard(limit: int = 30) -> dict[str, Any]:
        """Most-called MCP tools from the execution-events trail.

        Aggregates tool_call_started events by capability_name. Useful
        for observing agent habits — which tools dominate, which sit
        idle despite being registered.
        """
        return runtime.tool_use_leaderboard(
            resolve_project_root(),
            limit=int(limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Task Progress Streak",
        },
    )
    @renders_as("status", title="task progress streak")
    @timed_sync
    def task_progress_streak(session_id: str | None = None) -> Any:
        """Consecutive-days streak of task_complete activity.

        Pass session_id to scope; default is project-wide. Returns
        current_streak, longest_streak, last_active_date, and the
        last 30 active dates for sparkline rendering.
        """
        return runtime.task_progress_streak(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Backlog Inbox",
        },
    )
    @renders_as("list", title="backlog inbox")
    @timed_sync
    def backlog_inbox(limit: int = 20) -> Any:
        """Top unchecked `- [ ]` lane items across all session PLAN.md
        files (skips done/abandoned/closed sessions). Useful for
        "what should I work on next?" surfaces.
        """
        return runtime.backlog_inbox(
            resolve_project_root(),
            limit=int(limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Task Velocity",
        },
    )
    @renders_as("status", title="task velocity")
    @timed_sync
    def task_velocity(session_id: str) -> Any:
        """Tasks-per-day + completion-ratio for a session.

        Counts task_lifecycle + task_complete entries, computes
        days_active (last - first activity), returns velocity
        metrics. Useful for stale-session detection.
        """
        return runtime.task_velocity(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Recent Denials For Session",
        },
    )
    @renders_as("list", title="recent denials")
    @timed_sync
    def recent_denials_for_session(
        session_id: str,
        limit: int = 50,
    ) -> Any:
        """Per-session gate-denial events ordered newest first.

        Forensic surface for "why did this lane keep getting blocked?"
        Returns {tier, timestamp} pairs scoped to the session.
        """
        return runtime.recent_denials_for_session(
            resolve_project_root(),
            session_id=session_id,
            limit=int(limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Hot Files With No Test",
        },
    )
    @timed_sync
    def hot_files_with_no_test(
        heatmap_limit: int = 100,
        untested_limit: int = 200,
    ) -> dict[str, Any]:
        """Files that are both heavily edited AND lack a matching test.

        The intersection of the cross-session edit heatmap and the
        untested-files index. Highest regression-risk surface.
        """
        return runtime.hot_files_with_no_test(
            resolve_project_root(),
            heatmap_limit=int(heatmap_limit),
            untested_limit=int(untested_limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Size Report",
        },
    )
    @timed_sync
    def project_size_report() -> dict[str, Any]:
        """One-call size report: indexed_files, total_lines, languages
        (top 5 by line count), session_count, edit_count_lifetime,
        denial_count_lifetime. Single read per store, no per-file work.
        """
        return runtime.project_size_report(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Task Open or Blocked",
        },
    )
    @timed_sync
    def task_open_or_blocked() -> dict[str, Any]:
        """Sessions in non-terminal state, split open vs blocked.

        Returns {open: [...], blocked: [...]} — blocked surfaces
        sessions whose Status mentions blocked OR whose Blockers
        section has content. Useful for "what needs my attention?"
        dashboard widgets.
        """
        return runtime.task_open_or_blocked(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Audit Snapshot",
        },
    )
    @timed_sync
    def project_audit_snapshot() -> dict[str, Any]:
        """One-call composite of every guardrail + health signal.

        Replaces 8+ separate audit calls with one — health,
        reserved_filenames, memory_shape, memory_content, memory_stale,
        archive_candidates, denial_tiers, open_or_blocked — each
        best-effort so partial breakage doesn't kill the audit. The
        `headline` field carries the rolled-up dashboard banner.
        """
        return runtime.project_audit_snapshot(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Files Touched Heatmap",
        },
    )
    @timed_sync
    def files_touched_heatmap(limit: int = 50) -> dict[str, Any]:
        """Cross-session edit-frequency heatmap.

        Aggregates the audit trail across all sessions to surface
        churn hotspots: which files keep getting rewritten? Returns
        sorted list with edit_count + session_count + last_edit.
        """
        return runtime.files_touched_heatmap(
            resolve_project_root(),
            limit=int(limit),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Config Diff From Default",
        },
    )
    @timed_sync
    def config_diff_from_default() -> dict[str, Any]:
        """Operator-overridden settings vs shipped defaults.

        Returns flat list of {path, current, default} for every
        setting that differs. Skips noisy overlays (action_hooks,
        languages, interaction templates) so the diff stays focused
        on what the operator actually changed.
        """
        return runtime.config_diff_from_default(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Workflow Step Chronograph",
        },
    )
    @timed_sync
    def workflow_step_chronograph(session_id: str) -> dict[str, Any]:
        """Action-kind sequence + per-kind timing stats for a session.

        Returns a run-length-compressed sequence (e.g.
        `["task_lifecycle", "12xtask_progress", "task_complete"]`) plus
        per-kind {count, mean_gap_seconds}. Surfaces loops + bottlenecks.
        """
        return runtime.workflow_step_chronograph(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Denial Trend (24h)",
        },
    )
    @timed_sync
    def denial_trend_24h(bucket_hours: int = 1) -> dict[str, Any]:
        """Rolling 24-hour denial histogram bucketed by hour.

        Builds on denial_tier_stats — returns N buckets covering the
        last 24h, each with {bucket_start, total, by_tier}. Default
        bucket = 1h → 24 buckets, suitable for a dashboard sparkline.
        """
        return runtime.denial_trend_24h(
            resolve_project_root(),
            bucket_hours=int(bucket_hours),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Export Markdown",
        },
    )
    @timed_sync
    def session_export_markdown(
        session_id: str,
        include_journal: bool = True,
        include_plan: bool = True,
        include_handoff: bool = True,
        max_journal_entries: int = 50,
    ) -> dict[str, Any]:
        """Bundle a session (SESSION + PLAN + journal + handoff) into one
        self-contained markdown blob suitable for paste/share/archive.
        """
        return runtime.session_export_markdown(
            resolve_project_root(),
            session_id=session_id,
            include_journal=bool(include_journal),
            include_plan=bool(include_plan),
            include_handoff=bool(include_handoff),
            max_journal_entries=int(max_journal_entries),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Task Breadcrumbs",
        },
    )
    @timed_sync
    def task_breadcrumbs(session_id: str, last_n: int = 10) -> dict[str, Any]:
        """Recent decisions for a session in compact "Nm ago" form.

        Designed for status-bar / hover-tooltip surfaces that need
        "10m ago: completed login flow tests" without parsing journal
        timestamps client-side.
        """
        return runtime.task_breadcrumbs(
            resolve_project_root(),
            session_id=session_id,
            last_n=int(last_n),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Memory Content Check (G4-G8)",
        },
    )
    @timed_sync
    def memory_content_check(memory_root: str = ".MEMORY") -> dict[str, Any]:
        """Layer 7 G4-G8 in one traversal: tabular-code-inventory (G4),
        feedback-log (G5), bug-report (G6), wrong-project reference (G7),
        trivial-size + duplicate-basename (G8). Skips sessions/archive/
        .aidocs/.index dirs.
        """
        return runtime.memory_content_check(
            resolve_project_root(),
            memory_root=memory_root,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Memory Shape Check (G2 + G3)",
        },
    )
    @timed_sync
    def memory_shape_check(memory_root: str = ".MEMORY") -> dict[str, Any]:
        """Layer 7 G2 + G3: detect agent-exploration headers (G2) and
        plan-shape misuse (G3) inside the memory tree. Skips dirs where
        these shapes are expected (sessions/, archive/, .aidocs/,
        .index/, roadmaps/ for G3).
        """
        return runtime.memory_shape_check(
            resolve_project_root(),
            memory_root=memory_root,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Edit Rollback Batch",
        },
    )
    @timed_sync
    def edit_rollback_batch(
        session_id: str | None = None,
        file_path: str | None = None,
        last_n: int = 10,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Roll back the last N edits in one call.

        dry_run=True (default) returns the planned edit_ids without
        touching files. Pass session_id/file_path to scope the roll-back
        target.
        """
        return runtime.edit_rollback_batch(
            resolve_project_root(),
            session_id=session_id,
            file_path=file_path,
            last_n=int(last_n),
            dry_run=bool(dry_run),
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Archive Sessions Now",
        },
    )
    @timed_sync
    def archive_sessions_now(
        session_ids: list[str] | None = None,
        stale_after_days: int = 30,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Move stale done-sessions into .MEMORY/archive/sessions/.

        dry_run=True (default) previews without disk changes. Pass
        explicit session_ids to override the staleness filter.
        """
        root = resolve_project_root()
        # RBAC enforcement (2026-04-21): admin.manage_sessions at
        # global scope. dry_run also requires the permission — viewers
        # shouldn't be able to probe session staleness either.
        _rbac = hub.require_permission(
            root,
            "admin.manage_sessions",
            scope_type="global",
            scope_id=None,
            tool_name="archive_sessions_now",
            extra_payload={
                "session_ids_count": len(session_ids or []),
                "dry_run": bool(dry_run),
            },
        )
        if not _rbac["ok"]:
            return _rbac
        return runtime.archive_sessions_now(
            root,
            session_ids=session_ids,
            stale_after_days=int(stale_after_days),
            dry_run=bool(dry_run),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Reserved Filename Check (Memory G1)",
        },
    )
    @timed_sync
    def reserved_filename_check(memory_root: str = ".MEMORY") -> dict[str, Any]:
        """Layer 7 G1: catch reserved-filename violations under .MEMORY.

        Reserved names (INDEX.md, SESSION.md, PLAN.md, journal.md,
        context.md) must live at their canonical paths — anything else
        matching those names is an accidental scaffold worth flagging.
        Empty violations list = clean.
        """
        return runtime.reserved_filename_check(
            resolve_project_root(),
            memory_root=memory_root,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Memory Stale Finder",
        },
    )
    @timed_sync
    def memory_stale_finder(
        stale_after_days: int = 90,
        memory_root: str = ".MEMORY",
    ) -> dict[str, Any]:
        """Memory files unedited for N days (default 90).

        Skips sessions/archive/.aidocs/.index/config dirs. Sorted oldest
        first so review prioritization is obvious.
        """
        return runtime.memory_stale_finder(
            resolve_project_root(),
            stale_after_days=int(stale_after_days),
            memory_root=memory_root,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Edit Diff Summary",
        },
    )
    @timed_sync
    def edit_diff_summary(session_id: str) -> dict[str, Any]:
        """Per-file edit/line-add/line-remove counts for a session.

        Conservative diff approximation from the audit trail. Sorted
        by total impact (edits + adds + removes) descending. Pairs
        with files_touched for PR-style "+N -M in K files" surfaces.
        """
        return runtime.edit_diff_summary(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Health Score",
        },
    )
    @timed_sync
    def project_health_score() -> dict[str, Any]:
        """Composite 0-100 project health (index + coverage + sessions
        + denials). Returns headline score plus per-component breakdown
        so dashboards can chart the trend AND drill in.
        """
        return runtime.project_health_score(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "List Archive Candidates",
        },
    )
    @timed_sync
    def list_archive_candidates(stale_after_days: int = 30) -> dict[str, Any]:
        """Sessions ripe for archive: status=done/abandoned/closed + no
        edits newer than `stale_after_days`. Sorted oldest-edit first
        so the operator can review the longest-stale ones first.
        """
        return runtime.list_archive_candidates(
            resolve_project_root(),
            stale_after_days=int(stale_after_days),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Freshness (Heartbeat)",
        },
    )
    @timed_sync
    def project_freshness() -> dict[str, Any]:
        """One-call dashboard heartbeat.

        Replaces 4-5 separate calls (index_status, edit_history_list,
        denial_tier_stats, project_list_sessions) with a single read.
        Returns indexed_files, last_index_sync, edit_count_24h,
        total_denials, denial_tiers_active, session_count_total/active.
        """
        return runtime.project_freshness(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Session Timeline",
        },
    )
    @timed_sync
    def session_timeline(
        session_id: str,
        action_kinds: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Chronological view of task-lifecycle events for a session.

        Default filter shows task_lifecycle/task_progress/task_complete.
        Pass action_kinds=[] to surface every journal action_kind. Newest
        first; capped by limit.
        """
        return runtime.session_timeline(
            resolve_project_root(),
            session_id=session_id,
            action_kinds=action_kinds,
            limit=int(limit),
        )

    # Internal helper. Tool surface removed 2026-05-12 — ai_task(mode='status').
    @renders_as("status", title="task")
    @timed_sync
    def task_status(session_id: str) -> Any:
        """Read-only quick peek at the active task in a session.

        Returns a small dict (typically <500B) with goal, current state,
        partial goals, blockers, lane scope. No side effects — useful
        for status bars, dashboards, monitors. Pairs with task_begin/
        task_update which write but never read back the current shape.
        """
        return runtime.task_status(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Verification Gate",
        },
    )
    @timed_sync
    def verification_gate(
        session_id: str,
        lane_id: str | None = None,
        verification_evidence: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Return runtime-owned verification status for a session or lane."""
        return runtime.verification_gate(
            resolve_project_root(),
            session_id=session_id,
            lane_id=lane_id,
            verification_evidence=verification_evidence,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Update Roadmap Feedback",
        },
    )
    def roadmap_feedback_update(step_text: str, feedback: str) -> dict[str, Any]:
        """Update a pending roadmap step after user feedback."""
        return runtime.update_roadmap_feedback_state(
            resolve_project_root(),
            step_text=step_text,
            feedback=feedback,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "List Planning Docs",
        },
    )
    def planning_docs_list() -> dict[str, Any]:
        """List all planning documents (roadmaps, plans, specs) with checkbox status summary."""
        docs = hub.sessions.list_planning_docs(resolve_project_root())
        return {"docs": docs, "total": len(docs)}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Mark Planning Step",
        },
    )
    def planning_step_mark(path: str, line_number: int, status: str = "done") -> dict[str, Any]:
        """Toggle a checkbox in a planning doc. Status: done, open, skip, in_progress, blocked."""
        return hub.sessions.mark_planning_step(resolve_project_root(), path, line_number, status)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Normalize Plan Prose",
        },
    )
    def plan_normalize_prose(session_id: str) -> dict[str, Any]:
        """Preserve prose-only plan additions and append normalized steps awaiting feedback."""
        return hub.sessions.normalize_plan_feedback_sections(
            resolve_project_root(),
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Normalize Session Artifacts",
        },
    )
    def session_artifacts_normalize(session_id: str) -> dict[str, Any]:
        """Normalize explicit session artifacts and report changed vs untouched items."""
        return hub.sessions.normalize_session_artifacts(
            resolve_project_root(),
            session_id=session_id,
        )

    # ── C.20 direct registry dispatch (king directive 2026-05-29) ──
    #
    # Doctrine: the consolidator ai_worker(action="status"/"list"/
    #           "kill"/"resume") in tool_interface dispatches via
    #           _delegate(name) which historically created a NEW MCP
    #           server and round-tripped through srv.call_tool to
    #           invoke these closures. We now also register each
    #           closure in tool_interface._IMPLS so _delegate calls
    #           them directly, in-process, with the same captured
    #           scope (runtime / hub / project_root_resolver) as the
    #           server registration.
    # Why:      removes ~150ms round-trip per consolidator call AND
    #           makes the call graph greppable — anyone reading
    #           tool_interface.ai_worker can now follow ai_status to
    #           its real impl without spelunking create_server.
    # Apply:    register_impl is idempotent; the latest create_server
    #           invocation wins. test_c20_direct_dispatch asserts
    #           parity between the direct path and the server path.
    from . import tool_interface as _ti_c20

    _ti_c20.register_impl("ai_spawn", ai_spawn)
    _ti_c20.register_impl("ai_status", ai_status)
    _ti_c20.register_impl("ai_jobs", ai_jobs)
    _ti_c20.register_impl("ai_kill", ai_kill)
    _ti_c20.register_impl("ai_resume", ai_resume)
