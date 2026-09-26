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
    r"(?:^|[;&|\n])\s*(?P<git>git)\s+(?:commit|tag|merge|notes|stash)\b"
    r"(?:(?![;&|\n]).)*?"
    r"(?:-m|--message)(?:=|\s+)"
    r"(?P<val>(?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s;&|<>'\"])+)",
    re.IGNORECASE | re.DOTALL,
)

# NOTE (r0b, blast-radius-ax round 3): the legacy `_HEREDOC` regex that used to
# live here is GONE, and nothing consumes it any more. It was a second, weaker
# heredoc grammar — identifier-only tags, blind to `<<'PY-1'` / `<<$TAG` — and
# `heredoc_fail_closed` fell back to it whenever `_shell_lex` could prove
# nothing. That let ONE recognised inert heredoc launder every heredoc the
# lexer could not prove. `_shell_lex` is now the ONLY heredoc grammar here.

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
        # ONE heredoc authority (r0b, blast-radius-ax rounds 2-3). The REFUSAL
        # sees exactly the heredocs the masking half sees: `_shell_lex`.
        #
        # There is deliberately NO fallback parser. The old code fell back to
        # the identifier-only `_HEREDOC` regex when the lexer could prove
        # nothing and returned `<unparsed>` only when that regex matched
        # NOTHING — so a single old-regex-recognisable INERT heredoc
        # (`cat <<'EOF' … EOF`) suppressed the guard for every heredoc the
        # lexer could not prove, and an execution-bearing one beside it
        # (`python <<$TAG … $TAG`) became invisible again. "One authority"
        # cannot mean "the lexer, then a weaker grammar when inconvenient":
        # one parsed heredoc must never launder another's uncertainty.
        #
        # So: lexer proves it, or — if heredoc syntax may be present at all —
        # the command is refused as `<unparsed>`. Masking stays raw/visible on
        # the same uncertainty (`mask_infrastructure_data_windows` returns the
        # RAW command when `ok` is False), so refusal and masking agree.
        # Fewer false positives come from enriching `_shell_lex`, never from
        # reintroducing a second grammar.
        ok, _top, docs = _shell_lex(masked_msgs)
        if not ok:
            return ["<unparsed>"] if "<<" in masked_msgs.replace("<<<", "") else []
        positions = [d.pos for d in docs]
        risky: list[str] = []
        for pos in positions:
            sinks, has_redirect = _heredoc_pipeline_sinks(masked_msgs, pos)
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


class _Heredoc:
    """One syntactically real, COMPLETE heredoc, as proven by `_shell_lex`."""

    __slots__ = (
        "pos",
        "quoted",
        "top_level",
        "tail_start",
        "body_start",
        "body_end",
        "after",
    )

    def __init__(self, pos: int, quoted: bool, top_level: bool, tail_start: int = -1) -> None:
        self.pos = pos  # index of the `<<`
        self.quoted = quoted  # delimiter quoted/escaped => body is literal
        self.top_level = top_level  # not inside $( ) / backticks / quotes
        # index just past the parsed delimiter word — everything from here to
        # the opener line's newline is the heredoc's OWN tail (pipeline,
        # redirection, further operands). Callers that must prove a heredoc's
        # consumer is literally one command need to see it.
        self.tail_start = tail_start
        self.body_start = -1
        self.body_end = -1  # exclusive; the terminator line is excluded
        self.after = -1  # index just past the terminator line


_WORD_END = " \t\n;&|<>()"


