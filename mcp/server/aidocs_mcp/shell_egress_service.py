"""ShellEgressService — single chokepoint for shell execution.

Doctrine 2026-05-29 (king re-seal — shell egress hardening):
every code path that wants to execute a command MUST route through
this service. The bash_policy + heuristic_judge + destructive_floor
+ lifecycle preflight + timeout/tree-kill + audit + output_guard
semantics live here, in one place, so no surface (canonical ai_run,
outer-gate ai_run, legacy code_runner ai_run / code_build /
code_test, host integrations) can drift from the others.

CURRENT STATE — staged migration
--------------------------------
This module is the SHELL of the destination architecture. It
publishes the public API (execute, kill, posture validation) and
the doctrine constants, but the per-callsite migration of the 27
files in mcp/server/aidocs_mcp/ that currently use
subprocess.run / Popen / os.system directly is the next pass.

The doctrine tests in
`mcp/tests/security/test_shell_egress_doctrine.py` pin:
  - the ALLOWLIST of callsites permitted to remain on direct
    subprocess until the migration completes,
  - the semgrep rule (core/semgrep/aidocs-laws.yml) that refuses
    NEW direct subprocess.* / os.system outside the allowlist,
  - the network_posture vocabulary and refusal of `no_network`
    until a real sandbox is wired.

So this commit lands the chokepoint + the rule + the inventory +
the tests, then each subsequent migration commit shrinks the
allowlist by one or more files until the inventory is "this module
+ tests/ + scripts/" only.

Network posture
---------------
The `network_posture` field carries the caller's intent:

  - "default" → ambient runner network policy (no extra isolation)
  - "loopback_only" → caller asserts the command should only reach
    127.0.0.0/8; NOT enforced today (refused with NotImplementedError)
  - "no_network" → caller asserts the command should reach NO network;
    NOT enforced today (refused with NotImplementedError)

The refusal is deliberate. Accepting these without a real sandbox
(network namespaces, seccomp, Linux unshare, or a wrapping container
runtime) would let a caller believe network was disabled when it
wasn't. The PR-quarantine design doc cites this explicitly: GitHub-
native runner isolation is what the quarantine workflow uses; the
ShellEgressService refuses to fake network isolation it cannot
deliver.

The future migration to a real sandbox will swap the
`NotImplementedError` for the actual enforcement; the call sites
will already be passing the right posture value, so the wire
doesn't change.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Tear down a process AND its descendants.

    `subprocess.run(timeout=…)` on Windows kills only the DIRECT child and
    then blocks in `communicate()` until grandchildren close the inherited
    pipes — so a 2s-timeout command that spawned a 30s sleeper hangs the
    harness for the full 30s. Launching in a dedicated process group lets us
    signal the whole tree, then drain pipes with a short bounded wait.
    """
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # taskkill /T tree-kills children, /F forces. Quiet on a
            # missing pid — we only want the side effect.
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except ProcessLookupError:
                pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_capture_tree_kill(
    args: "list[str] | str",
    *,
    cwd: str | None,
    env: "dict[str, str] | None",
    timeout: float,
    shell: bool,
) -> "tuple[int, str, str, bool]":
    """Run a command capturing stdout/stderr with a GUARANTEED
    return-within-timeout, tree-killing the whole process group on timeout.

    Returns (returncode, stdout, stderr, timed_out). On timeout the tree is
    killed, pipes drained with a 3s bound, and timed_out=True.
    """
    popen_kwargs: dict[str, Any] = dict(
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=shell,
    )
    # Own process group so the whole tree is signalable on timeout.
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(args, **popen_kwargs)  # noqa: S603
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return -1, stdout or "", stderr or "", True


# Callable shape for the lifecycle preflight authority. The hub-
# construction site captures (hub, project_root, tool_name) and
# hands back a closure of this shape — the service holds no hub
# reference, so the import surface stays clean.
#
# Return contract:
#   - None  → preflight allows the call (active task present, or
#             exempt tool, or unmanaged project per the
#             require_active_task helper's own semantics).
#   - dict  → preflight REFUSES; the dict's reason/error fields
#             surface to the caller via refused_reason=
#             "lifecycle_no_active_task".
LifecyclePreflightFn = Callable[[str, str], "dict[str, Any] | None"]


