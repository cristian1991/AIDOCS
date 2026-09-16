"""Shared shell semantic projection: mask DATA-ONLY windows so prose payloads
cannot impersonate executable syntax to the danger/judge matchers.

The danger detectors (bash_policy._evaluate_dangerous_chain, the destructive
floor, the heuristic judge) scan the raw command string for execution shapes
(`rm -rf /`, `curl … | sh`, `$(…)`). But a git COMMIT MESSAGE that merely
QUOTES those shapes — e.g. `git commit -m "fixed cat $(rm victim)"` — is DATA,
never executed, yet it tripped the matchers and froze the session.

``mask_data_windows`` returns the command with proven data-only windows
replaced by spaces (SAME length / positions, so existing regexes + offsets are
unaffected). Matchers run against the masked surface: execution tokens stay
visible, data prose disappears. Masking with spaces can only REMOVE matches,
never synthesise one, so it cannot weaken execution detection — and it is
fail-safe: any parse it cannot do confidently leaves the text un-masked
(strict matching preserved).

Scope is deliberately narrow — only proven data windows:
  * git message payloads: ``-m``/``--message`` (and ``=`` forms) on a git
    commit/tag/merge/notes/stash SEGMENT (chain-bounded, so `cd x && git
    commit -m "…"` masks only the message and a real `&& rm -rf /` stays
    visible). NOT a blanket `-m` mask — `python -m <module>` is execution.
  * heredoc bodies (``<<TAG`` … ``TAG``) ONLY when fed to a PROVEN inert-data
    reader (cat/grep/…). A heredoc fed to an interpreter/shell/eval/awk is the
    program, not data: it stays VISIBLE and ``heredoc_fail_closed`` refuses it
    before spawn.
"""

from __future__ import annotations

import re

# git message value on a git commit/tag/merge/notes/stash segment. The segment
# scope `(?:(?![;&|\n]).)*?` keeps us before any chain operator, so only THIS
# git command's message is masked.
#
# The value is ONE SHELL WORD: a run of ADJACENT quoted and unquoted chunks,
# which is what the shell concatenates into a single argv element.
#
# MEASURED DEFECT (#588 specimen 2, 2026-07-29). The value used to be the
# alternation `"…" | '…' | bare-token` — at most ONE chunk. `shlex.quote`, which
# `mcp_server._git_commit_command` applies to every `ai_git(op='commit')`
# message, renders an apostrophe as a THREE-chunk word:
#
#     don't   ->   'don'"'"'t'
#
# so `'[^']*'` matched only `'don'` and the REST OF THE PROSE stayed visible to
# every shape rule. Commit messages carry apostrophes constantly; five distinct
# rules (BASH_SUDO, BASH_OVERWRITE_REDIRECT, CFG_GIT_SWITCH,
# EGRESS_UNPARSEABLE_DESTINATION, …) refused five honest commits in one night.
#
# The word deliberately STOPS at unquoted whitespace, `;`, `&`, `|`, `<`, `>` —
# so a real redirect or chain AFTER the message (`git commit -m 'ok' > /etc/motd`,
# `git commit -m 'ok' && sudo rm -rf /var`) lies outside the window and stays
# fully graded. The unquoted branch also excludes the quote characters, so an
# UNTERMINATED quote matches nothing at all and nothing is masked (fail-safe:
# strict matching preserved). Whether the matched word is actually inert is then
# decided per chunk by `_git_message_is_literal`.
_GIT_MSG_VALUE = re.compile(
    r"(?:^|[;&|\n])\s*git\s+(?:commit|tag|merge|notes|stash)\b"
    r"(?:(?![;&|\n]).)*?"
    r"(?:-m|--message)(?:=|\s+)"
    r"(?P<val>(?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s;&|<>'\"])+)",
    re.IGNORECASE | re.DOTALL,
)

# heredoc opener: << / <<- with an optional quoted tag, then the body up to a
# line that is exactly the tag. Body (group "body") is data.
_HEREDOC = re.compile(
    r"<<-?\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)"
    r"(?P<opener_tail>[^\n]*)\n"  # rest of the opener LINE (pipeline `| python`,
    # redirection `> file`) — deliberately OUTSIDE <body>, so it is never masked
    # and stays fully visible to the policy + judge matchers.
    r"(?P<body>.*?)"
    r"(?:^|\n)[ \t]*(?P=tag)(?:$|\n)",
    re.DOTALL | re.MULTILINE,
)

