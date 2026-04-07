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
            from .service_hub import AidocsServiceHub
            from .runtime_service import RuntimeService
            from .agent_orchestrator import AgentOrchestrator

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
                self.project_root, self.session_id, self.agent_id,
            )

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        """Called when an agent finishes. Release session claim."""
        logger.debug("AIDOCS: agent_end %s", getattr(agent, "name", "unknown"))
        if self.session_id:
            self.orchestrator.conductor_release(
                self.project_root, self.session_id, self.agent_id,
            )

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        """Called BEFORE a tool executes. Block if not allowed."""
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

        decision = self.orchestrator.check_tool(
            self.project_root, tool_name, tool_input,
        )

        if not decision.allowed:
            # Record the block
            self.orchestrator._record_event(
                self.project_root, "tool_blocked_openai", tool_name, "blocked",
                session_id=self.session_id or "", reason=decision.reason,
                agent_id=self.agent_id,
            )
            raise RuntimeError(
                f"AIDOCS blocked tool '{tool_name}': {decision.reason}"
            )

        if decision.advisory:
            logger.info("AIDOCS advisory for %s: %s", tool_name, decision.advisory)

        # Record tool start
        from .metrics import get_collector
        get_collector().record_tool_call(
            tool_name=tool_name, status="started",
            session_id=self.session_id or "",
        )

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        """Called AFTER a tool executes. Run output guard + metrics."""
        tool_name = getattr(tool, "name", str(tool))
        result_text = str(result) if result is not None else ""

        # Output guard
        from .output_guard import scan_text
        from .config import get_setting
        output_guard_enabled = get_setting(
            "gate.output_guard", project_root=self.project_root, default=True,
        )
        if output_guard_enabled and result_text:
            redact = get_setting(
                "gate.output_guard_redact", project_root=self.project_root, default=True,
            )
            guard_result = scan_text(result_text, redact=bool(redact))
            if not guard_result.clean:
                self.orchestrator._record_event(
                    self.project_root, "output_guard_finding_openai", tool_name, "flagged",
                    session_id=self.session_id or "",
                    finding_count=len(guard_result.findings),
                    redaction_count=guard_result.redaction_count,
                    agent_id=self.agent_id,
                )
                if guard_result.redaction_count > 0:
                    logger.warning(
                        "AIDOCS output guard: %d credentials redacted in %s result",
                        guard_result.redaction_count, tool_name,
                    )

        # Record tool completion
        from .metrics import get_collector
        tokens_in = max(1, len(result_text.encode("utf-8")) // 4)
        get_collector().record_tool_call(
            tool_name=tool_name, status="completed",
            session_id=self.session_id or "",
            tokens_in_estimate=tokens_in,
        )

    async def on_llm_start(self, context: Any, agent: Any) -> None:
        """Called before LLM inference."""
        pass

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        """Called after LLM inference."""
        pass

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        """Called when handing off between agents."""
        from_name = getattr(from_agent, "name", "unknown")
        to_name = getattr(to_agent, "name", "unknown")
        logger.info("AIDOCS: handoff %s → %s", from_name, to_name)
        self.orchestrator._record_event(
            self.project_root, "agent_handoff", f"{from_name}→{to_name}", "handoff",
            session_id=self.session_id or "",
            agent_id=self.agent_id,
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
