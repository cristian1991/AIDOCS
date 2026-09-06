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


def _summarise_auth_ladder(diag: list[dict[str, Any]] | None) -> str:
    """Render the auth ladder's per-rung report into the refusal itself.

    WHY THE REFUSAL CARRIES THIS (2026-08-29). The old message said only "Sign
    in through the Dashboard/Codenexus flow or run `aidocs operator-login`" —
    advice a REMOTE WebMCP principal cannot act on, naming doors that exist only
    locally. Meanwhile ai_whoami reported that same caller as an authenticated
    SUPER_ADMIN, so the operator was handed two statements that could not both
    be true and nothing to tell them apart.

    They ARE both true. `gate_verdict.authenticated` describes THIS REQUEST;
    the shell door asks `_authenticated_uid`, whose first rung reads
    `current_gate_principal()` — a ContextVar that must be STAMPED from the
    request. An unstamped path drops to three rungs that are all LOCAL (env
    token / approved host-session binding / machine login), which no remote
    caller can satisfy. Printing the rung that declined turns that from a
    contradiction into a fact.

    ai_gate_explain cannot cover this: it is surface=local_only, so the
    instrument that explains a gate refusal does not exist on the gate. Its own
    allowlist entry already names the intended end state — "retire when the
    ladder and strike tables are SELF-DESCRIBING ON THE REFUSAL ENVELOPE".

    Rungs only: `path` and `outcome`, plus `detail` when there is one. No
    credential material, and no identity_db path — the diag carries one for the
    #557 local case and a filesystem path is not something a remote refusal
    should hand out.
    """
    if not diag:
        return ""
    parts: list[str] = []
    for entry in diag:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "?")
        outcome = str(entry.get("outcome") or "?")
        detail = str(entry.get("detail") or "").strip()
        parts.append(f"{path}={outcome}" + (f" ({detail})" if detail else ""))
    if not parts:
        return ""
    return (
        "Auth ladder, in order, so the declining rung is visible rather than "
        "inferred: " + "; ".join(parts) + ". "
        "`gate_principal` is the rung a remote WebMCP caller passes on. Absent, "
        "while ai_whoami reports you authenticated, means the request's "
        "principal was not stamped into this execution scope — no local login "
        "will change that. `ok` on a refusal means the shell door and the gate "
        "disagree about the same caller (#614/F), and the refusal is not this "
        "ladder's."
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
# ai_schema, etc.) intentionally absent — exploration must not require
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
        # ai_file mutates in three of its four modes (create / rename / delete;
        # restore recovers). Added with the tool, 2026-08-28 #958 — a
        # NAME-KEYED registry like this one goes stale the moment a capability
        # gets a new name, and a mutating tool missing from here is not treated
        # as mutating at all (see :1407 and the no_active_task friction).
        "ai_file",
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
    # WHICH rule refused (bash_policy matched_rule, or the judge rule_id for a
    # judge verdict). The #571 ladder reads it via getattr at every mint site;
    # it was never carried on the decision, so the ladder could only ever see
    # the TIER of a judge refusal, never the rule — which is how a shell write
    # to a .css file and an `rm -rf /var` arrived looking identical.
    matched_rule: str = ""


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
    # ai_replace is the unified entry (Empire doctrine 2026-05-01): one
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


_DESTRUCTIVE_DENY_SEEN: set[tuple[str, str]] = set()


def _first_destructive_denial(session_id: str, command: str) -> bool:
    """True iff this (session, command) destructive DENY is the FIRST this
    process has seen -- and records it. A first denied destructive command
    stays a PLAIN deny (no confirmable freeze); a REPEAT of the SAME command
    escalates. Operator directive 2026-07-15: 'don't freeze on the first rm
    attempt' -- one abandoned rm must not wedge the session. Per-process is
    sufficient: check_tool (MCP server) owns both the deny and this decision,
    so a retry re-enters here in the same process. Untrackable (no session /
    empty command) -> treat as first (lenient): the deny still holds; only the
    freeze is skipped.
    """
    import hashlib as _h

    cmd = (command or "").strip()
    if not session_id or not cmd:
        return True
    key = (session_id, _h.sha256(cmd.encode("utf-8", "replace")).hexdigest()[:32])
    if key in _DESTRUCTIVE_DENY_SEEN:
        return False
    _DESTRUCTIVE_DENY_SEEN.add(key)
    return True


def _bash_ask_rung_satisfied(tool_name: str, tool_input: dict) -> bool:
    """Import shim for the ONE confirmation check (local backlog 984).

    The comparison itself lives in `canonical_invocation` beside the mint that
    produces the hash, so the two can never drift. This wrapper exists only
    because the import must stay LOCAL: `canonical_invocation` reaches into the
    runtime helpers, and a module-level import here would tie a hot enforcement
    module to that graph at import time.

    Fails CLOSED on any error — an unreadable proof is not a proof, and the
    caller then simply asks, which is the safe outcome.
    """
    try:
        from . import canonical_invocation as _ci

        return _ci.bash_ask_confirmation_satisfies(tool_name, tool_input or {})
    except Exception:
        return False


class AgentOrchestrator:
    """Agent-agnostic orchestration — tool gating, safety, context building."""

    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    @property
    def hub(self) -> Any:
        return self.runtime.hub

    def _bash_unauthenticated_refusal(
        self,
        project_root: Path,
        session_id: str,
        tool_name: str,
        is_privileged_caller: bool,
    ) -> ToolDecision | None:
        """Refuse shell execution unless the shared auth seam proves a user.

        Interactive and delegated callers obey the same invariant. A delegated
        process may inherit a real machine login, bearer token, or enabled host
        binding, but never authority from its process role or machine presence.
        """
        # THE LADDER'S OWN DIAGNOSIS NOW TRAVELS WITH THE REFUSAL (2026-08-29).
        # This called the plain `_authenticated_uid` façade and threw the
        # per-rung report away, so the refusal could only say "not
        # authenticated" — never WHICH of the four rungs declined, or why.
        #
        # MEASURED LIVE: a WebMCP caller that ai_whoami reports as an
        # authenticated SUPER_ADMIN was refused shell execution. Rung 0
        # (gate_principal) exists precisely for that caller — #906 added it
        # after "a web connector with org role OWNER ... could never pass" — so
        # either it is not stamped on this path or it arrives without a
        # user_id, and the old message could not tell those apart. The operator
        # was left inferring a ContextVar's state from a sentence that
        # mentioned neither.
        #
        # ai_gate_explain IS NOT THE ANSWER HERE: it is surface=local_only, so
        # the one instrument that explains a gate refusal is unavailable ON the
        # gate — the instrument behind the block it explains (law 311bf3e6).
        # Its own allowlist entry already names the right end state: "retire
        # when the ladder and strike tables are SELF-DESCRIBING ON THE REFUSAL
        # ENVELOPE". This is that, for this rung.
        #
        # THE OLD REMEDY WAS ITSELF UNREACHABLE FOR THE CALLER IT REFUSED:
        # "Sign in through the Dashboard/Codenexus flow or run `aidocs
        # operator-login`" is a LOCAL act a remote WebMCP principal cannot
        # perform, so the refusal named a door absent from its surface.
        # THE VERDICT STAYS ON THE FAÇADE; ONLY THE MESSAGE READS THE DIAG.
        # Corrected after Gate 2b: routing the VERDICT through
        # `_authenticated_uid_diag` looked like a pure refactor (the façade is a
        # one-line delegation to it) and was NOT. Two suites stub the FAÇADE —
        # test_defect_F_bash_refusal_ignores_a_fully_authenticated_gate_principal
        # and test_delegated_shell_does_not_bypass_login — so calling past it
        # ran the REAL ladder and let a caller through that both suites require
        # refused. A message change had silently become an authority change,
        # which is exactly what this commit claimed it was not.
        #
        # It also surfaced something worth keeping: with the façade stubbed to
        # "" the real ladder's rung 0 answers `ok`, so the rendered ladder can
        # now read `gate_principal=ok` on a REFUSAL. That is #614/F itself made
        # legible — the shell door and the gate disagreeing about the same
        # caller — and it is a diagnosis, not a grant. Which rung actually
        # decides is the operator's call and the phase-4 AgentExecutionIdentity
        # contract's job, not something to slip in behind a message fix.
        del is_privileged_caller
        diag: list[dict[str, Any]] = []
        try:
            from .mcp_server_runtime_helpers import current_calling_host_session_id
            from .project_authority import _authenticated_uid

            host_session_id = str(current_calling_host_session_id() or "").strip()
            user_id = _authenticated_uid(project_root, host_session_id)
        except Exception:
            user_id = ""
            host_session_id = ""
        if user_id:
            return None
        try:
            from .project_authority import _authenticated_uid_diag

            _, diag = _authenticated_uid_diag(project_root, host_session_id)
        except Exception:
            diag = []
        try:
            self._record_event(
                project_root,
                "bash_policy_block",
                tool_name,
                "blocked",
                session_id=session_id,
                reason=(
                    "Unauthenticated caller — shell execution requires an "
                    "authenticated operator."
                ),
                matched_rule="policy.unauthenticated_host_session",
            )
        except Exception:
            pass
        return ToolDecision(
            allowed=False,
            reason=(
                "Refused: shell execution requires an authenticated operator. "
                # KEPT: this is the RIGHT remedy for a local caller, and the
                # three local rungs are the only ones a local caller has. It was
                # briefly deleted as "unreachable advice" — true for a remote
                # principal, wrong for everyone else. The ladder appended below
                # is what tells the two apart, so the advice is qualified rather
                # than removed.
                "Sign in through the Dashboard/Codenexus flow or run "
                "`aidocs operator-login`, then retry. "
                + _summarise_auth_ladder(diag)
            ),
            blocked_by="bash_policy",
        )

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
        hook as last_host_session_id. The harness writes task/deploy output to
        ``<TEMP>/claude/<slug>/<host-uuid>/tasks/``, so the host UUID is the
        binding that lets THIS session read its own output (and refuses other
        sessions'). #464: ALSO includes the owned host-id chain — every host
        uuid the authenticated hooks stamped for this managed session
        (host ids rotate on CLI resume) plus the harness transcript-dir
        uuid (the axis the task-artifact home is actually keyed by).
        De-duplicated, empties dropped."""
        out: list[str] = []
        seen: set[str] = set()

        def _add(value: str) -> None:
            v = str(value or "").strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)

        sid = str(session_id or "").strip()
        _add(sid)
        try:
            _add(self.hub.query_gate.get_last_host_session_id(project_root, sid) or "")
        except Exception:
            pass
        try:
            for cid in self.hub.query_gate.get_host_session_id_chain(project_root, sid) or []:
                _add(cid)
        except Exception:
            pass
        return out

    def _resolve_actor(self, project_root: Path, session_id: str) -> tuple[str, str]:
        """Resolve (actor, lane_id) for a tool-call security strike.

        actor ∈ {agent, subagent, lane_worker} (operator is the prompt
        path). #360: delegates to the ONE identity seam
        (``gate_tool._strike_actor_and_lane`` →
        ``task_actor_identity.resolve_task_actor``). The old read of the
        session's shared ``query_gate.current_lane_id`` is gone — that is
        the TASK's file-lane scope, session-shared state, so a CONDUCTOR
        strike thrown while any lane was bound got attributed to a
        lane_worker. The seam keys on the CALLER's authenticated identity
        (env stamp / principal / #217 registry), so attribution follows
        the actor, not whatever the session was doing.
        """
        del session_id  # scope axis is handled downstream; actor is per-caller
        from .gate_tool import _strike_actor_and_lane

        return _strike_actor_and_lane(project_root)

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
                    # #107 pause/resume protocol: the refusal is not a dead
                    # end — stamp the caller lane blocked_on_conflict (peer
                    # identity attached) and emit the lane_file_conflict
                    # signal so the conductor sees the blocked set in
                    # plan_conductor_status and arbitrates; the worker WAITS
                    # for plan_conductor_resume_lane instead of retry-spinning.
                    _stamped = None
                    try:
                        _stamped = _coord.stamp_conflict_block(
                            project_root,
                            _os.environ.get("AIDOCS_EXPERT_ID", "").strip(),
                            _xconf,
                        )
                        self.hub.execution.record_event(
                            project_root,
                            event_kind="lane_file_conflict",
                            source_kind="cross_agent_gate",
                            session_id=(_stamped or {}).get("session_id") or None,
                            capability_name=tool_name,
                            action_kind="conflict",
                            target_entity=str(_xconf.get("file_path") or "")[:300],
                            status="blocked",
                            payload={
                                "blocked_lane_id": (_stamped or {}).get("lane_id", ""),
                                "blocked_worker_id": (_stamped or {}).get("worker_id", ""),
                                "peer_lane_id": _xconf.get("owner_lane_id", ""),
                                "peer_session_id": _xconf.get("owner_session_id", ""),
                                "file_path": _xconf.get("file_path", ""),
                            },
                        )
                    except Exception:
                        pass  # stamp/signal is best-effort; the refusal stands
                    return ToolDecision(
                        allowed=False,
                        reason=(
                            _xconf["doctrine"]
                            + " The conductor has been signaled (lane_file_conflict"
                            + (
                                f"; your lane {_stamped['lane_id']} is marked blocked_on_conflict"
                                if _stamped
                                else ""
                            )
                            + "). WAIT for plan_conductor_resume_lane — do not retry "
                            "until the conductor resumes your lane."
                        ),
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

        # Deferred test-retry bookkeeping (#582). `test_retry` is a
        # BEHAVIOURAL counter — "you already ran this verification and it told
        # you what it had to tell you". Recording the invocation at the gate,
        # before the law cascade had decided anything, made it count ATTEMPTS
        # instead: a command refused downstream for the SHAPE of an argument
        # never ran, produced no signal, taught the agent nothing — and still
        # spent a unit of retry budget. Three of those and the gate announced
        # "this verification has already run multiple times", which is false
        # and sends the agent to the wrong repair, since only an EDIT clears
        # the counter. Input-shape rejection and behavioural repetition are
        # different categories and no longer share a counter. Every ALLOW path
        # downstream of the retry gate exits through _sec012_finalize, so the
        # count still rises on exactly the invocations that reach execution.
        _pending_test_invocation: list[tuple[str, str]] = []

        def _sec012_finalize(decision: ToolDecision) -> ToolDecision:
            if decision.allowed and _pending_test_invocation:
                _key, _lane = _pending_test_invocation[0]
                self._record_test_invocation(project_root, session_id, _key, lane_id=_lane)
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

        # #253 §XIX: over the WebMCP gate, resolve managed mode STRICTLY per the
        # authenticated gate identity (per-conductor, never the global singleton),
        # so one tenant's activation cannot authorize another tenant's tools.
        # Local dispatch has no gate principal -> singleton path unchanged.
        from .mcp_server_runtime_helpers import current_gate_principal as _cgp
        from .mcp_server_runtime_helpers import resolve_conductor_key as _rck

        _gp = _cgp()
        _gate_uid = str(_gp.get("user_id") or "").strip() if isinstance(_gp, dict) else ""
        # #906 -- THIS READER IS NOT ALLOWED TO SPELL THE KEY ITSELF.
        #
        # It used to, and so did both writers, three different ways: the gate's
        # session_select wrote under the bare OAuth user_id, ai_session(connect)
        # wrote under the composed web-<sha256(user_id + conversation)>, and this
        # line picked one of them. A single reader can only match a single
        # writer, so choosing either side fixed one caller and broke the other --
        # measured both ways on 2026-08-25, the second time as 15 red checks on
        # the VPS smoke. The key now comes from resolve_conductor_key(), which
        # every writer calls too; see its docstring for the rungs and why the
        # principal rung is a granularity fallback and NOT an identity one.
        #
        # #855 hoisted this call OUT of the `if _gate_uid:` below, because the
        # LOCAL branch needs the caller's key too (see there for why). The
        # resolver is a ContextVar read plus, at most, a principal read: no I/O,
        # no side effects, safe to ask on every dispatch. On the local surface it
        # returns rung 'host_session' -- the shim's window id -- or ("", "none"),
        # which is the honest "this caller is nobody" the branch below relies on.
        # The name still says `gate` because #906's parity tests pin THIS
        # IDENTIFIER as the only permitted source for a host_session_id kwarg
        # anywhere in check_tool; it is the resolver's output on both surfaces.
        _gate_hsid = ""
        _key_rung = "none"
        try:
            _gate_hsid, _key_rung = _rck()
        except Exception:  # noqa: BLE001 -- a resolver fault never authorizes
            _gate_hsid, _key_rung = "", "none"
        from .managed_mode_service import explain_managed_session

        def _door(**kw) -> tuple[str, bool]:
            """(session id, still-managed) from the #1027 authority door.

            THE SECOND VALUE IS NOT REDUNDANT. `stale_bind` /
            `managed_binding_names_no_session` mean "the binding names no
            usable session" -- they are the DENY-ish side of the door, and
            this gate must keep enforcing on them. Collapsing them into
            "unmanaged" makes check_tool return allowed=True for a
            destructive command: measured, not theorised (the pathb and
            bash-policy suites went green-to-allow on exactly that).
            """
            sid, reason = explain_managed_session(self.hub.managed_mode, project_root, **kw)
            if sid:
                return sid, True
            return "", reason.startswith("stale_bind:") or (
                reason == "managed_binding_names_no_session"
            )

        if _gate_uid:
            managed = self.hub.managed_mode.get_mode(
                project_root, host_session_id=_gate_hsid, strict=True
            )
            # #1027 THE AUTHORITY DOOR. `active` is DIAGNOSTIC and can report
            # True for a session that does not exist; this function DECIDES,
            # so it decides on the session that may actually act. `managed`
            # is kept for the trace and the session-id reads further down.
            _managed_sid, _managed_gate = _door(
                host_session_id=_gate_hsid,
                strict=True,
            )
        else:
            # #855 -- ASK WHO IS CALLING ON THE LOCAL PATH TOO.
            #
            # MEASURED 2026-08-20: a live session was refused a Write to its OWN
            # scratchpad as "ANOTHER session's", and the refusal MINTED A
            # SECURITY STRIKE (2/3 -- three freeze the session and need the very
            # operator whose absence the agent was working through).
            #
            # This branch was the cause. Local dispatch -- every Write/Edit from
            # a CLI conductor, i.e. the case that refused it -- carries no gate
            # principal, so get_mode was called with NO caller identity and fell
            # through to the DEPRECATED project singleton. `managed["session_id"]`
            # was then WHICHEVER SESSION BOUND THE PROJECT LAST, and everything
            # keyed off it downstream -- the `_host_session_ids` ownership bag,
            # the workspace-isolation verdict, the strike ledger -- answered a
            # question about the PROJECT when the caller had asked one about
            # ITSELF. With several sessions on one project (this repo carries
            # several) the caller's own workspace uuid is simply absent from that
            # bag, so the verdict is PROVEN FOREIGN on the caller's own
            # directory. Id rotation is not needed to reproduce it: two
            # concurrent sessions on one project are enough.
            #
            # #58 settled that per-conductor is authoritative and the singleton
            # is back-compat only, and #786 spent an entire investigation on this
            # same confusion. The managed-mode path was fixed then; the ISOLATION
            # path still resolved identity the old way and inherited the failure.
            #
            # SO: ask who is calling -- via `_gate_hsid`, the SAME
            # resolve_conductor_key() output the WebMCP branch uses, hoisted
            # above -- and ADOPT that answer only when it names an ACTIVE
            # session. strict=True so this second lookup can never hand the
            # singleton back a second time.
            #
            # THE KEY IS NOT RE-SPELLED HERE, and that is not a style point.
            # The first draft of this fix read current_calling_host_session_id()
            # into a local of its own, which is the #906 defect exactly: the row
            # key derived in a fourth place. #906 was measured twice in one day
            # in OPPOSITE directions (a web agent locked out, then 15 red smoke
            # checks after "fixing" it by repointing the reader), and its parity
            # tests pin `_gate_hsid` as the ONLY permitted source for a
            # host_session_id kwarg anywhere in this function. They caught the
            # draft. On the local surface the resolver's first rung IS that
            # stamp, so routing through it costs nothing and keeps one home.
            #
            # DELIBERATELY NOT a bare `get_mode(root, host_session_id=...)` in
            # place of the singleton call: a caller that IS named but has no
            # per-conductor binding leaves that returning INACTIVE, which would
            # trade a false "another session's workspace" for a false "managed
            # mode is not active" -- the same availability harm, wider. A caller
            # that cannot be resolved therefore keeps precisely the singleton it
            # has today (#672's carve-out: an actor claiming to be no one has
            # nothing substituted FOR it). Nothing loosens on either branch --
            # the caller's own session is a NARROWER and better-proven ownership
            # bag than the project's, never a wider one.
            managed = self.hub.managed_mode.get_mode(project_root)
            _managed_sid, _managed_gate = _door()
            if _gate_hsid:
                try:
                    _per_caller = self.hub.managed_mode.get_mode(
                        project_root, host_session_id=_gate_hsid, strict=True
                    )
                except Exception:  # noqa: BLE001 -- a resolver fault never authorizes
                    _per_caller = {}
                try:
                    # #1027 authority door -- SAME key (_gate_hsid, the
                    # #906-pinned source) and same strictness as the lookup
                    # above. The duck-typed isinstance guard below is kept
                    # deliberately: it is what the adoption test did before,
                    # and tightening or loosening it moves the verdict.
                    _per_sid, _per_gate = _door(
                        host_session_id=_gate_hsid,
                        strict=True,
                    )
                except Exception:  # noqa: BLE001 -- a resolver fault never authorizes
                    _per_sid, _per_gate = "", False
                if _per_sid and isinstance(_per_caller, dict) and _per_caller.get("active"):
                    managed = _per_caller
                    _managed_sid = _per_sid
                    _managed_gate = _per_gate
        _sec012_trace.managed_mode_active = bool(managed.get("active"))
        _sec012_trace.session_id = str(managed.get("session_id") or "")

        if not _managed_gate:
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
                    # #906 -- NAME THE KEY. This refusal used to be one sentence
                    # with no identity in it, which made a key mismatch between
                    # the writer and this reader UNFALSIFIABLE: connect answered
                    # `connected: true`, this answered "managed mode is not
                    # active", and nothing on either side said WHICH binding
                    # each was talking about. Diagnosing it took a live session
                    # and a code read; it should have taken one refusal.
                    #
                    # Same medicine as #557 (auth), #910 (the palace axis) and
                    # #914 (the trust skip): a refusal that cannot explain itself
                    # turns a five-minute fix into an unfalsifiable loop.
                    _why = (
                        "AIDOCS project detected but managed mode is not active. "
                        "Run /aidocs first to bind a session."
                    )
                    if _gate_uid:
                        _looked = _gate_hsid or "<none>"
                        _why = (
                            "managed mode is not active FOR THIS CALLER. Looked up "
                            f"the per-conductor binding under host_session_id "
                            f"'{_looked}' (gate principal '{_gate_uid}', key rung "
                            f"'{_key_rung}') and found none. That key comes from "
                            "resolve_conductor_key(), which every WRITER calls "
                            "too, so a binding made by ai_session(connect) or by "
                            "session_select for this principal should be visible "
                            "here -- if either reported success and this still "
                            # One literal, deliberately: an assertion reads this
                            # SOURCE, and splitting the phrase across two adjacent
                            # string literals hides it from that reader (#840).
                            "refuses, the two sides are resolving different identities"
                            " and THAT is the bug, not a missing bind. REMEDY: "
                            "call ai_session(mode='connect', session_id=...) in "
                            "this same conversation."
                        )
                        if _key_rung == "no_conversation_claim":
                            # A DIFFERENT CAUSE WITH A DIFFERENT REMEDY, and it
                            # must not read as "you forgot to connect": the
                            # caller is authenticated, but the request carried no
                            # conversation claim, so the gate stamped an honest
                            # empty and there is no conductor to look up. The
                            # fix belongs in the CLIENT, not in another connect.
                            _why += (
                                " NOTE: this request carried an authenticated "
                                "principal but no conversation claim, so no host "
                                "session could be composed and there is no key to "
                                "bind under. Send the conversation claim in "
                                "params._meta. Refusing rather than substituting "
                                "the account-wide id or reading the global "
                                "binding -- either would cross a tenant boundary, "
                                "and one user is not one host session."
                            )
                        elif not _gate_hsid:
                            _why += (
                                " NOTE: no host session could be resolved for this "
                                "request at all, so there is no conductor to look "
                                "up -- refusing rather than reading the global "
                                "binding, which would cross a tenant boundary."
                            )
                    return _sec012_finalize(
                        ToolDecision(
                            allowed=False,
                            reason=_why,
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
                # #279 cross-session scratchpad WRITE isolation. #266 approved
                # the whole <TEMP>/claude/ subtree as APPROVED_EXTERNAL_WORKSPACE,
                # so a write there no longer hits the sensitive-zone block below —
                # but a write into a SIBLING session's <slug>/<uuid>/ subtree is a
                # context-poisoning vector (the sibling re-reads its own outputs as
                # trusted). Reads are already session-bound by the host-read rail;
                # mirror that binding here for write/mutating tools. Fires BEFORE
                # (and regardless of) the zone chain so it also catches the
                # APPROVED_EXTERNAL_WORKSPACE fall-through. Own-session writes and
                # non-session-uuid paths pass (helper returns False).
                _norm_tool = self._normalized_tool(tool_name)
                _is_write_family = (
                    _norm_tool in ("write", "edit", "notebookedit")
                    or _norm_tool in MUTATING_MCP_TOOLS
                )
                if _is_write_family:
                    try:
                        from .session_artifact import (
                            OWNERSHIP_FOREIGN,
                            OWNERSHIP_LAUNDERED,
                            OWNERSHIP_UNESTABLISHED,
                            session_workspace_write_verdict,
                        )

                        _ws_verdict = session_workspace_write_verdict(
                            raw_target,
                            host_session_ids=self._host_session_ids(
                                project_root,
                                session_id_for_event,
                            ),
                        )
                    except Exception:
                        _ws_verdict = "unavailable"
                    # #672 C.3 — the WRITE-side counterpart of the read fix.
                    # MEASURED, not assumed: this detector was NEVER fail-open
                    # in production. `_host_session_ids` always prepends the
                    # AIDOCS session SLUG, so the id bag is non-empty even when
                    # no harness uuid is known; the old boolean therefore
                    # skipped its emptiness guard, compared the path uuid
                    # against a set holding nothing comparable, and returned
                    # True for EVERY workspace write — including the caller's
                    # OWN — while telling it the path was "ANOTHER session's".
                    # The BLOCK was correct ("unknown is not a pass"); the
                    # stated FACT was fabricated. So unestablished keeps
                    # refusing (nothing loosens, per #279) and gets its own
                    # blocked_by + message; `foreign` now means PROVEN foreign:
                    # a usable uuid existed and did not match.
                    if _ws_verdict == OWNERSHIP_UNESTABLISHED:
                        _sec012_trace.add(
                            "cross_session_scratchpad",
                            "block",
                            path=raw_target,
                            note="ownership unestablished (no usable host uuid)",
                        )
                        self._record_event(
                            project_root,
                            "cross_session_scratchpad_block",
                            tool_name,
                            "blocked",
                            session_id=session_id_for_event,
                            reason=f"workspace ownership unestablished: {raw_target}",
                        )
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=False,
                                reason=(
                                    f"Write blocked: `{raw_target}` is inside the "
                                    "managed per-session workspace "
                                    "(<TEMP>/claude/<slug>/<session-uuid>/) and "
                                    "AIDOCS could not establish who owns it: no "
                                    "harness session UUID is recorded for this "
                                    "session. This is NOT a finding that the path "
                                    "belongs to another session — ownership could "
                                    "not be checked either way, and an unproven "
                                    "write into this tree is refused because a "
                                    "sibling session re-reads its own workspace as "
                                    "trusted (#279). Owning UUIDs are stamped from "
                                    "the Claude Code hook payload, so if the hooks "
                                    "are not reaching AIDOCS nothing here is "
                                    "writable. Next: write under the project tree "
                                    "or your own scratchpad instead, or restore the "
                                    "hook path. If identity SHOULD be known here, "
                                    "file it: ai_backlog(mode='add', "
                                    "tags=['false-positive'], "
                                    "content='rule_id=cross_session_scratchpad "
                                    f"ownership unestablished for {raw_target}')."
                                ),
                                blocked_by="cross_session_scratchpad_unestablished",
                            ),
                        )
                    if _ws_verdict == OWNERSHIP_LAUNDERED:
                        _sec012_trace.add(
                            "cross_session_scratchpad",
                            "block",
                            path=raw_target,
                            note="path-laundering inside the session workspace",
                        )
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=False,
                                reason=(
                                    f"Write blocked: `{raw_target}` reaches into the "
                                    "managed per-session workspace through a `..` "
                                    "segment, so which session it lands in cannot be "
                                    "read off the path. Path-laundering is refused "
                                    "regardless of ownership. Next: write to the "
                                    "real, fully-resolved path."
                                ),
                                blocked_by="cross_session_scratchpad_laundered",
                            ),
                        )
                    if _ws_verdict == OWNERSHIP_FOREIGN:
                        _sec012_trace.add(
                            "cross_session_scratchpad",
                            "block",
                            path=raw_target,
                        )
                        self._record_event(
                            project_root,
                            "cross_session_scratchpad_block",
                            tool_name,
                            "blocked",
                            session_id=session_id_for_event,
                            reason=f"foreign session workspace: {raw_target}",
                        )
                        _sv = self._security_strike(
                            project_root,
                            session_id_for_event,
                            "cross_session_scratchpad",
                            tool_name,
                            raw_target,
                        )
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=False,
                                reason=self._augment_reason(
                                    f"Path `{raw_target}` is inside ANOTHER "
                                    "session's scratchpad/task workspace "
                                    "(<TEMP>/claude/<slug>/<other-session-uuid>/). "
                                    "A session may only write under its OWN "
                                    "session dir — cross-session writes are "
                                    "refused (context-poisoning guard, #279).",
                                    _sv,
                                ),
                                blocked_by="cross_session_scratchpad",
                            ),
                        )
                
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
                managed=_managed_gate,
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
        if allow_raw_shell and _managed_gate:
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
                managed=_managed_gate,
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
                # STAGED, not recorded (#582): the count rises in
                # _sec012_finalize and only on an ALLOW, so an invocation the
                # cascade refuses never spends retry budget it did not use.
                _pending_test_invocation.append((key, current_lane_id))
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

        # Canonical shell law (#319 / doctrine XXII): ai_run and every
        # native-shell transport consume the SAME [bash] policy. Native
        # execution is a capability/transport decision, never a second
        # namespace or authority. _JUDGE_DENYLIST trumps every allow.
        normalized_tool = tool_name.lower()

        def _command_read_gate() -> ToolDecision | None:
            """Command read-intent gate. A command that prints file CONTENT
            (cat .env / python -c "open('.env')" / base64 .env / sqlite3
            secrets.db .dump / cp <secret> /tmp) is a read in disguise — it
            must obey the SAME policy as the Read tool. Run each detected
            target through host_read_decision. This is the pre-execution
            half (spec D); run_output_guard is the content half. gate_enforce
            scoped (a managed-mode posture, like bash_policy).

            ONE definition, TWO call sites (#472 attempt-24 red): the main
            post-bash_policy sequence, AND inside bash_policy's ask branch —
            an ASK must never preempt this harder law, or `base64 .env` is
            one operator click from running. Returns the block decision
            (caller finalizes) or None on pass.
            """
            if normalized_tool not in ("bash", "ai_run") or not gate_enforce:
                return None
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
                return ToolDecision(
                    allowed=False,
                    reason=self._augment_reason(read_decision.reason, _sv),
                    blocked_by="command_read_intent",
                )
            _sec012_trace.add(
                "command_read_intent",
                "pass",
                targets=(len(read_decision.targets) if read_decision else 0),
            )
            return None

        if normalized_tool in ("bash", "ai_run") and gate_enforce:
            # #257: fail CLOSED on an UNAUTHENTICATED host session where auth is
            # actually required. The declarative bash allow-table is a PERSISTED
            # grant — it must NOT keep authorizing shell execution after the
            # operator's dashboard session (TTL) expires. Scoped so it never
            # breaks local usage: only the INTERACTIVE operator on a corpo
            # install must present live auth; local solo/dev (machine presence IS
            # the authority) and delegated subagent/lane callers (inherit the
            # conductor's auth) are exempt.
            _auth_refusal = self._bash_unauthenticated_refusal(
                project_root, session_id, tool_name, is_privileged_caller
            )
            if _auth_refusal is not None:
                return _sec012_finalize(_auth_refusal)

            from .bash_policy import (
                CANONICAL_SHELL_POLICY_NAMESPACE,
                evaluate_bash_policy,
                load_canonical_bash_policy,
            )

            policy_namespace = CANONICAL_SHELL_POLICY_NAMESPACE
            bash_policy = load_canonical_bash_policy(project_root=project_root)
            if not isinstance(bash_policy, dict) or not bash_policy:
                # Single-source contract: with NO canonical declarative [bash]
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
                # bash_policy is enforced for both the native Bash transport
                # (after its surface gate) and ai_run. Tool-level user_granted does NOT
                # lift the allowlist — only per-command subcommand
                # grants (flowing through `extras` / user_intent_
                # subcommands) can, and those require their own verb+
                # proximity check at grant time. Deny rules are never
                # bypassed.
                # LOCAL BACKLOG 984 — the ask rung, and ONLY the ask rung, may
                # be satisfied by a confirmation proved at the gate boundary.
                #
                # Placed HERE, before the refusal block, so that satisfying it
                # does not RETURN anything: control falls through into the rest
                # of check_tool exactly as it would had bash_policy allowed the
                # command outright, and the judge / tool_policy / infra / freeze
                # rungs below all still run and still decide. An `if confirmed:
                # return allow` at this point would have been a bypass wearing a
                # confirmation's clothes.
                #
                # The read law keeps its precedence (#472 attempt-24): the same
                # `_command_read_gate()` that outranks an ask is consulted
                # first, so `base64 .env` cannot become click-through by being
                # approved. It is evaluated twice on the refusal path — once
                # here, once in the branch below — which is a few microseconds
                # to keep ONE ordering rule in ONE place instead of restructuring
                # a hot security branch around a new feature.
                #
                # On a hook-capable host `current_gate_confirmation()` is never
                # set for shell, so this is inert there and local semantics are
                # unchanged by construction rather than by a flag.
                if (
                    not policy_decision["allowed"]
                    and policy_decision.get("verdict") == "ask"
                    and _command_read_gate() is None
                    and _bash_ask_rung_satisfied(tool_name, tool_input or {})
                ):
                    self._record_event(
                        project_root,
                        "bash_policy_ask_confirmed",
                        tool_name,
                        "allow",
                        session_id=session_id,
                        reason=str(policy_decision.get("reason") or ""),
                        matched_rule=matched,
                    )
                    self._journal_bash_decision(
                        project_root,
                        session_id,
                        str((tool_input or {}).get("command", "")),
                        "ask_confirmed",
                        f"{matched}: operator approved THIS invocation",
                    )
                    # One rung, satisfied. Not a grant: nothing is written back
                    # to policy, so the next identical command asks again.
                    policy_decision = {
                        **policy_decision,
                        "allowed": True,
                        "verdict": "ask_confirmed",
                    }

                if not policy_decision["allowed"]:
                    matched_rule = str(policy_decision.get("matched_rule") or "")
                    if policy_decision.get("verdict") == "ask":
                        # #472 attempt-24 red (VPS-caught): the read law
                        # outranks an ask. Without this, default=ask turned
                        # `base64 .env` / `sqlite3 secrets.db .dump` from a
                        # hard block into a one-click confirmation and the
                        # read gate never fired. Harder gate wins.
                        _read_block = _command_read_gate()
                        if _read_block is not None:
                            return _sec012_finalize(_read_block)
                        self._record_event(
                            project_root,
                            "bash_policy_ask",
                            tool_name,
                            "ask",
                            session_id=session_id,
                            reason=policy_decision["reason"],
                            matched_rule=matched_rule,
                        )
                        self._journal_bash_decision(
                            project_root,
                            session_id,
                            str((tool_input or {}).get("command", "")),
                            "ask",
                            f"{matched_rule}: {policy_decision['reason']}",
                        )
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=False,
                                needs_confirmation=True,
                                reason=str(policy_decision["reason"]),
                                blocked_by="bash_policy_ask",
                            ),
                        )
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
                            ) and not _first_destructive_denial(
                                session_id,
                                str((tool_input or {}).get("command", "")),
                            )
                        except Exception:
                            confirmable = False

                    _sec012_trace.add(
                        "bash_policy",
                        "confirmable" if confirmable else "block",
                        matched_rule=matched_rule,
                    )
                    # #100 FIX2: a resolved-binary/path-spoofing refusal gets a
                    # distinct audit kind (it is never confirmable — a spoofed
                    # binary must fail flat-closed, and it is neither deny-table
                    # nor a destructive-default hit, so `confirmable` is already
                    # False here).
                    _bp_event_kind = (
                        "bash_policy_path_mismatch"
                        if matched_rule.endswith(".path_mismatch")
                        else ("bash_policy_confirmable" if confirmable else "bash_policy_block")
                    )
                    self._record_event(
                        project_root,
                        _bp_event_kind,
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

        # Command read-intent gate — ONE definition above
        # (_command_read_gate), consumed here after bash_policy and inside
        # bash_policy's ask branch (#472: the read law outranks an ask).
        _read_block_main = _command_read_gate()
        if _read_block_main is not None:
            return _sec012_finalize(_read_block_main)

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

            # #448: SEMANTIC_CONTEXT is message-improvement-only BY
            # CONTRACT (safe_advisory; enrichment-never-weakens seam law).
            # It must never participate in the A/B/C dominant-class
            # computation — before this filter it fell into the default
            # Class C and flipped a credential-only verdict set into the
            # generic destructive path (is_credential_only went False).
            # It stays in judge.verdicts for audit/UX; SEMANTIC_GATE_WRITE
            # (a real ADDED refusal ground) still classifies normally.
            _gating_verdicts = [
                v for v in judge.verdicts if v.rule_id != "SEMANTIC_CONTEXT"
            ]

            # Classify unfiltered first.
            full_credential = [v for v in _gating_verdicts if _class_of(v) == "A"]
            full_sensitive = [v for v in _gating_verdicts if _class_of(v) == "B"]
            full_destructive = [v for v in _gating_verdicts if _class_of(v) == "C"]

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
                # ─── Path-B operator-approval lift (backlog #307) ───
                # When an operator APPROVES a bubbled escalation,
                # escalation_store.create_grant mints a live grant
                # scoped to (user, machine, session, permission). Honor
                # it HERE — before this gate re-blocks / re-bubbles the
                # destructive action — via check_live_grant_or_bubble,
                # which finds the live grant on the exact scope tuple
                # and consumes exactly one use (guarded UPDATE, never
                # over-consumed). Scope-narrow by design:
                #   - only this confirm-class destructive path (Class
                #     C). The sensitive-read hard floor (Class B), the
                #     credential flow, T0 raw-tool blocks, and the bash
                #     denylist / dangerous-chain / bash_policy refusals
                #     all return EARLIER in check_tool and can never
                #     reach this lift;
                #   - only the "run_destructive" permission the freeze
                #     bubble files escalations under (freeze_service.
                #     build_freeze_response);
                #   - exactly once — an expired or use-exhausted grant
                #     is invisible to find_live_grant, so the next
                #     identical attempt re-blocks and re-bubbles.
                if session_id and not credential_verdicts:
                    try:
                        import hashlib as _h_pathb

                        from . import host_concurrency_store, identity_resolver
                        from .escalation_hook import check_live_grant_or_bubble

                        # Same fingerprint convention as SEC-003: a
                        # grant minted WITH an operation fingerprint
                        # only lifts that exact command; a permission-
                        # only grant (no hash) lifts any command under
                        # its (user, machine, session, permission).
                        _pathb_cmd = str((tool_input or {}).get("command") or "").strip()
                        _pathb_hash = (
                            _h_pathb.sha256(
                                _pathb_cmd.encode("utf-8", "replace"),
                            ).hexdigest()[:32]
                            if _pathb_cmd
                            else None
                        )
                        _pathb_lift = check_live_grant_or_bubble(
                            project_root,
                            gate_permission="run_destructive",
                            session_id=session_id,
                            requester_user_id=identity_resolver.current_user_id(
                                project_root,
                            ),
                            machine_id=host_concurrency_store.machine_id(),
                            command_hash=_pathb_hash,
                        )
                    except Exception:
                        _pathb_lift = {"ok": False}
                    if isinstance(_pathb_lift, dict) and _pathb_lift.get("ok"):
                        _grant_id = str(_pathb_lift.get("grant_id") or "")
                        self._record_event(
                            project_root,
                            "judge_block_lifted_by_operator_grant",
                            tool_name,
                            "allowed",
                            session_id=session_id,
                            risk=top.risk,
                            reason=f"operator-approved grant {_grant_id} consumed",
                        )
                        if tool_name.lower() in ("bash", "ai_run"):
                            self._journal_bash_decision(
                                project_root,
                                session_id,
                                str((tool_input or {}).get("command", "")),
                                "allow",
                                "operator-approved escalation grant consumed "
                                f"({_grant_id}; retry-once)",
                            )
                        _sec012_trace.add(
                            "heuristic_judge",
                            "bypass",
                            reason="operator_grant_lift",
                            rule_id=str(getattr(top, "rule_id", "") or ""),
                        )
                        return _sec012_finalize(
                            ToolDecision(
                                allowed=True,
                                advisory="Operator-approved escalation grant "
                                "consumed — this retry passes once.",
                            ),
                        )
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
                            # The rule, not just the tier, so verdict_class can
                            # tell a tool-shape redirect (SHELL_WRITE_SOURCE)
                            # from a destructive act (BASH_RM_RF_ABSPATH).
                            matched_rule=str(getattr(top, "rule_id", "") or ""),
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
                        matched_rule=str(getattr(top, "rule_id", "") or ""),
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

        # #755/#756: canonical connect. This runs on EVERY hook event, and the
        # raw `with sqlite3.connect ... as conn:` it replaces is sqlite3's
        # TRANSACTION context manager — it committed and never CLOSED the
        # handle, so the hottest read in the orchestrator leaked one connection
        # per event. read_only=True is the truthful mode (this only SELECTs;
        # esc.init_db above has already created the file) and still carries
        # synchronous/busy_timeout/foreign_keys, which this reader had none of.
        from ._sqlite_connect import connect as _canonical_connect
        from .escalation_store import _iso_now, _row_to_grant

        now_iso = _iso_now()
        try:
            with _canonical_connect(esc.db_path(project_root), read_only=True) as conn:
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
            # Backlog #307: lift-class grants are consumed by their
            # LIFT paths, never by this additive bookkeeping.
            #   - "self_approve_confirm" (Path A) is consumed by
            #     freeze_service.consume_confirm_grant_if_matching at
            #     the top of check_tool;
            #   - "run_destructive" (Path B, operator approval) is
            #     consumed by escalation_hook.check_live_grant_or_
            #     bubble at the judge-block site.
            # Both run in the SAME check_tool pass as this matcher.
            # If bookkeeping spent the single max_uses first, the lift
            # would find nothing, the retry would re-block, and the
            # operator's approval would be silently burned.
            if (g.permission_name or "").strip().lower() in (
                "run_destructive",
                "self_approve_confirm",
            ):
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
            "ai_replace",
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
            # ai_replace(mode='anchor') carries its new text as
            # `replacement`, mode='symbol' as `new_body`. Omitting them
            # silenced the comment nudge on two of the four edit modes.
            "replacement",
            "new_body",
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