def _shell_lex(s: str) -> tuple[bool, list[bool], list[_Heredoc]]:  # noqa: C901
    """Minimal lexical walk proving WHERE heredocs really are (blast-radius-ax).

    Tracks single/double/ANSI-C quotes, backslash escapes, comments, command
    substitution `$( )` / backticks and arithmetic `$(( ))` / `(( ))`, so a
    `<<` inside a quote, a comment, an arithmetic shift, or a here-string
    `<<<` is never read as an opener. Heredoc bodies are consumed after the
    opener line's newline as the shell does (terminator = the whole line,
    leading tabs stripped only for `<<-`).

    Returns ``(ok, top, heredocs)``: ``top[i]`` is True when char ``i`` is
    unquoted top-level command text; ``heredocs`` are complete openers only.
    ``ok`` is False on ANY uncertainty (unterminated quote / substitution /
    heredoc, empty delimiter, process substitution, construct not modelled) —
    a heredoc DELIMITER word is not expanded by the shell, so `<<$TAG` is the
    literal delimiter `$TAG` with an expansion-bearing body; callers
    must then mask NOTHING. Masking is permission; this may only fail stricter.
    """
    n = len(s)
    top = [False] * n
    docs: list[_Heredoc] = []
    stack: list[list] = []  # frames: ["dq"], ["cmd"], ["bt"], ["ar", depth]
    pending: list[tuple[_Heredoc, str, bool]] = []
    i = 0
    while i < n:
        c = s[i]
        ctx = stack[-1][0] if stack else "top"
        if not stack:
            top[i] = True
        if ctx == "ar":
            if s.startswith("$((", i):
                stack.append(["ar", 0])
                i += 3
            elif s.startswith("$(", i):
                stack.append(["cmd"])
                i += 2
            elif c == "(":
                stack[-1][1] += 1
                i += 1
            elif c == ")":
                if stack[-1][1] > 0:
                    stack[-1][1] -= 1
                    i += 1
                elif s.startswith("))", i):
                    stack.pop()
                    i += 2
                else:
                    return False, top, []
            elif c in "'\"`\n":
                return False, top, []  # not modelled inside arithmetic
            else:
                i += 1
            continue
        if ctx == "dq":
            if c == "\\":
                i += 2
            elif c == '"':
                stack.pop()
                i += 1
            elif s.startswith("$((", i):
                stack.append(["ar", 0])
                i += 3
            elif s.startswith("$(", i):
                stack.append(["cmd"])
                i += 2
            elif c == "`":
                stack.append(["bt"])
                i += 1
            else:
                i += 1
            continue
        # command context: top / cmd / bt
        if c == "\n":
            if not pending:
                i += 1
                continue
            j = i + 1
            for doc, delim, strip in pending:
                doc.body_start = j
                while True:
                    nl = s.find("\n", j)
                    line = s[j:] if nl < 0 else s[j:nl]
                    if (line.lstrip("\t") if strip else line) == delim:
                        doc.body_end = j
                        j = n if nl < 0 else nl + 1
                        doc.after = j
                        break
                    if nl < 0:
                        return False, top, []  # unterminated heredoc
                    j = nl + 1
                docs.append(doc)
            pending = []
            i = j
            continue
        if c == "\\":
            i += 2
            continue
        if c == "#" and (i == 0 or s[i - 1] in " \t\n;&|()"):
            nl = s.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if c == "'" or s.startswith("$'", i):
            if c == "$":
                j = i + 2
                while j < n and s[j] != "'":
                    j += 2 if s[j] == "\\" else 1
            else:
                j = s.find("'", i + 1)
                j = n if j < 0 else j
            if j >= n:
                return False, top, []
            i = j + 1
            continue
        if c == '"':
            stack.append(["dq"])
            i += 1
            continue
        if s.startswith("$((", i):
            stack.append(["ar", 0])
            i += 3
            continue
        if s.startswith("((", i) and (i == 0 or s[i - 1] in " \t\n;&|("):
            stack.append(["ar", 0])
            i += 2
            continue
        if s.startswith("$(", i):
            stack.append(["cmd"])
            i += 2
            continue
        if c == "`":
            if ctx == "bt":
                stack.pop()
            else:
                stack.append(["bt"])
            i += 1
            continue
        if c == ")" and ctx == "cmd":
            stack.pop()
            i += 1
            continue
        if s.startswith("<(", i) or s.startswith(">(", i):
            # Process substitution is NOT modelled (r0b follow-up). A heredoc
            # opened inside `<( … )` would be marked top-level and
            # `_heredoc_pipeline_sinks` would attribute it to the OUTER
            # command. Until the context is tracked, prove nothing: the shared
            # representation stays truthful and both halves fail closed.
            return False, top, []
        if s.startswith("<<<", i):
            i += 3  # here-string: NOT a heredoc
            continue
        if s.startswith("<<", i):
            j = i + 2
            strip = j < n and s[j] == "-"
            if strip:
                j += 1
            while j < n and s[j] in " \t":
                j += 1
            parts: list[str] = []
            quoted = False
            while j < n and s[j] not in _WORD_END:
                ch = s[j]
                if ch in "'\"":
                    close = s.find(ch, j + 1)
                    if close < 0 or "\n" in s[j + 1 : close]:
                        return False, top, []
                    parts.append(s[j + 1 : close])
                    quoted = True
                    j = close + 1
                elif ch == "\\":
                    if j + 1 >= n:
                        return False, top, []
                    parts.append(s[j + 1])
                    quoted = True
                    j += 2
                elif ch in "$`":
                    # CORRECTED MODEL (r0b, round 3). Bash does NOT apply
                    # parameter / command / arithmetic expansion to a heredoc
                    # DELIMITER word — only quote removal. `<<$TAG` therefore
                    # opens a heredoc whose delimiter is the LITERAL text
                    # `$TAG`, and because that word carries no quoting the
                    # BODY stays expansion-bearing (`quoted` is not set). The
                    # old comment called this an "expanding delimiter" and
                    # refused; that refusal is what made the command unlexable
                    # and let the legacy fallback launder it.
                    #
                    # `$(`/`${` are still refused: `(` ends a word here
                    # (`_WORD_END`) so the parsed delimiter would be a
                    # truncated, wrong word — conservative refusal, not a
                    # claim about expansion.
                    if ch == "$" and j + 1 < n and s[j + 1] in "({":
                        return False, top, []
                    parts.append(ch)
                    j += 1
                else:
                    parts.append(ch)
                    j += 1
            word = "".join(parts)
            if not word:
                return False, top, []  # empty delimiter: fail closed
            pending.append((_Heredoc(i, quoted, not stack, j), word, strip))
            i = j
            continue
        i += 1
    if stack or pending:
        return False, top, []
    return True, top, docs


