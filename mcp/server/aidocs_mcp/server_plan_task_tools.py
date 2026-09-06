from __future__ import annotations

from pathlib import Path
from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as

# One-line directive appended to task_begin so a fresh agent knows
# the lifecycle protocol without needing to re-read workflow rules.
# The agent writes task_update at each meaningful step and
# task_complete when done; this string is the only useful thing to
# echo back from task_begin.
_TASK_BEGIN_DIRECTIVE = "call task_update at each step; task_complete when done"

# ── Delegated single-task lanes (no plan required, 2026-07-11) ───────
# lane_id namespace for ad-hoc delegated dispatch. The project-backlog
# tracking entry IS the lane's brief — one truth, no parallel tracker:
# the lane id embeds the backlog id, session_connect resolves the brief
# from it, and the worker's outcome writes back onto the same entry.
DELEGATED_LANE_PREFIX = "delegated-"


def _apply_operator_brief(packet: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Attach the operator's ad-hoc lane brief to a dispatch packet (#774).

    Complements the delegated-lane path above: there the backlog entry IS the
    brief; here the caller supplies one inline for a plan lane.

    INSTRUCTIONS ONLY. This deliberately never touches ``allowed_files``. A
    lane's file scope is a security boundary the edit gate enforces
    (cross_agent_scope_conflict), so letting free text widen it would be
    privilege escalation by prose. The plan keeps owning scope; the brief only
    says what to do inside it.

    Extracted to module level because it is the MIDDLE LINK of the brief's
    journey (tool -> packet -> worker prompt) and was the one step no test could
    reach while it was inline -- a mutation that dropped it survived.
    """
    brief = str(prompt or "").strip()
    if brief:
        packet["operator_brief"] = brief
    return packet

# ── #83 HARD merge (Emperor ruling 2026-07-18): ai_task absorbs the todo
# surface, scope-driven, NO alias. Route law (pure, pinned by
# test_ai_task_todo_merge.py):
#   * mode ∈ {add, list, remove}                → todo operation
#   * mode == 'update' AND scope ∈ {task, session} → todo patch by id
#   * everything else (begin/update/complete/status, unknown) → lifecycle
# The explicit scope param is the disambiguator for the one colliding
# mode name ('update'): the lifecycle progress-update never takes scope;
# the todo patch always operated on a scope ('task' default pre-merge, so
# old ai_todo(mode='update', ...) maps to ai_task(mode='update',
# scope='task', ...)).
_TODO_ONLY_MODES = frozenset({"add", "list", "remove"})
_TODO_SCOPES = frozenset({"task", "session"})


def route_task_mode(mode: str, scope: str) -> str:
    """Return 'todo' or 'lifecycle' for an ai_task call (#83 route law)."""
    m = (mode or "").strip().lower()
    if m in _TODO_ONLY_MODES:
        return "todo"
    if m == "update" and (scope or "").strip().lower() in _TODO_SCOPES:
        return "todo"
    return "lifecycle"


def delegated_lane_brief(project_root: Path, lane_id: str) -> dict[str, Any] | None:
    """Resolve a delegated lane's brief from its backlog tracking entry.

    Returns None for anything that is not a LIVE delegated lane —
    non-delegated ids, malformed ids, unknown / tombstoned / rejected
    backlog rows. Never guesses (never-fall-back-to-permissive): a dead
    delegation must not keep briefing workers.
    """
    lid = (lane_id or "").strip()
    if not lid.startswith(DELEGATED_LANE_PREFIX):
        return None
    suffix = lid[len(DELEGATED_LANE_PREFIX):]
    if not suffix.isdigit():
        return None
    from . import project_backlog_store

    row = project_backlog_store.get_by_id(project_root, backlog_id=int(suffix))
    if row is None or str(row.get("status") or "") in ("removed", "rejected"):
        return None
    return {
        "backlog_id": int(row["id"]),
        "title": str(row.get("title") or ""),
        "content": str(row.get("content") or ""),
        "status": str(row.get("status") or ""),
    }


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
    # query_gate.last_host_session_id (deprecated here) which carried the
    # CONDUCTOR's stamp, not the worker's — making the dispatcher
    # fire `<host> --resume <conductor's id>` against the wrong
    # target. host_session_id may be empty if the worker exits before
    # its first session event lands; the dispatcher handles empty
    # gracefully (returns dispatched=False with a clear error).
    backend = ""
    host_session_id = ""
    try:
        import sqlite3 as _sqlite

        from ._sqlite_connect import connect as _canonical_connect
        from .execution_index_store import ExecutionIndexStore

        store_path = ExecutionIndexStore().db_path(project_root)
        # read_only=True: one SELECT to resolve the worker's backend + host
        # session. It also stops this lookup MATERIALISING an empty registry
        # when the store has not been created yet — the surrounding
        # try/except then reports "not found" honestly instead of reading an
        # index it just invented. Closes the #756 leak in the same edit.
        with _canonical_connect(str(store_path), read_only=True) as conn:
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


def _alias_norm(s: object) -> str:
    """Voice-tolerant alias normalization (same rule as
    tool_interface._normalize_voice, inlined to avoid the import):
    lowercase, keep only [a-z0-9]."""
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _resolve_task_session(
    hub: Any,
    project_root: Path,
    session_id: str,
) -> str:
    """Identity doctrine (2026-07-16, internal-id-binding war).

    An agent-supplied ``session_id`` is a HUMAN ALIAS, never a
    match-key. The active work-session derives from the HOST BINDING
    (host_session_id -> bound work-session -> epoch); the alias only
    selects which work-session the host should be bound to:

    - empty alias    -> the host-bound session (nothing to resolve)
    - alias == bound -> the bound session (canonical spelling wins;
                        comparison is voice-tolerant)
    - alias != bound -> resolve + REBIND the host to the alias
                        (session_connect semantics) — never refuse

    Replaces #71's _check_session_bind_match, whose refuse-on-mismatch
    just forced the agent to re-type the matching internal id. After
    this resolver runs, task_begin's current_task_id WRITE and the
    universal task gate's READ (require_active_task, which resolves
    the managed session with the same calling host identity) key off
    the SAME host-derived session, so a begin is immediately visible
    to the gate.

    Returns the resolved internal session_id ('' only when neither an
    alias nor a host binding exists).
    """
    requested = (session_id or "").strip()
    # Host identity: request-scoped ContextVar first (canonical), then
    # the query-gate stamp for the named alias (legacy fallback).
    host_sid = ""
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
        )

        host_sid = current_calling_host_session_id() or ""
    except Exception:
        host_sid = ""
    # NO SLOT FALLBACK (#892; operator law: "identity has no fallback").
    #
    # This used to recover `host_sid` from `get_last_host_session_id(requested)`
    # when the request carried no identity of its own. That value is keyed by
    # the WORK SESSION, so it answers "who wrote here last", never "who is
    # calling" — #859's guess, and #54 option A, already refused elsewhere.
    #
    # AND IT WAS NOT A READ. `host_sid` is handed to `set_mode(...)` below,
    # which REPOINTS that window's per-conductor row to the session THIS caller
    # asked for. So an unidentified caller displaced a real window's binding:
    # the shape test_subagent_session_displacement_599.py documents ("the
    # parent's next request reads its own row and finds the subagent's
    # session"), reached through the fallback instead of a shared host id.
    #
    # mcp_server.py:4501 meets the identical recovered value and REFUSES on it —
    # "THE RECOVERED VALUE IS NOT AN IDENTITY, and no roster check can make it
    # one." This site had the same value and no such refusal.
    #
    # An unidentified caller now resolves to no host identity, `get_mode("")`
    # reports no bound session, and the requested alias is returned UNCHANGED
    # with nothing rebound. The call still works; only the silent displacement
    # is gone.
    #
    # ...AND THE READ HAD TO FOLLOW (#1027, measured 2026-09-05). The paragraph
    # above says "get_mode("") reports no bound session, and the requested
    # alias is returned UNCHANGED with nothing rebound". IT DID NOT.
    # `get_mode(host_session_id="")` falls through to the SINGLETON, which is
    # normally active, so `bound` came back as whatever session was last
    # written project-wide. A requested alias that differed then reached
    # `set_mode(...)` below with host_sid="" and REWROTE THE SINGLETON — the
    # displacement #892 removed, restored by the read it left behind.
    #
    # MEASURED CONSEQUENCE: tests/security/test_object_param_arrival_593.py
    # passes the fixture id "probe-593" through the real delegate and stdio
    # paths. Running it (or the whole security suite) left the live project's
    # singleton naming a session that does not exist, which broke the dashboard
    # (#1012: FileNotFoundError on every per-session read) and then
    # read-grounding, which refused every ai_replace with "no read of <path> in
    # session 'probe-593'" and named a remedy that could not work.
    #
    # An identity-LESS caller now consults nothing and rebinds nothing. It
    # claims to be no one, so it has no standing to read a project-wide binding
    # as "its" session, and none whatever to overwrite one.
    bound = ""
    if host_sid:
        try:
            from .managed_mode_service import resolve_managed_session

            bound = resolve_managed_session(
                hub.managed_mode,
                project_root,
                host_session_id=host_sid,
            )
        except Exception:
            bound = ""
    if not requested:
        return bound
    if not bound:
        return requested
    if _alias_norm(requested) == _alias_norm(bound):
        # Alias names the bound session — internal spelling wins.
        return bound
    # Alias names a different work-session: internals bind under the
    # hood off the host id. Rebind (per-conductor + singleton) and
    # proceed — the human alias is authoritative, never a match-key.
    # Unreachable without a host_sid now, by the guard above.
    try:
        hub.managed_mode.set_mode(
            project_root,
            requested,
            source="task_alias_rebind",
            host_session_id=host_sid,
        )
    except Exception:
        pass
    return requested


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
            # War HH debrief (#483 territory): NEVER collapse a blocked
            # complete to the bare "verification_blocked" default while a
            # real reason exists — the runtime's own `error` (no_open_task,
            # workflow_actions_pending, tool_report refusals) and the
            # verification gate's `reason` must ride through the trim so
            # the agent doesn't have to probe verification_gate directly.
            reason = (
                result.get("reason")
                or (verification.get("reason") if isinstance(verification, dict) else None)
                or result.get("error")
                or "verification_blocked"
            )
            extra: dict[str, Any] = {"tool": "task_complete"}
            if isinstance(verification, dict) and verification.get("details"):
                extra["details"] = str(verification["details"])[:500]
            # Machine-legible gate outcome for the agent's structured view.
            if isinstance(verification, dict):
                if verification.get("status"):
                    extra["verification_status"] = str(verification["status"])
                if verification.get("reason"):
                    extra["verification_reason"] = str(verification["reason"])
            # War HH: refusal envelopes (tool_report schema/audience) carry
            # a rule_id + status — surface them so the refusal stays legible
            # through the trim.
            for key in ("rule_id", "status"):
                if result.get(key) is not None:
                    extra[key] = result[key]
            return _da.fail(
                tool_name="task_complete",
                error=f"task_complete blocked: {reason}",
                extra_structured=extra,
            )

    # #475: blocked/failed task_begin & task_update must surface as
    # structured failures. The old envelope stamped every begin result
    # "ok" + directive, so a refusal (session_missing, placeholder goal,
    # king-field block) vanished behind a green chip.
    if result.get("ok") is False or result.get("blocked"):
        extra: dict[str, Any] = {"tool": tool}
        for key in ("rule_id", "status", "session_id", "who_can_satisfy", "next_action"):
            if result.get(key) is not None:
                extra[key] = result[key]
        return _da.fail(
            tool_name=tool,
            error=str(result.get("error") or result.get("reason") or "blocked"),
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
    # War HH: the feedback receipt and the once-per-session nudge must
    # survive the trim — they are the whole point of the surfacing rail.
    structured: dict[str, Any] = {}
    if result.get("tool_report") is not None:
        structured["tool_report"] = result["tool_report"]
    if result.get("tool_report_nudge"):
        structured["tool_report_nudge"] = result["tool_report_nudge"]
    return _da.ok(
        tool_name="task_complete",
        pretty_lines=pretty,
        structured=structured or None,
    )


def register_plan_task_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_sync: Any,
) -> None:
    # plan_connect MCP tool REMOVED 2026-05-02 (Empire directive — paved-road
    # entry: session_connect is the only entry, side-doors die). The
    # wrapper dumped a swamp (lanes, decisions, step_analysis,
    # plan_feedback, normalized_lines) without binding identity.
    # Conductors get plans-list-with-status from session_connect; per-plan
    # bodies via the upcoming ai_plan(mode="read", name=...) consolidation.
    # runtime.plan_connect() stays for internal callers.
    # Mirrors session_select (2026-04-28), session_start (2026-04-30),
    # session_read (2026-05-02).

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

    @timed_sync
    def ai_plan_graph(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Return the conductor lane graph for a lane-aware session plan."""
        return runtime.plan_conductor_graph(resolve_project_root(), session_id=session_id)

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

    @timed_sync
    def ai_plan_dispatch(session_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Return the next delegated lane task packet for a session plan."""
        return runtime.plan_dispatch_next(resolve_project_root(), session_id=session_id)

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

    @timed_sync
    def ai_plan_resume(session_id: str, lane_id: str, timeout: int | None = None) -> dict[str, Any]:
        """Resume a paused lane after explicit user override or conflict resolution."""
        return runtime.plan_conductor_resume_lane(
            resolve_project_root(),
            session_id=session_id,
            lane_id=lane_id,
        )

    def ai_plan_pause(session_id: str, lane_id: str, reason: str = "") -> dict[str, Any]:
        """Pause a running lane. The lane will not be dispatched until resumed."""
        return runtime._conductor_state.pause_lane(
            resolve_project_root(),
            session_id,
            lane_id,
            reason=reason,
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
        prompt: str = "",
        adhoc: bool = False,
        allowed_files: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        verification: str = "",
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
        # #377: refuse an undispatchable backend BEFORE plan_dispatch_next
        # advances the lane's dispatch state. Otherwise the plan records a
        # dispatch for a worker that was never spawned, and the caller is
        # handed success=True plus a worker_id it can only poll as missing.
        from .agent_expert_service import unsupported_backend_reason

        _backend_refusal = unsupported_backend_reason(backend)
        if _backend_refusal is not None:
            return {"success": False, "error": _backend_refusal, "backend": backend}

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
            # the caller permitted (authenticated RBAC — every flavor, #404).
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
        if adhoc:
            # #779: a lane is a subagent, and the SPAWNER is the authority for its
            # own subagents. Nothing is looked up in the plan here, so nothing in
            # the plan can refuse it -- which is the point. Requiring a plan entry
            # first put the plan DOCUMENT in charge of who may exist, and the only
            # tool that writes one replaces PLAN.md wholesale (#778), so the cost
            # of one ad-hoc probe was risking the session's main plan.
            # Scope comes from THIS call and defaults to nothing (see
            # build_adhoc_task_packet); the brief instructs, it never grants.
            from .runtime_conductor_dispatch_service import build_adhoc_task_packet

            if not str(lane_id or "").strip():
                return {
                    "success": False,
                    "error": (
                        "adhoc=True requires an explicit lane_id -- an ad-hoc lane has "
                        "no plan entry to take its identity from, and an unnamed "
                        "subagent cannot be addressed, reviewed or killed."
                    ),
                }
            _brief = str(prompt or "").strip()
            packet = build_adhoc_task_packet(
                session_id=session_id,
                lane_id=lane_id,
                goal=(_brief.splitlines()[0].strip() if _brief else lane_id),
                brief=_brief,
                allowed_files=allowed_files,
                allowed_tools=allowed_tools,
                verification=verification,
            )
            # Bypassing plan_dispatch_next skips its LOOKUP, which is the point.
            # It must not skip its OBLIGATIONS. plan_dispatch_next also stamps
            # the lane's gate scope (set_lane_scope: "agent can only access
            # lane's files AND only call tools the lane allows"), and a lane
            # with no session_query_gate row is one whose worker is refused by
            # its own gate. MEASURED: the first ad-hoc lane started, received
            # its brief, and then had every tool refused with
            # managed_mode_not_active while the caller was told success=True.
            try:
                runtime._conductor_state.set_lane_scope(
                    project_root,
                    session_id,
                    lane_id,
                    allowed_files=packet.get("allowed_files", []),
                    lane_allowed_tools=packet.get("lane_allowed_tools", []),
                )
            except Exception as _scope_err:
                # Refuse rather than spawn a worker that cannot act. A lane that
                # runs without a scope row burns a full timeout doing nothing and
                # reports as a WORKER failure, which is the hardest kind of bug
                # to trace back to dispatch.
                return {
                    "success": False,
                    "error": (
                        f"adhoc lane {lane_id!r} could not be given a gate scope "
                        f"({_scope_err}); refusing to spawn a worker whose every "
                        "tool call would be refused managed_mode_not_active."
                    ),
                    "blocked_by": "lane_scope_unavailable",
                }
            dispatch = {
                "session_id": session_id,
                "mode": "adhoc",
                "dispatch_state": "adhoc",
                "reason": "spawner-owned lane; no plan entry consulted",
                "packet": packet,
            }
        else:
            dispatch = runtime.plan_dispatch_next(
                project_root,
                session_id=session_id,
                lane_id=lane_id,
            )
            packet = dispatch.get("packet")
            if not packet:
                return {
                    "success": False,
                    "error": "No dispatch packet available",
                    "dispatch": dispatch,
                }
        if model:
            route = packet.get("route")
            if not isinstance(route, dict):
                route = {}
            route["model"] = model
            packet["route"] = route
        # #774: the operator's ad-hoc brief rides ON the packet, the same seam
        # `model` uses. INSTRUCTIONS ONLY -- it deliberately does not touch
        # allowed_files. A lane's file scope is a security boundary the edit gate
        # enforces (cross_agent_scope_conflict), so letting free text widen it
        # would be privilege escalation by prose. The plan still owns scope; the
        # brief only says what to do inside it.
        packet = _apply_operator_brief(packet, prompt)
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
            # Unaliased: see the fingerprint note at the audited_run below.
            import subprocess
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
                            # #1031: a FORCED PROCESS-TREE KILL is exactly what
                            # the audit ledger exists to record, and this one
                            # was invisible to it — hidden behind the
                            # `**/server_plan_task_tools.py` semgrep exclude
                            # that was meant to be temporary noise control.
                            # Routed now, with CREATE_NO_WINDOW so the census
                            # can see the posture rather than infer it.
                            from .shell_egress_service import audited_run

                            _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                            audited_run(
                                ["taskkill", "/F", "/T", "/PID", str(pid)],
                                fingerprint=(
                                    "server_plan_task_tools.py",
                                    "ai_kill",
                                    "subprocess.run",
                                ),
                                reason="forced tree-kill of a detached ai_run child",
                                # Canonical name, not the `_sub_kill` alias:
                                # the fingerprint records what the AST sees and
                                # the registry only knows canonical callees.
                                run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603
                                capture_output=True,
                                timeout=10,
                                creationflags=_no_window,
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

            from ._sqlite_connect import connect as _canonical_connect
            from .execution_index_store import ExecutionIndexStore

            store = ExecutionIndexStore()
            store.init_db(project_root)
            import time as _t

            now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
            # RUNTIME (the default): session_lane_agents.state is LIVE worker
            # bookkeeping, and the process this row is being marked 'killed'
            # for is dead anyway once the machine goes down — a power cut that
            # loses the state change cannot resurrect the worker, so nothing is
            # handed back. Not read_only: this block UPDATEs.
            with _canonical_connect(str(store.db_path(project_root))) as conn:
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

            from ._sqlite_connect import connect as _canonical_connect
            from .execution_index_store import ExecutionIndexStore

            store = ExecutionIndexStore()
            store.init_db(project_root)
            # read_only=True: a single SELECT resolving the worker to resume,
            # after init_db has guaranteed the file. Also closes the #756 leak.
            with _canonical_connect(
                str(store.db_path(project_root)), read_only=True,
            ) as conn:
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

    # #83 hard merge: the todo impl is built lazily on first todo-mode call
    # (register_todo_backlog_tools runs after this registrar in
    # create_server, so eager construction order must not matter).
    _todo_handler_box: dict[str, Any] = {}

    def _todo_handler() -> Any:
        handler = _todo_handler_box.get("fn")
        if handler is None:
            from .server_todo_backlog_tools import build_todo_handler

            handler = build_todo_handler(hub=hub)
            _todo_handler_box["fn"] = handler
        return handler

    @server.tool(
        annotations={"destructiveHint": True, "openWorldHint": False, "title": "ai_task"},
        meta={"anthropic/alwaysLoad": True},
    )
    def ai_task(
        mode: str,
        session_id: str = "",
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
        tool_report: list[dict[str, Any]] | None = None,
        scope: str = "",
        content: str = "",
        id: int = 0,
        status: str = "",
        tags: list[str] | None = None,
        include_done: bool = False,
        include_removed: bool = False,
        reason: str = "",
        columns: list[str] | None = None,
        urgency: str = "",
        format: str = "",
    ) -> Any:
        """Unified work-item tool — task lifecycle + task-owned todos
        (#83 hard merge, scope-driven; Emperor ruling 2026-07-18).

        Lifecycle modes (operate on THE task; session_id resolves via the
        host binding, empty = bound session):
        mode='begin'    — register a new task. Required: session_id.
        mode='update'   — record progress on the active task (no scope).
        mode='complete' — close the active task. Required: session_id,
                         result_summary. Optional tool_report (superadmin/
                         dev principals only): [{tool, rating 1-10, note}]
                         structured tool feedback → tool_usage_reports +
                         backlog #469 digest (War HH 2026-07-19).
        mode='status'   — read-only peek. Required: session_id.

        Todo modes (operate on todo ITEMS owned by the current task/session;
        the former ai_todo surface — same shapes, same store):
        mode='add'    — create a todo (content required; active task required).
        mode='list'   — list todos (scope='task' default | 'session';
                        include_done/include_removed/tags/columns;
                        format='table'|'csv' opt-in per #20).
        mode='update' + scope='task'|'session' — patch a todo by id
                        (status/content/tags/urgency). The EXPLICIT scope
                        is what routes 'update' to the todo surface;
                        without scope, 'update' is the lifecycle progress
                        update above.
        mode='remove' — tombstone a todo by id (reason >=8 chars).

        Runtime branches by route_task_mode(mode, scope); lifecycle gates
        (host-derived session-alias resolution, lane-worker §VIII capture
        for complete) run inside the per-mode branches via the helpers.
        """
        if route_task_mode(mode, scope) == "todo":
            return _todo_handler()(
                mode=(mode or "").strip().lower(),
                content=content,
                id=id,
                status=status,
                tags=tags,
                scope=((scope or "").strip().lower() or "task"),
                include_done=include_done,
                include_removed=include_removed,
                reason=reason,
                columns=columns,
                urgency=urgency,
                format=format,
            )
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
                tool_report=tool_report,
            )
        if m == "status":
            return task_status(session_id=session_id)
        return {
            "error": (
                f"unknown mode: {mode!r} (lifecycle: begin|update|complete|status; "
                f"todos: add|list|remove, and update with scope='task'|'session')"
            ),
        }

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

        session_id is a human ALIAS (identity doctrine 2026-07-16):
        it resolves against the host binding — empty means the bound
        session; a divergent alias rebinds the host, never refuses.
        The task then lives under the same host-derived session the
        universal task gate reads.
        """
        root = resolve_project_root()
        session_id = _resolve_task_session(hub, root, session_id)
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

        session_id is a human alias — resolved against the host
        binding (identity doctrine 2026-07-16).
        """
        root = resolve_project_root()
        session_id = _resolve_task_session(hub, root, session_id)
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
        tool_report: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Complete task work in a session and update session state.

        session_id is a human alias — resolved against the host
        binding (identity doctrine 2026-07-16).
        """
        root = resolve_project_root()
        session_id = _resolve_task_session(hub, root, session_id)
        # #684 SINK SCREEN: result_summary / verification_evidence are agent
        # prose that lands in sqlite and in the handoff, never in a file the
        # write guard scans. Repair, never refuse — refusing a completion
        # would wedge the agent at the one moment it is trying to finish.
        from .agent_prose_screen import repair_agent_prose, repair_agent_prose_deep

        result_summary = repair_agent_prose(result_summary, sink="task_result_summary")
        verification_evidence = repair_agent_prose_deep(
            verification_evidence, sink="task_verification_evidence"
        )
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
            if tool_report and isinstance(capture, dict):
                # War HH: the lane-worker capture path never reaches the
                # runtime's tool_report gate — say so instead of silently
                # eating the feedback (a silent drop would eat feedback).
                capture["tool_report"] = {
                    "refused": True,
                    "rule_id": "tool_report.audience",
                    "reason": (
                        "tool_report is not honored on the lane-worker "
                        "capture path — relay tool feedback in your REPORT "
                        "text for the conductor to harvest."
                    ),
                }
            return capture
        result = runtime.task_complete(
            root,
            session_id=session_id,
            result_summary=result_summary,
            next_status=next_status,
            verification_evidence=verification_evidence,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
            tool_report=tool_report,
        )
        return _trim_lifecycle_result(result, include_code_bundle, tool="task_complete")

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

    # @server.tool deliberately absent (120% clause B): folded into
    # ai_lane(action='activity'). register_impl target (end of module)
    # so the consolidator reaches it. Backlog #289: lane observability
    # backed by the execution_events AUDIT ledger — the exact table the
    # dashboard reads — never a new parallel log.
    def ai_lane_activity(
        session_id: str = "",
        lane_id: str = "",
        worker_id: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Per-lane-worker activity from the execution_events audit ledger.

        For each lane worker (session_lane_agents roster row) returns:
          * recent_events — bounded tail (``limit``, cap 100, always
            chronological) of the worker's subagent audit rows: ts, tool,
            event/action kind, status, ok flag, short args digest
            (target_entity or input_hash·bytes — NEVER the full payload).
          * counts — ledger-derived totals for the worker's window:
            events, tool_calls (native_tool_use), failed, turns
            (assistant_turn_end), tokens_in (payload estimates).
          * last_output — latest assistant_turn_end: stop_reason,
            tool_use_count, message_len, plus a ≤400-char message_tail
            ONLY when ``audit.capture_response_content`` stored the text
            (``content_captured`` labels which case you got — a label is
            cheaper than a lie).

        Identity keying mirrors ai_events (the audit read path #289
        names): the worker's session_id + principal_type='subagent' +
        the worker's started_at..completed_at(+60s) window. Filter
        shapes: worker_id → that worker; session_id (+optional lane_id)
        → up to the 10 most recent workers. No identity → refusal,
        never a full-table scan.
        """
        import json as _json
        import sqlite3 as _sqlite3

        # #755/#756: the ONE canonical connect. This was
        # `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION
        # context manager, which commits and NEVER closes the handle --
        # with no pragmas. The worker roster read is a pure SELECT, so
        # read_only=True; _sqlite3.Row is kept below unchanged.
        from ._sqlite_connect import connect as _canonical_connect

        from .execution_index_store import ExecutionIndexStore

        root = resolve_project_root()
        store = ExecutionIndexStore()
        store.init_db(root)
        capped = max(1, min(int(limit or 20), 100))
        sid = (session_id or "").strip()
        lid = (lane_id or "").strip()
        wid = (worker_id or "").strip()
        if not sid and not wid:
            return {
                "error": (
                    "Pass session_id (optionally lane_id) OR worker_id. "
                    "Get both from ai_lane(action='agents')."
                ),
                "workers": [],
                "worker_count": 0,
                "limit": capped,
            }
        fail_states = ("failed", "denied", "blocked", "error")

        def _digest(row: Any) -> str:
            """Short args digest — forensic pointer, never payload bytes."""
            target = str(row["target_entity"] or "").strip()
            if target:
                return target[:120]
            try:
                payload = _json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            input_hash = str(payload.get("input_hash") or "")
            if input_hash:
                return f"{input_hash}·{payload.get('input_bytes') or 0}B"[:120]
            return ""

        workers: list[dict[str, Any]] = []
        error = ""
        with _canonical_connect(str(store.db_path(root)), read_only=True) as conn:
            conn.row_factory = _sqlite3.Row
            if wid:
                roster = conn.execute(
                    "SELECT * FROM session_lane_agents WHERE worker_id = ?",
                    (wid,),
                ).fetchall()
                if not roster:
                    error = f"worker_id {wid!r} not found in session_lane_agents registry."
            else:
                roster_sql = "SELECT * FROM session_lane_agents WHERE session_id = ?"
                roster_params: list[Any] = [sid]
                if lid:
                    roster_sql += " AND lane_id = ?"
                    roster_params.append(lid)
                roster_sql += " ORDER BY started_at DESC LIMIT 10"
                roster = conn.execute(roster_sql, roster_params).fetchall()
            for reg in roster:
                w_sid = str(reg["session_id"])
                started_at = str(reg["started_at"] or "") or None
                completed_at = str(reg["completed_at"] or "") or None
                # Same window shape as ai_events (server_audit_tools):
                # conductor session + subagent principal + worker window.
                window = " AND session_id = ? AND principal_type = 'subagent'"
                wparams: list[Any] = [w_sid]
                if started_at:
                    window += " AND observed_at >= ?"
                    wparams.append(started_at)
                if completed_at:
                    window += " AND observed_at <= datetime(?, '+60 seconds')"
                    wparams.append(completed_at)
                counts_row = conn.execute(
                    "SELECT COUNT(*) AS events, "
                    "SUM(CASE WHEN event_kind = 'native_tool_use' "
                    "    THEN 1 ELSE 0 END) AS tool_calls, "
                    "SUM(CASE WHEN status IN ('failed','denied','blocked','error') "
                    "    THEN 1 ELSE 0 END) AS failed, "
                    "SUM(CASE WHEN event_kind = 'assistant_turn_end' "
                    "    THEN 1 ELSE 0 END) AS turns, "
                    "SUM(COALESCE(CAST(json_extract(payload_json, "
                    "    '$.tokens_in_estimate') AS INTEGER), 0)) AS tokens_in "
                    "FROM execution_events WHERE 1=1" + window,
                    wparams,
                ).fetchone()
                rows = conn.execute(
                    "SELECT observed_at, capability_name, event_kind, "
                    "action_kind, status, target_entity, payload_json "
                    "FROM execution_events WHERE 1=1"
                    + window
                    + " ORDER BY observed_at DESC, event_id DESC LIMIT ?",
                    [*wparams, capped],
                ).fetchall()
                recent_events = [
                    {
                        "ts": r["observed_at"],
                        "tool": r["capability_name"],
                        "event_kind": r["event_kind"],
                        "action_kind": r["action_kind"],
                        "status": r["status"],
                        "ok": str(r["status"] or "").lower() not in fail_states,
                        "args_digest": _digest(r),
                    }
                    # DESC query tail → reversed = chronological slice.
                    for r in reversed(rows)
                ]
                turn = conn.execute(
                    "SELECT observed_at, status, payload_json "
                    "FROM execution_events "
                    "WHERE event_kind = 'assistant_turn_end'"
                    + window
                    + " ORDER BY observed_at DESC, event_id DESC LIMIT 1",
                    wparams,
                ).fetchone()
                last_output = None
                if turn is not None:
                    try:
                        turn_payload = _json.loads(turn["payload_json"] or "{}")
                    except Exception:
                        turn_payload = {}
                    text = turn_payload.get("message_text")
                    captured = isinstance(text, str) and bool(text)
                    last_output = {
                        "observed_at": turn["observed_at"],
                        "stop_reason": str(
                            turn_payload.get("stop_reason") or turn["status"] or "",
                        ),
                        "tool_use_count": turn_payload.get("tool_use_count"),
                        "message_len": turn_payload.get("message_len"),
                        "content_captured": captured,
                        "message_tail": text[-400:] if captured else None,
                    }
                workers.append(
                    {
                        "worker_id": reg["worker_id"],
                        "session_id": w_sid,
                        "lane_id": reg["lane_id"],
                        "backend": reg["backend"],
                        "state": reg["state"],
                        "started_at": started_at,
                        "completed_at": completed_at,
                        "counts": {
                            "events": int(counts_row["events"] or 0),
                            "tool_calls": int(counts_row["tool_calls"] or 0),
                            "failed": int(counts_row["failed"] or 0),
                            "turns": int(counts_row["turns"] or 0),
                            "tokens_in": int(counts_row["tokens_in"] or 0),
                        },
                        "recent_events": recent_events,
                        "last_output": last_output,
                    },
                )
        out: dict[str, Any] = {
            "session_id": sid or None,
            "lane_id": lid or None,
            "worker_id": wid or None,
            "worker_count": len(workers),
            "workers": workers,
            "limit": capped,
            "source": "execution_events",
        }
        if error:
            out["error"] = error
        return out

    # Delegated single-task lane — reachable ONLY via the consolidator
    # ai_lane(action='delegate'). register_impl target (end of module)
    # so the consolidator reaches it; deliberately NO standalone
    # @server.tool alias (mirror of ai_lane_activity, #289 pattern).
    def ai_lane_delegate(
        session_id: str,
        prompt: str = "",
        backend: str = "claude",
        model: str = "",
        timeout: int = 600,
        lane_id: str = "",
    ) -> dict[str, Any]:
        """Dispatch ONE ad-hoc delegated task to a fresh tracked lane worker.

        Two of the conductor's three dispatch modes land here (the third,
        laned-plan, is ai_plan(create) -> ai_lane(action='spawn')):

          MODE 1 — backlog-redirect: lane_id='delegated-<backlog_id>'
            ADOPTS that existing item. Its body IS the brief and its own
            row carries the outcome, so "fix backlog item #N" dispatches
            without copying anything anywhere. `prompt` is optional here
            and is APPENDED as extra conductor direction, never a
            replacement. Adoption keys off the lane id this function
            already derives from the backlog id, so mode 1 costs no new
            ai_lane parameter and the public inputSchema / gate goldens
            hold.

          MODE 2 — freetext: no lane_id, `prompt` required. Mints its own
            tracking entry (the original #265 shape).

        Adoption never falls back permissively: an unresolvable lane id, a
        non-delegated (plan) lane id, a settled/tombstoned item, or an item
        another worker already owns is REFUSED and nothing is minted.
        Quietly minting a duplicate would be the worst outcome — the
        operator would believe #N was being worked while a copy carried the
        real worker.

        No plan required — this is the single-task sibling of the lane
        PLAN shape (both stay first-class). The task is tracked as a
        project-backlog entry (the EXISTING tracking primitive, no
        parallel tracker):

          * the entry body IS the worker's brief — session_connect's
            lane-worker branch resolves it via delegated_lane_brief
            when no plan declares the lane;
          * lane_id = 'delegated-<backlog_id>' so lane, worker roster
            row, and tracking entry all key off one id;
          * when the worker finishes, its outcome writes back onto the
            same entry: status 'done' (success + claimed_done) or
            'blocked' (failure/blockers/crash) plus a bounded outcome
            section (never full payload dumps).

        Spawn goes through the SAME tracked path as plan lanes
        (spawn_worker_async: caps, audit events, job registry,
        session_lane_agents registration) — on_complete replaces
        plan_dispatch_report because there is no plan to report to.
        """
        from . import project_backlog_store

        sid = (session_id or "").strip()
        brief = (prompt or "").strip()
        requested_lane = (lane_id or "").strip()
        if not sid:
            return {
                "success": False,
                "error": (
                    "session_id required — the delegated lane worker "
                    "binds to a session like any other lane worker"
                ),
            }
        project_root = resolve_project_root()

        # ── MODE 1: backlog-redirect (adopt an existing tracking entry) ──
        adopted = False
        prior_status = "open"
        if requested_lane:
            if not requested_lane.startswith(DELEGATED_LANE_PREFIX):
                return {
                    "success": False,
                    "error": (
                        f"lane_id {requested_lane!r} is not a delegated lane. "
                        "action='delegate' either ADOPTS an existing backlog "
                        f"item via lane_id='{DELEGATED_LANE_PREFIX}<backlog_id>' "
                        "(mode 1) or mints its own tracking entry from prompt "
                        "(mode 2). A plan lane is dispatched with "
                        "action='spawn' (mode 3)."
                    ),
                }
            existing = delegated_lane_brief(project_root, requested_lane)
            if existing is None:
                return {
                    "success": False,
                    "error": (
                        f"lane_id {requested_lane!r} does not resolve to a live "
                        "backlog item (unknown id, malformed id, or a removed / "
                        "rejected row). Refusing rather than minting a "
                        "duplicate tracking entry."
                    ),
                }
            backlog_id = existing["backlog_id"]
            prior_status = existing["status"]
            if prior_status in ("done", "merged"):
                return {
                    "success": False,
                    "error": (
                        f"backlog #{backlog_id} is {prior_status} — a settled "
                        "item is not resurrected by a dispatch. Reopen it "
                        "first if the work is genuinely unfinished."
                    ),
                }
            if prior_status == "in_progress":
                return {
                    "success": False,
                    "error": (
                        f"backlog #{backlog_id} is already in_progress — a "
                        "worker owns it. Check ai_lane(action='status'), or "
                        "reopen the item before re-dispatching it."
                    ),
                }
            body = str(existing["content"] or "").strip()
            if not body:
                return {
                    "success": False,
                    "error": (
                        f"backlog #{backlog_id} has an empty body — there is "
                        "no brief to dispatch. Fill the item in, or use mode 2 "
                        "(prompt) for an ad-hoc task."
                    ),
                }
            # The item body IS the brief; a prompt is EXTRA direction.
            brief = f"{body}\n\n## Conductor direction\n{brief}" if brief else body
            write_base = body
            adopted = True
        # ── MODE 2: freetext (mint the tracking entry) ────────────────
        else:
            if not brief:
                return {
                    "success": False,
                    "error": (
                        "prompt required — it becomes the tracked brief "
                        "the delegated worker executes. To dispatch an "
                        "EXISTING backlog item instead, pass "
                        f"lane_id='{DELEGATED_LANE_PREFIX}<backlog_id>'."
                    ),
                }
            added = project_backlog_store.add(
                project_root,
                content=brief,
                tags=["delegated-lane"],
                created_in_session_id=sid,
            )
            if not added.get("ok"):
                return {
                    "success": False,
                    "error": f"tracking entry refused: {added.get('error')}",
                }
            backlog_id = int(added["id"])
            write_base = brief
        lane_id = f"{DELEGATED_LANE_PREFIX}{backlog_id}"
        project_backlog_store.update(
            project_root,
            backlog_id=backlog_id,
            status="in_progress",
        )
        packet: dict[str, Any] = {
            "lane_id": lane_id,
            "session_id": sid,
            "delegated": True,
            "adopted": adopted,
            "backlog_id": backlog_id,
            "brief": brief,
        }
        # #781: attach through the SAME seam ai_spawn uses. `brief` above is the
        # delegated packet's historical key and existing tests pin it, so it
        # stays -- but the renderer reads `operator_brief`, so hand-rolling the
        # key here meant every delegated worker was dispatched with a brief it
        # could not see and died as "produced 1 MCP event, never called
        # task_complete". One attach helper, both routes.
        packet = _apply_operator_brief(packet, brief)
        if model:
            packet["route"] = {"model": model}

        def _write_back(result: dict[str, Any]) -> None:
            """Land the worker outcome on the tracking entry (bounded)."""
            try:
                row = project_backlog_store.get_by_id(
                    project_root,
                    backlog_id=backlog_id,
                )
                base = str((row or {}).get("content") or write_base)
                ok = bool(result.get("success")) and bool(result.get("claimed_done"))
                out_lines: list[str] = [
                    base,
                    "",
                    (
                        f"## Delegated lane outcome — {lane_id} "
                        f"(worker {result.get('worker_id') or 'unknown'})"
                    ),
                    (
                        f"- success: {bool(result.get('success'))} / "
                        f"claimed_done: {bool(result.get('claimed_done'))}"
                    ),
                ]
                error = str(result.get("error") or "").strip()
                if error:
                    out_lines.append(f"- error: {error[:500]}")
                blockers = result.get("blockers") or []
                if isinstance(blockers, (list, tuple)) and blockers:
                    out_lines.append(
                        "- blockers: "
                        + "; ".join(str(b)[:200] for b in blockers[:10]),
                    )
                files = result.get("files_changed") or []
                if isinstance(files, (list, tuple)) and files:
                    out_lines.append(
                        "- files_changed: " + ", ".join(str(f) for f in files[:20]),
                    )
                raw = str(result.get("raw_output") or "").strip()
                if raw:
                    out_lines.append(f"- output tail: {raw[-600:]}")
                project_backlog_store.update(
                    project_root,
                    backlog_id=backlog_id,
                    status="done" if ok else "blocked",
                    content="\n".join(out_lines),
                )
            except Exception:
                import logging as _logging

                _logging.getLogger(__name__).exception(
                    "delegated write-back failed for backlog #%s (lane %s)",
                    backlog_id,
                    lane_id,
                )

        handle = runtime._agent_expert.spawn_worker_async(
            project_root,
            packet,
            backend=backend or "claude",
            timeout=int(timeout) if timeout else 600,
            session_id=sid,
            on_complete=_write_back,
        )
        if not handle.get("worker_id"):
            # Spawn refused (concurrency cap / machine ceiling): the
            # tracking entry must not dangle in_progress forever. A MINTED
            # entry is only ever this dispatch, so 'blocked' is its truth; an
            # ADOPTED item pre-existed the dispatch, so it returns to the
            # status it had -- a capacity refusal is not a finding about the
            # work, and marking a real war 'blocked' would be a lie the
            # operator has to unpick.
            refusal = str(handle.get("error") or "spawn refused")
            project_backlog_store.update(
                project_root,
                backlog_id=backlog_id,
                status=prior_status if adopted else "blocked",
                content=(
                    write_base
                    + "\n\n## Delegated lane spawn refused\n- "
                    + refusal[:500]
                ),
            )
            return {
                "success": False,
                "error": refusal,
                "adopted": adopted,
                "backlog_id": backlog_id,
                "lane_id": lane_id,
            }
        return {
            "success": True,
            "delegated": True,
            "adopted": adopted,
            "backlog_id": backlog_id,
            "lane_id": lane_id,
            "worker_id": handle.get("worker_id"),
            "backend": handle.get("backend"),
            "state": handle.get("state"),
            "hint": (
                "Poll ai_lane(action='status'/'activity'). The outcome "
                f"writes back onto backlog #{backlog_id} when the worker "
                "finishes."
            ),
        }

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
        from .operator_auth_service import OperatorAuthService
        from .permission_catalog import PERM_ADMIN_MANAGE_CONFIG
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

    # C.20: hidden implementation binding only; ToolSpec owns metadata.
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

    # ── C.20 direct registry dispatch (Empire directive 2026-05-29) ──
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

    _ti_c20.register_impl("verification_gate", verification_gate)
    _ti_c20.register_impl("ai_spawn", ai_spawn)
    _ti_c20.register_impl("ai_status", ai_status)
    _ti_c20.register_impl("ai_jobs", ai_jobs)
    _ti_c20.register_impl("ai_kill", ai_kill)
    _ti_c20.register_impl("ai_resume", ai_resume)

    # 2026-07-10 (tool-surface map §7.4): extend the same fast path to the
    # deprecation-window siblings this module registers — the ai_lane/ai_plan
    # consolidators dispatch to these by literal legacy name via _delegate,
    # which without an _IMPLS entry pays the ~150ms create_server round-trip
    # to reach the very closures registered above. Same objects, same captured
    # scope (hub/runtime), zero behavior change — only the routing latency.
    # Coverage: test_c20_direct_dispatch (full legacy-sibling section).
    _ti_c20.register_impl("ai_lane_summary", ai_lane_summary)
    # #289 lane observability — reachable ONLY via the consolidator
    # (ai_lane action='activity'); no standalone @server.tool alias.
    _ti_c20.register_impl("ai_lane_activity", ai_lane_activity)
    # Delegated single-task lane — reachable ONLY via the consolidator
    # (ai_lane action='delegate'); no standalone @server.tool alias.
    _ti_c20.register_impl("ai_lane_delegate", ai_lane_delegate)
    _ti_c20.register_impl("ai_plan_create", ai_plan_create)
    _ti_c20.register_impl("ai_plan_graph", ai_plan_graph)
    _ti_c20.register_impl("ai_plan_status", ai_plan_status)
    _ti_c20.register_impl("ai_plan_dispatch", ai_plan_dispatch)
    _ti_c20.register_impl("ai_plan_report", ai_plan_report)
    _ti_c20.register_impl("ai_plan_overlap", ai_plan_overlap)
    _ti_c20.register_impl("ai_plan_resume", ai_plan_resume)
    _ti_c20.register_impl("ai_plan_pause", ai_plan_pause)
    _ti_c20.register_impl("ai_plan_expand", ai_plan_expand)
    _ti_c20.register_impl("ai_plan_reopen", ai_plan_reopen)
    _ti_c20.register_impl("ai_plan_mark_ready", ai_plan_mark_ready)
    _ti_c20.register_impl("ai_plan_signal", ai_plan_signal)
