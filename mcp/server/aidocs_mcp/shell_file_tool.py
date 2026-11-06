"""File-operand detector for shell file readers / editors (bash_policy Phase 3).

Policy: a shell file read or edit is BLOCKED by default, never asked -- a
prompt stalls an agent -- and allowing one utility on files is a separate,
explicit per-utility allowance.

A text utility (sed, grep/rg, cat, head, tail, awk, cut, sort, wc, less, ...)
given a FILE operand is a file read that bypasses the indexed read tools; with
``-i`` (sed / perl) or as ``tee FILE`` it is a file edit that bypasses the
governed edit tools. bash_policy refuses such a segment with guidance unless
the operator explicitly allowed that utility (bash.allow rule, family verdict
``allow``, default=allow, or a grant captured from the prompt). It never asks.

The same utility reading ONLY piped stdin (``git log | sed -n '2,3p'``) is not
a file use and is graded by the existing rungs, unchanged.

This module only CLASSIFIES one segment. It never authorizes anything.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# Utilities whose first positional is a program / pattern, unless the
# program is given by a flag (-e / -f / --regexp / --file / --expression).
_PROGRAM_FIRST: frozenset[str] = frozenset(
    {"sed", "gsed", "grep", "egrep", "fgrep", "rg", "ag", "awk", "gawk", "mawk", "nawk"},
)
_AWK: frozenset[str] = frozenset({"awk", "gawk", "mawk", "nawk"})
_SED: frozenset[str] = frozenset({"sed", "gsed"})
# Tools that search the working tree when given no path and no piped stdin.
_TREE_SEARCHERS: frozenset[str] = frozenset({"rg", "ag"})
_GREP: frozenset[str] = frozenset({"grep", "egrep", "fgrep"})

# Plain readers: every positional is a file.
_PLAIN_READERS: frozenset[str] = frozenset(
    {
        "cat", "tac", "nl", "head", "tail", "less", "more", "most", "bat", "batcat",
        "cut", "sort", "uniq", "wc", "xxd", "od", "hexdump", "strings", "column",
        "fold", "rev",
    },
)

FILE_TOOL_BASES: frozenset[str] = _PROGRAM_FIRST | _PLAIN_READERS | {"tee", "perl"}

# Flags that take a separate value (short flags may also carry it attached).
_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sed": frozenset({"-e", "-f", "-l", "--expression", "--file", "--line-length"}),
    "grep": frozenset(
        {
            "-e", "-f", "-A", "-B", "-C", "-m", "-d", "-D", "--regexp", "--file",
            "--max-count", "--context", "--after-context", "--before-context",
            "--include", "--exclude", "--exclude-dir", "--label", "--color", "--colour",
        },
    ),
    "rg": frozenset(
        {
            "-e", "-f", "-A", "-B", "-C", "-m", "-g", "-t", "-T", "-j", "-M", "-E", "-r",
            "--regexp", "--file", "--max-count", "--context", "--after-context",
            "--before-context", "--glob", "--iglob", "--type", "--type-not", "--threads",
            "--max-columns", "--encoding", "--sort", "--sortr", "--max-depth",
            "--replace", "--max-filesize", "--colors", "--color",
        },
    ),
    "ag": frozenset({"-A", "-B", "-C", "-m", "-G", "-g", "--depth", "--context"}),
    "awk": frozenset({"-f", "-v", "-F", "--file", "--assign", "--field-separator"}),
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes", "-s", "--sleep-interval", "--pid"}),
    "cut": frozenset(
        {
            "-d", "-f", "-c", "-b", "--delimiter", "--fields", "--characters", "--bytes",
            "--output-delimiter",
        },
    ),
    "sort": frozenset(
        {
            "-o", "-k", "-t", "-S", "-T", "--output", "--key", "--field-separator",
            "--buffer-size", "--temporary-directory", "--parallel",
        },
    ),
    "uniq": frozenset({"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}),
    "nl": frozenset({"-b", "-d", "-f", "-h", "-i", "-l", "-n", "-s", "-v", "-w"}),
    "fold": frozenset({"-w", "--width"}),
    "column": frozenset({"-c", "-s", "-o", "-t", "-N", "-R"}),
    "xxd": frozenset({"-c", "-g", "-l", "-o", "-s"}),
    "od": frozenset({"-A", "-j", "-N", "-t", "-w"}),
    "hexdump": frozenset({"-e", "-f", "-n", "-s"}),
    "strings": frozenset({"-n", "-t", "-e"}),
    "perl": frozenset({"-e", "-E", "-I", "-M", "-m", "-x"}),
    "wc": frozenset(),
}
for _alias, _canon in (
    ("gsed", "sed"), ("egrep", "grep"), ("fgrep", "grep"), ("gawk", "awk"),
    ("mawk", "awk"), ("nawk", "awk"), ("batcat", "bat"),
):
    _VALUE_FLAGS.setdefault(_alias, _VALUE_FLAGS.get(_canon, frozenset()))

_PROGRAM_FLAGS: frozenset[str] = frozenset(
    {"-e", "-f", "--regexp", "--file", "--expression"},
)
# Flag values that are themselves files the tool reads (program / pattern file).
_FILE_VALUE_FLAGS: frozenset[str] = frozenset({"-f", "--file"})
_NOT_FILES: frozenset[str] = frozenset({"-", "/dev/null", "/dev/stdin"})
_AWK_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(slots=True)
class FileToolUse:
    """A segment that reads or edits files through a shell text utility."""

    base: str
    kind: str  # "read" | "edit"
    paths: list[str] = field(default_factory=list)


def _tokens(segment: str) -> list[str] | None:
    try:
        lex = shlex.shlex(segment, posix=True, punctuation_chars="<>|&;()")
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return None


_SED_SIMPLE_COMMANDS = frozenset("pPdDnNgGhHx=zF")


def _sed_program_io(prog: str) -> list[tuple[str, str]] | None:
    """File I/O named by a sed script: ``r``/``R`` read a file, ``w``/``W``
    and the ``s///w`` flag write one, ``e`` and the ``s///e`` flag run a
    command. ``s`` / ``y`` bodies are skipped, so a replacement text that
    merely contains ``r foo`` is not a command. None: not parseable."""
    s = prog
    n = len(s)
    out: list[tuple[str, str]] = []

    def _close(i: int, delim: str) -> int:
        while i < n:
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == delim:
                return i + 1
            i += 1
        return -1

    def _eol(i: int) -> int:
        j = s.find("\n", i)
        return n if j < 0 else j

    i = 0
    while i < n:
        c = s[i]
        if c in " \t\n;{}!":
            i += 1
            continue
        # Address: line numbers, $, /re/ or \cREc, ranges, steps, then `!`.
        while i < n:
            c = s[i]
            if c.isdigit() or c in "$,~+ \t!":
                i += 1
            elif c == "/" or c == "\\":
                i = _close(i + 1, "/") if c == "/" else _close(i + 2, s[i + 1] if i + 1 < n else "")
                if i < 0:
                    return None
                while i < n and s[i] in "IM":
                    i += 1
            else:
                break
        if i >= n:
            return out
        cmd = s[i]
        if cmd in "{};\n":
            i += 1
            continue
        if cmd in "sy":
            if i + 1 >= n or s[i + 1] in "\n\\":
                return None
            delim = s[i + 1]
            j = _close(i + 2, delim)
            j = _close(j, delim) if j >= 0 else -1
            if j < 0:
                return None
            if cmd == "s":
                while j < n and s[j] not in ";\n}":
                    if s[j] in "wW":
                        end = _eol(j + 1)
                        out.append(("edit", s[j + 1 : end].strip() or "?"))
                        j = end
                        break
                    if s[j] == "e":
                        out.append(("read", "<sed s///e command>"))
                    j += 1
            i = j
        elif cmd in "rRwW":
            end = _eol(i + 1)
            out.append(("read" if cmd in "rR" else "edit", s[i + 1 : end].strip() or "?"))
            i = end
        elif cmd == "e":
            out.append(("read", "<sed e command>"))
            i = _eol(i + 1)
        elif cmd in "aic":
            i = _eol(i + 1)
        elif cmd in ":btT":
            i += 1
            while i < n and s[i] not in ";\n":
                i += 1
        elif cmd in "qQlL":
            i += 1
            while i < n and s[i].isdigit():
                i += 1
        elif cmd in _SED_SIMPLE_COMMANDS:
            i += 1
        else:
            return None
    return out


def _awk_strip_literals(prog: str) -> str | None:
    """``prog`` with string literals emptied and regex literals removed, so
    a `>` or `|` inside them is not read as a redirect. None: unbalanced."""
    out: list[str] = []
    i = 0
    n = len(prog)
    while i < n:
        c = prog[i]
        if c == '"' or (c == "/" and ("".join(out).rstrip()[-1:] or "(") in "~(,{;!&|"):
            j = i + 1
            while j < n and prog[j] != c:
                j += 2 if prog[j] == "\\" else 1
            if j >= n:
                return None
            out.append('""' if c == '"' else "//")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_AWK_PRINT_RE = re.compile(r"\bprintf?\b")


def _awk_program_io(prog: str) -> list[tuple[str, str]] | None:
    """File / command I/O in an awk program: ``getline < f``, ``cmd |
    getline``, ``cmd |& getline``, ``print > f`` / ``>> f`` / ``| cmd`` /
    ``|& cmd``, ``system()``, gawk ``/inet/``, ``@load`` / ``@include``.
    None: not parseable."""
    code = _awk_strip_literals(prog)
    if code is None:
        return None
    out: list[tuple[str, str]] = []
    if "/inet/" in prog or re.search(r"@(load|include)\b", prog):
        out.append(("read", "<awk network/load>"))
    if re.search(r"\bsystem\s*\(", code):
        out.append(("read", "<awk system()>"))
    # `cmd | getline`, gawk's two-way coprocess `cmd |& getline`, `getline < f`.
    if re.search(r"\|&?\s*getline\b|\bgetline\b[^;{}\n]*<", code):
        out.append(("read", "<awk getline>"))
    for m in _AWK_PRINT_RE.finditer(code):
        depth = 0
        for ch in code[m.end() :]:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif depth <= 0 and ch in ";}\n":
                break
            elif depth <= 0 and ch in ">|":
                out.append(("edit", "<awk print redirect>"))
                break
    return out


def classify_file_tool_use(
    base: str,
    segment: str,
    *,
    piped_stdin: bool,
) -> FileToolUse | None:
    """Return the file use of ``segment`` (whose base command is ``base``).

    None when the utility is not in the family or touches no file (a pure
    stdin filter). Unparseable input in the family is treated as a file use
    with no known path (fail closed toward the block, never toward an ask).
    """
    if base not in FILE_TOOL_BASES:
        return None
    toks = _tokens(segment)
    if toks is None:
        return FileToolUse(base=base, kind="read", paths=[])
    # Drop leading env assignments and the command word itself.
    while toks and _AWK_ASSIGN_RE.match(toks[0]) and "/" not in toks[0].split("=", 1)[0]:
        toks = toks[1:]
    toks = toks[1:]

    value_flags = _VALUE_FLAGS.get(base, frozenset())
    kind = "edit" if base == "tee" else "read"
    perl_inplace = False
    program_given = False
    recursive = False
    positionals: list[str] = []
    programs: list[str] = []
    read_paths: list[str] = []
    write_paths: list[str] = []
    end_of_opts = False
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        # Redirections: `<` reads a file; output redirects / fd dups are not
        # the tool's own operands (the redirect floor grades those).
        if t and t[0] in "<>&|;()":
            if t == "<" and i + 1 < n:
                read_paths.append(toks[i + 1])
            i += 2 if t in ("<", ">", ">>", "<<", "<<<", ">|", "&>", "&>>", "<>", ">&", "<&", ">>&") else 1
            continue
        if t.isdigit() and i + 1 < n and toks[i + 1][:1] in "<>":
            i += 1
            continue
        if end_of_opts or not t.startswith("-") or t == "-":
            positionals.append(t)
            i += 1
            continue
        if t == "--":
            end_of_opts = True
            i += 1
            continue
        if t.startswith("--"):
            name, eq, value = t.partition("=")
            if name == "--in-place" and base in _SED:
                kind = "edit"
            if name in ("--recursive", "--dereference-recursive"):
                recursive = True
            if name in ("--output",) and base == "sort":
                kind = "edit"
                if eq:
                    write_paths.append(value)
                elif i + 1 < n:
                    write_paths.append(toks[i + 1])
                    i += 1
                i += 1
                continue
            if name in value_flags and not eq:
                value = toks[i + 1] if i + 1 < n else ""
                i += 1
            if name == "--expression" and base in _SED:
                programs.append(value)
            if name in _PROGRAM_FLAGS and base in _PROGRAM_FIRST:
                program_given = True
                if name in _FILE_VALUE_FLAGS and value:
                    read_paths.append(value)
            i += 1
            continue
        # Short flag cluster.
        cluster = t[1:]
        j = 0
        while j < len(cluster):
            ch = cluster[j]
            flag = "-" + ch
            if ch == "i" and base in _SED:
                kind = "edit"
                break  # the rest of the cluster is the backup suffix
            if ch == "i" and base == "perl":
                perl_inplace = True
                break
            if ch in "rR" and base in _GREP:
                recursive = True
            if flag in value_flags:
                rest = cluster[j + 1 :]
                if rest:
                    value = rest
                else:
                    value = toks[i + 1] if i + 1 < n else ""
                    i += 1
                if flag == "-o" and base == "sort":
                    kind = "edit"
                    write_paths.append(value)
                elif flag in _PROGRAM_FLAGS and base in _PROGRAM_FIRST:
                    if flag == "-e" and base in _SED:
                        programs.append(value)
                    if base in _AWK and flag != "-f":
                        pass
                    else:
                        program_given = True
                        if flag in _FILE_VALUE_FLAGS and value:
                            read_paths.append(value)
                elif base == "perl" and flag in ("-e", "-E"):
                    program_given = True
                break
            j += 1
        i += 1

    if base == "perl":
        if not perl_inplace:
            return None
        kind = "edit"
        files = positionals if program_given else positionals[1:]
        return FileToolUse(base=base, kind=kind, paths=[p for p in files if p not in _NOT_FILES])

    # A search actually runs only with a pattern (`rg --version` / `--help`
    # / `--files` search nothing).
    searches = program_given or bool(positionals)
    if base in _PROGRAM_FIRST and not program_given and positionals:
        if base in _SED or base in _AWK:
            programs.append(positionals[0])
        positionals = positionals[1:]
    # File access INSIDE the program text (`sed '1r f'`, `sed 'w f'`, awk
    # `getline < f` / `print > f` / `system()`), r0b2-13 B3. A program the
    # scanner cannot prove is treated as a file use (block, never ask).
    for prog in programs:
        io = _sed_program_io(prog) if base in _SED else _awk_program_io(prog)
        if io is None:
            return FileToolUse(base=base, kind="read", paths=[])
        for io_kind, io_path in io:
            if io_kind == "edit":
                kind = "edit"
                write_paths.append(io_path)
            else:
                read_paths.append(io_path)
    if base in _AWK:
        positionals = [p for p in positionals if not _AWK_ASSIGN_RE.match(p)]
    files = [p for p in positionals if p not in _NOT_FILES]
    paths = read_paths + write_paths + files
    paths = [p for p in paths if p and p not in _NOT_FILES]
    if not paths:
        if kind == "edit" and base in _SED:
            # `sed -i` with no file operand edits nothing; still an edit shape.
            return FileToolUse(base=base, kind=kind, paths=[])
        if base in _TREE_SEARCHERS and searches and not piped_stdin:
            return FileToolUse(base=base, kind="read", paths=["."])
        if recursive and searches and base in _GREP:
            return FileToolUse(base=base, kind="read", paths=["."])
        return None
    return FileToolUse(base=base, kind=kind, paths=paths)