# Output-redirection on a heredoc opener line (`>`, `>>`, `2>`, `&>`). A heredoc
# whose reader writes its (attacker-controlled) body to a file is a WRITER sink,
# never inert — refuse before spawn.
_REDIRECT_RE = re.compile(r"(?:^|[^0-9<>])(?:\d*>>?|&>)")


# PROVEN inert-data stdin readers: they read the heredoc as data and
# emit/filter/display it — never EXECUTE it and never WRITE it to an arbitrary
# path. Only these have their heredoc body masked + the command stays usable.
# Deliberately tight: `tee`/`sed` (write / `e`-execute), `awk` (executes),
# every interpreter/shell, and any unknown command are NOT here → fail closed.
_INERT_HEREDOC_CONSUMERS: frozenset[str] = frozenset(
    {
        "cat",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "head",
        "tail",
        "sort",
        "uniq",
        "wc",
        "nl",
        "comm",
        "column",
        "cut",
        "tr",
        "rev",
        "diff",
        "less",
        "more",
        "tac",
        "git.commit-message",
    },
)

_ENV_PREFIX_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def _basename(token: str) -> str:
    t = token.strip().replace("\\", "/")
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        t = t[1:-1]
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    if t.lower().endswith(".exe"):
        t = t[:-4]
    return t.lower()


_GIT_COMMIT_STDIN_MESSAGE = re.compile(
    r"^\s*git\s+commit\b(?:(?![;&|\n]).)*(?:-F\s+-|--file(?:=|\s+)-)(?:\s|$)",
    re.IGNORECASE,
)
_SHELL_CODE_PREFIX = re.compile(
    r"\b(?:ba|z|k|c|d)?sh\b|\beval\b|\bsource\b|(?:^|\s)\.\s|\bssh\b",
    re.IGNORECASE,
)
_SINGLE_QUOTED = re.compile(r"'[^']*'", re.DOTALL)


def _git_message_is_literal(value: str) -> bool:
    """True only when a parsed git message WORD cannot run substitution.

    The word is a concatenation of adjacent chunks (see `_GIT_MSG_VALUE`), so
    literality is decided PER CHUNK, not from the word's first and last
    character. `shlex.quote("don't")` is `'don'"'"'t'` — it both starts and ends
    with a single quote while carrying a double-quoted chunk in the middle, so
    the old "starts and ends with `'`" shortcut would also have called
    `'x'"$(rm -rf /var)"'y'` literal. Each chunk now stands on its own:

      * single-quoted -> always literal (POSIX: no expansion whatsoever)
      * double-quoted -> literal only without a `$` expansion or a backtick
      * unquoted      -> literal only without a `$` expansion or a backtick

    Anything the chunk parser cannot close (an unterminated quote) returns
    False, leaving the window UNMASKED so every matcher sees the raw text. That
    is the fail-safe direction: a mask that does not happen can only make the
    verdict stricter, never weaker.
    """
    i = 0
    n = len(value)
    if not n:
        return False
    while i < n:
        c = value[i]
        if c == "'":
            close = value.find("'", i + 1)
            if close < 0:
                return False
            i = close + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and value[j] != '"':
                j += 2 if value[j] == "\\" else 1
            if j >= n:
                return False
            inner = value[i + 1 : j]
            if "$" in inner or "`" in inner:
                return False
            i = j + 1
            continue
        if c in "$`":
            return False
        i += 2 if c == "\\" else 1
    return True


def _heredoc_sink_kind(stage: str) -> str:
    """Classify a heredoc consumer, preserving the safe git message shape."""

    seg = _ENV_PREFIX_RE.sub("", stage).strip()
    if not seg:
        return ""
    base = _basename(seg.split()[0])
    if base == "git" and _GIT_COMMIT_STDIN_MESSAGE.search(seg):
        return "git.commit-message"
    return base


