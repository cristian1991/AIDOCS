"""Neutral, stdlib-only shell WRAPPER PEELER (#893, extracted for #1135).

ONE grammar for "which command does this wrapper really run?", shared by:

  * ``heuristic_judge`` -- the policy-aware judge on the gated surfaces (it
    re-exports every name below under its historical private spelling, so
    its rules and tests are unchanged);
  * ``host_destructive_floor`` -- the host floor that runs inside the
    stdlib-only PreToolUse launcher shim, BEFORE the package is imported.

WHY NEUTRAL. The floor is loaded by FILE PATH from ``~/.aidocs/runtime/``
beside the shim, where no package exists. This module therefore imports
nothing but the standard library and never uses a relative import; it is
deployed verbatim next to the shim by ``claude_hooks_install`` and loaded the
same way. Before #1135 this code lived inside the judge, so the host floor
would have had to either import the whole judge (defeating the shim) or keep
a second copy (which drifts). A second copy is how the first-word-only
locked check and the judge's peeler came to disagree in the first place.

The tables below are the judge's #893 tables, moved verbatim.
"""

from __future__ import annotations

import re

#: Leading ``NAME=VALUE`` assignment. Case-insensitive because the floor peels
#: ORIGINAL-case argv; on the judge's lowercased input it is identical to the
#: judge's own ``_ENV_ASSIGN_RE``.
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# ── wrapper option arity (#893 round 2) ─────────────────────────────
# A wrapper that RUNS another command may take options OF ITS OWN, and some
# of those options take a VALUE. Peeling that only skips `-`-prefixed tokens
# lands on the VALUE, not on the wrapped command: `sudo -u root git reset
# --hard` stopped at `root`, so GIT_RESET_HARD never fired. That is a MISS —
# the dangerous direction — so each wrapper gets an explicit arity table,
# checked against its real CLI:
#   sudo(8)     value: -C -D -g -h -p -R -r -t -T -U -u (+ long forms)
#   doas(1)     value: -a -C -u
#   env(1)      value: -u -C -S; also leading NAME=VALUE assignments
#   nice(1)     value: -n/--adjustment (legacy `-5` is an unknown short → branch)
#   ionice(1)   value: -c -n -p -P -u
#   timeout(1)  value: -k -s; ONE leading positional (DURATION)
#   stdbuf(1)   value: -i -o -e
#   xargs(1)    value: -a -d -E -I -L -n -P -s (-e/-i/-l take OPTIONAL args →
#               they fall through to the unknown branch and fail closed)
#   nohup(1) / eval  no options
#   command(1)  bool only: -p -v -V
#   exec        value: -a; bool: -c -l
#   time(1)     value: -o -f (GNU /usr/bin/time); bool: -p -a -v
# `--` always ends the wrapper's own options.
# UNKNOWN option → FAIL CLOSED: the walk BRANCHES, following both "it took a
# value" and "it did not", so an unrecognised spelling can only ADD candidate
# command positions, never hide one.
WRAPPER_OPTIONS: dict[str, dict[str, object]] = {
    "sudo": {
        "value": frozenset(
            {
                "-C", "-D", "-g", "-h", "-p", "-R", "-r", "-t", "-T", "-U", "-u",
                "--close-from", "--chdir", "--group", "--host", "--prompt",
                "--chroot", "--role", "--type", "--command-timeout",
                "--other-user", "--user",
            },
        ),
        "bool": frozenset(
            {
                "-A", "-b", "-B", "-E", "-e", "-H", "-i", "-K", "-k", "-l", "-n",
                "-P", "-S", "-s", "-V", "-v",
                "--askpass", "--background", "--bell", "--preserve-env", "--edit",
                "--set-home", "--login", "--remove-timestamp", "--reset-timestamp",
                "--list", "--non-interactive", "--preserve-groups", "--stdin",
                "--shell", "--version", "--validate",
            },
        ),
        "positional": 0,
        "assignments": True,  # `sudo FOO=bar cmd` is accepted
    },
    "doas": {
        "value": frozenset({"-a", "-C", "-u"}),
        "bool": frozenset({"-L", "-n", "-s"}),
        "positional": 0,
        "assignments": False,
    },
    "env": {
        "value": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
        "bool": frozenset(
            {"-i", "-0", "-v", "--ignore-environment", "--null", "--debug"},
        ),
        "positional": 0,
        "assignments": True,
    },
    "nice": {
        "value": frozenset({"-n", "--adjustment"}),
        "bool": frozenset(),
        "positional": 0,
        "assignments": False,
    },
    "ionice": {
        "value": frozenset({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata",
                            "--pid", "--pgid", "--uid"}),
        "bool": frozenset({"-t", "--ignore"}),
        "positional": 0,
        "assignments": False,
    },
    "timeout": {
        "value": frozenset({"-k", "-s", "--kill-after", "--signal"}),
        "bool": frozenset({"-v", "--verbose", "--preserve-status", "--foreground"}),
        "positional": 1,  # DURATION
        "assignments": False,
    },
    "stdbuf": {
        "value": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
        "bool": frozenset(),
        "positional": 0,
        "assignments": False,
    },
    "xargs": {
        "value": frozenset(
            {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s",
             "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines",
             "--max-args", "--max-procs", "--max-chars"},
        ),
        "bool": frozenset({"-0", "-p", "-r", "-t", "-x", "--null", "--interactive",
                           "--no-run-if-empty", "--verbose", "--exit"}),
        "positional": 0,
        "assignments": False,
    },
    "nohup": {"value": frozenset(), "bool": frozenset(), "positional": 0,
              "assignments": False},
    "eval": {"value": frozenset(), "bool": frozenset(), "positional": 0,
             "assignments": False},
    "command": {"value": frozenset(), "bool": frozenset({"-p", "-v", "-V"}),
                "positional": 0, "assignments": False},
    "exec": {"value": frozenset({"-a"}), "bool": frozenset({"-c", "-l"}),
             "positional": 0, "assignments": False},
    "time": {"value": frozenset({"-o", "-f", "--output", "--format"}),
             "bool": frozenset({"-p", "-a", "-v", "--append", "--verbose", "--portability"}),
             "positional": 0, "assignments": False},
}
PEEL_MAX_CANDIDATES = 24
#: Recursion cap of the argv peeler. A nesting deeper than this stops being
#: peeled; the floor treats "could not finish peeling" as an unanalysed
#: payload (deny), the judge keeps its historical additive reading.
PEEL_MAX_DEPTH = 4

