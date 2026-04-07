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
import subprocess
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
        }


def _build_worker_prompt(packet: dict[str, object]) -> str:
    """Build a structured prompt from a task packet for the worker agent."""
    parts = [
        f"# Task: {packet.get('goal', 'Complete the assigned work')}",
        "",
        "## Allowed Files",
        *[f"- {f}" for f in packet.get("allowed_files", [])],
        "",
        "## Constraints",
        *[f"- {c}" for c in packet.get("constraints", [])],
        "",
        "## Must NOT",
        *[f"- {m}" for m in packet.get("must_not", [])],
        "",
        "## Done Definition",
        *[f"- {d}" for d in packet.get("done_definition", [])],
        "",
        "## Verification Commands",
        *[f"- `{v}`" for v in packet.get("verification_commands", [])],
        "",
        "## Required Output",
        "When done, output a JSON block with:",
        "```json",
        json.dumps(packet.get("output_schema", {}), indent=2),
        "```",
    ]
    if packet.get("required_symbols"):
        parts.extend(["", "## Key Symbols (pre-resolved from index)"])
        parts.extend(f"- {s}" for s in packet["required_symbols"])
    return "\n".join(parts)


def _parse_worker_output(raw: str, lane_id: str) -> WorkerResult:
    """Parse structured output from worker agent response."""
    # Try to extract JSON from the output
    result = WorkerResult(lane_id=lane_id, success=True, raw_output=raw[-2000:])
    try:
        # Look for JSON block in output
        json_start = raw.rfind("{")
        json_end = raw.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            candidate = raw[json_start:json_end]
            data = json.loads(candidate)
            result.files_changed = data.get("files_changed", [])
            result.commands_run = data.get("commands_run", [])
            result.command_results = data.get("command_results", [])
            result.verification_results = data.get("verification_results", {})
            result.blockers = data.get("blockers", [])
            result.hidden_dependencies = data.get("hidden_dependencies", [])
            result.claimed_done = bool(data.get("claimed_done", False))
    except (json.JSONDecodeError, Exception):
        pass  # Best-effort parsing
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
            f"Reason: {reason or 'conductor intervention'}"
        )

    def resume(self, instructions: str = "") -> None:
        """Signal the agent to resume work."""
        self.paused = False
        self.send_message(
            f"CONDUCTOR RESUME: Continue your task. {instructions}"
        )

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



class AgentWorkerService:
    """Manages worker agent lifecycle for conductor lanes."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._active_workers: dict[str, InteractiveWorker] = {}  # lane_id → worker

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
        prompt = _build_worker_prompt(packet)

        claude_path = shutil.which("claude")
        if not claude_path:
            return WorkerResult(
                lane_id=lane_id,
                success=False,
                error="Claude CLI not found. Install Claude Code CLI.",
            )

        try:
            proc = subprocess.run(
                [claude_path, "-p", prompt, "--output-format", "text"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                return WorkerResult(
                    lane_id=lane_id,
                    success=False,
                    error=f"Claude exited with code {proc.returncode}: {proc.stderr[-500:]}",
                    raw_output=proc.stdout[-2000:],
                )
            return _parse_worker_output(proc.stdout, lane_id)
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
            proc = subprocess.run(
                [codex_path, "-q", prompt],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                return WorkerResult(
                    lane_id=lane_id,
                    success=False,
                    error=f"Codex exited with code {proc.returncode}: {proc.stderr[-500:]}",
                    raw_output=proc.stdout[-2000:],
                )
            return _parse_worker_output(proc.stdout, lane_id)
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
        return WorkerResult(
            lane_id=str(packet.get("lane_id", "unknown")),
            success=False,
            error=f"Unknown agent backend: {backend}",
        )

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
