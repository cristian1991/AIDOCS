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

from .run_tool_contracts import (
    AI_RUN_ANNOTATIONS,
    AI_RUN_DESCRIPTION,
    AI_RUN_KILL_ANNOTATIONS,
    AI_RUN_KILL_DESCRIPTION,
    AI_RUN_OUTPUT_ANNOTATIONS,
    AI_RUN_OUTPUT_DESCRIPTION,
    RunAction,
    RunScope,
)
from .mcp_server_runtime_helpers import (
    require_active_task,
    resolve_project_root,
)
from .tool_display import renders_as

# #466 client-idle guard: MCP clients kill a tool call that produces no
# response for roughly 300s, which used to orphan the server-side pytest
# (verdict lost, zombie load). No governed call may therefore BLOCK longer
# than this; long runs return/reattach via run_id (ai_run action='wait' /
# 'output') instead of dying with the client.
CLIENT_IDLE_GUARD_SECONDS = 240

# #757: DETACH EARLY, KEEP OWNERSHIP. The inline wait used to run to the
# 240s client-idle guard, so a caller sat blocked for four minutes before
# learning the run was still going. Ownership never depended on that wait:
# the run is spawned detached, its lifetime is granted at birth (job object,
# clamped to 30 min), and its verdict lands in the run log either way -- so
# blocking bought nothing but latency, and the long block is what made the
# detach look like an abandonment.
#
# 3s is the operator's number: long enough that genuinely fast suites still
# answer inline (measured: most single-file runs finish under it), short
# enough that a slow one is handed back immediately and POLLED via
# ai_run(action='output') rather than held.
DETACH_AFTER_SECONDS = 3.0


def resolve_run_cwd(project_root: Any, cwd: str) -> Any:
    """Resolve the run directory for ai_run / ai_test (#477, Wars BA/S).

    ``cwd`` is project-relative and must stay inside the workspace —
    an escaping path returns a structured refusal dict. With no ``cwd``
    the default matches ai_test's auto-mcp rule: run from ``mcp/`` when
    it holds the project's pyproject.toml, else the project root — so
    ai_run and ai_test agree on where toolchain commands execute.
    """
    from pathlib import Path

    root = Path(project_root)
    if cwd:
        target = root / cwd
        try:
            target.resolve().relative_to(root.resolve())
        except ValueError:
            return {
                "ok": False,
                "err": f"cwd escapes project root: {cwd!r}",
                "blocked_by": "cwd_escapes_workspace",
            }
        return target
    if (root / "mcp" / "pyproject.toml").is_file():
        return root / "mcp"
    return root


def venv_augmented_env(
    project_root: Any, run_dir: Any, base_env: Any
) -> dict[str, str] | None:
    """Child env with the project venv's Scripts/bin dir FIRST on PATH.

    War AZ (#469): ai_run 'ruff ...' exited 127 because .venv/Scripts was
    not on the spawned child's PATH. Candidates in preference order:
    run_dir/.venv, root/.venv, root/mcp/.venv. Returns None when no venv
    exists (caller lets the child inherit unchanged).
    """
    import os
    from pathlib import Path

    bin_name = "Scripts" if os.name == "nt" else "bin"
    candidates = (
        Path(run_dir) / ".venv",
        Path(project_root) / ".venv",
        Path(project_root) / "mcp" / ".venv",
    )
    for cand in candidates:
        vbin = cand / bin_name
        if vbin.is_dir():
            env = {str(k): str(v) for k, v in dict(base_env).items()}
            env["PATH"] = str(vbin) + os.pathsep + env.get("PATH", "")
            return env
    return None