# ── options whose VALUE is itself a COMMAND LINE (#893 round 3) ─────
# `env -S 'git reset --hard'` EXECUTES git: `-S`/`--split-string` makes env
# word-split its operand and run it. Consuming that operand as inert option
# DATA hid the whole invocation — a MISS, the dangerous direction. Every such
# option is listed here and its value is fed BACK through this same parser.
# `su -c` is the same shape; with this in place `su` is no longer excluded.
# Shell wrappers are added for the same reason: `sh -c 'git reset --hard'`
# was previously unreachable for these rules because `sh` was not a known
# wrapper at all.
COMMAND_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "env": frozenset({"-S", "--split-string"}),
    "su": frozenset({"-c", "--command"}),
    "sh": frozenset({"-c"}),
    "bash": frozenset({"-c"}),
    "zsh": frozenset({"-c"}),
    "dash": frozenset({"-c"}),
    "ksh": frozenset({"-c"}),
    "busybox": frozenset({"-c"}),
}
WRAPPER_OPTIONS["su"] = {
    "value": frozenset({"-s", "--shell", "-g", "--group", "-G", "--supp-group",
                        "-w", "--whitelist-environment", "-c", "--command"}),
    "bool": frozenset({"-l", "--login", "-m", "-p", "--preserve-environment", "-f",
                       "--fast", "-P", "--pty"}),
    "positional": 1,  # USER
    "assignments": False,
}
for _shell in ("sh", "bash", "zsh", "dash", "ksh", "busybox"):
    WRAPPER_OPTIONS[_shell] = {
        "value": frozenset({"-c", "-o", "+o"}),
        "bool": frozenset({"-l", "-i", "-s", "-e", "-x", "-u", "--login",
                           "--noprofile", "--norc", "--posix"}),
        "positional": 0,
        "assignments": False,
    }
del _shell

# The judge lowercases the command before matching, which COLLAPSES options
# that differ only by case — `sudo -H` (boolean, set HOME) and `sudo -h`
# (takes a HOST value) become the same token. Reading such a token as
# "takes a value" ate the wrapped command (`sudo -E -H git reset --hard`
# resolved to no git at all — a MISS). Every case-collapsed spelling is
# therefore AMBIGUOUS and is followed BOTH ways.
WRAPPER_TABLES: dict[str, dict[str, object]] = {}
for _name, _spec in WRAPPER_OPTIONS.items():
    _value_l = frozenset(o.lower() for o in _spec["value"])  # type: ignore[union-attr]
    _bool_l = frozenset(o.lower() for o in _spec["bool"])  # type: ignore[union-attr]
    _cmd_l = frozenset(o.lower() for o in COMMAND_VALUE_OPTIONS.get(_name, frozenset()))
    WRAPPER_TABLES[_name] = {
        "value": _value_l,
        "bool": _bool_l,
        # A command-bearing option is never merely ambiguous: its value is
        # ALWAYS re-parsed as a command as well as consumed as a value.
        "ambiguous": (_value_l & _bool_l) - _cmd_l,
        "command_value": _cmd_l,
        "positional": _spec["positional"],
        "assignments": _spec["assignments"],
    }
