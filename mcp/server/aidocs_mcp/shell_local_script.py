"""Governed project-local SCRIPT-FILE execution.

The governed [bash] profile already permits arbitrary project-local code via
the interpreter allow-list (`python script.py`, `node x.js`, `make`, `cargo`):
those run repo-owned code whose contents the policy never inspects. A
project-local SHELL SCRIPT FILE (`bash ./scripts/build.sh`,
`bash mcp/scripts/deploy_aidocs_gate.sh --full`) is the SAME threat class — so
the goal requires it to "work through local Bash" too.

What stays forbidden (this is the line that keeps it from being ungated bash):
  * ``bash -c '<inline>'`` / any flag BEFORE the script — that is an arbitrary
    inline shell command, the real bypass; rejected.
  * a bare ``sh`` / ``bash`` reading stdin or interactive — no script file.
  * a home (``~``) / remote (``http(s)://``) / ``..``-traversing script path.
  * any path — relative OR absolute — that lands outside the workspace.
  * any shell metacharacter (chain / redirection / subshell / expansion /
    glob / substitution / quote) in the invocation — those enable writes,
    network, and escape. (Chain operators are already split off upstream; a
    surviving ``$()`` / backtick is also caught by the dangerous-chain rule.)
  * a script path that does not resolve to an existing file UNDER the
    workspace — no pre-authorising a not-yet-written path, no escape.

This runs AFTER the unbypassable deny-table + dangerous-chain phases in
``evaluate_bash_policy``, so ``bash s.sh && rm -rf /`` (the ``rm`` segment
hits the deny table) and ``bash s.sh | sh`` (the second ``sh`` has no script)
stay denied. The script's INTERNAL commands are not individually gated — that
is inherent to running a script, and identical to the already-permitted
``python script.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPT_INTERPRETERS: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "dash", "ash", "ksh"},
)

# Leading `VAR=value ` env assignments (e.g. `AIDOCS_DEPLOY_SKIP_BINARY_SCAN=1
# bash …`) — stripped before interpreter detection, mirroring bash_policy.
_ENV_PREFIX_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")

# Metacharacters that enable chain / redirection / subshell / expansion /
# glob / substitution / quoting. '=' is intentionally allowed (it appears in
# benign `--opt=val` script args and was already consumed from the env
# prefix). '/' '.' '-' ':' are path/flag characters, not metacharacters.
_FORBIDDEN_CHARS = set(";&|`$()<>{}[]*?~!\\\n\r\t\"'")


def _basename(token: str) -> str:
    t = token.strip().replace("\\", "/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    if t.lower().endswith(".exe"):
        t = t[:-4]
    return t.lower()


def _bash_drive_spelling(norm: str, ws: Path) -> str:
    """Render bash's ``/d/proj/...`` drive spelling the way BASH reads it.

    On a Windows workspace the shell that will EXECUTE this command reads
    ``/d/x`` as ``D:/x``, so grading the literal ``/d/x`` measures a path the
    interpreter will never open. Bounded twice: only a single-letter first
    segment, and only when it names the workspace's OWN drive — so it can
    never redirect a token anywhere the containment check below would not
    already have had to approve. A no-op on POSIX (no drive on a workspace).
    """
    drive = ws.drive
    if not drive or not norm.startswith("/"):
        return norm
    parts = norm.split("/")
    if len(parts) >= 3 and len(parts[1]) == 1 and parts[1].isalpha():
        if parts[1].lower() == drive[0].lower():
            return drive + "/" + "/".join(parts[2:])
    return norm


def workspace_bounded_path(
    token: str,
    workspace_root: str | Path,
) -> tuple[Path | None, str]:
    """Canonical workspace bound for one file token in the governed families.

    Returns ``(resolved_path, "")`` when the token names a location INSIDE the
    workspace, else ``(None, reason)`` — and the reason names the PATH and
    where it actually landed, not merely the shape that was expected (#582).

    BOTH spellings of the same file are accepted: ``mcp/scripts/x.sh`` and
    ``D:/proj/mcp/scripts/x.sh`` (plus the bash ``/d/proj/...`` form on a
    Windows workspace). This is a bijection, NOT a widening — the reachable
    FILE set is identical, because every in-root absolute path names a file
    already nameable relatively. Refusing the absolute spelling bought no
    safety; the containment check below is the whole security boundary, and
    it applies to both spellings alike. The old pre-filter only punished the
    agent that followed its host tool's advice to prefer absolute paths, and
    the gate has no cwd awareness at all — so it was privileging the ONE form
    it cannot resolve over the form it can.

    Still refused: ``~`` home paths, ``http(s)://`` remotes, ``..`` traversal,
    and anything resolving outside the workspace. Existence is the caller's
    check — some callers legitimately accept a not-yet-created path.
    """
    raw = (token or "").strip()
    if not raw:
        return None, "empty path"
    norm = raw.replace("\\", "/")
    if norm.startswith(("http://", "https://")):
        return None, f"'{raw}' is a REMOTE url; only files inside the workspace are permitted"
    if norm.startswith("~"):
        return None, f"'{raw}' is a HOME path; only files inside the workspace are permitted"
    if ".." in norm.split("/"):
        return None, f"'{raw}' uses parent traversal ('..'), which is not permitted"
    try:
        ws = Path(workspace_root).resolve()
    except Exception:
        return None, "workspace root unresolvable"
    # "Rooted" = posix-absolute (/etc), a drive (C:/), or Path-absolute. On
    # Windows ``Path('/etc').is_absolute()`` is FALSE (no drive), so the
    # leading-'/' test is load-bearing: without it an absolute *nix path would
    # be joined onto the workspace and smuggle itself inside the bound.
    rooted = norm.startswith("/") or (len(raw) >= 2 and raw[1] == ":") or Path(raw).is_absolute()
    try:
        rp = Path(_bash_drive_spelling(norm, ws)).resolve() if rooted else (ws / raw).resolve()
    except Exception:
        return None, f"path '{raw}' unresolvable"
    if rp != ws and ws not in rp.parents:
        return None, (
            f"'{raw}' resolves to '{rp.as_posix()}', which is OUTSIDE the workspace "
            f"'{ws.as_posix()}'. Name a file inside the project — either relative to "
            f"the project root, or an absolute path under '{ws.as_posix()}'"
        )
    return rp, ""


def is_governed_local_script(
    segment: str,
    workspace_root: str | Path,
) -> tuple[bool, str]:
    """True for ``<shell-interpreter> <project-local-script-file> [args]``.

    Default-deny. Returns (ok, reason). See the module docstring for the full
    set of forms this deliberately refuses.
    """
    s = (segment or "").strip()
    if not s:
        return False, "empty segment"
    s2 = _ENV_PREFIX_RE.sub("", s).strip()
    if not s2:
        return False, "empty after env-prefix strip"
    # No metacharacters anywhere in the invocation.
    if any(ch in _FORBIDDEN_CHARS for ch in s2):
        return False, "shell metacharacter (chain/redirection/subshell/expansion) not permitted"
    toks = s2.split()
    if not toks:
        return False, "no tokens"
    if _basename(toks[0]) not in _SCRIPT_INTERPRETERS:
        return False, "not a shell interpreter"
    # First non-`--` token must be the script FILE — a flag before it
    # (``-c``/``-lc``/``-s``/``-x`` …) means inline/stdin, which is refused.
    script: str | None = None
    for tok in toks[1:]:
        if tok == "--":
            continue
        if tok.startswith("-"):
            return False, (
                f"shell flag '{tok}' before a script file — inline (`-c`) / stdin "
                "forms are not permitted; only `<shell> <project-local-script> [args]`"
            )
        script = tok
        break
    if script is None:
        return False, "no script file (interactive / stdin shell is not permitted)"
    rp, why = workspace_bounded_path(script, workspace_root)
    if rp is None:
        return False, f"script {why}"
    if not rp.is_file():
        return False, f"script '{script}' is not an existing file"
    return True, f"governed project-local script: {script}"


# Parse-only flags for `bash -n` syntax checks (#327). `-n` = read-commands-but-
# do-NOT-execute (a pure syntax check); `--norc`/`--noprofile` only suppress
# startup files. NONE of these can execute the script. `-c` (inline code), `-s`
# (stdin), `-i` (interactive), `-x` (trace), `-O`/`-o` (behavior), and any
# combined short-flag are deliberately absent → they fall through to default.
_PARSE_ONLY_FLAGS: frozenset[str] = frozenset({"-n", "--norc", "--noprofile"})


def is_governed_parse_check(
    segment: str,
    workspace_root: str | Path,
) -> tuple[bool, str]:
    """True for ``bash -n [--norc --noprofile] <project-local-script>`` — a
    PARSE-ONLY syntax check that never executes the script.

    Strictly safer than the already-permitted execution form
    (``is_governed_local_script``): ``-n`` reads commands but does not run them,
    so even a script full of ``rm -rf /`` is only parsed. Default-deny:

      * ``-n`` is MANDATORY — its absence would be EXECUTION (that path is the
        execution helper's job, not this one).
      * every flag must be in ``_PARSE_ONLY_FLAGS`` — so ``-c`` / ``-s`` / ``-i``
        / ``-x`` / a combined ``-in`` are refused (they execute / read stdin /
        go interactive).
      * exactly one project-local script FILE follows, workspace-bounded, with
        the SAME metachar / absolute / traversal / must-exist floors as the
        execution form.

    Runs AFTER the unbypassable deny-table + dangerous-chain phases in
    ``evaluate_bash_policy``, so ``bash -n s.sh && rm -rf /`` still denies. Same
    verdict on raw Bash and ai_run (one shared core, empire-doctrine XXII).
    """
    s = (segment or "").strip()
    if not s:
        return False, "empty segment"
    s2 = _ENV_PREFIX_RE.sub("", s).strip()
    if not s2:
        return False, "empty after env-prefix strip"
    if any(ch in _FORBIDDEN_CHARS for ch in s2):
        return False, "shell metacharacter (chain/redirection/subshell/expansion) not permitted"
    toks = s2.split()
    if not toks:
        return False, "no tokens"
    if _basename(toks[0]) not in _SCRIPT_INTERPRETERS:
        return False, "not a shell interpreter"
    saw_n = False
    script: str | None = None
    for tok in toks[1:]:
        if tok == "--":
            continue
        if tok.startswith("-"):
            if tok not in _PARSE_ONLY_FLAGS:
                return False, (
                    f"flag '{tok}' is not parse-only — only `-n` (syntax check), "
                    "`--norc`, `--noprofile` are permitted; -c/-s/-i/-x and "
                    "combined short-flags execute and are refused"
                )
            if tok == "-n":
                saw_n = True
            continue
        script = tok
        break
    if not saw_n:
        return False, "not a parse-only check ('-n' absent — this would EXECUTE)"
    if script is None:
        return False, "no script file (a bare '-n' parses stdin, not a bounded file)"
    rp, why = workspace_bounded_path(script, workspace_root)
    if rp is None:
        return False, f"script {why}"
    if not rp.is_file():
        return False, f"script '{script}' is not an existing file"
    return True, f"governed parse-only syntax check: {script}"


# awk safe-shape (#313). awk/gawk/mawk are FULL languages that can exec
# (`system`, `"cmd"|getline`, `print|"cmd"`), reach the network (gawk `/inet/`),
# write files (`print > file`), load code (`-f`, gawk `@load`/`--source`), and
# leak env (`ENVIRON`). A blacklist of arg patterns cannot cover that surface
# safely, so this is a POSITIVE safe-shape allow: a text-extraction program with
# NONE of those constructs, only -F/-v flags, reading stdin or workspace-bounded
# files. Everything else falls through to default-block.
_AWK_INTERPRETERS: frozenset[str] = frozenset({"awk", "gawk", "mawk", "nawk"})

# Substrings that make an awk PROGRAM execute / pipe / network / write / load /
# leak. Matched case-insensitively against the shlex-extracted program token.
# `>` / `<` also catch numeric comparisons — a deliberate deny-side false
# positive (safe): a comparison-using awk is refused, never wrongly allowed.
_AWK_PROGRAM_DANGER: tuple[str, ...] = (
    "system",
    "getline",
    "|",
    "/inet/",
    ">",
    "<",
    "@load",
    "@include",
    "environ",
)


def _awk_flag_ok(tok: str) -> bool:
    """Only -F<sep> (field separator) and -v var=val (variable) are safe: their
    VALUES are data, never a program. -f/--file/-e/--source/-i/-l/--load/--exec
    and any other flag can load or run code and are refused."""
    return tok in ("-F", "-v") or tok.startswith("-F") or tok.startswith("-v")


def is_governed_awk_safe(
    segment: str,
    workspace_root: str | Path,
) -> tuple[bool, str]:
    """True for a PROVABLY-SAFE awk text-extraction invocation:
    ``awk|gawk|mawk [-F<sep>] [-v var=val] '<program>' [workspace-file ...]``
    where the program contains NO exec/pipe/network/write/load/env construct.

    Default-deny. Runs AFTER the unbypassable deny-table + dangerous-chain +
    substitution + heredoc phases in ``evaluate_bash_policy`` (so a surviving
    ``$()`` / ``| sh`` / ``&& rm`` was already refused). Same verdict on raw
    Bash and ai_run (one shared core, empire-doctrine XXII).
    """
    import shlex

    s = (segment or "").strip()
    if not s:
        return False, "empty segment"
    s2 = _ENV_PREFIX_RE.sub("", s).strip()
    if not s2:
        return False, "empty after env-prefix strip"
    try:
        toks = shlex.split(s2)
    except ValueError:
        return False, "unparseable awk invocation (unbalanced quotes)"
    if not toks:
        return False, "no tokens"
    if _basename(toks[0]) not in _AWK_INTERPRETERS:
        return False, "not an awk interpreter"

    program: str | None = None
    files: list[str] = []
    i = 1
    n = len(toks)
    while i < n:
        tok = toks[i]
        if tok == "--":
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            if not _awk_flag_ok(tok):
                return False, (
                    f"awk flag '{tok}' is not in the safe set (-F / -v only); "
                    "-f/--file/-e/--source/-i/-l load or run code and are refused"
                )
            # -F / -v with a SEPARATE value consume the next token as data.
            if tok in ("-F", "-v"):
                i += 2
                continue
            i += 1
            continue
        if program is None:
            program = tok
        else:
            files.append(tok)
        i += 1

    if program is None:
        return False, "no awk program"
    low = program.lower()
    for bad in _AWK_PROGRAM_DANGER:
        if bad in low:
            return False, (
                f"awk program contains '{bad}' — exec / pipe / network / file / "
                "load / env constructs are not the safe text-extraction shape"
            )

    # Input files (if any) must be workspace-bounded existing files — an awk that
    # reads an out-of-workspace file would leak its content to stdout.
    for f in files:
        rp, why = workspace_bounded_path(f, workspace_root)
        if rp is None:
            return False, f"awk input {why}"
        if not rp.is_file():
            return False, f"awk input '{f}' is not an existing file"

    return True, f"governed safe awk text-extraction: {program[:40]}"
