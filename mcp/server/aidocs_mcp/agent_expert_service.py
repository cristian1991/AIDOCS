"""Agent worker service — spawns and manages worker agents for conductor lanes.

Supports multiple agent backends:
- Claude SDK via `claude -p` (subscription billing)
- OpenAI Agents SDK via Python (subscription or API key)

The conductor calls spawn_worker() with a task packet. The worker runs
the task and returns structured output for the conductor to ingest.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .session_lane_agents_store import SessionLaneAgentsStore

logger = logging.getLogger(__name__)


# Trimmed-eager: tools a lane realistically needs. Excludes conductor-
# control (plan_conductor_*, conductor_*), spawn (agent_spawn_*), mode
# switches, and registry mutations. Conductor can override per-lane via
# the dispatch packet's `lane_allowed_tools` field.
_DEFAULT_LANE_CLI_TOOLS: tuple[str, ...] = (
    # Lane boot — MANDATORY first tool call (paved-road, 2026-05-02).
    # session_connect detects AIDOCS_EXPERT_ID + AIDOCS_EXPERT_LANE_ID
    # env vars (spawner-set, can't be forged from inside the subprocess),
    # verifies the spawn registry row, latches sub_agent flag ON one-way,
    # AND returns the lane plan body in the same response. Single tool,
    # single trip, bound + briefed. Replaces the prior two-step
    # lane_worker_bind / get_lane_plan dance.
    "ai_session",
    "ai_task",
    "ai_plan_report",
    "ai_find",
    "ai_investigate",
    "ai_trace",
    "ai_bundle",
    "ai_text_search",
    "ai_search",
    "schema_query",
    "ai_get_lines",
    "ai_get_symbol_snippet",
    "ai_get_symbol_info",
    # ai_replace is the unified entry; standalone tools stay during
    # transition. King doctrine 2026-05-01.
    "ai_replace",
    "ai_anchor_replace",
    "ai_str_replace",
    "ai_edit_lines",
    "ai_insert_lines",
    "ai_batch_edit",
    "ai_create_file",
    # ai_test is the SUBAGENT-SAFE test runner (language-agnostic; argv-form,
    # shell=False). Lane workers verify their work through it. Raw ai_run is
    # DELIBERATELY NOT granted to subagents (2026-06-13): a worker with raw
    # shell can write to / evade gate code (mcp/server/aidocs_mcp/ is
    # SELF_MOD_GATE_CODE-protected) — observed repeatedly. ai_run stays a
    # CONDUCTOR tool; a conductor may grant it to a specific lane via
    # lane_allowed_tools when a worker genuinely needs raw shell.
    "ai_test",
    "memory_read",
    "memory_search",
    "memory_capture",
    "verification_gate",
    "index_status",
    "ai_index_status",
    # Raw Bash is tier-0 blocked on managed projects, and ai_run is withheld
    # from subagents (above). Workers run tests via ai_test; for anything
    # else they ask the conductor (who can grant ai_run per-lane).
)


def _build_cli_allowed_tools(packet: dict[str, object]) -> list[str]:
    """Compute the --allowedTools list for a lane worker.

    Precedence: packet.lane_allowed_tools (conductor override) →
    _DEFAULT_LANE_CLI_TOOLS (trimmed-eager fallback). Every entry is
    emitted as `mcp__aidocs__<name>` since the CLI matches full tool
    names, not bare ones. Glob entries in lane_allowed_tools
    (e.g. `code_*`) pass through verbatim — callers keep control of
    how loose the match is.

    Host-native tools added unconditionally:
      - ToolSearch: lets the worker load deferred MCP schemas.
      - ScheduleWakeup: enables the park-and-wake mailbox pattern
        (worker can self-park after task_update → wait for conductor
        prompt via lane_send_prompt).
    """
    raw = packet.get("lane_allowed_tools")
    names: tuple[str, ...]
    if isinstance(raw, (list, tuple)) and raw:
        names = tuple(str(n) for n in raw if n)
    else:
        names = _DEFAULT_LANE_CLI_TOOLS
    out: list[str] = []
    for name in names:
        if name.startswith("mcp__") or "*" in name:
            out.append(name)
        else:
            out.append(f"mcp__aidocs__{name}")
    # Host-native additions (not AIDOCS-prefixed).
    # ScheduleWakeup intentionally omitted per king's doctrine 2026-05-01
    # — foreign anatomy; Experts are bound to their process and exit when
    # the work returns. Park-and-wake is conductor/operator territory.
    out.append("ToolSearch")
    return out


_OPENCODE_FORWARD_ENV_KEYS: tuple[str, ...] = (
    # AIDOCS_PROJECT_ROOT MUST be forwarded explicitly (Phoenix
    # 2026-05-12, dental bug report). Opencode propagates parent
    # shell env to MCP children; if the operator's shell exports
    # AIDOCS_PROJECT_ROOT pointing at an AIDOCS install, that value
    # leaks into the worker's MCP child and overrides the conductor's
    # project. Explicit forward from worker_env (which inherited
    # AIDOCS_PROJECT_ROOT from the conductor's .mcp.json) wins.
    "AIDOCS_PROJECT_ROOT",
    "AIDOCS_EXPERT_ID",
    "AIDOCS_EXPERT_LANE_ID",
    "AIDOCS_EXPERT_SESSION_ID",
    "AIDOCS_EXPERT_LANE_ALLOWED",
    "AIDOCS_EXPERT_LANE_FILES",
    "AIDOCS_GLOBAL_CONFIG_DB",
    "AIDOCS_EMPIRE_DB",
    "AIDOCS_TEST_GLOBAL_CONFIG_DIR",
)


def _write_opencode_worker_config(
    project_root: Path,
    worker_env: dict[str, str],
) -> Path | None:
    """Write a per-spawn opencode config that forwards AIDOCS_EXPERT_*
    env to the child AIDOCS MCP process.

    #163 (Phoenix 2026-05-10): opencode does NOT pass parent process
    env to its child MCP servers. Without this, the AIDOCS MCP child
    spawned by opencode sees no AIDOCS_EXPERT_ID env, takes the
    CONDUCTOR branch in session_connect, and returns plans-list-shelf
    instead of the lane plan. Worker treats response as session
    bootstrap, never calls task_begin / edits.

    The config emits an `mcp.aidocs.environment` block with the
    forward-list keys interpolated to their current values. Opencode
    merges multiple configs (per docs/config/), so this per-spawn
    file is layered on top of the project's opencode.jsonc — only
    the MCP environment is overridden, the rest (instructions,
    models, etc.) still loads from the project config.

    Pointed at via OPENCODE_CONFIG env so opencode auto-reads it.
    Caller cleans the file in finally.

    Returns the path on success, None on write failure (best-effort —
    a config-write failure shouldn't block the spawn; the worker
    will hit the same idle-on-conductor-branch shape and the next
    layer surfaces it).
    """
    import json as _json
    import os as _os
    import tempfile as _tempfile

    try:
        tmp_dir = project_root / ".MEMORY" / ".index" / "opencode_worker_config"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, path_str = _tempfile.mkstemp(
            prefix="oc-worker-config-",
            suffix=".jsonc",
            dir=str(tmp_dir),
        )
        _os.close(fd)
        path = Path(path_str)
        env_payload: dict[str, str] = {}
        for key in _OPENCODE_FORWARD_ENV_KEYS:
            val = worker_env.get(key)
            if val is not None and str(val) != "":
                env_payload[key] = str(val)
        # The AIDOCS MCP entry is named "aidocs" in the project's
        # opencode.jsonc. Opencode 1.4 does NOT deep-merge nested
        # mcp.<name>.* keys — listing only `environment` here yields
        # "Missing key mcp.aidocs.enabled" (witnessed live 2026-05-10
        # smoke test r_632cc7b7d788). Repeat the full MCP entry so
        # opencode has type/command/enabled in this layer too; the
        # `environment` block is the actual override the spawn cares
        # about. Mirror the project opencode.jsonc shape.
        # Phoenix 2026-05-12 (#168 root fix): include the "plugin"
        # array so opencode actually loads the aidocs plugin in
        # spawn-mode workers. Without this, opencode 1.14.41 logs
        # "loading plugin" from ~/.config/opencode/plugins/aidocs.js
        # but never evaluates the module body — chat.message hook
        # is dead, host_session_id never stamps via plugin, and
        # task_complete captures empty review rows. Per opencode
        # schema (https://opencode.ai/config.json), the field is
        # `plugin` (singular), array of identifiers/paths.
        _user_plugin_path = "file:///C:/Users/User/.config/opencode/plugins/aidocs.js"
        config: dict[str, object] = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "aidocs": {
                    "type": "local",
                    "enabled": True,
                    "timeout": 120000,
                    "command": [
                        "python",
                        "-m",
                        "aidocs_mcp.mcp_server",
                    ],
                    "environment": env_payload,
                },
            },
            "plugin": [_user_plugin_path],
        }
        path.write_text(
            _json.dumps(config, indent=2),
            encoding="utf-8",
        )
        return path
    except Exception:
        return None


_CLAUDE_FORWARD_ENV_KEYS: tuple[str, ...] = (
    # Same set the opencode forward list uses, for the same reason:
    # claude does NOT propagate parent process env to its MCP children.
    # Only the keys explicitly named in .mcp.json's `env` block reach
    # the MCP child. The project's static .mcp.json carries only
    # AIDOCS_PROJECT_ROOT; lane-worker identity (AIDOCS_EXPERT_*) was
    # missing, so session_connect fell into the CONDUCTOR branch and
    # the worker idled as if it were a fresh conductor (dental bug
    # report 2026-05-12, claude case B).
    "AIDOCS_PROJECT_ROOT",
    "AIDOCS_EXPERT_ID",
    "AIDOCS_EXPERT_LANE_ID",
    "AIDOCS_EXPERT_SESSION_ID",
    "AIDOCS_EXPERT_LANE_ALLOWED",
    "AIDOCS_EXPERT_LANE_FILES",
    "AIDOCS_GLOBAL_CONFIG_DB",
    "AIDOCS_EMPIRE_DB",
    "AIDOCS_TEST_GLOBAL_CONFIG_DIR",
)


def _write_claude_worker_mcp_config(
    project_root: Path,
    worker_env: dict[str, str],
) -> Path | None:
    """Write a per-spawn .mcp.json that carries the worker's identity
    env vars into the AIDOCS MCP child's env block.

    Symmetric to `_write_opencode_worker_config`. The project's static
    .mcp.json only declares AIDOCS_PROJECT_ROOT in its env block;
    spawned-worker identity (AIDOCS_EXPERT_*) was invisible to the
    MCP child, so session_connect fell into the CONDUCTOR branch
    instead of LANE WORKER. This helper copies the project's .mcp.json
    and augments the aidocs server entry's env block with the worker's
    identity, returning a temp path for `--mcp-config`.

    Best-effort: returns None on failure (caller falls back to the
    project's static .mcp.json — same failure mode as before this fix).
    """
    import json as _json
    import os as _os
    import tempfile as _tempfile

    try:
        src = project_root / ".mcp.json"
        if not src.is_file():
            return None
        config = _json.loads(src.read_text(encoding="utf-8"))
        servers = config.get("mcpServers")
        if not isinstance(servers, dict):
            return None
        aidocs_entry = servers.get("aidocs")
        if not isinstance(aidocs_entry, dict):
            return None
        env_block = aidocs_entry.get("env")
        if not isinstance(env_block, dict):
            env_block = {}
            aidocs_entry["env"] = env_block
        for key in _CLAUDE_FORWARD_ENV_KEYS:
            val = worker_env.get(key)
            if val is not None and str(val) != "":
                env_block[key] = str(val)
        tmp_dir = project_root / ".MEMORY" / ".index" / "claude_worker_config"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, path_str = _tempfile.mkstemp(
            prefix="claude-worker-mcp-",
            suffix=".json",
            dir=str(tmp_dir),
        )
        _os.close(fd)
        path = Path(path_str)
        path.write_text(_json.dumps(config, indent=2), encoding="utf-8")
        return path
    except Exception:
        return None


def _write_worker_settings_file(
    project_root: Path,
    allowed_tools: list[str],
    disallowed_tools: list[str],
) -> Path:
    """Write a per-spawn Claude CLI settings file with pre-approvals.

    The CLI's `-p` headless mode ignores --dangerously-skip-permissions
    for MCP tools unless the tool is pre-approved in a settings.json
    (anthropics/claude-code #581, #28580, #13077). This writer emits a
    throwaway settings file naming every tool the worker should be able
    to call (mirrors --allowedTools) plus explicit denials that mirror
    --disallowedTools — belt-and-suspenders against the CLI's
    inconsistent flag handling.

    Returns the path; caller is responsible for cleanup.
    """
    import json as _json
    import tempfile as _tempfile

    tmp_dir = project_root / ".MEMORY" / ".index" / "worker_settings"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, path_str = _tempfile.mkstemp(
        prefix="worker-settings-",
        suffix=".json",
        dir=str(tmp_dir),
    )
    import os as _os

    _os.close(fd)
    path = Path(path_str)
    settings: dict[str, object] = {
        "permissions": {
            "allow": list(allowed_tools),
            "deny": list(disallowed_tools),
        },
    }
    path.write_text(_json.dumps(settings, indent=2), encoding="utf-8")
    return path


@dataclass
class BackgroundJob:
    """A detached worker spawn tracked by worker_id.

    Runs subprocess.run in a daemon thread so the MCP event loop stays
    responsive while the sub-CLI (claude/codex) is executing. The job
    holds the eventual WorkerResult once the thread completes.
    """

    worker_id: str
    lane_id: str
    backend: str
    started_at: float
    thread: threading.Thread
    result: WorkerResult | None = None
    done: bool = False
    error: str | None = None

    def status(self, *, verbose: bool = False) -> dict[str, object]:
        # Report-cannot-lie (120% §968): never claim "running" without a live
        # managing thread. `self.done` is a flag the worker thread flips on
        # completion — if the thread DIED without flipping it (self-dispose /
        # crash / unhandled exception), the worker is finished, not running.
        # `thread.is_alive()` is the liveness ground truth.
        if self.done:
            failed = bool(self.error) or (self.result is not None and not self.result.success)
            state = "failed" if failed else "done"
        elif not self.thread.is_alive():
            state = "stale_unknown"
        else:
            state = "running"
        payload: dict[str, object] = {
            "worker_id": self.worker_id,
            "lane_id": self.lane_id,
            "backend": self.backend,
            "state": state,
            "terminal": state != "running",
            "elapsed_seconds": round(time.monotonic() - self.started_at, 2),
        }
        if self.done:
            if self.result is not None:
                payload["result"] = self.result.to_dict() if verbose else self.result.to_summary()
            else:
                payload["result"] = None
            if self.error:
                payload["error"] = self.error
        elif state == "stale_unknown":
            payload["error"] = (
                "managing thread is dead but completion was never recorded — "
                "worker self-disposed or crashed; treat as terminal (resume/kill by lane)"
            )
        return payload


@dataclass
class WorkerResult:
    """Structured result from a worker agent."""

    lane_id: str
    success: bool
    files_changed: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    command_results: list[dict[str, object]] = field(default_factory=list)
    verification_results: dict[str, object] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    hidden_dependencies: list[dict[str, str]] = field(default_factory=list)
    claimed_done: bool = False
    raw_output: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "lane_id": self.lane_id,
            "success": self.success,
            "files_changed": self.files_changed,
            "commands_run": self.commands_run,
            "command_results": self.command_results,
            "verification_results": self.verification_results,
            "blockers": self.blockers,
            "hidden_dependencies": self.hidden_dependencies,
            "claimed_done": self.claimed_done,
            "error": self.error,
            "raw_output": self.raw_output,
        }

    def to_summary(self) -> dict[str, object]:
        """Slim status payload — drops raw_output, keeps the short lists.

        Use verbose=True on ai_status to get the full to_dict().
        blockers stay as a full list (never large) because callers need to
        render each one, not just a count. raw_output is the real byte
        hog and stays omitted from the slim view.
        """
        return {
            "lane_id": self.lane_id,
            "success": self.success,
            "claimed_done": self.claimed_done,
            "files_changed_count": len(self.files_changed),
            "commands_run_count": len(self.commands_run),
            "blockers": list(self.blockers),
            "error": self.error,
        }


_RESULT_MARKER = "AIDOCS_EXPERT_RESULT"
# Fenced-block marker used by workers to wrap the final structured result.
# Picking a distinctive, self-describing token avoids colliding with the
# prompt's own example JSON or any ad-hoc `{...}` in worker prose.


def _build_worker_prompt(packet: dict[str, object]) -> str:
    """Build the spawn prompt for a lane worker.

    Contract (Phoenix amendment 2026-05-08): the prior contract sent
    the literal one-word string "session_connect" assuming the model
    would parse it as a tool-call directive. Opus 4.7 (and likely
    other strong models) parsed it as a noun and idled with
    "Acknowledged. Awaiting your task." instead. The minimum signal
    the model needs to act is now an explicit imperative: "Call the
    mcp__aidocs__ai_session(mode='connect') tool now to receive your lane plan
    and instructions." Still injection-scanner-friendly (no
    multi-step instructions, no doctrine leak); just a single
    unambiguous tool-call directive.

    Original contract (2026-05-02, kept for context): worker calls
    session_connect; tool detects AIDOCS_EXPERT_ID +
    AIDOCS_EXPERT_LANE_ID env vars and returns the lane plan body.
    Single tool, single trip, bound + briefed.

    The full protocol, result-block shape, tool allowlist, and
    verification contract live in the plan the worker receives in
    session_connect's response. The prompt itself
    contains ZERO instructions the prompt-injection scanner could
    flag, because none of the instructions are IN the prompt.

    Previous multi-step prompts ("1. call X  2. call Y ...") tripped
    the injection scanner when the worker's later MCP reads surfaced
    their own prompt text through SESSION.md / plan reads. Keeping
    the spawn prompt to just the tool name is the minimum signal the
    model needs to act: "call get_lane_plan". Everything else is on
    disk and goes through indexed reads (which are trusted).
    """
    # Unused — retained for API compatibility with call sites that
    # still pass a packet expected to parametrize the prompt.
    del packet
    # Post-2026-05-12 mode-collapse: ai_session(mode='connect') replaced
    # the standalone session_connect tool. The wrapping prompt at the
    # spawn sites (spawn_worker_opencode / spawn_worker_claude) tells the
    # worker to call ai_session with mode='connect'.
    return "ai_session"


def _extract_worker_result_block(raw: str) -> str | None:
    """Pull the JSON payload out of the AIDOCS_EXPERT_RESULT fenced block."""
    open_token = f"```{_RESULT_MARKER}"
    idx = raw.rfind(open_token)
    if idx < 0:
        return None
    after_open = raw.find("\n", idx)
    if after_open < 0:
        return None
    close_idx = raw.find("```", after_open + 1)
    if close_idx < 0:
        return None
    return raw[after_open + 1 : close_idx].strip()


def _collect_worker_result_from_db(
    project_root: Path,
    session_id: str,
    lane_id: str,
    worker_registry_id: str,
    raw: str,
) -> WorkerResult | None:
    """Sqlite-first WorkerResult collector (2026-04-24).

    The worker already writes structured state through MCP tool calls
    (task_begin, task_update, ai_run, task_complete, plan_dispatch_report).
    Every fact the legacy AIDOCS_EXPERT_RESULT free-text block tried
    to capture is already in sqlite. Reading from there gives us the
    authoritative, unfakeable answer: did the worker call task_complete?
    what ai_run commands did it execute? what events flagged?

    Returns None when the worker made no meaningful MCP progress —
    caller falls back to the legacy sentinel-parse for backward
    compatibility with claude workers that still emit the block.

    started_at is derived from session_lane_agents (authoritative
    source of the worker's spawn timestamp). If the registry row
    is missing we cannot bound the event query and abstain, letting
    the caller try the legacy sentinel parse.
    """
    import sqlite3 as _sqlite

    from .execution_index_store import ExecutionIndexStore

    store = ExecutionIndexStore()
    store.init_db(project_root)
    started_at_iso = ""
    try:
        with _sqlite.connect(str(store.db_path(project_root))) as conn:
            conn.row_factory = _sqlite.Row
            if worker_registry_id:
                reg = conn.execute(
                    "SELECT started_at FROM session_lane_agents WHERE worker_id = ?",
                    (worker_registry_id,),
                ).fetchone()
                if reg is not None:
                    started_at_iso = str(reg["started_at"] or "")
            if not started_at_iso:
                return None
            rows = conn.execute(
                "SELECT observed_at, capability_name, action_kind, "
                "status, payload_json "
                "FROM execution_events "
                "WHERE session_id = ? "
                "  AND principal_type = 'subagent' "
                "  AND observed_at >= ? "
                "ORDER BY observed_at ASC, event_id ASC",
                (session_id, started_at_iso),
            ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    completed = any(
        r["capability_name"] == "task_complete" and r["status"] == "completed" for r in rows
    )
    commands_run: list[str] = []
    command_results: list[dict[str, object]] = []
    for r in rows:
        if r["capability_name"] != "ai_run" or r["status"] != "completed":
            continue
        try:
            p = json.loads(r["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        args_preview = p.get("args_preview") or ""
        if isinstance(args_preview, str) and args_preview:
            commands_run.append(args_preview[:300])
        # Best-effort exit-code extraction from payload result preview.
        exit_code = p.get("exit_code")
        if isinstance(exit_code, int):
            command_results.append({"exit_code": exit_code})
    blockers: list[str] = []
    for r in rows:
        if r["status"] == "blocked" or r["action_kind"] == "security":
            payload = r["payload_json"] or ""
            if payload and len(blockers) < 5:
                blockers.append(f"{r['capability_name']}:{r['status']} {payload[:200]}")
    # Worker succeeded if it reached task_complete AND has no hard blocks.
    # No blockers entry for security-flagged reads (those are warnings).
    hard_blockers = [b for b in blockers if ":blocked" in b]
    result = WorkerResult(
        lane_id=lane_id,
        success=completed and not hard_blockers,
        files_changed=[],  # TODO: derive from edit_history for this task_id
        commands_run=commands_run,
        command_results=command_results,
        verification_results={"mcp_tool_events": len(rows)},
        blockers=hard_blockers,
        claimed_done=completed,
        raw_output=raw[-4000:] if raw else "",
    )
    if not completed:
        result.error = f"Worker produced {len(rows)} MCP events but never called task_complete."
    return result


def _parse_worker_output(
    raw: str,
    lane_id: str,
    *,
    project_root: Path | None = None,
    session_id: str = "",
    worker_registry_id: str = "",
) -> WorkerResult:
    """Build a WorkerResult from the worker's stdout + sqlite state.

    Sqlite-first (2026-04-24): when project_root + session_id +
    worker_registry_id are supplied, _collect_worker_result_from_db
    runs first and wins if the worker made any MCP progress. Falls
    back to the legacy free-text sentinel-parse for workers that
    predate the protocol change (claude workers still emit
    AIDOCS_EXPERT_RESULT via the plan's prompt).
    """
    if project_root is not None and session_id and worker_registry_id:
        db_result = _collect_worker_result_from_db(
            project_root,
            session_id,
            lane_id,
            worker_registry_id,
            raw,
        )
        if db_result is not None:
            return db_result
    # Legacy path — sentinel-scan stdout.
    result = WorkerResult(lane_id=lane_id, success=False, raw_output=raw[-4000:])
    block = _extract_worker_result_block(raw)
    if block is None:
        result.error = f"Worker did not emit a `{_RESULT_MARKER}` result block."
        return result
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        result.error = f"Worker result block was not valid JSON: {exc}"
        return result
    if not isinstance(data, dict):
        result.error = "Worker result block was not a JSON object."
        return result
    result.files_changed = list(data.get("files_changed", []) or [])
    result.commands_run = list(data.get("commands_run", []) or [])
    result.command_results = list(data.get("command_results", []) or [])
    result.verification_results = dict(data.get("verification_results", {}) or {})
    result.blockers = list(data.get("blockers", []) or [])
    result.hidden_dependencies = list(data.get("hidden_dependencies", []) or [])
    result.claimed_done = bool(data.get("claimed_done", False))
    result.success = not result.blockers and not data.get("error")
    return result


class InteractiveWorker:
    """A running agent process with interactive control (Full mode only).

    Holds the process handle so the conductor can:
    - Send pause/resume messages via stdin
    - Read incremental output via stdout
    - Monitor token usage via GetContextUsage
    - Gracefully stop with partial result preservation
    """

    def __init__(self, lane_id: str, process: subprocess.Popen, backend: str) -> None:
        self.lane_id = lane_id
        self.process = process
        self.backend = backend
        self.output_buffer: list[str] = []
        self.paused = False

    def send_message(self, content: str) -> None:
        """Send a message to the agent via stdin."""
        if self.process.stdin and self.process.poll() is None:
            try:
                msg = json.dumps({"type": "user_message", "content": content}) + "\n"
                self.process.stdin.write(msg)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def pause(self, reason: str = "") -> None:
        """Signal the agent to pause — save progress and output partial result."""
        self.paused = True
        self.send_message(
            f"CONDUCTOR PAUSE: Stop current work immediately. Save all progress. "
            f"Output a partial result JSON with what you've done so far. "
            f"Reason: {reason or 'conductor intervention'}",
        )

    def resume(self, instructions: str = "") -> None:
        """Signal the agent to resume work."""
        self.paused = False
        self.send_message(f"CONDUCTOR RESUME: Continue your task. {instructions}")

    def read_output(self) -> str:
        """Read available output without blocking."""
        if self.process.stdout and self.process.poll() is not None:
            remaining = self.process.stdout.read()
            if remaining:
                self.output_buffer.append(remaining)
        return "".join(self.output_buffer)

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def wait(self, timeout: int = 60) -> WorkerResult:
        """Wait for the agent to finish and return the result."""
        try:
            stdout, stderr = self.process.communicate(timeout=timeout)
            if stdout:
                self.output_buffer.append(stdout)
        except subprocess.TimeoutExpired:
            return WorkerResult(
                lane_id=self.lane_id,
                success=False,
                error=f"Worker timed out after {timeout}s",
                raw_output="".join(self.output_buffer)[-2000:],
            )
        full_output = "".join(self.output_buffer)
        if self.process.returncode != 0:
            return WorkerResult(
                lane_id=self.lane_id,
                success=False,
                error=f"Worker exited with code {self.process.returncode}",
                raw_output=full_output[-2000:],
            )
        return _parse_worker_output(full_output, self.lane_id)

    def terminate(self) -> None:
        """Force-terminate the agent process (last resort)."""
        if self.is_alive():
            self.process.terminate()


class AgentExpertService:
    """Manages worker agent lifecycle for conductor lanes."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._active_workers: dict[str, InteractiveWorker] = {}  # lane_id → worker
        self._jobs: dict[str, BackgroundJob] = {}  # worker_id → background job
        self._jobs_lock = threading.Lock()
        self._lane_agents_store = SessionLaneAgentsStore()

    def _register_lane_worker(
        self,
        project_root: Path,
        packet: dict[str, object],
        backend: str,
    ) -> str | None:
        """Register a lane worker row and return its AIDOCS_EXPERT_ID.

        Returns None when the packet lacks session/lane context or the
        registration backend raises — callers still spawn, but without a
        worker_id the reaper can't track the process.
        """
        session_id = str(packet.get("session_id", "") or "")
        lane_id = str(packet.get("lane_id", "") or "")
        if not session_id or not lane_id:
            return None
        allowed_raw = packet.get("allowed_files") or []
        allowed_list: list[str] = []
        if isinstance(allowed_raw, (list, tuple)):
            allowed_list = [str(p) for p in allowed_raw if str(p).strip()]
        try:
            return self._lane_agents_store.register_worker(
                project_root=project_root,
                session_id=session_id,
                lane_id=lane_id,
                backend=backend,
                allowed_files=allowed_list,
            )
        except Exception:
            logger.exception(
                "Failed to register lane worker session=%s lane=%s backend=%s",
                session_id,
                lane_id,
                backend,
            )
            return None

    @property
    def hub(self) -> Any:
        return self.runtime.hub

    def available_backends(self) -> list[dict[str, str]]:
        """List available agent backends."""
        backends = []
        if shutil.which("claude"):
            backends.append({"id": "claude", "name": "Claude SDK", "billing": "subscription"})
        if shutil.which("codex"):
            backends.append({"id": "codex", "name": "OpenAI Codex", "billing": "subscription"})
        if shutil.which("opencode"):
            backends.append({"id": "opencode", "name": "OpenCode", "billing": "subscription"})
        return backends

    def spawn_worker_claude(
        self,
        project_root: Path,
        packet: dict[str, object],
        *,
        timeout: int = 300,
    ) -> WorkerResult:
        """Spawn a Claude worker via `claude -p` for a conductor lane."""
        lane_id = str(packet.get("lane_id", "unknown"))
        # See spawn_worker_opencode for the spawn-site wrapper rationale.
        # Same fix: keep _build_worker_prompt's one-word contract for
        # tests; wrap with an imperative here so claude -p actually
        # acts on it. Witnessed 2026-05-10 against opencode; same
        # regression shape applies to claude headless.
        kicker = _build_worker_prompt(packet)
        prompt = (
            f"Call the mcp__aidocs__{kicker} tool with mode='connect' now "
            f"to receive your lane plan and instructions, then act on "
            f"the plan body in its response."
        )

        claude_path = shutil.which("claude")
        if not claude_path:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error="Claude CLI not found. Install Claude Code CLI.",
            )

        try:
            # stdin=DEVNULL so the CLI knows there's no interactive input
            # and exits after producing output. Without this the CLI sits
            # open on its stdin waiting for EOF from a non-existent human,
            # hanging the whole spawn until the timeout kills it.
            # Lane sub-agent tool policy: auto-approve only aidocs MCP
            # tools, hard-deny every raw tool. The AIDOCS hook already
            # denies raw file tools at a second layer — --disallowedTools
            # just prevents the sub-CLI from wasting a turn trying. This
            # is the minimum configuration that makes the sub-CLI
            # functional: stdin=DEVNULL means any permission prompt
            # auto-denies, so every tool the sub-CLI needs MUST be in
            # --allowedTools to bypass the approval UI.
            # Raw host tools + bootstrap/routing MCP tools the worker
            # must never reach for. Bootstrap tools (orchestrate,
            # handle_prompt, route_prompt, project_bootstrap_or_resume,
            # runtime_preflight) re-activate managed mode and dump
            # 5-100kb routing payloads meant for human
            # /aidocs entry; workers already have session+lane bound
            # via spawn env and a task brief. Conductor-control tools
            # (ai_lane_control, ai_plan_*, ai_resolve_*, ai_guidance,
            # ai_review, ai_lane_grant, ai_spawn, mode_*, session
            # claim/release) would let a worker steer its own lane or
            # siblings, defeating lane isolation.
            # Bash is explicitly NOT in --disallowedTools — workers need
            # it for test verification (pytest, npm test, cargo test).
            # bash_policy (allow/deny tables) + test-retry tier-0 +
            # foreground-long-running cap are the real gates; the CLI
            # layer would double-block with no gain.
            disallowed = [
                "Edit",
                "Write",
                "Read",
                "Glob",
                "Grep",
                "MultiEdit",
                "NotebookEdit",
                "Task",
                "Patch",
                "mcp__aidocs__orchestrate",
                "mcp__aidocs__handle_prompt",
                "mcp__aidocs__route_prompt",
                "mcp__aidocs__classify_prompt",
                "mcp__aidocs__project_bootstrap_or_resume",
                "mcp__aidocs__runtime_preflight",
                "mcp__aidocs__session_create",
                "mcp__aidocs__session_claim",
                "mcp__aidocs__session_release",
                "mcp__aidocs__mode_set",
                "mcp__aidocs__mode_clear",
                "mcp__aidocs__mode_get",
                "mcp__aidocs__ai_spawn",
                "mcp__aidocs__ai_status",
                "mcp__aidocs__ai_jobs",
                # Conductor-control surface (renamed 2026-05-12, doctrine
                # ai_<verb>(mode)). Workers must not steer their own lane
                # or siblings. ai_plan_expand and ai_plan_signal are
                # INTENTIONALLY omitted — they are in the worker lane
                # scope (see access_gate._worker_lane_scope) for legitimate
                # mid-flight scope-extension / signal-emission.
                # ai_seat is the unified conductor seat tool (2026-05-12
                # collapse: replaces conductor_mode_enter / exit /
                # status / overview). Workers must NOT call ai_seat —
                # mode='enter' would let them self-elevate to conductor.
                # Banned at the CLI layer here (belt-and-suspenders;
                # the lane gate also blocks at the MCP layer).
                "mcp__aidocs__ai_seat",
                "mcp__aidocs__ai_lane_control",
                "mcp__aidocs__ai_lane_grant",
                "mcp__aidocs__ai_resolve_scope",
                "mcp__aidocs__ai_resolve_backend",
                "mcp__aidocs__ai_guidance",
                "mcp__aidocs__ai_review",
                "mcp__aidocs__ai_plan_status",
                "mcp__aidocs__ai_plan_graph",
                "mcp__aidocs__ai_plan_pause",
                "mcp__aidocs__ai_plan_resume",
                "mcp__aidocs__ai_plan_reopen",
                "mcp__aidocs__ai_plan_mark_ready",
                "mcp__aidocs__ai_plan_overlap",
                "mcp__aidocs__ai_plan_dispatch",
                "mcp__aidocs__ai_plan_create",
                "mcp__aidocs__ai_plan_template",
            ]
            # Pass lane identity in the env so the worker's MCP server
            # can read it WITHOUT writing to session_query_gate. Writing
            # the worker's lane_id into the shared session row was the
            # root cause of the conductor-demotion bug: the conductor
            # reads the same row and would see its own current_lane_id
            # flipped to the worker's lane on the next PreToolUse. Env-
            # scoped identity keeps worker state per-process instead of
            # per-session.
            import json as _json_env
            import os as _os_env

            worker_env = dict(_os_env.environ)
            worker_env["AIDOCS_EXPERT_LANE_ID"] = lane_id
            worker_env["AIDOCS_EXPERT_SESSION_ID"] = str(packet.get("session_id", ""))
            # Lane tool scope — see spawn_worker_opencode for rationale.
            # claude already has CLI-side --allowedTools so this is
            # belt-and-suspenders, but keeps both backends symmetric
            # and cuts MCP tool-registration work at server init.
            _lane_tools_raw = packet.get("lane_allowed_tools")
            if isinstance(_lane_tools_raw, (list, tuple)) and _lane_tools_raw:
                _lane_tools = [str(n) for n in _lane_tools_raw if n]
            else:
                _lane_tools = list(_DEFAULT_LANE_CLI_TOOLS)
            worker_env["AIDOCS_EXPERT_LANE_ALLOWED"] = _json_env.dumps(_lane_tools)
            # Register the worker row BEFORE spawn so AIDOCS_EXPERT_ID
            # exists in the subprocess env from the first tool call.
            # The reaper + dashboard key off session_lane_agents.worker_id;
            # without the env var the sub-CLI has no stable handle to
            # report lifecycle transitions back to the shared registry.
            worker_registry_id = self._register_lane_worker(
                project_root,
                packet,
                backend="claude",
            )
            if worker_registry_id:
                worker_env["AIDOCS_EXPERT_ID"] = worker_registry_id
            # lane_exact_paths follows the same env-override pattern as
            # current_lane_id: the session_query_gate row is shared
            # across parallel dispatches, so the last dispatch wins and
            # earlier lanes see the wrong files. Pass the packet's
            # allowed_files list in env so the worker's query_gate.get()
            # can return ITS own lane scope even if three siblings
            # clobbered the shared row.
            lane_files_raw = packet.get("allowed_files") or []
            if isinstance(lane_files_raw, (list, tuple)):
                worker_env["AIDOCS_EXPERT_LANE_FILES"] = _json_env.dumps(
                    [str(p) for p in lane_files_raw if str(p).strip()],
                )
            # Strip VSCode-context vars so the spawned claude.exe doesn't
            # inherit "I'm running inside the extension" state from our
            # parent process. When CLAUDE_CODE_ENTRYPOINT=claude-vscode
            # and CLAUDECODE=1 are present, claude.exe forces
            # permissionMode=default and ignores --dangerously-skip-
            # permissions / --permission-mode. Stripping these lets the
            # CLI treat the spawn as a normal headless -p invocation.
            # (Diagnosed 2026-04-19 from worker jsonl entrypoint field.)
            for _vsc_var in (
                "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDECODE",
                "CLAUDE_VSCODE_IPC_SOCK",
                "VSCODE_INJECTION",
                "VSCODE_PID",
                "VSCODE_IPC_HOOK",
                "VSCODE_IPC_HOOK_CLI",
                "TERM_PROGRAM",
            ):
                worker_env.pop(_vsc_var, None)
            # --dangerously-skip-permissions is required because the
            # softer --permission-mode bypassPermissions still defers to
            # the project's .claude/settings.local.json allow list, which
            # is narrow and leaves the worker toolless on every AIDOCS MCP
            # call. The AIDOCS server-side gate (access_gate.check_tool
            # and lane_tool_block) is the real defense-in-depth; the CLI
            # flag only skips CLI-side prompts. --disallowedTools still
            # blocks raw file/shell tools at the CLI layer.
            # (lane-b-test 2026-04-19: first two spawns failed toolless.)
            cli_allowed = _build_cli_allowed_tools(packet)
            # Known CLI bugs (anthropics/claude-code #581, #13077, #28580,
            # #12863) break every softer bypass under -p headless mode:
            # --allowedTools wildcards silently fail to match MCP tool
            # names, --permission-mode acceptEdits/bypassPermissions
            # still prompts per-call, and --disallowedTools doesn't
            # reach MCP tools. Only --dangerously-skip-permissions is
            # actually honored. AIDOCS access_gate.check_lane_tool +
            # check_edit remain the authoritative server-side gate; the
            # CLI flag just lets the subprocess run at all.
            # --settings passes a pre-approved permissions block the
            # CLI honors in -p headless mode. Without it the CLI demands
            # per-tool approval for MCP tools despite --dangerously-skip-
            # permissions (anthropics/claude-code #581, #28580, #13077).
            # Worker-scoped temp path per spawn avoids collisions between
            # concurrent lanes; removed in the finally block.
            worker_settings_path = _write_worker_settings_file(
                project_root,
                cli_allowed,
                disallowed,
            )
            try:
                # --allow-dangerously-skip-permissions GATES whether the
                # mode is available; --dangerously-skip-permissions
                # activates it. CC source (permissionSetup.ts:937-943)
                # checks `permissionMode === 'bypassPermissions' ||
                # allowDangerouslySkipPermissions` — under a Statsig
                # killswitch or managed settings, passing only the
                # activation flag drops silently to 'default' mode.
                # Passing both ensures the mode is enabled AND used.
                #
                # Prompt is piped through stdin instead of passed via
                # `-p <string>` because long lane IDs (slugified from
                # brief text) push argv past Windows' 32 KB command-
                # line limit. stdin has no such limit.
                # (Diagnosed 2026-04-19 sla-1 spawn: exit code 1,
                # "The command line is too long.")
                #
                # --mcp-config explicit pass (2026-04-20 fix): the CLI
                # under -p headless mode does NOT reliably auto-load
                # project-level `.mcp.json` from cwd — spawned workers
                # report `mcp__aidocs__*` namespace absent, can't call
                # lane_worker_bind, can't use code_* tools. Pointing
                # the CLI at the project's .mcp.json explicitly makes
                # the aidocs MCP server attach from the first turn so
                # the worker's Step 0 (lane_worker_bind) actually runs.
                #
                # Ensure the aidocs entry in .mcp.json is fresh BEFORE
                # the subprocess reads it. Stale entries (old PYTHONPATH,
                # wrong command) would survive the "already present"
                # short-circuit in ensure_claude_mcp_config and leave
                # the worker's MCP server failing to start. Calling it
                # here means every spawn validates the config against
                # the current install layout.
                try:
                    self.runtime.ensure_claude_mcp_config(project_root)
                except Exception as _mcp_exc:
                    logger.warning(
                        "ensure_claude_mcp_config failed before worker "
                        "spawn: %s — sub-CLI may have stale MCP config",
                        _mcp_exc,
                    )
                # Model resolution: packet route → conductor.claude_model
                # config → omit (CLI default wins). Unlike opencode, we
                # don't refuse when unset — `claude -p` picks a working
                # default model on every supported install, so "no model"
                # is a valid (and cheapest-common-path) dispatch shape.
                claude_model = ""
                _route = packet.get("route")
                if isinstance(_route, dict):
                    claude_model = str(_route.get("model") or "").strip()
                if not claude_model:
                    try:
                        from .config import get_setting

                        claude_model = str(
                            get_setting(
                                "conductor.claude_model",
                                project_root=project_root,
                                default="",
                            )
                            or "",
                        ).strip()
                    except Exception:
                        claude_model = ""

                # Phoenix 2026-05-12 (dental bug report Case B): write a
                # per-spawn .mcp.json that augments the aidocs entry's
                # env block with worker identity (AIDOCS_EXPERT_*).
                # Claude does NOT propagate parent process env to MCP
                # children; only the keys in .mcp.json's env reach the
                # MCP child. Without this, session_connect falls into
                # the CONDUCTOR branch and the worker idles as if it
                # were a fresh conductor. Falls back to project's
                # static .mcp.json on writer failure.
                worker_mcp_config_path = _write_claude_worker_mcp_config(
                    project_root,
                    worker_env,
                )
                mcp_config_path = worker_mcp_config_path or (project_root / ".mcp.json")
                cli_args = [
                    claude_path,
                    "-p",
                    # Phoenix 2026-05-09: switched text → json so
                    # spawn-site parse can extract claude's session_id
                    # for §VIII deny-path host_session_id stamping.
                    # claude headless does NOT invoke PreToolUse
                    # hooks, so the claude_hook stamp path is
                    # structurally inaccessible. JSON output's
                    # `session_id` field is the only reliable surface.
                    "--output-format",
                    "json",
                    "--allow-dangerously-skip-permissions",
                    "--dangerously-skip-permissions",
                    "--settings",
                    str(worker_settings_path),
                    "--allowedTools",
                    *cli_allowed,
                    "--disallowedTools",
                    *disallowed,
                ]
                if claude_model:
                    cli_args.extend(["--model", claude_model])
                if mcp_config_path.is_file():
                    cli_args.extend(["--mcp-config", str(mcp_config_path)])
                proc = subprocess.run(
                    cli_args,
                    input=prompt,
                    cwd=str(project_root),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=worker_env,
                )
            finally:
                try:
                    worker_settings_path.unlink()
                except OSError:
                    pass
                if worker_mcp_config_path is not None:
                    try:
                        worker_mcp_config_path.unlink()
                    except OSError:
                        pass
            if proc.returncode != 0:
                return WorkerResult(
                    lane_id=lane_id,
                    success=False,
                    error=f"Claude exited with code {proc.returncode}: {proc.stderr[-500:]}",
                    raw_output=proc.stdout[-2000:],
                )
            # Phoenix 2026-05-09 §VIII: parse claude's JSON summary
            # for session_id, then stamp + patch (mirror opencode
            # spawn_worker_opencode pattern). claude -p emits a
            # single-line JSON object on stdout containing
            # "session_id":"<uuid>", "result":"<assistant text>",
            # plus telemetry. Hand `result` to _parse_worker_output
            # for the AIDOCS_EXPERT_RESULT sentinel; fall back to
            # full stdout on parse failure (text-format compat).
            import json as _json_claude

            captured_host_session_id = ""
            result_text = proc.stdout
            try:
                claude_summary = _json_claude.loads(proc.stdout.strip().splitlines()[-1])
                if isinstance(claude_summary, dict):
                    sid = claude_summary.get("session_id")
                    if isinstance(sid, str) and sid.strip():
                        captured_host_session_id = sid.strip()
                    res = claude_summary.get("result")
                    if isinstance(res, str):
                        result_text = res
            except Exception:
                pass
            if captured_host_session_id and worker_registry_id:
                try:
                    from .session_lane_agents_store import SessionLaneAgentsStore

                    SessionLaneAgentsStore().set_host_session_id(
                        project_root,
                        worker_registry_id,
                        captured_host_session_id,
                    )
                except Exception:
                    pass
                try:
                    import sqlite3 as _sql_patch_c

                    review_db = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
                    if review_db.exists():
                        with _sql_patch_c.connect(str(review_db)) as conn:
                            conn.execute(
                                "UPDATE lane_completion_reviews "
                                "SET host_session_id = ? "
                                "WHERE worker_id = ? AND "
                                "(host_session_id IS NULL OR host_session_id = '')",
                                (captured_host_session_id, worker_registry_id),
                            )
                except Exception:
                    pass
            return _parse_worker_output(
                result_text,
                lane_id,
                project_root=project_root,
                session_id=str(packet.get("session_id", "") or ""),
                worker_registry_id=worker_registry_id or "",
            )
        except subprocess.TimeoutExpired:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error=f"Claude worker timed out after {timeout}s",
            )
        except Exception as exc:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error=str(exc),
            )

    def spawn_worker_codex(
        self,
        project_root: Path,
        packet: dict[str, object],
        *,
        timeout: int = 300,
    ) -> WorkerResult:
        """Spawn an OpenAI Codex worker for a conductor lane."""
        lane_id = str(packet.get("lane_id", "unknown"))
        prompt = _build_worker_prompt(packet)

        codex_path = shutil.which("codex")
        if not codex_path:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error="Codex CLI not found. Install OpenAI Codex CLI.",
            )

        try:
            # stdin=DEVNULL — see spawn_worker_claude for the same
            # non-interactive hang that bites both CLIs when parent
            # doesn't close stdin explicitly.
            import json as _json_env
            import os as _os_env

            worker_env = dict(_os_env.environ)
            worker_env["AIDOCS_EXPERT_LANE_ID"] = lane_id
            worker_env["AIDOCS_EXPERT_SESSION_ID"] = str(packet.get("session_id", ""))
            # AIDOCS_EXPERT_ID — see spawn_worker_claude for the reaper/
            # dashboard contract this registration maintains.
            worker_registry_id = self._register_lane_worker(
                project_root,
                packet,
                backend="codex",
            )
            if worker_registry_id:
                worker_env["AIDOCS_EXPERT_ID"] = worker_registry_id
            # lane_exact_paths env override — see spawn_worker_claude
            # for the shared-row-clobber bug this works around.
            lane_files_raw = packet.get("allowed_files") or []
            if isinstance(lane_files_raw, (list, tuple)):
                worker_env["AIDOCS_EXPERT_LANE_FILES"] = _json_env.dumps(
                    [str(p) for p in lane_files_raw if str(p).strip()],
                )
            # Strip host-IDE context vars — see spawn_worker_claude for
            # the same inheritance trap (claude.exe forces default perm
            # mode when it detects the VSCode extension env).
            for _vsc_var in (
                "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDECODE",
                "CLAUDE_VSCODE_IPC_SOCK",
                "VSCODE_INJECTION",
                "VSCODE_PID",
                "VSCODE_IPC_HOOK",
                "VSCODE_IPC_HOOK_CLI",
                "TERM_PROGRAM",
            ):
                worker_env.pop(_vsc_var, None)
            proc = subprocess.run(
                [codex_path, "-q", prompt],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                env=worker_env,
            )
            if proc.returncode != 0:
                return WorkerResult(
                    lane_id=lane_id,
                    success=False,
                    error=f"Codex exited with code {proc.returncode}: {proc.stderr[-500:]}",
                    raw_output=proc.stdout[-2000:],
                )
            return _parse_worker_output(
                proc.stdout,
                lane_id,
                project_root=project_root,
                session_id=str(packet.get("session_id", "") or ""),
                worker_registry_id=worker_registry_id or "",
            )
        except subprocess.TimeoutExpired:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error=f"Codex worker timed out after {timeout}s",
            )
        except Exception as exc:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error=str(exc),
            )

    def spawn_worker_opencode(
        self,
        project_root: Path,
        packet: dict[str, object],
        *,
        timeout: int = 300,
        host_session_id: str = "",
        prompt_override: str = "",
    ) -> WorkerResult:
        """Spawn an OpenCode worker for a conductor lane.

        Uses `opencode run --model <provider/model> "<prompt>"` (v1.4+
        syntax; the older -p flag is deprecated — see
        https://opencode.ai/docs/cli/). One-shot; no --attach (that's
        the conductor-tier path, not lane workers).

        host_session_id (Phoenix 2026-05-10): when set, adds
        `--session <id>` to the opencode CLI args so the spawn
        resumes a prior opencode session (the LLM continues where it
        left off, in-context). Used by `resume_opencode_worker` for
        the kill-then-resurrect path.

        prompt_override: when set, replaces the default imperative
        kicker. Used by resume to send "continue" or operator-supplied
        guidance instead of the bare `session_connect` first-step.
        """
        lane_id = str(packet.get("lane_id", "unknown"))
        # _build_worker_prompt returns the bare one-word kicker
        # ("session_connect") per the test contract: prompt content
        # stays a single registered MCP tool name, no instructions,
        # injection-scanner-safe. But that bare token reads as a noun
        # to Opus 4.7 / minimax / opencode-backed models, which then
        # idle ("Acknowledged. Awaiting your task.") instead of
        # calling the tool. Wrap the kicker in an imperative ONLY at
        # spawn-site — the test contract stays satisfied (the
        # function still returns the bare token), and the worker
        # actually acts. Filed 2026-05-10 as the worker-idle
        # regression (Phoenix amendment, witnessed live across two
        # opencode workers w-ba60fb77acd5 + w-60a3648d82d2).
        if prompt_override.strip():
            prompt = prompt_override.strip()
        else:
            kicker = _build_worker_prompt(packet)
            prompt = (
                f"Call the mcp__aidocs__{kicker} tool with mode='connect' now "
                f"to receive your lane plan and instructions, then act "
                f"on the plan body in its response."
            )

        opencode_path = shutil.which("opencode")
        if not opencode_path:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error="OpenCode CLI not found. Install opencode.",
            )

        # Model resolution: packet route → config default → hardcoded
        # universal fallback. conductor_resolve_backend upstream
        # populates packet['route']['model'] from task_routing.
        model_slug = ""
        route = packet.get("route")
        if isinstance(route, dict):
            model_slug = str(route.get("model") or "").strip()
        if not model_slug:
            try:
                from .config import get_setting

                model_slug = str(
                    get_setting(
                        "conductor.opencode_model",
                        project_root=project_root,
                        default="",
                    )
                    or "",
                ).strip()
            except Exception:
                model_slug = ""
        if not model_slug:
            # No silent fallback. Different opencode installs enable
            # different provider sets (openrouter/*, opencode-zen/*,
            # google/*, etc.) — guessing "anthropic/claude-sonnet"
            # produced "Model not found" errors on installs without
            # claude. Refuse explicitly so the operator knows to set
            # conductor.opencode_model or the per-task route.
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error=(
                    "OpenCode worker requires a model slug. Set "
                    "conductor.opencode_model config or include "
                    "route.model in the lane's task_routing entry. "
                    "Run `opencode models` to see enabled providers."
                ),
            )

        try:
            import json as _json_env
            import os as _os_env

            worker_env = dict(_os_env.environ)
            worker_env["AIDOCS_EXPERT_LANE_ID"] = lane_id
            worker_env["AIDOCS_EXPERT_SESSION_ID"] = str(packet.get("session_id", ""))
            # Lane tool scope (2026-04-24): the worker's MCP server
            # reads this to filter the registered tool surface.
            # claude-backend workers get CLI-side filtering via
            # --allowedTools; opencode has no equivalent, so the
            # MCP-layer filter is mandatory. Matches _build_cli_allowed_tools
            # precedence: packet override → _DEFAULT_LANE_CLI_TOOLS.
            _lane_tools_raw = packet.get("lane_allowed_tools")
            if isinstance(_lane_tools_raw, (list, tuple)) and _lane_tools_raw:
                _lane_tools = [str(n) for n in _lane_tools_raw if n]
            else:
                _lane_tools = list(_DEFAULT_LANE_CLI_TOOLS)
            worker_env["AIDOCS_EXPERT_LANE_ALLOWED"] = _json_env.dumps(_lane_tools)
            worker_registry_id = self._register_lane_worker(
                project_root,
                packet,
                backend="opencode",
            )
            if worker_registry_id:
                worker_env["AIDOCS_EXPERT_ID"] = worker_registry_id
            lane_files_raw = packet.get("allowed_files") or []
            if isinstance(lane_files_raw, (list, tuple)):
                worker_env["AIDOCS_EXPERT_LANE_FILES"] = _json_env.dumps(
                    [str(p) for p in lane_files_raw if str(p).strip()],
                )
            # Strip host-IDE context vars (same trap as claude/codex).
            for _vsc_var in (
                "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDECODE",
                "CLAUDE_VSCODE_IPC_SOCK",
                "VSCODE_INJECTION",
                "VSCODE_PID",
                "VSCODE_IPC_HOOK",
                "VSCODE_IPC_HOOK_CLI",
                "TERM_PROGRAM",
            ):
                worker_env.pop(_vsc_var, None)
            # #163 (Phoenix 2026-05-10) — opencode does NOT forward
            # parent process env to its child MCP servers. Without
            # explicit `environment` config, the AIDOCS MCP child
            # spawned by opencode sees no AIDOCS_EXPERT_ID env, takes
            # the CONDUCTOR branch in session_connect, and returns
            # plans-list-shelf instead of the lane plan. Worker then
            # treats response as "session startup", never acts.
            #
            # Fix: write a per-spawn opencode config to a temp file
            # carrying the worker env vars in the MCP `environment`
            # block, then point opencode at it via OPENCODE_CONFIG.
            # Opencode merges configs (project's opencode.jsonc still
            # loads for instructions, models, etc.); only the MCP
            # environment block is overridden per-spawn.
            #
            # See https://opencode.ai/docs/config/ — OPENCODE_CONFIG
            # is supported as a config-file pointer.
            worker_oc_config_path = _write_opencode_worker_config(
                project_root,
                worker_env,
            )
            if worker_oc_config_path is not None:
                worker_env["OPENCODE_CONFIG"] = str(worker_oc_config_path)
            popen_kwargs: dict[str, object] = {
                "cwd": str(project_root),
                "capture_output": True,
                "text": True,
                "timeout": timeout,
                "stdin": subprocess.DEVNULL,
                "env": worker_env,
            }
            # Windows: suppress console flash when spawned from GUI.
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            # --format json → raw JSON event stream to stdout (the
            # default 'formatted' mode wraps the model response in
            # a TTY spinner animation that capture_output=True
            # can't cleanly harvest, leaving worker parse with
            # empty stdout).
            # --dangerously-skip-permissions → stdin=DEVNULL means
            # any interactive permission prompt deadlocks; the
            # sub-agent has to auto-approve or never exit. The
            # outer MCP still enforces every AIDOCS gate on the
            # sub-agent's tool calls — this flag only affects
            # opencode's internal per-tool confirm UI.
            # --print-logs → surface opencode internal logs on
            # stderr so the spawner's empty-stdout branch can show
            # the operator what happened.
            # See https://opencode.ai/docs/cli/.
            try:
                # #163-followup (Phoenix 2026-05-10): bind to the
                # `aidocs-worker` agent profile (.opencode/agents/
                # aidocs-worker.md). That profile denies every native
                # opencode tool (read/edit/bash/grep/glob/...) and
                # allows only `aidocs_*` MCP tools — opencode's
                # equivalent of claude's --disallowedTools native
                # ban. Witness 2026-05-10: w-96268ea3d980 used
                # opencode's built-in `read` tool to bypass the
                # AIDOCS lane allowlist (which only governs MCP-
                # routed tools). Castle law: claude uses
                # --disallowedTools, opencode uses --agent +
                # permission frontmatter. Different key, same door.
                _oc_argv = [
                    opencode_path,
                    "run",
                    "--agent",
                    "aidocs-worker",
                    "--format",
                    "json",
                    "--dangerously-skip-permissions",
                    "--print-logs",
                    "--model",
                    model_slug,
                ]
                if host_session_id and host_session_id.strip():
                    # Resume the prior opencode session — model picks
                    # up its in-context history. New worker_id, same
                    # opencode session.
                    _oc_argv.extend(["--session", host_session_id.strip()])
                _oc_argv.append(prompt)
                proc = subprocess.run(_oc_argv, **popen_kwargs)
            finally:
                # #163 cleanup: remove the per-spawn opencode config.
                if worker_oc_config_path is not None:
                    try:
                        worker_oc_config_path.unlink()
                    except OSError:
                        pass
            # Coerce None → "" defensively. capture_output=True with
            # text=True SHOULD always give strings, but a corrupted
            # pipe / Windows CREATE_NO_WINDOW interaction has been
            # seen to leave proc.stdout = None, which then crashes
            # .strip() / slice with 'NoneType' has no attribute 'strip'.
            stdout_s = proc.stdout or ""
            stderr_s = proc.stderr or ""
            # Phoenix 2026-05-10: dump full stdout + stderr to disk
            # for post-mortem inspection. WorkerResult.raw_output is
            # truncated to 2000 chars in the envelope, fine for normal
            # flows but loses the model's full JSON event stream when
            # debugging "why did the worker stop after task_begin?".
            # One log file per worker, keyed by worker_id. Best-effort
            # — disk-write failure must not break the worker return.
            try:
                if worker_registry_id:
                    log_dir = project_root / ".MEMORY" / ".index" / "opencode_worker_logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_path = log_dir / f"{worker_registry_id}.log"
                    log_path.write_text(
                        f"=== returncode: {proc.returncode} ===\n"
                        f"=== stdout ({len(stdout_s)} bytes) ===\n"
                        f"{stdout_s}\n"
                        f"=== stderr ({len(stderr_s)} bytes) ===\n"
                        f"{stderr_s}\n",
                        encoding="utf-8",
                    )
            except Exception:
                pass
            if proc.returncode != 0:
                return WorkerResult(
                    lane_id=lane_id,
                    success=False,
                    error=f"OpenCode exited with code {proc.returncode}: {stderr_s[-500:]}",
                    raw_output=stdout_s[-2000:],
                )
            # OpenCode quirk: CLI exits 0 even when the model couldn't
            # run (e.g. "Model not found" gets written to stderr, stdout
            # stays empty). Surface that as a failure with the stderr
            # tail so operators don't see a silent green success.
            if not stdout_s.strip() and stderr_s.strip():
                return WorkerResult(
                    lane_id=lane_id,
                    success=False,
                    error=f"OpenCode exit 0 but stdout empty; stderr: {stderr_s[-500:]}",
                    raw_output="",
                )
            # --format json emits one JSON event per line. Only the
            # `text` events carry the model's prose; collate their
            # part.text into a single string so _parse_worker_output
            # can find the AIDOCS_EXPERT_RESULT sentinel just like
            # it does for claude/codex plain-stdout workers.
            #
            # Phoenix 2026-05-09 §VIII deny-path: every JSON event
            # carries top-level `sessionID` (the `ses_...` opencode
            # uuid). Capture it on first occurrence — opencode 1.4.3
            # only loads plugins registered via package.json (not
            # file drops in ~/.config/opencode/plugins/), so the
            # plugin-side chat.message stamp doesn't fire. Spawn-site
            # parse is the working surface. After the worker exits,
            # we stamp session_lane_agents.host_session_id AND patch
            # any already-captured lane_completion_reviews row that
            # was written with empty host_session_id (capture
            # happens at the worker's task_complete call, before
            # this parse runs).
            import json as _json_parse

            collated: list[str] = []
            captured_host_session_id = ""
            for line in stdout_s.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = _json_parse.loads(line)
                except (_json_parse.JSONDecodeError, ValueError):
                    # Non-JSON line (e.g. a startup banner opencode
                    # forgot to gate on --format). Pass it through so
                    # the parser still sees raw text if the sentinel
                    # happens to be there.
                    collated.append(line)
                    continue
                if not isinstance(event, dict):
                    continue
                if not captured_host_session_id:
                    sid = event.get("sessionID")
                    if isinstance(sid, str) and sid.strip():
                        captured_host_session_id = sid.strip()
                if event.get("type") == "text":
                    part = event.get("part") or {}
                    text = part.get("text") if isinstance(part, dict) else ""
                    if text:
                        collated.append(str(text))
            # Stamp host_session_id post-exit, idempotent updates.
            # Phoenix 2026-05-12: with the opencode plugin now properly
            # registered via the `plugin` config array, the plugin's
            # chat.message hook stamps host_session_id BEFORE the
            # worker reaches task_complete. This post-exit stamp is
            # 2nd-line defense for the case where the plugin path
            # fails (python not on PATH for the plugin's spawn, etc.).
            # The earlier #168 stderr-regex fallback was removed as a
            # huge-net workaround for a symptom (0-byte stdout) whose
            # root cause was the plugin never firing at all.
            if captured_host_session_id and worker_registry_id:
                try:
                    from .session_lane_agents_store import SessionLaneAgentsStore

                    SessionLaneAgentsStore().set_host_session_id(
                        project_root,
                        worker_registry_id,
                        captured_host_session_id,
                    )
                except Exception:
                    pass
                try:
                    import sqlite3 as _sql_patch

                    review_db = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
                    if review_db.exists():
                        with _sql_patch.connect(str(review_db)) as conn:
                            conn.execute(
                                "UPDATE lane_completion_reviews "
                                "SET host_session_id = ? "
                                "WHERE worker_id = ? AND "
                                "(host_session_id IS NULL OR host_session_id = '')",
                                (captured_host_session_id, worker_registry_id),
                            )
                except Exception:
                    pass
            prose = "\n".join(collated)
            return _parse_worker_output(
                prose,
                lane_id,
                project_root=project_root,
                session_id=str(packet.get("session_id", "") or ""),
                worker_registry_id=worker_registry_id or "",
            )
        except subprocess.TimeoutExpired as te:
            # Phoenix 2026-05-10: capture partial stdout/stderr from
            # the timeout exception so the post-mortem log file is
            # written even when the worker idle-times-out (the most
            # common failure mode for the post-task_begin idle bug).
            try:
                if worker_registry_id:
                    log_dir = project_root / ".MEMORY" / ".index" / "opencode_worker_logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_path = log_dir / f"{worker_registry_id}.log"
                    partial_out = (
                        te.stdout
                        if isinstance(te.stdout, str)
                        else (
                            te.stdout.decode("utf-8", errors="replace")
                            if isinstance(te.stdout, bytes)
                            else ""
                        )
                    )
                    partial_err = (
                        te.stderr
                        if isinstance(te.stderr, str)
                        else (
                            te.stderr.decode("utf-8", errors="replace")
                            if isinstance(te.stderr, bytes)
                            else ""
                        )
                    )
                    log_path.write_text(
                        f"=== TIMEOUT after {timeout}s ===\n"
                        f"=== partial stdout ({len(partial_out)} bytes) ===\n"
                        f"{partial_out}\n"
                        f"=== partial stderr ({len(partial_err)} bytes) ===\n"
                        f"{partial_err}\n",
                        encoding="utf-8",
                    )
            except Exception:
                pass
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error=f"OpenCode worker timed out after {timeout}s",
            )
        except Exception as exc:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error=str(exc),
            )

    def resume_opencode_worker(
        self,
        project_root: Path,
        *,
        prior_worker_id: str,
        host_session_id: str,
        session_id: str,
        lane_id: str,
        prompt: str = "continue",
        model: str = "",
    ) -> WorkerResult:
        """Re-spawn an opencode worker resuming a prior session.

        Builds a fresh lane packet and calls spawn_worker_opencode
        with `host_session_id` (→ `--session <id>`) so the LLM picks
        up its prior context, plus `prompt_override` for the resume
        kicker. New worker_id, same opencode session history.
        """
        packet = self.runtime._conductor_dispatch._build_subagent_task_packet(
            project_root,
            session_id,
            lane_id,
        )
        if isinstance(packet, dict):
            packet["resumed_from"] = prior_worker_id
        return self.spawn_worker_opencode(
            project_root,
            packet,
            host_session_id=host_session_id,
            prompt_override=prompt or "continue",
        )

    def spawn_worker(
        self,
        project_root: Path,
        packet: dict[str, object],
        *,
        backend: str = "claude",
        timeout: int = 300,
    ) -> WorkerResult:
        """Spawn a worker using the specified backend."""
        if backend == "claude":
            return self.spawn_worker_claude(project_root, packet, timeout=timeout)
        if backend in ("codex", "openai"):
            return self.spawn_worker_codex(project_root, packet, timeout=timeout)
        if backend == "opencode":
            return self.spawn_worker_opencode(project_root, packet, timeout=timeout)
        return WorkerResult(
            lane_id=str(packet.get("lane_id", "unknown")),
            success=False,
            error=f"Unknown agent backend: {backend}",
        )

    # ── Detached mode: fire-and-track background spawn ──

    def spawn_worker_async(
        self,
        project_root: Path,
        packet: dict[str, object],
        *,
        backend: str = "claude",
        timeout: int = 600,
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Spawn a worker in a background thread and return a handle immediately.

        The MCP tool call returns in milliseconds; the actual sub-CLI runs
        in a daemon thread. Poll `get_worker_status(worker_id)` for result.
        When session_id is provided, plan_dispatch_report is invoked on the
        runtime once the worker finishes, so conductor lane state tracks
        completion without the caller having to relay the result manually.

        Refuses the spawn when the project-level cap
        `conductor.max_concurrent_workers` is already hit. Each worker is
        a full Claude CLI subprocess + MCP stdio server; on a typical
        dev box >3-4 concurrent exhausts memory + provider rate limits.
        """
        lane_id = str(packet.get("lane_id", "unknown"))

        # Enforce the global spawn cap BEFORE minting the worker_id or
        # starting the thread — refusals should leave no stray job row.
        try:
            from .config import get_setting

            cap_raw = get_setting(
                "conductor.max_concurrent_workers",
                project_root=project_root,
                default=3,
            )
            cap = int(cap_raw) if cap_raw is not None else 3
        except Exception:
            cap = 3
        with self._jobs_lock:
            live = sum(1 for j in self._jobs.values() if not j.done)
        if cap > 0 and live >= cap:
            return {
                "success": False,
                "error": (
                    f"conductor.max_concurrent_workers={cap} reached "
                    f"({live} running). Wait for a lane to complete or "
                    f"raise the cap in the dashboard config."
                ),
                "live_workers": live,
                "cap": cap,
            }

        # Per-machine ceiling (2026-04-21): conductors + workers on THIS
        # host share one cap regardless of project. See standards.md.
        try:
            from .config import get_setting
            from .host_concurrency_store import check_machine_capacity

            machine_cap_raw = get_setting(
                "run.max_live_processes_per_machine",
                project_root=project_root,
                default=4,
            )
            machine_cap = int(machine_cap_raw) if machine_cap_raw is not None else 4
        except Exception:
            machine_cap = 4
        try:
            machine_decision = check_machine_capacity(
                max_processes=machine_cap,
                kind="worker",
            )
        except Exception:
            machine_decision = {"ok": True}
        if not machine_decision.get("ok"):
            return {
                "success": False,
                "error": machine_decision.get("error", "machine concurrency ceiling reached"),
                "blocked_by": "machine_concurrency",
                "live_count": machine_decision.get("live_count"),
                "max_processes": machine_decision.get("max_processes"),
            }

        worker_id = f"w-{uuid.uuid4().hex[:12]}"

        # Audit gap fill (2026-04-21): worker spawn event. Without this,
        # "who spawned what subagent when" is only reconstructable from
        # logs, never the audit chain. record_event stamps user_id via
        # identity_resolver which picks up the parent-process operator.
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="worker_spawned",
                source_kind="agent_worker",
                session_id=session_id,
                capability_name="ai_spawn",
                action_kind="spawn",
                target_entity=worker_id,
                status="running",
                payload={
                    "worker_id": worker_id,
                    "lane_id": lane_id,
                    "backend": backend,
                    "timeout": timeout,
                    "logical_mode": packet.get("think_mode"),
                    "native_mode": packet.get("native_think_mode"),
                    "fallback_used": bool(packet.get("fallback_used", False)),
                },
            )
        except Exception:
            pass

        # Register the machine-wide slot BEFORE starting the thread so
        # a rapid-fire second spawn on the same host sees the new
        # count immediately. Paired with unregister in _run's finally.
        try:
            import os as _os_register

            from .host_concurrency_store import HostConcurrencyStore

            # pid=os.getpid() (Phoenix 2026-05-12, king bug report):
            # without this the row carries pid=NULL and _sweep_dead's
            # `WHERE pid IS NOT NULL` clause skips it forever, so any
            # MCP-crash-orphaned worker row becomes a permanent phantom
            # blocking new spawns. Recording the parent MCP's pid lets
            # the next MCP boot's sweep reclaim slots from a dead MCP.
            # Workers within the SAME MCP still rely on _run's finally
            # to unregister (pid-alive check would say "alive" since
            # the MCP is alive).
            HostConcurrencyStore().register(
                worker_key=worker_id,
                kind="worker",
                pid=_os_register.getpid(),
                project_root=project_root,
                session_id=session_id,
            )
        except Exception:
            pass

        def _run() -> None:
            _start = time.monotonic()
            exit_payload: dict[str, object] = {
                "worker_id": worker_id,
                "lane_id": lane_id,
                "backend": backend,
                "logical_mode": packet.get("think_mode"),
                "native_mode": packet.get("native_think_mode"),
                "fallback_used": bool(packet.get("fallback_used", False)),
            }
            exit_status = "completed"
            try:
                result = self.spawn_worker(project_root, packet, backend=backend, timeout=timeout)
                with self._jobs_lock:
                    job = self._jobs.get(worker_id)
                    if job is not None:
                        job.result = result
                        job.done = True
                exit_payload["success"] = bool(getattr(result, "success", False))
                exit_payload["claimed_done"] = bool(getattr(result, "claimed_done", False))
                if session_id:
                    try:
                        self.runtime.plan_dispatch_report(
                            project_root,
                            session_id,
                            result.to_dict(),
                        )
                    except Exception as report_exc:  # noqa: BLE001
                        logger.warning(
                            "plan_dispatch_report failed for worker %s: %s",
                            worker_id,
                            report_exc,
                        )
            except Exception as exc:  # noqa: BLE001
                with self._jobs_lock:
                    job = self._jobs.get(worker_id)
                    if job is not None:
                        job.error = str(exc)
                        job.done = True
                exit_status = "failed"
                exit_payload["error"] = str(exc)[:500]
            finally:
                exit_payload["duration_seconds"] = round(time.monotonic() - _start, 2)
                try:
                    self.runtime.hub.execution.record_event(
                        project_root,
                        event_kind="worker_exited",
                        source_kind="agent_worker",
                        session_id=session_id,
                        capability_name="ai_spawn",
                        action_kind="exit",
                        target_entity=worker_id,
                        status=exit_status,
                        payload=exit_payload,
                    )
                except Exception:
                    pass
                # Release the machine-wide slot regardless of how we
                # exited — success, failure, or crash inside _run.
                try:
                    from .host_concurrency_store import HostConcurrencyStore

                    HostConcurrencyStore().unregister(worker_key=worker_id)
                except Exception:
                    pass

        thread = threading.Thread(target=_run, name=f"agent-worker-{worker_id}", daemon=True)
        job = BackgroundJob(
            worker_id=worker_id,
            lane_id=lane_id,
            backend=backend,
            started_at=time.monotonic(),
            thread=thread,
        )
        with self._jobs_lock:
            self._jobs[worker_id] = job
        thread.start()
        return {
            "worker_id": worker_id,
            "lane_id": lane_id,
            "backend": backend,
            "state": "running",
            "hint": "Poll ai_status(worker_id) until state=='done'.",
        }

    def get_worker_status(self, worker_id: str, *, verbose: bool = False) -> dict[str, object]:
        with self._jobs_lock:
            job = self._jobs.get(worker_id)
        if job is None:
            return {"worker_id": worker_id, "state": "unknown", "error": "no such worker_id"}
        return job.status(verbose=verbose)

    def resolve_worker_for_lane(self, lane_id: str) -> str | None:
        """The worker a conductor means when it acts BY LANE (120% clause B:
        'resume + kill by lane, not opaque worker_id'). Prefers a LIVE worker;
        falls back to the most-recently-started. None when the lane has none.
        """
        lane_id = (lane_id or "").strip()
        if not lane_id:
            return None
        with self._jobs_lock:
            candidates = [
                (wid, job)
                for wid, job in self._jobs.items()
                if getattr(job, "lane_id", "") == lane_id
            ]
        if not candidates:
            return None
        live = [(wid, j) for wid, j in candidates if not j.done and j.thread.is_alive()]
        pool = live or candidates
        pool.sort(key=lambda pair: pair[1].started_at, reverse=True)
        return pool[0][0]

    def list_worker_jobs(self, *, verbose: bool = False) -> list[dict[str, object]]:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        return [j.status(verbose=verbose) for j in jobs]

    # ── Full mode: interactive workers ──

    def spawn_interactive(
        self,
        project_root: Path,
        packet: dict[str, object],
        *,
        backend: str = "claude",
    ) -> InteractiveWorker | None:
        """Spawn an interactive worker (Full mode). Returns worker handle for control."""
        lane_id = str(packet.get("lane_id", "unknown"))
        prompt = _build_worker_prompt(packet)

        cli = shutil.which("claude" if backend == "claude" else "codex")
        if not cli:
            logger.warning("CLI not found for backend: %s", backend)
            return None

        try:
            proc = subprocess.Popen(
                [cli, "-p", prompt, "--output-format", "text"],
                cwd=str(project_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            worker = InteractiveWorker(lane_id, proc, backend)
            self._active_workers[lane_id] = worker
            return worker
        except Exception as exc:
            logger.error("Failed to spawn interactive worker: %s", exc)
            return None

    def get_worker(self, lane_id: str) -> InteractiveWorker | None:
        """Get an active interactive worker by lane ID."""
        return self._active_workers.get(lane_id)

    def pause_worker(self, lane_id: str, reason: str = "") -> bool:
        """Pause an active worker."""
        worker = self._active_workers.get(lane_id)
        if worker and worker.is_alive():
            worker.pause(reason)
            return True
        return False

    def resume_worker(self, lane_id: str, instructions: str = "") -> bool:
        """Resume a paused worker."""
        worker = self._active_workers.get(lane_id)
        if worker and worker.is_alive():
            worker.resume(instructions)
            return True
        return False

    def collect_worker(self, lane_id: str, timeout: int = 60) -> WorkerResult | None:
        """Collect result from a completed or paused worker."""
        worker = self._active_workers.pop(lane_id, None)
        if not worker:
            return None
        return worker.wait(timeout=timeout)

    def active_worker_status(self) -> list[dict[str, object]]:
        """Status of all active interactive workers."""
        return [
            {
                "lane_id": w.lane_id,
                "backend": w.backend,
                "alive": w.is_alive(),
                "paused": w.paused,
                "output_size": len("".join(w.output_buffer)),
            }
            for w in self._active_workers.values()
        ]