def _consult_judge(
    command: str,
    cwd: str,
    reachability: "CommandReachability",
) -> "ShellEgressResult | None":
    """Heuristic-judge consultation with reachability-aware
    fail-closed semantics.

    Doctrine 2026-05-29 (king re-seal — judge fail-closed):

      - agent_reachable: judge MUST be available AND return a
        recognized verdict. If the module won't import, the call
        raises, or the verdict shape is unrecognized, we REFUSE
        with refused_reason='judge_unavailable' or
        'judge_unrecognized_verdict'. The destructive_floor is the
        unbypassable layer-one seal but the judge is layer two,
        and silently degrading layer two for agent-reachable
        commands lets a finer-grained malicious pattern slip past
        the floor's regex set.
      - operator_local / test_only: the operator authored the call
        themselves. If the judge isn't available we proceed with
        floor-only protection (the previous behavior). Explicit
        forbidden/refused verdicts still fail closed.

    Returns a refusal ShellEgressResult to short-circuit the
    caller, or None to proceed.
    """
    is_strict = reachability == "agent_reachable"
    try:
        from .heuristic_judge import evaluate_tool_call as _judge

        verdict = _judge(tool_name="ai_run", tool_input={"command": command}, project_root=cwd)
    except Exception as exc:
        if is_strict:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"heuristic_judge unavailable: {exc!r}",
                duration_s=0.0,
                refused_reason="judge_unavailable",
            )
        return None
    # Path A — JudgeResult-shaped: consult the EXPLICIT taxonomy
    # decision (operator_destructive_intent=False since shell egress
    # is the path an agent uses; the operator's "do it anyway"
    # signal travels via the confirm_token / break-glass surface,
    # NOT here). This is the canonical surface — max_risk is
    # telemetry, not a decision input.
    if hasattr(verdict, "decide"):
        try:
            decision_obj = verdict.decide(operator_destructive_intent=False)
        except Exception as exc:
            if is_strict:
                return ShellEgressResult(
                    ok=False,
                    rc=None,
                    stdout="",
                    stderr=f"heuristic_judge.decide raised: {exc!r}",
                    duration_s=0.0,
                    refused_reason="judge_unavailable",
                )
            return None
        decision = str(getattr(decision_obj, "decision", "")).lower()
        if decision == "allow":
            return None
        # ask_confirm / block_freeze_no_confirm / block_strike all
        # refuse — shell egress doesn't carry the confirm surface
        # for this path, so anything short of an explicit allow is
        # a hard refuse.
        rule_id = getattr(decision_obj, "triggering_rule_id", "") or "(no rule id)"
        reason = getattr(decision_obj, "reason", "") or decision
        return ShellEgressResult(
            ok=False,
            rc=None,
            stdout="",
            stderr=f"heuristic judge: decision={decision} rule_id={rule_id} reason={reason}",
            duration_s=0.0,
            refused_reason="heuristic_judge",
        )

    # Path B — dict-shaped class/verdict/classification (older /
    # mock callers).
    def _field(key: str) -> Any:
        if isinstance(verdict, dict):
            return verdict.get(key)
        return getattr(verdict, key, None)

    cls = str(_field("class") or _field("verdict") or _field("classification") or "").lower()
    if cls in ("forbidden", "refused", "blocked"):
        return ShellEgressResult(
            ok=False,
            rc=None,
            stdout="",
            stderr=f"heuristic judge: {cls}",
            duration_s=0.0,
            refused_reason="heuristic_judge",
        )
    # Path C — explicit allow field.
    if any(_field(k) for k in ("allowed", "allow", "ok", "proceed")):
        return None

    # No recognized shape → for agent_reachable, refuse. The judge
    # MUST return a verdict we know how to interpret.
    if is_strict:
        return ShellEgressResult(
            ok=False,
            rc=None,
            stdout="",
            stderr=f"heuristic_judge returned unrecognized shape: {type(verdict).__name__}",
            duration_s=0.0,
            refused_reason="judge_unrecognized_verdict",
        )
    return None


def _consult_lifecycle_preflight(
    cwd: str,
    reachability: "CommandReachability",
    audit_tag: str,
    preflight: "LifecyclePreflightFn | None",
) -> "ShellEgressResult | None":
    """Lifecycle preflight consultation, reachability-aware.

    Doctrine 2026-05-29 (king re-seal) + 2026-05-29 lifecycle-
    injection lift:

      - operator_local / test_only: skip silently. Operator-driven
        calls are the break-glass path; the lifecycle gate is
        deliberately not consulted.
      - agent_reachable, preflight handle NOT bound: refuse with
        refused_reason='lifecycle_preflight_unwired'. Honest about
        the gap — we do not silently allow commands that the
        doctrine says require an active lifecycle context.
      - agent_reachable, preflight bound: invoke
        preflight(cwd, audit_tag). None → allow. dict → refuse
        with refused_reason='lifecycle_no_active_task' and the
        dict's reason/error/detail field surfaced in stderr.

    The handle is held by the service instance; the hub-
    construction site is the canonical binder.
    """
    if reachability != "agent_reachable":
        return None
    if preflight is None:
        return ShellEgressResult(
            ok=False,
            rc=None,
            stdout="",
            stderr=(
                "lifecycle preflight unwired for agent_reachable shell egress. "
                "Use reachability='operator_local' for explicit operator "
                "break-glass; agent paths require ShellEgressService(lifecycle_"
                "preflight=...) to be bound — see mcp_server bootstrap."
            ),
            duration_s=0.0,
            refused_reason="lifecycle_preflight_unwired",
        )
    try:
        verdict = preflight(cwd or "", audit_tag)
    except Exception as exc:
        # Handle raised → validator failure; refuse with the
        # error-shape marker so the audit row distinguishes
        # "validator broken" from "no active task".
        return ShellEgressResult(
            ok=False,
            rc=None,
            stdout="",
            stderr=f"lifecycle preflight raised: {exc!r}",
            duration_s=0.0,
            refused_reason="lifecycle_preflight_error",
        )
    if verdict is None:
        return None
    # Refusal — distinguish validator failure (`error` field) from
    # no-active-task (`reason` field). The strict shell-egress
    # preflight returns either shape; the older require_active_task
    # path used `reason` only.
    if isinstance(verdict, dict) and verdict.get("error"):
        detail = verdict.get("detail") or verdict.get("error")
        return ShellEgressResult(
            ok=False,
            rc=None,
            stdout="",
            stderr=f"lifecycle validator failed: {detail}",
            duration_s=0.0,
            refused_reason="lifecycle_preflight_error",
        )
    reason_msg = (
        verdict.get("reason") or verdict.get("detail") or "no active task"
        if isinstance(verdict, dict)
        else "no active task"
    )
    return ShellEgressResult(
        ok=False,
        rc=None,
        stdout="",
        stderr=f"lifecycle preflight refused: {reason_msg}",
        duration_s=0.0,
        refused_reason="lifecycle_no_active_task",
    )