del _name, _spec, _value_l, _bool_l, _cmd_l


# ── GNU `env -S` split-string decoding (#893 round 4) ───────────────
# The judge whitespace-splits the RAW command BEFORE shell/env semantics.
# `env -S 'git\_reset\_--hard'` therefore arrives as ONE token, while env
# itself splits it into argv `git`, `reset`, `--hard` and runs the hard
# reset — a MISS. GNU env's documented -S processing is decoded here:
#   \_        argument separator OUTSIDE env's own quotes; a literal space
#             INSIDE them
#   \t \n \r \f \v    control characters (never separators)
#   \\ \# \$ \' \"    literal backslash / # / $ / quote
#   \c        end of string — the remainder is discarded
#   ' " …'    env's own quoting; \_ inside does not split
#   #         a COMMENT when it is the first non-blank character
#   ${VAR} $VAR       expansion — NOT resolvable statically
# Anything this decoder cannot reproduce faithfully returns None, and the
# caller treats the value as an UNPARSED COMMAND (dangerous), never as
# inert data.
_ENV_S_CONTROL_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f", "v": "\v"}
_ENV_S_LITERAL_ESCAPES = {"\\": "\\", "#": "#", "$": "$", "'": "'", '"': '"'}


def strip_one_quote_layer(value: str) -> tuple[str, bool]:
    """Undo ONE layer of SHELL quoting around an option value.

    Returns ``(content, expansion_possible)``. A double-quoted or unquoted
    value that still contains `$` or a backtick is expansion-bearing: the
    shell resolves it at run time and no static decoding is faithful.
    """
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "'\"":
        inner = stripped[1:-1]
        if stripped[0] == "'":
            return inner, False  # single quotes are fully literal
        return inner, ("$" in inner or "`" in inner)
    return stripped, ("$" in stripped or "`" in stripped)


def env_split_string_tokens(value: str) -> list[str] | None:
    """GNU ``env -S`` argv for ``value``, or None when not faithfully decodable.

    NOTE on the unquoted spelling (`env -S git\\_reset\\_--hard`): a real
    shell eats the backslashes before env sees them, so env would receive a
    single `git_reset_--hard` argument. We decode `\\_` as a separator in
    BOTH spellings anyway — that direction can only ADD a detected command,
    which is the safe way to be wrong about which layer consumed the escape.
    """
    content, expansion = strip_one_quote_layer(value)
    if expansion:
        return None  # ${VAR} / $(…) / backtick — unresolvable statically
    content = content.lstrip(" \t")
    if content.startswith("#"):
        return []  # a leading # makes the whole string a comment
    tokens: list[str] = []
    current: list[str] = []
    started = False
    quote: str | None = None
    i = 0
    n = len(content)

    def _flush() -> None:
        nonlocal started
        if started:
            tokens.append("".join(current))
            current.clear()
            started = False

    while i < n:
        ch = content[i]
        if ch == "\\":
            if i + 1 >= n:
                return None  # dangling backslash — env errors; we fail closed
            nxt = content[i + 1]
            i += 2
            if nxt == "c":
                if quote is not None:
                    return None  # \c inside quotes is an env error
                _flush()
                return tokens
            if nxt == "_":
                if quote is None:
                    _flush()  # argument separator
                else:
                    current.append(" ")
                    started = True
                continue
            if nxt in _ENV_S_CONTROL_ESCAPES:
                current.append(_ENV_S_CONTROL_ESCAPES[nxt])
                started = True
                continue
            if nxt in _ENV_S_LITERAL_ESCAPES:
                current.append(_ENV_S_LITERAL_ESCAPES[nxt])
                started = True
                continue
            return None  # undocumented escape — env rejects it; fail closed
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
                started = True
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            started = True  # `env -S "" x` yields an empty first argument
            i += 1
            continue
        if ch in " \t":
            _flush()
            i += 1
            continue
        if ch in "$`":
            return None  # bare expansion inside the split string
        current.append(ch)
        started = True
        i += 1
    if quote is not None:
        return None  # unterminated quote — fail closed
    _flush()
    return tokens


def command_value_name(token: str, command_value: frozenset[str]) -> str | None:
    """The command-bearing option ``token`` spells, separate or attached.

    Matches `-S`, `-S'git …'` (the shell hands us `-Sgit`), `--split-string`
    and `--split-string=…`. Longest spelling first so `--split-string` is not
    shadowed by a shorter prefix.
    """
    for opt in sorted(command_value, key=len, reverse=True):
        if token == opt or token.startswith(opt + "="):
            return opt
        if len(opt) == 2 and not opt.startswith("--") and token.startswith(opt):
            return opt
    return None


def basename_token(token: str) -> str:
    base = token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-4] if base.endswith(".exe") else base


