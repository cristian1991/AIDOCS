"""Agent-agnostic orchestration layer — core logic for tool gating, safety, and context.

This module contains the decision logic that applies to ALL agents (Claude Code,
Codex, DeepSeek, generic CLI). Agent-specific adapters (claude_hook.py, etc.)
map their host's event format to these functions.

The orchestrator does NOT know about hook payload formats, event names, or
host-specific response structures. It returns simple decisions that adapters
translate into host-native responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_service import RuntimeService


@dataclass(slots=True)
class ToolDecision:
    """Result of a tool gate check."""
    allowed: bool
    reason: str = ""
    advisory: str = ""  # non-blocking nudge text


@dataclass(slots=True)
class PromptContext:
    """Context to inject into the agent's prompt."""
    text: str
    action_kind: str = ""
    session_id: str = ""


# Tools that bypass all gate checks regardless of agent
SYSTEM_TOOLS: set[str] = {
    "todowrite",
    "todoread",
    "compact",
    "askuserquestion",
    "enterplanmode",
    "exitplanmode",
    "skill",
    "enterworktree",
    "exitworktree",
    "agent",
}

# ── Tool Tiering ──
# Tier 1 (eager): agent sees these immediately, no ToolSearch needed
# Everything else: deferred, discoverable via ToolSearch when user asks
EAGER_TOOLS: set[str] = {
    # Core code intelligence
    "code_investigate", "code_find", "code_get_lines", "code_bundle",
    "code_trace", "code_search", "code_text_search", "code_get_symbol_info",
    "code_get_symbol_snippet",
    # Code editing
    "code_edit_lines", "code_str_replace", "code_create_file",
    "code_batch_edit", "code_batch_str_replace", "code_insert_lines",
    # Build/test/git
    "code_build_project", "code_test_project", "code_run_command", "git_ops",
    # Session connection (bootstrap + lifecycle — keep simple)
    "session_start", "session_read", "session_list", "session_select",
    "session_create", "session_update", "session_resume_bundle",
    "session_claim", "session_release",
    "task_begin", "task_update", "task_complete",
    # Project bootstrap
    "project_bootstrap_or_resume", "project_init", "project_status",
    "project_check", "runtime_preflight",
    # Orchestration
    "orchestrate", "handle_prompt", "route_prompt",
    # Memory
    "memory_read", "memory_search", "memory_capture",
    # Index
    "code_index_sync", "index_sync", "project_sync_indexes",
    # Metrics
    "metrics_snapshot",
    # Plan connection
    "plan_connect", "plan_conductor_status",
    # Session journal
    "session_journal_log", "session_journal_read",
    # Handoff
    "session_handoff_get", "session_handoff_update",
}


def is_eager_tool(tool_name: str) -> bool:
    """Check if a tool should be eagerly loaded (visible to agent immediately)."""
    name = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__",):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name in EAGER_TOOLS or name in SYSTEM_TOOLS