_OUTPUT_GUARD_WITHHELD_NOTICE = "[OUTPUT WITHHELD: output_guard finding, redaction unavailable]"
_OUTPUT_GUARD_SCAN_ERROR_NOTICE = "[OUTPUT WITHHELD: output_guard scan_error, fail-closed]"


def _post_exec_handler(
    *,
    rc: int,
    stdout: str,
    stderr: str,
    duration_s: float,
    request_audit_tag: str,
    request_cwd: str,
    request_argv_head: str,
    request_network_posture: str,
    request_reachability: str,
    session_id: Any | None = None,
) -> "ShellEgressResult":
    """Single post-execution handler shared by execute() and
    execute_shell(). Applies the output_guard fail-closed scan and
    writes the audit row. Factored out so the two egress surfaces
    cannot drift on either contract — every byte that reaches a
    caller passes through these steps.

    Output-guard semantics (identical across both surfaces):
      - clean output → pass through;
      - finding WITH a redacted variant → swap in the redacted text;
      - finding WITHOUT a redacted variant → withhold + marker;
      - scan_error (exception during scan) → withhold + marker.

    The final ok bool is False if rc != 0 OR the output_guard
    withheld anything. No raw output can reach a caller without
    going through this gate.
    """
    guarded_stdout = stdout
    guarded_stderr = stderr
    output_guard_marker = ""
    try:
        from .output_guard import scan_text

        for name, text in (("stdout", guarded_stdout), ("stderr", guarded_stderr)):
            if not text:
                continue
            g = scan_text(text, redact=True)
            if not getattr(g, "clean", True):
                redacted = getattr(g, "redacted_text", None)
                if redacted is not None:
                    if name == "stdout":
                        guarded_stdout = redacted
                    else:
                        guarded_stderr = redacted
                else:
                    if name == "stdout":
                        guarded_stdout = _OUTPUT_GUARD_WITHHELD_NOTICE
                    else:
                        guarded_stderr = _OUTPUT_GUARD_WITHHELD_NOTICE
                    output_guard_marker = "failed_closed"
    except Exception:
        # Scan error → cannot certify safety; withhold.
        guarded_stdout = _OUTPUT_GUARD_SCAN_ERROR_NOTICE
        guarded_stderr = _OUTPUT_GUARD_SCAN_ERROR_NOTICE
        output_guard_marker = "scan_error_failed_closed"

    # Audit row (best-effort).
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            request_cwd,
            event_kind="shell_egress_executed",
            source_kind="shell_egress_service",
            session_id=session_id,
            capability_name=request_audit_tag,
            action_kind="run",
            target_entity="",
            status="ok" if rc == 0 else "nonzero_exit",
            payload={
                "argv_head": request_argv_head,
                "rc": rc,
                "duration_s": round(duration_s, 3),
                "output_guard_marker": output_guard_marker,
                "network_posture": request_network_posture,
                "reachability": request_reachability,
            },
        )
    except Exception:
        pass

    return ShellEgressResult(
        ok=(rc == 0 and not output_guard_marker),
        rc=rc,
        stdout=guarded_stdout,
        stderr=guarded_stderr,
        duration_s=duration_s,
        refused_reason=output_guard_marker,
    )


def shlex_quote_each(argv: tuple[str, ...]) -> list[str]:
    """Per-token shell quoting for safe shell-string projection. Used
    when the destructive floor / judge wants the original shell shape
    even though the request was argv-form."""
    return [shlex.quote(t) for t in argv]


# ── Posture vocabulary ─────────────────────────────────────────────

NetworkPosture = Literal["default", "loopback_only", "no_network"]
NETWORK_POSTURES: tuple[str, ...] = ("default", "loopback_only", "no_network")


CommandReachability = Literal["agent_reachable", "operator_local", "test_only"]
COMMAND_REACHABILITIES: tuple[str, ...] = (
    "agent_reachable",
    "operator_local",
    "test_only",
)


# ── Public dataclasses ─────────────────────────────────────────────


@dataclass(frozen=True)
class ShellEgressRequest:
    """Single self-contained record of a command + the policy posture
    under which the caller is asking it to run. Frozen so the audit
    log can record a hashable shape."""

    argv: tuple[str, ...]
    """argv (list-shape, NOT a shell string). Callers that hold a
    shell-composed command must pre-split via shlex.split() so the
    audit record reflects the actual exec invocation."""

    cwd: str | None = None
    """Working directory. Required for agent-reachable calls;
    operator-local cleanup paths may pass None to inherit."""

    env: dict[str, str] | None = None
    """Environment override. None inherits the parent env (NOT the
    same as `env={}` — empty dict means 'execute with no env')."""

    timeout_s: float | None = None
    """Hard wall-clock cap. None means inherit the service default
    (currently 30 minutes)."""

    network_posture: NetworkPosture = "default"
    """Caller's network intent. See module doctrine for the
    `loopback_only` / `no_network` refusal."""

    reachability: CommandReachability = "agent_reachable"
    """Who can reach this surface. agent_reachable is the strictest
    (full gate cascade); operator_local skips the heuristic judge
    because the operator authored the call themselves; test_only is
    for tests/fixtures."""

    audit_tag: str = "shell_egress"
    """Free-form tag attached to the audit record so a reader can
    grep for the originating subsystem."""

    confirm_token: str = ""
    """Two-phase confirm token, when the underlying policy demands
    one (e.g. destructive_floor commands)."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Arbitrary structured fields the policy chain may consult
    (e.g. lane_id, session_id, agent_name)."""


