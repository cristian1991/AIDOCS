"""Command read-intent detector — closes file-read bypasses via ai_run.

Host Read is gated by ``AccessGate.host_read_decision``: an agent cannot
open ``.env`` or undiscovered indexed source through the Read tool. But an
agent can still read the SAME bytes by running a shell command that prints
them:

    cat .env
    python -c "print(open('.env').read())"
    node -e "console.log(fs.readFileSync('.env','utf8'))"
    base64 .env
    sqlite3 secrets.db .dump
    cp .env /tmp/x          # copy a secret out, then read the copy

This module detects those read SHAPES, extracts the target path(s), and
runs each path through the EXACT SAME policy the Read tool would face
(``host_read_decision``). It is the PRE-EXECUTION half of the defense
(spec D): an obvious sensitive read never spawns. The complementary half
is the command OUTPUT guard (``run_output_guard``), which catches a
credential that leaks through an otherwise-safe file's bytes.

Design notes:
  - The interpreter inline-body scan is gated to interpreter base commands
    only. ``echo "open('.env')"`` must NOT be treated as a read — only
    ``python -c "open('.env')"`` is.
  - Shell wrappers (``bash -c '...'``) are recursed into so a nested
    ``cat .env`` is still caught.
  - Detection is conservative: a non-sensitive, non-indexed artifact path
    (``cat tmp/output.log``) extracts a target but ``host_read_decision``
    ALLOWS it. The detector never blocks on its own — the host-read policy
    is the single source of truth for allow/block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .access_gate import (
    GateDecision,
    host_read_decision,
    host_read_hard_block,
)
from .bash_policy import _normalize_basename, _split_chained

# Base commands whose non-flag arguments are file paths being READ to
# stdout (or encoded/dumped, which is the same exposure).
_READER_COMMANDS: frozenset[str] = frozenset(
    {
        "cat",
        "tac",
        "type",
        "nl",
        "head",
        "tail",
        "less",
        "more",
        "most",
        "bat",
        "batcat",
        "xxd",
        "od",
        "hexdump",
        "strings",
        "base64",
        "base32",
        "get-content",
        "gc",  # PowerShell aliases (if a ps backend runs them)
    },
)

# Encoders that take the source file via a value flag (``-in FILE`` /
# ``-encode FILE``) rather than as a bare positional.
_VALUE_FLAG_READERS: dict[str, frozenset[str]] = {
    "openssl": frozenset({"-in"}),
    "certutil": frozenset({"-f"}),
}

# Interpreters whose inline bodies (``-c`` / ``-e`` / ``-p`` / ``--eval``)
# may contain a file-read call. Only these have their bodies scanned.
_INTERPRETERS: frozenset[str] = frozenset(
    {
        "python",
        "python2",
        "python3",
        "py",
        "perl",
        "ruby",
        "node",
        "nodejs",
        "php",
        "deno",
        "bun",
        "rscript",
    },
)

# Shell wrappers — recurse into the ``-c`` body so ``bash -c 'cat .env'``
# is detected.
_SHELL_WRAPPERS: frozenset[str] = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "dash",
        "ash",
        "ksh",
    },
)

# Program-then-files readers: the first positional is a SCRIPT/PATTERN,
# the remaining positionals are files whose CONTENT is read to stdout.
# (sed prints/transform-prints its input; awk runs a program over files;
# grep prints matching lines from files.) With -e/-f the program is given
# by a flag, so ALL positionals are files.
_PROGRAM_THEN_FILES: frozenset[str] = frozenset(
    {
        "sed",
        "awk",
        "gawk",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
    },
)
_PROGRAM_VALUE_FLAGS: frozenset[str] = frozenset({"-e", "-f", "--regexp", "--file"})

# Copy/move commands: copying a protected source to a scratch path and
# reading the copy bypasses the read gate. We run the read policy on the
# SOURCE. (Provenance: a copy of a protected source is itself protected —
# the copy is blocked pre-exec, and the scratch destination is an unknown
# external path that host_read_decision blocks on the later Read.)
_COPY_COMMANDS: frozenset[str] = frozenset(
    {
        "cp",
        "mv",
        "copy",
        "move",
        "install",
        "rsync",
        "ln",
    },
)

# sqlite read verbs — a sqlite3 invocation that dumps/queries reads the DB.
_SQLITE_READ_TOKENS: tuple[str, ...] = (
    ".dump",
    ".read",
    ".schema",
    ".tables",
    ".output",
    ".once",
)
_SQLITE_READ_KEYWORDS = re.compile(r"\b(select|pragma|attach)\b", re.IGNORECASE)

# Inline read-call patterns. Each captures group(1) = the path literal.
# Applied ONLY to interpreter bodies.
_INLINE_READ_PATTERNS: tuple[re.Pattern[str], ...] = (
    # python / generic: open('path' ...)  — read mode is the default.
    re.compile(r"\bopen\s*\(\s*['\"]([^'\"]+)['\"]"),
    # pathlib: Path('path').read_text()/read_bytes()
    re.compile(r"Path\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*read_(?:text|bytes)"),
    # node: fs.readFileSync('path' ...) / readFile('path' ...)
    re.compile(r"\breadFileSync\s*\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\breadFile\s*\(\s*['\"]([^'\"]+)['\"]"),
    # php: file_get_contents('path')
    re.compile(r"\bfile_get_contents\s*\(\s*['\"]([^'\"]+)['\"]"),
    # ruby: File.read('path') / IO.read('path')
    re.compile(r"\b(?:File|IO)\.(?:read|readlines|binread)\s*\(\s*['\"]([^'\"]+)['\"]"),
    # perl: open(FH, "< path") / open($fh, '<', 'path')
    re.compile(r"\bopen\s*\([^,]+,\s*['\"]\s*<?\s*([^'\"]+?)['\"]"),
    re.compile(r"\bopen\s*\([^,]+,\s*['\"]<['\"]\s*,\s*['\"]([^'\"]+)['\"]"),
)

# Tokens that are operators / redirections, never file targets.
_OPERATOR_PREFIXES: tuple[str, ...] = ("-", "<", ">", "|", "&", "$", "(", ")", "`")


@dataclass(slots=True)
class ReadTarget:
    """A file path a command would read, plus the shape that revealed it."""

    path: str
    shape: str  # "reader:cat", "inline:python", "sqlite3", "copy_source", ...


@dataclass(slots=True)
class CommandReadDecision:
    """Result of running detected read targets through the host-read policy."""

    blocked: bool
    targets: list[ReadTarget] = field(default_factory=list)
    blocked_target: ReadTarget | None = None
    level: str = ""
    reason: str = ""


def _tokenize(segment: str) -> list[str]:
    """Split a single command segment into tokens, honoring quotes.

    Quotes are stripped from the returned tokens. Not a full shell parser
    — good enough to pull out base command + positional file args.
    """
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(segment)
    have = False
    while i < n:
        c = segment[i]
        if quote is not None:
            if quote == '"' and c == "\\" and i + 1 < n:
                buf.append(segment[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
                i += 1
                continue
            buf.append(c)
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            have = True
            i += 1
            continue
        if c.isspace():
            if have or buf:
                tokens.append("".join(buf))
                buf.clear()
                have = False
            i += 1
            continue
        buf.append(c)
        i += 1
    if have or buf:
        tokens.append("".join(buf))
    return tokens


def _strip_env_prefix(tokens: list[str]) -> list[str]:
    """Drop leading ``VAR=value`` assignments (``ENV=x cmd ...``)."""
    out = list(tokens)
    while out and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", out[0]):
        out.pop(0)
    return out


def _looks_like_path(token: str) -> bool:
    if not token:
        return False
    if token.startswith(_OPERATOR_PREFIXES):
        return False
    return True


def _quoted_bodies(segment: str) -> list[str]:
    """Return every quoted run in a segment (the interpreter-body source).

    For ``python -c "print(open('.env').read())"`` the outer double-quoted
    body is returned; the inner single-quoted ``.env`` stays inside it so
    the read-call regexes can find it.
    """
    bodies: list[str] = []
    i = 0
    n = len(segment)
    while i < n:
        c = segment[i]
        if c in ("'", '"'):
            quote = c
            j = i + 1
            buf: list[str] = []
            while j < n:
                cj = segment[j]
                if quote == '"' and cj == "\\" and j + 1 < n:
                    buf.append(segment[j + 1])
                    j += 2
                    continue
                if cj == quote:
                    break
                buf.append(cj)
                j += 1
            bodies.append("".join(buf))
            i = j + 1
            continue
        i += 1
    return bodies


def _detect_segment(segment: str, depth: int) -> list[ReadTarget]:
    targets: list[ReadTarget] = []
    tokens = _strip_env_prefix(_tokenize(segment))
    if not tokens:
        return targets
    base = _normalize_basename(tokens[0])
    args = tokens[1:]

    # Shell wrapper: recurse into the -c / -lc body so nested reads count.
    if base in _SHELL_WRAPPERS and depth < 3:
        for k, tok in enumerate(args):
            flag = tok.lstrip("-").lower()
            if "c" in flag and k + 1 < len(args):
                inner = args[k + 1]
                for seg in _split_chained(inner):
                    targets.extend(_detect_segment(seg, depth + 1))
                return targets
        return targets

    # Plain readers: every non-flag positional is a read target.
    if base in _READER_COMMANDS:
        for tok in args:
            if _looks_like_path(tok):
                targets.append(ReadTarget(path=tok, shape=f"reader:{base}"))
        return targets

    # Value-flag readers (openssl -in FILE, certutil -f FILE / -encode FILE).
    if base in _VALUE_FLAG_READERS:
        flags = _VALUE_FLAG_READERS[base]
        for k, tok in enumerate(args):
            if tok.lower() in flags and k + 1 < len(args):
                cand = args[k + 1]
                if _looks_like_path(cand):
                    targets.append(ReadTarget(path=cand, shape=f"reader:{base}"))
        # certutil -encode <file> <out>: the encoded source is positional.
        if base == "certutil":
            positionals = [t for t in args if not t.startswith("-")]
            if positionals:
                cand = positionals[0]
                if _looks_like_path(cand):
                    targets.append(ReadTarget(path=cand, shape="reader:certutil"))
        return targets

    # Interpreters: scan ONLY their inline bodies for read-call literals.
    if base in _INTERPRETERS:
        for body in _quoted_bodies(segment):
            for pat in _INLINE_READ_PATTERNS:
                for m in pat.finditer(body):
                    cand = m.group(1).strip()
                    if cand and not cand.startswith(("http://", "https://")):
                        targets.append(
                            ReadTarget(path=cand, shape=f"inline:{base}"),
                        )
        return targets

    # sed / awk / grep: program/pattern then file(s). The file content is
    # read to stdout (sed prints, awk processes, grep matches).
    if base in _PROGRAM_THEN_FILES:
        has_program_flag = any(
            a in _PROGRAM_VALUE_FLAGS or a.startswith(("-e", "-f", "--regexp", "--file"))
            for a in args
        )
        positionals: list[str] = []
        skip_next = False
        for tok in args:
            if skip_next:
                skip_next = False
                continue
            if tok in _PROGRAM_VALUE_FLAGS:
                skip_next = True
                continue
            if tok.startswith("-"):
                continue
            positionals.append(tok)
        # Without -e/-f the FIRST positional is the script/pattern, not a
        # file; the rest are files. With -e/-f all positionals are files.
        files = positionals if has_program_flag else positionals[1:]
        for cand in files:
            if _looks_like_path(cand):
                targets.append(ReadTarget(path=cand, shape=f"reader:{base}"))
        return targets

    # sqlite3 <db> with a read verb dumps/queries DB content.
    if base == "sqlite3":
        rest = " ".join(args)
        reads = any(t in args for t in _SQLITE_READ_TOKENS) or bool(
            _SQLITE_READ_KEYWORDS.search(rest),
        )
        # The DB path is the first positional that is neither a flag nor a
        # sqlite dot-command (.dump / .schema / ...).
        db = next(
            (
                t
                for t in args
                if not t.startswith("-") and not t.startswith(".") and _looks_like_path(t)
            ),
            None,
        )
        # Bare ``sqlite3 db`` opens an interactive shell over the DB; still
        # an exposure of DB content, so treat any db path as a target.
        if db and (reads or len(args) >= 1):
            targets.append(ReadTarget(path=db, shape="sqlite3"))
        return targets

    # cp/mv/etc: run the read policy on the SOURCE (first positional path).
    if base in _COPY_COMMANDS:
        positionals = [t for t in args if not t.startswith("-")]
        # dd if=<file> form.
        for tok in args:
            if tok.lower().startswith("if="):
                cand = tok.split("=", 1)[1]
                if _looks_like_path(cand):
                    targets.append(ReadTarget(path=cand, shape="copy_source"))
        if len(positionals) >= 2:  # need a source AND a dest to be a copy
            src = positionals[0]
            if _looks_like_path(src):
                targets.append(ReadTarget(path=src, shape="copy_source"))
        return targets

    # dd if=<file> (dd is its own base command).
    if base == "dd":
        for tok in args:
            if tok.lower().startswith("if="):
                cand = tok.split("=", 1)[1]
                if _looks_like_path(cand):
                    targets.append(ReadTarget(path=cand, shape="copy_source"))
        return targets

    # tar/zip writing a specific member to stdout (-O extract, or `-f -`).
    if base in ("tar", "zip"):
        joined = " ".join(args)
        to_stdout = "-O" in args or "-" in args or "/dev/stdout" in joined
        if to_stdout:
            for tok in args:
                if tok != "-" and _looks_like_path(tok) and not tok.startswith("/dev"):
                    targets.append(ReadTarget(path=tok, shape=f"archive:{base}"))
        return targets

    return targets


def _canon_path(path: str) -> str:
    """Canonical-ish path for equivalence matching (NOT a real resolve).

    - strip surrounding quotes + whitespace
    - backslashes → forward slashes
    - collapse repeated ``//`` and internal ``/./``
    - drop leading ``./``
    - strip a trailing ``/``
    - PRESERVE ``..`` so host_read_decision's traversal check still fires.

    Two spellings of the same path (``logs/foo.txt`` vs ``./logs/foo.txt``)
    canonicalize to the same string so provenance + grant matching line up.
    """
    canon, _ = _canon_with_dirflag(path)
    return canon


def _canon_with_dirflag(path: str) -> tuple[str, bool]:
    """Return (canonical_path, ended_with_separator).

    ``ended_with_separator`` is computed BEFORE the trailing slash is
    stripped, so a copy destination written as ``logs/`` is recognized as a
    directory.
    """
    t = (path or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        t = t[1:-1].strip()
    t = t.replace("\\", "/")
    while "//" in t:
        t = t.replace("//", "/")
    while "/./" in t:
        t = t.replace("/./", "/")
    while t.startswith("./"):
        t = t[2:]
    is_dir = t.endswith("/")
    if len(t) > 1:
        t = t.rstrip("/")
    return t, is_dir


def _basename(path: str) -> str:
    canon = _canon_path(path)
    return canon.rsplit("/", 1)[-1] if "/" in canon else canon


def detect_copy_edges(command: str) -> dict[str, str]:
    """Map a copy/move DESTINATION path → its SOURCE path, across the chain.

    Copying undiscovered indexed source (or a secret) into an artifact-shaped
    destination (``cp src/foo.py logs/foo.txt``) and then reading the
    destination would launder the source past the read gate. This map lets
    the policy carry the SOURCE's read decision forward to any later read of
    the DEST. (``cp src dst dir`` with multiple sources maps each
    ``dir/basename(src) → src``.)
    """
    edges: dict[str, str] = {}
    for segment in _split_chained(command):
        tokens = _strip_env_prefix(_tokenize(segment))
        if not tokens:
            continue
        base = _normalize_basename(tokens[0])
        args = tokens[1:]
        if base in _COPY_COMMANDS:
            positionals = [t for t in args if not t.startswith("-")]
            if len(positionals) >= 2:
                dest = positionals[-1]
                sources = positionals[:-1]
                dnorm, dest_is_dir = _canon_with_dirflag(dest)
                # dest is a directory when written with a trailing slash OR
                # when there are multiple sources (cp a b c dir/). Each
                # source then lands at <dir>/<basename(source)>.
                if dest_is_dir or len(sources) > 1:
                    for s in sources:
                        if not _looks_like_path(s):
                            continue
                        edges[f"{dnorm}/{_basename(s)}"] = s
                elif _looks_like_path(sources[0]):
                    # Single source, no trailing slash → STATICALLY AMBIGUOUS:
                    # `cp src/foo.py logs` is a file rename if `logs` is a
                    # file, but a dir-copy (→ logs/foo.py) if `logs` already
                    # exists as a directory. We can't stat at parse time, so
                    # map BOTH forms; the provenance lookup only fires when a
                    # later read actually targets one of them.
                    s = sources[0]
                    edges[dnorm] = s
                    edges[f"{dnorm}/{_basename(s)}"] = s
        elif base == "dd":
            src = dst = None
            for tok in args:
                low = tok.lower()
                if low.startswith("if="):
                    src = tok.split("=", 1)[1]
                elif low.startswith("of="):
                    dst = tok.split("=", 1)[1]
            if src and dst and _looks_like_path(src) and _looks_like_path(dst):
                edges[_canon_path(dst)] = src
    return edges


def detect_read_targets(command: str) -> list[ReadTarget]:
    """Detect every file path ``command`` would read, across all segments.

    Returns an empty list for commands with no file-read shape (the common
    case: ``pytest``, ``npm test``, ``git status``, ``echo hi``).
    """
    if not command or not command.strip():
        return []
    out: list[ReadTarget] = []
    for segment in _split_chained(command):
        out.extend(_detect_segment(segment, depth=0))
    # De-dup on (path, shape) preserving order.
    seen: set[tuple[str, str]] = set()
    deduped: list[ReadTarget] = []
    for t in out:
        key = (t.path, t.shape)
        if key not in seen:
            seen.add(key)
            deduped.append(t)
    return deduped


# Levels where the file cannot be read by ANY tool — the redirect hint ("use
# ai_find") is irrelevant and misleading. #275: keep redirect-vs-hardblock
# distinct in the operator-facing message.
_HARD_BLOCK_LEVELS = frozenset({"sensitive_file_protection"})


def _ai_run_block_reason(path: str, shape: str, decision: GateDecision) -> str:
    """Operator-facing ai_run read-refusal message. #275: branch the trailing
    hint on decision.level so a SECRET hard-block reads as a security boundary
    (no "use ai_find" — the file cannot be read at all), while an indexed/
    undiscovered-source REDIRECT points at the indexed reads. The block decision
    itself is unchanged; this is message text only."""
    base = (
        f"ai_run blocked: command would read '{path}' "
        f"({shape}), which the read gate refuses "
        f"[{decision.level}]. " + (decision.reason or "")
    )
    if (decision.level or "") in _HARD_BLOCK_LEVELS:
        tail = (
            " This is a security boundary — the file cannot be read by any tool "
            "(Read or a command that reads it); user intent does not override."
        )
    else:
        tail = (
            " Reading file content by running a command is subject to the same "
            "policy as the Read tool — use ai_find / ai_bundle / ai_get_lines "
            "for indexed source."
        )
    return (base + tail).strip()


def evaluate_command_read_policy(
    command: str,
    gate_state: dict[str, Any] | None,
) -> CommandReadDecision:
    """Run every detected read target through ``host_read_decision``.

    Blocks on the FIRST target the host-read policy would refuse — the
    same allow/block law the Read tool enforces. Safe artifacts
    (``tmp/output.log``) pass; secrets, undiscovered indexed source, and
    unknown-external paths block.
    """
    state = gate_state if isinstance(gate_state, dict) else {}
    targets = detect_read_targets(command)
    # Provenance map: a later read of a copy DEST inherits the SOURCE's
    # read decision (sealing the cp src/foo.py logs/foo.txt && cat
    # logs/foo.txt launder-through-artifact bypass).
    copy_edges = detect_copy_edges(command)

    def _reason(path: str, shape: str, decision: GateDecision) -> str:
        return _ai_run_block_reason(path, shape, decision)

    for t in targets:
        # The copy SEGMENT itself: a regular indexed-source copy is a file
        # op (not a read into context), so block it directly only for
        # PROTECTED sources (secrets/bootstrap/traversal). The
        # indexed-source-via-copy case is handled by provenance below when
        # the DEST is later read.
        if t.shape.startswith("copy_source"):
            decision: GateDecision | None = host_read_hard_block(
                _canon_path(t.path),
            )
            if decision is not None and not decision.allowed:
                return CommandReadDecision(
                    blocked=True,
                    targets=targets,
                    blocked_target=t,
                    level=decision.level,
                    reason=_reason(t.path, t.shape, decision),
                )
            continue

        # Reader / inline / sqlite / archive: the target's own read policy.
        # Canonicalize so ./-spellings match grants (known_exact_paths).
        decision = host_read_decision(state, _canon_path(t.path))
        if not decision.allowed:
            return CommandReadDecision(
                blocked=True,
                targets=targets,
                blocked_target=t,
                level=decision.level,
                reason=_reason(t.path, t.shape, decision),
            )

        # Provenance: if this read targets a path that was copied FROM some
        # source earlier in the chain, the SOURCE's full read decision
        # applies — an undiscovered indexed source (or secret) laundered
        # into an artifact-shaped dest stays blocked. Matching is on
        # canonical forms so ./ and trailing-slash spellings line up.
        source = copy_edges.get(_canon_path(t.path))
        if source:
            sdec = host_read_decision(state, _canon_path(source))
            if not sdec.allowed:
                return CommandReadDecision(
                    blocked=True,
                    targets=targets,
                    blocked_target=ReadTarget(path=source, shape="copy_provenance"),
                    level=sdec.level,
                    reason=(
                        f"ai_run blocked: command copies '{source}' to "
                        f"'{t.path}' then reads it back. The destination "
                        f"inherits the source's read policy [{sdec.level}]. "
                        + (sdec.reason or "")
                        + " Copying a file does not launder it past the read "
                        "gate — use ai_find / ai_bundle / ai_get_lines for "
                        "indexed source, and don't exfiltrate secrets."
                    ).strip(),
                )
    return CommandReadDecision(blocked=False, targets=targets)
