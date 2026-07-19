"""Host-CLI resume dispatcher for denied lane completion reviews.

Phoenix 2026-05-08, emperor §VIII implementation:

When a conductor calls `ai_review(verdict='denied')`,
the original worker's process has long exited (it captured into a
review row and exited cleanly via task_complete's lane-worker branch).
To reconnect work-in-progress with the conductor's rationale, we
resume the worker's host session via the host CLI's `--resume`/
`-s`/`resume` command — the host (Claude Code / OpenCode CLI / Codex)
restores the full conversation memory keyed on host_session_id.

Per co-conductor's research (2026-05-08, high-confidence):
  - Claude Code: `claude --resume <session-id>`
  - OpenCode:   `opencode -s <session-id>` or `opencode --session <session-id>`
  - Codex CLI:  `codex resume <session-id>` or `codex exec resume <session-id>`

The resumed worker picks up where it left off, plus the bootstrap
prompt directs it to address the deny rationale and call
task_complete again with updated evidence.

APPROVE is silent — no resume needed. The captured work was good;
the worker's previous exit was the natural end.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _build_resume_argv(
    backend: str,
    host_session_id: str,
    bootstrap: str,
) -> list[str] | None:
    """Phoenix 2026-05-09: build full NON-INTERACTIVE resume command
    per backend.

    Prior shape (`opencode -s <id>` / `claude --resume <id>` /
    `codex resume <id>`) opened the host's INTERACTIVE TUI mode —
    bootstrap written to stdin was never read because TUI mode does
    not consume stdin as a prompt. The resumed subprocess hung
    waiting for keyboard input, never delivered the bootstrap to
    the model, never produced subagent activity.

    Verified non-interactive forms (host CLI --help, 2026-05-09):
    - claude: `claude -r <id> -p "<prompt>"` (-p forces print mode)
    - opencode: `opencode run -s <id> "<message>"` (run subcommand
      treats message positional as one-shot in the resumed session;
      --dangerously-skip-permissions + --format json mirror the
      spawn_worker_opencode flags so resumed runs use the same
      gating + parser path as fresh spawns)
    - codex: `codex resume <SESSION_ID> "<PROMPT>"` (both positional
      per `codex resume --help`)

    Returns argv (with bootstrap as a positional argument), or None
    when backend is unsupported.
    """
    b = (backend or "").strip().lower()
    if b == "claude":
        return ["claude", "-r", host_session_id, "-p", bootstrap]
    if b == "opencode":
        # Phoenix 2026-05-11 (#165 fix): include --agent aidocs-worker
        # for parity with spawn_worker_opencode's fresh-spawn path.
        # Without the agent profile, the resumed worker re-enters
        # with native opencode tools enabled (read/edit/grep), which
        # bypasses the lane gate — Stone #19 vulnerability (same
        # class as #163 env-passthrough). The agent profile is per-
        # invocation, not session-bound, so --session resume does
        # NOT carry it across; must be re-specified.
        # Also include --print-logs so resumed runs surface stderr
        # the same way fresh spawns do.
        return [
            "opencode",
            "run",
            "--agent",
            "aidocs-worker",
            "--dangerously-skip-permissions",
            "--print-logs",
            "--format",
            "json",
            "-s",
            host_session_id,
            bootstrap,
        ]
    if b == "codex":
        return ["codex", "resume", host_session_id, bootstrap]
    return None


_SUPPORTED_BACKENDS = ("claude", "opencode", "codex")


def _build_deny_bootstrap(
    *,
    lane_id: str,
    review_id: str,
    conductor_message: str,
    work_summary: str,
) -> str:
    """Bootstrap prompt for the resumed worker. Plain, directive,
    no doctrine leak. Tells the worker exactly what happened and
    what to do next.
    """
    msg = (conductor_message or "").strip() or "(no rationale provided)"
    summary = (work_summary or "").strip() or "(no prior summary on file)"
    # Phoenix 2026-05-11 (#165 fix): explicit precedence + no-op trap.
    # Prior bootstrap left room for the model to weight "lane plan
    # goal" over "conductor rationale" — smoke witness showed the
    # resumed worker re-read the plan, saw the file already matched
    # plan-text, and re-submitted with no further edits. Bootstrap
    # now declares: rationale + plan TOGETHER define done; current
    # file state matching the plan does NOT mean the work is
    # complete; the rationale's additional requirements MUST be
    # acted on before re-requesting review.
    return (
        f"## Your prior task_complete review for lane `{lane_id}` was "
        f"DENIED by the conductor.\n"
        f"\n"
        f"### Conductor rationale (BINDING — this is the recycle directive):\n"
        f"{msg}\n"
        f"\n"
        f"### Your prior work summary (what you submitted that was denied):\n"
        f"{summary}\n"
        f"\n"
        f"### Precedence and discipline:\n"
        f"- The conductor's rationale above is BINDING. It supersedes "
        f"any prior interpretation of the lane plan as already-satisfied. "
        f"The plan's done-definition has been extended by the rationale; "
        f"both must be satisfied now.\n"
        f"- If the file's current state already matches the lane plan's "
        f"original text but does NOT yet reflect the rationale's "
        f"additional requirements, the work is NOT done. The plan + the "
        f"rationale together define done.\n"
        f"- Read the rationale carefully. Identify the SPECIFIC change "
        f"or addition it asks for (append a line, restructure a region, "
        f"add a comment, etc.). Apply that change to the files in your "
        f"plan's Files: scope. Verify by reading the file back after "
        f"the edit.\n"
        f"- Then call `mcp__aidocs__ai_task(mode='complete')` again with updated "
        f"`result_summary` (mention BOTH the original edit AND the "
        f"rationale-driven change) and `verification_evidence`.\n"
        f"\n"
        f"Your full session context (the lane plan, prior tool calls, "
        f"prior edits) is preserved across this resume. The conductor "
        f"will review the updated work the same way (silent on approve, "
        f"resume on deny). Review id was: {review_id}."
    )


def resume_worker_on_deny(
    project_root: Path,
    *,
    review_row: dict[str, Any],
    conductor_message: str,
) -> dict[str, Any]:
    """Spawn the host CLI in --resume mode for a denied review.

    review_row carries host_session_id + backend (stamped at capture
    time by task_complete's lane-worker branch). Returns
    {dispatched, backend, host_session_id, [error]}.

    Best-effort: any failure (missing CLI, missing stamps, spawn
    error) returns dispatched=False with an error string. The
    conductor's verdict has already been recorded; the failure
    means the worker won't auto-resume but the deny is on file.
    """
    backend = str(review_row.get("backend") or "").strip().lower()
    host_session_id = str(review_row.get("host_session_id") or "").strip()
    lane_id = str(review_row.get("lane_id") or "").strip()
    review_id = str(review_row.get("review_id") or "").strip()
    work_summary = str(review_row.get("work_summary") or "").strip()

    if not host_session_id:
        return {
            "dispatched": False,
            "error": (
                "review row has no host_session_id stamp — likely a "
                "conductor-side test review or pre-Phoenix-2026-05-08 row. "
                "Worker cannot be resumed; conductor must reach out manually."
            ),
        }
    if backend not in _SUPPORTED_BACKENDS:
        return {
            "dispatched": False,
            "backend": backend,
            "host_session_id": host_session_id,
            "error": (
                f"unknown backend '{backend}' — no resume command mapped. "
                f"Supported: {sorted(_SUPPORTED_BACKENDS)}."
            ),
        }
    bootstrap = _build_deny_bootstrap(
        lane_id=lane_id,
        review_id=review_id,
        conductor_message=conductor_message,
        work_summary=work_summary,
    )
    argv = _build_resume_argv(backend, host_session_id, bootstrap)
    if argv is None:
        return {
            "dispatched": False,
            "backend": backend,
            "host_session_id": host_session_id,
            "error": f"backend '{backend}' has no argv builder",
        }
    cli_path = shutil.which(argv[0])
    if not cli_path:
        return {
            "dispatched": False,
            "backend": backend,
            "error": (f"host CLI '{argv[0]}' not on PATH — cannot resume."),
        }
    cli_args = [cli_path] + argv[1:]
    # Inherit env, including AIDOCS_EXPERT_LANE_ID + AIDOCS_EXPERT_ID
    # so the resumed process re-enters as a lane worker. The host
    # session resume restores conversation memory; env restores
    # worker identity for the gate cascade.
    spawn_env = dict(os.environ)
    spawn_env["AIDOCS_EXPERT_LANE_ID"] = lane_id
    if review_row.get("worker_id"):
        spawn_env["AIDOCS_EXPERT_ID"] = str(review_row["worker_id"])

    # Phoenix 2026-05-11: write the conductor message to the lane
    # mailbox BEFORE firing the resume. The opencode `run -s <id>
    # "<msg>"` form does NOT inject <msg> as a new user turn
    # (witnessed in smoke 2026-05-11 lane-3 — resumed worker
    # re-played the original prompt and ignored the deny rationale
    # entirely). Mailbox delivery is the canonical channel: the
    # resumed worker's session_connect drains it and surfaces the
    # conductor's directive in the response payload, where the
    # model cannot miss it. The CLI bootstrap arg stays for
    # claude/codex (may work there) and as a debugging breadcrumb;
    # for opencode it's dead-letter but harmless.
    try:
        from .lane_mailbox_store import LaneMailboxStore

        prior_worker_id = str(review_row.get("worker_id") or "").strip()
        if prior_worker_id:
            session_id = str(review_row.get("session_id") or "").strip()
            LaneMailboxStore().put(
                project_root,
                worker_id=prior_worker_id,
                session_id=session_id,
                prompt=bootstrap,
                author_session_id=session_id,
            )
    except Exception:
        # Mailbox write best-effort. If it fails, the resume still
        # fires; the worker won't receive the directive but the
        # operator can intervene manually.
        pass

    try:
        # Detached spawn — fire-and-forget. The resumed process is
        # owned by the host, not by this MCP server. We don't wait.
        # Bootstrap is passed as a positional/print arg in cli_args
        # (per backend-specific non-interactive form); stdin is
        # closed immediately so the host doesn't hang waiting for
        # interactive input on a misconfigured invocation.
        # Mailbox (above) is the canonical delivery for opencode.
        # #345: routed through audited_popen — resumed lane agents are spawns
        # an operator absolutely wants visible in the ledger. Passthrough
        # lambda IS the registered AST callsite; kwargs pass through UNCHANGED.
        from .shell_egress_service import audited_popen

        audited_popen(
            cli_args,
            fingerprint=("lane_resume_dispatcher.py", "resume_worker_on_deny", "subprocess.Popen"),
            reason="lane-resume-dispatch",
            session_id=str(review_row.get("session_id") or "") or None,
            popen=lambda *a, **kw: subprocess.Popen(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=spawn_env,
            creationflags=_WIN_NO_WINDOW,
        )
    except Exception as exc:
        return {
            "dispatched": False,
            "backend": backend,
            "host_session_id": host_session_id,
            "error": f"subprocess spawn failed: {exc!r}",
        }
    return {
        "dispatched": True,
        "backend": backend,
        "host_session_id": host_session_id,
        "lane_id": lane_id,
        "review_id": review_id,
        "cli_args": cli_args,
    }