@dataclass(frozen=True)
class ShellEgressResult:
    """Outcome of an execute() call. Designed to be safely surfaced
    through the MCP tool layer — no internal paths, no operator
    identity beyond what the caller already supplied."""

    ok: bool
    rc: int | None
    stdout: str
    stderr: str
    duration_s: float
    refused_reason: str = ""
    """Empty when ok=True; otherwise names the gate that refused
    (bash_policy / heuristic_judge / destructive_floor / preflight /
    output_guard / sandbox_unavailable)."""

    audit_id: str = ""
    """Audit-log row id when one was written."""


# ── Public service ─────────────────────────────────────────────────


class ShellEgressService:
    """The single chokepoint. ALL shell execution in aidocs_mcp must
    route through here once the per-callsite migration completes.

    Today this class is the SHELL of the destination: it publishes
    the API and the policy-stack ordering, and it refuses any
    posture beyond `default` because there is no sandbox wired to
    back the other postures.
    """

    DEFAULT_TIMEOUT_S: float = 30 * 60

    def __init__(
        self,
        *,
        lifecycle_preflight: "LifecyclePreflightFn | None" = None,
    ) -> None:
        """Construct the service.

        ``lifecycle_preflight`` is the optional authority handle the
        cascade consults at step 5. When None, step 5 stays in its
        honest fail-closed-unwired posture for ``agent_reachable``
        calls; when bound, the handle is called as
        ``preflight(cwd, tool_name) -> dict | None`` (None = allow,
        dict = refuse with the dict's ``reason``/``error`` field
        surfaced to the caller).

        The hub-construction path in mcp_server.py is the canonical
        binder — it captures the live AidocsServiceHub and the
        ``require_active_task`` helper, producing a callable the
        service can hold without needing a hub reference of its own.
        """
        self._lifecycle_preflight = lifecycle_preflight

    # ── lifecycle-handle plumbing ───────────────────────────────────

    def bind_lifecycle_preflight(self, fn: "LifecyclePreflightFn | None") -> None:
        """Late-binding setter for the singleton case. The hub boot
        path constructs ``default_service()`` BEFORE it can produce
        the require_active_task closure (circular import surface);
        this setter lets the boot finalize the wire after the hub
        is constructed without rebuilding the service singleton."""
        self._lifecycle_preflight = fn

    def execute(self, request: ShellEgressRequest) -> ShellEgressResult:
        """Run a command through the full gate cascade.

        Current law (2026-05-29, king re-seal — single source of truth
        about which steps actually fire today):

          1. argv shape validation — WIRED. Refuses anything that
             isn't a non-empty tuple of strings.
          2. network_posture enforcement — WIRED. Refuses
             `loopback_only` / `no_network` with
             `refused_reason="sandbox_unavailable"` because there
             is no real OS-level sandbox to honor them; accepting
             them silently would be a confused-deputy lie. The
             vocabulary is published so callers can already pass
             the right value; the runtime swap to real enforcement
             is the future-sandbox work.
          3. bash_policy.evaluate_destructive_floor — WIRED, every
             reachability. The floor is the unbypassable layer-one
             seal; an injected `rm -rf /` or `curl … | sh` fails
             closed here regardless of agent_reachable /
             operator_local.
          4. heuristic_judge consultation (via `_consult_judge`) —
             WIRED via the explicit-taxonomy decision path
             (`JudgeResult.decide(operator_destructive_intent=
             False)`), NOT max_risk telemetry. Only
             `DECISION_ALLOW` proceeds; `ask_confirm`,
             `block_freeze_no_confirm`, and `block_strike` all
             refuse with refused_reason='heuristic_judge'. This
             closes the previous gap where a low/medium-risk rule
             tagged `malicious_forbidden` would slip past a
             max_risk-only check.
               • agent_reachable: judge unavailable, unrecognized
                 verdict shape, missing decide() and missing
                 class/allow field all REFUSE
                 (`refused_reason="judge_unavailable"` or
                 `"judge_unrecognized_verdict"`).
               • operator_local / test_only: degrade to floor-only
                 protection; the operator authored the call.
          5. Lifecycle preflight (via `_consult_lifecycle_preflight`)
             — WIRED when the `lifecycle_preflight` handle is bound
             on the service; HONEST-FAIL-CLOSED-UNWIRED when not.
             • agent_reachable + handle bound: the helper invokes
               `preflight(cwd, audit_tag)` (the
               `require_active_task`-shaped callable). None →
               allow; dict → refuse with
               `refused_reason="lifecycle_no_active_task"` and the
               handle's reason field surfaced in stderr.
             • agent_reachable + handle absent: refuse with
               `refused_reason="lifecycle_preflight_unwired"`. No
               silent allow, no false claim of enforcement.
             • operator_local / test_only: skip the gate entirely
               (operator-driven break-glass).
             The hub-construction site in mcp_server.py is the
             canonical binder; it captures the AidocsServiceHub
             and produces a closure of shape
             `(cwd, tool_name) -> dict | None` that the service
             holds without needing a hub reference of its own.
          6. subprocess.run with timeout — WIRED. argv-list, no
             shell. Tree-kill of grand-children is the future
             migration of code_runner_detached._kill_process_tree
             into this service; today subprocess.run's own timeout
             handling fires.
          7. output_guard scan via the shared `_post_exec_handler`
             — WIRED. Identical fail-closed semantics in
             `execute()` and `execute_shell()`: finding without
             redacted variant → withhold + marker; scan error →
             withhold + marker; clean → pass through. ok bool
             collapses to False whenever the guard withholds, so
             no raw stdout/stderr ever reaches a caller past a
             finding.
          8. Audit row via the shared `_post_exec_handler` —
             WIRED, best-effort. ExecutionIndexStore.record_event
             with argv head, rc, duration, output_guard marker,
             network_posture, reachability. Failure to record
             does NOT affect the gate verdict.

        Single source of truth: `_post_exec_handler` is shared
        with `execute_shell()` so steps 7-8 cannot drift between
        the two surfaces. The only remaining migration that
        changes behavior is the lifecycle-handle injection at
        step 5; everything else above is the actual current law.
        """
        # Step 1 — argv shape.
        if not isinstance(request.argv, tuple) or not request.argv:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr="argv must be a non-empty tuple of strings",
                duration_s=0.0,
                refused_reason="argv_shape",
            )
        for token in request.argv:
            if not isinstance(token, str):
                return ShellEgressResult(
                    ok=False,
                    rc=None,
                    stdout="",
                    stderr=f"argv token must be str: {token!r}",
                    duration_s=0.0,
                    refused_reason="argv_shape",
                )

        # Step 2 — network posture refusal.
        if request.network_posture not in NETWORK_POSTURES:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"unknown network_posture: {request.network_posture!r}",
                duration_s=0.0,
                refused_reason="unknown_posture",
            )
        if request.network_posture in ("loopback_only", "no_network"):
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=(
                    f"network_posture={request.network_posture!r} is not "
                    "implemented — no sandbox is wired to enforce it. "
                    "Accepting it would be a confused-deputy lie. See "
                    "shell_egress_service.py doctrine."
                ),
                duration_s=0.0,
                refused_reason="sandbox_unavailable",
            )

        # Step 3 — destructive-primitive floor (bash_policy).
        # Always runs; the floor is the unbypassable seal.
        # For argv-form requests we join into a shell-string proxy
        # so the regex-based floor sees the same input shape it
        # was authored against.
        floor_command = " ".join(shlex_quote_each(request.argv))
        try:
            from .bash_policy import evaluate_destructive_floor

            floor = evaluate_destructive_floor(floor_command)
        except Exception as exc:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"destructive_floor unavailable: {exc!r}",
                duration_s=0.0,
                refused_reason="destructive_floor_unavailable",
            )
        if not floor.get("allowed", True):
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"refused by destructive floor: {floor.get('reason')}",
                duration_s=0.0,
                refused_reason="destructive_floor",
            )

        # Step 4 — heuristic judge consultation (reachability-aware
        # fail-closed semantics; agent_reachable refuses on unknown
        # verdict or unavailability).
        _maybe_refused = _consult_judge(
            floor_command,
            request.cwd or "",
            request.reachability,
        )
        if _maybe_refused is not None:
            return _maybe_refused

        # Step 5 — lifecycle preflight. For agent_reachable, refuses
        # explicitly rather than claiming an enforcement that isn't
        # wired.
        _preflight_refused = _consult_lifecycle_preflight(
            request.cwd or "",
            request.reachability,
            request.audit_tag,
            self._lifecycle_preflight,
        )
        if _preflight_refused is not None:
            return _preflight_refused

        # Step 6 — execute with timeout + tree-kill. Popen in its own
        # process group so a timeout tears down the WHOLE tree and drains
        # within a bounded window (a bare subprocess.run timeout would block
        # until grandchildren close the inherited pipes).
        timeout = request.timeout_s or self.DEFAULT_TIMEOUT_S
        import time

        t0 = time.monotonic()
        try:
            rc, _so, _se, _timed_out = _run_capture_tree_kill(
                list(request.argv),
                cwd=request.cwd,
                env=request.env,
                timeout=timeout,
                shell=False,
            )
            if _timed_out:
                return ShellEgressResult(
                    ok=False,
                    rc=None,
                    stdout=_so,
                    stderr=f"timeout after {timeout:.0f}s (process tree killed)",
                    duration_s=time.monotonic() - t0,
                    refused_reason="timeout",
                )
            completed = subprocess.CompletedProcess(list(request.argv), rc, _so, _se)
        except OSError as exc:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"exec failed: {exc!r}",
                duration_s=time.monotonic() - t0,
                refused_reason="exec_error",
            )
        duration = time.monotonic() - t0
        # Steps 7-8 — shared post-exec handler: output_guard fail-
        # closed scan + audit. Identical to execute_shell()'s path.
        return _post_exec_handler(
            rc=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_s=duration,
            request_audit_tag=request.audit_tag,
            request_cwd=request.cwd or "",
            request_argv_head=request.argv[0] if request.argv else "",
            request_network_posture=request.network_posture,
            request_reachability=request.reachability,
            session_id=request.metadata.get("session_id"),
        )

    def execute_shell(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: float | None = None,
        reachability: CommandReachability = "agent_reachable",
        network_posture: NetworkPosture = "default",
        audit_tag: str = "shell_egress_shell",
        metadata: dict[str, Any] | None = None,
    ) -> ShellEgressResult:
        """Shell-string variant for legacy callers that compose pipes,
        redirects, env-substitution, etc. and can't easily switch to
        argv form. Runs the same gate cascade: destructive floor +
        heuristic judge + output guard + audit. The execution itself
        uses subprocess.run(shell=True) — the destructive floor is
        the unbypassable seal that closes the injection surface."""
        # Cascade — same shape as execute(), but the command is the
        # shell string directly, not joined from argv.
        if not isinstance(command, str) or not command:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr="command must be a non-empty string",
                duration_s=0.0,
                refused_reason="argv_shape",
            )
        if network_posture not in NETWORK_POSTURES:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"unknown network_posture: {network_posture!r}",
                duration_s=0.0,
                refused_reason="unknown_posture",
            )
        if network_posture in ("loopback_only", "no_network"):
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=(
                    f"network_posture={network_posture!r} is not implemented — no "
                    "sandbox is wired to enforce it."
                ),
                duration_s=0.0,
                refused_reason="sandbox_unavailable",
            )
        try:
            from .bash_policy import evaluate_destructive_floor

            floor = evaluate_destructive_floor(command)
        except Exception as exc:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"destructive_floor unavailable: {exc!r}",
                duration_s=0.0,
                refused_reason="destructive_floor_unavailable",
            )
        if not floor.get("allowed", True):
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"refused by destructive floor: {floor.get('reason')}",
                duration_s=0.0,
                refused_reason="destructive_floor",
            )
        _refused = _consult_judge(command, cwd, reachability)
        if _refused is not None:
            return _refused
        _preflight_refused = _consult_lifecycle_preflight(
            cwd,
            reachability,
            audit_tag,
            self._lifecycle_preflight,
        )
        if _preflight_refused is not None:
            return _preflight_refused
        import time as _time

        t0 = _time.monotonic()
        timeout = timeout_s or self.DEFAULT_TIMEOUT_S
        try:
            # nosemgrep: aidocs-no-shell-true-in-subprocess  # shell=True is intentional in execute_shell(): it is the ONE chokepoint where shell-string semantics (pipes, redirects, env-substitution, glob expansion) survive after the destructive_floor + heuristic_judge cascade. argv-form callers MUST use execute() instead — which never sets shell=True. This waiver explicitly does NOT claim the destructive_floor "closes" all injection: the floor catches *destructive* shapes (rm -rf root, dd if=/dev/, fork-bombs, eval $(curl …)), and the judge catches credential-exfil / container-escape / hypervisor / inline-runtime-bypass shapes — but neither is a substitute for argv-form input sanitization. The contract that REMAINS THE CALLER'S responsibility is: do NOT concatenate untrusted (agent-derived / user-input) fragments into the command string passed here. If you must build a shell string at all, use shlex.quote() on every interpolated value, OR (preferred) switch the caller to argv-form and route through execute(). A doctrine test `test_no_untrusted_fragment_concat_into_execute_shell` enforces this against the callsite inventory.
            # Popen + process-group + tree-kill (2026-06): on timeout this
            # tears down the WHOLE tree and drains within a bounded window —
            # a subprocess.run(timeout=) here would block until grandchildren
            # close the inherited pipes (a 2s timeout could hang 30s).
            rc, _so, _se, _timed_out = _run_capture_tree_kill(
                command, cwd=cwd, env=None, timeout=timeout, shell=True
            )
            if _timed_out:
                return ShellEgressResult(
                    ok=False,
                    rc=None,
                    stdout=_so,
                    stderr=f"timeout after {timeout:.0f}s (process tree killed)",
                    duration_s=_time.monotonic() - t0,
                    refused_reason="timeout",
                )
            completed = subprocess.CompletedProcess(command, rc, _so, _se)
        except OSError as exc:
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=f"exec failed: {exc!r}",
                duration_s=_time.monotonic() - t0,
                refused_reason="exec_error",
            )
        duration = _time.monotonic() - t0
        # Shared post-exec handler — identical output_guard fail-
        # closed scan + audit as execute(). This is the seal that
        # prevents code_runner.ai_run (which delegates here) from
        # leaking raw output past the chokepoint.
        return _post_exec_handler(
            rc=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_s=duration,
            request_audit_tag=audit_tag,
            request_cwd=cwd,
            request_argv_head=command.split()[0] if command.split() else "",
            request_network_posture=network_posture,
            request_reachability=reachability,
            session_id=(metadata or {}).get("session_id"),
        )


