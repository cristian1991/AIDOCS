"""Detached run MCP tools — the single shell surface for AIDOCS.

All shell work goes through a spawn-and-poll model. Output never
floods the agent's context unless explicitly fetched via
`ai_run_output`. The old sync wrappers (code_run_command,
code_test_project, code_build_project, code_run_async) were deleted
2026-04-20; this module is the canonical shell interface.

Tools:
  ai_run(command, timeout_seconds=180, foreground=false) →
      spawn detached and return run_id. Enforces bash_policy +
      heuristic judge + user-intent subcommand grants BEFORE spawn.
      If the command finishes in <500ms the response includes
      done=True + tail (fast-path, one round-trip). Foreground=true
      opens a visible terminal window (desktop operator use case).
  ai_run_status(run_id) → state + exit_code + log_bytes.
  ai_run_output(run_id, tail_bytes=4096, wait_seconds=0) → read
      log tail, optionally blocking until finish. Test/build output
      is classified automatically via render_test/build/probe.
  ai_run_kill(run_id) → stop a runaway run.
"""

from __future__ import annotations

from typing import Any

from .mcp_server_runtime_helpers import (
    require_active_task,
    resolve_project_root,
)
from .tool_display import renders_as


def register_run_tools(*, server: Any, hub: Any, runtime: Any) -> None:

    def _caller_session(root: Any) -> str:
        """Best-effort calling-session id for output-guard audit rows."""
        try:
            from .managed_mode_service import ManagedModeService
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id as _ccsid,
            )

            managed = ManagedModeService().get_mode(
                root,
                host_session_id=_ccsid(),
            )
            if isinstance(managed, dict) and managed.get("active"):
                return str(managed.get("session_id") or "").strip()
        except Exception:
            pass
        return ""

    def _lifecycle_preflight(hub_, root, command, *, session_id):
        """Future-sight preflight for the ai_run path. Returns a refusal
        dict (deny or freeze) to short-circuit the spawn, or None to
        proceed. Always records a future_sight_preflight audit; enforces
        only when tools.shell_lifecycle_preflight_enforce is on.
        """
        # Resolve the enforce posture first so import uncertainty can fail
        # CLOSED under enforcement instead of silently proceeding.
        try:
            from .config import get_setting

            enforce = bool(
                get_setting(
                    "tools.shell_lifecycle_preflight_enforce",
                    project_root=root,
                    default=False,
                ),
            )
        except Exception:
            enforce = False
        # ONE shared strict-lifecycle authority — IDENTICAL to the native
        # ShellPolicy path: name-based classification + manifest x-ray
        # evidence + future_sight_preflight audit, collapsed into
        # lifecycle_preflight so neither transport sees a weaker law. Fails
        # CLOSED under enforcement if the machinery is unavailable.
        try:
            from .shell_lifecycle import lifecycle_preflight
        except Exception:
            if enforce:
                return {
                    "ok": False,
                    "err": (
                        "future-sight preflight is unavailable; failing closed under enforcement"
                    ),
                    "command": command,
                    "blocked_by": "preflight_unavailable",
                }
            return None
        verdict = lifecycle_preflight(
            command,
            project_root=root,
            enforce=enforce,
            hub=hub_,
            session_id=session_id,
        )
        action = verdict.action
        fam = verdict.family
        reason = verdict.reason
        if action == "proceed":
            return None
        if action == "deny":
            return {
                "ok": False,
                "err": (f"hidden execution chain denied ({fam}): {reason}"),
                "command": command,
                "blocked_by": f"lifecycle_{fam}",
            }
        if action == "confirm" and session_id:
            # Single freeze authority for ai_run = freeze_service.
            try:
                from .freeze_service import build_freeze_response

                env = build_freeze_response(
                    root,
                    session_id,
                    tool_name="ai_run",
                    tool_input={"command": command},
                    judge_summary=(f"hidden execution chain ({fam}): {reason}"),
                    risk_class=f"lifecycle:{fam}",
                    scope=command[:200],
                    consequence=(
                        "If approved, AIDOCS runs this command, which "
                        "triggers the detected hidden execution chain."
                    ),
                )
                return {
                    "ok": False,
                    "err": env["permissionDecisionReason"],
                    "command": command,
                    "freeze_state": env.get("freeze_state"),
                    "blocked_by": env.get("blocked_by", f"lifecycle_{fam}"),
                }
            except Exception:
                # Mint failed → fail closed (deny), no hollow prompt.
                return {
                    "ok": False,
                    "err": (
                        "hidden execution chain requires approval but the "
                        "freeze could not be created; action blocked"
                    ),
                    "command": command,
                    "blocked_by": "freeze_mint_failed",
                }
        if action == "confirm":
            # Confirm required but no session to bind a freeze → fail closed.
            return {
                "ok": False,
                "err": (
                    f"hidden execution chain requires operator approval "
                    f"({fam}) but no session is bound; action blocked"
                ),
                "command": command,
                "blocked_by": "freeze_no_session",
            }
        return None

    def _run_shell_unified(
        command: str,
        timeout_seconds: int = 600,
        foreground: bool = False,
    ) -> Any:
        """Unified shell tool body. Single caller: ai_run.

        Always detached; inline-tail returns fast commands in one
        round-trip.

        Enforces the SAME three-layer policy as raw Bash:
          1. bash_policy allow/deny (with user-intent subcommand
             grants lifting specific allowlist entries).
          2. Heuristic judge (rm -rf, curl|sh, destructive chains,
             inline-code bypasses via python -c / node -e / etc).
          3. Tier-0 destructive denylist (fires inside the judge as
             critical-severity verdicts).
        Refusals return a structured error without spawning.
        """
        # ai_run boundary trace (dev.runtime.ai_run_trace, dev flavor only).
        from ._dev_trace import trace as _trace
        from .code_runner_detached import spawn_detached
        from .gate_tool import enforce_tool_call
        from .tool_display import _dispatch_run_output

        root = resolve_project_root()

        def _bc(marker: str) -> None:
            _trace(root, marker)

        _bc("A entered _run_shell_unified")
        gate = require_active_task(hub, root, "ai_run")
        _bc("B after require_active_task")
        if gate is not None:
            return gate

        if not command or not command.strip():
            return _dispatch_run_output(
                {"ok": False, "err": "command is required", "command": command},
                {},
            )

        # Slice 1 (canonical 2026-04-29): the existing-freeze short-
        # circuit, the AgentOrchestrator.check_tool cascade
        # (bash_policy + heuristic_judge + tool_policy + infra check),
        # the needs_confirmation freeze mint, and the fail-closed
        # evaluator-exception refusal all live in
        # gate_tool.enforce_tool_call now. Behavior here is unchanged;
        # other tool surfaces reuse the same helper.
        _bc("C before enforce_tool_call")
        result = enforce_tool_call(
            hub,
            root,
            "ai_run",
            {"command": command},
            fail_closed=True,
            include_freeze=True,
            runtime=runtime,
        )
        _bc("D after enforce_tool_call")
        if result.refusal is not None:
            refusal = result.refusal
            return _dispatch_run_output(
                {
                    "ok": False,
                    "err": refusal.get("reason", "refused"),
                    "command": command,
                    "freeze_state": refusal.get("freeze_state"),
                    "blocked_by": refusal.get("blocked_by"),
                },
                {},
            )

        # Future-sight preflight: detect hidden execution chains (package /
        # build / script-runner / CI / git-hook / interpreter / local
        # script) BEFORE spawning. ADDITIVE — runs after the existing gate
        # cascade (which is preserved). Always audits the detection; only
        # ENFORCES (deny / freeze) when the operator-owned enforce flag is
        # on, so existing ai_run behavior is unchanged by default.
        _lc_refusal = _lifecycle_preflight(
            hub,
            root,
            command,
            session_id=result.session_id,
        )
        if _lc_refusal is not None:
            return _dispatch_run_output(_lc_refusal, {})

        _bc("E before spawn_detached")
        raw = spawn_detached(
            command,
            root,
            timeout_seconds=timeout_seconds,
            foreground=bool(foreground),
        )
        _bc("F after spawn_detached")
        if isinstance(raw, dict) and "command" not in raw:
            raw = {**raw, "command": command}
        # Command output guard: scan/redact the inline tail BEFORE it
        # reaches the agent. We own this return value, so redaction here
        # is real pre-context protection (unlike host Read).
        try:
            from .run_output_guard import guard_run_output

            raw = guard_run_output(
                raw,
                hub=hub,
                project_root=root,
                session_id=_caller_session(root),
                run_id=str(raw.get("run_id", "")) if isinstance(raw, dict) else "",
                command=command,
            )
        except Exception:
            pass
        _bc("G before dispatch_run_output")
        out = _dispatch_run_output(raw, {})
        _bc("H after dispatch_run_output")
        _bc("I before return")
        return out

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            # Running a test suite executes code + can emit artifacts; re-running
            # is not guaranteed to yield the same state, so it is NOT idempotent.
            # Must match _annotations() for its class (outer_gate_catalog).
            "idempotentHint": False,
            "openWorldHint": False,
            "title": "Run Tests",
        },
    )
    @renders_as("run_output", title="test")
    async def ai_test(
        framework: str = "",
        paths: str = "",
        name_filter: str = "",
        cwd: str = "",
        timeout_seconds: int = 900,
    ) -> Any:
        """Run the project's test suite — the subagent-SAFE test runner.

        Language-agnostic: auto-detects pytest / dotnet / cargo / go / npm
        from the project (or pass `framework=` to override). Unlike ai_run it
        executes ONLY the resolved test command in argv form (shell=False) —
        there is NO arbitrary-shell surface, so a lane worker can verify its
        work without the raw shell that could write to or evade gate code.

        framework: override auto-detection (pytest|dotnet|cargo|go|node).
        paths: optional whitespace-separated project-relative test paths
            (only for frameworks that accept them, e.g. pytest/node).
        name_filter: optional single test-name filter (mapped to -k / --filter
            / -run / positional per framework).
        cwd: optional project-relative subdir to run from (default: project
            root; AIDOCS auto-uses mcp/ when it holds pyproject.toml).
        """
        import asyncio

        from .shell_egress_service import ShellEgressRequest, default_service
        from .test_runner import TestRunnerError, resolve_test_command

        root = resolve_project_root()
        run_dir = root
        if cwd:
            sub = (root / cwd).resolve()
            try:
                sub.relative_to(root.resolve())
            except ValueError:
                return {"ok": False, "error": f"cwd escapes project root: {cwd!r}"}
            run_dir = sub
        elif (root / "mcp" / "pyproject.toml").is_file():
            run_dir = root / "mcp"

        path_list = [p for p in (paths or "").split() if p]
        try:
            fw, argv = resolve_test_command(
                run_dir, framework=framework, paths=path_list, name_filter=name_filter
            )
        except TestRunnerError as exc:
            return {"ok": False, "error": str(exc)}

        def _run():
            res = default_service().execute(
                ShellEgressRequest(
                    argv=tuple(argv),
                    cwd=str(run_dir),
                    timeout_s=float(timeout_seconds),
                    reachability="agent_reachable",
                    audit_tag="ai_test",
                ),
            )
            tail = res.stdout or ""
            if res.stderr:
                tail = (tail + "\n" + res.stderr) if tail else res.stderr
            return res, tail

        res, tail = await asyncio.to_thread(_run)
        return {
            "ok": bool(res.ok and res.rc == 0),
            "framework": fw,
            "rc": res.rc,
            "args": list(argv[1:]),  # omit the interpreter path (footprint/clarity)
            "duration_s": round(res.duration_s, 2),
            "output_tail": tail[-6000:],
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": True,
            "title": "Code Run",
        },
    )
    @renders_as("run_output", title="run")
    def ai_run(
        command: str = "",
        timeout_seconds: int = 600,
        foreground: bool = False,
        action: str = "",
        run_id: str = "",
        tail_bytes: int = 4096,
        raw_output: bool = False,
        confirm_token: str = "",
        scope: str = "session",
    ) -> Any:
        """Unified shell tool. Start, read output, kill, or list runs.

        Modes (the agent never has to remember a sibling tool name):

          action="start"   (default when `command` is set):
              spawn `command` detached. Returns {run_id, done, tail?,
              exit_code?}. Fast commands (version checks, quick
              linters) finish inside the 500ms inline window and return
              done=True + tail in one round-trip. Slow commands (test
              suites, builds) return done=False; the completion 📣
              notify is pushed into the next tool response, then read
              via action="output".

          action="output"  (default when only `run_id` is set):
              read the tail of a completed run's log. Run must be
              completed; the 📣 notify carries the readiness signal.
              `raw_output=true` skips the renderer pipeline (useful for
              JSON streams or further parsing).

          action="kill":
              terminate a running command by run_id. Two-phase confirm:
              first call without `confirm_token` returns the expected
              phrase ('confirm-kill <run_id>'); the second call with the
              matching phrase actually kills. Misclick-proof.

          action="list":
              return the active runs (run_id, command snippet,
              started_at, session_id) currently tracked in this
              process. Scope defaults to the caller's session (only
              runs the caller's session_id spawned); pass scope="all"
              to see every in-process run regardless of session. No
              state change. Historical runs (across pm2 restarts)
              live as sidecar files on disk and are not surfaced
              here — that's the dashboard's view.

        Start mode enforces bash_policy allow/deny + user-intent
        subcommand grants + heuristic judge (rm -rf, curl|sh,
        destructive chains). Output/kill/list bypass the judge — those
        modes only manipulate already-spawned process state.

        Backwards compat: `ai_run_output(run_id=...)` and
        `ai_run_kill(run_id=...)` remain as thin aliases that forward
        to action="output" and action="kill" respectively. New code
        should use `ai_run(action=...)` so the agent has one tool for
        the entire process lifecycle.

        foreground=true opens a visible terminal window (start mode
        only) so the operator can watch. Agent gets no output in that
        mode. Useful for `npm run dev`, `docker compose up`, etc.
        """
        # Resolve action: explicit `action` wins; otherwise infer from
        # which arg the caller supplied. `command` ⇒ start (matches
        # pre-action-arg behavior); only `run_id` ⇒ output.
        if not action:
            if command:
                action = "start"
            elif run_id:
                action = "output"
            else:
                action = "list"

        if action == "start":
            if not command:
                return {"error": "missing_command", "detail": "action='start' requires command"}
            return _run_shell_unified(
                command,
                timeout_seconds=timeout_seconds,
                foreground=foreground,
            )
        if action == "output":
            if not run_id:
                return {"error": "missing_run_id", "detail": "action='output' requires run_id"}
            # Forward to the sibling closure (defined below in the
            # same `register_run_tools` scope; Python resolves names
            # at call time, so the forward reference is fine).
            return ai_run_output(run_id=run_id, tail_bytes=tail_bytes, raw_output=raw_output)
        if action == "kill":
            if not run_id:
                return {"error": "missing_run_id", "detail": "action='kill' requires run_id"}
            expected = f"confirm-kill {run_id}"
            if (confirm_token or "").strip() != expected:
                return {
                    "_error": "confirm_required",
                    "_detail": (
                        "action='kill' is confirmation-gated; "
                        "ask the user before re-invoking with "
                        "confirm_token"
                    ),
                    "action": "kill",
                    "run_id": run_id,
                    "confirm_token": expected,
                    "summary": (
                        f"About to kill run {run_id!r}. The user must "
                        f"confirm before this terminates the process."
                    ),
                }
            return ai_run_kill(run_id=run_id)
        if action == "list":
            # List active detached runs scoped to the caller. By
            # default returns runs spawned by the caller's session
            # ("mine"); operators can pass scope="all" to see every
            # in-process run (admin / debugging). Runs are read from
            # _LIVE_RUNS — historical runs (across pm2 restarts) live
            # as status sidecars on disk and aren't surfaced here.
            from .code_runner_detached import (
                _LIVE_RUNS,
                _LIVE_RUNS_LOCK,
            )
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            caller_sid = current_calling_host_session_id() or ""
            normalized_scope = (scope or "session").lower().strip()
            want_all = normalized_scope == "all"
            with _LIVE_RUNS_LOCK:
                items = []
                for rid, lr in _LIVE_RUNS.items():
                    lr_sid = getattr(lr, "session_id", "") or ""
                    if not want_all and caller_sid and lr_sid != caller_sid:
                        continue
                    items.append(
                        {
                            "run_id": rid,
                            "command": getattr(lr, "command", "")[:120],
                            "started_at": getattr(lr, "started_at", 0),
                            "session_id": lr_sid,
                        },
                    )
            return {
                "runs": items,
                "scope": "all" if want_all else "session",
                "caller_session_id": caller_sid,
            }
        return {
            "error": "unknown_action",
            "detail": (f"action={action!r} not recognized (expected: start, output, kill, list)"),
        }

    # ai_run_status DEPRECATED 2026-04-23. Notify-on-done already
    # pushes a 📣 completion block into the next tool response; polling
    # status is redundant and burns context. ai_run_output is itself
    # gated to return only after the run completes. If you genuinely
    # need status, the 📣 notify carries exit code + duration.

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Run Output",
        },
    )
    @renders_as("run_output", title="run output")
    def ai_run_output(
        run_id: str,
        tail_bytes: int = 4096,
        wait_seconds: float = 0.0,  # retained for API compat; ignored
        raw_output: bool = False,
    ) -> Any:
        """Read the tail of a completed run's log.

        Hard rule (2026-04-23): the run MUST be completed. While the
        run is still executing, the call is refused with a pointer to
        the pending 📣 notify block — every detached run pushes a
        completion notification into the next tool response, so
        polling is never needed. Wait for the notify, then read.

        `wait_seconds` parameter is accepted for backwards-compat but
        ignored — waiting inside a tool call is an anti-pattern when
        notify-on-done is universal.

        `raw_output` (Phoenix 2026-05-09): when True, skip the
        renderer pipeline and return the tail bytes verbatim. Useful
        when the classify/render-test/render-build summary loses
        information you need raw — JSON event streams (e.g. opencode
        run --format json), log file traces, command output meant
        for further parsing. Default False preserves the rendered
        view that's friendlier for casual reads.
        """
        from .code_runner_detached import (
            get_run_status,
            tail_run_log,
        )
        from .tool_display import _dispatch_run_output

        root = resolve_project_root()

        try:
            status = get_run_status(root, run_id)
            state = str(status.get("state", "")) if isinstance(status, dict) else ""
        except Exception:
            state = ""  # fall through to tail read

        if state == "unknown":
            refusal = {
                "ok": False,
                "run_id": run_id,
                "blocked_by": "run_unknown",
                "reason": (
                    "No record of this run_id. It may be stale, from "
                    "a prior MCP process, or its artifacts were "
                    "evicted. Start a fresh ai_run."
                ),
            }
            return _dispatch_run_output(refusal, {})

        if state == "running":
            refusal = {
                "ok": False,
                "run_id": run_id,
                "blocked_by": "run_not_complete",
                "reason": (
                    "Run still executing. Notify-on-done is universal "
                    "— a 📣 completion block will arrive in your next "
                    "tool response. Retrieve output AFTER that notify. "
                    "Do other work in the meantime."
                ),
                "run_id_pending_notify": run_id,
            }
            return _dispatch_run_output(refusal, {})

        # #50 L4 (canonical 2026-04-26): cross-session refusal.
        # The .status sidecar carries session_id at completion time
        # (#50 L3 stamped it). If the calling conductor's session
        # doesn't match the spawn session, refuse — runs spawned by
        # one conductor must not be readable by another. Empty
        # spawn-session (legacy sidecar or unattributed run) falls
        # through to back-compat unscoped read.
        try:
            from . import managed_mode_service as _mm_l4
            from .code_runner_detached import _read_status_sidecar
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id as _ccsid_l4,
            )

            sidecar = _read_status_sidecar(root, run_id)
            spawn_session = str(sidecar.get("session_id") or "").strip() if sidecar else ""
            if spawn_session:
                host_sid = _ccsid_l4()
                managed = _mm_l4.ManagedModeService().get_mode(
                    root,
                    host_session_id=host_sid,
                )
                caller_session = (
                    str(managed.get("session_id") or "").strip() if managed.get("active") else ""
                )
                if caller_session and caller_session != spawn_session:
                    refusal = {
                        "ok": False,
                        "run_id": run_id,
                        "blocked_by": "cross_session_run_id",
                        "reason": (
                            f"run_id={run_id} was spawned by session "
                            f"'{spawn_session}'; current session is "
                            f"'{caller_session}'. Cross-session output "
                            f"reads refused (#50)."
                        ),
                        "spawn_session_id": spawn_session,
                        "caller_session_id": caller_session,
                    }
                    return _dispatch_run_output(refusal, {})
        except Exception:
            # Refusal gate must never crash the read path; sidecar/
            # managed_mode failures fall through to the legacy
            # unscoped read for the same defensive reason.
            pass

        raw = tail_run_log(
            root,
            run_id,
            tail_bytes=int(tail_bytes),
            wait_seconds=0.0,
        )

        # Backlog #86 spec D + E: artifacts persist after read. The
        # agent gets the same rendered view on re-read while the
        # artifact is retained; eviction is mtime-based via
        # evict_old_logs (RUNS_DIR_SIZE_CAP_BYTES ceiling) plus the
        # age-based sweep in delete_old_artifacts. The previous
        # delete-on-read behavior (#32) is replaced — single-read
        # semantics caused diagnostic data loss when the rendered
        # summary truncated and the operator needed the raw log.

        # Phoenix 2026-05-07: dismiss the run's pending notification
        # now that the agent has actually read the output — closes
        # the 'until satisfied' lifecycle (Emperor 2026-05-07).
        try:
            from . import run_notifications as _rn_dismiss

            _rn_dismiss.dismiss_run(root, run_id=run_id)
        except Exception:
            pass

        # Command output guard runs on BOTH return paths. raw_output=True
        # skips the RENDERER, not the secret scan — the guard mutates the
        # tail in place before either path returns it to the agent.
        try:
            from .run_output_guard import guard_run_output

            raw = guard_run_output(
                raw,
                hub=hub,
                project_root=root,
                session_id=_caller_session(root),
                run_id=str(run_id),
            )
        except Exception:
            pass

        if raw_output:
            # Phoenix 2026-05-09: skip the renderer pipeline; return
            # the dict from tail_run_log verbatim. JSON event streams
            # (opencode run --format json), log traces, and any
            # output meant for further parsing need the bytes
            # uncategorized — render_test/render_build/render_probe
            # would summarize them away.
            return raw
        return _dispatch_run_output(raw, {})

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Code Run Kill",
        },
    )
    @renders_as("status", title="run kill")
    def ai_run_kill(run_id: str) -> Any:
        """Stop a running subprocess. No-op if already finished.

        Slice 1 (canonical 2026-04-29) gates: managed-mode session
        ownership (cross-session run_ids refused like ai_run_output),
        existing-freeze refusal (a frozen session must not kill a peer
        run), and a refusal audit event so silently-blocked kills
        appear in execution_events. ai_run side has its own gates;
        this tool used to be completely ungated.
        """
        from .code_runner_detached import (
            _read_status_sidecar,
            kill_run,
        )
        from .enforcement import (
            is_kill_switch_active,
            record_kill_switch_bypass,
        )
        from .managed_mode_service import ManagedModeService

        root = resolve_project_root()

        # [BREAK-GLASS] dev.kill_switch — checked BEFORE freeze
        # short-circuit so a frozen session can still terminate a
        # runaway process on a dev-flavor install. Castle law: the
        # emergency key must hang outside the prison cell.
        if is_kill_switch_active(root):
            record_kill_switch_bypass(
                root,
                source="ai_run_kill",
                target="ai_run_kill",
                payload={"run_id": str(run_id)[:200]},
            )
            from .code_runner_detached import kill_run as _kill_now

            try:
                _kill_now(root, run_id)
            except Exception:
                pass
            return {"ok": True, "run_id": run_id, "bypassed": True}

        # Resolve calling session via the same helper ai_run_output
        # uses. Empty host_session_id falls back to singleton get_mode
        # (#58 contract), matching the existing #50 L4 path.
        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id as _ccsid,
            )

            host_sid = _ccsid()
        except Exception:
            host_sid = ""
        try:
            managed = ManagedModeService().get_mode(
                root,
                host_session_id=host_sid,
            )
        except Exception:
            managed = {}
        caller_session = (
            str(managed.get("session_id") or "").strip()
            if isinstance(managed, dict) and managed.get("active")
            else ""
        )

        def _refuse(
            blocked_by: str,
            reason: str,
            *,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            refusal: dict[str, Any] = {
                "ok": False,
                "run_id": run_id,
                "blocked_by": blocked_by,
                "reason": reason,
            }
            if extra:
                refusal.update(extra)
            try:
                hub.execution.record_event(
                    root,
                    event_kind="tool_call_refused",
                    source_kind="ai_run_kill",
                    session_id=caller_session or None,
                    capability_name="ai_run_kill",
                    action_kind="kill",
                    target_entity=run_id[:200],
                    status="refused",
                    payload={
                        "run_id": run_id,
                        "blocked_by": blocked_by,
                        "reason": reason,
                    },
                )
            except Exception:
                pass
            return refusal

        # Freeze short-circuit. Mirrors the ai_run contract: while a
        # session is frozen pending operator confirmation, every gate
        # surface returns the same envelope.

        # Freeze gate intentionally NOT consulted here.
        # Doctrine: operation_class.DESTRUCTIVE_CLEANUP, 2026-05-04.
        # Remedial operations bypass the freeze gate — a frozen
        # session must STILL be able to kill a runaway process,
        # that's part of how the operator EXITS the freeze. The old
        # inline short-circuit contradicted the operation_class
        # registry; deleted in lane 2 phase 2.

        # Cross-session run_id refusal. The .status sidecar records
        # session_id at completion time (#50 L3). For still-running
        # rows the sidecar is absent; fall back to permitting the
        # kill so an operator can stop a runaway under managed mode
        # the conductor itself owns. (Sidecar-less legacy rows pre-
        # #50 are also permitted to preserve back-compat.)
        try:
            sidecar = _read_status_sidecar(root, run_id)
            spawn_session = str(sidecar.get("session_id") or "").strip() if sidecar else ""
        except Exception:
            spawn_session = ""
        if spawn_session and caller_session and spawn_session != caller_session:
            return _refuse(
                "cross_session_run_id",
                (
                    f"run_id={run_id} was spawned by session "
                    f"'{spawn_session}'; current session is "
                    f"'{caller_session}'. Cross-session kills refused."
                ),
                extra={
                    "spawn_session_id": spawn_session,
                    "caller_session_id": caller_session,
                },
            )

        return kill_run(root, run_id)