def _proven_heredocs(s: str) -> list[_Heredoc]:
    """Complete, syntactically real heredocs; ``[]`` on any lex uncertainty."""
    ok, _top, docs = _shell_lex(s)
    return docs if ok else []


# The Beat-7 commit form: `git commit -m "$(cat <<'TAG'` … `TAG` `)"`. The
# substitution's ONLY command is `cat` of a quote-delimited (literal) heredoc,
# and its output is exactly the git message word — proven prose, not code.
_GIT_MSG_CAT_SUBST = re.compile(
    r"(?:^|[;&|\n])[ \t]*(?P<git>git)[ \t]+commit\b[^;&|\n]*?"
    r"(?:-m|--message)(?:=|[ \t]+)\"\$\([ \t]*cat[ \t]+(?=<<)",
)
_SUBST_CLOSE = re.compile(r"[ \t\n]*\)\"(?=$|[ \t\n;&|])")


def _is_git_message_cat_substitution(s: str, top: list[bool], doc: _Heredoc) -> bool:
    """True only when the whole nested data-flow is PROVEN literally `cat`.

    r0b (blast-radius-ax round 2): the exception used to prove only the opener
    PREFIX (`git commit -m "$(cat <<`) and the substitution CLOSE (`)"`). It
    never proved there was no further consumer of the body, so a Beat-7-LOOKING
    opener with `| bash`, `| python` or `> payload.sh` on the opener line was
    masked unconditionally — an execution-bearing heredoc hidden from the
    infrastructure check. The accepted conservative form is literally only
    `cat`:

      * the prefix proves the substitution's first command is `cat` and its
        only operand is this heredoc;
      * NOTHING but whitespace may follow the parsed delimiter on the opener
        line (no pipeline, no redirection, no extra operand);
      * the substitution must close IMMEDIATELY after the terminator line, so
        no second command inside `$( )` consumes the body either.
    """
    if not doc.quoted or doc.top_level:
        return False
    if doc.tail_start < 0 or doc.body_start <= doc.tail_start:
        return False
    # Opener-line tail: everything from just past the delimiter word up to the
    # newline that starts the body. Must be whitespace only.
    tail = s[doc.tail_start : doc.body_start - 1]
    if tail.strip():
        return False
    for m in _GIT_MSG_CAT_SUBST.finditer(s, 0, doc.pos + 2):
        if m.end() == doc.pos and top[m.start("git")]:
            return bool(_SUBST_CLOSE.match(s, doc.after))
    return False