# Convenience singleton for callers that don't need lifecycle injection.
_default_service: ShellEgressService | None = None


def default_service() -> ShellEgressService:
    global _default_service
    if _default_service is None:
        _default_service = ShellEgressService()
    return _default_service


# ── Migration inventory ────────────────────────────────────────────


# Files in mcp/server/aidocs_mcp/ that legitimately use
# subprocess.run / Popen / call / check_output / check_call / os.system
# directly TODAY. Every entry in this list is a future migration
# target. Adding a new file to this list requires landing a doctrine
# justification in the commit message AND a doctrine test row.
#
# Adding a row here is friction by design: a future contributor who
# wants to add a new subprocess call has to either (a) route through
# ShellEgressService (the desired path), or (b) document why the
# new call can't and add the row + the doctrine justification.
#
# Reachability classifications:
#   AR  = agent_reachable — full gate cascade required; THESE are
#         the most important to migrate first.
#   OL  = operator_local — operator runs these on their own dev
#         box; gate cascade nice-to-have but not strictly required.
#         Migration is still queued so the audit-log captures them.
#   TS  = test_only — fixtures + test harnesses; lowest priority.
#
# Per-callsite SEMANTIC fingerprint (king re-seal 2026-05-29 — upgrade
# from per-file count). Each row pins ONE call site by:
#   - relpath:                  file under mcp/server/aidocs_mcp/
#   - enclosing_fn:             function or method name that owns the call
#                               (or "<module>" for top-level)
#   - callee_kind:              one of {"subprocess.run", "subprocess.Popen",
#                               "subprocess.call", "subprocess.check_output",
#                               "subprocess.check_call", "os.system"}
#   - shell_flag:               "shell=True" / "shell=False" / "n/a"
#                               (os.system is always shell — recorded as "n/a")
#   - argv_head:                a stable literal substring of the first argv
#                               element or shell-string head (e.g. "git",
#                               "/home/app/aidocs-gate/.venv/bin/python3").
#                               Empty string ("") means "command is a
#                               runtime-built argv — head shape is enforced
#                               at the caller, not here".
#   - reachability:             AR | OL | TS (see classifications below)
#   - owner:                    team label for triage
#   - rationale:                why this call still exists; if it carries
#                               a follow-up commit, name it.
#
# A dangerous mutation of an EXISTING call cannot hide behind the same
# count anymore: changing shell=False → shell=True, swapping `subprocess.
# run` for `os.system`, or changing the argv_head to a different binary
# all break the fingerprint match. The doctrine test
# `test_legacy_subprocess_callsites_match_fingerprints` parses each file
# with `ast` and refuses if any callsite drifts from its recorded
# fingerprint, OR if a callsite appears in the file that has no row here.
#
# To migrate a row OUT: route the call through ShellEgressService.execute()
# (argv) or execute_shell() (legacy shell-string) and delete the row.
# To accept a NEW row: land it in the same commit as the new call, with
# a stated rationale and owner.
LEGACY_SUBPROCESS_FINGERPRINTS: tuple[tuple[str, str, str, str, str, str, str, str], ...] = (
    # (relpath, enclosing_fn, callee_kind, shell_flag, argv_head,
    #  reachability, owner, rationale)
    # AR — agent-reachable, migrate first
    (
        "agent_expert_service.py",
        "spawn_interactive",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "expert-fanout",
        "expert subprocess fanout",
    ),
    (
        "agent_expert_service.py",
        "spawn_worker_claude",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "expert-fanout",
        "spawn_worker_claude subprocess",
    ),
    (
        "agent_expert_service.py",
        "spawn_worker_codex",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "expert-fanout",
        "spawn_worker_codex subprocess",
    ),
    (
        "agent_expert_service.py",
        "spawn_worker_opencode",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "expert-fanout",
        "spawn_worker_opencode subprocess",
    ),
    (
        "code_runner.py",
        "_kill_process_tree",
        "subprocess.run",
        "shell=False",
        "taskkill",
        "AR",
        "code-runner",
        "Windows taskkill helper; argv-only",
    ),
    (
        "code_runner.py",
        "_run_process",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "code-runner",
        "primary code_runner subprocess; legacy shim while callers migrate",
    ),
    (
        "code_runner_detached.py",
        "spawn_detached",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "detached-runner",
        "detached run dispatcher; needs lifecycle binding in tests",
    ),
    (
        "conductor_verification_service.py",
        "_run_command",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "conductor-verification",
        "needs lifecycle binding in tests; prior migration attempt reverted",
    ),
    (
        "governed_bash_service.py",
        "_default_probe",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "governed-bash",
        "default probe; migration tracked",
    ),
    (
        "governed_bash_service.py",
        "_verify_os_signature",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "governed-bash",
        "PowerShell os-signature verification via the ABSOLUTE canonical system "
        "PowerShell (argv[0] is the _canonical_powershell() variable, never a "
        "PATH-resolved 'powershell' literal — the MCP server env may lack it on "
        "PATH); provider path via env var + -LiteralPath; operator-local",
    ),
    (
        "governed_shell_attest.py",
        "_publisher_ok",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "governed-shell",
        "bounded fixed-argv Authenticode publisher probe invoked by ABSOLUTE "
        "canonical system PowerShell (argv[0] is the _canonical_powershell() "
        "variable, never a PATH-resolved literal); attestation evidence the "
        "output-guard would withhold; operator-local, no untrusted input",
    ),
    (
        "lane_resume_dispatcher.py",
        "resume_worker_on_deny",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "lane-dispatch",
        "lane resume dispatcher; needs lifecycle binding in tests",
    ),
    (
        "mcp_server.py",
        "conductor_start",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "mcp-server",
        "conductor_start Popen #1",
    ),
    (
        "mcp_server.py",
        "conductor_start",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "mcp-server",
        "conductor_start Popen #2",
    ),
    (
        "mcp_server.py",
        "conductor_start",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "mcp-server",
        "conductor_start Popen #3",
    ),
    # OL — operator-local
    (
        "aidocs_nlp/installer.py",
        "_run",
        "subprocess.Popen",
        "shell=False",
        "",
        "OL",
        "nlp-bootstrap",
        "_run during NLP bootstrap",
    ),
    (
        "aidocs_nlp/installer.py",
        "uninstall",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "nlp-bootstrap",
        "uninstall during NLP teardown",
    ),
    (
        "checkpoint_service.py",
        "_git_cat_file_bytes",
        "subprocess.run",
        "shell=False",
        "git",
        "OL",
        "checkpoint",
        "git cat-file for checkpoint inspection",
    ),
    (
        "cli.py",
        "_run_install",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "cli",
        "installer invocation",
    ),
    ("cli.py", "cmd_doctor", "subprocess.run", "shell=False", "", "OL", "cli", "doctor probe"),
    (
        "cli.py",
        "cmd_setup",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "cli",
        "setup post-install probe",
    ),
    (
        "csharp_roslyn_client.py",
        "_run",
        "subprocess.run",
        "shell=False",
        "dotnet",
        "OL",
        "roslyn-client",
        "dotnet helper invocation",
    ),
    (
        "csharp_roslyn_client.py",
        "_start_unlocked",
        "subprocess.Popen",
        "shell=False",
        "dotnet",
        "OL",
        "roslyn-client",
        "Roslyn worker spawn",
    ),
    (
        "failure_stewardship.py",
        "capture_first_seen_tree_hash",
        "subprocess.run",
        "shell=False",
        "git",
        "OL",
        "failure-stewardship",
        "git inspection for failure capture #1",
    ),
    (
        "failure_stewardship.py",
        "capture_first_seen_tree_hash",
        "subprocess.run",
        "shell=False",
        "git",
        "OL",
        "failure-stewardship",
        "git inspection for failure capture #2",
    ),
    (
        "failure_stewardship.py",
        "capture_head_sha",
        "subprocess.run",
        "shell=False",
        "git",
        "OL",
        "failure-stewardship",
        "git head sha capture",
    ),
    (
        "file_ops.py",
        "_check_syntax",
        "subprocess.run",
        "shell=False",
        "node",
        "OL",
        "file-ops",
        "node-based syntax check",
    ),
    (
        "git_helpers.py",
        "run_git_sync",
        "subprocess.run",
        "shell=False",
        "git",
        "OL",
        "git-helpers",
        "general git porcelain helper",
    ),
    (
        "outer_gate_projects.py",
        "_git",
        "subprocess.run",
        "shell=False",
        "git",
        "OL",
        "outer-gate-projects",
        "git query for project discovery",
    ),
    (
        "package_integrity.py",
        "_default_subprocess_runner",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "package-integrity",
        "package integrity verifier default runner",
    ),
    (
        "runtime_bootstrap_service.py",
        "project_init",
        "subprocess.run",
        "shell=False",
        "gh",
        "OL",
        "runtime-bootstrap",
        "gh CLI invocation during project init",
    ),
    (
        "runtime_provisioner.py",
        "_default_runner",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "runtime-provisioner",
        "runtime provisioner default runner",
    ),
    (
        "runtime_service.py",
        "recent_commits_touching_file",
        "subprocess.run",
        "shell=False",
        "git",
        "OL",
        "runtime-service",
        "git log for recent commits touching file",
    ),
    (
        "server_legacy_git_tools.py",
        "_run_psql",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "legacy-git-tools",
        "psql for legacy git/schema tools",
    ),
    (
        "server_plan_task_tools.py",
        "ai_models",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "plan-task-tools",
        "ai_models probe",
    ),
    (
        "shell_resolver.py",
        "_safe_run",
        "subprocess.Popen",
        "shell=False",
        "",
        "OL",
        "shell-resolver",
        "shell binary resolution probe",
    ),
    (
        "slop_backends.py",
        "_default_runner",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "slop-backends",
        "slop backend default runner",
    ),
    (
        "updater_service.py",
        "_run_script",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "updater-service",
        "updater script execution",
    ),
    (
        "workflow_action_service.py",
        "verify_action",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "workflow-action",
        "workflow action verification",
    ),
    # TS — test-shaped predicates
    (
        "conditional_predicates.py",
        "_git_clean",
        "subprocess.run",
        "shell=False",
        "git",
        "TS",
        "conditional-predicates",
        "_git_clean sentinel check #1",
    ),
    (
        "conditional_predicates.py",
        "_git_clean",
        "subprocess.run",
        "shell=False",
        "git",
        "TS",
        "conditional-predicates",
        "_git_clean sentinel check #2",
    ),
)


