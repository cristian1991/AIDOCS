"""Fail-CLOSED hook launcher — the survivor of the runtime package swap (#616).

THE FAILURE THIS SEALS (observed 2026-07-29, operator machine): the Claude Code
hook command was ``<pythonw> -m aidocs_mcp.claude_hook``, so the enforcement
ENTRY POINT lived inside the very package the runtime refresh reinstalls.
During the venv-tier package swap (``runtime_provisioner.
_install_package_into_venv`` phase C — "NOT ATOMIC, MERELY NARROW": a
quarantine rename followed by one wheel unpack, and an UNBOUNDED window on the
unstaged fallback or a crash between the two) the interpreter died inside
runpy — "Error while finding module specification for
'aidocs_mcp.claude_hook'" — before ONE line of gate code had loaded. Claude
Code treats a hook that exits non-zero (other than 2) as a NON-BLOCKING
failure and proceeds, so every tool call in the window ran ungoverned. #589
made the gate REFUSE when it cannot verify its own integrity, and that path
worked exactly as designed the same night — but a missing module never reaches
#589's logic, because the code that refuses was itself absent. And nothing
recorded it: both breadcrumb writers (``_report_hook_failure``,
``gate_health.record_hook_decline``) live in the missing package too.

The window is reachable OUTSIDE deploys — the watchdog drives
``runtime_refresh``, ``aidocs runtime --fix`` is operator-run, and any manual
pip operation on the runtime venv reproduces it — so the seal cannot live in
the deploy. It must live in a file the package swap cannot take away.

THIS FILE'S CONTRACT:

* ``claude_hooks_install.ensure_hook_shim`` copies it VERBATIM to
  ``~/.aidocs/runtime/`` — OUTSIDE site-packages, untouched by any pip
  install/uninstall — and the hook command in ``~/.claude/settings.json``
  launches THAT copy.
* stdlib-only, no aidocs imports at module import time: it must run when the
  package is absent, partial, or broken. That is its entire reason to exist.
* HEALTHY runtime → delegate to ``aidocs_mcp.claude_hook.main()`` verbatim.
  stdin is read HERE and replayed to it byte-for-byte (the crash posture below
  needs the event name, and it cannot be recovered from a stream the real hook
  has already consumed); stdout is passed through a witness that only records
  whether the hook spoke.
* CRASHED hook (import succeeded, evaluation did not finish) → the SAME
  refusal. This was originally left to propagate, on the grounds that the shim
  "only guards the LOAD" — but a propagated exception is a non-zero exit, and
  the host treats that exactly as it treats a missing module: NON-BLOCKING. So
  every other cause (a syntax error in a lazily imported module, a drifted
  interpreter, MemoryError, a helper calling sys.exit) reopened the identical
  ungoverned window. The loud contract is kept — the real hook still reports
  via ``_report_hook_failure``, the traceback still reaches stderr, the
  breadcrumb still lands — and the host now also gets a verdict it honours. If
  the hook ALREADY answered, the exception propagates unchanged rather than
  appending a second verdict.
* UNLOADABLE runtime → REFUSE with the #589 postures: PreToolUse (the
  enforcement floor, the only event where a deny prevents the ungoverned
  action) emits an explicit DENY verdict; UserPromptSubmit / SessionStart /
  PostToolUse degrade LOUDLY via additionalContext (denying them would lock
  the operator out before the remedy text could be read); Stop-class,
  SubagentStart and unknown events get stderr — a block on Stop can loop the
  host, and no verdict shape is VERIFIED on SubagentStart. Every
  refusal appends a leading-UTC-stamped breadcrumb to
  ``<daemon_dir>/hook_failures.log`` so ``gate_health`` counts the window
  instead of it vanishing without a trace.

WHY FAILING CLOSED HERE CANNOT WEDGE A DEPLOY: a PreToolUse deny only blocks
the agent's NEXT tool call. The refresh that heals the window runs inside an
already-permitted tool call (the deploy gate's shell) or inside the watchdog —
neither passes through this hook again mid-install, so the install always
completes and the very next hook spawn loads the repaired package and resumes
governing.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time

# Kept in lockstep with claude_hook._DRIFT_BANNER_EVENTS / the #589 posture
# table — and with gate_health._SHIM_GOVERNED_EVENTS, a THIRD copy that
# decides whether these breadcrumbs count as governed. Duplicated BY DESIGN:
# importing them would defeat this file. "Kept in lockstep" was an unenforced
# claim until tests/host/test_claude_hook_shim_fail_closed.py::
# test_shim_posture_tiers_are_in_lockstep_with_their_two_duplicates — a
# comment cannot hold three copies together, only a test can.
_DENY_EVENT = "PreToolUse"
_BANNER_EVENTS = ("UserPromptSubmit", "SessionStart", "PostToolUse")
# The THIRD tier, named rather than left to the unknown-event fall-through.
# `SubagentStart` (registered 2026-08-22) arrived at a table that described
# only two of its three tiers, so a newly registered event could start
# reaching the fail-closed launcher with nobody having CHOSEN its posture —
# it would silently inherit whatever "unknown" happens to do. It sits in this
# tier deliberately, on the two criteria above:
#   * a DENY buys nothing. The spawn is itself a PreToolUse call (`Task` and
#     `Agent` are both PreToolUse-matched), as is every tool the subagent then
#     calls — in an unloadable-runtime window all of them are already denied.
#   * a BANNER is unverified. additionalContext is the loudest SAFE channel
#     only where the host is known to honour it; nothing establishes that for
#     SubagentStart (the one measurement of this event wrote nothing to
#     stdout by design, so it captured the payload, never the output
#     contract). An unverified shape, emitted from the file that must work
#     when nothing else does, is the wrong side of that uncertainty — so the
#     posture that emits NOTHING wins until the host behaviour is measured.
_STDERR_ONLY_EVENTS = ("Stop", "SubagentStop", "SubagentStart", "PostCompact")


def _posture_for(event: str) -> str:
    """The #589 tier this event gets. Also written into the breadcrumb, so the
    durable trace can tell a DECLARED silence from an event nobody has
    classified yet: the first is a decision, the second is news."""
    if event == _DENY_EVENT:
        return "deny"
    if event in _BANNER_EVENTS:
        return "banner"
    if event in _STDERR_ONLY_EVENTS:
        return "stderr-only"
    return "stderr-only-unregistered"


def _daemon_dir() -> str:
    """Mirror of aidocs_service.daemon_dir, stdlib-only (the original lives in
    the package that may be missing)."""
    return os.environ.get("AIDOCS_DAEMON_DIR") or os.path.join(
        os.path.expanduser("~"), ".aidocs", "daemon"
    )


def _breadcrumb(text: str) -> None:
    """Append the durable refusal trace. Leading UTC stamp is the contract
    gate_health._parse_leading_utc counts on; same file both in-package
    writers use (one evidence trail, one reader). Never raises."""
    try:
        d = _daemon_dir()
        os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(os.path.join(d, "hook_failures.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} claude_hook shim REFUSED (fail-closed): {text}\n")
    except Exception:
        pass


def _read_event() -> str:
    """The incoming hook event name, or '' when stdin is empty/unparseable.
    Only called before delegation — nothing has consumed stdin yet."""
    return _event_from_raw(_slurp_stdin())


def _slurp_stdin() -> str:
    """#1017: decode the payload UTF-8, never with the console codec.

    `sys.stdin.read()` here decoded with the platform default (cp1252 on
    Windows), so a payload carrying an em dash or an emoji was mojibaked before
    anything downstream saw it. Reading the buffer sidesteps the text wrapper.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        try:
            return buffer.read().decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        return sys.stdin.read()
    except Exception:
        return ""