# Options a stage may carry BEFORE its subcommand. Anything not listed makes
# the subcommand unprovable, and an unprovable owner masks NOTHING.
_PRE_SUBCOMMAND_VALUE_OPTS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env", "-R", "--repo"},
)
_PRE_SUBCOMMAND_FLAG_OPTS: frozenset[str] = frozenset(
    {
        "--no-pager",
        "--paginate",
        "-P",
        "--bare",
        "--literal-pathspecs",
        "--no-literal-pathspecs",
        "--no-replace-objects",
        "--no-optional-locks",
    },
)
_COMMAND_WRAPPERS: frozenset[str] = frozenset({"sudo", "command", "env", "nohup", "time"})


def _stage_owner_keys_at(s: str, top: list[bool], pos: int) -> set[str]:
    """Owner keys for the top-level pipeline stage that contains ``pos``.

    Option spelling is not semantic authority across arbitrary binaries (r0b
    round 2), and the BASE COMMAND is not authority either (r0b round 3):
    `git`, `gh` and `glab` dispatch external subcommands, aliases and
    extensions that hand argv to another program, so `--format`/`--body` there
    mean whatever that program says — possibly a path. A flag mask must
    therefore be scoped to a KNOWN BUILTIN SUBCOMMAND + flag.

    Returns every key the stage could be matched on, most general first:
    ``{"gh", "gh pr", "gh pr create"}`` for `gh pr create …`. A caller's
    mapping opts in per key, so `git log`/`gh pr create` can be named while
    `git my-ext`/`gh extension exec` simply have no key in the table and mask
    nothing. Binary-global masks (curl/wget) keep naming the base alone.
    Returns ``set()`` when the owning command cannot be determined.
    """
    start = 0
    for i in range(pos - 1, -1, -1):
        if top[i] and s[i] in ";&|\n":
            start = i + 1
            break
    seg = _ENV_PREFIX_RE.sub("", s[start:pos])
    toks = seg.split()
    if not toks:
        return set()
    idx = 0
    base = _basename(toks[idx])
    while base in _COMMAND_WRAPPERS and idx + 1 < len(toks):
        idx += 1
        base = _basename(toks[idx])
    keys = {base}
    subs: list[str] = []
    j = idx + 1
    while j < len(toks) and len(subs) < 2:
        tok = toks[j]
        if tok.startswith("-"):
            name = tok.split("=", 1)[0]
            if "=" in tok or name in _PRE_SUBCOMMAND_FLAG_OPTS:
                j += 1
                continue
            if name in _PRE_SUBCOMMAND_VALUE_OPTS:
                j += 2
                continue
            # An option we cannot classify may or may not swallow the next
            # word, so the subcommand position is unprovable: stop, leaving
            # only the base key (which extensible families never name).
            return keys
        subs.append(tok.lower())
        j += 1
    for k in range(len(subs)):
        keys.add(base + " " + " ".join(subs[: k + 1]))
    return keys