class AgentOrchestrator:
    """Agent-agnostic orchestration — tool gating, safety, context building."""

    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    @property
    def hub(self) -> Any:
        return self.runtime.hub

    def check_tool(
        self,
        project_root: Path,
        tool_name: str,
        tool_input: dict[str, object] | None = None,
    ) -> ToolDecision:
        """Check if a tool call should be allowed. Agent-agnostic.

        Returns ToolDecision with allowed=True/False and reason/advisory text.
        Adapters translate this into host-specific responses.
        """
        from .access_gate import AccessGate, GateContext

        tool_input = tool_input or {}
        managed = self.hub.managed_mode.get_mode(project_root)

        if not managed.get("active"):
            if (project_root / ".MEMORY").is_dir():
                if tool_name.strip().lower() not in SYSTEM_TOOLS:
                    return ToolDecision(
                        allowed=False,
                        reason="AIDOCS project detected but managed mode is not active. Run /aidocs first to bind a session.",
                    )
            return ToolDecision(allowed=True)

        if tool_name.strip().lower() in SYSTEM_TOOLS:
            return ToolDecision(allowed=True)

        session_id = str(managed.get("session_id") or "").strip()

        # User-intent bypass
        if session_id:
            user_intent_tools = self.hub.query_gate.get_user_intent_tools(
                project_root, session_id
            )
            if tool_name.lower() in user_intent_tools:
                return ToolDecision(allowed=True)

        # Tool policies
        from .tool_policy import evaluate_tool as _eval_policy
        policy = _eval_policy(project_root, tool_name)
        if policy.blocked:
            self._record_event(project_root, "tool_policy_block", tool_name, "blocked",
                session_id=session_id, reason=policy.reason)
            return ToolDecision(allowed=False, reason=policy.reason or f"Tool `{tool_name}` blocked by project policy.")

        # Resolve config
        effective = self.runtime.effective_config(project_root)
        dev_config = effective.get("dev", {}) if isinstance(effective, dict) else {}
        gate_config = effective.get("gate", {}) if isinstance(effective, dict) else {}
        agents_config = effective.get("agents", {}) if isinstance(effective, dict) else {}
        dev_mode = bool(dev_config.get("dev_mode", False))
        allow_config_edit = bool(dev_config.get("allow_config_edit", False))
        gate_enforce = bool(gate_config.get("enforce", True))
        allow_subagents = bool(agents_config.get("allow_subagents", False))

        # Raw tool gate
        raw = AccessGate.check_raw_tool(
            GateContext(
                managed=True, session_id=session_id,
                dev_mode=dev_mode, allow_config_edit=allow_config_edit,
                gate_enforce=gate_enforce, gate_state={},
            ),
            tool_name, allow_subagents=allow_subagents, tool_input=tool_input,
        )
        if not raw.allowed:
            self._record_event(project_root, "raw_tool_block", tool_name, "blocked",
                session_id=session_id, reason=raw.reason)
            return ToolDecision(allowed=False, reason=raw.reason or "Blocked by AIDOCS managed mode.")

        # Bash allowlist
        if tool_name.lower() == "bash" and gate_enforce:
            from .access_gate import check_bash_allowed
            bash = check_bash_allowed(tool_input)
            if not bash.allowed:
                self._record_event(project_root, "bash_allowlist_block", tool_name, "blocked",
                    session_id=session_id, reason=bash.reason)
                return ToolDecision(allowed=False, reason=bash.reason or "Bash command not in allowlist.")

        # Heuristic judge
        from .heuristic_judge import evaluate_tool_call
        judge = evaluate_tool_call(tool_name, tool_input, project_root=project_root)
        if judge.should_block:
            top = judge.verdicts[0] if judge.verdicts else None
            reason = top.description if top else "Heuristic judge blocked this action."
            rec = top.recommendation if top else ""
            self._record_event(project_root, "judge_block", tool_name, "blocked",
                session_id=session_id, risk=judge.max_risk, reason=reason)
            return ToolDecision(allowed=False, reason=f"Risk assessment: {reason}" + (f" {rec}" if rec else ""))
        if judge.verdicts:
            self._record_event(project_root, "judge_advisory", tool_name, "allowed",
                session_id=session_id, risk=judge.max_risk)

        # Infrastructure protection
        infra = self._check_infrastructure(tool_name, tool_input, dev_mode=dev_mode, allow_config_edit=allow_config_edit)
        if infra:
            return ToolDecision(allowed=False, reason=infra)

        # Advisory nudges (non-blocking)
        advisory_parts: list[str] = []
        mcp_nudge = self._suggest_mcp_alternative(tool_name, tool_input)
        if mcp_nudge:
            advisory_parts.append(mcp_nudge)
        comment_nudge = self._comment_quality_nudge(tool_name)
        if comment_nudge:
            advisory_parts.append(comment_nudge)

        return ToolDecision(allowed=True, advisory=" ".join(advisory_parts))

    def grant_user_intent_tools(
        self, project_root: Path, session_id: str, prompt: str,
    ) -> set[str]:
        """Classify prompt and grant tool access based on user intent."""
        from .intent_guard import check_intent, TOOL_INTENT_MAP
        granted: set[str] = set()
        for tool_name, category in TOOL_INTENT_MAP.items():
            result = check_intent(tool_name, prompt)
            if result.allowed and result.category == "intent_required":
                granted.add(tool_name.lower())
        if granted:
            self.hub.query_gate.set_user_intent_tools(project_root, session_id, granted)
        return granted

    def clear_user_intent_tools(self, project_root: Path, session_id: str) -> None:
        """Clear user intent tool grants (call at start of each prompt)."""
        self.hub.query_gate.set_user_intent_tools(project_root, session_id, set())

    def build_lifecycle_nudge(
        self, project_root: Path, session_id: str, action_kind: str,
    ) -> str:
        """Build lifecycle follow-through nudge text."""
        compliance = self.runtime.session_compliance_summary(project_root, session_id)
        if not isinstance(compliance, dict):
            return ""

        parts: list[str] = []
        task_open = compliance.get("task_open")
        logging_debt = compliance.get("logging_debt")
        journal_coverage = compliance.get("journal_coverage", {})
        meaningful_since = journal_coverage.get("meaningful_event_count_since_journal", 0) if isinstance(journal_coverage, dict) else 0

        if task_open and action_kind in ("edit", "write_memory", "git_commit"):
            if meaningful_since and meaningful_since > 3:
                parts.append(
                    f"Lifecycle follow-through: meaningful edit work happened since the last lifecycle tool call; "
                    f"use `aidocs_task_complete` if the task is done."
                )
            elif meaningful_since and meaningful_since > 0:
                parts.append(
                    f"Lifecycle follow-through: meaningful work has accumulated since the last lifecycle tool call; "
                    f"use `aidocs_task_update` to record progress."
                )

        return " ".join(parts)

    # ── Conductor session lock ──

    def conductor_claim(
        self,
        project_root: Path,
        session_id: str,
        conductor_id: str,
        *,
        stale_minutes: int = 5,
    ) -> dict[str, object]:
        """Claim a session for conductor use. Rejects if another conductor has a fresh claim.

        Returns {claimed, conductor_id, existing_conductor, reason}.
        """
        claims = self.hub.sessions.list_claims(project_root, session_id, stale_after_minutes=stale_minutes)
        conductor_claims = [c for c in claims if str(c.get("mode", "")).startswith("conductor")]

        for existing in conductor_claims:
            if existing.get("agent_id") != conductor_id:
                return {
                    "claimed": False,
                    "conductor_id": conductor_id,
                    "existing_conductor": existing.get("agent_id"),
                    "last_seen": existing.get("last_seen"),
                    "reason": f"Session already claimed by conductor '{existing.get('agent_id')}' (last seen: {existing.get('last_seen')}). Wait for it to release or expire ({stale_minutes} min stale timeout).",
                }

        # Claim or refresh
        self.hub.sessions.claim_session(
            project_root, session_id,
            agent_id=conductor_id,
            run_id=f"conductor-{conductor_id}",
            mode="conductor",
        )
        return {
            "claimed": True,
            "conductor_id": conductor_id,
            "reason": "Session claimed for conductor use.",
        }

    def conductor_heartbeat(
        self,
        project_root: Path,
        session_id: str,
        conductor_id: str,
    ) -> dict[str, object]:
        """Refresh the conductor's claim heartbeat. Call every 30-60 seconds."""
        self.hub.sessions.claim_session(
            project_root, session_id,
            agent_id=conductor_id,
            run_id=f"conductor-{conductor_id}",
            mode="conductor",
        )
        return {"refreshed": True, "conductor_id": conductor_id}

    def conductor_release(
        self,
        project_root: Path,
        session_id: str,
        conductor_id: str,
    ) -> dict[str, object]:
        """Release the conductor's claim on a session."""
        self.hub.sessions.release_claim(
            project_root, session_id,
            agent_id=conductor_id,
        )
        return {"released": True, "conductor_id": conductor_id}

    # ── Internal helpers ──

    def _record_event(
        self, project_root: Path, event_kind: str, tool_name: str,
        status: str, session_id: str = "", **extra: object,
    ) -> None:
        try:
            self.hub.execution.record_event(
                project_root,
                event_kind=event_kind,
                source_kind="orchestrator",
                session_id=session_id or None,
                capability_name=tool_name,
                action_kind="security",
                status=status,
                payload={k: v for k, v in extra.items() if v is not None},
            )
        except Exception:
            pass

    def _check_infrastructure(
        self, tool_name: str, tool_input: dict[str, object],
        *, dev_mode: bool = False, allow_config_edit: bool = False,
    ) -> str | None:
        """Check infrastructure protection. Returns block reason or None."""
        from .config import render_interaction_text

        name = tool_name.strip().lower()
        if name not in ("bash", "write", "edit"):
            return None

        target = ""
        if name == "bash":
            cmd = str(tool_input.get("command", "")).lower()
            for pattern in ("aidocs.toml", "aidocs-plugin.json", "aidocs_mcp"):
                if pattern in cmd:
                    target = pattern
                    break
        else:
            target = str(tool_input.get("file_path", tool_input.get("path", ""))).lower()

        if not target:
            return None

        if "aidocs-plugin.json" in target:
            return render_interaction_text("interaction.gate_messages.infrastructure_edit_blocked", label="aidocs-plugin.json")
        if "aidocs.toml" in target and not allow_config_edit:
            return render_interaction_text("interaction.gate_messages.infrastructure_edit_blocked", label="aidocs.toml")
        if "aidocs_mcp" in target and not dev_mode:
            return render_interaction_text("interaction.gate_messages.infrastructure_source_blocked", path=target)

        return None

    def _suggest_mcp_alternative(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Suggest MCP alternatives for raw tools (advisory)."""
        name = tool_name.strip().lower()
        if name != "bash":
            return ""
        cmd = str(tool_input.get("command", ""))
        # Only nudge for file-reading bash commands
        file_patterns = (".py", ".ts", ".js", ".cs", ".go", ".rs", ".java", ".toml", ".json", ".yaml", ".yml")
        if any(ext in cmd for ext in file_patterns):
            if any(kw in cmd.lower() for kw in ("cat ", "head ", "tail ", "grep ", "find ", "ls ")):
                return "Use AIDOCS tools (code_get_lines, code_find, code_search) instead of bash for code files — saves tokens and grants indexed read access."
        return ""

    def _comment_quality_nudge(self, tool_name: str) -> str:
        """Return comment quality reminder for edit tools."""
        if tool_name.lower() not in ("edit", "write"):
            return ""
        from .config import CODE_QUALITY_COMMENT_ENFORCEMENT
        if CODE_QUALITY_COMMENT_ENFORCEMENT in ("strict", "advisory"):
            from .config import render_interaction_text
            return render_interaction_text("interaction.gate_messages.comment_quality")
        return ""
