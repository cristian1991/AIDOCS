"""OpenAI Agents SDK adapter — hooks AIDOCS orchestration into openai-agents.

Provides RunHooksBase and AgentHooksBase implementations that delegate to
AgentOrchestrator for tool gating, output guard, metrics, and lifecycle.

Usage:
    from openai_agents import Agent, Runner
    from aidocs_mcp.openai_agents_adapter import create_aidocs_hooks

    hooks = create_aidocs_hooks(project_root="/path/to/project")
    result = Runner.run_sync(agent, "do the work", hooks=hooks)

Requires: pip install openai-agents
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AIDOCSRunHooks:
    """OpenAI Agents SDK RunHooksBase implementation.

    Maps on_tool_start/end to AgentOrchestrator.check_tool() and output guard.
    Works without subclassing RunHooksBase so the openai-agents package
    is optional (adapter pattern, no import-time dependency).
    """

    def __init__(
        self,
        project_root: Path,
        session_id: str | None = None,
        agent_id: str = "openai-agent",
    ) -> None:
        self.project_root = Path(project_root)
        self.session_id = session_id
        self.agent_id = agent_id
        self._orchestrator = None
        self._runtime = None

    @property
    def orchestrator(self) -> Any:
        if self._orchestrator is None:
            from .agent_orchestrator import AgentOrchestrator
            from .runtime_service import RuntimeService
            from .service_hub import AidocsServiceHub

            hub = AidocsServiceHub(
                templates_root=self._resolve_templates_root(),
            )
            self._runtime = RuntimeService(hub)
            self._orchestrator = AgentOrchestrator(self._runtime)
        return self._orchestrator

    @property
    def runtime(self) -> Any:
        if self._runtime is None:
            _ = self.orchestrator  # triggers init
        return self._runtime

    def _resolve_templates_root(self) -> Path:
        """Find AIDOCS templates directory."""
        import os

        env_path = os.environ.get("AIDOCS_PATH")
        if env_path:
            candidate = Path(env_path) / ".MEMORY" / ".aidocs"
            if candidate.is_dir():
                return candidate
        for parent_offset in (3, 2):
            candidate = Path(__file__).resolve().parents[parent_offset] / ".MEMORY" / ".aidocs"
            if candidate.is_dir():
                return candidate
        return Path(__file__).resolve().parent

    # ── RunHooksBase interface ──

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        """Called when an agent starts. Claim session if conductor."""
        logger.debug("AIDOCS: agent_start %s", getattr(agent, "name", "unknown"))
        if self.session_id:
            self.orchestrator.conductor_claim(
                self.project_root,
                self.session_id,
                self.agent_id,
            )

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        """Called when an agent finishes. Release session claim."""
        logger.debug("AIDOCS: agent_end %s", getattr(agent, "name", "unknown"))
        if self.session_id:
            self.orchestrator.conductor_release(
                self.project_root,
                self.session_id,
                self.agent_id,
            )

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        """Called BEFORE a tool executes. One call to the canonical
        entry point ToolGate.evaluate_tool composes the full pretool
        pipeline (kill-switch → managed-mode → audit → freeze →
        orchestrator → sticky-grant ask → conductor comms →
        read-memory goggles). CC and OpenCode call the same surface.
        """
        tool_name = getattr(tool, "name", str(tool))
        tool_input = getattr(tool, "input", None)
        if isinstance(tool_input, str):
            import json

            try:
                tool_input = json.loads(tool_input)
            except (json.JSONDecodeError, TypeError):
                tool_input = {"raw": tool_input}
        elif not isinstance(tool_input, dict):
            tool_input = {}

        from .tool_gate_service import ToolGate

        verdict = ToolGate(self.orchestrator.runtime).evaluate_tool(
            tool_name=tool_name,
            tool_input=tool_input,
            host_session_id=self.session_id or "",
            project_root=self.project_root,
            payload={"tool_use_id": ""},
        )

        # ShellPolicy shadow (Batch 1.6, observe-only). Side-effect-free:
        # consumes the already-computed live verdict, never re-runs the
        # cascade, never blocks. Native shell tools only; unguardable
        # host/provider pairs are skipped (recorded would_block/ai_run).
        try:
            from .shell_policy_shadow import run_pretool_shadow

            run_pretool_shadow(
                project_root=self.project_root,
                host="openai_agents",
                tool_name=tool_name,
                tool_input=tool_input,
                host_session_id=self.session_id or "",
                live_verdict=str(verdict.verdict or ""),
                live_reason=str(verdict.reason or ""),
                live_why=tuple(verdict.why or ()),
            )
        except Exception:
            pass

        if verdict.verdict in ("deny", "ask"):
            self.orchestrator._record_event(
                self.project_root,
                "tool_blocked_openai",
                tool_name,
                "blocked",
                session_id=self.session_id or "",
                reason=verdict.reason,
                agent_id=self.agent_id,
            )
            raise RuntimeError(f"AIDOCS blocked tool '{tool_name}': {verdict.reason}")

        # Record tool start (OA-specific telemetry)
        from .metrics import get_collector

        get_collector().record_tool_call(
            tool_name=tool_name,
            status="started",
            session_id=self.session_id or "",
        )

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        """Called AFTER a tool executes. Routes through:
          1. LifecycleService.on_tool_end_output_guard (secret scan +
             redact/block per policy)
          2. LifecycleService.on_post_tool_use_audit (universal pair
             event with status detection)
          3. metrics collector (OA-specific telemetry)

        Same audit surface as CC's PostToolUse + same output-guard
        as the original OA-only implementation. Now all hosts share.
        """
        tool_name = getattr(tool, "name", str(tool))
        result_text = str(result) if result is not None else ""

        from .lifecycle_service import LifecycleService

        lc = LifecycleService(self.orchestrator.runtime)

        # 1. Output guard scan (was OA-only; now shared)
        lc.on_tool_end_output_guard(
            tool_name=tool_name,
            result_text=result_text,
            host_session_id=self.session_id or "",
            agent_id=self.agent_id,
            project_root=self.project_root,
        )

        # 2. Universal post-tool audit (matches CC's PostToolUse shape)
        lc.on_post_tool_use_audit(
            tool_name=tool_name,
            tool_input={},  # OA hooks don't expose tool_input on end
            tool_response={"text": result_text},
            host_session_id=self.session_id or "",
            project_root=self.project_root,
            payload={"tool_use_id": ""},
        )

        # 3. OA-specific metrics
        from .metrics import get_collector

        tokens_in = max(1, len(result_text.encode("utf-8")) // 4)
        get_collector().record_tool_call(
            tool_name=tool_name,
            status="completed",
            session_id=self.session_id or "",
            tokens_in_estimate=tokens_in,
        )

    async def on_llm_start(self, context: Any, agent: Any) -> None:
        """Called before LLM inference."""

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        """Called after LLM inference."""

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        """Agent-to-agent handoff — delegated to LifecycleService
        (host-agnostic). Same audit shape any future host with
        multi-agent flows will emit.
        """
        from_name = getattr(from_agent, "name", "unknown")
        to_name = getattr(to_agent, "name", "unknown")
        logger.info("AIDOCS: handoff %s → %s", from_name, to_name)
        from .lifecycle_service import LifecycleService

        LifecycleService(self.orchestrator.runtime).record_agent_handoff(
            from_agent_name=from_name,
            to_agent_name=to_name,
            host_session_id=self.session_id or "",
            agent_id=self.agent_id,
            project_root=self.project_root,
        )


class AIDOCSAgentHooks:
    """Per-agent hooks (optional, for more granular control)."""

    def __init__(self, parent: AIDOCSRunHooks) -> None:
        self.parent = parent

    async def on_start(self, context: Any, agent: Any) -> None:
        await self.parent.on_agent_start(context, agent)

    async def on_end(self, context: Any, agent: Any, output: Any) -> None:
        await self.parent.on_agent_end(context, agent, output)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        await self.parent.on_tool_start(context, agent, tool)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        await self.parent.on_tool_end(context, agent, tool, result)


def create_aidocs_hooks(
    project_root: str | Path,
    session_id: str | None = None,
    agent_id: str = "openai-agent",
) -> AIDOCSRunHooks:
    """Create AIDOCS hooks for the OpenAI Agents SDK.

    Usage:
        from openai_agents import Agent, Runner
        from aidocs_mcp.openai_agents_adapter import create_aidocs_hooks

        hooks = create_aidocs_hooks("/path/to/project", session_id="my-session")
        agent = Agent(name="coder", model="gpt-4o", instructions="...")
        result = Runner.run_sync(agent, "implement feature X", hooks=hooks)
    """
    return AIDOCSRunHooks(
        project_root=Path(project_root),
        session_id=session_id,
        agent_id=agent_id,
    )