def _normalise_owner_key(key: str) -> str:
    """`/usr/bin/git log` -> `git log`; the first word is path-normalised."""
    parts = key.split()
    if not parts:
        return ""
    return " ".join([_basename(parts[0])] + [p.lower() for p in parts[1:]])


def mask_infrastructure_data_windows(

    command: str,
    literal_value_flags: tuple[str, ...] | dict[str, tuple[str, ...]] = (),
) -> str:
    """Projection for the INFRASTRUCTURE-PROTECTION substring check.

    Masks (same length) ONLY:
      * proven-literal git message values whose `git` is top-level text;
      * bodies of complete, top-level, quote-delimited heredocs fed to a
        proven inert reader (the consumer law above), plus the Beat-7
        ``git commit -m "$(cat <<'TAG' … )"`` form;
      * the value word of a caller-named inert, non-path flag
        (``literal_value_flags``) when that word is proven literal.

    Unlike `mask_data_windows` it does NOT mask pattern-reader quoted
    operands (a quoted grep FILE operand is a path the check must see).
    Any lex uncertainty or internal error returns the RAW command.
    """
    s = command or ""
    if not s:
        return s
    try:
        ok, top, docs = _shell_lex(s)
        if not ok:
            return s
        buf = list(s)
        for m in _GIT_MSG_VALUE.finditer(s):
            if top[m.start("git")] and _git_message_is_literal(m.group("val")):
                _blank_span(buf, *m.span("val"))
        s_git = "".join(buf)
        for doc in docs:
            if doc.quoted and doc.top_level and _heredoc_is_inert(s_git, doc.pos):
                _blank_span(buf, doc.body_start, doc.body_end)
            elif _is_git_message_cat_substitution(s, top, doc):
                _blank_span(buf, doc.body_start, doc.body_end)
        if literal_value_flags:
            # SUBCOMMAND-SCOPED flag masks (r0b rounds 2-3). A mapping
            # {owner: flags} masks a flag's value only inside a stage whose
            # owner keys contain that owner. An owner is either a whole binary
            # (`curl`) or — for EXTENSIBLE families that dispatch external
            # subcommands/aliases/extensions — a proven builtin subcommand
            # (`git log`, `gh pr create`). An unknown subcommand produces no
            # matching key, so it masks nothing. A bare tuple keeps the legacy
            # global assertion for callers that have not been scoped yet.
            if isinstance(literal_value_flags, dict):
                owners: dict[str, set[str]] = {}
                for cmd_base, flags in literal_value_flags.items():
                    for f in flags:
                        owners.setdefault(f, set()).add(_normalise_owner_key(cmd_base))
            else:
                owners = {f: {"*"} for f in literal_value_flags}
            flag_re = re.compile(
                r"(?<!\S)(?P<flag>"
                + "|".join(re.escape(f) for f in sorted(owners, key=len, reverse=True))
                + r")(?:=|[ \t]+)(?P<val>(?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s;&|<>'\"])+)",
            )
            for m in flag_re.finditer(s):
                scope = owners.get(m.group("flag"), set())
                if "*" not in scope and not (scope & _stage_owner_keys_at(s, top, m.start())):
                    continue
                if top[m.start()] and _git_message_is_literal(m.group("val")):
                    _blank_span(buf, *m.span("val"))
        return "".join(buf)
    except Exception:
        return s


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
# `git -C <dir> --no-pager … <subcommand>` — the global-option run that may
# precede git's own subcommand. Kept deliberately small: only options git
# actually accepts before the subcommand.
_GIT_GLOBAL_OPTS = (
    r"(?:-C\s+\S+\s+|-c\s+\S+\s+|--git-dir[=\s]\S+\s+|--work-tree[=\s]\S+\s+"
    r"|--no-pager\s+|--paginate\s+|--literal-pathspecs\s+|--no-replace-objects\s+)*"
)
# #893 (2026-09-17): `git grep` is a pattern reader exactly like grep/rg — its
# quoted PATTERN is a search string that executes nothing. It was NOT in this
# family, so `git grep 'reset --hard'` / `git grep -n "reset\", \"--hard" path`
# left the pattern visible and the judge's git rules read the search text as a
# command. A measured read-only search was blocked as a mutation and froze the
# session. The reader list is inert-by-construction: every command here only
# reads.
_PATTERN_READER_CMD = re.compile(
    r"(?:^|[;&|\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:git\s+" + _GIT_GLOBAL_OPTS + r"grep|grep|egrep|fgrep|rg)\b",
    re.IGNORECASE,
)
# The history readers take a pattern too, but only via an explicit pattern
# FLAG — their bare operands are revisions/paths. So the window opens only
# when such a flag is present in the same segment; otherwise nothing is
# masked (fail-safe).
_GIT_PATTERN_READER_CMD = re.compile(
    r"(?:^|[;&|\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"git\s+" + _GIT_GLOBAL_OPTS + r"(?:log|show|diff|rev-list|shortlog|blame)\b",
    re.IGNORECASE,
)
_GIT_PATTERN_FLAG = re.compile(
    r"(?:--grep|--author|--committer|--grep-reflog)[=\s]|(?<![\w-])-[SGL](?=[\s'\"])",
)


