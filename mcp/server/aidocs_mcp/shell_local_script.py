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
    path_base: str | Path | None = None,
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

    ``path_base`` (#1166) is where a RELATIVE token resolves from: the host
    shell's real cwd. The BOUND stays ``workspace_root`` -- a cwd inside
    another project never widens what this window may touch (r0b1
    34648e9f-0d6: never launder another project's scripts under this one's
    policy). Absent, relative tokens resolve against the workspace as before.
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
        base = Path(path_base).resolve() if path_base else ws
        rp = Path(_bash_drive_spelling(norm, ws)).resolve() if rooted else (base / raw).resolve()
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
    path_base: str | Path | None = None,
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
    rp, why = workspace_bounded_path(script, workspace_root, path_base)
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
    path_base: str | Path | None = None,
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
    verdict on raw Bash and ai_run (one shared core, empire-doctrine VI).
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
    rp, why = workspace_bounded_path(script, workspace_root, path_base)
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
    Bash and ai_run (one shared core, empire-doctrine VI).
    """
    import shlex

    s = (segment or "").strip()
    if not s:
        return False, "empty segment"
    s2 = _ENV_PREFIX_RE.sub("", s).strip()
    if not s2:
        return False, "empty after env-prefix strip"
    # shlex never expands, so a double-quoted "{print $(cmd)}" or
    # "BEGIN{print $AWK_EXPR}" was graded as literal text while Bash ran `cmd`
    # or spliced the environment into the program first (r0b2-12 B1). Any `$`
    # or backtick outside single quotes is refused; inside single quotes `$2`
    # is awk's own field syntax and stays allowed.
    expansion = _shell_construct(s2, any_dollar=True, redirects=False)
    if expansion:
        return False, f"awk invocation carries a shell {expansion}"
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


# ── Governed stdin-only OUTPUT FILTER: sed ──────────────────────────────────
#
# `git diff | sed -n '1,50p'` trims another command's output and reads no file.
# sed is a small language, though: `e` and `s///e` EXECUTE, `w` / `W` and
# `s///w` WRITE, `r` / `R` READ a named file, and `-i` / `-f` / a file operand
# make it an editor or a reader. So this is a POSITIVE grammar like the awk
# check: the script must parse as addresses plus print / delete / hold /
# branch / substitute / transliterate commands only, and the invocation must
# take its input from stdin alone. Anything the parser does not recognise is
# refused, never guessed.
#
# This grades ONE segment. bash_policy grants it only to a DOWNSTREAM pipe
# stage whose later stages are inert filters too (STDIN_FILTER_SINKS, never a
# shell), after every unbypassable phase has already run on the full command.

_SED_BINARIES: frozenset[str] = frozenset({"sed", "gsed"})
_SED_SIMPLE_COMMANDS: frozenset[str] = frozenset("pdDnNPhHgGxlz=F")
_SED_SUBST_FLAGS: frozenset[str] = frozenset("gpiImM0123456789")
_SED_ADDR_FLAGS: frozenset[str] = frozenset("IM")
_SED_BOOL_SHORT: frozenset[str] = frozenset("nEr")
_SED_BOOL_LONG: frozenset[str] = frozenset(
    {"--quiet", "--silent", "--regexp-extended", "--posix", "--sandbox"},
)
_SED_LABEL_RE = re.compile(r"[A-Za-z0-9_.-]+")
# Unquoted characters that make the shell redirect, chain, group, glob or
# background: anything other than handing the filter a plain argv.
_FILTER_UNQUOTED_META: frozenset[str] = frozenset("<>;&|(){}*?[]~\n\r")

# Stages that may sit DOWNSTREAM of a stdin-only filter. Each is still graded
# on its own by bash_policy; this set only decides whether the filter's output
# may flow on. A shell, eval, xargs, tee or interpreter is never here.
STDIN_FILTER_SINKS: frozenset[str] = frozenset(
    {
        "grep", "egrep", "fgrep", "rg", "head", "tail", "cat", "sed", "gsed",
        "awk", "gawk", "mawk", "nawk", "sort", "uniq", "wc", "cut", "tr", "nl",
        "column", "fold", "rev", "tac",
    },
)


def _shell_construct(text: str, *, any_dollar: bool, redirects: bool) -> str:
    """The first construct the SHELL acts on before the filter sees its argv.

    Single-quoted text is inert. Everywhere else a backtick or ``$(`` is a
    command substitution; with ``any_dollar`` every other unescaped ``$``
    (a variable, ``${...}``, ``$'...'``) counts too; with ``redirects`` an
    unquoted redirect / chain / group / glob metacharacter counts. ``shlex``
    never expands, so without this the check would grade literal text the
    shell is about to rewrite. Returns "" when there is none.
    """
    quote = ""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = ""
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return "command substitution (backtick)"
        if c == "$":
            if i + 1 < n and text[i + 1] == "(":
                return "command substitution"
            if any_dollar:
                return "expansion ($)"
        if quote == '"':
            if c == '"':
                quote = ""
        elif c in "'\"":
            quote = c
        elif redirects and c in _FILTER_UNQUOTED_META:
            return f"metacharacter {c!r}"
        i += 1
    return "unbalanced quote" if quote else ""


def _sed_bracket_end(s: str, i: int, delim: str) -> int:
    """Index past the bracket expression opening at ``s[i] == '['``.

    -1 when it never closes or holds the delimiter: GNU sed ends the field at
    that delimiter while POSIX/BSD treat it as literal, so the two would see
    different scripts. Refusing is the only reading both agree is safe.
    """
    n = len(s)
    j = i + 1
    if j < n and s[j] == "^":
        j += 1
    if j < n and s[j] == "]":
        j += 1
    while j < n:
        c = s[j]
        if c == delim or c == "\n":
            return -1
        if c == "[" and j + 1 < n and s[j + 1] in ":.=":
            close = s.find(s[j + 1] + "]", j + 2)
            if close < 0 or delim in s[j : close + 2]:
                return -1
            j = close + 2
            continue
        if c == "]":
            return j + 1
        j += 1
    return -1


def _sed_delimited(s: str, i: int, parts: int, *, brackets: bool) -> int:
    """Index past ``parts`` fields delimited by ``s[i]``; -1 if not provable."""
    n = len(s)
    if i >= n:
        return -1
    delim = s[i]
    if delim in "\\\n[]" or delim.isspace() or delim.isalnum():
        return -1
    i += 1
    for part in range(parts):
        regex = brackets and part == 0
        while True:
            if i >= n or s[i] == "\n":
                return -1
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == delim:
                i += 1
                break
            if regex and c == "[":
                i = _sed_bracket_end(s, i, delim)
                if i < 0:
                    return -1
                continue
            i += 1
    return i


def _sed_address(s: str, i: int, *, second: bool) -> int:
    """Index past one address at ``s[i]``; ``i`` itself when there is none."""
    n = len(s)
    if i >= n:
        return i
    c = s[i]
    if second and c in "+~":
        j = i + 1
        while j < n and s[j].isdigit():
            j += 1
        return j if j > i + 1 else -1
    if c.isdigit():
        j = i
        while j < n and s[j].isdigit():
            j += 1
        if j < n and s[j] == "~":
            k = j + 1
            while k < n and s[k].isdigit():
                k += 1
            if k == j + 1:
                return -1
            j = k
        return j
    if c == "$":
        return i + 1
    if c in "/\\":
        j = _sed_delimited(s, i if c == "/" else i + 1, 1, brackets=True)
        if j < 0:
            return -1
        while j < n and s[j] in _SED_ADDR_FLAGS:
            j += 1
        return j
    return i


def _sed_script_unsafe(script: str) -> str:
    """Empty when every command in ``script`` is in the inert set, else why not."""
    s = script
    n = len(s)
    i = 0
    depth = 0

    def _ws(k: int) -> int:
        while k < n and s[k] in " \t":
            k += 1
        return k

    while True:
        while i < n and s[i] in " \t\n;":
            i += 1
        if i >= n:
            break
        c = s[i]
        if c == "#":
            nl = s.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if c == "}":
            depth -= 1
            if depth < 0:
                return "unbalanced '}' in the sed script"
            i += 1
            continue
        if c == ":":
            m = _SED_LABEL_RE.match(s, _ws(i + 1))
            if not m:
                return "unparseable sed label"
            i = m.end()
        else:
            j = _sed_address(s, i, second=False)
            if j < 0:
                return "unparseable sed address"
            if i < j < n and s[j] == ",":
                k = _sed_address(s, j + 1, second=True)
                if k <= j + 1:
                    return "unparseable sed address range"
                j = k
            j = _ws(j)
            if j < n and s[j] == "!":
                j = _ws(j + 1)
            if j >= n:
                return "sed address without a command"
            c = s[j]
            if c == "{":
                depth += 1
                i = j + 1
                continue
            if c in _SED_SIMPLE_COMMANDS:
                i = j + 1
            elif c in "qQ":
                i = _ws(j + 1)
                while i < n and s[i].isdigit():
                    i += 1
            elif c in "btT":
                i = _ws(j + 1)
                m = _SED_LABEL_RE.match(s, i)
                if m:
                    i = m.end()
            elif c in "sy":
                k = _sed_delimited(s, j + 1, 2, brackets=c == "s")
                if k < 0:
                    return f"unparseable sed '{c}' command"
                if c == "s":
                    while k < n and s[k] in _SED_SUBST_FLAGS:
                        k += 1
                i = k
            else:
                return (
                    f"sed command '{c}' is not in the stdin-filter safe set "
                    "(e / w / W / r / R execute, write or read a file; a / i / c "
                    "and every other command are refused too)"
                )
        i = _ws(i)
        if i < n and s[i] not in ";\n}#":
            return (
                f"unexpected {s[i]!r} after a sed command (an s/// flag outside "
                "g p i I m M or a number, such as e or w, executes or writes)"
            )
    if depth:
        return "unbalanced '{' in the sed script"
    return ""


def is_governed_sed_stdin_filter(segment: str) -> tuple[bool, str]:
    """True for ``sed [-n] [-E|-r] [-e SCRIPT ...] [SCRIPT]`` with NO file.

    The invocation takes its input from stdin only: no file operand, no
    ``-f`` / ``--file`` script, no ``-i``, no redirect, no substitution or
    expansion; and the script parses entirely into the inert command set
    (``_sed_script_unsafe``). Default-deny. Whether the segment is a downstream
    pipe stage is bash_policy's decision, not this function's.
    """
    import shlex

    s = (segment or "").strip()
    if not s:
        return False, "empty segment"
    construct = _shell_construct(s, any_dollar=True, redirects=True)
    if construct:
        return False, f"sed invocation carries a shell {construct}"
    try:
        toks = shlex.split(s)
    except ValueError:
        return False, "unparseable sed invocation (unbalanced quotes)"
    if not toks or toks[0].lower() not in _SED_BINARIES:
        return False, "not a bare sed invocation"
    scripts: list[str] = []
    operands: list[str] = []
    i = 1
    n = len(toks)
    while i < n:
        tok = toks[i]
        if tok == "--":
            operands.extend(toks[i + 1 :])
            break
        if tok in ("-e", "--expression"):
            if i + 1 >= n:
                return False, f"sed {tok} without a script"
            scripts.append(toks[i + 1])
            i += 2
            continue
        if tok.startswith("--expression="):
            scripts.append(tok.split("=", 1)[1])
            i += 1
            continue
        if tok in _SED_BOOL_LONG:
            i += 1
            continue
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            step = 1
            for j, ch in enumerate(tok[1:]):
                if ch in _SED_BOOL_SHORT:
                    continue
                if ch == "e":
                    rest = tok[j + 2 :]
                    if rest:
                        scripts.append(rest)
                    elif i + 1 < n:
                        scripts.append(toks[i + 1])
                        step = 2
                    else:
                        return False, "sed -e without a script"
                    break
                return False, (
                    f"sed flag '-{ch}' is not in the stdin-filter safe set "
                    "(-n / -E / -r / -e only; -i edits files, -f reads a script file)"
                )
            i += step
            continue
        if tok.startswith("-") and tok != "-":
            return False, f"sed option '{tok}' is not in the stdin-filter safe set"
        operands.append(tok)
        i += 1
    if not scripts:
        if not operands:
            return False, "no sed script"
        scripts.append(operands.pop(0))
    if operands:
        return False, f"sed file operand '{operands[0]}': a stdin filter names no file"
    why = _sed_script_unsafe("\n".join(scripts))
    if why:
        return False, why
    return True, "governed stdin-only sed filter"