def _heredoc_pipeline_sinks(s: str, lt_pos: int) -> tuple[list[str], bool]:
    """Pipeline-aware stdin law for the heredoc whose ``<<`` starts at ``lt_pos``.

    A heredoc feeds its consumer's stdin, but the consumer's stdout flows down
    the pipeline — so the body (or a transform of it) reaches EVERY downstream
    sink. Returns ``(base_commands, has_output_redirect)`` for the consumer
    stage and every stage to its right, within the SAME pipeline statement
    (bounded by ``;`` / ``&&`` / ``||`` / newline; single ``|`` does NOT bound).

    The body is inert-usable only when every returned base command is a proven
    inert reader AND there is no output redirect. An empty/undeterminable stage
    yields ``""`` so the caller fails closed.
    """
    # Physical opener line containing the `<<`.
    line_start = s.rfind("\n", 0, lt_pos) + 1
    nl = s.find("\n", lt_pos)
    line_end = len(s) if nl == -1 else nl
    line = s[line_start:line_end]
    rel = lt_pos - line_start

    # Statement containing the heredoc: bounded by ; && || (NOT single |).
    stmt_start, stmt_end = 0, len(line)
    for mt in re.finditer(r";|&&|\|\|", line):
        if mt.end() <= rel:
            stmt_start = mt.end()
        elif mt.start() >= rel:
            stmt_end = mt.start()
            break
    stmt = line[stmt_start:stmt_end]
    rel_in_stmt = rel - stmt_start

    has_redirect = bool(_REDIRECT_RE.search(stmt))

    # Pipeline stages within the statement (single `|`); keep consumer + right.
    stages: list[tuple[int, str]] = []  # (start_offset, stage_text)
    last = 0
    for mt in re.finditer(r"\|", stmt):
        stages.append((last, stmt[last : mt.start()]))
        last = mt.end()
    stages.append((last, stmt[last:]))
    cons_idx = 0
    for i, (off, _txt) in enumerate(stages):
        if off <= rel_in_stmt:
            cons_idx = i
    sinks: list[str] = []
    for _off, txt in stages[cons_idx:]:
        sinks.append(_heredoc_sink_kind(txt))
    return sinks, has_redirect


def _heredoc_is_inert(s: str, lt_pos: int) -> bool:
    """True only when EVERY pipeline sink is a proven inert reader and there is
    no output redirect — i.e. the heredoc body is provably never executed,
    interpreted, or written to a file."""
    sinks, has_redirect = _heredoc_pipeline_sinks(s, lt_pos)
    if has_redirect or not sinks:
        return False
    return all(c in _INERT_HEREDOC_CONSUMERS for c in sinks)


def heredoc_fail_closed(command: str) -> list[str]:
    """Consumer base command of every heredoc whose consumer is NOT a proven
    inert-data reader (interpreter / shell / eval / source / awk / writer /
    undecidable). Non-empty → the command feeds execution-bearing stdin and
    MUST be refused before spawn. Empty → no risky heredoc.
    """
    s = command or ""
    if not s:
        return []
    try:
        buf = list(s)
        for m in _GIT_MSG_VALUE.finditer(s):
            if _git_message_is_literal(m.group("val")):
                a, b = m.span("val")
                _blank_span(buf, a, b)
        masked_msgs = "".join(buf)
        risky: list[str] = []
        for m in _HEREDOC.finditer(masked_msgs):
            sinks, has_redirect = _heredoc_pipeline_sinks(masked_msgs, m.start())
            bad = [c if c else "<unknown>" for c in sinks if c not in _INERT_HEREDOC_CONSUMERS]
            if has_redirect:
                bad.append("<redirect>")
            if not sinks:
                bad.append("<unknown>")
            if bad:
                risky.append(",".join(bad))
        return risky
    except Exception:
        return ["<unparsed>"] if "<<" in s else []


def _blank_span(buf: list[str], start: int, end: int) -> None:
    for i in range(start, min(end, len(buf))):
        if buf[i] != "\n":  # keep newlines so line structure (and chain) holds
            buf[i] = " "


def mask_git_message_values(command: str) -> str:
    """Return ``command`` with proven-literal git message VALUES blanked.

    The git-message half of `mask_data_windows`, exported so every matcher that
    needs "the executable surface minus the commit prose" shares ONE definition
    of where that prose starts and ends (#588). `heuristic_judge` used to carry
    a second, weaker pattern of its own; two definitions is how one of them
    stays broken after the other is fixed.
    """
    s = command or ""
    if not s:
        return s
    try:
        buf = list(s)
        for m in _GIT_MSG_VALUE.finditer(s):
            if _git_message_is_literal(m.group("val")):
                a, b = m.span("val")
                _blank_span(buf, a, b)
        return "".join(buf)
    except Exception:
        return s


