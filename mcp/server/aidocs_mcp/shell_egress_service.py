"""ShellEgressService — single chokepoint for shell execution.

Doctrine 2026-05-29 (Empire re-seal — shell egress hardening):
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
            # #345: routed through audited_run (tree-kill actions belong in
            # the ledger too) + CREATE_NO_WINDOW (taskkill is a console-
            # subsystem binary; under the pythonw daemon it would allocate
            # a visible console without the flag).
            audited_run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                fingerprint=("shell_egress_service.py", "_kill_process_tree", "subprocess.run"),
                reason="governed-shell-tree-kill",
                run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
                capture_output=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


def terminate_process_tree(proc: "subprocess.Popen") -> None:
    """Canonical public process-tree termination primitive.

    Callers must launch the child in a dedicated process group/session before
    handing it here; this function owns the cross-platform termination policy.
    """
    _kill_process_tree(proc)


def _augment_path_with_tools(env: "dict | None") -> dict:
    """Guarantee essential CLI tools (git + its bundled unix tools) resolve in the
    governed-shell subprocess, regardless of how the daemon was launched.

    The governed shell is the ONE spawn chokepoint for ai_run + internal git ops.
    A daemon started from a Windows service / non-login shell can inherit a PATH
    that lacks the Git install dir, so every governed `git ...` fails with
    'command not found' (reported 2026-07-09: Deploy.ps1 couldn't run because git
    wasn't on the AIDOCS-gated shell's PATH). We prepend known tool dirs ONLY when
    git is not already resolvable in the given env's PATH — a no-op on a healthy
    PATH, additive (never removes) otherwise. Never raises.
    """
    import os
    import shutil

    try:
        e = dict(env) if env is not None else os.environ.copy()
        if os.name == "nt":
            # ANALYSED MUST EQUAL EXECUTED. Since #561 phase 1 the interpreter is a
            # resolved Git Bash, and MSYS rewrites POSIX-looking arguments on their
            # way into a native .exe. Measured through this very chokepoint:
            #   '/S'             -> 'S:/'          (a flag became a drive)
            #   '-v /c/foo:/bar' -> '-v C:\foo;C:\Program Files\Git\bar'
            #   '--flag=/opt/z'  -> '--flag=C:/Program Files/Git/opt/z'
            # cmd.exe did none of this, so the corruption ARRIVED with the
            # interpreter change. It is not cosmetic: the floor, the judge and the
            # allow/deny tables all reason about the literal command string, and the
            # audit records that string — so a rewrite means the vetted command and
            # the executed command are different commands. Both switches are needed;
            # MSYS_NO_PATHCONV alone leaves some conversions in place.
            e.setdefault("MSYS_NO_PATHCONV", "1")
            e.setdefault("MSYS2_ARG_CONV_EXCL", "*")
        path = e.get("PATH") or e.get("Path") or ""
        if shutil.which("git", path=path or None):
            return e  # already resolvable — nothing to do
        candidates: list[str] = []
        if os.name == "nt":
            for base in (r"C:\Program Files\Git", r"C:\Program Files (x86)\Git"):
                candidates += [base + r"\cmd", base + r"\mingw64\bin", base + r"\usr\bin"]
        else:
            candidates += ["/usr/local/bin", "/usr/bin", "/opt/homebrew/bin"]
        lower = path.lower()
        extra = [c for c in candidates if os.path.isdir(c) and c.lower() not in lower]
        if extra:
            e["PATH"] = os.pathsep.join(extra + ([path] if path else []))
        return e
    except Exception:
        return dict(env) if isinstance(env, dict) else {}


def _resolve_interpreter_argv(command: str, cwd: str | None) -> "tuple[list[str], str]":
    """Return (argv, interpreter_label) for a governed command.

    ([], "platform_default") when no shell could be resolved — the caller then
    keeps the legacy shell=True spawn rather than refusing, so a resolver fault
    can never brick every governed command (#561 phase 1; same
    do-not-lock-yourself-out sequencing the operator applied in #560).

    The argv shape comes from ResolvedShell.argv_template — `[bash, "-c",
    "{command}"]` with the trailing placeholder replaced by the literal command
    string. That is `-c` and NOT `-lc` by design: no login files, no user PATH
    mutation, no aliases leaking into controlled execution.
    """
    try:
        from pathlib import Path as _Path

        from .shell_resolver import resolve_shell as _resolve

        res = _resolve(_Path(cwd) if cwd else _Path.cwd())
        if getattr(res, "verdict", "") != "usable":
            return [], f"platform_default(unresolved:{getattr(res, 'verdict', '?')})"
        template = list(getattr(res, "argv_template", []) or [])
        if len(template) < 2 or template[-1] != "{command}":
            return [], "platform_default(bad_template)"
        return [*template[:-1], command], getattr(res, "path", "") or "resolved"
    except Exception:  # noqa: BLE001 — resolution must never break execution
        return [], "platform_default(resolver_error)"


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
        # #684 SOURCE FIX — pin the decode. `text=True` alone decodes with
        # locale.getpreferredencoding(False), i.e. the system ANSI codepage
        # (cp1252/cp1251/cp950 …). A child emitting UTF-8 — which git,
        # pytest and every modern tool do — came back MOJIBAKED through this
        # chokepoint, and agents then copied that echo into commit messages,
        # backlog bodies and memory captures. Same root cause, same remedy as
        # mempalace/_stdio.py for stdio.
        # errors='replace', never 'strict': some real subprocess output
        # genuinely is not UTF-8, and a UnicodeDecodeError here would take
        # ai_git/ai_run/ai_test down. U+FFFD is a VISIBLE marker that a byte
        # was undecodable — and it is itself a mojibake signature, so the
        # write guard still catches it downstream.
        encoding="utf-8",
        errors="replace",
        shell=shell,
    )
    # Own process group so the whole tree is signalable on timeout, and NO
    # CONSOLE. CREATE_NEW_PROCESS_GROUP alone does NOT suppress a console:
    # the daemon now runs under pythonw (GUI subsystem, no console of its
    # own), so a console-subsystem child spawned from it ALLOCATES A FRESH
    # CONSOLE — a window on the operator's screen. Inheriting the parent's
    # console used to hide this; the windowless-daemon fix removed the very
    # console that was hiding it. CREATE_NO_WINDOW is what actually closes it.
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_kwargs["start_new_session"] = True

    # Routed through audited_popen so the governed-shell chokepoint — which
    # ai_test and every synchronous governed command flow through — lands a
    # ledger row like every other spawn. It was the last unaudited spawn in
    # the hot path, which is exactly why "absent from the ledger" wrongly
    # read as "not ours" (#334/#345). The passthrough lambda preserves this
    # file's registered direct-Popen AST callsite for the fingerprint gate.
    proc = audited_popen(
        args,
        fingerprint=("shell_egress_service.py", "_run_capture_tree_kill", "subprocess.Popen"),
        reason="governed-shell-capture",
        popen=lambda *a, **kw: subprocess.Popen(*a, **kw),  # noqa: S603  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
        **popen_kwargs,
    )
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

    Doctrine 2026-05-29 (Empire re-seal — judge fail-closed):

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

    Doctrine 2026-05-29 (Empire re-seal) + 2026-05-29 lifecycle-
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

# ── Output-certification vocabulary (#608) ─────────────────────────────
#
# The guard's outcome is FOUR states, and `ok` could only carry two. A command
# that exited 0 whose echo could not be shown was reported as `ok=False` with a
# refused_reason — the language of a REFUSED ACTION, when nothing refused it.
# That cost a measured retry of an already-landed commit, and repeated apparent
# failures accrue freeze strikes. So the guard's verdict gets its own field and
# the action's outcome gets its own field; neither is inferred from the other.
#
# Mirrors the vocabulary shell_receipt.py already uses on the NATIVE hook path
# (`guard_status`: clean / redacted / degraded), which never conflated the two.
OUTPUT_CLEAN = "clean"
OUTPUT_REDACTED = "redacted"
OUTPUT_WITHHELD_FINDING = "withheld_finding"
OUTPUT_WITHHELD_SCAN_ERROR = "withheld_scan_error"
OUTPUT_NOT_EXECUTED = "not_executed"
# #665 — the SIXTH member, added because none of the five above could tell the
# truth about a TIMEOUT: the command ran, it produced bytes, and those bytes are
# truncated at an arbitrary offset by the tree-kill. `clean`/`redacted` claim a
# certification a truncated payload cannot carry; `withheld_finding` invents a
# finding; `withheld_scan_error` blames a scanner that was never asked; and
# `not_executed` — the silent default this branch used to return — denies that
# the command ran at all. The vocabulary is EXTENDED, never forked (§XXII).
OUTPUT_WITHHELD_TRUNCATED = "withheld_truncated"
OUTPUT_STATUSES: tuple[str, ...] = (
    OUTPUT_CLEAN,
    OUTPUT_REDACTED,
    OUTPUT_WITHHELD_FINDING,
    OUTPUT_WITHHELD_SCAN_ERROR,
    OUTPUT_WITHHELD_TRUNCATED,
    OUTPUT_NOT_EXECUTED,
)


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
    output_status = OUTPUT_CLEAN

    def _withheld(notice: str) -> str:
        # #371 (WAR U): a withheld output is a refusal surface — carry the
        # file-it-as-FP affordance (additive footer; the withhold still holds).
        try:
            from .tool_gate_service import false_positive_affordance

            return notice + "\n" + false_positive_affordance(
                "shell_egress.output_withheld", project_root=request_cwd
            )
        except Exception:
            return notice

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
                    if output_status == OUTPUT_CLEAN:
                        output_status = OUTPUT_REDACTED
                else:
                    if name == "stdout":
                        guarded_stdout = _withheld(_OUTPUT_GUARD_WITHHELD_NOTICE)
                    else:
                        guarded_stderr = _withheld(_OUTPUT_GUARD_WITHHELD_NOTICE)
                    output_guard_marker = "failed_closed"
                    output_status = OUTPUT_WITHHELD_FINDING
    except Exception:
        # Scan error → cannot certify safety; withhold.
        guarded_stdout = _withheld(_OUTPUT_GUARD_SCAN_ERROR_NOTICE)
        guarded_stderr = _withheld(_OUTPUT_GUARD_SCAN_ERROR_NOTICE)
        output_guard_marker = "scan_error_failed_closed"
        output_status = OUTPUT_WITHHELD_SCAN_ERROR

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
                "output_status": output_status,
                "network_posture": request_network_posture,
                "reachability": request_reachability,
            },
        )
    except Exception:
        pass

    return ShellEgressResult(
        # `ok` stays the CONSERVATIVE conjunction — an uncertifiable output is
        # still not a clean result, and every existing fail-closed seal reads
        # this field. What changes is that it is no longer the ONLY thing a
        # caller can read: `action_ok` states the command's own outcome and
        # `output_status` states the guard's, so #608's retry hazard has a
        # truthful field to consult instead of an invented failure.
        ok=(rc == 0 and not output_guard_marker),
        rc=rc,
        stdout=guarded_stdout,
        stderr=guarded_stderr,
        duration_s=duration_s,
        refused_reason=output_guard_marker,
        action_ok=(rc == 0),
        output_status=output_status,
    )


_TIMEOUT_WITHHELD_NOTICE = (
    "[OUTPUT WITHHELD: the command outlasted its timeout and its process tree "
    "was killed mid-write, so the captured output is TRUNCATED at an arbitrary "
    "offset. A truncated payload cannot be certified — a credential can straddle "
    "the cut — so it is withheld rather than scanned-and-passed. This is an "
    "INFRASTRUCTURE outcome (timeout), not a policy refusal and not a failure of "
    "the command's own logic; rc is UNKNOWN. Re-run with a larger timeout_s, or "
    "have the command write to a file the caller reads back through a guarded "
    "path, to obtain certifiable output.]"
)


def _timeout_post_exec_handler(
    *,
    timeout_s: float,
    stdout_captured: str,
    stderr_captured: str,
    duration_s: float,
    request_audit_tag: str,
    request_cwd: str,
    request_argv_head: str,
    request_network_posture: str,
    request_reachability: str,
    session_id: Any | None = None,
) -> "ShellEgressResult":
    """The TIMEOUT counterpart of `_post_exec_handler` (#665), shared by
    `execute()` and `execute_shell()` for the same reason that one is shared:
    both surfaces carried an identical `_timed_out` branch, and each returned
    RAW child stdout before reaching the guard while writing NO audit row. That
    made a timeout the cleanest way for output to escape unscanned and
    unrecorded — and timeouts are ordinary, not adversarial.

    WHY THIS IS NOT "run the guard here too":

      1. COMPLETENESS CANNOT BE ESTABLISHED. `_run_capture_tree_kill` kills the
         tree and then drains; the byte stream is cut wherever the kill landed,
         possibly mid-token. A secret that straddles that cut is invisible to a
         pattern scan, so a partial payload scanned clean is NOT proven clean.
         Unknown is not a pass, so the payload is withheld UNCONDITIONALLY.
      2. THE CHILD IS ALREADY REAPED. The kill (`_kill_process_tree`) and the
         bounded 3s drain both complete before this handler is entered, so
         nothing can append to the payload after the decision — and if that
         drain itself timed out, the captured text is empty anyway.
      3. THE GUARD CANNOT HANG THIS PATH, because it is never invoked on it.
         Withholding is strictly stronger than scanning, so there is nothing to
         gain by scanning and a second unbounded step would be exactly the
         hazard this path exists to escape: `output_guard.scan_text` is
         CPU-bound regex work over the whole payload, and adding it to the path
         that exists because something already hung would be a second timeout.

    The partial output is NOT ERASED as a fix — a blank is not a measurement.
    Its SIZE is recorded in the audit row (a measurement), while the bytes
    themselves stay behind the withhold: the audit ledger is not an egress
    channel for uncertified output either.

    ACCESS IS NOT NARROWED: the caller still learns THAT it timed out, after
    how long, and how to get certifiable output — only WHAT was printed is
    withheld.
    """
    # Audit row (best-effort, exactly as the non-timeout path).
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
            # INFRASTRUCTURE LANGUAGE. Not "ok", not "nonzero_exit" — neither is
            # true, and calling a timeout either one would be the same class of
            # lie #608 named.
            status="timeout",
            payload={
                "argv_head": request_argv_head,
                # NO FABRICATED EXIT CODE. The tree was killed; it never
                # reported a status. `None` plus an explicit exit_state says
                # "unknown" — 0 would claim success, 1 would claim failure.
                "rc": None,
                "exit_state": "unknown_tree_killed",
                "timeout_s": round(float(timeout_s), 3),
                "duration_s": round(duration_s, 3),
                "output_guard_marker": "timeout_withheld_truncated",
                "output_status": OUTPUT_WITHHELD_TRUNCATED,
                # Sizes, never bytes — evidence that output existed without
                # egressing it.
                "stdout_bytes_captured": len(stdout_captured or ""),
                "stderr_bytes_captured": len(stderr_captured or ""),
                "network_posture": request_network_posture,
                "reachability": request_reachability,
            },
        )
    except Exception:
        pass

    return ShellEgressResult(
        # `ok` is UNCHANGED from the pre-fix branch (False), so every
        # fail-closed seal and security test that reads it holds byte-for-byte.
        ok=False,
        # `rc` stays None — no invented exit code.
        rc=None,
        stdout=_TIMEOUT_WITHHELD_NOTICE,
        # The child's stderr was ALREADY replaced by this service-authored
        # sentence before the fix, which is why only stdout leaked; it stays
        # service-authored so the infrastructure outcome survives the withhold.
        stderr=f"timeout after {timeout_s:.0f}s (process tree killed)",
        duration_s=duration_s,
        # Unchanged vocabulary: callers already branch on this exact string.
        refused_reason="timeout",
        # The documented third state — "it did not fail, it did not succeed, it
        # never happened". `ShellEgressResult.action_ok` names a timeout
        # tree-kill explicitly as one of its None cases.
        action_ok=None,
        output_status=OUTPUT_WITHHELD_TRUNCATED,
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
    """CARRIES NO AUTHORITY. Transport plumbing only — `execute()` never reads
    it, and nothing in this module validates it.

    It previously documented itself as "a two-phase confirm token, when the
    underlying policy demands one (e.g. destructive_floor commands)", which was
    false: the identifier appeared exactly twice in this file, its own
    declaration and one comment. A reader deciding whether confirmation was
    enforced would have found a field saying yes.

    CONFIRMATION IS NOT DECIDED HERE (local backlog 984). The canonical
    authority is `canonical_invocation.ConfirmStore` — server-minted, single-use,
    bound to operator/project/session/tool/args/intent — and it is consumed at
    the GATE BOUNDARY, above this service. A successfully consumed handle is
    carried into one dispatch as `with_gate_confirmation(tool, args_hash)`.

    THIS FIELD MUST NEVER VALIDATE CONFIRMATION INDEPENDENTLY. Two homes for
    confirmation truth is the defect this docstring used to be an instance of;
    if it is ever wired, it forwards to ConfirmStore and says so here."""

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

    action_ok: bool | None = None
    """Did the ACTION succeed — ``rc == 0`` — independently of whether its
    output could be shown? ``None`` means the command NEVER RAN (a gate
    refusal, a timeout tree-kill, an exec error), which is a third state a
    bool cannot hold: "it did not fail, it did not succeed, it never
    happened".

    #608: ``ok`` alone conflated this with output certification, so a commit
    that landed was reported ``success:false`` beside ``exit_code:0`` — and an
    agent that reasonably retried it walked toward a session freeze. A caller
    asking "did my command work?" reads THIS field."""

    output_status: str = OUTPUT_NOT_EXECUTED
    """The output_guard's verdict on the payload: one of ``OUTPUT_STATUSES``.
    Default ``not_executed`` because a refusal produced no output to certify —
    calling that ``clean`` would be the same class of lie #608 names."""


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

        Current law (2026-05-29, Empire re-seal — single source of truth
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
                env=_augment_path_with_tools(request.env),
                timeout=timeout,
                shell=False,
            )
            if _timed_out:
                # #665 — an EXECUTED path, so it gets a post-exec handler like
                # every other executed path: withhold the truncated payload and
                # write the audit row. It used to return `_so` RAW here.
                return _timeout_post_exec_handler(
                    timeout_s=timeout,
                    stdout_captured=_so,
                    stderr_captured=_se,
                    duration_s=time.monotonic() - t0,
                    request_audit_tag=request.audit_tag,
                    request_cwd=request.cwd or "",
                    request_argv_head=request.argv[0] if request.argv else "",
                    request_network_posture=request.network_posture,
                    request_reachability=request.reachability,
                    session_id=request.metadata.get("session_id"),
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
        # #561 phase 1 — NAME THE INTERPRETER. Until now this spawned with
        # shell=True and no executable=, so CPython chose the PLATFORM DEFAULT:
        # cmd.exe on Windows (the shell shell_resolver refuses PERMANENTLY) and
        # /bin/sh on POSIX — hardcoded, not $SHELL — which is dash on
        # Debian/Ubuntu. Meanwhile shell_resolver resolved a real bash, emitted
        # `shell_provider_resolved`, and was ignored ("REPORT-ONLY in Batch A ...
        # the existing shell=True Popen still runs"). The audit therefore
        # described a decision that did not govern, and the bash-grammar core law
        # reasoned about strings another shell would execute.
        #
        # `bash -c "<string>"` preserves EVERY shell-string semantic that
        # shell=True provided (pipes, redirects, globs, env substitution) — only
        # the choice of interpreter becomes explicit. The template is `-c`, never
        # `-lc`: no login files, no user PATH mutation, no aliases (see
        # shell_resolver.ResolvedShell).
        #
        # FAIL-CLOSED (was phase 1's fallback). The fallback was shell=True with no
        # executable=, i.e. the PLATFORM DEFAULT — which is precisely the pair of
        # interpreters shell_resolver refuses by name:
        #     Windows: "cmd.exe NEVER. No flag lifts this — the doctrine is absolute."
        #     POSIX:   "Refuse. /bin/sh and dash are NOT acceptable defaults."
        # So the degraded path ran the one shell the resolver forbids, while the
        # audit recorded a resolved bash. That is worse than not running: the
        # operator reads a governed verdict over an ungoverned execution.
        #
        # This is NOT the #560 lockout shape the operator deferred. That one
        # fails closed on an integrity SIGNAL, which a bug can spuriously trip.
        # This fails closed only when no bash exists ON DISK — a condition that is
        # binary, locally checkable, and repaired by installing Git for Windows
        # (which AIDOCS already requires: git is invoked directly by
        # failure_stewardship, bash_policy, mcp_server and this module, and the
        # deploy gate is itself a bash script). The refusal names that remedy.
        _resolved_argv, _interpreter = _resolve_interpreter_argv(command, cwd)
        if not _resolved_argv:
            # NAME WHAT WAS REFUSED. "No usable shell" alone is a dead end for an
            # operator: the box plainly HAS shells. The candidate registry exists
            # for exactly this sentence (#561 phase 3 — "one discovery pass that
            # can name what it REFUSES"), so the refusal reports the shells that
            # were found AND why each was rejected. Best-effort: a discovery that
            # throws must not swallow the real refusal underneath it.
            _rejected = ""
            try:
                from .shell_candidate_registry import ineligible_candidates

                _seen = [f"{c.path} ({c.reason})" for c in ineligible_candidates()[:6]]
                if _seen:
                    _rejected = " Detected but not eligible: " + "; ".join(_seen) + "."
            except Exception:
                _rejected = ""
            return ShellEgressResult(
                ok=False,
                rc=None,
                stdout="",
                stderr=(
                    "no usable shell resolved "
                    f"({_interpreter}) — refusing rather than falling back to the "
                    "platform default, which is cmd.exe on Windows and /bin/sh on "
                    "POSIX, both of which AIDOCS refuses by doctrine."
                    f"{_rejected} Install Git (Git for Windows bundles the bash "
                    "AIDOCS uses) and retry."
                ),
                duration_s=_time.monotonic() - t0,
                refused_reason="shell_unresolved",
            )
        try:
            # nosemgrep: aidocs-no-shell-true-in-subprocess  # shell=True is intentional in execute_shell(): it is the ONE chokepoint where shell-string semantics (pipes, redirects, env-substitution, glob expansion) survive after the destructive_floor + heuristic_judge cascade. argv-form callers MUST use execute() instead — which never sets shell=True. This waiver explicitly does NOT claim the destructive_floor "closes" all injection: the floor catches *destructive* shapes (rm -rf root, dd if=/dev/, fork-bombs, eval $(curl …)), and the judge catches credential-exfil / container-escape / hypervisor / inline-runtime-bypass shapes — but neither is a substitute for argv-form input sanitization. The contract that REMAINS THE CALLER'S responsibility is: do NOT concatenate untrusted (agent-derived / user-input) fragments into the command string passed here. If you must build a shell string at all, use shlex.quote() on every interpolated value, OR (preferred) switch the caller to argv-form and route through execute(). A doctrine test `test_no_untrusted_fragment_concat_into_execute_shell` enforces this against the callsite inventory.
            # Popen + process-group + tree-kill (2026-06): on timeout this
            # tears down the WHOLE tree and drains within a bounded window —
            # a subprocess.run(timeout=) here would block until grandchildren
            # close the inherited pipes (a 2s timeout could hang 30s).
            rc, _so, _se, _timed_out = _run_capture_tree_kill(
                _resolved_argv,
                cwd=cwd,
                env=_augment_path_with_tools(None),
                timeout=timeout,
                shell=False,  # the interpreter is NAMED above; never the platform default
            )
            if _timed_out:
                # #665 PARITY — the identical branch lived on both surfaces, so
                # curing only execute() would have MOVED the hole to whichever
                # caller used the shell-string door (code_runner.ai_run does).
                return _timeout_post_exec_handler(
                    timeout_s=timeout,
                    stdout_captured=_so,
                    stderr_captured=_se,
                    duration_s=_time.monotonic() - t0,
                    request_audit_tag=audit_tag,
                    request_cwd=cwd,
                    request_argv_head=command.split()[0] if command.split() else "",
                    request_network_posture=network_posture,
                    request_reachability=reachability,
                    session_id=(metadata or {}).get("session_id"),
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


# ── Audited spawn helper (backlog #335 Phase 1 — process-audit war) ──
#
# PURE OBSERVABILITY. audited_popen does NOT replace the fingerprint
# ENFORCEMENT gate below — LEGACY_SUBPROCESS_FINGERPRINTS stays the
# allow-list authority — it only ADDS a runtime ledger row per spawn
# (process_audit_store) so operators/agents stop guessing what
# subprocesses run, why, and when.


def audited_popen(
    argv,
    *,
    fingerprint,
    reason: str,
    session_id: str | None = None,
    lifetime=None,
    popen=None,
    **popen_kwargs,
):
    """subprocess.Popen with a process-audit ledger row.

    ``fingerprint`` names the LEGACY_SUBPROCESS_FINGERPRINTS registry
    row this spawn belongs to — canonically its
    ``(relpath, enclosing_fn, callee_kind)`` prefix, or the equivalent
    ``'::'``-joined string. ``reason`` is the human WHY
    (e.g. ``'watchdog-daemon-supervision'``).

    ``popen`` is an injection seam (tests pass a fake; production
    callers may pass a passthrough lambda so their file keeps its
    registered direct-Popen AST callsite for the doctrine scan). All
    ``popen_kwargs`` are forwarded UNCHANGED, so spawn behavior is
    byte-identical to a direct ``subprocess.Popen`` call.

    Recording is best-effort: a broken ledger never blocks or alters
    the spawn. Reaping runs on a lightweight daemon wait-thread
    (``Popen.wait`` is thread-safe alongside the caller's own
    poll/wait), exposed on the returned proc as
    ``_aidocs_audit_thread`` / ``_aidocs_audit_row_id`` for
    deterministic tests.
    """
    import threading
    import time

    from . import process_audit_store

    if popen is None:
        popen = subprocess.Popen  # this file is the sanctioned chokepoint

    start = time.monotonic()
    if lifetime is None:
        # NO SILENT DEFAULT (#757). This chokepoint spawns BOTH bounded tool
        # runs and the long-lived daemons (watchdog, mcp_server, hook broker).
        # Defaulting to bounded would give the watchdog a 30-minute life;
        # defaulting to persistent would recreate the orphans. So intent is
        # DECLARED by the caller, and an undeclared spawn keeps exactly its
        # historical behaviour rather than silently acquiring a new one.
        proc = popen(argv, **popen_kwargs)
    else:
        from .process_lifetime import spawn_with_lifetime

        proc = spawn_with_lifetime(argv, lifetime=lifetime, popen=popen, **popen_kwargs)

    row_id: int | None = None
    try:
        row_id = process_audit_store.record_spawn(
            pid=getattr(proc, "pid", None),
            ppid=os.getpid(),
            argv=list(argv),
            fingerprint=fingerprint,
            reason=reason,
            session_id=session_id,
        )
    except Exception:  # noqa: BLE001 — observability must never block a spawn
        row_id = None

    def _reap() -> None:
        try:
            code = proc.wait()
        except Exception:  # noqa: BLE001
            code = getattr(proc, "returncode", None)
        if row_id is None:
            return
        try:
            process_audit_store.record_reap(
                row_id,
                exit_code=code,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception:  # noqa: BLE001 — same law as record_spawn
            pass

    reaper = threading.Thread(
        target=_reap,
        name=f"aidocs-process-audit-reap-{getattr(proc, 'pid', '?')}",
        daemon=True,
    )
    reaper.start()
    try:
        proc._aidocs_audit_thread = reaper
        proc._aidocs_audit_row_id = row_id
    except Exception:  # noqa: BLE001 — a __slots__ proc still spawns fine
        pass
    return proc


def audited_run(
    argv,
    *,
    fingerprint,
    reason: str,
    session_id: str | None = None,
    run=None,
    **run_kwargs,
):
    """subprocess.run with a process-audit ledger row (#345 seal).

    The synchronous sibling of ``audited_popen`` — same laws:

    - ``fingerprint`` names the LEGACY_SUBPROCESS_FINGERPRINTS registry
      row this spawn belongs to; ``reason`` is the human WHY.
    - ``run`` is an injection seam (tests pass a fake; production
      callers pass a passthrough lambda so their file keeps its
      registered direct-run AST callsite for the doctrine scan). All
      ``run_kwargs`` are forwarded UNCHANGED, so behavior is
      byte-identical to a direct ``subprocess.run`` call — including
      ``check=True`` raising CalledProcessError and ``timeout=``
      raising TimeoutExpired.
    - Recording is BEST-EFFORT: a broken ledger never blocks or alters
      the spawn.

    ``subprocess.run`` never exposes the child's pid, so the spawn row
    is stamped with pid=NULL before launch and reaped inline when the
    call returns (or raises — TimeoutExpired leaves exit_code an
    honest NULL; CalledProcessError records the real returncode).
    """
    import time

    from . import process_audit_store

    if run is None:
        run = subprocess.run  # this file is the sanctioned chokepoint

    start = time.monotonic()
    row_id: int | None = None
    try:
        row_id = process_audit_store.record_spawn(
            pid=None,
            ppid=os.getpid(),
            argv=list(argv) if isinstance(argv, (list, tuple)) else [str(argv)],
            fingerprint=fingerprint,
            reason=reason,
            session_id=session_id,
        )
    except Exception:  # noqa: BLE001 — observability must never block a spawn
        row_id = None

    exit_code: int | None = None
    try:
        completed = run(argv, **run_kwargs)
        exit_code = getattr(completed, "returncode", None)
        return completed
    except subprocess.CalledProcessError as exc:  # check=True — real exit code known
        exit_code = exc.returncode
        raise
    finally:
        if row_id is not None:
            try:
                process_audit_store.record_reap(
                    row_id,
                    exit_code=exit_code,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            except Exception:  # noqa: BLE001 — same law as record_spawn
                pass


# ── Deliberate-console spawn registry (#345 windowless seal) ────────
#
# Every win32 spawn in mcp/server/aidocs_mcp/ must be windowless
# (CREATE_NO_WINDOW, or DETACHED_PROCESS which allocates no console at
# all) — the daemon runs under pythonw (GUI subsystem, no console of
# its own), so an unflagged console-subsystem child ALLOCATES A FRESH
# CONSOLE: a visible window on the operator's screen.
#
# The ONLY spawns allowed to show a console are the ones an operator
# is MEANT to watch. Each is registered here by its
# (relpath, enclosing_fn) callsite; the structural seal test
# (test_spawn_surface_seal.py) fails on any win32 spawn that neither
# carries a windowless flag nor appears in this registry. Adding a row
# here is friction by design: name the operator-facing justification.
DELIBERATE_CONSOLE_SPAWNS: tuple[tuple[str, str, str], ...] = (
    # (relpath, enclosing_fn, justification)
    (
        "cli.py",
        "_run_install",
        ("`aidocs setup` dependency install streams live output to the "
        "operator's OWN console (no capture); operator-initiated, never "
        "runs under the daemon."),
    ),
    (
        "cli.py",
        "cmd_config",
        ("`aidocs config --edit` launches the operator's $EDITOR, which is the "
        "WHOLE POINT of the command — a windowless editor would be an editor "
        "nobody can type into. Operator-initiated from their own terminal, "
        "never under the pythonw daemon. Routed through audited_run in #1031, "
        "having been invisible behind the `**/cli.py` semgrep exclude."),
    ),
)
# NOTE: code_runner_detached.spawn_detached(foreground=True) deliberately
# uses CREATE_NEW_CONSOLE (0x10) so the operator can watch a command live.
# It needs no row here: the flag split lives in _popen_kwargs_for_platform
# (windowless 0x200 | CREATE_NO_WINDOW by default), and the seal test pins
# both branches behaviorally (test_spawn_surface_seal.py).


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
# Per-callsite SEMANTIC fingerprint (Empire re-seal 2026-05-29 — upgrade
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
    # NOTE: claude_hook_shim.py::_enter_active_generation is deliberately NOT
    # listed here. A fingerprint row exists to pin an AUDITED callsite, and a
    # row without one reads as stale to test_map_is_both_ways_complete — which
    # is correct. That callsite cannot be audited at all (it must run when the
    # package holding the auditor is absent), so it is recorded once, in
    # spawn_census.UNAUDITABLE_PRE_IMPORT_SPAWNS, and every doctrine surface
    # consults that single entry rather than each keeping its own copy.
    # #1031 — TWO SPAWNS THE FILE-LEVEL SEMGREP EXCLUDES WERE HIDING. Both
    # existed unaudited behind `**/cli.py` and `**/server_plan_task_tools.py`
    # in the aidocs-laws exclude list, which the rule's own comment called
    # "noise control ... NOT the seal". Teaching the rule to recognise the
    # audited seam let the excludes go, and these two fell out immediately.
    # Now routed, not waived.
    (
        "cli.py",
        "cmd_config",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "operator-cli",
        ("operator-initiated $EDITOR launch — argv from shlex.split of the "
        "operator's own $EDITOR, no shell; console is DELIBERATE (see "
        "DELIBERATE_CONSOLE_SPAWNS)"),
    ),
    (
        "server_plan_task_tools.py",
        "ai_kill",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "run-lifecycle",
        ("forced tree-kill (taskkill /F /T) of a detached ai_run child — a "
        "destructive process operation that was invisible to the ledger; fixed "
        "argv, pid is an internally-tracked int, windowless"),
    ),
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
        "aidocs_service.py",
        "_spawn_broker_process",
        "subprocess.Popen",
        "shell=False",
        "",
        "OL",
        "daemon-supervision",
        (
            "#609: the watchdog spawning the hook-broker HOST it supervises. Fixed "
            "argv (pythonw -m aidocs_mcp.hook_broker_host), no shell, no agent "
            "input. A CHILD is the only way a deploy can give the resident hook "
            "evaluator fresh code that it can PROVE it loaded — an in-process "
            "rebuild recomputes the on-disk identity and would declare itself "
            "fresh while running the previous generation."
        ),
    ),
    (
        "aidocs_service.py",
        "_pid_listening_on",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "daemon-supervision",
        (
            "#568 D4 / #569 R2: naming the process that holds the proxy port when "
            "a bind has ALREADY failed. Fixed argv (netstat -ano / lsof), no "
            "shell, no agent input — only an int port the supervisor already "
            "owns. Diagnosis on a fatal path: it is hard-capped at 3s and every "
            "exception returns None, so a broken resolver degrades a loud correct "
            "failure into a slightly less specific one, never into a hang."
        ),
    ),
    (
        "code_runner.py",
        "_kill_process_tree",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "code-runner",
        "Windows taskkill helper; argv-only (head 'taskkill' fixed at caller; routed via audited_run)",
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
        "_spawn_and_track",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "detached-runner",
        ("detached run dispatcher (spawn_detached tail, hoisted for the "
        "#466 argv/shell dispatch split); needs lifecycle binding in tests"),
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
        ("PowerShell os-signature verification via the ABSOLUTE canonical system "
        "PowerShell (argv[0] is the _canonical_powershell() variable, never a "
        "PATH-resolved 'powershell' literal — the MCP server env may lack it on "
        "PATH); provider path via env var + -LiteralPath; operator-local"),
    ),
    (
        "governed_shell_attest.py",
        "_publisher_ok_uncached",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "governed-shell",
        ("bounded fixed-argv Authenticode publisher probe invoked by ABSOLUTE "
        "canonical system PowerShell (argv[0] is the _canonical_powershell() "
        "variable, never a PATH-resolved literal); attestation evidence the "
        "output-guard would withhold; operator-local, no untrusted input"),
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
        "aidocs_service.py",
        "spawn",
        "subprocess.Popen",
        "shell=False",
        "",
        "OL",
        "service-watchdog",
        "watchdog spawns the AIDOCS daemon it supervises (#249) — fixed argv, no agent input",
    ),
    (
        "cli.py",
        "_spawn_watchdog",
        "subprocess.Popen",
        "shell=False",
        "",
        "OL",
        "service-watchdog",
        "`aidocs service start` spawns the detached watchdog (#249) — fixed argv [sys.executable -m aidocs_mcp.cli service run], operator-initiated",
    ),
    (
        "cli.py",
        "cmd_service",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "service-watchdog",
        "`aidocs service install` registers the logon task via schtasks — fixed argv, operator-initiated, routed via audited_run",
    ),
    (
        "cli.py",
        "cmd_service",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "service-watchdog",
        "`aidocs service uninstall` removes the logon task via schtasks — fixed argv, operator-initiated, routed via audited_run",
    ),
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
        "backend_models.py",
        "_default_run_opencode",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "model-catalog",
        "opencode models host-CLI probe (moved from server_plan_task_tools.ai_models)",
    ),
    (
        "checkpoint_service.py",
        "_git_cat_file_bytes",
        "subprocess.run",
        "shell=False",
        "",
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
        "",
        "OL",
        "roslyn-client",
        "dotnet helper invocation",
    ),
    (
        "csharp_roslyn_client.py",
        "_spawn_process",
        "subprocess.Popen",
        "shell=False",
        "",
        "OL",
        "roslyn-client",
        "Roslyn worker spawn",
    ),
    (
        "failure_stewardship.py",
        "capture_first_seen_tree_hash",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "failure-stewardship",
        "git inspection for failure capture #1",
    ),
    (
        "failure_stewardship.py",
        "capture_first_seen_tree_hash",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "failure-stewardship",
        "git inspection for failure capture #2",
    ),
    (
        "failure_stewardship.py",
        "capture_head_sha",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "failure-stewardship",
        "git head sha capture",
    ),
    (
        "failure_stewardship.py",
        "_default_reverify_runner",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "failure-stewardship",
        "deterministic nodeid re-verify rerun (bug #68); nodeids validated path::test, no flag injection",
    ),
    (
        "file_ops.py",
        "_check_syntax",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "file-ops",
        "node-based syntax check",
    ),
    (
        "git_helpers.py",
        "run_git_sync",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "git-helpers",
        "general git porcelain helper",
    ),
    (
        "outer_gate_projects.py",
        "_git",
        "subprocess.run",
        "shell=False",
        "",
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
        "",
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
        "runtime_refresh.py",
        "refresh_runtime",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "runtime-refresh",
        # TEMPORARY — expected to be DELETED, not maintained. The 2026-07-28 CLI
        # ruling (#575) retires runtime_refresh.py by folding its logic into
        # `aidocs doctor`: refresh is a STEP INSIDE a repair path, never its own
        # entry point. This row exists only so the callsite registry does not abort
        # the deploy that carries the fold. Remove the row with the file.
        "provisions via a NAMED argv (aidocs_mcp.cli runtime --fix); no shell",
    ),
    (
        "runtime_service.py",
        "recent_commits_touching_file",
        "subprocess.run",
        "shell=False",
        "",
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
    (
        "server_deploy_tools.py",
        "resolve_git_origin",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "server-deploy-tools",
        "read-only `git config --get remote.origin.url` origin probe — fixed argv, no shell, bounded timeout, fail-closed",
    ),
    (
        "server_deploy_tools.py",
        "resolve_git_commit",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "server-deploy-tools",
        "read-only `git rev-parse --verify <ref>^{commit}` commit-pin probe (§5) — fixed argv, no shell, bounded timeout, fail-closed",
    ),
    (
        "backlog_sync_sitter.py",
        "_default_git",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "backlog-autosync",
        "backlog autosync git replication on the event-log dir (fixed git subcommands pull/status/add/commit/push) routed through audited_run; passthrough-lambda seam (#345). No shell, no agent-derived input, system-internal, fail-open; CREATE_NO_WINDOW.",
    ),
    (
        "test_runner.py",
        "interpreter_drift",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "ai_test-interpreter-drift",
        "reads the DECLARED test interpreter's installed version of the project under test, to say whether that interpreter has drifted from the tree it is about to run. Routed through audited_run; passthrough-lambda seam (#345). The interpreter path comes from the `test.interpreter` catalog setting (project/global scope, operator-written) and is never agent-supplied; fixed argv, no shell, bounded timeout, DIAGNOSTIC ONLY — every failure path returns None so it can never become the reason a suite does not run. CREATE_NO_WINDOW.",
    ),
    (
        "backlog_staleness.py",
        "_git",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "backlog-staleness",
        "#836 backlog staleness scan: ONE bounded, memoized `git log` over commit subjects/bodies to flag open items a commit already claims, routed through audited_run; passthrough-lambda seam (#345). Fixed argv, no shell, no agent-derived input, READ-ONLY (log only, never a mutating subcommand), fail-quiet; CREATE_NO_WINDOW because the daemon runs under pythonw.",
    ),
    (
        "failure_stewardship.py",
        "_nodeid_is_collectable",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "stewardship-collectability",
        "#775 intake collectability probe: ONE bounded `pytest --collect-only -q <nodeid>` per genuinely NEW failure signature (skipped entirely for reobservations), routed through audited_run; passthrough-lambda seam (#345). Fixed argv, no shell, READ-ONLY — collection only, it never executes a test. The nodeid is not agent-authored prose: it is a pytest id scraped from a test run and already argv-guarded. FAILS OPEN on every ambiguity (missing file, subprocess crash, timeout, unexpected exit code, no project_root); ONLY a confirmed 'file parsed, named test absent' (exit 4 + 'not found' + 'no match in any of') rejects, so a real failure is never lost to a probe error.",
    ),
    (
        "env_floor_audit.py",
        "_run_pip_check",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "env-floor-audit",
        "session-start declared-floor preflight `pip check` ([sys.executable,-m,pip,check]) routed through audited_run; passthrough-lambda seam (#345). Read-only metadata probe, no shell, no agent input, bounded timeout, fail-open; CREATE_NO_WINDOW.",
    ),
    # TS — test-shaped predicates
    (
        "conditional_predicates.py",
        "_git_clean",
        "subprocess.run",
        "shell=False",
        "",
        "TS",
        "conditional-predicates",
        "_git_clean sentinel check #1",
    ),
    (
        "conditional_predicates.py",
        "_git_clean",
        "subprocess.run",
        "shell=False",
        "",
        "TS",
        "conditional-predicates",
        "_git_clean sentinel check #2",
    ),
    (
        "outer_gate_sandbox.py",
        "_docker",
        "subprocess.run",
        "shell=False",
        "",
        "OL",
        "cloudagent-runtime",
        "governed sandbox-worker docker lifecycle (create/inspect/rm) via fixed argv; no shell",
    ),
    (
        "lsp/client.py",
        "_spawn",
        "subprocess.Popen",
        "shell=False",
        "",
        "OL",
        "lsp-door",
        ("aidocs_lsp DOOR (§XXXII): spawns a vendored language server "
        "(pyright/csharp-ls/rust-analyzer) behind the fail-open door via "
        "audited_popen passthrough — fixed argv (resolved binary + spec args), "
        "no shell, no agent-derived input, hard-timeout + evict lifecycle"),
    ),
)


LEGACY_SUBPROCESS_CALLSITES: tuple[tuple[str, str, str], ...] = (
    # (relpath, classification, rationale)
    ("agent_expert_service.py", "AR", "expert subprocess fanout"),
    (
        "aidocs_service.py",
        "OL",
        ("watchdog spawns the AIDOCS daemon it supervises (#249) — fixed argv "
        "[sys.executable -m aidocs_mcp.mcp_server --http --port N], no shell, "
        "no agent-derived input; runs detached with no runtime context, so the "
        "egress chokepoint (agent-shell law) does not apply"),
    ),
    (
        "claude_hook_shim.py",
        "OL",
        ("the fail-closed hook launcher re-enters itself under the runtime "
        "generation the activation pointer names (#1030). It is stdlib-only and "
        "executes from ~/.aidocs/runtime/ OUTSIDE site-packages so a package "
        "swap cannot take it away (#616), so it cannot import this module "
        "without depending on the package it exists to survive the absence of. "
        "Fixed argv [<generation python> <this file>], no shell, no "
        "agent-derived input; the process it starts is the hook itself, which "
        "is then governed normally by the runtime it entered"),
    ),
    (
        "server_plan_task_tools.py",
        "AR",
        ("#1031: forced tree-kill (taskkill /F /T) of a detached ai_run child. "
        "Was unaudited behind the `**/server_plan_task_tools.py` semgrep "
        "exclude — a destructive process operation invisible to the ledger. "
        "Now routed through audited_run: fixed argv, no shell, pid is an "
        "internally-tracked int, CREATE_NO_WINDOW."),
    ),
    ("aidocs_nlp/installer.py", "OL", "pip install bootstrap"),
    ("backend_models.py", "OL", "host-CLI model-catalog probe — opencode models, fixed argv, no shell, read-only"),
    ("checkpoint_service.py", "OL", "git checkpoint shelling"),
    ("cli.py", "OL", "operator-initiated CLI ops"),
    (
        "test_runner.py",
        "OL",
        ("ai_test drift probe — reads the DECLARED test interpreter's installed "
        "version of the project under test. Routed through audited_run; the "
        "flagged line is the fingerprint-preserving passthrough lambda. Fixed "
        "argv, no shell; the interpreter path is the operator-written "
        "`test.interpreter` setting, never agent input. Diagnostic only — every "
        "failure path returns None"),
    ),
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
    (
        "lsp/client.py",
        "OL",
        ("aidocs_lsp door (§XXXII) — vendored language-server spawn via "
        "audited_popen passthrough; fixed argv, no shell, fail-open + evict"),
    ),
    ("mcp_server.py", "AR", "server-tier shellouts"),
    ("outer_gate_projects.py", "OL", "project bootstrap shellouts"),
    ("outer_gate_sandbox.py", "OL", "sandbox-worker docker lifecycle — fixed argv, no shell"),
    ("package_integrity.py", "OL", "signing/integrity checks"),
    ("runtime_bootstrap_service.py", "OL", "runtime bootstrap probes"),
    ("runtime_provisioner.py", "OL", "venv provisioning"),
    (
        "runtime_refresh.py",
        "OL",
        ("local enforcement-runtime reinstall (#569) — fixed argv "
        "[sys.executable -m aidocs_mcp.cli runtime --fix], no shell, no "
        "agent-derived input, bounded timeout, CREATE_NO_WINDOW; runs from the "
        "watchdog to repair the runtime the egress chokepoint itself executes "
        "under, so it must not require that runtime to be healthy. TEMPORARY "
        "alongside its LEGACY_SUBPROCESS_FINGERPRINTS row: #575 retires this "
        "file into `aidocs doctor` and both rows go with it"),
    ),
    ("runtime_service.py", "OL", "runtime status probes"),
    (
        "env_floor_audit.py",
        "OL",
        ("session-start declared-floor preflight: `pip check` via fixed argv "
        "[sys.executable -m pip check], no shell, no agent-derived input, "
        "read-only, short timeout, fail-open — a metadata probe, not agent egress"),
    ),
    (
        "backlog_sync_sitter.py",
        "OL",
        ("backlog autosync git replication: fixed git subcommands (pull/status/"
        "add/commit/push) on the event-log dir, no shell, no agent-derived "
        "input; system-internal continuous sync, fail-open — not agent egress"),
    ),
    (
        "backlog_staleness.py",
        "OL",
        ("#836 staleness scan: ONE bounded, memoized `git log` over commit "
        "subjects/bodies to flag open backlog items a commit already claims. "
        "READ-ONLY by construction — log is the only subcommand it can issue — "
        "fixed argv, no shell, no agent-derived input, fail-quiet; not agent "
        "egress"),
    ),
    (
        "failure_stewardship.py",
        "OL",
        ("#775 intake collectability probe: ONE bounded `pytest --collect-only` "
        "per genuinely NEW failure signature, to keep an uncollectable nodeid "
        "out of the stewardship ledger — one malformed id there permanently "
        "disables the self-healing re-verify loop. READ-ONLY by construction "
        "(collection only, never executes a test), fixed argv, no shell; FAILS "
        "OPEN on every ambiguity so a real failure is never lost to a probe "
        "error; not agent egress"),
    ),
    ("server_deploy_tools.py", "OL", "read-only git remote.origin.url origin probe — fixed argv, no shell, fail-closed"),
    ("server_legacy_git_tools.py", "OL", "legacy git tool surfaces"),
    ("shell_resolver.py", "OL", "shell probe (--version / -c)"),
    ("slop_backends.py", "OL", "slop backend tooling"),
    ("updater_service.py", "OL", "self-update probes"),
    ("workflow_action_service.py", "OL", "workflow action shellouts"),
)