def register_run_tools(
    *, server: Any, hub: Any, runtime: Any, expose_run: bool = False
) -> None:
    """Register ai_test (always) and the ai_run trio (only when expose_run).

    ONE RUNNER PER SURFACE (operator ruling 2026-07-26). The axis is HARNESS vs
    NO-HARNESS, not geography: a LOCAL agent — the operator's keywords are
    ``local``, ``serveragent`` and ``remoteagent`` — already holds a shell that
    the AIDOCS gate governs (PreToolUse -> shell_enforcement / bash_policy /
    heuristic_judge), so routing it through ai_run is a governance detour to
    reach a capability it already has. A WebMCP caller has NO harness and no
    shell, so the governed dispatcher is the only way it can run anything.

    Having BOTH routes produced a DEADLOCK: policy refused a deploy via ai_run
    ("use the Bash tool") while the Bash allowlist refused `bash` ("requires
    operator confirmation") — each refusal pointing at the other, leaving the
    action unreachable by any harness agent.

    expose_run mirrors expose_deploy exactly, including its fail-safe
    direction: DEFAULT False, so a call site that FORGETS the flag HIDES the
    runner rather than exposing it. Only the gate's internal execution servers
    opt in with True. ai_test is NOT gated — it is a normal tier-M action that
    runs only the resolved test argv (shell=False), so a remote caller needs it
    exactly as much as a local one, and it stays surface=BOTH.
    """
    # Conditional decoration: `@_run_tool(...)` registers on the gate's
    # execution servers and is a no-op everywhere else. Declaring the functions
    # unconditionally (and only binding them when exposed) keeps one definition
    # of each contract instead of forking the module.
    if expose_run:
        _run_tool = server.tool
    else:

        def _run_tool(*_a: Any, **_k: Any) -> Any:
            def _skip(fn: Any) -> Any:
                return fn

            return _skip

    def _caller_session(root: Any) -> str:
        """Authoritative calling-session id for run-notification attribution.

        Isolation law (2026-07-09): a run's session attribution must come from
        an AUTHORITATIVE calling host-session identity — NEVER borrowed from the
        project's managed-mode singleton. In the shared HTTP daemon (one
        process, many host windows) a call with no host_session_id (a hooks-off
        agent) that fell through to get_mode's singleton_fallback inherited
        WHICHEVER session owns the resolved project_root — cross-attributing one
        tenant's run-completion to another (a DentalClinic agent's ai_run
        surfacing in AIDOCS's ubermega notification drain).

        When the caller's host-session identity is unknown, return '' —
        unattributed. Per #50 an empty-session record is invisible to every
        session's filtered drain and GC'd after the grace window, so it can
        never leak into another session's context. This changes ONLY the
        empty-host_session_id path; a bound session (host_session_id present)
        resolves exactly as before.
        """
        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id as _ccsid,
            )

            sid = _ccsid()
            if not sid:
                # No authoritative caller identity → unattributed (isolation).
                return ""
            from .managed_mode_service import (
                ManagedModeService,
                resolve_managed_session,
            )

            return resolve_managed_session(
                ManagedModeService(),
                root,
                host_session_id=sid,
            )
        except Exception:
            pass
        return ""

    def _caller_actor(root: Any) -> str:
        try:
            from .mcp_server_runtime_helpers import current_calling_agent_context_id

            return current_calling_agent_context_id(root)
        except Exception:
            return ""

    def _caller_lane() -> str:
        import os

        return os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()

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
        if action == "confirm":
            # #571 three-way routing, BEFORE the session check below. A rung-3
            # lifecycle block needs no session and no freeze, so an unlisted
            # lifecycle family no longer hard-denies for want of a session.
            from .verdict_class import OUTCOME_ALLOW, OUTCOME_BLOCK, outcome_for

            _lc_outcome, _lc_class = outcome_for(risk_class=f"lifecycle:{fam}")
            if _lc_outcome == OUTCOME_ALLOW:
                return None
            if _lc_outcome == OUTCOME_BLOCK:
                from .freeze_service import build_workflow_block_response

                _blk = build_workflow_block_response(
                    tool_name="ai_run",
                    tool_input={"command": command},
                    reason=f"hidden execution chain ({fam}): {reason}",
                    verdict_class=_lc_class,
                )
                return {
                    "ok": False,
                    "err": _blk["permissionDecisionReason"],
                    "command": command,
                    "block_state": _blk["block_state"],
                    "blocked_by": _blk["blocked_by"],
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
        cwd: str = "",
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

        # #465: pure-sleep spawn refusal. Agents polling a pending run by
        # spawning `sleep N` / `python -c "time.sleep(N)"` / `Start-Sleep`
        # / `timeout /t N` burn process slots and leave orphans (#456).
        # Structural refusal BEFORE any spawn; the message names the
        # governed wait affordances. Refusal carries the #449 role-branch
        # reporting footer via refusal_with_affordance.
        from .heuristic_judge import (
            SLEEP_SPAWN_HINT,
            SLEEP_SPAWN_RULE_ID,
            detect_sleep_spawn,
        )

        _sleep_evidence = detect_sleep_spawn(command)
        if _sleep_evidence:
            from .tool_gate_service import refusal_with_affordance

            _sleep_reason = refusal_with_affordance(
                (
                    f"Refused: `{_sleep_evidence}` is a pure-sleep spawn "
                    f"used as a wait — no process is spawned for waiting. "
                    f"{SLEEP_SPAWN_HINT}"
                ),
                SLEEP_SPAWN_RULE_ID,
                project_root=root,
            )
            try:
                hub.execution.record_event(
                    root,
                    event_kind="sleep_spawn_refused",
                    source_kind="ai_run",
                    session_id=_caller_session(root) or None,
                    capability_name="ai_run",
                    action_kind="block",
                    target_entity=_sleep_evidence[:200],
                    status="blocked",
                    payload={"command": command[:400]},
                )
            except Exception:
                pass
            return _dispatch_run_output(
                {
                    "ok": False,
                    "err": _sleep_reason,
                    "blocked_by": "sleep_spawn_refused",
                    "command": command,
                },
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

        # #477 (Wars AZ/BA/S): honor a project-relative cwd (validated inside
        # the workspace), default to ai_test's auto-mcp run dir, and prepend
        # the project venv's Scripts/bin to the child PATH so `ruff` /
        # `pytest` resolve without absolute paths.
        run_dir = resolve_run_cwd(root, cwd)
        if isinstance(run_dir, dict):
            return _dispatch_run_output({**run_dir, "command": command}, {})
        import os as _os_env

        _child_env = venv_augmented_env(root, run_dir, _os_env.environ)

        _bc("E before spawn_detached")
        raw = spawn_detached(
            command,
            root,
            timeout_seconds=timeout_seconds,
            foreground=bool(foreground),
            cwd=run_dir,
            env=_child_env,
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

        Python interpreter: `test.interpreter` if set, else <test root>/.venv.
        Declare it on any SYNCED checkout — `.venv` is untracked, so a clone
        never carries one and discovery alone cannot succeed there. The refusal
        names every path it tried.

        #456/#466 governance: the pytest worker count (-n) derives from the
        box profile (small operator box: agents -n 2 max, conductor serial;
        big boxes scale); the caller's timeout_seconds is honored exactly up
        to the box ceiling (over-ceiling requests get an explicit refusal);
        and the call never blocks past the client-idle guard — a long suite
        detaches with a run_id for ai_run(action='wait'/'output') reattach,
        so a client-side death can never orphan the server-side run.
        """
        import asyncio
        import os

        from .box_profile import (
            get_box_profile,
            resolve_run_timeout,
            resolve_test_workers,
        )
        from .code_runner_detached import (
            get_run_status,
            spawn_detached,
            tail_run_log,
        )
        from .test_runner import (
            TestRunnerError,
            build_test_env,
            interpreter_drift,
            resolve_test_command,
        )

        root = resolve_project_root()
        # #477: ONE cwd law for ai_test and ai_run (resolve_run_cwd) — the
        # two tools must agree on where toolchain commands execute.
        run_dir = resolve_run_cwd(root, cwd)
        if isinstance(run_dir, dict):
            return {**run_dir, "error": run_dir.get("err", "invalid cwd")}

        path_list = [p for p in (paths or "").split() if p]
        # DECLARED INTERPRETER (2026-08-29). Read here rather than inside
        # test_runner so the runner stays free of config knowledge — the policy
        # lives at the tool layer, the mechanism in the resolver.
        #
        # Needed because DISCOVERY CANNOT WORK ON A CLONE: `.venv` is untracked,
        # so the gate's synced tenant checkout can never contain one. Measured
        # live — ai_test reached `mcp/` correctly and still refused, because
        # mcp/.venv does not exist there and structurally cannot.
        from .config import get_setting as _get_setting

        _declared_interp = str(
            _get_setting("test.interpreter", project_root=root, default="") or ""
        ).strip()
        try:
            fw, argv = resolve_test_command(
                run_dir,
                framework=framework,
                paths=path_list,
                name_filter=name_filter,
                interpreter=_declared_interp,
            )
        except TestRunnerError as exc:
            return {"ok": False, "error": str(exc)}

        # The resolved interpreter's own bin/Scripts dir goes FIRST on the
        # child's PATH — build_test_env owns that, and its docstring says why it
        # takes the resolved path instead of searching for a venv the way
        # ai_run's venv_augmented_env does.
        run_env = build_test_env(
            fw,
            os.environ,
            argv[0] if argv and os.path.isabs(argv[0]) else "",
        )

        # #456: box-adaptive worker budget. Lane workers get the agent
        # cap; a caller without a lane id sits in the conductor seat.
        profile = get_box_profile()
        is_lane_worker = bool(os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip())
        workers, worker_rationale = resolve_test_workers(
            fw,
            run_dir,
            is_lane_worker=is_lane_worker,
            profile=profile,
        )
        if workers is not None:
            # Explicit -n LAST so it overrides any addopts `-n auto`.
            argv = [*argv, "-n", str(workers)]

        # #466: honor the caller's timeout exactly up to the box ceiling;
        # over-ceiling is an explicit refusal (never a silent clamp).
        verdict = resolve_run_timeout(
            timeout_seconds,
            tool_default=900,
            project_root=root,
        )
        if not verdict["ok"]:
            return {**verdict, "framework": fw}
        governed_timeout = int(verdict["timeout_seconds"])
        governance = {
            "timeout_seconds": governed_timeout,
            "timeout_governed_by": verdict["timeout_governed_by"],
            "timeout_ceiling_seconds": verdict["timeout_ceiling_seconds"],
            "box_profile": verdict["box_profile"],
            "workers": workers,
            "worker_rationale": worker_rationale,
        }

        # DRIFT OF THE DECLARED INTERPRETER (2026-08-29). Only checked when one
        # was DECLARED: a discovered `<cwd>/.venv` lives beside the tree and moves
        # with it, so it has no drift to report, and paying a subprocess on the
        # common local path to say nothing would be waste. The declared case is
        # exactly the one that can be a stale snapshot.
        #
        # Rides `governance` because that is already the "how this run was
        # decided" envelope and it is returned on EVERY path, including the early
        # spawn-failure return — a drift note that only appeared on success would
        # be missing precisely when the run blew up and the reader needed it.
        if _declared_interp and argv:
            drift = interpreter_drift(argv[0], run_dir)
            if drift:
                governance["interpreter_drift"] = drift

        # Detached spawn in argv mode (shell=False end-to-end): the run's
        # lifetime belongs to the SERVER, not to this request — if the
        # client dies mid-call, the suite keeps its verdict in the run log
        # and the 📣 notify still fires. No orphaned pytest, ever.
        raw = spawn_detached(
            "",
            root,
            timeout_seconds=governed_timeout,
            cwd=run_dir,
            argv_command=[str(t) for t in argv],
            env=run_env,
        )
        if not isinstance(raw, dict) or not raw.get("ok"):
            out = dict(raw) if isinstance(raw, dict) else {"ok": False, "err": str(raw)}
            out.setdefault("framework", fw)
            out["governance"] = governance
            return out
        run_id = str(raw.get("run_id", ""))

        state = "done" if raw.get("done") else "running"
        if state == "running":
            # Inline wait bounded by the client-idle guard so this call
            # can never be killed client-side while the server blocks.
            # #757: hand back after DETACH_AFTER_SECONDS, not after the 240s
            # idle guard. Ownership is unaffected -- the run is detached with a
            # lifetime granted at birth and stays pollable via its run_id.
            inline_budget = float(min(governed_timeout, DETACH_AFTER_SECONDS))
            import time as _time_t

            deadline = _time_t.monotonic() + inline_budget
            while _time_t.monotonic() < deadline:
                try:
                    status = get_run_status(root, run_id)
                    state = str(status.get("state", "")) if isinstance(status, dict) else ""
                except Exception:
                    state = ""
                if state != "running":
                    break
                await asyncio.sleep(min(1.0, max(0.05, deadline - _time_t.monotonic())))

        if state == "running":
            # Suite still going — hand the run back instead of dying at
            # the client's ~300s idle cap. The run continues under its
            # governed timeout; reattach via the existing wait affordance.
            return {
                "ok": True,
                "done": False,
                "detached": True,
                "run_id": run_id,
                "framework": fw,
                "args": list(argv[1:]),
                "reason": (
                    f"Suite still running after the {DETACH_AFTER_SECONDS:g}s "
                    f"client-idle guard window. It CONTINUES server-side under its "
                    f"{governed_timeout}s timeout — reattach with "
                    f"ai_run(action='wait', run_id='{run_id}') or read after the "
                    f"📣 notify with ai_run(action='output', run_id='{run_id}')."
                ),
                "governance": {**governance, "call_returned_by": "idle"},
            }

        # Completed within the inline window — deliver the verdict now and
        # dismiss the completion notify (already consumed here).
        tail_info = tail_run_log(root, run_id, tail_bytes=8192)
        status = get_run_status(root, run_id)
        rc_val = tail_info.get("exit_code") if isinstance(tail_info, dict) else None
        if rc_val is None and isinstance(status, dict):
            rc_val = status.get("exit_code")
        rc = -1 if rc_val is None else int(rc_val)
        duration = None
        if isinstance(status, dict) and status.get("duration_seconds") is not None:
            duration = round(float(status["duration_seconds"]), 2)
        try:
            from . import run_notifications as _rn_test_dismiss

            _rn_test_dismiss.dismiss_run(
                root,
                run_id=run_id,
                session_id=_caller_session(root),
                agent_context_id=_caller_actor(root),
                lane_id=_caller_lane(),
            )
        except Exception:
            pass
        tail = str(tail_info.get("tail") or "") if isinstance(tail_info, dict) else ""
        return {
            "ok": rc == 0,
            "framework": fw,
            "rc": rc,
            "args": list(argv[1:]),  # omit the interpreter path (footprint/clarity)
            "duration_s": duration,
            "output_tail": tail[-6000:],
            "run_id": run_id,
            "governance": governance,
        }

    @_run_tool(
        description=AI_RUN_DESCRIPTION,
        annotations=dict(AI_RUN_ANNOTATIONS),
    )
    @renders_as("run_output", title="run")
    async def ai_run(
        command: str = "",
        timeout_seconds: int = 600,
        foreground: bool = False,
        cwd: str = "",
        action: RunAction = "",
        run_id: str = "",
        tail_bytes: int = 4096,
        raw_output: bool = False,
        confirm_token: str = "",
        scope: RunScope = "session",
    ) -> Any:
        """Unified shell tool. Start, read output, kill, or list runs.
        Modes:
          action="start" (default when `command` is set) — spawn `command`
              detached → {run_id, done, tail?, exit_code?}. Fast commands
              finish inside the 500ms inline window (done=True + tail in
              one round-trip); slow ones return done=False and push a 📣
              completion notify into the next tool response — never poll.
          action="output" (default when only `run_id` is set) — tail of a
              run's log. Completed → full result; still RUNNING → progress
              snapshot (elapsed + bounded live tail, #484); the 📣 notify
              still arrives on completion.
              raw_output=true skips the renderer (JSON streams, parsing).
          action="wait" — governed block on an EXISTING run (#465): tail on
              completion within timeout_seconds, structured
              {blocked_by: "wait_timeout"} otherwise (run keeps executing).
          action="kill" — two-phase confirm: first call returns the phrase
              ('confirm-kill <run_id>'); echo it in confirm_token to kill.
          action="list" — active runs for this session (scope="all" for
              every in-process run). Read-only.

        cwd (start only): project-relative run directory, validated inside
        the workspace. Default matches ai_test: mcp/ when it holds the
        project's pyproject.toml, else the project root. The child PATH
        gets the project venv's Scripts/bin prepended so `ruff` / `pytest`
        resolve without absolute paths (#477).

        Start enforces bash_policy + user-intent grants + heuristic judge
        (rm -rf, curl|sh, destructive chains); output/wait/kill/list only
        touch already-spawned state. foreground=true (start only) opens a
        visible terminal for the operator — agent gets no output.
        ai_run_output / ai_run_kill remain thin aliases; prefer
        ai_run(action=...).
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
                cwd=cwd,
            )
        if action == "output":
            if not run_id:
                return {"error": "missing_run_id", "detail": "action='output' requires run_id"}
            # Forward to the sibling closure (defined below in the same
            # `register_run_tools` scope). ai_run_output is a @server.tool, which
            # the daemon registers as an ASYNC callable — calling it here returns
            # a COROUTINE, so awaiting it is required (else the coroutine leaked
            # into the result: "<coroutine object ai_run_output>"). Guarded for
            # the sync case (standalone / future refactor) so it's robust either
            # way. (2026-07-10 fix.)
            import inspect as _inspect_out

            _res = ai_run_output(run_id=run_id, tail_bytes=tail_bytes, raw_output=raw_output)
            return (await _res) if _inspect_out.iscoroutine(_res) else _res
        if action == "wait":
            # #465 governed wait: block on an EXISTING run's completion.
            # No new process is spawned; polling happens in-server on the
            # async loop (event loop stays free — asyncio.sleep, never a
            # thread-blocking sleep). On completion the read is forwarded
            # to ai_run_output, so the ownership law (#50 L4), the output
            # guard, and the notify-dismiss lifecycle all apply unchanged.
            if not run_id:
                return {"error": "missing_run_id", "detail": "action='wait' requires run_id"}
            import asyncio as _asyncio_wait
            import inspect as _inspect_wait
            import time as _time_wait

            from .code_runner_detached import get_run_status
            from .tool_display import _dispatch_run_output

            root = resolve_project_root()

            def _state() -> str:
                try:
                    status = get_run_status(root, run_id)
                    return str(status.get("state", "")) if isinstance(status, dict) else ""
                except Exception:
                    return ""

            state = _state()
            if state == "unknown":
                return _dispatch_run_output(
                    {
                        "ok": False,
                        "run_id": run_id,
                        "blocked_by": "run_unknown",
                        "reason": (
                            "No record of this run_id. It may be stale, from "
                            "a prior MCP process, or its artifacts were "
                            "evicted. Start a fresh ai_run."
                        ),
                    },
                    {},
                )
            _requested_budget = max(1.0, float(timeout_seconds or 0))
            # #466 client-idle guard: a wait that blocks past ~300s gets
            # killed CLIENT-side (the run itself is unharmed, but the
            # caller loses the response). Slice each wait call below the
            # guard; the caller simply waits again — the run keeps going.
            _budget = min(_requested_budget, float(CLIENT_IDLE_GUARD_SECONDS))
            _sliced = _budget < _requested_budget
            _deadline = _time_wait.monotonic() + _budget
            while state == "running":
                _remaining = _deadline - _time_wait.monotonic()
                if _remaining <= 0:
                    return _dispatch_run_output(
                        {
                            "ok": False,
                            "run_id": run_id,
                            "blocked_by": "wait_timeout",
                            "reason": (
                                f"Run still executing after waiting "
                                f"{int(_budget)}s"
                                + (
                                    f" (per-call wait sliced from the requested "
                                    f"{int(_requested_budget)}s to stay under the "
                                    f"client's ~300s idle cap)"
                                    if _sliced
                                    else ""
                                )
                                + f". The process keeps running "
                                f"— call ai_run(action='wait', run_id="
                                f"'{run_id}') again, do other work until the "
                                f"📣 notify, or ai_run(action='kill')."
                            ),
                            "waited_seconds": int(_budget),
                            "wait_governed_by": ("idle" if _sliced else "requested"),
                        },
                        {},
                    )
                await _asyncio_wait.sleep(min(1.0, _remaining))
                state = _state()
            # Completed (or state undecidable → let ai_run_output's own
            # refusal law decide). Forward for the tail + ownership gates.
            _res_w = ai_run_output(run_id=run_id, tail_bytes=tail_bytes, raw_output=raw_output)
            return (await _res_w) if _inspect_wait.iscoroutine(_res_w) else _res_w
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
            import inspect as _inspect_kill

            _res_k = ai_run_kill(run_id=run_id)
            return (await _res_k) if _inspect_kill.iscoroutine(_res_k) else _res_k
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
            if want_all:
                from .mcp_server_runtime_helpers import current_gate_principal
                from .outer_gate_project_acl import is_org_admin

                gate_principal = current_gate_principal()
                if gate_principal is not None and not is_org_admin(gate_principal):
                    return {
                        "ok": False,
                        "blocked_by": "org_admin_required",
                        "reason": (
                            "scope='all' may expose runs owned by other sessions; "
                            "remote callers must be an org administrator"
                        ),
                        "scope": "all",
                        "caller_session_id": caller_sid,
                    }
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
            "detail": (
                f"action={action!r} not recognized "
                "(expected: start, output, wait, kill, list)"
            ),
        }

    # ai_run_status DEPRECATED 2026-04-23. Notify-on-done already
    # pushes a 📣 completion block into the next tool response; polling
    # status is redundant and burns context. ai_run_output is itself
    # gated to return only after the run completes. If you genuinely
    # need status, the 📣 notify carries exit code + duration.

    @_run_tool(
        description=AI_RUN_OUTPUT_DESCRIPTION,
        annotations=dict(AI_RUN_OUTPUT_ANNOTATIONS),
    )
    @renders_as("run_output", title="run output")
    def ai_run_output(
        run_id: str,
        tail_bytes: int = 4096,
        wait_seconds: float = 0.0,  # retained for API compat; ignored
        raw_output: bool = False,
    ) -> Any:
        """Read the tail of a run's log.

        Completed runs return the full-result shape (unchanged).
        RUNNING runs (#484, 2026-07-19) return a progress snapshot —
        {status: 'running', elapsed_seconds, started_at, run_id,
        output_tail (last ~30 lines / 8KB, labeled "tail of M bytes
        total"), output_bytes_so_far} — instead of the pre-#484 bare
        refusal. The 📣 completion notify still arrives; the progress
        read is a signal, not a license to poll in a tight loop.

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
            # #484 (Emperor 2026-07-19): a read against a RUNNING job
            # returns real progress — status + elapsed from the recorded
            # spawn start + a bounded, honestly-labeled live tail —
            # instead of the old bare "wait for 📣 notify" refusal that
            # forced blind poll cycles on long suites. Cross-owner
            # callers still get the old refusal shape: a live tail is
            # command output and must not leak across sessions (mirrors
            # the #50 L4 gate on completed reads; attribution comes from
            # the live registry because the .status sidecar does not
            # exist until completion).
            from .code_runner_detached import (
                live_run_owner,
                run_owner_mismatch as _rom_rp,
                running_progress,
            )

            owner_mismatch = ""
            try:
                spawn_attr = live_run_owner(run_id)
                if spawn_attr.get("session_id") or spawn_attr.get("agent_context_id"):
                    from . import managed_mode_service as _mm_rp
                    from .mcp_server_runtime_helpers import (
                        current_calling_agent_context_id as _actor_rp,
                        current_calling_host_session_id as _ccsid_rp,
                    )

                    caller_session = _mm_rp.resolve_managed_session(
                        _mm_rp.ManagedModeService(),
                        root,
                        host_session_id=_ccsid_rp(),
                    )
                    owner_mismatch = _rom_rp(
                        spawn_attr,
                        session_id=caller_session,
                        agent_context_id=_actor_rp(root),
                    )
            except Exception:
                # Ownership gate must never crash the progress path;
                # resolution failures fall through like the completed-read
                # gate does.
                owner_mismatch = ""
            if owner_mismatch:
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

            progress = running_progress(root, run_id)
            # Secret guard applies to live tails on BOTH return paths —
            # raw_output skips the renderer, never the scan.
            try:
                from .run_output_guard import guard_run_output as _grop_rp

                progress = _grop_rp(
                    progress,
                    hub=hub,
                    project_root=root,
                    session_id=_caller_session(root),
                    run_id=str(run_id),
                )
            except Exception:
                pass
            if raw_output:
                return progress
            return _dispatch_run_output(progress, {})

        # #50 L4 (canonical 2026-04-26): cross-session refusal.
        # The .status sidecar carries session_id at completion time
        # (#50 L3 stamped it). If the calling conductor's session
        # doesn't match the spawn session, refuse — runs spawned by
        # one conductor must not be readable by another. Empty
        # spawn-session (legacy sidecar or unattributed run) falls
        # through to back-compat unscoped read.
        try:
            from . import managed_mode_service as _mm_l4
            from .code_runner_detached import (
                _read_status_sidecar,
                run_owner_mismatch,
            )
            from .mcp_server_runtime_helpers import (
                current_calling_agent_context_id as _actor_id_l4,
                current_calling_host_session_id as _ccsid_l4,
            )

            sidecar = _read_status_sidecar(root, run_id)
            spawn_session = str(sidecar.get("session_id") or "").strip() if sidecar else ""
            spawn_actor = str(sidecar.get("agent_context_id") or "").strip() if sidecar else ""
            if spawn_session or spawn_actor:
                host_sid = _ccsid_l4()
                caller_actor = _actor_id_l4(root)
                caller_session = _mm_l4.resolve_managed_session(
                    _mm_l4.ManagedModeService(),
                    root,
                    host_session_id=host_sid,
                )
                owner_mismatch = run_owner_mismatch(
                    sidecar,
                    session_id=caller_session,
                    agent_context_id=caller_actor,
                )
                if owner_mismatch:
                    refusal = {
                        "ok": False,
                        "run_id": run_id,
                        "blocked_by": owner_mismatch,
                        "reason": (
                            f"run_id={run_id} owner mismatch ({owner_mismatch}); "
                            f"spawn session='{spawn_session}', current='{caller_session}'; "
                            f"spawn actor='{spawn_actor}', current='{caller_actor}'. "
                            "Output read refused."
                        ),
                        "spawn_session_id": spawn_session,
                        "caller_session_id": caller_session,
                        "spawn_agent_context_id": spawn_actor,
                        "caller_agent_context_id": caller_actor,
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

            _rn_dismiss.dismiss_run(
                root,
                run_id=run_id,
                session_id=_caller_session(root),
                agent_context_id=_caller_actor(root),
                lane_id=_caller_lane(),
            )
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

    @_run_tool(
        description=AI_RUN_KILL_DESCRIPTION,
        annotations=dict(AI_RUN_KILL_ANNOTATIONS),
    )
    @renders_as("status", title="run kill")
    def ai_run_kill(run_id: str) -> Any:
        """Stop a running subprocess. No-op if already finished.

        Gates: managed-mode session ownership (cross-session run_ids
        refused like ai_run_output), and a refusal audit event so
        silently-blocked kills appear in execution_events. ai_run side
        has its own gates; this tool used to be completely ungated.

        A FREEZE DOES NOT BLOCK THIS TOOL. ai_run_kill is
        operation_class DESTRUCTIVE_CLEANUP (Q2 doctrine 2026-05-04):
        killing a runaway process is precisely what an operator needs
        under emergency lockdown, so the freeze gate must not hold it.
        This docstring claimed the opposite ("existing-freeze refusal —
        a frozen session must not kill a peer run") for ~3 months after
        the doctrine inverted. Corrected 2026-07-28 (#564): a lying
        docstring is a live trap, because a DOC-MATCH test greps exactly
        this prose and can pass while the behaviour is the reverse.
        Measured, not assumed — see
        tests/security/test_code_run_kill_gates.py::
        test_ai_run_kill_is_not_blocked_by_an_active_freeze (drives the
        wrapper with a freeze active) and
        tests/security/test_code_run_kill_remedial.py (the cascade).
        """
        from .code_runner_detached import (
            _read_status_sidecar,
            kill_run,
            run_owner_mismatch,
        )
        from .managed_mode_service import ManagedModeService, resolve_managed_session

        root = resolve_project_root()

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
            caller_session = resolve_managed_session(
                ManagedModeService(),
                root,
                host_session_id=host_sid,
            )
        except Exception:
            caller_session = ""
        caller_actor = _caller_actor(root)

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
            spawn_actor = str(sidecar.get("agent_context_id") or "").strip() if sidecar else ""
            owner_mismatch = run_owner_mismatch(
                sidecar,
                session_id=caller_session,
                agent_context_id=caller_actor,
            )
        except Exception:
            spawn_session = ""
            spawn_actor = ""
            owner_mismatch = ""
        if owner_mismatch:
            return _refuse(
                owner_mismatch,
                (
                    f"run_id={run_id} owner mismatch ({owner_mismatch}); "
                    f"spawn session='{spawn_session}', current='{caller_session}'; "
                    f"spawn actor='{spawn_actor}', current='{caller_actor}'. Kill refused."
                ),
                extra={
                    "spawn_session_id": spawn_session,
                    "caller_session_id": caller_session,
                    "spawn_agent_context_id": spawn_actor,
                    "caller_agent_context_id": caller_actor,
                },
            )

        return kill_run(root, run_id)

    # #768 / C.20: Tool Interface delegates to these exact closures.
    # FastMCP registration remains the local surface; metadata comes from
    # run_tool_contracts on both projections.
    from . import tool_interface as _ti_c20_run

    _ti_c20_run.register_impl("ai_run", ai_run)
    _ti_c20_run.register_impl("ai_run_output", ai_run_output)
    _ti_c20_run.register_impl("ai_run_kill", ai_run_kill)
