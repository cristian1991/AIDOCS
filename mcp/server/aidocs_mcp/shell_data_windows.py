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
# git command's message is masked. The value is a quoted string (single or
# double, with escapes) or a single bare token.
_GIT_MSG_VALUE = re.compile(
    r"(?:^|[;&|\n])\s*git\s+(?:commit|tag|merge|notes|stash)\b"
    r"(?:(?![;&|\n]).)*?"
    r"(?:-m|--message)(?:=|\s+)"
    r"(?P<val>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s;&|]+)",
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
        seg = _ENV_PREFIX_RE.sub("", txt).strip()
        sinks.append(_basename(seg.split()[0]) if seg else "")
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


def mask_data_windows(command: str) -> str:
    """Return ``command`` with data-only windows blanked (same length).

    Execution-bearing tokens are untouched; only proven prose payloads (git
    message values + heredoc bodies) are blanked. Fail-safe: returns the input
    unchanged on any internal error."""
    s = command or ""
    if not s:
        return s
    try:
        buf = list(s)
        for m in _GIT_MSG_VALUE.finditer(s):
            a, b = m.span("val")
            _blank_span(buf, a, b)
        # Heredoc bodies (run on the git-masked text so positions still align).
        s2 = "".join(buf)
        buf2 = list(s2)
        for m in _HEREDOC.finditer(s2):
            if _heredoc_is_inert(s2, m.start()):
                a, b = m.span("body")
                _blank_span(buf2, a, b)
        return "".join(buf2)
    except Exception:
        return s