def _literal_quoted_windows(s: str, start: int) -> tuple[list[tuple[int, int]], int]:
    """Proven-literal quoted spans from ``start`` to the end of the segment.

    Quote-aware forward walk (a `|` INSIDE the quoted pattern must not end
    the segment — `grep -E 'scp|ssh' f` is one segment). Unterminated
    quotes mask nothing (fail-safe: unmasked text can only grade stricter).
    Single quotes are always literal; a double-quoted chunk is literal only
    when it carries no `$` and no backtick (a `"$(…)"` pattern stays visible
    and fails closed). Returns (spans, index where the segment ended).
    """
    out: list[tuple[int, int]] = []
    n = len(s)
    i = start
    while i < n:
        c = s[i]
        if c == "'":
            close = s.find("'", i + 1)
            if close < 0:
                return out, n  # unterminated → leave visible
            out.append((i, close + 1))
            i = close + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                return out, n  # unterminated → leave visible
            inner = s[i + 1 : j]
            if "$" not in inner and "`" not in inner:
                out.append((i, j + 1))
            i = j + 1
            continue
        if c in ";&|\n":
            break  # unquoted separator ends the reader's segment
        i += 1
    return out, i


def _pattern_reader_quoted_windows(s: str) -> list[tuple[int, int]]:
    """Spans of proven-literal quoted chunks inside pattern-reader segments."""
    out: list[tuple[int, int]] = []
    for m in _PATTERN_READER_CMD.finditer(s):
        spans, _end = _literal_quoted_windows(s, m.end())
        out.extend(spans)
    for m in _GIT_PATTERN_READER_CMD.finditer(s):
        spans, end = _literal_quoted_windows(s, m.end())
        if not spans:
            continue
        if not _GIT_PATTERN_FLAG.search(s[m.end() : end]):
            continue  # no pattern flag → operands are refs/paths, not data
        out.extend(spans)
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
        # Only lexer-PROVEN, complete, top-level, quote-delimited heredocs
        # (blast-radius-ax: `<<<`, a quoted/commented `<<`, arithmetic `<<`
        # never open a window; any lex uncertainty masks no heredoc at all).
        s2 = "".join(buf)
        buf2 = list(s2)
        for doc in _proven_heredocs(s2):
            if doc.quoted and doc.top_level and _heredoc_is_inert(s2, doc.pos):
                _blank_span(buf2, doc.body_start, doc.body_end)
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
        for doc in _proven_heredocs(current):
            if doc.quoted:
                _blank_span(buf, doc.body_start, doc.body_end)

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
