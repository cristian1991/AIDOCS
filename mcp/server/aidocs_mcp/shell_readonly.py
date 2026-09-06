"""Governed read-only native-bash classifier (Batch 2.0-B1).

DEMOTED 2026-06-05 — capability-routing EVIDENCE, not an authorization
gate. Native execution is the normal governed shell surface gated by the
canonical ai_run law (see shell_adapter). `is_read_only_safe` + the bounded
catalog are used as EVIDENCE that a command's output is host-replaceable and
that a path-silent host may use the operator-pinned READ-ONLY self-bind; the
`tools.native_shell_readonly_enabled` enable flag no longer gates (the master
switch is `tools.shell_enforcement_live`). Do not reintroduce it as an
authorization gate.

Expands native execution from the tiny B0 pilot allowlist to a broader
GOVERNED READ-ONLY class — read-only inspection commands the way ai_run
users actually use them — WITHOUT allowing writes, network, scripts,
package managers, or shell escapes. This is the safety delta on top of the
core law: a command only reaches here when ShellEnforcement already
returned execute_native (⇒ enabled + capability-safe + output replacement +
proven identity + the SAME bash_policy/judge/read-bypass/egress law ai_run
runs). This module additionally bounds it to read-only.

Design = DEFAULT DENY + EXTENSIBLE base:
  * reject any shell metacharacter (chain / redirection / subshell /
    expansion / glob) — these enable writes, network, and escape;
  * reject parent traversal and explicit binary paths;
  * an IMMUTABLE hard-deny family (writes / network / scripts / eval /
    package managers / secret-env dumps) is refused ALWAYS — operator
    config can never re-enable it;
  * the binary (first token) MUST be in the read-only allowlist =
    base set ∪ operator EXTRA set (tools.native_shell_readonly_extra_
    commands), minus the hard-deny family — so the policy is extensible
    without weakening the doctrine;
  * per-binary guards reject the write/exec escape flags of otherwise
    read-only tools (sort -o, find -exec/-delete, git write subcommands).

File-READING commands (cat/head/grep/…) remain governed by the existing
read-bypass law (command_read_intent) at the core-law layer — secrets /
undiscovered source are blocked there, before this classifier is reached.
Output is still receipted + guarded + capped post-exec. ai_run remains the
canonical provider for anything outside this class (and for forms using
metacharacters / =, which route to ai_run).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# ── Finite bound caps (truly-bounded doctrine) ──────────────────────
# Output is already capped universally by the B0.1 receipt (64 KiB). These
# bound the WORK of commands that can otherwise walk unboundedly even with
# capped output (history walks, traversal, large count flags).
_MAX_LINES = 10_000  # head/tail -n, grep -A/-B/-C context
_MAX_BYTES = 1_000_000  # head/tail -c
_MAX_DEPTH = 8  # find -maxdepth, du -d, tree -L
_MAX_GIT_COUNT = 1_000  # git log/rev-list/... -n


def _collect_values(args: list[str], flags: tuple[str, ...]) -> list[str]:
    """Values given to numeric flags. Handles '-n 5', '-n5',
    '--lines 5'. ('--flag=value' forms are pre-rejected by the metachar
    boundary, so '=' never appears here.)
    """
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        for f in flags:
            if a == f:
                out.append(args[i + 1] if i + 1 < len(args) else "")
                i += 1
                break
            if (not f.startswith("--")) and a.startswith(f) and len(a) > len(f):
                out.append(a[len(f) :])
                break
        i += 1
    return out


def _bare_counts(args: list[str]) -> list[str]:
    """Bare '-<int>' count tokens (head -100, git log -5)."""
    return [a[1:] for a in args if re.fullmatch(r"-\d+", a)]


def _all_in_bound(values: list[str], cap: int) -> tuple[bool, str]:
    """Every value must be a positive int <= cap. Empty values list is OK
    (the caller decides whether a bound is REQUIRED).
    """
    for v in values:
        if not re.fullmatch(r"\d+", v or ""):
            return False, f"non-numeric / missing count ({v!r})"
        n = int(v)
        if n <= 0 or n > cap:
            return False, f"count {n} out of bound (1..{cap})"
    return True, ""


# Same strict boundary as the B0 pilot: any of these disqualifies the
# command (blocks chaining, redirection/writes, subshell/network, env
# assignment, expansion/globs).
_FORBIDDEN_CHARS = set(";&|`$()<>{}[]*?~!\\=\n\r\t\"'")

# IMMUTABLE hard-deny family. These are NEVER native-eligible, even if an
# operator adds them to tools.native_shell_readonly_extra_commands. This is
# what keeps "no writes / network / scripts / package managers" true
# regardless of config — the policy is extensible, but not weakenable.
_HARD_DENY_BINARIES: frozenset[str] = frozenset(
    {
        # network
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "telnet",
        "rsync",
        "socat",
        "ssh-keygen",
        "ssh-add",
        # scripts / eval / dynamic exec
        "bash",
        "sh",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "python",
        "python2",
        "python3",
        "node",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
        "rscript",
        "osascript",
        "powershell",
        "pwsh",
        "cmd",
        "wsl",
        "eval",
        "exec",
        "source",
        "xargs",
        "sed",
        "awk",
        "gawk",
        # secret / environment dumps + shell state
        "env",
        "printenv",
        "export",
        "set",
        "declare",
        # package managers / builders
        "npm",
        "pnpm",
        "yarn",
        "npx",
        "pip",
        "pip3",
        "pipx",
        "poetry",
        "pipenv",
        "conda",
        "cargo",
        "go",
        "dotnet",
        "gradle",
        "mvn",
        "make",
        "cmake",
        "ninja",
        "docker",
        "podman",
        "kubectl",
        "helm",
        "terraform",
        "ansible",
        "gcc",
        "g++",
        "clang",
        "ld",
        # writes / destructive / privilege
        "rm",
        "rmdir",
        "mv",
        "cp",
        "dd",
        "tee",
        "truncate",
        "touch",
        "mkdir",
        "ln",
        "install",
        "chmod",
        "chown",
        "chgrp",
        "mkfs",
        "mount",
        "umount",
        "shred",
        "patch",
        "sudo",
        "su",
        "doas",
    },
)

# find action flags that write or execute — disqualify.
_FIND_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        "-exec",
        "-execdir",
        "-delete",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    },
)

# Unbounded forms refused for EVERY command: follow/stream (logs) and
# recursive scans. No native read-only command needs these — they are the
# "unbounded scanners/logs" the policy forbids.
_FOLLOW_FLAGS: frozenset[str] = frozenset({"-f", "-F", "--follow"})
_RECURSE_FLAGS: frozenset[str] = frozenset({"-R", "--recursive"})


def _v_generic(args: list[str]) -> tuple[bool, str]:
    for a in args:
        if a in _FOLLOW_FLAGS:
            return False, f"{a} is a follow/stream form (unbounded log)"
        if a in _RECURSE_FLAGS:
            return False, f"{a} is a recursive form (unbounded scan)"
    return True, ""


def _v_sort(args: list[str]) -> tuple[bool, str]:
    ok, r = _v_generic(args)
    if not ok:
        return ok, r
    for a in args:
        if a in ("-o", "--output") or (a.startswith("-o") and len(a) > 2):
            return False, "sort -o writes a file"
    return True, ""


def _v_count(args: list[str]) -> tuple[bool, str]:
    """head/tail: default count is finite; any -n/-c/bare-count must be a
    finite value within bounds (large count flags denied).
    """
    ok, r = _v_generic(args)
    if not ok:
        return ok, r
    lines = _collect_values(args, ("-n", "--lines")) + _bare_counts(args)
    ok, r = _all_in_bound(lines, _MAX_LINES)
    if not ok:
        return False, f"line {r}"
    byts = _collect_values(args, ("-c", "--bytes"))
    ok, r = _all_in_bound(byts, _MAX_BYTES)
    if not ok:
        return False, f"byte {r}"
    return True, ""


def _v_grep(args: list[str]) -> tuple[bool, str]:
    ok, r = _v_generic(args)
    if not ok:
        return ok, r
    for a in args:
        if a in ("-r", "-R", "--recursive", "--dereference-recursive", "-d"):
            return False, "recursive grep is an unbounded scan"
    # Bound context-line flags (-A/-B/-C N) — large counts denied.
    ctx = _collect_values(
        args,
        ("-A", "-B", "-C", "--after-context", "--before-context", "--context"),
    )
    ok, r = _all_in_bound(ctx, _MAX_LINES)
    if not ok:
        return False, f"grep context {r}"
    return True, ""


def _v_find(args: list[str]) -> tuple[bool, str]:
    for a in args:
        if a in _FIND_FORBIDDEN_FLAGS:
            return False, f"find {a} executes/writes"
    depths = _collect_values(args, ("-maxdepth",))
    if not depths:
        return False, f"find must be depth-bounded (-maxdepth N, N<={_MAX_DEPTH})"
    ok, r = _all_in_bound(depths, _MAX_DEPTH)
    if not ok:
        return False, f"find -maxdepth {r}"
    return True, ""


def _v_du(args: list[str]) -> tuple[bool, str]:
    ok, r = _v_generic(args)
    if not ok:
        return ok, r
    if any(a.startswith("-s") or a == "--summarize" for a in args):
        return True, ""  # summary is inherently bounded
    depths = _collect_values(args, ("-d", "--max-depth"))
    if not depths:
        return False, f"du must be bounded (-s or -d N, N<={_MAX_DEPTH})"
    ok, r = _all_in_bound(depths, _MAX_DEPTH)
    if not ok:
        return False, f"du depth {r}"
    return True, ""


def _v_tree(args: list[str]) -> tuple[bool, str]:
    ok, r = _v_generic(args)
    if not ok:
        return ok, r
    depths = _collect_values(args, ("-L",))
    if not depths:
        return False, f"tree must be depth-bounded (-L N, N<={_MAX_DEPTH})"
    ok, r = _all_in_bound(depths, _MAX_DEPTH)
    if not ok:
        return False, f"tree -L {r}"
    return True, ""


# git subcommand categories (truly-bounded). git grep removed — it is an
# unbounded repo scanner; use ai_run.
_GIT_HISTORY = frozenset(
    {
        "log",
        "shortlog",
        "reflog",
        "whatchanged",
        "rev-list",
    },
)
_GIT_PATCH = frozenset({"diff", "show"})
_GIT_SUMMARY_FLAGS = frozenset(
    {
        "--stat",
        "--numstat",
        "--shortstat",
        "--name-only",
        "--name-status",
        "--dirstat",
        "--summary",
    },
)
# Inherently finite (working-tree / object lookups), output-capped.
_GIT_BOUNDED = frozenset(
    {
        "status",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "cat-file",
        "describe",
        "symbolic-ref",
        "for-each-ref",
    },
)

# git GLOBAL options that change repo / config / execution / scope context
# (or escape the project). Denied anywhere — native git must run finite and
# project-local in the current repo. (-c key=val is also blocked by the
# metacharacter boundary, but denied explicitly here too.)
_GIT_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "-C",
    "--git-dir",
    "--work-tree",
    "--exec-path",
    "--namespace",
    "-c",
    "--config-env",
    "--no-index",
    # hidden write (--output FILE, space form) + pager spawn (hang/noise)
    "--output",
    "--paginate",
)

# Full-patch output flags — denied for every subcommand (no patch dumps).
_GIT_PATCH_FLAGS: frozenset[str] = frozenset(
    {
        "-p",
        "--patch",
        "--patch-with-stat",
        "--patch-with-raw",
        "-u",
        "--unified",
    },
)


def _git_forbidden_token(args: list[str]) -> str | None:
    for a in args:
        for tok in _GIT_FORBIDDEN_TOKENS:
            if a == tok:
                return tok
            # combined short form (-C<path>, -c<x>)
            if not tok.startswith("--") and a.startswith(tok):
                return tok
            # long form with attached value (already blocked by '=', belt)
            if tok.startswith("--") and a.startswith(tok + "="):
                return tok
    return None


def _v_git(args: list[str]) -> tuple[bool, str]:
    # Context-changing global options → deny (repo/config/exec/scope escape).
    bad = _git_forbidden_token(args)
    if bad is not None:
        return False, (
            f"git global option {bad} is not allowed (changes repo / config "
            "/ execution context or escapes the project)"
        )
    # No full-patch output, ever.
    for a in args:
        if a in _GIT_PATCH_FLAGS or a.startswith("-U"):
            return False, (
                f"git full-patch output ({a}) is not allowed — use a bounded "
                "summary (--stat / --name-only / …)"
            )
    sub = ""
    for a in args:
        if not a.startswith("-"):
            sub = a.lower()
            break
    if not sub:
        return False, "git requires a read-only subcommand"
    if sub in _GIT_HISTORY:
        counts = _collect_values(args, ("-n", "--max-count")) + _bare_counts(args)
        if not counts:
            return False, (
                f"git {sub} must be count-bounded (-n N, N<={_MAX_GIT_COUNT}) "
                "— unbounded history walk denied"
            )
        ok, r = _all_in_bound(counts, _MAX_GIT_COUNT)
        if not ok:
            return False, f"git {sub} {r}"
        return True, ""
    if sub in _GIT_PATCH:
        if not any(f in args for f in _GIT_SUMMARY_FLAGS):
            return False, (
                f"git {sub} must use a bounded summary "
                "(--stat / --name-only / --numstat / …), not a full patch"
            )
        return True, ""
    if sub == "blame":
        if not any(a == "-L" or a.startswith("-L") for a in args):
            return False, "git blame must be line-bounded (-L start,end)"
        return True, ""
    if sub in _GIT_BOUNDED:
        return True, ""
    return False, f"git {sub} is not a bounded read-only subcommand"


# ── Validator CATALOG ───────────────────────────────────────────────
# EVERY native-eligible command MUST have a validator here. A command with
# no validator can never be enabled (not even via extra) — "no extra
# command without a validator". Each validator bounds the command (rejects
# unbounded scanners/logs and write/exec escapes).
_VALIDATORS: dict[str, Any] = {
    # bounded by nature (finite file/object input + universal output cap)
    **dict.fromkeys(
        (
            "pwd",
            "whoami",
            "hostname",
            "uname",
            "arch",
            "id",
            "uptime",
            "tty",
            "date",
            "df",
            "stat",
            "file",
            "basename",
            "dirname",
            "realpath",
            "readlink",
            "which",
            "type",
            "wc",
            "cat",
            "nl",
            "tac",
            "uniq",
            "cut",
            "comm",
            "cmp",
            "diff",
            "column",
            "fold",
            "rev",
            "tr",
            "ls",
        ),
        _v_generic,
    ),
    # finite default count; large -n/-c flags denied
    "head": _v_count,
    "tail": _v_count,
    "sort": _v_sort,
    "grep": _v_grep,
    "egrep": _v_grep,
    "fgrep": _v_grep,
    "rg": _v_grep,  # ripgrep — read-only search; grep-like context bounding
    "find": _v_find,
    "du": _v_du,
    "tree": _v_tree,
    "git": _v_git,
}

# Default-ENABLED subset of the catalog. Traversal scanners (find, du,
# tree) are catalog-validated but NOT on by default — the operator must
# enable them via extra (operator-owned). rg/awk/etc. are NOT in the
# catalog (no bounded validator), so they can never be enabled here.
_BASE_COMMANDS: frozenset[str] = frozenset(
    {
        "pwd",
        "whoami",
        "hostname",
        "uname",
        "arch",
        "id",
        "uptime",
        "tty",
        "date",
        "df",
        "stat",
        "file",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "which",
        "type",
        "wc",
        "cat",
        "head",
        "tail",
        "nl",
        "tac",
        "uniq",
        "cut",
        "comm",
        "cmp",
        "diff",
        "column",
        "fold",
        "rev",
        "tr",
        "ls",
        "sort",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "git",
    },
)


# Universal argument escapes denied for EVERY command (not just per-binary):
#   --files0-from F : reads a NUL-list of filenames from F and processes
#       their contents — a read INDIRECTION that bypasses the cmdline
#       read-bypass target detection (sort/wc/du support it).
_ESCAPE_ARG_FLAGS: frozenset[str] = frozenset({"--files0-from"})
# Special filesystems whose reads are streaming/unbounded/hang-prone noise.
_SPECIAL_FS_PREFIXES: tuple[str, ...] = ("/dev/", "/proc/", "/sys/")
_SPECIAL_FS_EXACT: frozenset[str] = frozenset({"/dev", "/proc", "/sys"})


def _universal_arg_guard(args: list[str]) -> tuple[bool, str]:
    for a in args:
        if a in _ESCAPE_ARG_FLAGS:
            return False, (
                f"{a} is a file-list indirection that bypasses read-bypass target detection"
            )
        if a in _SPECIAL_FS_EXACT or a.startswith(_SPECIAL_FS_PREFIXES):
            return False, (
                f"reading special filesystem path {a!r} is not allowed (streaming/unbounded noise)"
            )
    return True, ""


def has_validator(binary: str) -> bool:
    return (binary or "").strip().lower() in _VALIDATORS


def is_read_only_safe(
    command: str,
    extra_allowed: frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    """True for the governed, BOUNDED read-only class. Default-deny.

    A command is eligible only if its binary has a validator in the catalog
    AND is enabled (base set ∪ operator extra), the validator bounds it,
    and no hard-deny / metacharacter / path rule fires. ``extra_allowed``
    is the operator's catalog-filtered extra set (already lower-cased).
    """
    c = (command or "").strip()
    if not c:
        return False, "empty command"
    if any(ch in _FORBIDDEN_CHARS for ch in c):
        return False, (
            "shell metacharacter / chain / redirection / expansion is not "
            "allowed for native read-only execution"
        )
    if ".." in c:
        return False, "parent traversal is not allowed"
    toks = c.split()
    raw_binary = toks[0]
    if "/" in raw_binary:
        return False, "explicit binary path is not allowed (bare name only)"
    binary = raw_binary.lower()
    # Immutable hard-deny — config can never re-enable these.
    if binary in _HARD_DENY_BINARIES:
        return False, (
            f"'{binary}' is permanently denied for native execution "
            "(writes / network / scripts / package managers / secret-env)"
        )
    if binary not in _VALIDATORS:
        return False, (
            f"no read-only validator exists for '{binary}' — it cannot be "
            "enabled for native execution (use ai_run)"
        )
    if binary not in _BASE_COMMANDS and binary not in extra_allowed:
        return False, (
            f"'{binary}' is catalog-validated but not enabled "
            "(operator must enable it via tools.native_shell_readonly_"
            "extra_commands)"
        )
    # Universal arg escapes (indirection / special-fs) apply to every cmd.
    ok, reason = _universal_arg_guard(toks[1:])
    if not ok:
        return False, reason
    ok, reason = _VALIDATORS[binary](toks[1:])
    if not ok:
        return False, reason
    return True, f"governed read-only: {binary}"


# The governed read-only family that runs through the GOVERNED shell
# (ai_run) is the read-only catalog PLUS the bounded traversal scanner
# `find`. find is intentionally NOT in _BASE_COMMANDS (native B1 keeps
# traversal scanners operator-opt-in for noise/perf reasons), but its
# _v_find validator already mandates a depth bound and rejects
# -exec/-delete/-name escapes, and the workspace bound below adds path
# containment — so it is safe to include in the GOVERNED family the goal
# documents (grep/rg/find/cat/head/tail/git-status). du/tree stay opt-in.
_GOVERNED_FAMILY_EXTRA: frozenset[str] = frozenset({"find"})


def is_governed_readonly(
    command: str,
    workspace_root: str | Path,
    extra_allowed: frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    """is_read_only_safe (governed family) + WORKSPACE-BOUNDED path checks.

    The governed read-only family is usable through the governed shell
    (ai_run) for in-workspace reads only: every command must pass the
    bounded read-only catalog (base set ∪ ``find`` ∪ operator extra) AND
    every path argument must stay inside the workspace (no absolute path
    escaping it, no `..` traversal). Network binaries (curl/wget/ssh/…)
    are hard-denied by the catalog, so egress can never ride this path.
    Default-deny.

    Parsing note: ``command.split()`` is sufficient for this bounded,
    metacharacter-free family (is_read_only_safe rejects quoting/chains/
    redirection up front). If the shell law ever grows to richer commands,
    switch to shlex/argv parsing. Known accepted limitation: a grep/rg
    *search pattern* that happens to look like an absolute path is treated
    as a path and may be over-restricted — a usability cost, not a hole.
    """
    ok, reason = is_read_only_safe(command, extra_allowed | _GOVERNED_FAMILY_EXTRA)
    if not ok:
        return False, reason
    try:
        ws = Path(workspace_root).resolve()
    except Exception:
        return False, "workspace root unresolvable"
    toks = command.split()
    for tok in toks[1:]:
        if tok.startswith("-"):
            continue  # flag, not a path
        norm = tok.replace("\\", "/")
        # `..` traversal anywhere in a path token escapes the workspace.
        if ".." in norm.split("/"):
            return False, f"path '{tok}' uses parent traversal (..)"
        # "Rooted" = posix-absolute (/etc), a drive (C:\), or Path-absolute.
        # NB: on Windows ``Path('/etc').is_absolute()`` is FALSE (no drive),
        # so the leading-'/' check is essential — without it an absolute
        # *nix path slips through the workspace bound on Windows.
        rooted = (
            norm.startswith("/") or (len(tok) >= 2 and tok[1] == ":") or Path(tok).is_absolute()
        )
        cand = Path(tok) if rooted else (ws / tok)
        try:
            rp = cand.resolve()
        except Exception:
            return False, f"path '{tok}' unresolvable"
        if rp != ws and ws not in rp.parents:
            return False, (
                f"path '{tok}' resolves outside the workspace {ws} — "
                "governed reads are workspace-bounded"
            )
    return True, "governed read-only (workspace-bounded)"


def _readonly_flag(project_root: Path) -> bool:
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "tools.native_shell_readonly_enabled",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


def _extra_commands(project_root: Path) -> frozenset[str]:
    """Operator-extended read-only binaries (tools.native_shell_readonly_
    extra_commands), lower-cased and FILTERED against the immutable
    hard-deny family — an operator cannot smuggle curl/rm/python in here.
    """
    try:
        from .config import get_setting

        raw = str(
            get_setting(
                "tools.native_shell_readonly_extra_commands",
                project_root=project_root,
                default="",
            )
            or "",
        ).strip()
    except Exception:
        return frozenset()
    if not raw:
        return frozenset()
    out: set[str] = set()
    for chunk in raw.replace(";", os.pathsep).replace(",", os.pathsep).split(os.pathsep):
        name = chunk.strip().lower()
        if not name or "/" in name or name in _HARD_DENY_BINARIES:
            continue
        # No extra command without a validator: only catalog commands may
        # be enabled. A name with no bounded validator is dropped.
        if name not in _VALIDATORS:
            continue
        out.add(name)
    return frozenset(out)


def readonly_native_allowed(
    project_root: Path,
    envelope: Any,
) -> tuple[bool, str]:
    """2.0-B1 conditions BEYOND an execute_native verdict: the read-only
    flag, host=claude_code, provider=bash, and a governed read-only
    command (base ∪ operator-extra, minus hard-deny). Fails closed.
    """
    try:
        if not _readonly_flag(project_root):
            return False, "native_shell_readonly_enabled is off"
        if getattr(envelope, "host", "") != "claude_code":
            return False, "read-only native host must be claude_code"
        if getattr(envelope, "provider", "") != "bash":
            return False, "read-only native provider must be bash"
        extra = _extra_commands(project_root)
        return is_read_only_safe(
            getattr(envelope, "command", "") or "",
            extra,
        )
    except Exception as exc:
        return False, f"read-only check error: {type(exc).__name__}"