def peel_wrapper_candidates(
    tokens: list[str],
    depth: int = 0,
    notes: list[str] | None = None,
) -> list[list[str]]:
    """Every token list that could be the REAL command after wrapper peeling.

    Additive and fail-closed: the wrapper's own token list is always kept (a
    rule that targets `sudo` itself keeps matching), and an option whose arity
    this table does not know BRANCHES into both readings, so an unknown
    spelling can only add candidate command positions.
    """
    if not tokens or depth >= PEEL_MAX_DEPTH:
        return [tokens]
    base_name = basename_token(tokens[0])
    spec = WRAPPER_TABLES.get(base_name)
    if spec is None:
        return [tokens]
    if base_name == "eval" and notes is not None and len(tokens) > 1:
        # `eval` is the degenerate command-bearing shape: its OPERAND is the
        # command string, carried by no option at all.
        operand = " ".join(tokens[1:])
        if strip_one_quote_layer(operand)[1]:
            notes.append(operand[:200])
    value_opts: frozenset[str] = spec["value"]  # type: ignore[assignment]
    bool_opts: frozenset[str] = spec["bool"]  # type: ignore[assignment]
    ambiguous: frozenset[str] = spec["ambiguous"]  # type: ignore[assignment]
    command_value: frozenset[str] = spec["command_value"]  # type: ignore[assignment]
    positional_left: int = spec["positional"]  # type: ignore[assignment]
    assignments: bool = spec["assignments"]  # type: ignore[assignment]

    rest = tokens[1:]
    branches: list[list[str]] = []
    while rest:
        tok = rest[0].lower()
        if tok == "--":
            rest = rest[1:]
            break
        # An option whose VALUE IS A COMMAND LINE (`env -S`, `su -c`,
        # `sh -c`). The command is re-parsed by this same walker, so
        # `env -S 'git reset --hard'` reaches git. The command string has
        # already been whitespace-split by the caller, so the value is
        # simply "the remainder of this token plus the tokens after it";
        # the inert reading is ALSO followed, so nothing is lost either way.
        cmd_opt = command_value_name(tok, command_value)
        if cmd_opt is not None:
            attached = rest[0][len(cmd_opt) :].lstrip("=")
            value_tokens = [attached, *rest[1:]] if attached else rest[1:]
            if value_tokens:
                value = " ".join(value_tokens)
                # env -S has its own tokenisation (`\_` separates arguments).
                # Decode it; the naive whitespace reading is kept as well, so
                # a decoder that returns None loses nothing.
                if cmd_opt in ("-s", "--split-string"):
                    decoded = env_split_string_tokens(value)
                    if decoded:
                        branches.append(decoded)
                    elif decoded is None and notes is not None:
                        notes.append(value[:200])  # not faithfully decodable
                elif notes is not None and strip_one_quote_layer(value)[1]:
                    # `sh -c` / `su -c` values are parsed by a SHELL; their
                    # quoting survives our split, but an expansion cannot be
                    # resolved statically — same verdict as env -S.
                    notes.append(value[:200])
                branches.append(value_tokens)
            rest = rest[1:] if attached else rest[2:]
            continue
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name in ambiguous:
                branches.append(rest[2:])
                rest = rest[1:]
                continue
            if "=" in tok or name in bool_opts:
                rest = rest[1:]
                continue
            if name in value_opts:
                rest = rest[2:]
                continue
            branches.append(rest[2:])  # unknown long option → fail closed
            rest = rest[1:]
            continue
        if tok.startswith("-") and len(tok) > 1:
            if tok in ambiguous:
                # Case-collapsed spelling: follow BOTH readings.
                branches.append(rest[2:])
                rest = rest[1:]
                continue
            if tok in value_opts:
                rest = rest[2:]
                continue
            if tok in bool_opts or all(("-" + ch) in bool_opts for ch in tok[1:]):
                rest = rest[1:]
                continue
            if ("-" + tok[1]) in value_opts:
                rest = rest[1:]  # attached value: `-uroot`, `-n5`
                continue
            branches.append(rest[2:])  # unknown short option → fail closed
            rest = rest[1:]
            continue
        if assignments and ENV_ASSIGN_RE.match(tok):
            rest = rest[1:]
            continue
        if positional_left > 0:
            positional_left -= 1
            branches.append(rest)  # it might already BE the command
            rest = rest[1:]
            continue
        break

    out: list[list[str]] = [tokens]
    for branch in [rest, *branches]:
        if not branch:
            continue
        for peeled in peel_wrapper_candidates(branch, depth + 1, notes):
            if peeled and peeled not in out:
                out.append(peeled)
            if len(out) >= PEEL_MAX_CANDIDATES:
                return out
    return out


# The two private spellings heuristic_judge calls by its historical names.
# They are the SAME objects, not copies.
_basename_token = basename_token
_peel_wrapper_candidates = peel_wrapper_candidates