LEGACY_SUBPROCESS_CALLSITES: tuple[tuple[str, str, str], ...] = (
    # (relpath, classification, rationale)
    ("agent_expert_service.py", "AR", "expert subprocess fanout"),
    ("aidocs_nlp/installer.py", "OL", "pip install bootstrap"),
    ("checkpoint_service.py", "OL", "git checkpoint shelling"),
    ("cli.py", "OL", "operator-initiated CLI ops"),
    ("code_runner.py", "AR", "legacy ai_run path — PRIORITY"),
    ("code_runner_detached.py", "AR", "detached ai_run + tree-kill — PRIORITY"),
    ("conditional_predicates.py", "OL", "evaluator shellouts"),
    (
        "conductor_verification_service.py",
        "AR",
        "verification commands — needs lifecycle binding in tests before migration",
    ),
    ("csharp_roslyn_client.py", "OL", "Roslyn daemon ipc"),
    ("failure_stewardship.py", "OL", "stewardship reporting"),
    ("file_ops.py", "OL", "git-aware file ops"),
    ("git_helpers.py", "OL", "git porcelain wrappers"),
    ("governed_bash_service.py", "AR", "governed-bash legacy — PRIORITY"),
    ("governed_shell_attest.py", "OL", "Authenticode publisher attestation probe"),
    ("lane_resume_dispatcher.py", "OL", "lane dispatch shellouts"),
    ("mcp_server.py", "AR", "server-tier shellouts"),
    ("outer_gate_projects.py", "OL", "project bootstrap shellouts"),
    ("package_integrity.py", "OL", "signing/integrity checks"),
    ("runtime_bootstrap_service.py", "OL", "runtime bootstrap probes"),
    ("runtime_provisioner.py", "OL", "venv provisioning"),
    ("runtime_service.py", "OL", "runtime status probes"),
    ("server_legacy_git_tools.py", "OL", "legacy git tool surfaces"),
    ("server_plan_task_tools.py", "OL", "plan/task tool surfaces"),
    ("shell_resolver.py", "OL", "shell probe (--version / -c)"),
    ("slop_backends.py", "OL", "slop backend tooling"),
    ("updater_service.py", "OL", "self-update probes"),
    ("workflow_action_service.py", "OL", "workflow action shellouts"),
)