def _event_from_raw(raw: str) -> str:
    """The hook event name carried by a raw stdin payload, or ''."""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("hook_event_name") or "").strip()


class _WitnessedStdout:
    """stdout that remembers whether the hook already spoke.

    The crash path must not append a SECOND verdict to one the real hook
    already delivered — the host would then be reading an answer nobody asked
    for. Everything else is delegated untouched.
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self.wrote = False

    def write(self, text):
        if text:
            self.wrote = True
        return self._stream.write(text)

    def __getattr__(self, name):  # flush/encoding/isatty/fileno/...
        return getattr(self._stream, name)


def refuse(exc: BaseException, *, event: str | None = None, crashed: bool = False) -> int:
    """The gate could not reach a verdict: refuse, loudly + durably.

    A gate that cannot run its own code cannot be trusted to PERMIT, but can
    always be trusted to REFUSE (#589's principle, extended to the failures
    #589 could not reach). Returns the process exit code — 0, because every
    refusal here is delivered AS A VERDICT (or as the loudest thing the event
    supports), never as a crash the host would shrug off as non-blocking.

    Two causes, ONE posture table. ``crashed=False`` is the unloadable runtime
    (the measured #616 window); ``crashed=True`` is the hook process dying
    AFTER a successful import — a syntax error in a lazily imported module, a
    drifted interpreter, MemoryError, a helper that called sys.exit. The host
    treats both identically (non-zero exit == non-blocking), so treating them
    differently HERE would leave the second one fail-open, which is the same
    defect wearing a different traceback.
    """
    if event is None:
        event = _read_event()
    if crashed:
        reason = (
            "AIDOCS GATE REFUSING - the hook could not complete its evaluation "
            f"and CRASHED ({type(exc).__name__}: {exc}). The gate reached no "
            "verdict, and a crashed hook is NON-BLOCKING to the host - so this "
            "tool call would otherwise have run ungoverned (#616). A gate that "
            "cannot decide cannot be trusted to PERMIT; it is DENIED instead. "
            "The traceback is on stderr above and in the hook failure log; if "
            "it persists, the runtime is broken - run: aidocs runtime --fix "
            "(under the runtime interpreter)."
        )
        blocked_by = "hook_crashed_before_verdict"
    else:
        reason = (
            "AIDOCS IS UPDATING - GATE REFUSING, tool calls are DENIED until it "
            "finishes. The "
            f"enforcement package is not importable right now ({type(exc).__name__}: "
            f"{exc}), which is what a runtime refresh package swap looks like from "
            "here (#616). A gate that cannot load its own code cannot be trusted to "
            "PERMIT, but can always be trusted to REFUSE (#589), so this is the "
            "system working, not a break. "
            "EXPECT ABOUT A MINUTE, NOT SECONDS - measured 2026-08-27: 37s of "
            "package absence inside a 91s provision. Retrying is pointless until "
            "then, and NO IN-SESSION RECOVERY EXISTS: an agent reading this cannot "
            "fix it, because the repair command needs the same shell this gate is "
            "denying. If it outlasts a few minutes it is not an update - tell the "
            "operator, who can run `aidocs runtime --fix` under the runtime "
            "interpreter."
        )
        blocked_by = "hook_runtime_unloadable"
    # CARRY THE CAUSE INTO THE BREADCRUMB (operator ruling 2026-09-05: "hook
    # declines during UPDATE should not register as degraded").
    #
    # This function has ALREADY decided which of the two causes it is — the
    # update-window package swap, or a hook that crashed after import — and it
    # says so to the operator in `reason` and to the host in `blocked_by`. It
    # then wrote a breadcrumb that mentioned NEITHER, so gate_health read a
    # line like "ModuleNotFoundError: No module named 'aidocs_mcp'" and could
    # only guess. It guessed "degraded", and a routine runtime refresh has been
    # reporting the gate as possibly-not-governing ever since.
    #
    # The classification is not re-derived downstream, and gate_health does not
    # sniff the exception text: the writer that KNOWS stamps it.
    _breadcrumb(
        f"{type(exc).__name__}: {exc} - event={event or 'unknown'} "
        f"- posture={_posture_for(event)} - cause={blocked_by}"
    )
    try:
        sys.stderr.write(f"[aidocs hook shim] {reason}\n")
    except Exception:
        pass
    if event == _DENY_EVENT:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": _DENY_EVENT,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                    "blocked_by": blocked_by,
                }
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0
    if event in _BANNER_EVENTS:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": reason,
                }
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0
    # _STDERR_ONLY_EVENTS (Stop / SubagentStop / SubagentStart / PostCompact)
    # and unrecognized events: no verdict shape prevents anything here, a
    # block on Stop can loop the host, and no shape is VERIFIED on
    # SubagentStart; stderr + the breadcrumb above (which records which of
    # those two cases this was) are the loudest available evidence.
    return 0


# --- THE ACTIVATION POINTER (#1030) ------------------------------------------
#
# DUPLICATED FROM aidocs_mcp.runtime_generations, DELIBERATELY. This file is
# stdlib-only and lives OUTSIDE site-packages precisely so a package swap cannot
# take it away — which is exactly why it cannot import the module that owns this
# contract. The two copies are held together by
# tests/host/test_runtime_generation_pointer_1030.py, the same way the #589
# posture table above is held: a comment cannot keep two copies in lockstep,
# only a test can.
#
# READ EXACTLY ONCE PER INVOCATION, before stdin and before any aidocs import. A
# second read mid-flight is how one hook call starts under generation A and
# finishes importing pieces of B — the MIXED state the design exists to make
# unreachable.
_POINTER_FILENAME = "current.json"
_GENERATIONS_DIRNAME = "generations"
_COMPLETE_MARKER = "generation.complete.json"
_GENERATION_ID_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: Stamped on the child so a re-exec can never recurse, and so anything
#: downstream can answer "which generation served this call?" honestly.
_GENERATION_ENV = "AIDOCS_RUNTIME_GENERATION"


def _runtime_root() -> str:
    return os.environ.get("AIDOCS_RUNTIME_ROOT") or os.path.join(
        os.path.expanduser("~"), ".aidocs", "runtime"
    )


def _active_generation() -> tuple[str, str]:
    """``(generation_id, failure_reason)`` — and the two "" are NOT the same.

    THE COLLAPSE THIS FIXES. The first cut returned a bare "" for everything,
    and the caller read "" as "stay on the interpreter that launched us". That
    merged the one case where staying is CORRECT with every case where it is a
    SUBSTITUTION:

      ('', '')          no pointer — a legacy single-tree install. The
                        launching interpreter IS the runtime; migration is not
                        a flag day and this must keep working.
      ('', <reason>)    a pointer EXISTS and cannot be served: unreadable,
                        malformed, naming an invalid id, naming a generation
                        that is absent, or one never sealed. The operator
                        activated a specific runtime, and continuing under a
                        different one is the substitution this file exists to
                        prevent — on a migrated box the interpreter that
                        launched us is the OLD generation.

    SEALED OR NOTHING remains: a directory that exists but was never marked
    complete is a build that died halfway, and entering it is the PARTIAL
    import that produced #932's four ungoverned calls.

    Mirrors ``runtime_generations.serving_venv``, which draws the same line for
    the package side. The duplication is the same deliberate one as the pointer
    constants, and the lockstep test holds them together.
    """
    try:
        path = os.path.join(_runtime_root(), _POINTER_FILENAME)
        if not os.path.isfile(path):
            return "", ""
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return "", "pointer_malformed"
        gid = str(raw.get("generation_id") or "").strip()
        if not gid:
            return "", "pointer_malformed"
        if not _GENERATION_ID_SHAPE.fullmatch(gid):
            return "", "pointer_names_invalid_generation_id"
        gdir = os.path.join(_runtime_root(), _GENERATIONS_DIRNAME, gid)
        if not os.path.isdir(gdir):
            return "", "pointer_names_a_generation_that_is_not_on_disk"
        if not os.path.isfile(os.path.join(gdir, _COMPLETE_MARKER)):
            return "", "generation_present_but_not_marked_complete"
        return gid, ""
    except Exception as exc:  # noqa: BLE001
        # A pointer we cannot read is not a pointer that is absent.
        return "", f"pointer_unreadable: {type(exc).__name__}"


def _generation_python(generation_id: str) -> str:
    """The interpreter belonging to a generation, or '' if it is not there."""
    base = os.path.join(_runtime_root(), _GENERATIONS_DIRNAME, generation_id, "venv")
    for rel in (os.path.join("Scripts", "pythonw.exe"), os.path.join("bin", "python")):
        cand = os.path.join(base, rel)
        if os.path.isfile(cand):
            return cand
    return ""


def _enter_active_generation() -> int | None:
    """Re-launch this shim under the ACTIVE generation's interpreter, or None to
    continue in this process. An int is a finished verdict — the child's exit
    code, or a refusal.

    THE INTERPRETER IS THE GENERATION SELECTOR: ``aidocs_mcp`` resolves out of
    whichever venv is running, so switching runtimes means switching pythons.

    None is returned ONLY when continuing here is CORRECT: we are already the
    active generation, or no generation is activated at all (a legacy install).
    A pointer that exists and cannot be served REFUSES instead. Continuing
    would govern the call with a runtime the operator did not activate — and
    after a migration the interpreter that launched us is the OLD generation,
    so "carry on" means "silently enforce superseded code".

    A SUBPROCESS, NOT ``os.execv``. On Windows execv terminates this process and
    starts another under a DIFFERENT pid; the host is waiting on ours, and a
    verdict arriving from a pid nobody is watching is a hook that did not
    answer. A child keeps one pid, one exit code and one stdio pair for the
    whole call.

    stdin is UNTOUCHED here and inherited by the child, which is why this must
    run before ``_slurp_stdin``.
    """
    if os.environ.get(_GENERATION_ENV):
        return None  # already inside a generation: never recurse
    gid, why = _active_generation()
    if not gid:
        if not why:
            return None  # legacy install: this interpreter IS the runtime
        return refuse(
            RuntimeError(f"the activated runtime generation cannot be served ({why})")
        )
    exe = _generation_python(gid)
    if not exe:
        return refuse(
            RuntimeError(f"the activated runtime generation {gid} has no interpreter")
        )
    if os.path.normcase(os.path.abspath(exe)) == os.path.normcase(
        os.path.abspath(sys.executable or "")
    ):
        return None  # this IS the active generation
    env = dict(os.environ)
    env[_GENERATION_ENV] = gid
    try:
        import subprocess

        # WINDOWLESS. The hook is launched by pythonw precisely so nothing
        # flashes on the operator's screen; spawning a plain child would put a
        # console window on every tool call. CREATE_NO_WINDOW is Windows-only,
        # so it is looked up rather than named.
        kwargs = {}
        flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flag:
            kwargs["creationflags"] = flag
        # THE WAIVER MUST TOUCH THE STATEMENT. semgrep honours `nosemgrep` only
        # on the finding's own line or the line immediately before it, so the
        # rationale lives here and the waiver itself rides the `return`.
        #
        # WHY THIS ONE CANNOT ROUTE THROUGH THE CHOKEPOINT: this file is
        # stdlib-only and executes from ~/.aidocs/runtime/ OUTSIDE
        # site-packages precisely so a package swap cannot take it away (#616).
        # Importing ShellEgressService would make the fail-closed launcher
        # depend on the very package whose absence it exists to survive. Fixed
        # argv [<generation python> <this file>], no shell, no agent-derived
        # input; the process it starts is the hook itself, governed normally by
        # the runtime it enters. Registered in LEGACY_SUBPROCESS_CALLSITES and
        # in spawn_census.UNAUDITABLE_PRE_IMPORT_SPAWNS.
        return subprocess.run(  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
            [exe, os.path.abspath(__file__)], env=env, check=False, **kwargs
        ).returncode
    except Exception as exc:  # noqa: BLE001
        # We could not enter the runtime the operator activated. Carrying on
        # under THIS interpreter would govern the call with a different runtime
        # than the active one — a substitution, not a fallback — so refuse.
        return refuse(exc)


def main() -> int:
    # ONE pointer read per invocation, before stdin and before any aidocs
    # import (#1030).
    code = _enter_active_generation()
    if code is not None:
        return code

    try:
        from aidocs_mcp.claude_hook import main as _real_main
    except BaseException as exc:  # noqa: BLE001 - ANY load failure means "cannot govern"
        return refuse(exc)

    # stdin is READ HERE, not left for the real hook: the crash path cannot
    # learn the event from a stream that has already been consumed, and the
    # event is what selects the #589 posture (deny vs banner vs stderr). The
    # real hook still reads EXACTLY the bytes the host sent - one
    # ``sys.stdin.read()`` in claude_hook.main, satisfied from this buffer.
    # #932: TELL THE INNER HOOK IT IS WRAPPED. claude_hook._report_hook_failure
    # writes "gate fails OPEN for this event" — true before this shim existed,
    # and FALSE whenever we are here, because the handler below converts any
    # crash into a DENY in the same second. gate_health believes that line and
    # counted a governed denial as an ungoverned escape, which raised a false
    # "the gate may NOT be governing this session" alarm four times (#803,
    # #890, #932). The inner hook cannot infer this, so it is told.
    os.environ["AIDOCS_HOOK_SHIM"] = "1"
    raw = _slurp_stdin()
    event = _event_from_raw(raw)
    sys.stdin = io.StringIO(raw)
    witness = _WitnessedStdout(sys.stdout)
    sys.stdout = witness
    try:
        _real_main()
    except SystemExit as exc:
        # A clean early exit is a hook CHOOSING to say nothing ("proceed"),
        # not a failure to decide. Any other code is the shape the host
        # silently ignored.
        code = exc.code
        if code in (0, None):
            return 0
        if witness.wrote:
            raise
        return refuse(exc, event=event, crashed=True)
    except BaseException as exc:  # noqa: BLE001 - ANY failure means "no verdict"
        if witness.wrote:
            # The hook already delivered a verdict the host will honour; keep
            # the loud non-zero contract rather than speaking twice.
            raise
        return refuse(exc, event=event, crashed=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