# #865 (2026-08-21): the quoted arguments of an inert PATTERN READER are
# data. `grep -rl -E 'scp|ssh|<vps-ip>'` was refused as
# EGRESS_UNPARSEABLE_DESTINATION — the words `ssh`/`scp` and the IP sat in
# (The address is written as a placeholder ON PURPOSE: the literal one tripped
# the aidocs-no-hardcoded-vps-ip deploy law from inside this very comment,
# blocking a deploy on 2026-08-22. The example loses nothing without it.)
# grep's quoted PATTERN, a search string that executes nothing. Same law as
# the commit-message window: mask only what is PROVEN literal (single-quoted
# always; double-quoted only without `$`/backtick — a "$(…)" pattern stays
# visible and fails closed), bounded to the reader's own segment (an
# unquoted `;`/`&`/`|`/newline ends the walk, so a chained real network
# call keeps its destination visible).
_PATTERN_READER_CMD = re.compile(
    r"(?:^|[;&|\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:grep|egrep|fgrep|rg)\b",
    re.IGNORECASE,
)


def _pattern_reader_quoted_windows(s: str) -> list[tuple[int, int]]:
    """Spans of proven-literal quoted chunks inside grep/rg segments.

    Quote-aware forward walk (a `|` INSIDE the quoted pattern must not end
    the segment — `grep -E 'scp|ssh' f` is one segment). Unterminated
    quotes mask nothing (fail-safe: unmasked text can only grade stricter).
    """
    out: list[tuple[int, int]] = []
    n = len(s)
    for m in _PATTERN_READER_CMD.finditer(s):
        i = m.end()
        while i < n:
            c = s[i]
            if c == "'":
                close = s.find("'", i + 1)
                if close < 0:
                    break  # unterminated → leave visible
                out.append((i, close + 1))
                i = close + 1
                continue
            if c == '"':
                j = i + 1
                while j < n and s[j] != '"':
                    j += 2 if s[j] == "\\" else 1
                if j >= n:
                    break  # unterminated → leave visible
                inner = s[i + 1 : j]
                if "$" not in inner and "`" not in inner:
                    out.append((i, j + 1))
                i = j + 1
                continue
            if c in ";&|\n":
                break  # unquoted separator ends the reader's segment
            i += 1
    return out


def mask_data_windows(command: str) -> str:
    """Return ``command`` with data-only windows blanked (same length).

    Execution-bearing tokens are untouched; only proven prose payloads (git
    message values + heredoc bodies + pattern-reader quoted arguments) are
    blanked. Fail-safe: returns the input unchanged on any internal error."""
    s = command or ""
    if not s:
        return s
    try:
        buf = list(s)
        for m in _GIT_MSG_VALUE.finditer(s):
            if _git_message_is_literal(m.group("val")):
                a, b = m.span("val")
                _blank_span(buf, a, b)
        # Heredoc bodies (run on the git-masked text so positions still align).
        s2 = "".join(buf)
        buf2 = list(s2)
        for m in _HEREDOC.finditer(s2):
            if m.group("q") and _heredoc_is_inert(s2, m.start()):
                a, b = m.span("body")
                _blank_span(buf2, a, b)
        # #865: quoted arguments of inert pattern readers (grep/rg family).
        s3 = "".join(buf2)
        buf3 = list(s3)
        for a, b in _pattern_reader_quoted_windows(s3):
            _blank_span(buf3, a, b)
        return "".join(buf3)
    except Exception:
        return s


def mask_shell_literal_windows(command: str) -> str:
    """Mask spans that the current shell proves literal before substitution scan.

    Quoted heredoc delimiters suppress shell expansion. Ordinary single-quoted
    strings do too, except when the quote is a program argument to a nested
    shell/eval/ssh consumer; those remain visible and fail closed.
    """

    s = command or ""
    if not s:
        return s
    try:
        buf = list(mask_data_windows(s))
        current = "".join(buf)
        for match in _HEREDOC.finditer(current):
            if match.group("q"):
                start, end = match.span("body")
                _blank_span(buf, start, end)

        current = "".join(buf)
        for match in _SINGLE_QUOTED.finditer(current):
            segment_start = max(
                current.rfind("\n", 0, match.start()),
                current.rfind(";", 0, match.start()),
                current.rfind("&", 0, match.start()),
                current.rfind("|", 0, match.start()),
            )
            prefix = current[segment_start + 1 : match.start()]
            if _SHELL_CODE_PREFIX.search(prefix):
                continue
            start, end = match.span()
            _blank_span(buf, start, end)
        return "".join(buf)
    except Exception:
        return s
